"""API interna consumida pelo painel (JSON). Protegida por HTTP Basic Auth."""

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import autenticar, get_db
from app.services import painel, whatsapp_client

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["api"], dependencies=[Depends(autenticar)])


class RespostaIn(BaseModel):
    texto: str


@router.get("/conversas/")
async def listar_conversas(
    filtro: str = "todas",
    limite: int = 50,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
):
    return await painel.listar_conversas(db, filtro=filtro, limite=limite, offset=offset)


@router.get("/conversas/{conversa_id}/")
async def detalhar_conversa(conversa_id: int, db: AsyncSession = Depends(get_db)):
    conversa = await painel.obter_conversa(db, conversa_id)
    if conversa is None:
        raise HTTPException(status_code=404, detail="Conversa não encontrada")
    mensagens = await painel.carregar_mensagens(db, conversa_id)
    return {
        "id": conversa.id,
        "numero_whatsapp": conversa.numero_whatsapp,
        "modo": conversa.modo,
        "estado": conversa.estado,
        "paciente_hamilton_id": conversa.paciente_hamilton_id,
        "dados_coletados": conversa.dados_coletados,
        "mensagens": [
            {
                "id": m.id,
                "direcao": m.direcao,
                "origem": m.origem,
                "tipo": m.tipo,
                "texto": m.texto,
                "criada_em": m.criada_em,
            }
            for m in mensagens
        ],
    }


@router.post("/conversas/{conversa_id}/responder/")
async def responder(conversa_id: int, payload: RespostaIn, db: AsyncSession = Depends(get_db)):
    conversa = await painel.obter_conversa(db, conversa_id)
    if conversa is None:
        raise HTTPException(status_code=404, detail="Conversa não encontrada")
    texto = payload.texto.strip()
    if not texto:
        raise HTTPException(status_code=400, detail="Texto vazio")
    try:
        await painel.responder_como_thaina(db, conversa, texto)
    except whatsapp_client.WhatsAppError:
        raise HTTPException(status_code=502, detail="Falha ao enviar pela Cloud API")
    return {"status": "enviado"}


@router.post("/conversas/{conversa_id}/assumir/")
async def assumir(conversa_id: int, db: AsyncSession = Depends(get_db)):
    conversa = await painel.obter_conversa(db, conversa_id)
    if conversa is None:
        raise HTTPException(status_code=404, detail="Conversa não encontrada")
    await painel.assumir(db, conversa)
    return {"status": "ok", "modo": "humano"}


@router.post("/conversas/{conversa_id}/devolver-bot/")
async def devolver_bot(conversa_id: int, db: AsyncSession = Depends(get_db)):
    conversa = await painel.obter_conversa(db, conversa_id)
    if conversa is None:
        raise HTTPException(status_code=404, detail="Conversa não encontrada")
    await painel.devolver_ao_bot(db, conversa)
    return {"status": "ok", "modo": "bot"}
