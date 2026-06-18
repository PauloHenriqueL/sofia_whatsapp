"""Sofia — FastAPI application

Monta o webhook do WhatsApp, a API interna e o painel web da Thainá.
"""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.logging_config import configurar_logging
from app.routers import api, health, painel, webhook

configurar_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup e shutdown da aplicação"""
    logger.info(f"Sofia iniciando em {settings.environment} environment")
    yield
    logger.info("Sofia encerrada")


# Criar aplicação
app = FastAPI(
    title="Sofia",
    description="Bot de WhatsApp para Allos - Clínica-escola de Psicologia",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # TODO: Restringir em produção
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Arquivos estáticos do painel (css)
app.mount(
    "/static",
    StaticFiles(directory=str(Path(__file__).resolve().parent / "static")),
    name="static",
)

# Rotas
app.include_router(health.router)
app.include_router(webhook.router)
app.include_router(api.router)
app.include_router(painel.router)


@app.get("/")
async def root():
    """Raiz: leva ao painel da Thainá."""
    return RedirectResponse("/painel/")


@app.exception_handler(Exception)
async def erro_nao_tratado(request: Request, exc: Exception):
    """Loga qualquer erro não tratado com stack trace e responde 500 genérico."""
    logger.exception(f"Erro não tratado em {request.method} {request.url.path}")
    return JSONResponse({"error": "internal_server_error"}, status_code=500)
