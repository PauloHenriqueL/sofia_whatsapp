"""Mensagem programada: a Sofia manda sozinha numa data marcada.

Pedido da Thainá (23/11/2026): "se o paciente me diz que só pode começar no dia
20, porque está de férias ou porque só vai ter dinheiro no dia 20, eu quero
programar a Sofia pra mandar mensagem pra ele no dia 20". Antes, essa conversa
morria ali: ninguém lembrava de voltar.

Quem programa:
- **a equipe**, na página da conversa (data, hora e o texto);
- **a própria Sofia**, pela ferramenta `programar_retomada`, quando combina com
  a pessoa "te chamo no dia 20".

Quem dispara é o cron que comprovadamente existe (`/tasks/lembretes-terapeutas`,
a cada 30 min), então a hora marcada vale com até meia hora de atraso.

**A janela de 24h da Meta decide o que sai.** No dia marcado quase nunca há
janela aberta, e fora dela só template aprovado:
- janela aberta → sai o texto programado, como fala da Sofia;
- janela fechada → sai o template de início de contato
  (`template_inicio_contato`, o mesmo do botão "Iniciar contato"), e o texto
  programado vira contexto: quando a pessoa responder, a Sofia recebe uma nota
  com ele (`nota_de_retomada`) e retoma dali, uma vez só.

**Diferente do "Iniciar contato", isto não assume a conversa**: quem responde é
a Sofia (ou quem já estava atendendo, se a conversa estiver em modo humano).
"""

import logging
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Conversa, MensagemProgramada
from app.services import conversation, escalation, inicio_contato, whatsapp_client
from app.utils import mascarar_telefone

logger = logging.getLogger(__name__)

# Horário de Brasília (sem horário de verão desde 2019).
FUSO = timezone(timedelta(hours=-3))
HORA_PADRAO = time(10, 0)
TAMANHO_MAXIMO = 1000
PRAZO_MAXIMO = timedelta(days=120)
TENTATIVAS_MAXIMAS = 3
POR_RODADA = 20
# Saiu como template e a pessoa não respondeu em 3 dias: a nota não vale mais.
VALIDADE_RETOMADA = timedelta(days=3)
SOFIA = "sofia"

NOTA_RETOMADA = (
    "Esta conversa foi retomada por uma mensagem programada {quem}para hoje. Ela dizia: "
    "\"{texto}\". Como a janela do WhatsApp estava fechada, a pessoa recebeu só um convite "
    "curto da equipe da Allos e acabou de responder. Retome a partir daí: diga, com as suas "
    "palavras, o que a mensagem programada dizia, e conduza para o próximo passo."
)


class ProgramacaoInvalida(ValueError):
    """Data no passado, longe demais, ou texto vazio/grande demais. `codigo`
    vai na URL do redirect do painel (o texto da mensagem não vai)."""

    def __init__(self, mensagem: str, codigo: str):
        super().__init__(mensagem)
        self.codigo = codigo


ERROS = {
    "vazio": "Escreva a mensagem.",
    "grande": f"A mensagem passa de {TAMANHO_MAXIMO} caracteres.",
    "passado": "Escolha uma data e hora no futuro.",
    "longe": "Dá para programar até 120 dias para a frente.",
    "data": "Data ou hora inválida.",
}


def agora_utc() -> datetime:
    return datetime.now(timezone.utc)


def _utc(valor: datetime) -> datetime:
    """SQLite devolve sem fuso: o que está gravado é UTC."""
    return valor if valor.tzinfo else valor.replace(tzinfo=timezone.utc)


def de_brasilia(dia: date, hora: time | None = None) -> datetime:
    """Data e hora de Brasília (o que a equipe digita) → UTC."""
    return datetime.combine(dia, hora or HORA_PADRAO, tzinfo=FUSO).astimezone(timezone.utc)


def em_brasilia(valor: datetime) -> datetime:
    return _utc(valor).astimezone(FUSO)


