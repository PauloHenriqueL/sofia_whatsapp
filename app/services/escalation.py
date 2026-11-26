"""Lógica de escalada para a Thainá (Passo 5).

Escalar significa: marcar a conversa como `modo = humano` e `estado = escalado`,
registrar a escalada e alertar a Thainá pelo template aprovado na Meta.
"""

import logging
import re
import unicodedata
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Conversa, Escalada, Mensagem
from app.services import tools, usuarios, whatsapp_client
from app.config import settings

logger = logging.getLogger(__name__)

# Texto de acolhimento usado quando o LLM escala mas não gera uma fala própria.
# Alinhado ao briefing (seção System prompt): sem travessões, acolhedor.
ESCALADA_FALLBACK = (
    "Vou chamar a Thainá pra continuar daqui contigo. Ela é a coordenadora "
    "da nossa clínica e vai te responder em pouco tempo."
)

# Resposta a quem escreve DEPOIS de a conversa já ter sido escalada. Sai uma vez
# por escalada (ver `webhook._avisar_escalada_uma_vez`): antes disso a Sofia
# ficava totalmente muda em modo humano e a pessoa escrevia no vazio até alguém
# abrir o painel. Texto fixo de propósito — em modo humano a Sofia não pode
# retomar o fluxo nem escrever por cima de quem assumiu a conversa.
AVISO_EM_ATENDIMENTO = (
    "Sua mensagem chegou aqui e a Thainá já foi avisada. "
    "Ela vai te responder por aqui mesmo, assim que puder."
)


# --------------------------------------------------------------------------- #
# Rede de proteção: a Sofia disse que ia acionar alguém e não chamou a tool
# --------------------------------------------------------------------------- #
#
# O modelo tem dois canais (texto e tool call) e erra o canal: escreve "vou
# confirmar com a Thainá, ela te responde em pouco tempo" e encerra o turno sem
# chamar `escalar_para_thaina`. A pessoa fica esperando uma resposta que ninguém
# sabe que deve — nada aparece no painel, nenhum alerta sai. Em 11/11/2026 isso
# aconteceu em 3 de 7 conversas de teste.
#
# Não é substituível por prompt (o prompt reforça, mas LLM não garante canal).
# Por isso a conferência é em código, no texto que de fato vai pro paciente.
#
# O casamento é em PRIMEIRA PESSOA de propósito. "A Thainá te chama em até 36
# horas" é o aviso normal do cadastro, não uma promessa de escalada — e é a
# frase mais comum da Sofia. Só conta quando é ELA dizendo que vai acionar.
_VERBOS_DE_ACIONAR = (
    r"chamar|acionar|avisar|confirmar|pedir|passar|repassar|encaminhar|levar|"
    r"ver|verificar|checar|conferir|perguntar|falar|consultar|marcar|agendar"
)
_PROMESSA_DE_ACIONAR = re.compile(
    # "vou confirmar com a Thainá", "vou pedir para a Thainá te orientar"
    rf"\b(?:vou|irei)\s+(?:ja\s+)?(?:{_VERBOS_DE_ACIONAR})\b[^.!?\n]{{0,70}}?\b(thaina|amanda)\b"
    # "já avisei a Thainá", "chamei a Thainá"
    r"|\b(?:chamei|avisei|acionei)\s+(?:a\s+)?(thaina|amanda)\b"
    # "estou chamando a Thainá", "tô passando pra Thainá"
    r"|\b(?:estou|to|tou)\s+(?:chamando|acionando|avisando|passando)\b[^.!?\n]{0,40}?\b(thaina|amanda)\b",
    re.IGNORECASE,
)


def _sem_acento(texto: str) -> str:
    n = unicodedata.normalize("NFKD", texto or "")
    return "".join(c for c in n if not unicodedata.combining(c))


def promessa_de_acionar(texto: str | None) -> tuple[str, str] | None:
    """A fala promete, em primeira pessoa, acionar a Thainá ou a Amanda?

    Devolve `(frase, quem)` — a frase que prometeu e `"thaina"`/`"amanda"` — ou
    `None`. A frase vai pro `contexto` da escalada: é o que a pessoa leu, e é por
    onde a Thainá entende o que está sendo esperado dela.
    """
    if not texto:
        return None
    for frase in re.split(r"(?<=[.!?])\s+|\n+", texto):
        m = _PROMESSA_DE_ACIONAR.search(_sem_acento(frase))
        if m:
            quem = next(g for g in m.groups() if g).lower()
            return frase.strip(), quem
    return None


