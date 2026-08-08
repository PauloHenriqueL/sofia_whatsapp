"""Desconto que a Sofia pode oferecer sozinha na mensalidade (tool `oferecer_desconto`).

O ponto destes testes é que o VALOR nunca vem do modelo. Desconto é dinheiro
recorrente: um número inventado não custa R$ 20, custa R$ 20 por mês pelo tempo
que a pessoa ficar. O modelo pede autorização; quem calcula é o código.
"""

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base
from app.models import Conversa
from app.routers.webhook import _autorizar_desconto, _executar_tool
from app.services import config_negocio, llm_client, tools


@pytest_asyncio.fixture
async def sessao():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


@pytest.fixture(autouse=True)
def _config_isolada():
    orig = dict(config_negocio._cache)
    config_negocio._cache["preco_terapia_mensal"] = 200
    config_negocio._cache["desconto_maximo_pct"] = 10
    yield
    config_negocio._cache.clear()
    config_negocio._cache.update(orig)


async def _conversa(sessao) -> Conversa:
    c = Conversa(numero_whatsapp="5531999990000")
    sessao.add(c)
    await sessao.commit()
    return c


class TestValorVemDoCodigo:
    @pytest.mark.asyncio
    async def test_calcula_o_valor_e_nao_aceita_do_modelo(self, sessao):
        conversa = await _conversa(sessao)
        r = await _autorizar_desconto(sessao, conversa, "está desempregada")
        assert r["autorizado"] is True
        assert r["valor_mensal"] == 180  # 200 - 10%
        assert r["valor_cheio"] == 200

    @pytest.mark.asyncio
    async def test_percentual_do_painel_manda(self, sessao):
        config_negocio._cache["desconto_maximo_pct"] = 25
        conversa = await _conversa(sessao)
        r = await _autorizar_desconto(sessao, conversa, "sem renda")
        assert r["valor_mensal"] == 150

    @pytest.mark.asyncio
    async def test_zero_por_cento_desliga(self, sessao):
        # Com o desconto desligado a objeção de preço tem que ir pra Thainá, e a
        # Sofia não pode "arredondar" nada por conta própria.
        config_negocio._cache["desconto_maximo_pct"] = 0
        conversa = await _conversa(sessao)
        r = await _autorizar_desconto(sessao, conversa, "sem renda")
        assert r["autorizado"] is False
        assert "valor_mensal" not in r
        assert conversa.desconto_oferecido_em is None


class TestRegistroParaAuditoria:
    @pytest.mark.asyncio
    async def test_grava_valor_motivo_e_momento(self, sessao):
        conversa = await _conversa(sessao)
        await _autorizar_desconto(sessao, conversa, "desempregada desde março")
        assert conversa.desconto_valor == 180
        assert conversa.desconto_motivo == "desempregada desde março"
        assert conversa.desconto_oferecido_em is not None

    @pytest.mark.asyncio
    async def test_guarda_valor_e_nao_percentual(self, sessao):
        # O percentual pode mudar no painel depois; o histórico tem que continuar
        # dizendo quanto foi realmente oferecido àquela pessoa.
        conversa = await _conversa(sessao)
        await _autorizar_desconto(sessao, conversa, "x")
        config_negocio._cache["desconto_maximo_pct"] = 50
        assert conversa.desconto_valor == 180

    @pytest.mark.asyncio
    async def test_motivo_vazio_vira_nulo(self, sessao):
        conversa = await _conversa(sessao)
        await _autorizar_desconto(sessao, conversa, "   ")
        assert conversa.desconto_motivo is None


class TestUmDescontoPorConversa:
    @pytest.mark.asyncio
    async def test_segunda_chamada_nao_baixa_de_novo(self, sessao):
        conversa = await _conversa(sessao)
        await _autorizar_desconto(sessao, conversa, "primeiro pedido")
        r = await _autorizar_desconto(sessao, conversa, "insistindo")
        assert r["ja_oferecido_antes"] is True
        assert r["valor_mensal"] == 180
        assert "não baixe mais" in r["instrucao"].lower()

    @pytest.mark.asyncio
    async def test_nao_sobrescreve_o_registro_original(self, sessao):
        conversa = await _conversa(sessao)
        await _autorizar_desconto(sessao, conversa, "motivo original")
        primeiro = conversa.desconto_oferecido_em
        await _autorizar_desconto(sessao, conversa, "outro motivo")
        assert conversa.desconto_motivo == "motivo original"
        assert conversa.desconto_oferecido_em == primeiro


class TestIntegracaoComODispatcher:
    @pytest.mark.asyncio
    async def test_tool_chega_pelo_executar_tool(self, sessao):
        conversa = await _conversa(sessao)
        tc = llm_client.ToolCall(
            id="1", name=tools.OFERECER_DESCONTO, arguments={"motivo": "apertada"}
        )
        r = await _executar_tool(sessao, conversa, tc)
        assert r["autorizado"] is True
        assert r["valor_mensal"] == 180

    def test_tool_esta_exposta_ao_modelo(self):
        nomes = [t["function"]["name"] for t in tools.TOOLS]
        assert tools.OFERECER_DESCONTO in nomes

    def test_schema_nao_deixa_o_modelo_escolher_valor(self):
        # Se o schema tivesse um campo de valor, o modelo preencheria — e aí o
        # desconto passaria a ser opinião do LLM sobre dinheiro recorrente.
        tool = next(t for t in tools.TOOLS if t["function"]["name"] == tools.OFERECER_DESCONTO)
        props = tool["function"]["parameters"]["properties"]
        assert set(props) == {"motivo"}
