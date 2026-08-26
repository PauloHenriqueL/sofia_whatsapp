"""Lógica de escalada para a Thainá (Passo 5).

Escalar significa: marcar a conversa como `modo = humano` e `estado = escalado`,
registrar a escalada e alertar a Thainá pelo template aprovado na Meta.
"""

import logging
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
    telefones = await usuarios.telefones_para_alerta(db)
    if not telefones:
        logger.info("Nenhum usuário com alerta ligado agora; %s não foi notificado", rotulo)
        return False
    dados = conversa.dados_coletados or {}
    nome = dados.get("nome_completo") or conversa.numero_whatsapp
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