async def garantir_escalada_prometida(
    session: AsyncSession, conversa: Conversa, texto: str | None
) -> bool:
    """Se a fala prometeu acionar alguém, registra a escalada que o modelo não fez.

    Chame só quando NENHUMA escalada aconteceu neste turno. Faz o mesmo que a
    tool `escalar_para_thaina` faria: modo humano, registro e alerta. Devolve
    True se escalou.
    """
    achado = promessa_de_acionar(texto)
    if achado is None:
        return False
    frase, quem = achado
    # Prometer a Amanda é o convite da neuro aceito; o resto não dá pra saber o
    # motivo pelo texto, e `outro` com a frase é honesto.
    motivo = "neuro_reuniao" if quem == "amanda" else "outro"
    contexto = (
        "Registrada automaticamente: a Sofia disse ao paciente que ia acionar a "
        f'equipe e não escalou. O que ela disse: "{frase[:300]}"'
    )
    await registrar_escalada(session, conversa, motivo, contexto)
    await alertar_thaina(session, conversa, motivo)
    # Sem a frase no log (pode citar o que o paciente contou — LGPD).
    logger.warning(
        "Conversa %s: promessa de acionar a equipe sem tool call; escalada "
        "registrada pela rede de proteção (motivo=%s)",
        conversa.id,
        motivo,
    )
    return True


# --- Dados antes de escalar -------------------------------------------------
#
# A Thainá assume a conversa e, sem dado nenhum, tem que perguntar tudo de novo.
# O prompt já pedia "pegue os dados antes de escalar" desde 17/11, e em 23/11 a
# Sofia ouviu "quero falar com uma pessoa, não com um robô" e escalou na hora,
# sem nem o nome. O modelo leu o pedido de humano como recusa. Prompt não
# garante, então a regra vive aqui:
#
# - Na PRIMEIRA tentativa de escalar com dado faltando, a escalada não acontece.
#   O código anota a escalada como pendente e o modelo pede o que falta.
# - Na seguinte, ela acontece com o que houver, a pessoa tendo mandado os dados
#   ou não. É o "uma tentativa só": quem recusa não fica preso.
# - Crise e menor de 11 não esperam. Áudio, anexo e a trava do robô nem passam
#   por aqui (escalam em código).
CAMPOS_ANTES_DE_ESCALAR = {
    "nome_completo": "seu nome completo",
    "data_nascimento": "sua data de nascimento",
    "horarios_disponiveis": "os horários em que você tem disponibilidade durante a semana",
}
# O que a tool de escalada aceita gravar na ficha (allowlist).
CAMPOS_DA_ESCALADA = (*CAMPOS_ANTES_DE_ESCALAR, "telefone_contato")
MOTIVOS_SEM_PEDIR_DADOS = frozenset({"crise", "menor_11"})
CHAVE_PENDENTE = "escalada_pendente"
# Pendente velha não vale: quem volta dias depois começa outra conversa.
VALIDADE_PENDENTE = timedelta(hours=24)

# O que vai pro modelo quando a escalada é segurada (resultado da tool).
INSTRUCAO_PEDIR_DADOS = (
    "Ainda NÃO passei a conversa pra Thainá. Peça agora, numa mensagem só, o que "
    "falta (lista em `faltam`), dizendo que é pra Thainá chegar sabendo e não "
    "precisar perguntar tudo de novo. Responda antes o que você souber da mensagem "
    "dela. Não diga que já chamou ou que vai chamar a Thainá agora: diga que passa "
    "a conversa pra ela assim que a pessoa mandar. Se ela mandar, recusar ou "
    "insistir, chame `escalar_para_thaina` de novo no próximo turno, com os dados "
    "que ela der."
)
# Nota de sistema no turno seguinte ao pedido.
NOTA_ESCALADA_PENDENTE = (
    "Você já pediu os dados desta pessoa para passar a conversa à Thainá (motivo "
    "`{motivo}`). Agora chame `escalar_para_thaina` com esse motivo e com os dados "
    "que ela mandou (nome_completo, data_nascimento no formato YYYY-MM-DD, "
    "horarios_disponiveis), mesmo que ela não tenha mandado nada, e avise que "
    "passou a conversa. Não peça os dados de novo."
)
AVISO_PASSEI = "Pronto, passei a conversa pra Thainá. Ela te responde por aqui mesmo, em pouco tempo."


def _texto(valor) -> str:
    return valor.strip() if isinstance(valor, str) else ""


