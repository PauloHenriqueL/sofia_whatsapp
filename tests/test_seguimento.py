"""Testes do follow-up de lead parado (Frente 2): seleção, envio e endpoint."""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app import config
from app.database import Base, get_db
from app.main import app
from app.models import Conversa, Mensagem
from app.services import seguimento

# Tudo em horário "ingênuo" (naive) e consistente, pra não esbarrar no
# tratamento de timezone do SQLite nos testes.
AGORA = datetime(2026, 6, 24, 12, 0, 0)


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


async def _lead(session, *, numero, horas_atras, **kwargs):
    """Cria uma conversa com uma mensagem recebida `horas_atras` horas atrás."""
    conversa = Conversa(numero_whatsapp=numero, **kwargs)
    session.add(conversa)
    await session.flush()
    session.add(
        Mensagem(
            conversa_id=conversa.id,
            direcao="recebida",
            origem="paciente",
            tipo="texto",
            texto="oi",
            criada_em=AGORA - timedelta(hours=horas_atras),
        )
    )
    await session.flush()
    return conversa


class TestBuscarLeadsParados:
    @pytest.mark.asyncio
    async def test_lead_na_janela_e_elegivel(self, session):
        await _lead(session, numero="5531900000001", horas_atras=21)
        leads = await seguimento.buscar_leads_parados(session, AGORA)
        assert [c.numero_whatsapp for c in leads] == ["5531900000001"]

    @pytest.mark.asyncio
    async def test_recente_demais_nao_entra(self, session):
        await _lead(session, numero="5531900000002", horas_atras=2)
        assert await seguimento.buscar_leads_parados(session, AGORA) == []

    @pytest.mark.asyncio
    async def test_fora_da_janela_de_24h_nao_entra(self, session):
        await _lead(session, numero="5531900000003", horas_atras=30)
        assert await seguimento.buscar_leads_parados(session, AGORA) == []

    @pytest.mark.asyncio
    async def test_ja_cadastrado_nao_entra(self, session):
        await _lead(
            session,
            numero="5531900000004",
            horas_atras=21,
            paciente_hamilton_id=10,
            estado="cadastrado",
        )
        assert await seguimento.buscar_leads_parados(session, AGORA) == []

    @pytest.mark.asyncio
    async def test_em_modo_humano_nao_entra(self, session):
        await _lead(
            session,
            numero="5531900000005",
            horas_atras=21,
            modo="humano",
            estado="escalado",
        )
        assert await seguimento.buscar_leads_parados(session, AGORA) == []

    @pytest.mark.asyncio
    async def test_ja_seguido_nao_entra(self, session):
        await _lead(
            session,
            numero="5531900000006",
            horas_atras=21,
            seguimento_enviado_em=AGORA - timedelta(hours=1),
        )
        assert await seguimento.buscar_leads_parados(session, AGORA) == []


class TestRodarSeguimentos:
    @pytest.mark.asyncio
    async def test_envia_marca_e_nao_reenvia(self, session):
        await _lead(session, numero="5531900000007", horas_atras=21)
        with patch(
            "app.services.seguimento.whatsapp_client.enviar_texto", new_callable=AsyncMock
        ) as mock_envia:
            enviados = await seguimento.rodar_seguimentos(session, AGORA)
        assert enviados == 1
        mock_envia.assert_awaited_once()

        # Segunda rodada: a conversa já foi seguida, não reenvia.
        with patch(
            "app.services.seguimento.whatsapp_client.enviar_texto", new_callable=AsyncMock
        ) as mock_envia2:
            assert await seguimento.rodar_seguimentos(session, AGORA) == 0
        mock_envia2.assert_not_awaited()


@pytest_asyncio.fixture
async def cliente():
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
        yield client
    app.dependency_overrides.clear()
    await engine.dispose()


class TestEndpoint:
    @pytest.mark.asyncio
    async def test_403_sem_token(self, cliente):
        original = config.settings.tasks_token
        config.settings.tasks_token = "segredo"
        try:
            resp = await cliente.post("/tasks/seguimentos")
        finally:
            config.settings.tasks_token = original
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_dispara_com_token(self, cliente):
        original = config.settings.tasks_token
        config.settings.tasks_token = "segredo"
        try:
            resp = await cliente.post("/tasks/seguimentos", headers={"X-Tasks-Token": "segredo"})
        finally:
            config.settings.tasks_token = original
        assert resp.status_code == 200
        assert resp.json() == {"enviados": 0}
