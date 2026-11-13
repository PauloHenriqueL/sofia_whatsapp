"""Lógica de escalada para a Thainá (Passo 5).

Escalar significa: marcar a conversa como `modo = humano` e `estado = escalado`,
registrar a escalada e alertar a Thainá pelo template aprovado na Meta.
"""

import logging
import re
import unicodedata
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Conversa, Escalada
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


async def registrar_escalada(
    session: AsyncSession,
    conversa: Conversa,
    motivo: str,
    contexto: str | None = None,
) -> Escalada:
    """Marca a conversa como humana/escalada e cria o registro de escalada."""
    conversa.modo = "humano"
    conversa.estado = "escalado"
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