def guardar_dados_da_escalada(conversa: Conversa, argumentos: dict) -> None:
    """Grava na ficha o que a pessoa passou e o modelo mandou na tool de escalada.

    Só campos da allowlist e só valor não vazio: um campo ausente não apaga o que
    já havia sido coletado. Reatribui o dict (o JSON não detecta mutação).
    """
    novos = {c: _texto(argumentos.get(c)) for c in CAMPOS_DA_ESCALADA}
    novos = {c: v for c, v in novos.items() if v}
    if novos:
        conversa.dados_coletados = {**(conversa.dados_coletados or {}), **novos}


def escalada_pendente(conversa: Conversa) -> dict | None:
    """A escalada que ficou esperando os dados, se ainda vale."""
    pendente = (conversa.dados_coletados or {}).get(CHAVE_PENDENTE)
    if not isinstance(pendente, dict):
        return None
    try:
        pedido_em = datetime.fromisoformat(pendente["pedido_em"])
    except (KeyError, TypeError, ValueError):
        return None
    if pedido_em.tzinfo is None:
        pedido_em = pedido_em.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) - pedido_em > VALIDADE_PENDENTE:
        return None
    return pendente


def marcar_pendente(conversa: Conversa, motivo: str, contexto: str | None) -> None:
    conversa.dados_coletados = {
        **(conversa.dados_coletados or {}),
        CHAVE_PENDENTE: {
            "motivo": motivo,
            "contexto": contexto,
            "pedido_em": datetime.now(timezone.utc).isoformat(),
        },
    }


def limpar_pendente(conversa: Conversa) -> None:
    dados = conversa.dados_coletados or {}
    if CHAVE_PENDENTE in dados:
        conversa.dados_coletados = {k: v for k, v in dados.items() if k != CHAVE_PENDENTE}


def faltam_dados(conversa: Conversa, motivo: str, recusou: bool = False) -> list[str]:
    """Os campos que a Sofia ainda precisa pedir antes de escalar (vazio = pode escalar).

    Não pede: em crise ou menor de 11 (ou com sinal de crise nas últimas falas,
    marcado no turno pelo webhook), se a pessoa já recusou passar os dados, se
    já pediu uma vez (escalada pendente), se ela já está cadastrada no Hamilton
    (a ficha existe) ou se a conversa é de terapeuta.
    """
    if (
        motivo in MOTIVOS_SEM_PEDIR_DADOS
        or getattr(conversa, "_sinal_de_crise", False)
        or recusou
        or conversa.paciente_hamilton_id
        or conversa.eh_terapeuta
        or escalada_pendente(conversa) is not None
    ):
        return []
    dados = conversa.dados_coletados or {}
    return [c for c in CAMPOS_ANTES_DE_ESCALAR if not _texto(dados.get(c))]


def rotulos(faltam: list[str]) -> list[str]:
    return [CAMPOS_ANTES_DE_ESCALAR[c] for c in faltam]


def pedido_de_dados(faltam: list[str]) -> str:
    """O pedido fixo, para quando a rede (não o modelo) segura a escalada."""
    itens = rotulos(faltam)
    lista = itens[0] if len(itens) == 1 else ", ".join(itens[:-1]) + " e " + itens[-1]
    return (
        "Pra Thainá, nossa coordenadora clínica, chegar sabendo e não precisar te "
        f"perguntar tudo de novo, me manda {lista}? Assim que você mandar, eu passo "
        "a conversa pra ela."
    )


async def cumprir_pendente(
    session: AsyncSession, conversa: Conversa, pendente: dict, texto: str | None
) -> str | None:
    """Turno seguinte ao pedido: se o modelo não escalou, o código escala.

    É o que torna o "uma tentativa só" uma garantia: mandando os dados ou não, a
    pessoa chega na Thainá nesta resposta. Devolve o texto a enviar, com o aviso
    de que a conversa passou quando a fala do modelo não diz isso.
    """
    if conversa.modo == "humano":
        return texto
    motivo = pendente.get("motivo") or "outro"
    contexto = " ".join(
        p
        for p in (
            _texto(pendente.get("contexto")),
            "(A Sofia pediu os dados uma vez antes de passar; o que a pessoa mandou está na conversa.)",
        )
        if p
    )
    await registrar_escalada(session, conversa, motivo, contexto)
    await alertar_thaina(session, conversa, motivo)
    logger.info("Conversa %s: escalada pendente cumprida pelo código (motivo=%s)", conversa.id, motivo)
    if not _texto(texto):
        return ESCALADA_FALLBACK
    if promessa_de_acionar(texto) is None:
        return f"{texto.strip()}\n\n{AVISO_PASSEI}"
    return texto


