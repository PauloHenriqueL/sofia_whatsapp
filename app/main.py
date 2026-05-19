"""Sofia — FastAPI application
Passo 1: Esqueleto + Webhook em modo eco
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.routers import health, webhook

# Configurar logging
logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
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

# Rotas
app.include_router(health.router)
app.include_router(webhook.router)


@app.get("/")
async def root():
    """Root endpoint"""
    return {
        "status": "ok",
        "app": "Sofia",
        "version": "0.1.0",
        "environment": settings.environment,
    }
