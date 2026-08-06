"""Pesquisa de satisfação conduzida pela Sofia no WhatsApp.

Substitui o trabalho que a equipe de Qualidade fazia à mão: abordar o paciente,
fazer as perguntas uma a uma e anotar as respostas fora do sistema.

## Como a Sofia descobre que tem pesquisa pra fazer

Ela **pergunta**, não é avisada. O Hamilton já cria a `Avaliacao` com
`status='pendente'` por gatilho interno quando o terapeuta lança a primeira
consulta ou registra uma alta/desistência — ou seja, **a fila já existe lá**.
A Sofia lê essa fila no mesmo cron que já roda os follow-ups.

A alternativa (o Hamilton chamar a Sofia no momento do salvamento) foi
descartada: o Hamilton é síncrono, sem fila nem worker, então a chamada rodaria
dentro do request do terapeuta salvando o prontuário — que ficaria esperando a
Sofia acordar. Puxar em vez de empurrar também dá retry de graça: o pendente
continua lá até ser respondido.

## Como a pesquisa acontece

A conversa entra em "modo pesquisa" (`conversa.pesquisa_avaliacao_id`) e o turno
do bot passa a rodar com o prompt da pesquisa em vez do prompt de acolhimento —
a pessoa já é paciente, não é mais um lead a ser qualificado e cadastrado.

O modelo conduz e o modelo extrai: no fim, uma chamada separada transforma a
transcrição no JSON das respostas, que vai pro Hamilton por PATCH. Isso é uma
decisão consciente, com um custo conhecido: nada garante que todas as perguntas
sejam feitas nem que cada resposta seja lida corretamente, e ninguém confere
depois. Se aparecer dado incompleto ou impreciso na `Avaliacao`, é aqui que se
mexe (o conserto é registrar cada resposta por tool, em vez de extrair no fim).

## Prazos

Lembrete único em 20h de silêncio, encerramento em 44h. Os dois ficam colados na
janela de 24h da Meta: passada ela, texto livre não sai mais (só template), então
o lembrete tem que ser antes — e o encerramento é só marcação interna, não
precisa de mensagem.
"""

import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Conversa
from app.services import config_prompt, conversation, hamilton_client, llm_client, whatsapp_client
from app.utils import mascarar_telefone

logger = logging.getLogger(__name__)

# Silêncio até insistir uma única vez, e até desistir. Ver a nota sobre a janela
# de 24h da Meta no topo do módulo.
HORAS_LEMBRETE = 20
HORAS_ENCERRAMENTO = 44

MOMENTO_PRIMEIRA_SESSAO = "No início do processo (primeira sessão)"
MOMENTO_ENCERRAMENTO = "Após o encerramento da terapia"

# Texto do lembrete. Fixo e sem LLM: é um empurrãozinho, não um turno de
# conversa, e insistir com criatividade seria pressão.
LEMBRETE_TEXTO = (
    "Oi, passando pra lembrar da pesquisa que te mandei. "
    "Se puder responder, ajuda demais a gente a melhorar. "
    "Se preferir deixar pra lá, tudo bem também."
)


class PesquisaError(Exception):
    """Falha ao conduzir ou registrar a pesquisa."""


def _agora() -> datetime:
    return datetime.now(timezone.utc)


def _aware(valor: datetime | None) -> datetime | None:
    """Datas vindas do SQLite voltam sem tzinfo; compara-las com aware estoura."""
    if valor is not None and valor.tzinfo is None:
        return valor.replace(tzinfo=timezone.utc)
    return valor


def em_pesquisa(conversa: Conversa) -> bool:
    return conversa.pesquisa_avaliacao_id is not None


# --------------------------------------------------------------------------- #
# Montagem do prompt
# --------------------------------------------------------------------------- #


