"""Encaminhamento assistido: do pedido do paciente ao "Aceito" do terapeuta.

O objetivo é tirar da Thainá o trabalho de entrar no Hamilton e ligar paciente
a terapeuta. O caminho:

1. A rodada do cron extrai do que o paciente contou os critérios estruturados
   (formato, abordagem, horários) e registra o pedido no Hamilton
   (`registrar_pedidos`). A Thainá é avisada.
2. Na aba Hoje, a Thainá vê os terapeutas que servem — o Hamilton ordena — e
   escolhe um horário. O Hamilton reserva; a Sofia manda ao paciente o template
   `proposta_horario_paciente` (`propor_ao_paciente`).
3. Toque do paciente (`tratar_botao_paciente`): "Pode ser" manda ao terapeuta o
   template `encaminhamento_terapeuta`; "Prefiro outro" devolve à Thainá.
4. Toque do terapeuta (`tratar_botao_terapeuta`): "Aceito" faz o Hamilton gravar
   o vínculo, a sessão na grade e o Match. "Não aceito" devolve à Thainá e abre
   espaço para ele escrever o motivo, que é opcional (`capturar_motivo`).

**Quem guarda o estado e a reserva é o Hamilton**, dono da grade: uma reserva
que só existisse aqui não impediria outra tela de lá de alocar por cima. As
variáveis dos templates também vêm prontas de lá. A trava geral
(`ENCAMINHAMENTO_ATIVO`) também é do Hamilton: desligada, a rodada não faz nada.

Os códigos dos botões são "<código>:<id do encaminhamento>". Os do paciente
começam com `prop_`, os do terapeuta com `enc_`.
"""

import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models import Conversa
from app.services import conversation, escalation, hamilton_client, llm_client, whatsapp_client
from app.utils import mascarar_telefone

logger = logging.getLogger(__name__)

# Marca em `dados_coletados`: o pedido desta conversa já foi registrado no
# Hamilton (id do encaminhamento) ou não cabia (paciente antigo voltando).
CHAVE_PEDIDO = "encaminhamento_pedido"
# Só cadastros recentes viram pedido. Sem isto, ligar a trava faria a primeira
# rodada extrair critérios de toda conversa antiga — uma chamada de LLM cada.
JANELA_PEDIDO = timedelta(days=14)
PEDIDOS_POR_RODADA = 10

TEXTO_PACIENTE_SIM = (
    "Combinado! Já avisamos quem vai te atender. Assim que a confirmação chegar, "
    "te mandamos uma mensagem por aqui."
)
TEXTO_PACIENTE_NAO = (
    "Tudo bem! Vamos procurar outro horário e te mandamos uma nova proposta por aqui."
)
TEXTO_PROPOSTA_VENCIDA = (
    "Essa proposta não está mais valendo. Nossa equipe vai te mandar uma nova em breve."
)
TEXTO_PACIENTE_CONFIRMADO = (
    "Tudo certo! {terapeuta} confirmou o seu atendimento. A primeira sessão é {quando}. "
    "Antes disso, vocês vão combinar uma conversa rápida para se conhecerem."
)
TEXTO_TERAPEUTA_ACEITO = (
    "Obrigado! {paciente} já está vinculado(a) a você no Hamilton, com a primeira "
    "sessão na sua grade ({quando})."
)
TEXTO_TERAPEUTA_RECUSOU = (
    "Tudo bem, a equipe vai encaminhar para outra pessoa. Se quiser, responda esta "
    "mensagem contando o motivo (é opcional)."
)
TEXTO_MOTIVO_OBRIGADO = "Obrigado, já repassamos para a equipe."
TEXTO_SLOT_TOMADO = (
    "Esse horário foi ocupado na sua agenda enquanto esperávamos a resposta, então o "
    "encaminhamento não pôde ser concluído. A equipe vai falar com você."
)
TEXTO_ENCAMINHAMENTO_FECHADO = (
    "Esse encaminhamento não está mais aberto. Se tiver dúvida, fale com o Alan ou o Victor."
)

# O terapeuta que tocou em "Não aceito" tem um tempo para escrever o motivo. Na
# memória de propósito: o motivo é opcional, e perdê-lo num restart custa só o
# texto. Fora desse tempo, o que ele escrever volta a receber a resposta fixa.
_TEMPO_PARA_MOTIVO = 2 * 3600
_aguardando_motivo: dict[str, tuple[int, float]] = {}
# Reentrega da Meta e toque duplo não podem virar duas respostas.
_WAMID_GUARDADO_SEGUNDOS = 3600
_wamids_vistos: dict[str, float] = {}


