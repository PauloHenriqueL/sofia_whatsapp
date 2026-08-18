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


class TestContratoComoTextoPadrao:
    """O contrato não é prompt: é o documento que o paciente assina (Demanda E).

    Estes testes vigiam o **padrão de fábrica** (o que o botão "Resetar" restaura).
    Se alguém apagar um marcador do arquivo, todo contrato futuro que não estiver
    customizado no painel sai sem aquele dado — e o Hamilton, que renderiza,
    recusa. Falhar aqui é muito mais barato.
    """

    MARCADORES = ("{{PAC_NOME}}", "{{PAC_ENDERECO}}", "{{FIN_MENSAL}}", "{{VIG_DATA}}")

    def test_o_contrato_e_declarado_como_documento(self):
        campo = config_prompt.PROMPTS["prompt_contrato"]
        assert campo.destino == "documento"
        assert campo.vai_pro_bot is False  # nunca entra no system prompt

    def test_tem_os_quatro_marcadores(self):
        texto = config_prompt.padrao("prompt_contrato")
        for marcador in self.MARCADORES:
            assert marcador in texto, f"marcador sumiu do contrato padrão: {marcador}"

    def test_nao_tem_marcador_que_ninguem_preenche(self):
        import re

        texto = config_prompt.padrao("prompt_contrato")
        assert set(re.findall(r"\{\{[^}]*\}\}", texto)) == set(self.MARCADORES)

    def test_nao_tem_cabecalho_de_documentacao(self):
        """Tudo que estiver no arquivo vai pro contrato — inclusive um `>` de nota."""
        texto = config_prompt.padrao("prompt_contrato")
        assert not texto.lstrip().startswith(("#", ">"))
        assert "Uso pela Sofia" not in texto

    def test_reflete_as_decisoes_da_demanda(self):
        texto = config_prompt.padrao("prompt_contrato")
        # A entrada é mensalidade cheia — o sistema não cobra pro rata.
        assert "pro rata" not in texto.lower()
        assert "mensalidade integral" in texto
        # Terapeuta e supervisor não são qualificados (LGPD + independe do match)...
        assert "CRP" not in texto
        # ...mas o modelo de supervisão continua declarado (consentimento informado).
        assert "supervisão clínica continuada" in texto


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