def _contexto_encerramento(avaliacao: dict) -> str:
    """Instrução de tom pra pesquisa de encerramento.

    O texto muda conforme o tipo de saída e quem a iniciou. Não é preciosismo:
    perguntar "por que você decidiu interromper?" a quem foi desligado pelo
    terapeuta é factualmente errado, e tratar um reencaminhamento como saída é
    pior ainda — a pessoa continua na Allos, só vai trocar de terapeuta.
    """
    tipo = (avaliacao.get("tipo_saida") or "").strip().lower()
    cancelador = (avaliacao.get("cancelador") or "").strip().lower()
    partes: list[str] = []

    if "reencaminhamento" in tipo:
        partes.append(
            "ESTE CASO É UM REENCAMINHAMENTO: a pessoa NÃO está saindo da Allos, ela vai "
            "trocar de terapeuta e continua sendo atendida. Deixe isso claro logo no começo, "
            "com naturalidade. NÃO pergunte por que ela decidiu interromper (ela não "
            "interrompeu nada). Pergunte como foi a experiência com o terapeuta anterior. "
            "Diga que a Thainá, coordenadora clínica, vai entrar em contato pra combinar os "
            "detalhes e tirar dúvidas, sem prometer prazo."
        )
    elif tipo == "alta":
        partes.append(
            "ESTE CASO É UMA ALTA: o processo terminou bem, foi uma conclusão, não um "
            "abandono. Reconheça isso com leveza. NÃO pergunte por que ela decidiu "
            "interromper — pergunte como foi a experiência ao longo do processo."
        )
    elif "não responde" in tipo or "nao responde" in tipo:
        partes.append(
            "O terapeuta registrou que a pessoa parou de responder. Aborde sem cobrança e "
            "sem culpa: só diga que sentiu falta dela por aqui e que gostaria de saber como "
            "foi a experiência. Se ela não responder, tudo bem."
        )
    else:
        partes.append(
            "Pergunte, com cuidado e sem cobrança, o motivo pelo qual a pessoa decidiu "
            "interromper o processo."
        )

    if cancelador == "terapeuta":
        partes.append(
            "ATENÇÃO: quem encerrou foi o terapeuta, não a pessoa. NUNCA pergunte por que "
            "ELA decidiu interromper — seria factualmente errado e soaria como cobrança."
        )
    return "\n".join(partes)


def montar_prompt(avaliacao: dict) -> str:
    """System prompt do turno de pesquisa (substitui o prompt de acolhimento)."""
    base = config_prompt.texto("prompt_pesquisa")
    momento = avaliacao.get("momento") or ""
    nome = (avaliacao.get("paciente_nome") or "").split(" ")[0]
    terapeuta = avaliacao.get("terapeuta_nome") or "o terapeuta"

    if momento == MOMENTO_ENCERRAMENTO:
        roteiro = config_prompt.texto("prompt_pesquisa_encerramento")
        contexto = _contexto_encerramento(avaliacao)
    else:
        roteiro = config_prompt.texto("prompt_pesquisa_primeira_sessao")
        contexto = (
            "A pessoa acabou de ter a primeira sessão. Esta é uma pesquisa de "
            "acompanhamento do início do processo."
        )

    return (
        f"{base}\n\n"
        f"## Contexto deste atendimento\n\n"
        f"Primeiro nome da pessoa: {nome or '(desconhecido)'}\n"
        f"Terapeuta dela: {terapeuta}\n\n"
        f"{contexto}\n\n"
        f"## Roteiro de perguntas\n\n{roteiro}"
    )


# --------------------------------------------------------------------------- #
# Ciclo de vida da pesquisa
# --------------------------------------------------------------------------- #


async def iniciar(
    db: AsyncSession, conversa: Conversa, avaliacao: dict, agora: datetime | None = None
) -> bool:
    """Manda o convite da pesquisa e coloca a conversa em modo pesquisa.

    Só marca a conversa se o convite realmente saiu: falha de envio deixa a
    avaliação pendente e ela volta na próxima rodada.
    """
    agora = agora or _agora()
    pk = avaliacao.get("pk_avaliacao")
    if not pk:
        return False

    try:
        resposta = await _turno(conversa, avaliacao, historico=[], abertura=True)
    except llm_client.LLMError as exc:
        logger.error("Não consegui gerar o convite da pesquisa %s: %s", pk, exc)
        return False

    if not await _enviar(db, conversa, resposta):
        return False

    conversa.pesquisa_avaliacao_id = pk
    conversa.pesquisa_iniciada_em = agora
    await db.flush()

    # Marca no Hamilton que já abordamos: 'pendente' quer dizer "sem resposta",
    # não "sem envio" — sem isso a pessoa seria abordada de novo a cada rodada.
    try:
        await hamilton_client.get_hamilton_client().atualizar_avaliacao(
            pk, {"sofia_enviada_em": agora.isoformat()}
        )
    except hamilton_client.HamiltonError as exc:
        logger.error("Pesquisa %s enviada mas não marcada no Hamilton: %s", pk, exc)
    await db.commit()
    return True


