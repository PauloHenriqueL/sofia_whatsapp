"""Lógica de escalada para a Thainá (Passo 5).

Escalar significa: marcar a conversa como `modo = humano` e `estado = escalado`,
registrar a escalada e alertar a Thainá pelo template aprovado na Meta.
"""

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Conversa, Escalada
from app.services import tools, whatsapp_client

logger = logging.getLogger(__name__)

# Texto de acolhimento usado quando o LLM escala mas não gera uma fala própria.
# Alinhado ao briefing (seção System prompt): sem travessões, acolhedor.
ESCALADA_FALLBACK = (
    "Vou chamar a Thainá pra continuar daqui contigo. Ela é a coordenadora "
    "da nossa clínica e vai te responder em pouco tempo."
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


async def alertar_thaina(conversa: Conversa, motivo: str) -> bool:
    """Envia o template de alerta pra Thainá. Retorna True se enviou com sucesso.

    Falha no envio é logada e não derruba a conversa (a escalada já foi
    registrada e aparece no painel mesmo sem o alerta chegar).
    """
    dados = conversa.dados_coletados or {}
    nome = dados.get("nome_completo") or conversa.numero_whatsapp
    rotulo = tools.MOTIVO_LABELS.get(motivo, motivo)
    try:
        await whatsapp_client.enviar_template(
            settings.thaina_whatsapp_number,
            settings.alert_template_name,
            parametros=[str(nome), rotulo],
        )
        return True
    except whatsapp_client.WhatsAppError:
        logger.error("Não consegui alertar a Thainá pelo template")
        return False
