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
from starlette.middleware.sessions import SessionMiddleware

from app.config import settings
from app.logging_config import configurar_logging
from app.routers import api, auth, health, painel, tasks, webhook

configurar_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup e shutdown da aplicação"""
    logger.info(f"Sofia iniciando em {settings.environment} environment")
    # Carrega os valores de negócio salvos no banco pro cache em memória.
    # Falha aqui (ex.: tabela ainda não migrada) não derruba o app: usa os padrões.
    try:
        from app.database import async_session
        from app.services import config_negocio, config_prompt

        async with async_session() as db:
            await config_negocio.carregar_do_banco(db)
            await config_prompt.carregar_do_banco(db)
    except Exception:
        logger.exception("Não carreguei a config (valores/prompts); seguindo com os padrões")
    yield
    logger.info("Sofia encerrada")


# Em produção, não expõe a documentação interativa da API (/docs, /redoc).
_producao = settings.environment == "production"

# Criar aplicação
app = FastAPI(
    title="Sofia",
    description="Bot de WhatsApp para Allos - Clínica-escola de Psicologia",
    version="0.1.0",
    lifespan=lifespan,
    docs_url=None if _producao else "/docs",
    redoc_url=None if _producao else "/redoc",
    openapi_url=None if _producao else "/openapi.json",
)

# CORS: o painel é same-origin (server-rendered) e o webhook é server-to-server
# (Meta), então não há cliente browser cross-origin. Mantemos fechado para evitar
# requisições credenciadas de outras origens. `*` + credentials seria inseguro.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Sessão do painel (cookie assinado). https_only só em produção (dev é http).
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    https_only=_producao,
    same_site="lax",
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
app.include_router(auth.router)
app.include_router(api.router)
app.include_router(painel.router)
app.include_router(tasks.router)


@app.get("/")
async def root():
    """Raiz: leva ao painel da Thainá."""
    return RedirectResponse("/painel/")


@app.exception_handler(Exception)
async def erro_nao_tratado(request: Request, exc: Exception):
    """Loga qualquer erro não tratado com stack trace e responde 500 genérico."""
    logger.exception(f"Erro não tratado em {request.method} {request.url.path}")
    return JSONResponse({"error": "internal_server_error"}, status_code=500)
