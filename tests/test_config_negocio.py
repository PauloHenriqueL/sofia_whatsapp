"""Testes da config de negócio editável (cache + persistência + injeção no prompt)."""

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base
from app.models import Configuracao
from app.services import config_negocio, llm_client


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


@pytest.fixture(autouse=True)
def restaura_cache():
    # O cache é global; restaura no fim pra não vazar entre testes.
    snap = dict(config_negocio._cache)
    yield
    config_negocio._cache.clear()
    config_negocio._cache.update(snap)


class TestConfigNegocio:
    def test_valor_padrao_sem_banco(self):
        assert config_negocio.valor("preco_neuro") == config_negocio.CAMPOS["preco_neuro"][1]

    @pytest.mark.asyncio
    async def test_salvar_atualiza_cache_e_banco(self, session):
        await config_negocio.salvar(session, {"preco_neuro": 1500, "parcelas_max": 10})
        assert config_negocio.valor("preco_neuro") == 1500
        assert config_negocio.valor("parcelas_max") == 10
        guardado = (
            await session.execute(
                Configuracao.__table__.select().where(Configuracao.chave == "preco_neuro")
            )
        ).first()
        assert guardado.valor == "1500"

    @pytest.mark.asyncio
    async def test_carregar_do_banco_sobrepoe_padrao(self, session):
        session.add(Configuracao(chave="preco_neuro", valor="1800"))
        await session.commit()
        # zera o cache pro padrão e recarrega do banco
        config_negocio._cache["preco_neuro"] = config_negocio.CAMPOS["preco_neuro"][1]
        await config_negocio.carregar_do_banco(session)
        assert config_negocio.valor("preco_neuro") == 1800

    @pytest.mark.asyncio
    async def test_valor_novo_reflete_no_prompt(self, session):
        await config_negocio.salvar(session, {"preco_neuro": 1999})
        prompt = llm_client.carregar_system_prompt()
        assert "1.999" in prompt  # formatado em reais e injetado
