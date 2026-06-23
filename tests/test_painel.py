"""Testes do painel da Thainá: login por sessão, API, páginas e CSRF."""

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
from app.services import painel as painel_service


class TestUrlHamiltonPaciente:
    def test_monta_url_da_tela_de_edicao(self):
        original = settings.hamilton_api_url
        settings.hamilton_api_url = "https://hamilton-v2.onrender.com/"
        try:
            url = painel_service.url_hamilton_paciente(123)
        finally:
            settings.hamilton_api_url = original
        assert url == "https://hamilton-v2.onrender.com/api/v1/pacientes/123/editar/"

    def test_sem_id_retorna_none(self):
        assert painel_service.url_hamilton_paciente(None) is None


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


async def _login(client):
    resp = await client.post(
        "/login",
        data={"usuario": settings.painel_user, "senha": settings.painel_password},
    )
    assert resp.status_code == 303


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


class TestLogin:
    @pytest.mark.asyncio
    async def test_pagina_login_abre(self, ambiente):
        client, _ = ambiente
        resp = await client.get("/login")
        assert resp.status_code == 200
        assert "Allos" in resp.text

    @pytest.mark.asyncio
    async def test_login_invalido(self, ambiente):
        client, _ = ambiente
        resp = await client.post("/login", data={"usuario": "x", "senha": "y"})
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_login_valido(self, ambiente):
        client, _ = ambiente
        await _login(client)


class TestAuth:
    @pytest.mark.asyncio
    async def test_api_exige_login(self, ambiente):
        client, _ = ambiente
        resp = await client.get("/api/conversas/")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_painel_sem_login_redireciona(self, ambiente):
        client, _ = ambiente
        resp = await client.get("/painel/")
        assert resp.status_code == 303
        assert resp.headers["location"] == "/login"


class TestListaEDetalhe:
    @pytest.mark.asyncio
    async def test_lista_conversas(self, ambiente):
        client, maker = ambiente
        await _login(client)
        await _seed_conversa(maker)
        resp = await client.get("/api/conversas/")
        assert resp.status_code == 200
        dados = resp.json()
        assert len(dados) == 1
        assert dados[0]["numero_whatsapp"] == "5531999998888"
        assert dados[0]["preview"] == "oi, quero terapia"

    @pytest.mark.asyncio
    async def test_painel_html_renderiza(self, ambiente):
        client, maker = ambiente
        await _login(client)
        await _seed_conversa(maker)
        resp = await client.get("/painel/")
        assert resp.status_code == 200
        assert "Conversas" in resp.text
        assert "5531999998888" in resp.text


class TestAcoes:
    @pytest.mark.asyncio
    async def test_responder_envia_e_persiste(self, ambiente):
        client, maker = ambiente
        await _login(client)
        cid = await _seed_conversa(maker)
        with patch(
            "app.services.painel.whatsapp_client.enviar_texto", new_callable=AsyncMock
        ) as mock_enviar:
            resp = await client.post(
                f"/api/conversas/{cid}/responder/",
                json={"texto": "Oi, aqui é a Thainá"},
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
        await _login(client)
        cid = await _seed_conversa(maker, modo="bot")

        resp = await client.post(f"/api/conversas/{cid}/assumir/")
        assert resp.status_code == 200
        async with maker() as s:
            assert (await s.get(Conversa, cid)).modo == "humano"

        resp = await client.post(f"/api/conversas/{cid}/devolver-bot/")
        assert resp.status_code == 200
        async with maker() as s:
            assert (await s.get(Conversa, cid)).modo == "bot"


class TestCSRF:
    @pytest.mark.asyncio
    async def test_post_de_outra_origem_e_rejeitado(self, ambiente):
        """Mesmo logado, POST cross-site (Origin de outro host) é bloqueado."""
        client, maker = ambiente
        await _login(client)
        cid = await _seed_conversa(maker)
        resp = await client.post(
            f"/api/conversas/{cid}/assumir/",
            headers={"Origin": "http://site-malicioso.example"},
        )
        assert resp.status_code == 403
        async with maker() as s:
            assert (await s.get(Conversa, cid)).modo == "bot"  # não mudou