async def responder(db: AsyncSession, conversa: Conversa, numero: str) -> None:
    """Conduz um turno da pesquisa (chamado no lugar do turno normal do bot)."""
    avaliacao = await _buscar_avaliacao(conversa)
    if avaliacao is None:
        # Sem contexto não dá pra conduzir; encerra o modo pesquisa e deixa a
        # conversa voltar ao fluxo normal em vez de travar o paciente.
        logger.warning(
            "Pesquisa %s sumiu do Hamilton; saindo do modo pesquisa", conversa.pesquisa_avaliacao_id
        )
        await _limpar(db, conversa)
        return

    historico = await conversation.carregar_historico(db, conversa)
    try:
        resposta = await _turno(conversa, avaliacao, historico=historico)
    except llm_client.LLMError:
        logger.error("LLM falhou no turno da pesquisa da conversa %s", conversa.id)
        return

    concluida = _marcador_de_fim(resposta)
    await _enviar(db, conversa, _sem_marcador(resposta))
    if concluida:
        await finalizar(db, conversa, avaliacao, recusou=concluida == "recusou")


async def finalizar(
    db: AsyncSession, conversa: Conversa, avaliacao: dict, recusou: bool = False
) -> None:
    """Extrai as respostas da conversa, grava no Hamilton e sai do modo pesquisa.

    Recusa e silêncio caem os dois em `nao_respondeu`: pro time de Qualidade a
    diferença não muda o que fazer, e o Hamilton não tem status separado.
    """
    pk = conversa.pesquisa_avaliacao_id
    payload: dict = {"status": "nao_respondeu" if recusou else "avaliado"}

    if not recusou:
        historico = await conversation.carregar_historico(db, conversa, limite=60)
        try:
            payload.update(await extrair_respostas(historico, avaliacao))
        except (llm_client.LLMError, PesquisaError) as exc:
            # Sem extração, ainda assim marcamos como avaliado: a conversa está
            # no painel e a pessoa respondeu de verdade. Perder o status seria
            # pior — a pesquisa seria reenviada pra quem já respondeu.
            logger.error("Não consegui extrair as respostas da pesquisa %s: %s", pk, exc)

    try:
        await hamilton_client.get_hamilton_client().atualizar_avaliacao(pk, payload)
    except hamilton_client.HamiltonError as exc:
        logger.error("Não consegui gravar a pesquisa %s no Hamilton: %s", pk, exc)

    await _limpar(db, conversa)


async def extrair_respostas(historico: list[dict], avaliacao: dict) -> dict:
    """Transforma a conversa da pesquisa no payload de respostas do Hamilton.

    Só devolve campos que a `Avaliacao` conhece e cujos valores são plausíveis:
    o modelo pode devolver campo inventado, nota fora da escala ou texto onde
    era pra ser número, e nada disso pode chegar ao banco.
    """
    transcricao = "\n".join(
        f"{'Paciente' if m.get('role') == 'user' else 'Sofia'}: {m.get('content')}"
        for m in historico
        if m.get("role") in ("user", "assistant") and m.get("content")
    )
    if not transcricao.strip():
        raise PesquisaError("Sem transcrição pra extrair")

    instrucao = config_prompt.texto("prompt_pesquisa_extracao")
    resposta = await llm_client.get_llm_client().gerar_resposta(
        [{"role": "user", "content": transcricao}], system_prompt=instrucao
    )
    return _normalizar_extracao(resposta.texto, avaliacao)


# Campos gravaveis na Avaliacao, por tipo. Allowlist: o que o modelo devolver
# fora daqui é descartado em silêncio, sem chance de virar erro 400 no Hamilton.
_NOTAS = (
    "individual",
    "interpessoal",
    "social",
    "geral",
    "qualidade_geral",
    "nota_terapeuta",
    "nota_indicacao",
    "nota_sofia",
)
_TEXTOS = (
    "feedback_livre",
    "atendimento_rapido",
    "indicaria_allos",
    "motivo_interrupcao",
)
_BOOLEANOS = ("consentimento_paciente", "atendimento_rapido_bool", "indicaria_allos_bool")


