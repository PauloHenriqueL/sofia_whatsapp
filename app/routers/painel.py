"""Painel web da Thainá (HTML server-rendered + HTMX). HTTP Basic Auth."""

import logging
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db, requer_login_pagina, verificar_origem
from app.services import cadastro, metricas, painel, whatsapp_client

logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/painel",
    tags=["painel"],
    dependencies=[Depends(requer_login_pagina), Depends(verificar_origem)],
)

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


def _fmt_data(valor):
    if not isinstance(valor, datetime):
        return ""
    return valor.strftime("%d/%m %H:%M")


def _ha_quanto_tempo(valor):
    if not isinstance(valor, datetime):
        return ""
    agora = datetime.now(timezone.utc)
    ref = valor if valor.tzinfo else valor.replace(tzinfo=timezone.utc)
    minutos = int((agora - ref).total_seconds() // 60)
    if minutos < 1:
        return "agora"
    if minutos < 60:
        return f"há {minutos} min"
    horas = minutos // 60
    if horas < 24:
        return f"há {horas}h"
    return f"há {horas // 24}d"


templates.env.filters["data"] = _fmt_data
templates.env.filters["desde"] = _ha_quanto_tempo


@router.get("/")
async def pagina_lista(request: Request, filtro: str = "todas", db: AsyncSession = Depends(get_db)):
    conversas = await painel.listar_conversas(db, filtro=filtro)
    return templates.TemplateResponse(
        "painel_lista.html",
        {"request": request, "conversas": conversas, "filtro": filtro},
    )


@router.get("/metricas")
async def pagina_metricas(request: Request, db: AsyncSession = Depends(get_db)):
    m = await metricas.calcular_metricas(db)
    return templates.TemplateResponse(
        "painel_metricas.html",
        {"request": request, "m": m},
    )


@router.get("/fragment/conversas")
async def fragment_conversas(
    request: Request, filtro: str = "todas", db: AsyncSession = Depends(get_db)
):
    conversas = await painel.listar_conversas(db, filtro=filtro)
    return templates.TemplateResponse(
        "_conversas_fragment.html",
        {"request": request, "conversas": conversas, "filtro": filtro},
    )


@router.get("/conversas/{conversa_id}/")
async def pagina_conversa(request: Request, conversa_id: int, db: AsyncSession = Depends(get_db)):
    conversa = await painel.obter_conversa(db, conversa_id)
    if conversa is None:
        raise HTTPException(status_code=404, detail="Conversa não encontrada")
    mensagens = await painel.carregar_mensagens(db, conversa_id)
    return templates.TemplateResponse(
        "painel_conversa.html",
        {
            "request": request,
            "conversa": conversa,
            "mensagens": mensagens,
            "hamilton_url": painel.url_hamilton_paciente(conversa.paciente_hamilton_id),
        },
    )


@router.get("/conversas/{conversa_id}/fragment/mensagens")
async def fragment_mensagens(
    request: Request, conversa_id: int, db: AsyncSession = Depends(get_db)
):
    mensagens = await painel.carregar_mensagens(db, conversa_id)
    return templates.TemplateResponse(
        "_mensagens_fragment.html",
        {"request": request, "mensagens": mensagens},
    )


@router.post("/conversas/{conversa_id}/responder")
async def responder(
    request: Request,
    conversa_id: int,
    texto: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    conversa = await painel.obter_conversa(db, conversa_id)
    if conversa is None:
        raise HTTPException(status_code=404, detail="Conversa não encontrada")
    texto = texto.strip()
    if texto:
        try:
            await painel.responder_como_thaina(db, conversa, texto)
        except whatsapp_client.WhatsAppError:
            logger.error(f"Falha ao enviar resposta da Thainá (conversa {conversa_id})")
    mensagens = await painel.carregar_mensagens(db, conversa_id)
    return templates.TemplateResponse(
        "_mensagens_fragment.html",
        {"request": request, "mensagens": mensagens},
    )


@router.post("/conversas/{conversa_id}/assumir")
async def assumir(conversa_id: int, db: AsyncSession = Depends(get_db)):
    conversa = await painel.obter_conversa(db, conversa_id)
    if conversa is None:
        raise HTTPException(status_code=404, detail="Conversa não encontrada")
    await painel.assumir(db, conversa)
    return RedirectResponse(f"/painel/conversas/{conversa_id}/", status_code=303)


@router.post("/conversas/{conversa_id}/devolver-bot")
async def devolver_bot(conversa_id: int, db: AsyncSession = Depends(get_db)):
    conversa = await painel.obter_conversa(db, conversa_id)
    if conversa is None:
        raise HTTPException(status_code=404, detail="Conversa não encontrada")
    await painel.devolver_ao_bot(db, conversa)
    return RedirectResponse(f"/painel/conversas/{conversa_id}/", status_code=303)


@router.post("/conversas/{conversa_id}/cadastrar")
async def cadastrar(conversa_id: int, db: AsyncSession = Depends(get_db)):
    """Tenta (ou re-tenta) cadastrar o paciente no Hamilton com os dados coletados."""
    conversa = await painel.obter_conversa(db, conversa_id)
    if conversa is None:
        raise HTTPException(status_code=404, detail="Conversa não encontrada")
    await cadastro.cadastrar_paciente(db, conversa)
    await db.commit()
    return RedirectResponse(f"/painel/conversas/{conversa_id}/", status_code=303)