async def programar(
    db: AsyncSession, conversa: Conversa, texto: str, enviar_em: datetime, criada_por: str
) -> MensagemProgramada:
    texto = (texto or "").strip()
    if not texto:
        raise ProgramacaoInvalida(ERROS["vazio"], "vazio")
    if len(texto) > TAMANHO_MAXIMO:
        raise ProgramacaoInvalida(ERROS["grande"], "grande")
    agora = agora_utc()
    enviar_em = _utc(enviar_em)
    if enviar_em <= agora:
        raise ProgramacaoInvalida(ERROS["passado"], "passado")
    if enviar_em - agora > PRAZO_MAXIMO:
        raise ProgramacaoInvalida(ERROS["longe"], "longe")
    programada = MensagemProgramada(
        conversa_id=conversa.id,
        texto=texto,
        enviar_em=enviar_em,
        criada_por=(criada_por or "equipe")[:50],
    )
    db.add(programada)
    await db.flush()
    logger.info(
        "Mensagem programada %s para %s (conversa %s, por %s)",
        programada.id, em_brasilia(enviar_em).strftime("%d/%m %H:%M"), conversa.id,
        programada.criada_por,
    )
    return programada


async def da_conversa(db: AsyncSession, conversa: Conversa) -> list[MensagemProgramada]:
    """As que ainda vão sair, da mais próxima para a mais distante, e as que
    já saíram ou falharam nos últimos 30 dias (histórico na tela)."""
    corte = agora_utc() - timedelta(days=30)
    linhas = (
        await db.execute(
            select(MensagemProgramada)
            .where(MensagemProgramada.conversa_id == conversa.id)
            .order_by(MensagemProgramada.enviar_em)
        )
    ).scalars().all()
    return [
        p for p in linhas
        if (p.enviada_em is None and p.cancelada_em is None) or _utc(p.enviar_em) >= corte
    ]


async def cancelar(db: AsyncSession, conversa: Conversa, programada_id: int) -> bool:
    programada = await db.get(MensagemProgramada, programada_id)
    if (
        programada is None
        or programada.conversa_id != conversa.id
        or programada.enviada_em is not None
        or programada.cancelada_em is not None
    ):
        return False
    programada.cancelada_em = agora_utc()
    await db.flush()
    return True


# --- Disparo (cron) ---------------------------------------------------------


async def rodar(db: AsyncSession, agora: datetime | None = None) -> dict:
    agora = agora or agora_utc()
    vencidas = (
        await db.execute(
            select(MensagemProgramada)
            .where(
                MensagemProgramada.enviar_em <= agora,
                MensagemProgramada.enviada_em.is_(None),
                MensagemProgramada.cancelada_em.is_(None),
                MensagemProgramada.tentativas < TENTATIVAS_MAXIMAS,
            )
            .order_by(MensagemProgramada.enviar_em)
            .limit(POR_RODADA)
        )
    ).scalars().all()
    resultado = {"enviadas": 0, "nao_enviadas": 0}
    for programada in vencidas:
        ok = await _disparar(db, programada, agora)
        resultado["enviadas" if ok else "nao_enviadas"] += 1
        await db.commit()
    return resultado


