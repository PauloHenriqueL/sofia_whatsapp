"""Cobrança da mensalidade depois da primeira sessão realizada (Demanda D).

Desenho fechado em grilling (08/08/2026). Os pontos que exigem ler mais de um
arquivo pra entender:

**O gatilho é `is_realizado`, não a pesquisa.** A Sofia pergunta ao Hamilton
(`status-primeira-consulta`) quem já teve a primeira consulta *realizada*. Isso é
deliberadamente diferente do gatilho da pesquisa, que é um signal disparado na
CRIAÇÃO da consulta e **ignora** `is_realizado` — ou seja, quem faltou à primeira
sessão recebe a pesquisa e **não** pode receber cobrança. Os dois sinais divergem
no mesmo registro; usar o da pesquisa cobraria quem não foi atendido.

**A pesquisa vem antes, por sequência e não por dependência.** Se há pesquisa em
curso, o tick pula a conversa e quem encadeia é `pesquisa.finalizar` chamando
`encadear()` no fim — respondida, recusada ou expirada, os três caminhos passam
por lá. Sem pesquisa em curso, a cobrança sai sozinha no tick. Nenhum dos dois
lados sabe do outro além dessa chamada.

**A Sofia retoma o controle mesmo em modo humano** (decisão do Paulo, contra a
minha recomendação). A primeira sessão é lançada dias depois de acontecer, então
colisão em tempo real com a Thainá é improvável; e o caso de borda — cobrar quem
escalou por `gratuidade` — resolve-se pela pessoa reagir e a Sofia escalar de
novo. Por isso o modo cobrança **tem** `escalar_para_thaina`: sem ela essa rede
de segurança não existe. O `modo` da conversa NÃO é alterado (a escalada aberta
continua valendo pra Thainá); quem abre exceção é o portão do webhook.

**Janela de 24h da Meta.** Fora dela, texto livre não sai e não temos template
aprovado. A cobrança que não consegue ser entregue **não some**: marca
`cobranca_status='sem_janela'` e aparece na fila "Pronto pra cobrança" do
/painel/acompanhamento, que já existe. Vale pro lembrete também.

**A entrada é sempre a mensalidade cheia**, idêntica no Pix e no cartão — ver o
porquê em `pagamentos.criar_assinatura_mensalidade`.
"""

import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Conversa
from app.services import (
    config_negocio,
    config_prompt,
    conversation,
    hamilton_client,
    links,
    llm_client,
    pagamentos,
    stripe_client,
    tools,
    whatsapp_client,
)
from app.utils import mascarar_telefone

logger = logging.getLogger(__name__)

# Prazo pra desistir de esperar resposta. Igual ao da pesquisa, de propósito: são
# a mesma conversa pro paciente, e dois relógios diferentes só confundiriam quem
# for ler o log depois. O lembrete é configurável (`cobranca_lembrete_horas`)
# porque tem que caber na janela de 24h; este não fala com ninguém, só encerra.
HORAS_ENCERRAMENTO = 44

# `cobranca_status` -> rótulo na fila do painel. É o que distingue "a Sofia cobrou
# e a pessoa sumiu" de "a Sofia nunca conseguiu falar" — sem isso a Thainá trataria
# os dois casos igual.
STATUS_LABELS = {
    "enviada": "Sofia cobrou, aguardando resposta",
    "cartao": "Escolheu cartão",
    "pix": "Escolheu Pix, aguardando comprovante",
    "comprovante": "Comprovante recebido",
    "indefinido": "Ficou de pensar",
    "escalada": "Escalou pra Thainá",
    "sem_resposta": "Não respondeu à cobrança",
    "sem_janela": "Fora da janela de 24h — precisa de contato manual",
    "erro_link": "Falha ao gerar o link de pagamento",
    "assumida": "Interrompida — a Thainá assumiu a conversa",
}


class CobrancaError(Exception):
    """Falha ao montar ou conduzir a cobrança."""


def _agora() -> datetime:
    return datetime.now(timezone.utc)


def _aware(valor: datetime | None) -> datetime | None:
    """Datas vindas do SQLite voltam sem tzinfo; compará-las com aware estoura."""
    if valor is not None and valor.tzinfo is None:
        return valor.replace(tzinfo=timezone.utc)
    return valor


