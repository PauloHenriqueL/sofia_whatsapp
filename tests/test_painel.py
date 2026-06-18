"""Testes do painel da Thainá (API interna + páginas HTML). Passo 7."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.database import Base, get_db
from app.main import app
from app.models import Conversa, Mensagem

AUTH = (settings.painel_user, settings.painel_password)


@pytest_asyncio.fixture
async def ambiente():
    """Engine em memória + override do get_db + cliente ASGI assíncrono."""
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _get_db_override():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_db] = _get_db_override
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, maker
    app.dependency_overrides.clear()
    await engine.dispose()


async def _seed_conversa(maker, numero="5531999998888", modo="bot"):
    async with maker() as s:
        c = Conversa(numero_whatsapp=numero, modo=modo, estado="novo")
        s.add(c)
        await s.flush()
        s.add(
            Mensagem(
                conversa_id=c.id,
                direcao="recebida",
                origem="paciente",
                tipo="texto",
                texto="oi, quero terapia",
            )
        )
        await s.commit()
        return c.id


class TestAuth:
    @pytest.mark.asyncio
    async def test_api_exige_autenticacao(self, ambiente):
        client, _ = ambiente
        resp = await client.get("/api/conversas/")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_painel_exige_autenticacao(self, ambiente):
        client, _ = ambiente
        resp = await client.get("/painel/")
        assert resp.status_code == 401


class TestListaEDetalhe:
    @pytest.mark.asyncio
    async def test_lista_conversas(self, ambiente):
        client, maker = ambiente
        await _seed_conversa(maker)
        resp = await client.get("/api/conversas/", auth=AUTH)
        assert resp.status_code == 200
        dados = resp.json()
        assert len(dados) == 1
        assert dados[0]["numero_whatsapp"] == "5531999998888"
        assert dados[0]["preview"] == "oi, quero terapia"

    @pytest.mark.asyncio
    async def test_painel_html_renderiza(self, ambiente):
        client, maker = ambiente
        await _seed_conversa(maker)
        resp = await client.get("/painel/", auth=AUTH)
        assert resp.status_code == 200
        assert "Conversas" in resp.text
        assert "5531999998888" in resp.text


class TestAcoes:
    @pytest.mark.asyncio
    async def test_responder_envia_e_persiste(self, ambiente):
        client, maker = ambiente
        cid = await _seed_conversa(maker)
        with patch(
            "app.services.painel.whatsapp_client.enviar_texto", new_callable=AsyncMock
        ) as mock_enviar:
            resp = await client.post(
                f"/api/conversas/{cid}/responder/",
                json={"texto": "Oi, aqui é a Thainá"},
                auth=AUTH,
            )
        assert resp.status_code == 200
        mock_enviar.assert_awaited_once_with("5531999998888", "Oi, aqui é a Thainá")

        async with maker() as s:
            enviada = (
                await s.execute(select(Mensagem).where(Mensagem.origem == "thaina"))
            ).scalar_one()
            assert enviada.texto == "Oi, aqui é a Thainá"
            assert enviada.direcao == "enviada"

    @pytest.mark.asyncio
    async def test_assumir_e_devolver(self, ambiente):
        client, maker = ambiente
        cid = await _seed_conversa(maker, modo="bot")

        resp = await client.post(f"/api/conversas/{cid}/assumir/", auth=AUTH)
        assert resp.status_code == 200
        async with maker() as s:
            assert (await s.get(Conversa, cid)).modo == "humano"

        resp = await client.post(f"/api/conversas/{cid}/devolver-bot/", auth=AUTH)
        assert resp.status_code == 200
        async with maker() as s:
            assert (await s.get(Conversa, cid)).modo == "bot"