async def rede_de_promessa(session: AsyncSession, conversa: Conversa, texto: str | None) -> str | None:
    """A rede `garantir_escalada_prometida`, respeitando os dados antes de escalar.

    A fala prometeu acionar a Thainá sem chamar a tool. Se faltar dado e ainda
    não foi pedido, a promessa sai da fala, entra o pedido fixo e a escalada fica
    pendente. Senão, escala como antes. Devolve o texto a enviar.
    """
    achado = promessa_de_acionar(texto)
    if achado is None:
        return texto
    frase, quem = achado
    motivo = "neuro_reuniao" if quem == "amanda" else "outro"
    faltam = faltam_dados(conversa, motivo)
    if not faltam:
        await garantir_escalada_prometida(session, conversa, texto)
        return texto
    marcar_pendente(
        conversa,
        motivo,
        f'A Sofia disse que ia acionar a equipe sem escalar: "{frase[:300]}"',
    )
    resto = texto.replace(frase, "").strip()
    pedido = pedido_de_dados(faltam)
    logger.info("Conversa %s: promessa de escalar segurada para pedir os dados", conversa.id)
    return f"{resto}\n\n{pedido}" if resto else pedido


# --- Trava contra conversa com outro robô ---------------------------------
#
# Em 30/10/2026 a Sofia trocou **174 mensagens num dia** com o menu automático
# da Claro: 109 chegaram, ela respondeu 65. Nenhum paciente do outro lado, e
# cada volta é uma chamada ao modelo e uma mensagem na Meta. Parou por acaso,
# quando a conversa acabou escalada.
#
# O teto é por conversa e por hora. Gente de verdade quase nunca encosta nele
# (uma rajada humana vira UMA resposta, por causa da janela de agrupamento);
# robô encosta em minutos.
#
# Conta RESPOSTAS (turnos), não mensagens: cada resposta sai em 2 ou 3 bolhas, e
# contar bolhas fazia uma conversa normal de cadastro (explicação, dados,
# confirmação, pesquisa de entrada) bater no teto em uns 6 turnos — a Sofia se
# calava no meio e escalava como "robô". Uma resposta é uma sequência de
# mensagens do bot que vem depois de uma mensagem de outra origem.
RESPOSTAS_POR_HORA = 20
MOTIVO_LOOP = "loop_automatico"


async def passou_do_teto(session: AsyncSession, conversa: Conversa) -> bool:
    """Se a Sofia já respondeu demais nesta conversa na última hora."""
    desde = datetime.now(timezone.utc) - timedelta(hours=1)
    origens = (
        await session.execute(
            select(Mensagem.origem)
            .where(Mensagem.conversa_id == conversa.id, Mensagem.criada_em >= desde)
            .order_by(Mensagem.criada_em, Mensagem.id)
        )
    ).scalars().all()
    respostas = sum(
        1
        for i, origem in enumerate(origens)
        if origem == "bot" and (i == 0 or origens[i - 1] != "bot")
    )
    return respostas >= RESPOSTAS_POR_HORA


async def travar_se_for_loop(session: AsyncSession, conversa: Conversa) -> bool:
    """Teto estourado: cala a Sofia, escala e avisa a Thainá. Devolve se travou.

    Escala em vez de só silenciar porque, se do outro lado houver gente, alguém
    precisa aparecer — e `registrar_escalada` já põe a conversa em modo humano,
    que é o portão que cala a Sofia no webhook.
    """
    if not await passou_do_teto(session, conversa):
        return False
    await registrar_escalada(
        session,
        conversa,
        MOTIVO_LOOP,
        contexto=(
            f"A Sofia respondeu {RESPOSTAS_POR_HORA}+ vezes nesta conversa em uma hora. "
            "Parei de responder: do outro lado costuma estar outro robô (menu automático "
            "de operadora, por exemplo). Se for uma pessoa, responda por aqui."
        ),
    )
    await alertar_thaina(session, conversa, MOTIVO_LOOP)
    logger.warning(
        "Conversa %s travada por teto de respostas (%d/h): possível loop com outro robô",
        conversa.id,
        RESPOSTAS_POR_HORA,
    )
    return True