async def _disparar(db: AsyncSession, programada: MensagemProgramada, agora: datetime) -> bool:
    conversa = await db.get(Conversa, programada.conversa_id)
    if conversa is None:
        programada.cancelada_em = agora
        return False
    if programada.criada_por == SOFIA and conversa.paciente_hamilton_id:
        # A Sofia combinou "te chamo dia 20" com quem ainda não tinha começado;
        # se a pessoa voltou antes e já foi cadastrada, chamar perdeu o sentido.
        # (As da equipe saem sempre: foi uma pessoa que decidiu.)
        programada.cancelada_em = agora
        logger.info("Mensagem programada %s cancelada: paciente já cadastrado", programada.id)
        return False
    janela = inicio_contato.janela_aberta(await inicio_contato.janela_ate(db, conversa), agora)
    try:
        if janela:
            resposta = await whatsapp_client.enviar_texto(conversa.numero_whatsapp, programada.texto)
            texto_no_painel, tipo, como = programada.texto, "texto", "texto"
        else:
            template = inicio_contato.template_configurado()
            if not template:
                raise whatsapp_client.WhatsAppError("template de início de contato não configurado")
            nome = inicio_contato._primeiro_nome(conversa)
            resposta = await whatsapp_client.enviar_template(
                conversa.numero_whatsapp, template, [nome])
            # Só o convite: o histórico vai para o modelo como fala dele, e
            # texto entre colchetes ali vira eco. O assunto aparece no card de
            # mensagens programadas e chega ao modelo pela `nota_de_retomada`.
            texto_no_painel = inicio_contato.texto_do_convite(nome)
            tipo, como = "template", "template"
    except whatsapp_client.WhatsAppError as exc:
        programada.tentativas = (programada.tentativas or 0) + 1
        programada.erro = str(exc)[:200]
        logger.error(
            "Mensagem programada %s para %s não saiu (tentativa %s): %s",
            programada.id, mascarar_telefone(conversa.numero_whatsapp), programada.tentativas, exc,
        )
        if programada.tentativas >= TENTATIVAS_MAXIMAS:
            nome = (conversa.dados_coletados or {}).get("nome_completo") or conversa.numero_whatsapp
            await escalation.alertar_equipe(db, nome, "mensagem programada NÃO saiu, chamar à mão")
        return False

    await conversation.registrar_mensagem_enviada(
        db, conversa, texto_no_painel, origem="bot", tipo=tipo,
        whatsapp_message_id=whatsapp_client.id_da_resposta(resposta),
    )
    programada.enviada_em = agora
    programada.enviada_como = como
    programada.erro = None
    if conversa.arquivada_em is not None:
        conversa.arquivada_em = None  # a resposta tem que aparecer na lista
    logger.info("Mensagem programada %s enviada como %s (conversa %s)",
                programada.id, como, conversa.id)
    return True


# --- Retomada: a pessoa respondeu ao convite ---------------------------------


async def nota_de_retomada(db: AsyncSession, conversa: Conversa) -> str | None:
    """Se a última programada saiu como template e a pessoa respondeu agora, a
    nota com o assunto dela para o modelo. Consumida na hora: vale um turno."""
    limite = agora_utc() - VALIDADE_RETOMADA
    programada = (
        await db.execute(
            select(MensagemProgramada)
            .where(
                MensagemProgramada.conversa_id == conversa.id,
                MensagemProgramada.enviada_como == "template",
                MensagemProgramada.retomada_usada_em.is_(None),
                MensagemProgramada.enviada_em >= limite,
            )
            .order_by(MensagemProgramada.enviada_em.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if programada is None:
        return None
    programada.retomada_usada_em = agora_utc()
    quem = "por você mesma " if programada.criada_por == SOFIA else "pela equipe "
    return NOTA_RETOMADA.format(quem=quem, texto=programada.texto)


# --- Ferramenta da Sofia ------------------------------------------------------


async def programar_pela_sofia(db: AsyncSession, conversa: Conversa, args: dict) -> dict:
    """`programar_retomada`: a Sofia combinou com a pessoa voltar a falar numa
    data. Devolve o resultado para o modelo escrever a fala final."""
    try:
        dia = date.fromisoformat(str(args.get("data") or ""))
    except ValueError:
        return {"status": "data_invalida",
                "orientacao": "Não entendi a data. Confirme com a pessoa o dia (dd/mm)."}
    hoje = em_brasilia(agora_utc()).date()
    if dia <= hoje:
        return {"status": "data_invalida",
                "orientacao": "A data tem que ser depois de hoje. Confirme o dia com a pessoa."}
    try:
        await programar(db, conversa, str(args.get("mensagem") or ""), de_brasilia(dia), SOFIA)
    except ProgramacaoInvalida as exc:
        return {"status": "nao_programada", "orientacao": str(exc)}
    return {
        "status": "programada",
        "data": dia.strftime("%d/%m"),
        "orientacao": (
            f"Ficou programado: você chama a pessoa no dia {dia:%d/%m}, de manhã. Confirme isso "
            "com ela em uma frase, e diga que se ela quiser começar antes é só escrever."
        ),
    }