def ler_botao(payload: str | None, prefixo: str) -> tuple[str, int] | None:
    """'prop_sim:42' → ('prop_sim', 42), se começar com `prefixo`."""
    if not payload or not payload.startswith(prefixo):
        return None
    codigo, _, enc_id = payload.partition(":")
    return (codigo, int(enc_id)) if enc_id.isdigit() else None


def botao_da_mensagem(mensagem: dict, prefixo: str) -> tuple[str, int] | None:
    """O botão de encaminhamento tocado nesta mensagem do webhook, se houver."""
    if mensagem.get("type") != "button":
        return None
    return ler_botao((mensagem.get("button") or {}).get("payload"), prefixo)


def _ja_visto(wamid: str | None) -> bool:
    agora = time.monotonic()
    for chave, quando in list(_wamids_vistos.items()):
        if agora - quando >= _WAMID_GUARDADO_SEGUNDOS:
            del _wamids_vistos[chave]
    if not wamid:
        return False
    if wamid in _wamids_vistos:
        return True
    _wamids_vistos[wamid] = agora
    return False


def texto_da_proposta(variaveis: list[str]) -> str:
    """O texto do template `proposta_horario_paciente`, para o histórico do
    painel mostrar o que o paciente recebeu."""
    nome, terapeuta, quando = (list(variaveis) + ["", "", ""])[:3]
    return (
        f"Olá, {nome}! Encontramos um horário para a sua terapia na Allos:\n\n"
        f"Terapeuta: {terapeuta}\nPrimeira sessão: {quando}\n\n"
        "Esse horário funciona para você?"
    )


def _so_digitos(numero: str | None) -> str:
    return re.sub(r"\D", "", numero or "")


async def conversa_do_paciente(db: AsyncSession, paciente: dict) -> Conversa | None:
    """A conversa do paciente: pela ficha do Hamilton e, se não achar, pelo
    telefone (o Hamilton guarda sem DDI; o WhatsApp manda com 55)."""
    pid = paciente.get("id")
    if pid:
        achada = (
            await db.execute(select(Conversa).where(Conversa.paciente_hamilton_id == pid))
        ).scalars().first()
        if achada is not None:
            return achada
    telefone = hamilton_client.normalizar_telefone(paciente.get("telefone"))
    if not telefone:
        return None
    for conversa in (await db.execute(select(Conversa))).scalars().all():
        if hamilton_client.normalizar_telefone(conversa.numero_whatsapp) == telefone:
            return conversa
    return None


# --- 1. Pedido ---------------------------------------------------------------

INSTRUCAO_CRITERIOS = """Você lê a conversa de uma pessoa que procura terapia na Allos e extrai o \
que ela pediu para escolhermos o terapeuta. Responda SÓ com um JSON, sem texto em volta:

{"modalidade": "online" | "presencial" | null,
 "abordagem_id": <número da lista abaixo> | null,
 "horarios": [{"dia_semana": 0-6, "hora_inicio": "HH:MM"}],
 "especialidade": "<tema ou público pedido, ex.: luto, ansiedade, criança>" | null}

Regras:
- dia_semana: 0 = segunda ... 6 = domingo. Uma entrada por horário possível que a pessoa disse.
  "Terças e quintas à noite" vira terça 19:00 e quinta 19:00. Manhã = 09:00, tarde = 14:00,
  noite = 19:00 quando ela não disser a hora. Hora em múltiplos de 30 minutos.
- abordagem_id: só se ela pediu uma abordagem da lista. Nunca invente.
- Use null (ou lista vazia) para o que ela não disse. Não deduza preferência que não foi dita.

Abordagens: {abordagens}
"""


