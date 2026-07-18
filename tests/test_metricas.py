"""Testes dos KPIs do painel (Frente 3)."""

from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base
from app.models import Conversa, Escalada, Mensagem
from app.services import metricas

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


async def _conversa(session, *, numero, **kwargs):
    kwargs.setdefault("criada_em", AGORA)
    c = Conversa(numero_whatsapp=numero, **kwargs)
    session.add(c)
    await session.flush()
    return c


class TestCalcularMetricas:
    @pytest.mark.asyncio
    async def test_contagens_basicas_e_conversao(self, session):
        await _conversa(session, numero="551", paciente_hamilton_id=1, estado="cadastrado")
        await _conversa(session, numero="552", paciente_hamilton_id=2, estado="cadastrado")
        await _conversa(session, numero="553", estado="novo")  # lead sem cadastro
        await _conversa(session, numero="554", modo="humano", estado="escalado")

        m = await metricas.calcular_metricas(session, AGORA)

        assert m["total"] == 4
        assert m["leads_hoje"] == 4
        assert m["cadastrados"] == 2
        assert m["taxa_conversao"] == 50  # 2/4
        assert m["humano"] == 1
        assert m["escalados"] == 1
        assert m["autonomia"] == 75  # (4-1)/4

    @pytest.mark.asyncio
    async def test_arquivada_continua_contando_nos_kpis(self, session):
        """Arquivar tira da lista do painel, mas não apaga o histórico dos KPIs."""
        await _conversa(
            session,
            numero="555",
            paciente_hamilton_id=3,
            estado="cadastrado",
            arquivada_em=AGORA,
        )
        m = await metricas.calcular_metricas(session, AGORA)
        assert m["total"] == 1
        assert m["cadastrados"] == 1

    @pytest.mark.asyncio
    async def test_pendentes_e_escaladas_por_motivo(self, session):
        c = await _conversa(session, numero="561", estado="cadastro_pendente")
        session.add(Escalada(conversa_id=c.id, motivo="preco"))
        c2 = await _conversa(session, numero="562", modo="humano", estado="escalado")
        session.add(Escalada(conversa_id=c2.id, motivo="preco"))
        c3 = await _conversa(session, numero="563", modo="humano", estado="escalado")
        session.add(Escalada(conversa_id=c3.id, motivo="neuro_reuniao"))
        await session.flush()

        m = await metricas.calcular_metricas(session, AGORA)

        assert m["pendentes"] == 1
        # Ordenado por frequência: preco (2) antes de neuro_reuniao (1).
        assert m["escaladas_por_motivo"][0]["motivo"] == "preco"
        assert m["escaladas_por_motivo"][0]["qtd"] == 2
        assert m["escaladas_por_motivo"][0]["rotulo"] != "preco"  # rótulo legível

    @pytest.mark.asyncio
    async def test_followup_recuperado(self, session):
        # Levou follow-up e respondeu depois -> recuperado.
        c = await _conversa(session, numero="571", seguimento_enviado_em=AGORA - timedelta(hours=2))
        session.add(
            Mensagem(
                conversa_id=c.id,
                direcao="recebida",
                origem="paciente",
                tipo="texto",
                texto="ainda quero sim",
                criada_em=AGORA - timedelta(hours=1),
            )
        )
        # Levou follow-up e ficou quieto -> não recuperado.
        await _conversa(session, numero="572", seguimento_enviado_em=AGORA - timedelta(hours=2))
        await session.flush()

        m = await metricas.calcular_metricas(session, AGORA)

        assert m["followups"] == 2
        assert m["recuperados"] == 1

    @pytest.mark.asyncio
    async def test_banco_vazio_nao_quebra(self, session):
        m = await metricas.calcular_metricas(session, AGORA)
        assert m["total"] == 0
        assert m["taxa_conversao"] == 0
        assert m["autonomia"] == 0
        assert len(m["leads_por_dia"]) == 7
