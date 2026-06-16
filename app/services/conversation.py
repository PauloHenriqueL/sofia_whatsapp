"""Orquestração da conversa e persistência.

Passo 3: cria/busca conversa por número, persiste mensagens recebidas
(com idempotência por whatsapp_message_id) e enviadas.
"""

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Conversa, Mensagem

logger = logging.getLogger(__name__)


async def obter_ou_criar_conversa(session: AsyncSession, numero: str) -> Conversa:
    """Retorna a conversa do número, criando-a se ainda não existir."""
    result = await session.execute(
        select(Conversa).where(Conversa.numero_whatsapp == numero)
    )
    conversa = result.scalar_one_or_none()
    if conversa is None:
        conversa = Conversa(numero_whatsapp=numero)
        session.add(conversa)
        await session.flush()  # garante conversa.id disponível
        logger.info(f"Nova conversa criada para {numero} (id={conversa.id})")
    return conversa


async def mensagem_ja_processada(
    session: AsyncSession, whatsapp_message_id: str | None
) -> bool:
    """Idempotência: True se a mensagem da Meta já foi persistida antes."""
    if not whatsapp_message_id:
        return False
    result = await session.execute(
        select(Mensagem.id).where(
            Mensagem.whatsapp_message_id == whatsapp_message_id
        )
    )
    return result.scalar_one_or_none() is not None


async def registrar_mensagem_recebida(
    session: AsyncSession,
    conversa: Conversa,
    tipo: str,
    texto: str | None,
    whatsapp_message_id: str | None = None,
    extra: dict | None = None,
) -> Mensagem:
    """Persiste uma mensagem recebida do paciente."""
    mensagem = Mensagem(
        conversa_id=conversa.id,
        direcao="recebida",
        origem="paciente",
        tipo=tipo,
        texto=texto,
        whatsapp_message_id=whatsapp_message_id,
        extra=extra or {},
    )
    session.add(mensagem)
    await session.flush()
    return mensagem


async def registrar_mensagem_enviada(
    session: AsyncSession,
    conversa: Conversa,
    texto: str,
    origem: str = "bot",
    tipo: str = "texto",
    whatsapp_message_id: str | None = None,
) -> Mensagem:
    """Persiste uma mensagem enviada (bot ou Thainá)."""
    mensagem = Mensagem(
        conversa_id=conversa.id,
        direcao="enviada",
        origem=origem,
        tipo=tipo,
        texto=texto,
        whatsapp_message_id=whatsapp_message_id,
    )
    session.add(mensagem)
    await session.flush()
    return mensagem
