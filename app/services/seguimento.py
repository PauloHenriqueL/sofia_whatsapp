"""Follow-up automático de lead parado (Frente 2).

Quando um lead conversa mas some sem terminar o cadastro, a Sofia volta UMA vez,
dentro da janela de 24h da Meta (texto livre, sem template aprovado),
perguntando se a pessoa ainda tem interesse. É disparado por um cron externo
que bate em POST /tasks/seguimentos.
"""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Conversa, Mensagem
from app.services import conversation, whatsapp_client
from app.utils import mascarar_telefone

logger = logging.getLogger(__name__)

# Depois de 24h da última mensagem do paciente, a Meta não deixa mandar texto
# livre (exigiria template). O follow-up tem que sair dentro dessa janela.
JANELA_META_HORAS = 24

# Texto do follow-up. Fácil de ajustar aqui (acolhedor, sem emoji, sem travessão).
SEGUIMENTO_TEXTO = (
    "Oi, aqui é a Sofia, da Allos. A gente começou a conversar e acabou ficando "
    "pelo caminho. Você ainda tem interesse? Se quiser, me conta o que te impediu "
    "de seguir, ou se prefere que eu chame a Thainá. Tô por aqui."
)

# Estados que NÃO recebem follow-up: já resolvidos ou em tratamento humano.
ESTADOS_FINALIZADOS = ("cadastrado", "cadastro_pendente", "escalado")


async def buscar_leads_parados(db: AsyncSession, agora: datetime) -> list[Conversa]:
    """Conversas elegíveis pro follow-up.

    Critério: ainda no bot, sem cadastro, sem follow-up prévio, e cuja última
    mensagem do paciente caiu na janela [followup_horas, 24h) atrás (ou seja,
    parou de responder, mas ainda dá pra falar com texto livre).
    """
    limite_recente = agora - timedelta(hours=settings.followup_horas)
    limite_janela = agora - timedelta(hours=JANELA_META_HORAS)

    # Última mensagem recebida (do paciente) por conversa.
    ultima_recebida = (
        select(
            Mensagem.conversa_id.label("conversa_id"),
            func.max(Mensagem.criada_em).label("ult"),
        )
        .where(Mensagem.direcao == "recebida")
        .group_by(Mensagem.conversa_id)
        .subquery()
    )
    q = (
        select(Conversa)
        .join(ultima_recebida, ultima_recebida.c.conversa_id == Conversa.id)
        .where(
            Conversa.modo == "bot",
            Conversa.paciente_hamilton_id.is_(None),
            Conversa.estado.not_in(ESTADOS_FINALIZADOS),
            Conversa.seguimento_enviado_em.is_(None),
            ultima_recebida.c.ult <= limite_recente,
            ultima_recebida.c.ult >= limite_janela,
        )
    )
    return list((await db.execute(q)).scalars().all())


async def enviar_seguimento(db: AsyncSession, conversa: Conversa, agora: datetime) -> bool:
    """Manda o follow-up pro paciente e marca a conversa.

    Falha de envio fica logada e não derruba o lote; a conversa continua
    elegível na próxima rodada (seguimento_enviado_em só é setado no sucesso).
    """
    try:
        await whatsapp_client.enviar_texto(conversa.numero_whatsapp, SEGUIMENTO_TEXTO)
    except whatsapp_client.WhatsAppError:
        logger.error(f"Follow-up falhou pro número {mascarar_telefone(conversa.numero_whatsapp)}")
        return False
    await conversation.registrar_mensagem_enviada(db, conversa, texto=SEGUIMENTO_TEXTO)
    conversa.seguimento_enviado_em = agora
    await db.flush()
    return True


async def rodar_seguimentos(db: AsyncSession, agora: datetime | None = None) -> int:
    """Roda uma rodada de follow-ups. Retorna quantos foram enviados."""
    agora = agora or datetime.now(timezone.utc)
    leads = await buscar_leads_parados(db, agora)
    enviados = 0
    for conversa in leads:
        if await enviar_seguimento(db, conversa, agora):
            enviados += 1
    await db.commit()
    logger.info(f"Follow-ups: {enviados}/{len(leads)} enviados")
    return enviados