def _validar_criterios(bruto: str | None, abordagens: dict[int, str]) -> dict:
    """JSON do modelo → critérios que o Hamilton aceita. O que não passar some."""
    if not bruto:
        return {}
    texto = bruto.strip().removeprefix("```json").removeprefix("```").removesuffix("```")
    try:
        dados = json.loads(texto)
    except json.JSONDecodeError:
        logger.warning("Extração de critérios devolveu JSON inválido")
        return {}
    if not isinstance(dados, dict):
        return {}
    criterios: dict = {}
    if dados.get("modalidade") in ("online", "presencial"):
        criterios["modalidade"] = dados["modalidade"]
    if isinstance(dados.get("abordagem_id"), int) and dados["abordagem_id"] in abordagens:
        criterios["abordagem_id"] = dados["abordagem_id"]
    horarios = []
    for h in dados.get("horarios") or []:
        if not isinstance(h, dict):
            continue
        dia, hora = h.get("dia_semana"), str(h.get("hora_inicio") or "")
        m = re.fullmatch(r"(\d{1,2}):(\d{2})", hora)
        if isinstance(dia, int) and 0 <= dia <= 6 and m and int(m[1]) < 24 and m[2] in ("00", "30"):
            horarios.append({"dia_semana": dia, "hora_inicio": f"{int(m[1]):02d}:{m[2]}"})
    if horarios:
        criterios["horarios"] = horarios
    if isinstance(dados.get("especialidade"), str) and dados["especialidade"].strip():
        criterios["especialidade"] = dados["especialidade"].strip()[:200]
    return criterios


def _fala_original(conversa: Conversa) -> str:
    """O que o paciente disse sobre horário e preferência, como ele disse. A
    Thainá lê isto na tela quando a extração não pegar algo."""
    dados = conversa.dados_coletados or {}
    partes = [
        ("Horários", dados.get("horarios_disponiveis")),
        ("Preferência", dados.get("preferencia_terapeuta")),
        ("Motivo", dados.get("motivo_busca")),
        ("Observações", dados.get("observacoes")),
    ]
    return " | ".join(f"{rotulo}: {valor}" for rotulo, valor in partes if valor)[:1500]


async def extrair_criterios(db: AsyncSession, conversa: Conversa, abordagens: dict[int, str]) -> dict:
    """Critérios estruturados + a fala original. Se o modelo falhar, vai só a
    fala: o pedido entra na fila do mesmo jeito e a Thainá ajusta na tela."""
    criterios: dict = {}
    historico = await conversation.carregar_historico(db, conversa)
    transcricao = "\n".join(
        f"{'Pessoa' if m.get('role') == 'user' else 'Sofia'}: {m.get('content')}"
        for m in historico
        if m.get("role") in ("user", "assistant") and m.get("content")
    )
    contexto = "\n\n".join(p for p in (transcricao, _fala_original(conversa)) if p)
    if contexto.strip():
        lista = "; ".join(f"{pk} = {nome}" for pk, nome in sorted(abordagens.items()))
        try:
            resposta = await llm_client.get_llm_client().gerar_resposta(
                [{"role": "user", "content": contexto}],
                system_prompt=INSTRUCAO_CRITERIOS.replace("{abordagens}", lista or "(nenhuma)"),
            )
            criterios = _validar_criterios(resposta.texto, abordagens)
        except llm_client.LLMError:
            logger.warning("LLM falhou ao extrair critérios da conversa %s", conversa.id)
    criterios["texto_original"] = _fala_original(conversa)
    return criterios


async def _conversas_sem_pedido(db: AsyncSession, agora: datetime) -> list[Conversa]:
    candidatas = (
        await db.execute(
            select(Conversa)
            .where(Conversa.paciente_hamilton_id.is_not(None))
            .where(Conversa.eh_terapeuta.is_(False))
            .where(Conversa.cadastrado_em >= agora - JANELA_PEDIDO)
            .order_by(Conversa.cadastrado_em)
        )
    ).scalars().all()
    return [c for c in candidatas if CHAVE_PEDIDO not in (c.dados_coletados or {})][:PEDIDOS_POR_RODADA]