def _normalizar_extracao(bruto: str | None, avaliacao: dict) -> dict:
    """Valida e converte o JSON devolvido pelo modelo."""
    if not bruto:
        raise PesquisaError("Extração vazia")
    texto = bruto.strip()
    # O modelo às vezes embrulha o JSON num bloco de código.
    if texto.startswith("```"):
        texto = texto.strip("`")
        texto = texto.split("\n", 1)[-1] if texto.lower().startswith("json") else texto
    inicio, fim = texto.find("{"), texto.rfind("}")
    if inicio == -1 or fim == -1:
        raise PesquisaError("Extração não trouxe JSON")
    try:
        dados = json.loads(texto[inicio : fim + 1])
    except json.JSONDecodeError as exc:
        raise PesquisaError(f"JSON inválido na extração: {exc}") from exc
    if not isinstance(dados, dict):
        raise PesquisaError("Extração não é um objeto")

    limpo: dict = {}
    for campo in _NOTAS:
        valor = dados.get(campo)
        if valor is None:
            continue
        try:
            nota = int(valor)
        except (TypeError, ValueError):
            continue
        if 0 <= nota <= 10:  # fora da escala é dado ruim, não dado parcial
            limpo[campo] = nota
    for campo in _TEXTOS:
        valor = dados.get(campo)
        if isinstance(valor, str) and valor.strip():
            limpo[campo] = valor.strip()
    for campo in _BOOLEANOS:
        valor = dados.get(campo)
        if isinstance(valor, bool):
            limpo[campo] = valor

    data = dados.get("dat_ultima_sessao")
    if isinstance(data, str) and data.strip():
        try:
            limpo["dat_ultima_sessao"] = (
                datetime.fromisoformat(data.strip()[:10]).date().isoformat()
            )
        except ValueError:
            pass  # data que não dá pra ler é melhor ficar em branco

    # Motivo da interrupção só existe na pesquisa de encerramento.
    if avaliacao.get("momento") != MOMENTO_ENCERRAMENTO:
        limpo.pop("motivo_interrupcao", None)
    return limpo


# --------------------------------------------------------------------------- #
# Rodada do cron
# --------------------------------------------------------------------------- #


async def rodar_pesquisas(db: AsyncSession, agora: datetime | None = None) -> dict:
    """Uma rodada: envia convites novos, manda lembretes e encerra por prazo.

    Devolve um resumo (enviadas/lembretes/encerradas) pra quem chamou logar.
    Falha do Hamilton não derruba nada: a fila continua lá na próxima rodada.
    """
    agora = agora or _agora()
    resumo = {"enviadas": 0, "lembretes": 0, "encerradas": 0}

    try:
        pendentes = await hamilton_client.get_hamilton_client().avaliacoes_pendentes()
    except hamilton_client.HamiltonError as exc:
        logger.error("Não consegui buscar as avaliações pendentes: %s", exc)
        pendentes = []

    for avaliacao in pendentes:
        conversa = await _conversa_do_paciente(db, avaliacao)
        if conversa is None:
            # Paciente que não veio pela Sofia não tem conversa aberta, e fora da
            # janela de 24h da Meta não dá pra iniciar uma com texto livre.
            # Fica pra equipe abordar (exigiria um template aprovado).
            continue
        if em_pesquisa(conversa) or conversa.modo == "humano":
            continue  # já tem pesquisa em curso, ou a conversa está com a Thainá
        if await iniciar(db, conversa, avaliacao, agora):
            resumo["enviadas"] += 1

    resumo.update(await _acompanhar_em_curso(db, agora))
    logger.info(
        "Pesquisas: %s enviadas, %s lembretes, %s encerradas",
        resumo["enviadas"],
        resumo["lembretes"],
        resumo["encerradas"],
    )
    return resumo


async def _acompanhar_em_curso(db: AsyncSession, agora: datetime) -> dict:
    """Lembra quem está em silêncio há 20h e encerra quem passou de 44h."""
    resumo = {"lembretes": 0, "encerradas": 0}
    conversas = (
        (await db.execute(select(Conversa).where(Conversa.pesquisa_avaliacao_id.isnot(None))))
        .scalars()
        .all()
    )
    for conversa in conversas:
        silencio = agora - (_aware(conversa.pesquisa_iniciada_em) or agora)
        if silencio >= timedelta(hours=HORAS_ENCERRAMENTO):
            avaliacao = await _buscar_avaliacao(conversa) or {}
            await finalizar(db, conversa, avaliacao, recusou=True)
            resumo["encerradas"] += 1
        elif silencio >= timedelta(hours=HORAS_LEMBRETE):
            if await _mandar_lembrete(db, conversa, agora):
                resumo["lembretes"] += 1
    return resumo


async def _mandar_lembrete(db: AsyncSession, conversa: Conversa, agora: datetime) -> bool:
    """Manda o lembrete único, se ainda não foi mandado."""
    pk = conversa.pesquisa_avaliacao_id
    cliente = hamilton_client.get_hamilton_client()
    try:
        pendentes = await cliente.avaliacoes_pendentes(incluir_enviadas=True)
    except hamilton_client.HamiltonError:
        return False
    atual = next((a for a in pendentes if a.get("pk_avaliacao") == pk), None)
    if atual is None or atual.get("sofia_lembrete_em"):
        return False  # já lembramos; insistir de novo vira pressão

    if not await _enviar(db, conversa, LEMBRETE_TEXTO):
        return False
    try:
        await cliente.atualizar_avaliacao(pk, {"sofia_lembrete_em": agora.isoformat()})
    except hamilton_client.HamiltonError as exc:
        logger.error("Lembrete da pesquisa %s enviado mas não marcado: %s", pk, exc)
    await db.commit()
    return True


