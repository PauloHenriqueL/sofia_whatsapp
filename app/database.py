"""Conexão async com o banco e sessão SQLAlchemy.

Portável: usa asyncpg em Postgres (produção/Neon) e aiosqlite em SQLite
(dev local). A escolha do driver é derivada do esquema do DATABASE_URL.
"""

from collections.abc import AsyncGenerator
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

# Params estilo libpq que o asyncpg NÃO aceita (o Neon costuma incluir sslmode
# e channel_binding na URL). O SSL é ligado via connect_args (ver _connect_args).
_PARAMS_INCOMPATIVEIS = {"sslmode", "channel_binding"}


def _async_url(url: str) -> str:
    """Converte um DATABASE_URL síncrono para o driver async correspondente.

    Em Postgres, também remove params libpq que o asyncpg não entende (ex.: o
    `sslmode=require` que o Neon adiciona) — o TLS é configurado em connect_args.
    """
    if url.startswith("postgres://"):
        url = "postgresql+asyncpg://" + url[len("postgres://") :]
    elif url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + url[len("postgresql://") :]
    elif url.startswith("sqlite://"):
        return url.replace("sqlite://", "sqlite+aiosqlite://", 1)
    else:
        return url

    partes = urlsplit(url)
    query = [
        (k, v) for k, v in parse_qsl(partes.query) if k not in _PARAMS_INCOMPATIVEIS
    ]
    return urlunsplit(partes._replace(query=urlencode(query)))


def _connect_args(url: str) -> dict:
    """Liga SSL no asyncpg (o Neon exige TLS). Em SQLite não há SSL."""
    return {"ssl": True} if url.startswith("postgresql+asyncpg") else {}


class Base(DeclarativeBase):
    """Base declarativa dos modelos."""


_DB_URL = _async_url(settings.database_url)
engine = create_async_engine(_DB_URL, echo=False, connect_args=_connect_args(_DB_URL))
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Dependency do FastAPI que fornece uma sessão por request."""
    async with async_session() as session:
        yield session