async def registrar_pedidos(db: AsyncSession, agora: datetime | None = None) -> dict:
    """Registra no Hamilton o pedido de quem foi cadastrado e ainda não tem.

    Fica na rodada do cron, e não no cadastro, por dois motivos: a extração é
    uma chamada a mais ao modelo, que somaria segundos à resposta que o
    paciente está esperando; e o que falhar aqui (Hamilton fora) é tentado de
    novo na rodada seguinte, sem ninguém precisar lembrar."""
    agora = agora or datetime.now(timezone.utc)
    conversas = await _conversas_sem_pedido(db, agora)
    if not conversas:
        return {"registrados": 0}
    cliente = hamilton_client.get_hamilton_client()
    try:
        abordagens = {
            a["pk_abordagem"]: a["abordagem"]
            for a in await cliente.listar_abordagens()
            if "pk_abordagem" in a and "abordagem" in a
        }
    except hamilton_client.HamiltonError as exc:
        logger.error("Encaminhamento: sem lista de abordagens, tenta na próxima rodada: %s", exc)
        return {"erro": "hamilton_indisponivel"}

    registrados = ignorados = 0
    for conversa in conversas:
        criterios = await extrair_criterios(db, conversa, abordagens)
        try:
            status, corpo = await cliente.registrar_pedido_encaminhamento(
                conversa.paciente_hamilton_id, criterios)
        except hamilton_client.HamiltonError as exc:
            logger.error("Pedido de encaminhamento da conversa %s não registrado: %s", conversa.id, exc)
            continue
        if status == 200:
            marca = (corpo.get("encaminhamento") or {}).get("id")
            registrados += 1
            nome = (conversa.dados_coletados or {}).get("nome_completo") or conversa.numero_whatsapp
            await escalation.alertar_equipe(db, nome, "encaminhamento: escolher terapeuta (aba Hoje)")
        else:
            # 409: já tem terapeuta (paciente antigo voltando); 404: ficha sumiu.
            marca = corpo.get("codigo") or f"http_{status}"
            ignorados += 1
        conversa.dados_coletados = {**(conversa.dados_coletados or {}), CHAVE_PEDIDO: marca}
        await db.commit()
    return {"registrados": registrados, "ignorados": ignorados}


# --- 2. Proposta ao paciente -------------------------------------------------

async def propor_ao_paciente(db: AsyncSession, enc: dict) -> bool:
    """Depois da reserva: manda a proposta de horário ao paciente (template,
    porque a escolha costuma vir fora da janela de 24h). Devolve se saiu."""
    mensagem = (enc.get("mensagens") or {}).get("paciente")
    paciente = enc.get("paciente") or {}
    if not mensagem:
        return False
    conversa = await conversa_do_paciente(db, paciente)
    numero = conversa.numero_whatsapp if conversa else None
    if not numero:
        digitos = _so_digitos(paciente.get("telefone"))
        numero = f"55{digitos}" if len(digitos) in (10, 11) else None
    if not numero:
        logger.error("Encaminhamento %s: paciente sem telefone; proposta não enviada", enc.get("id"))
        return False
    try:
        resposta = await whatsapp_client.enviar_template(
            numero, mensagem["template"], mensagem["variaveis"], botoes=mensagem["botoes"])
    except whatsapp_client.WhatsAppError as exc:
        logger.error("Proposta do encaminhamento %s não saiu: %s", enc.get("id"), exc)
        return False
    if conversa is not None:
        wamid = ((resposta or {}).get("messages") or [{}])[0].get("id")
        await conversation.registrar_mensagem_enviada(
            db, conversa, texto_da_proposta(mensagem["variaveis"]),
            origem="bot", tipo="template", whatsapp_message_id=wamid)
        await db.commit()
    return True


# --- 3. Resposta do paciente -------------------------------------------------

async def _alertar_conversa(db: AsyncSession, conversa: Conversa, rotulo: str) -> None:
    nome = (conversa.dados_coletados or {}).get("nome_completo") or conversa.numero_whatsapp
    await escalation.alertar_equipe(db, nome, rotulo)


async def tratar_botao_paciente(db: AsyncSession, conversa: Conversa, codigo: str, enc_id: int) -> str:
    """Toque do paciente na proposta. Devolve o texto a responder a ele (a
    janela de 24h foi aberta pelo toque, então vai como texto livre)."""
    aceitou = codigo == "prop_sim"
    cliente = hamilton_client.get_hamilton_client()
    try:
        status, corpo = await cliente.responder_encaminhamento(enc_id, "paciente", aceitou)
    except hamilton_client.HamiltonError as exc:
        logger.error("Resposta do paciente ao encaminhamento %s não registrada: %s", enc_id, exc)
        await _alertar_conversa(db, conversa, "encaminhamento: resposta do paciente não registrada, conferir")
        return "Recebi! Nossa equipe vai confirmar e te responde por aqui."
    if status != 200:
        return TEXTO_PROPOSTA_VENCIDA

    enc = corpo.get("encaminhamento") or {}
    if not aceitou:
        await _alertar_conversa(db, conversa, "encaminhamento: paciente pediu outro horário (aba Hoje)")
        return TEXTO_PACIENTE_NAO

    aviso = (enc.get("mensagens") or {}).get("terapeuta")
    enviado = False
    if aviso and aviso.get("telefone"):
        try:
            await whatsapp_client.enviar_template(
                aviso["telefone"], aviso["template"], aviso["variaveis"], botoes=aviso["botoes"])
            enviado = True
        except whatsapp_client.WhatsAppError as exc:
            logger.error("Aviso ao terapeuta do encaminhamento %s não saiu: %s", enc_id, exc)
    if not enviado:
        # Sem o aviso o terapeuta nunca responde, e a reserva só cairia no prazo.
        await _alertar_conversa(db, conversa, "encaminhamento: aviso ao terapeuta NÃO saiu, avisar à mão")
    return TEXTO_PACIENTE_SIM