async def registrar_escalada(
    session: AsyncSession,
    conversa: Conversa,
    motivo: str,
    contexto: str | None = None,
) -> Escalada:
    """Marca a conversa como humana/escalada e cria o registro de escalada."""
    conversa.modo = "humano"
    conversa.estado = "escalado"
    # Escalou (pela tool, pela rede ou por áudio): não sobra nada esperando dado.
    limpar_pendente(conversa)
    escalada = Escalada(conversa_id=conversa.id, motivo=motivo, contexto=contexto)
    session.add(escalada)
    await session.flush()
    logger.info(f"Conversa {conversa.id} escalada (motivo={motivo})")
    return escalada


async def resolver_abertas(session: AsyncSession, conversa: Conversa) -> int:
    """Fecha as escaladas abertas desta conversa. Devolve quantas fechou.

    `Escalada.resolvida_em` existia no modelo desde o Passo 3 mas **nunca era
    preenchido em código de produção**. Como `pesquisa._abrir_entradas` exclui
    conversas com escalada aberta, o efeito era silencioso e permanente: quem foi
    escalado uma vez — por áudio, anexo, preço, gratuidade ou pedido de humano —
    ficava fora da pesquisa de linha de base **pra sempre**. Como praticamente
    toda conversa escala em algum momento, isso é a maioria dos pacientes.

    Chamado quando a Thainá devolve a conversa ao bot ou arquiva: é o momento em
    que ela terminou de tratar o que a fez assumir.
    """
    abertas = (
        (
            await session.execute(
                select(Escalada).where(
                    Escalada.conversa_id == conversa.id,
                    Escalada.resolvida_em.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    agora = datetime.now(timezone.utc)
    for escalada in abertas:
        escalada.resolvida_em = agora
    if abertas:
        await session.flush()
        logger.info("Conversa %s: %d escalada(s) resolvida(s)", conversa.id, len(abertas))
    return len(abertas)


async def _enviar_alerta(db: AsyncSession, conversa: Conversa, rotulo: str) -> bool:
    """Manda o template `alerta_thaina` pra quem está com o alerta ligado agora.

    Falha no envio é logada e **não** derruba a conversa: o evento já está
    registrado e aparece no painel mesmo sem o alerta chegar. O alerta é
    conveniência, não a fonte da verdade. Ninguém com o alerta ligado (ex.:
    Thainá de férias, Amanda ainda não assumiu) é um estado normal — a
    escalada/cadastro segue acontecendo, só ninguém é avisado por WhatsApp.
    """
    dados = conversa.dados_coletados or {}
    nome = dados.get("nome_completo") or conversa.numero_whatsapp
    return await alertar_equipe(db, nome, rotulo)


async def alertar_equipe(db: AsyncSession, nome: str, rotulo: str) -> bool:
    """O mesmo `alerta_thaina`, para quando só se tem o nome do paciente (ex.:
    uma reserva de encaminhamento que venceu, vinda do Hamilton)."""
    telefones = await usuarios.telefones_para_alerta(db)
    if not telefones:
        logger.info("Nenhum usuário com alerta ligado agora; %s não foi notificado", rotulo)
        return False
    enviou_algum = False
    for telefone in telefones:
        try:
            await whatsapp_client.enviar_template(
                telefone,
                settings.alert_template_name,
                parametros=[str(nome), rotulo],
            )
            enviou_algum = True
        except whatsapp_client.WhatsAppError:
            # Sem o nome do paciente no log (LGPD).
            logger.error("Não consegui alertar pelo template (%s)", rotulo)
        except Exception:
            # Qualquer outra falha (ex.: resposta inesperada da Meta) não pode
            # interromper o loop: os próximos telefones ainda precisam ser avisados.
            logger.exception("Falha inesperada ao alertar pelo template (%s)", rotulo)
    return enviou_algum


# --------------------------------------------------------------------------- #
# A IA parou (conta da OpenAI sem crédito ou com chave inválida)
#
# 25-26/11/2026: o crédito acabou e por ~24h toda pessoa que escreveu recebeu
# "tive um probleminha técnico, me manda de novo" — sem ninguém da equipe saber.
# Agora: cada conversa afetada é escalada (vai pra fila Hoje, com o alerta de
# sempre) e a equipe recebe UM aviso dizendo a causa e o que fazer.
MOTIVO_FALHA_IA = "falha_ia"
AVISO_CONTA_BLOQUEADA = (
    "SOFIA SEM IA: a OpenAI recusou a conta (crédito acabou ou chave inválida). "
    "Recarregue em platform.openai.com > Billing. Até lá as conversas vêm pra equipe."
)
INTERVALO_AVISO_CONTA = timedelta(hours=1)
_ultimo_aviso_conta: datetime | None = None


async def avisar_conta_bloqueada(erro: str = "") -> bool:
    """Avisa a equipe que a IA parou. No máximo uma vez por hora (em memória —
    1 instância no Render): cada mensagem de paciente bate nisso.

    Abre a própria sessão porque é chamado de dentro do cliente da OpenAI, que
    não tem banco. `erro` não vai pro alerta (só a causa genérica).
    """
    global _ultimo_aviso_conta
    agora = datetime.now(timezone.utc)
    if _ultimo_aviso_conta and agora - _ultimo_aviso_conta < INTERVALO_AVISO_CONTA:
        return False
    _ultimo_aviso_conta = agora
    from app.database import async_session

    async with async_session() as db:
        enviou = await alertar_equipe(db, "Sofia (sistema)", AVISO_CONTA_BLOQUEADA)
    logger.error("Equipe avisada de que a OpenAI recusou a conta (enviado=%s)", enviou)
    return enviou


async def escalar_por_falha_ia(
    session: AsyncSession, conversa: Conversa, conta_bloqueada: bool
) -> str:
    """O modelo não respondeu: a conversa passa pra equipe, em vez de a Sofia
    pedir "me manda de novo" a cada mensagem. Devolve o texto pro paciente.

    Vale também para falha passageira: o cliente da OpenAI já reenvia sozinho,
    então quando o erro chega aqui ele persistiu — e errar para o lado de chamar
    uma pessoa é o lado seguro.
    """
    causa = (
        "A IA da Sofia está fora: a conta da OpenAI recusou (crédito ou chave)."
        if conta_bloqueada
        else "A IA da Sofia falhou ao responder esta mensagem."
    )
    await registrar_escalada(
        session,
        conversa,
        MOTIVO_FALHA_IA,
        contexto=f"{causa} A pessoa não recebeu resposta da Sofia; responda por aqui.",
    )
    await session.commit()
    await alertar_thaina(session, conversa, MOTIVO_FALHA_IA)
    return ESCALADA_FALLBACK


async def alertar_thaina(db: AsyncSession, conversa: Conversa, motivo: str) -> bool:
    """Alerta de escalada: a conversa passou pra equipe."""
    return await _enviar_alerta(db, conversa, tools.MOTIVO_LABELS.get(motivo, motivo))


# O que a Thainá lê no template quando um cadastro acontece. Reusa o
# `alerta_thaina` (o texto é genérico: "<nome> — <o que aconteceu>"), pra não
# depender de aprovar um template novo na Meta.
ROTULOS_CADASTRO = {
    "cadastrado": "paciente novo cadastrado no Hamilton (ficha {id})",
    "atualizado": "paciente já conhecido voltou; ficha {id} atualizada",
    "cadastro_pendente": "CADASTRO FALHOU — precisa cadastrar à mão no Hamilton",
}


async def alertar_pesquisa(db: AsyncSession, conversa: Conversa, motivos: list[str]) -> bool:
    """Avisa a equipe que uma pesquisa terminou com sinal ruim.

    **Um alerta por pesquisa, no fim, com todos os motivos juntos.** Uma pesquisa
    ruim dispara vários gatilhos ao mesmo tempo (nota do terapeuta baixa + NPS
    baixo + "não combinou"); mandar um template por gatilho viraria três
    mensagens seguidas e o aviso perderia o efeito. Alertar no meio também não
    ajudaria: a conversa está em modo pesquisa e ninguém pode entrar sem
    atropelar a Sofia.

    Diferente da escalada, isto **não** muda o modo da conversa: a pessoa já
    terminou de responder e não está esperando ninguém. Quem decide se vira
    conversa é a equipe, pela fila do painel.
    """
    if not motivos:
        return False
    return await _enviar_alerta(db, conversa, f"pesquisa com sinal de alerta: {'; '.join(motivos)}")


async def alertar_cadastro(db: AsyncSession, conversa: Conversa, resultado: dict) -> bool:
    """Avisa a equipe do desfecho de um cadastro (novo, reencontro ou falha).

    `resultado` é o que `cadastro.cadastrar_paciente` devolve. Status desconhecido
    não manda nada (melhor silêncio que uma mensagem sem sentido).
    """
    modelo = ROTULOS_CADASTRO.get(resultado.get("status", ""))
    if not modelo:
        return False
    rotulo = modelo.format(id=resultado.get("paciente_id") or "?")
    return await _enviar_alerta(db, conversa, rotulo)
