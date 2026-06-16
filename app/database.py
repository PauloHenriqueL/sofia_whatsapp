"""Conexão async com o banco e sessão SQLAlchemy.

Portável: usa asyncpg em Postgres (produção/Neon) e aiosqlite em SQLite
(dev local). A escolha do driver é derivada do esquema do DATABASE_URL.
"""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import settings


def _async_url(url: str) -> str:
    """Converte um DATABASE_URL síncrono para o driver async correspondente."""
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+asyncpg://", 1)
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if url.startswith("sqlite://"):
        return url.replace("sqlite://", "sqlite+aiosqlite://", 1)
    return url


class Base(DeclarativeBase):
    """Base declarativa dos modelos."""


engine = create_async_engine(_async_url(settings.database_url), echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Dependency do FastAPI que fornece uma sessão por request."""
    async with async_session() as session:
        yield session
