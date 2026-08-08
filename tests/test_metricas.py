"""Testes dos KPIs do painel (Frente 3)."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base
from app.models import Conversa, Escalada, Mensagem
from app.services import hamilton_client, metricas

AGORA = datetime(2026, 6, 24, 12, 0, 0)


@pytest.fixture(autouse=True)
def _hamilton_mockado():
    """A métrica de tempo até a 1ª sessão consulta o Hamilton.

    Sem este mock os testes fazem chamada de REDE de verdade (2,3s por teste, e
    falhando por auth). Nenhum teste deste repo toca a rede — cada um sobe seu
    SQLite e mocka os externos.
    """
    cliente = AsyncMock()
    cliente.status_primeira_consulta.return_value = {}
    with patch.object(hamilton_client, "get_hamilton_client", return_value=cliente):
        yield cliente


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


class TestTempoAtePrimeiraSessao:
    """Do primeiro "oi" até estar na cadeira do terapeuta.

    Início = `conversa.criada_em` (Sofia); fim = `dat_primeira_consulta`
    (Hamilton, só data). Só entra quem JÁ foi atendido — quem ainda espera está
    na fila do acompanhamento, senão a métrica melhoraria sozinha justamente
    quando alguém demora.
    """

    @pytest.mark.asyncio
    async def test_mediana_e_extremos(self, session, _hamilton_mockado):
        hoje = datetime.now(timezone.utc)
        for i, atraso in enumerate([3, 9, 21]):
            await _conversa(
                session,
                numero=f"5531{i}",
                paciente_hamilton_id=100 + i,
                criada_em=hoje - timedelta(days=atraso),
            )
        await session.commit()
        _hamilton_mockado.status_primeira_consulta.return_value = {
            100
            + i: {
                "primeira_consulta_realizada": True,
                "dat_primeira_consulta": hoje.date().isoformat(),
            }
            for i in range(3)
        }
        m = await metricas.calcular_metricas(session)
        t = m["tempo_primeira_sessao"]
        assert t["mediana"] == 9  # mediana, não média (11): um caso lento não distorce
        assert t["media"] == 11
        assert (t["minimo"], t["maximo"]) == (3, 21)
        assert t["pacientes"] == 3

    @pytest.mark.asyncio
    async def test_ignora_quem_ainda_nao_foi_atendido(self, session, _hamilton_mockado):
        await _conversa(session, numero="5531x", paciente_hamilton_id=1)
        await session.commit()
        _hamilton_mockado.status_primeira_consulta.return_value = {
            1: {"primeira_consulta_realizada": False, "dat_primeira_consulta": None}
        }
        m = await metricas.calcular_metricas(session)
        assert m["tempo_primeira_sessao"] is None

    @pytest.mark.asyncio
    async def test_ignora_consulta_anterior_a_conversa(self, session, _hamilton_mockado):
        """Paciente antigo que só depois falou com a Sofia: não é espera, e
        contaria como 0, puxando a mediana pra baixo."""
        hoje = datetime.now(timezone.utc)
        await _conversa(session, numero="5531y", paciente_hamilton_id=1, criada_em=hoje)
        await session.commit()
        _hamilton_mockado.status_primeira_consulta.return_value = {
            1: {
                "primeira_consulta_realizada": True,
                "dat_primeira_consulta": (hoje - timedelta(days=30)).date().isoformat(),
            }
        }
        m = await metricas.calcular_metricas(session)
        assert m["tempo_primeira_sessao"] is None

    @pytest.mark.asyncio
    async def test_hamilton_fora_do_ar_nao_quebra_o_resto(self, session, _hamilton_mockado):
        await _conversa(session, numero="5531z", paciente_hamilton_id=1)
        await session.commit()
        _hamilton_mockado.status_primeira_consulta.side_effect = hamilton_client.HamiltonError(
            "fora"
        )
        m = await metricas.calcular_metricas(session)
        assert m["tempo_primeira_sessao"] is None
        assert m["total"] == 1  # o resto da página continua


class TestEscaladasEmAberto:
    @pytest.mark.asyncio
    async def test_escalada_resolvida_sai_do_grafico(self, session, _hamilton_mockado):
        """`resolvida_em` passou a ser preenchido de verdade; misturar aberto com
        fechado faz um motivo já tratado continuar parecendo problema ativo."""
        c = await _conversa(session, numero="5531w")
        session.add(Escalada(conversa_id=c.id, motivo="preco"))
        session.add(
            Escalada(conversa_id=c.id, motivo="crise", resolvida_em=datetime.now(timezone.utc))
        )
        await session.commit()
        m = await metricas.calcular_metricas(session)
        motivos = {e["motivo"] for e in m["escaladas_por_motivo"]}
        assert motivos == {"preco"}