# --- 4. Resposta do terapeuta ------------------------------------------------

async def _avisar_paciente_confirmado(enc: dict) -> None:
    """Conta ao paciente que deu certo. Texto livre: costuma estar dentro da
    janela aberta pelo toque dele em "Pode ser"; se não estiver, a Meta recusa
    e fica no log (o terapeuta vai procurá-lo de qualquer jeito)."""
    variaveis = ((enc.get("mensagens") or {}).get("paciente") or {}).get("variaveis") or []
    if len(variaveis) < 3:
        return
    texto = TEXTO_PACIENTE_CONFIRMADO.format(terapeuta=variaveis[1], quando=variaveis[2])
    async with async_session() as db:
        conversa = await conversa_do_paciente(db, enc.get("paciente") or {})
        if conversa is None:
            return
        try:
            resposta = await whatsapp_client.enviar_texto(conversa.numero_whatsapp, texto)
        except whatsapp_client.WhatsAppError as exc:
            logger.warning("Confirmação ao paciente do encaminhamento %s não saiu: %s", enc.get("id"), exc)
            return
        wamid = ((resposta or {}).get("messages") or [{}])[0].get("id")
        await conversation.registrar_mensagem_enviada(db, conversa, texto, whatsapp_message_id=wamid)
        await db.commit()


async def _alertar(nome: str, rotulo: str) -> None:
    async with async_session() as db:
        await escalation.alertar_equipe(db, nome, rotulo)


async def tratar_botao_terapeuta(numero: str, wamid: str | None, codigo: str, enc_id: int) -> None:
    """Toque do terapeuta no aviso de encaminhamento."""
    if wamid:
        await whatsapp_client.marcar_como_lida(wamid)
    if _ja_visto(wamid):
        return
    aceitou = codigo == "enc_aceito"
    cliente = hamilton_client.get_hamilton_client()
    try:
        status, corpo = await cliente.responder_encaminhamento(enc_id, "terapeuta", aceitou)
    except hamilton_client.HamiltonError as exc:
        logger.error("Resposta do terapeuta %s ao encaminhamento %s falhou: %s",
                     mascarar_telefone(numero), enc_id, exc)
        _wamids_vistos.pop(wamid or "", None)  # deixa tocar de novo: não foi culpa dele
        await whatsapp_client.enviar_texto(
            numero, "Não consegui registrar agora. Tente tocar de novo em alguns minutos.")
        return

    enc = corpo.get("encaminhamento") or {}
    nome_paciente = (enc.get("paciente") or {}).get("nome") or "Paciente"
    if status == 409 and corpo.get("codigo") == "slot_tomado":
        await whatsapp_client.enviar_texto(numero, TEXTO_SLOT_TOMADO)
        await _alertar(nome_paciente, "encaminhamento: horário ocupado antes do aceite, escolher de novo")
        return
    if status != 200:
        await whatsapp_client.enviar_texto(numero, TEXTO_ENCAMINHAMENTO_FECHADO)
        return

    variaveis = ((enc.get("mensagens") or {}).get("paciente") or {}).get("variaveis") or ["", "", ""]
    if aceitou:
        await whatsapp_client.enviar_texto(
            numero, TEXTO_TERAPEUTA_ACEITO.format(paciente=nome_paciente, quando=variaveis[2]),
            sigiloso=True)
        await _avisar_paciente_confirmado(enc)
        logger.info("Encaminhamento %s aceito pelo terapeuta; vínculo gravado no Hamilton", enc_id)
        return
    _aguardando_motivo[numero] = (enc_id, time.monotonic())
    await whatsapp_client.enviar_texto(numero, TEXTO_TERAPEUTA_RECUSOU)
    await _alertar(nome_paciente, "encaminhamento: terapeuta recusou, escolher outro (aba Hoje)")


