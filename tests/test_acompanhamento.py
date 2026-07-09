"""Testes do acompanhamento pós-cadastro (Demandas 3 e 4)."""

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base
from app.models import Conversa, Mensagem
from app.services import acompanhamento, hamilton_client
from app.services import painel as painel_service

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


class TestResolvidosEReabrir:
    """Resolvido é um ESTADO, não o fim: a conversa nunca some, e dá pra desfazer."""

    @pytest.mark.asyncio
    async def test_resolvido_sai_da_cobranca_e_entra_em_resolvidos(self, session):
        c = await _conversa(session, numero="5531999998888", pid=42, nome="Maria")
        ham = _FakeHamilton(
            {42: _st(42, "Maria", dias_cadastro=10, realizada=True, dias_consulta=2)}
        )

        antes = await acompanhamento.montar_acompanhamento(session, hamilton=ham, agora=AGORA)
        assert len(antes["cobranca"]) == 1 and antes["resolvidos"] == []

        await acompanhamento.marcar_cobranca_resolvida(session, c)
        depois = await acompanhamento.montar_acompanhamento(session, hamilton=ham, agora=AGORA)
        assert depois["cobranca"] == []
        assert len(depois["resolvidos"]) == 1
        assert depois["resolvidos"][0]["conversa_id"] == c.id
        assert depois["resolvidos"][0]["resolvida_em"] is not None

    @pytest.mark.asyncio
    async def test_a_conversa_e_as_mensagens_nunca_somem(self, session):
        """A queixa original: 'clica em resolver e a conversa some'. Não some."""
        c = await _conversa(session, numero="5531999998888", pid=42, nome="Maria")
        session.add(
            Mensagem(
                conversa_id=c.id, direcao="recebida", origem="paciente", tipo="texto", texto="oi"
            )
        )
        await session.commit()

        await acompanhamento.marcar_cobranca_resolvida(session, c)

        assert await session.get(Conversa, c.id) is not None
        msgs = (
            (await session.execute(select(Mensagem).where(Mensagem.conversa_id == c.id)))
            .scalars()
            .all()
        )
        assert len(msgs) == 1
        # E continua aparecendo na lista principal do painel.
        assert len(await painel_service.listar_conversas(session)) == 1

    @pytest.mark.asyncio
    async def test_reabrir_devolve_pra_fila_de_cobranca(self, session):
        c = await _conversa(session, numero="5531999998888", pid=42, nome="Maria")
        ham = _FakeHamilton(
            {42: _st(42, "Maria", dias_cadastro=10, realizada=True, dias_consulta=2)}
        )

        await acompanhamento.marcar_cobranca_resolvida(session, c)
        await acompanhamento.reabrir_cobranca(session, c)

        dados = await acompanhamento.montar_acompanhamento(session, hamilton=ham, agora=AGORA)
        assert len(dados["cobranca"]) == 1
        assert dados["resolvidos"] == []
        assert c.cobranca_resolvida_em is None

    @pytest.mark.asyncio
    async def test_resolver_nao_mexe_no_modo_da_conversa(self, session):
        """Resolver cobrança é sobre dinheiro, não sobre quem atende."""
        c = await _conversa(session, numero="5531999998888", pid=42, nome="Maria")
        c.modo = "humano"
        await session.commit()
        await acompanhamento.marcar_cobranca_resolvida(session, c)
        assert c.modo == "humano"

    @pytest.mark.asyncio
    async def test_resolvidos_vem_dos_mais_recentes_primeiro(self, session):
        agora = datetime.now(timezone.utc)
        for i, dias in enumerate([5, 1, 3], start=1):
            await _conversa(
                session,
                numero=f"553199999000{i}",
                pid=i,
                nome=f"P{i}",
                cobranca_resolvida_em=agora - timedelta(days=dias),
            )
        ham = _FakeHamilton(
            {
                i: _st(i, f"P{i}", dias_cadastro=20, realizada=True, dias_consulta=10)
                for i in (1, 2, 3)
            }
        )
        dados = await acompanhamento.montar_acompanhamento(session, hamilton=ham, agora=AGORA)
        assert [r["paciente_id"] for r in dados["resolvidos"]] == [2, 3, 1]  # 1d, 3d, 5d

    @pytest.mark.asyncio
    async def test_item_traz_o_modo_pra_thaina_ver_quem_atende(self, session):
        c = await _conversa(session, numero="5531999998888", pid=42, nome="Maria")
        c.modo = "humano"
        await session.commit()
        ham = _FakeHamilton(
            {42: _st(42, "Maria", dias_cadastro=10, realizada=True, dias_consulta=2)}
        )
        dados = await acompanhamento.montar_acompanhamento(session, hamilton=ham, agora=AGORA)
        assert dados["cobranca"][0]["modo"] == "humano"