# --------------------------------------------------------------------------- #
# Auxiliares
# --------------------------------------------------------------------------- #

# O modelo sinaliza o fim da pesquisa com um marcador no final da fala. É a única
# forma de saber que acabou sem uma máquina de estado — e ele nunca chega ao
# paciente (é removido antes do envio, e a sanitização de saída é a última rede).
MARCADOR_FIM = "[[PESQUISA_CONCLUIDA]]"
MARCADOR_RECUSA = "[[PESQUISA_RECUSADA]]"


def _marcador_de_fim(texto: str | None) -> str | None:
    if not texto:
        return None
    if MARCADOR_RECUSA in texto:
        return "recusou"
    if MARCADOR_FIM in texto:
        return "concluiu"
    return None


def _sem_marcador(texto: str | None) -> str | None:
    if not texto:
        return texto
    return texto.replace(MARCADOR_FIM, "").replace(MARCADOR_RECUSA, "").strip()


async def _turno(conversa, avaliacao: dict, historico: list[dict], abertura: bool = False) -> str:
    """Uma geração do modelo com o prompt da pesquisa."""
    mensagens = list(historico)
    if abertura:
        mensagens.append(
            {
                "role": "system",
                "content": (
                    "[Aviso do sistema: comece a pesquisa agora. Você já conhece esta "
                    "pessoa e já falou com ela antes — NÃO se reapresente. Peça "
                    "consentimento antes de começar as perguntas.]"
                ),
            }
        )
    resposta = await llm_client.get_llm_client().gerar_resposta(
        mensagens, system_prompt=montar_prompt(avaliacao)
    )
    return resposta.texto or ""


async def _enviar(db: AsyncSession, conversa: Conversa, texto: str | None) -> bool:
    """Envia a fala da pesquisa em bolhas, persistindo cada uma.

    Importado aqui dentro porque o router do webhook importa este módulo: no topo
    do arquivo daria import circular.
    """
    if not texto or not texto.strip():
        return False
    from app.routers import webhook

    try:
        await webhook._enviar_em_bolhas(db, conversa, conversa.numero_whatsapp, texto)
    except whatsapp_client.WhatsAppError:
        logger.error(
            "Não consegui falar com o número %s na pesquisa",
            mascarar_telefone(conversa.numero_whatsapp),
        )
        return False
    return True


async def _buscar_avaliacao(conversa: Conversa) -> dict | None:
    """Recupera do Hamilton a avaliação em curso nesta conversa."""
    if conversa.pesquisa_avaliacao_id is None:
        return None
    try:
        pendentes = await hamilton_client.get_hamilton_client().avaliacoes_pendentes(
            incluir_enviadas=True
        )
    except hamilton_client.HamiltonError as exc:
        logger.error("Não consegui recuperar a avaliação em curso: %s", exc)
        return None
    return next(
        (a for a in pendentes if a.get("pk_avaliacao") == conversa.pesquisa_avaliacao_id), None
    )


async def _conversa_do_paciente(db: AsyncSession, avaliacao: dict) -> Conversa | None:
    """Acha a conversa da Sofia com aquele paciente.

    Primeiro pelo vínculo de cadastro (`paciente_hamilton_id`), que é o caminho
    confiável; depois pelo telefone, que cobre quem conversou com a Sofia mas foi
    cadastrado à mão no Hamilton. Compara só os dígitos, porque o Hamilton guarda
    sem DDI (31...) e o WhatsApp manda com (5531...).
    """
    pid = avaliacao.get("fk_paciente")
    if pid:
        conversa = (
            await db.execute(select(Conversa).where(Conversa.paciente_hamilton_id == pid))
        ).scalar_one_or_none()
        if conversa is not None:
            return conversa

    telefone = hamilton_client.normalizar_telefone(avaliacao.get("paciente_telefone"))
    if not telefone:
        return None
    for conversa in (await db.execute(select(Conversa))).scalars().all():
        if hamilton_client.normalizar_telefone(conversa.numero_whatsapp) == telefone:
            return conversa
    return None


async def _limpar(db: AsyncSession, conversa: Conversa) -> None:
    """Tira a conversa do modo pesquisa (ela volta ao fluxo normal)."""
    conversa.pesquisa_avaliacao_id = None
    conversa.pesquisa_iniciada_em = None
    await db.commit()