def em_cobranca(conversa: Conversa) -> bool:
    """A conversa está no modo cobrança (turno roda com o prompt de pagamento)."""
    return conversa.cobranca_iniciada_em is not None and conversa.cobranca_encerrada_em is None


def ja_abordada(conversa: Conversa) -> bool:
    """A Sofia já falou de mensalidade com esta pessoa alguma vez.

    Nunca é limpo. Diferente da pesquisa — cuja memória mora no `sofia_enviada_em`
    da `Avaliacao`, no Hamilton — aqui não existe registro do outro lado, então é
    esta coluna que impede a pessoa de ser cobrada de novo a cada tick do cron.
    """
    return conversa.cobranca_iniciada_em is not None


def e_parceria(conversa: Conversa) -> bool:
    """Paciente de parceria (prefeitura/convênio) nunca é cobrado.

    A flag vem do `is_parceria` da `Captacao` do Hamilton, gravada no cadastro por
    `webhook._validar_captacao` — nunca da palavra do modelo.
    """
    return bool((conversa.dados_coletados or {}).get("is_parceria"))


def valor_mensal(conversa: Conversa) -> int:
    """Quanto cobrar desta pessoa, em reais.

    O desconto que a Sofia autorizou na conversa (tool `oferecer_desconto`) tem
    precedência sobre o preço de tabela — senão ela prometeria um valor e cobraria
    outro, que é o pior erro possível numa cobrança.
    """
    if conversa.desconto_valor:
        return int(conversa.desconto_valor)
    return int(config_negocio.valor("preco_terapia_mensal"))


def chave_pix() -> str:
    return str(config_negocio.valor("chave_pix") or "").strip()


# --------------------------------------------------------------------------- #
# Prompt
# --------------------------------------------------------------------------- #


def montar_prompt(conversa: Conversa, link: str | None) -> str:
    """Prompt do modo cobrança: condução + os dados concretos desta pessoa.

    O prompt é editável em /painel/prompts; os valores (mensalidade, chave Pix,
    link) são injetados aqui porque mudam por paciente e por configuração.
    """
    base = config_prompt.texto("prompt_cobranca")
    valor = valor_mensal(conversa)
    pix = chave_pix()
    nome = (conversa.dados_coletados or {}).get("nome_completo") or ""
    primeiro_nome = nome.split()[0] if nome else ""

    contexto = [
        "## Dados desta cobrança",
        f"- Mensalidade: R$ {valor}",
    ]
    if primeiro_nome:
        contexto.append(f"- Primeiro nome da pessoa: {primeiro_nome}")
    if link:
        contexto.append(f"- Link do cartão: {link}")
    else:
        contexto.append(
            "- Link do cartão: INDISPONÍVEL nesta conversa. NÃO invente um link e NÃO "
            "prometa mandar depois. Ofereça o Pix; se ela quiser cartão, escale pra Thainá."
        )
    if pix:
        contexto.append(f"- Chave Pix (CNPJ): {pix}")
    else:
        contexto.append("- Pix: NÃO oferecido nesta conversa. Ofereça apenas o cartão.")

    return f"{base}\n\n{chr(10).join(contexto)}"


# --------------------------------------------------------------------------- #
# Ciclo de vida
# --------------------------------------------------------------------------- #


async def _criar_link(db: AsyncSession, conversa: Conversa) -> str | None:
    """Gera a assinatura no Stripe e amarra ao paciente (`stripe_ref`).

    Falha aqui não cancela a cobrança: a Sofia ainda pode oferecer o Pix, e o
    status registra o problema pra Thainá gerar o link à mão pelo painel.
    """
    if not stripe_client.configurado():
        return None
    nome = (conversa.dados_coletados or {}).get("nome_completo") or conversa.numero_whatsapp
    try:
        resultado = await pagamentos.criar_assinatura_mensalidade(
            nome=nome,
            valor_mensal=valor_mensal(conversa),
            # Elo com o prontuário: é por ele que a contabilidade casa a
            # assinatura do Stripe com o paciente do Hamilton.
            paciente_id=conversa.paciente_hamilton_id,
        )
    except (pagamentos.ErroValidacao, stripe_client.StripeError) as exc:
        logger.error("Não consegui gerar o link de cobrança da conversa %s: %s", conversa.id, exc)
        return None
    # Só sobrescreve se ainda não houver vínculo: um paciente que já tem neuro
    # amarrado não pode perder essa referência por causa da mensalidade.
    if not conversa.stripe_ref:
        conversa.stripe_ref = resultado["ref"]
        await db.flush()
    return await links.encurtar(db, resultado["link"], conversa.id)