async def capturar_motivo(numero: str, texto: str | None) -> bool:
    """Se este terapeuta acabou de recusar, o texto dele é o motivo. Devolve se
    tratou (senão, quem chama segue com a resposta fixa de sempre)."""
    pendente = _aguardando_motivo.get(numero)
    if not pendente or not (texto or "").strip():
        return False
    enc_id, quando = pendente
    if time.monotonic() - quando > _TEMPO_PARA_MOTIVO:
        _aguardando_motivo.pop(numero, None)
        return False
    _aguardando_motivo.pop(numero, None)
    try:
        await hamilton_client.get_hamilton_client().motivo_encaminhamento(enc_id, texto.strip())
    except hamilton_client.HamiltonError as exc:
        logger.error("Motivo do encaminhamento %s não gravado: %s", enc_id, exc)
    await whatsapp_client.enviar_texto(numero, TEXTO_MOTIVO_OBRIGADO)
    return True


# --- Rodada do cron ----------------------------------------------------------

async def rodar(db: AsyncSession) -> dict:
    """Solta as reservas vencidas e registra os pedidos novos. Com a trava do
    Hamilton desligada, não faz nada."""
    cliente = hamilton_client.get_hamilton_client()
    try:
        expiracao = await cliente.expirar_encaminhamentos()
    except hamilton_client.HamiltonError as exc:
        logger.error("Encaminhamento: Hamilton fora, tenta na próxima rodada: %s", exc)
        return {"erro": "hamilton_indisponivel"}
    if not expiracao.get("ativo"):
        return {"ativo": False}
    for enc in expiracao.get("expirados") or []:
        nome = (enc.get("paciente") or {}).get("nome") or "Paciente"
        await escalation.alertar_equipe(db, nome, "encaminhamento sem resposta no prazo, escolher de novo")
    return {
        "ativo": True,
        "expirados": len(expiracao.get("expirados") or []),
        **await registrar_pedidos(db),
    }


# --- Painel: a tela em que a Thainá escolhe ----------------------------------

DIAS_CURTOS = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]
DIAS_LONGOS = ["Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado", "Domingo"]


def hora_legivel(hhmm: str | None) -> str:
    """'19:00' → '19h'; '18:30' → '18h30'."""
    m = re.fullmatch(r"(\d{1,2}):(\d{2})(?::\d{2})?", hhmm or "")
    if not m:
        return hhmm or ""
    return f"{int(m[1])}h" if m[2] == "00" else f"{int(m[1])}h{m[2]}"


def proxima_data(dia_semana: int, hoje=None):
    """Próxima data (a partir de amanhã) no dia da semana — a mesma regra do
    Hamilton para a 1ª sessão, para o campo já vir preenchido certo."""
    from datetime import date as _date
    dia = (hoje or _date.today()) + timedelta(days=1)
    return dia + timedelta(days=(dia_semana - dia.weekday()) % 7)


def etiquetas(criterios: dict, abordagens: dict[int, str] | None = None) -> list[dict]:
    """O pedido em etiquetas curtas para a fila. `filtra` diz se o critério
    entra no match ou é só leitura (especialidade, enquanto não há cadastro)."""
    abordagens = abordagens or {}
    saida = []
    if criterios.get("modalidade"):
        saida.append({"texto": criterios["modalidade"], "filtra": True})
    if criterios.get("abordagem_id"):
        nome = abordagens.get(criterios["abordagem_id"], f"abordagem {criterios['abordagem_id']}")
        saida.append({"texto": nome, "filtra": True})
    horarios = criterios.get("horarios") or []
    if horarios:
        saida.append({"texto": ", ".join(
            f"{DIAS_CURTOS[h['dia_semana']]} {hora_legivel(h['hora_inicio'])}" for h in horarios[:4]
        ) + (" …" if len(horarios) > 4 else ""), "filtra": True})
    if criterios.get("especialidade"):
        saida.append({"texto": criterios["especialidade"], "filtra": False})
    return saida


def _quando(iso: str | None):
    if not iso:
        return None
    try:
        valor = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    return valor if valor.tzinfo else valor.replace(tzinfo=timezone.utc)


async def _mapa_abordagens(cliente) -> dict[int, str]:
    try:
        return {a["pk_abordagem"]: a["abordagem"] for a in await cliente.listar_abordagens()
                if "pk_abordagem" in a}
    except hamilton_client.HamiltonError:
        return {}


