"""Testes dos prompts editáveis no painel (config_prompt)."""

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base
from app.services import config_prompt, llm_client


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
def _limpa_cache():
    # O _cache é global; restaura no fim pra não vazar entre testes.
    snap = dict(config_prompt._cache)
    yield
    config_prompt._cache.clear()
    config_prompt._cache.update(snap)


class TestConfigPrompt:
    def test_texto_usa_o_arquivo_por_padrao(self):
        t = config_prompt.texto("prompt_sistema")
        assert "Sofia" in t
        assert t == config_prompt.padrao("prompt_sistema")  # == conteúdo do arquivo
        assert config_prompt.customizado("prompt_sistema") is False

    @pytest.mark.asyncio
    async def test_salvar_vira_override_e_reflete_no_system_prompt(self, session):
        await config_prompt.salvar(session, "prompt_sistema", "PROMPT DE TESTE CUSTOMIZADO")
        assert config_prompt.texto("prompt_sistema") == "PROMPT DE TESTE CUSTOMIZADO"
        assert config_prompt.customizado("prompt_sistema") is True
        # O que o modelo recebe passa a usar o texto editado.
        assert "PROMPT DE TESTE CUSTOMIZADO" in llm_client.carregar_system_prompt()

    @pytest.mark.asyncio
    async def test_resetar_volta_pro_padrao(self, session):
        await config_prompt.salvar(session, "prompt_base", "base custom")
        assert config_prompt.customizado("prompt_base") is True
        await config_prompt.resetar(session, "prompt_base")
        assert config_prompt.customizado("prompt_base") is False
        assert config_prompt.texto("prompt_base") == config_prompt.padrao("prompt_base")