async def iniciar(db: AsyncSession, conversa: Conversa, agora: datetime | None = None) -> bool:
    """Manda a mensagem de cobrança e liga o modo cobrança.

    Só marca a conversa se a mensagem realmente saiu — mas, ao contrário da
    pesquisa, uma falha aqui **não** deixa o caso voltar no próximo tick em
    silêncio: marca `sem_janela` e joga pra fila da Thainá, porque a causa provável
    é a janela de 24h da Meta ter fechado, e ela não reabre sozinha.
    """
    agora = agora or _agora()
    link = await _criar_link(db, conversa)

    try:
        resposta = await _turno(db, conversa, link, historico=[], abertura=True)
    except llm_client.LLMError as exc:
        logger.error("Não consegui gerar a fala da cobrança (conversa %s): %s", conversa.id, exc)
        return False

    if not await _enviar(db, conversa, resposta):
        conversa.cobranca_iniciada_em = agora
        conversa.cobranca_encerrada_em = agora
        conversa.cobranca_status = "sem_janela"
        await db.commit()
        logger.warning("Cobrança da conversa %s não saiu; foi pra fila da Thainá", conversa.id)
        return False

    conversa.cobranca_iniciada_em = agora
    conversa.cobranca_encerrada_em = None
    conversa.cobranca_status = "enviada" if link else "erro_link"
    await db.commit()
    return True


async def responder(db: AsyncSession, conversa: Conversa, numero: str) -> None:
    """Conduz um turno da cobrança (no lugar do turno normal do bot)."""
    link = await _link_atual(db, conversa)
    historico = await conversation.carregar_historico(db, conversa)
    try:
        resposta = await _turno(db, conversa, link, historico=historico)
    except llm_client.LLMError:
        logger.error("LLM falhou no turno de cobrança da conversa %s", conversa.id)
        return
    await _enviar(db, conversa, resposta)


async def finalizar(db: AsyncSession, conversa: Conversa, status: str) -> None:
    """Desliga o modo cobrança, registrando o desfecho pra fila do painel."""
    conversa.cobranca_encerrada_em = _agora()
    conversa.cobranca_status = status
    await db.commit()


async def encadear(db: AsyncSession, conversa: Conversa) -> bool:
    """Emenda a cobrança no fim da pesquisa, se esta pessoa deve ser cobrada.

    Chamado por `pesquisa.finalizar` nos três desfechos (respondida, recusada,
    expirada). Revalida tudo do zero — inclusive no Hamilton — porque a pesquisa
    dispara pra quem só teve a consulta *lançada*, e cobrança exige *realizada*.
    """
    if not await _elegivel(db, conversa):
        return False
    return await iniciar(db, conversa)


# --------------------------------------------------------------------------- #
# Cron
# --------------------------------------------------------------------------- #


async def _elegivel(db: AsyncSession, conversa: Conversa) -> bool:
    """A pessoa deve receber cobrança automática agora?"""
    from app.services import pesquisa

    if not config_negocio.valor("cobranca_ativa"):
        return False
    if ja_abordada(conversa) or e_parceria(conversa):
        return False
    if conversa.arquivada_em is not None or conversa.cobranca_resolvida_em is not None:
        return False
    if conversa.paciente_hamilton_id is None:
        return False
    if pesquisa.em_pesquisa(conversa):
        return False  # a pesquisa encadeia a cobrança quando terminar
    try:
        status = await hamilton_client.get_hamilton_client().status_primeira_consulta(
            [conversa.paciente_hamilton_id]
        )
    except hamilton_client.HamiltonError as exc:
        logger.error("Não consegui checar a 1ª consulta da conversa %s: %s", conversa.id, exc)
        return False
    return bool(status.get(conversa.paciente_hamilton_id, {}).get("primeira_consulta_realizada"))


async def rodar_cobrancas(db: AsyncSession, agora: datetime | None = None) -> dict:
    """Tick do cron: aborda quem já teve a 1ª sessão e acompanha quem foi abordado."""
    agora = agora or _agora()
    if not config_negocio.valor("cobranca_ativa"):
        logger.info("Cobrança desligada no painel; nada a fazer")
        return {"enviadas": 0, "lembretes": 0, "encerradas": 0}

    enviadas = await _iniciar_pendentes(db, agora)
    acompanhamento = await _acompanhar_em_curso(db, agora)
    resumo = {"enviadas": enviadas, **acompanhamento}
    logger.info("Cobranças: %s", resumo)
    return resumo