_ERRO_FILA = "Não consegui buscar os encaminhamentos no Hamilton agora."


async def fila_para_painel(cliente=None) -> dict:
    """A fila do Hamilton pronta para a tela. **Nunca levanta**: qualquer defeito
    vira `erro`, e a aba Hoje — a home do painel — segue de pé com o resto.

    O `except Exception` é de propósito: não é só o Hamilton fora (`HamiltonError`).
    Uma resposta 200 com formato inesperado também derrubava a home inteira com 500,
    e esta fila é o pedaço menos importante da tela."""
    cliente = cliente or hamilton_client.get_hamilton_client()
    try:
        return await _fila_para_painel(cliente)
    except Exception as exc:  # noqa: BLE001 — ver docstring
        logger.error("Fila de encaminhamento indisponível: %s", exc, exc_info=True)
        return {"ativo": None, "itens": [], "erro": _ERRO_FILA}


async def _fila_para_painel(cliente) -> dict:
    fila = await cliente.fila_encaminhamentos()
    if not fila.get("ativo"):
        return {"ativo": False, "itens": [], "erro": None}
    itens_brutos = list(fila.get("itens") or [])
    abordagens = await _mapa_abordagens(cliente) if any(
        (i.get("criterios") or {}).get("abordagem_id") for i in itens_brutos) else {}
    itens = []
    for i in itens_brutos:
        enc, paciente, recusa = i.get("encaminhamento") or {}, i.get("paciente") or {}, i.get("ultima_recusa")
        itens.append({
            "id": enc.get("id"),
            "nome": paciente.get("nome") or "Paciente",
            "telefone": paciente.get("telefone"),
            "etiquetas": etiquetas(i.get("criterios") or {}, abordagens),
            "quando": _quando(enc.get("created_at")),
            "recusa": {
                "status": recusa.get("status_label"),
                "terapeuta": (recusa.get("terapeuta") or {}).get("nome"),
                "motivo": recusa.get("motivo_recusa"),
            } if recusa else None,
        })
    return {"ativo": True, "itens": itens, "erro": None}


async def item_da_fila(enc_id: int, cliente=None) -> dict | None:
    """O item bruto da fila (paciente, encaminhamento, critérios, recusa)."""
    cliente = cliente or hamilton_client.get_hamilton_client()
    fila = await cliente.fila_encaminhamentos()
    for i in fila.get("itens") or []:
        if (i.get("encaminhamento") or {}).get("id") == enc_id:
            return i
    return None


def criterios_do_formulario(modalidade: str, abordagem_id: str, dias: list[str],
                            horas: list[str], anteriores: dict) -> dict:
    """O que a Thainá ajustou na tela, no formato do Hamilton. Especialidade e
    fala original vêm do pedido: ela não edita, só lê."""
    criterios = {k: anteriores[k] for k in ("especialidade", "texto_original") if anteriores.get(k)}
    if modalidade in ("online", "presencial"):
        criterios["modalidade"] = modalidade
    if abordagem_id.isdigit():
        criterios["abordagem_id"] = int(abordagem_id)
    horarios = []
    for dia, hora in zip(dias, horas):
        if dia.isdigit() and 0 <= int(dia) <= 6 and re.fullmatch(r"\d{2}:\d{2}", hora or ""):
            horarios.append({"dia_semana": int(dia), "hora_inicio": hora})
    if horarios:
        criterios["horarios"] = horarios
    return criterios


def preparar_candidatos(candidatos: list[dict], hoje=None) -> list[dict]:
    """Candidatos do Hamilton com rótulos de horário, horas livres em português
    (vírgula decimal) e a data sugerida para a 1ª sessão de cada slot."""
    saida = []
    for c in candidatos:
        slots = []
        for s in c.get("slots") or []:
            slots.append({
                **s,
                "rotulo": f"{DIAS_CURTOS[s['dia_semana']]} {hora_legivel(s['hora_inicio'])}",
                "rotulo_longo": f"{DIAS_LONGOS[s['dia_semana']]}, {hora_legivel(s['hora_inicio'])}",
                "exato": s.get("distancia") == 0,
                "proxima": proxima_data(s["dia_semana"], hoje).isoformat(),
            })
        horas = c.get("horas_livres") or 0
        saida.append({**c, "slots": slots, "horas": f"{horas:.1f}".replace(".", ",")})
    return saida
