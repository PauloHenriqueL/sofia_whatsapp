"""Testes do acompanhamento pós-cadastro (Demandas 3 e 4)."""

from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base
from app.models import Conversa
from app.services import acompanhamento, hamilton_client

AGORA = datetime(2026, 7, 4, 12, 0, 0)


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


class _FakeHamilton:
    def __init__(self, status=None, erro=False):
        self._status = status or {}
        self._erro = erro

    async def status_primeira_consulta(self, ids):
        if self._erro:
            raise hamilton_client.HamiltonError("offline")
        return {pid: self._status[pid] for pid in ids if pid in self._status}


def _st(pid, nome, dias_cadastro, realizada, dias_consulta=None):
    dat = (AGORA - timedelta(days=dias_consulta)).date().isoformat() if dias_consulta else None
    return {
        "pk_paciente": pid,
        "nome": nome,
        "created_at": (AGORA - timedelta(days=dias_cadastro)).date().isoformat(),
        "primeira_consulta_realizada": realizada,
        "dat_primeira_consulta": dat,
    }


async def _conversa(session, *, numero, pid, nome=None, cobranca_resolvida_em=None):
    c = Conversa(
        numero_whatsapp=numero,
        paciente_hamilton_id=pid,
        estado="cadastrado",
        dados_coletados={"nome_completo": nome} if nome else {},
        cobranca_resolvida_em=cobranca_resolvida_em,
    )
    session.add(c)
    await session.flush()
    return c


class TestMontarAcompanhamento:
    @pytest.mark.asyncio
    async def test_separa_espera_e_cobranca_com_meta_e_ordem(self, session):
        await _conversa(session, numero="551", pid=1, nome="Ana")
        await _conversa(session, numero="552", pid=2, nome="Bia")
        await _conversa(session, numero="553", pid=3, nome="Cida")
        fake = _FakeHamilton(
            status={
                1: _st(1, "Ana", dias_cadastro=3, realizada=False),
                2: _st(2, "Bia", dias_cadastro=10, realizada=False),
                3: _st(3, "Cida", dias_cadastro=5, realizada=True, dias_consulta=1),
            }
        )
        dados = await acompanhamento.montar_acompanhamento(session, hamilton=fake, agora=AGORA)

        # Espera ordenada por dias desc (Bia 10 antes de Ana 3); >7 dias = fora da meta.
        assert [p["nome"] for p in dados["espera"]] == ["Bia", "Ana"]
        assert dados["espera"][0]["fora_da_meta"] is True
        assert dados["espera"][1]["fora_da_meta"] is False
        # Cobrança: só quem já teve a 1ª consulta.
        assert [p["nome"] for p in dados["cobranca"]] == ["Cida"]
        assert dados["erro"] is None

    @pytest.mark.asyncio
    async def test_cobranca_resolvida_sai_da_lista(self, session):
        await _conversa(session, numero="554", pid=4, nome="Dora", cobranca_resolvida_em=AGORA)
        fake = _FakeHamilton(
            status={4: _st(4, "Dora", dias_cadastro=5, realizada=True, dias_consulta=1)}
        )
        dados = await acompanhamento.montar_acompanhamento(session, hamilton=fake, agora=AGORA)
        assert dados["cobranca"] == []

    @pytest.mark.asyncio
    async def test_hamilton_offline_nao_quebra(self, session):
        await _conversa(session, numero="555", pid=5, nome="Eva")
        fake = _FakeHamilton(erro=True)
        dados = await acompanhamento.montar_acompanhamento(session, hamilton=fake, agora=AGORA)
        assert dados["espera"] == []
        assert dados["cobranca"] == []
        assert dados["erro"] is not None

    @pytest.mark.asyncio
    async def test_marcar_cobranca_resolvida(self, session):
        c = await _conversa(session, numero="556", pid=6, nome="Fia")
        await acompanhamento.marcar_cobranca_resolvida(session, c)
        assert c.cobranca_resolvida_em is not None