async def _iniciar_pendentes(db: AsyncSession, agora: datetime) -> int:
    """Aborda quem já teve a primeira consulta realizada e ainda não foi cobrado.

    O filtro pesado (SQL) roda antes; o Hamilton só é consultado pra quem sobrou,
    numa chamada só — o endpoint aceita a lista inteira de ids.
    """
    from app.services import pesquisa

    candidatas = (
        (
            await db.execute(
                select(Conversa).where(
                    Conversa.paciente_hamilton_id.is_not(None),
                    Conversa.cobranca_iniciada_em.is_(None),
                    Conversa.cobranca_resolvida_em.is_(None),
                    Conversa.arquivada_em.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    candidatas = [c for c in candidatas if not e_parceria(c) and not pesquisa.em_pesquisa(c)]
    if not candidatas:
        return 0

    try:
        status = await hamilton_client.get_hamilton_client().status_primeira_consulta(
            [c.paciente_hamilton_id for c in candidatas]
        )
    except hamilton_client.HamiltonError as exc:
        logger.error("Hamilton fora do ar; nenhuma cobrança nesta rodada: %s", exc)
        return 0

    enviadas = 0
    for conversa in candidatas:
        info = status.get(conversa.paciente_hamilton_id) or {}
        if not info.get("primeira_consulta_realizada"):
            continue
        if await iniciar(db, conversa, agora):
            enviadas += 1
    return enviadas


async def _acompanhar_em_curso(db: AsyncSession, agora: datetime) -> dict:
    """Lembrete único e encerramento por prazo das cobranças em aberto."""
    horas_lembrete = int(config_negocio.valor("cobranca_lembrete_horas"))
    conversas = (
        (
            await db.execute(
                select(Conversa).where(
                    Conversa.cobranca_iniciada_em.is_not(None),
                    Conversa.cobranca_encerrada_em.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    lembretes = encerradas = 0
    for conversa in conversas:
        inicio = _aware(conversa.cobranca_iniciada_em) or agora
        decorrido = agora - inicio
        if decorrido >= timedelta(hours=HORAS_ENCERRAMENTO):
            await finalizar(db, conversa, "sem_resposta")
            encerradas += 1
        elif decorrido >= timedelta(hours=horas_lembrete) and conversa.cobranca_lembrete_em is None:
            if await _mandar_lembrete(db, conversa, agora):
                lembretes += 1
    return {"lembretes": lembretes, "encerradas": encerradas}


async def _mandar_lembrete(db: AsyncSession, conversa: Conversa, agora: datetime) -> bool:
    """Lembrete único, texto fixo e sem LLM.

    Sem LLM de propósito: é uma mensagem sobre dinheiro mandada pra quem não
    respondeu, e o espaço pra ela sair diferente do combinado não compensa o
    ganho de naturalidade.
    """
    texto = (
        "Oi! Só passando pra lembrar do pagamento da mensalidade, pra garantir "
        "sua vaga na agenda. Se tiver qualquer dúvida ou se esse não for um bom "
        "momento, me fala que a gente vê junto."
    )
    if not await _enviar(db, conversa, texto):
        await finalizar(db, conversa, "sem_janela")
        return False
    conversa.cobranca_lembrete_em = agora
    await db.commit()
    return True


# --------------------------------------------------------------------------- #
# Turno
# --------------------------------------------------------------------------- #


async def _link_atual(db: AsyncSession, conversa: Conversa) -> str | None:
    """Recupera o link de pagamento já criado pra esta conversa, encurtado.

    O `stripe_ref` guarda o id (`plink_...` hoje; `cs_...` no que foi criado antes
    da troca de Checkout Session por Payment Link) e a URL fica no Stripe. Buscar
    ao vivo evita guardar uma segunda cópia que poderia divergir — e é o que faz a
    Sofia repetir o MESMO link nos turnos seguintes em vez de gerar outro.

    O encurtamento é idempotente por destino, então o slug também é o mesmo em
    todos os turnos: a pessoa não recebe dois endereços diferentes pro mesmo
    pagamento.
    """
    ref = (conversa.stripe_ref or "").strip()
    url = None
    try:
        if ref.startswith("plink_"):
            url = (await stripe_client.obter_payment_link(ref)).get("url")
        elif ref.startswith("cs_"):
            # Legado: a URL da Session morre 24h depois de criada. Se já venceu,
            # é melhor não ter link (a Sofia oferece o Pix) que mandar um morto.
            sessao = await stripe_client.obter_checkout_session(ref)
            url = None if sessao.get("status") == "expired" else sessao.get("url")
        elif ref.startswith("https://buy.stripe.com/"):
            url = ref
    except stripe_client.StripeError:
        logger.warning("Não recuperei o link de pagamento da conversa %s", conversa.id)
    return await links.encurtar(db, url, conversa.id) if url else None


async def _turno(
    db: AsyncSession,
    conversa: Conversa,
    link: str | None,
    historico: list[dict],
    abertura: bool = False,
) -> str:
    """Uma geração do modelo com o prompt de cobrança, com tool calling."""
    from app.routers import webhook

    mensagens = list(historico)
    if abertura:
        mensagens.append(
            {
                "role": "system",
                "content": (
                    "[Aviso do sistema: a primeira sessão desta pessoa já aconteceu. "
                    "Abra o assunto da mensalidade agora, pela primeira vez. Você já "
                    "conhece esta pessoa — NÃO se reapresente e NÃO recomece um "
                    "atendimento. Se a conversa anterior terminou em outro assunto, "
                    "faça a transição com naturalidade antes de falar de pagamento.]"
                ),
            }
        )
    system_prompt = montar_prompt(conversa, link)
    cliente = llm_client.get_llm_client()
    resposta = await cliente.gerar_resposta(
        mensagens, tools=tools.TOOLS_COBRANCA, system_prompt=system_prompt
    )
    if not resposta.tool_calls:
        return resposta.texto or ""

    resultados = []
    for tc in resposta.tool_calls:
        if tc.name == tools.REGISTRAR_FORMA_PAGAMENTO:
            resultados.append((tc, await _registrar_forma(db, conversa, tc)))
        else:
            # Escalada (e qualquer outra tool compartilhada) reusa o handler do
            # webhook, pra não existirem dois caminhos de escalar com regras
            # diferentes. Escalar encerra o modo cobrança: quem conduz agora é a Thainá.
            resultados.append((tc, await webhook._executar_tool(db, conversa, tc)))
            if tc.name == tools.ESCALAR_PARA_THAINA:
                await finalizar(db, conversa, "escalada")

    assistant_msg = {
        "role": "assistant",
        "content": resposta.texto or "",
        "tool_calls": [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.name,
                    "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                },
            }
            for tc, _ in resultados
        ],
    }
    tool_msgs = [
        {
            "role": "tool",
            "tool_call_id": tc.id,
            "content": json.dumps(res, ensure_ascii=False),
        }
        for tc, res in resultados
    ]
    # O `system_prompt` PRECISA ir de novo: sem ele o modelo cairia no prompt de
    # acolhimento no meio de uma cobrança.
    final = await cliente.gerar_resposta(
        [*mensagens, assistant_msg, *tool_msgs], system_prompt=system_prompt
    )
    return final.texto or resposta.texto or ""


async def _registrar_forma(db: AsyncSession, conversa: Conversa, tc) -> dict:
    """Anota a forma de pagamento escolhida. NÃO encerra o modo cobrança.

    A pessoa costuma escolher e emendar uma dúvida ("consigo mudar depois?"); sair
    do modo aqui jogaria essa dúvida no prompt de acolhimento, que é um roteiro de
    qualificação de lead novo — e tem `oferecer_desconto` disponível.
    """
    forma = str((tc.arguments or {}).get("forma") or "").strip().lower()
    if forma not in ("cartao", "pix", "indefinido"):
        return {"status": "invalida"}
    conversa.cobranca_status = forma
    await db.commit()
    return {"status": "registrada", "forma": forma}


async def _enviar(db: AsyncSession, conversa: Conversa, texto: str | None) -> bool:
    """Envia a fala da cobrança em bolhas, persistindo cada uma.

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
            "Não consegui falar com o número %s na cobrança",
            mascarar_telefone(conversa.numero_whatsapp),
        )
        return False
    return True
