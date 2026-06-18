"""Operações do painel da Thainá (consultas e ações sobre conversas).

Compartilhado pelos routers de API (JSON) e de painel (HTML/HTMX) para não
duplicar a lógica de listar, responder, assumir e devolver ao bot.
"""

from datetime import datetime, timezone

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Conversa, Mensagem
from app.services import conversation, whatsapp_client


async def listar_conversas(
    db: AsyncSession, filtro: str = "todas", limite: int = 50, offset: int = 0
) -> list[dict]:
    """Lista conversas (mais recentes primeiro) com preview da última mensagem."""
    q = select(Conversa).order_by(desc(Conversa.atualizada_em))

    if filtro == "humano":
        q = q.where(Conversa.modo == "humano")
    elif filtro == "escalada":
        q = q.where(Conversa.estado == "escalado")
    elif filtro == "cadastradas_hoje":
        inicio = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        q = q.where(Conversa.estado == "cadastrado", Conversa.atualizada_em >= inicio)

    q = q.limit(limite).offset(offset)
    conversas = (await db.execute(q)).scalars().all()

    resultado = []
    for c in conversas:
        ultima = (
            await db.execute(
                select(Mensagem)
                .where(Mensagem.conversa_id == c.id)
                .order_by(desc(Mensagem.criada_em), desc(Mensagem.id))
                .limit(1)
            )
        ).scalar_one_or_none()
        resultado.append(
            {
                "id": c.id,
                "numero_whatsapp": c.numero_whatsapp,
                "nome": (c.dados_coletados or {}).get("nome_completo"),
                "modo": c.modo,
                "estado": c.estado,
                "paciente_hamilton_id": c.paciente_hamilton_id,
                "atualizada_em": c.atualizada_em,
                "preview": (ultima.texto[:80] if ultima and ultima.texto else None),
            }
        )
    return resultado


async def obter_conversa(db: AsyncSession, conversa_id: int) -> Conversa | None:
    return await db.get(Conversa, conversa_id)


async def carregar_mensagens(db: AsyncSession, conversa_id: int) -> list[Mensagem]:
    """Mensagens da conversa em ordem cronológica (para exibir no chat)."""
    result = await db.execute(
        select(Mensagem)
        .where(Mensagem.conversa_id == conversa_id)
        .order_by(Mensagem.criada_em, Mensagem.id)
    )
    return list(result.scalars().all())


async def responder_como_thaina(db: AsyncSession, conversa: Conversa, texto: str) -> None:
    """Envia a resposta da Thainá pelo WhatsApp e persiste com origem='thaina'."""
    await whatsapp_client.enviar_texto(conversa.numero_whatsapp, texto)
    await conversation.registrar_mensagem_enviada(db, conversa, texto=texto, origem="thaina")
    await db.commit()


async def assumir(db: AsyncSession, conversa: Conversa) -> None:
    """Thainá assume a conversa: passa para modo humano (bot para de responder)."""
    conversa.modo = "humano"
    await db.commit()


async def devolver_ao_bot(db: AsyncSession, conversa: Conversa) -> None:
    """Encerra o atendimento humano e devolve a conversa ao bot."""
    conversa.modo = "bot"
    await db.commit()
