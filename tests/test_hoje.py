"""Testes da tela "Hoje" — a fila do que precisa de uma pessoa.

O que importa garantir aqui, porque é onde o desenho pode se trair sozinho:

1. **A fila não tem recorte de tempo.** Uma escalada velha continua na lista.
   Se alguém "otimizar" isso pra últimos N dias, o esquecido some — que é
   exatamente o caso que a tela existe pra pegar.
2. **Uma linha por conversa.** A mesma pessoa dispara dois sinais em cobrança
   com comprovante; duas linhas fariam a fila mentir o tamanho dela.
3. **Hamilton fora não derruba a página.** Só a lista "de olho" fala com ele.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.database import Base, get_db
from app.main import app
from app.models import Conversa, Escalada, Mensagem
from app.services import config_negocio, hamilton_client, hoje, usuarios


@pytest.fixture(autouse=True)
def _isola_cache_de_config():
    snap = dict(config_negocio._cache)
    yield
    config_negocio._cache.clear()
    config_negocio._cache.update(snap)


@pytest_asyncio.fixture
async def ambiente():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    # O lifespan real migra o usuário do .env no startup; ASGITransport não
    # dispara lifespan, então fazemos aqui pra /login funcionar no teste.
    async with maker() as s:
        await usuarios.migrar_usuario_do_env(s)

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
        "/login", data={"usuario": settings.painel_user, "senha": settings.painel_password}
    )
    assert resp.status_code == 303


def _hamilton_vazio():
    """Hamilton que responde sem pacientes — evita chamada de rede no teste."""
    falso = AsyncMock()
    falso.status_primeira_consulta = AsyncMock(return_value={})
    # A home também pergunta a fila do encaminhamento assistido; sem isto o
    # AsyncMock devolve uma corrotina onde a tela espera a lista de pendências.
    falso.fila_encaminhamentos = AsyncMock(return_value={"ativo": False, "itens": []})
    return falso


async def _conversa(maker, numero, **campos):
    campos.setdefault("estado", "novo")
    async with maker() as s:
        c = Conversa(numero_whatsapp=numero, modo="bot", **campos)
        s.add(c)
        await s.commit()
        return c.id


class TestFilaNaoTemRecorteDeTempo:
    @pytest.mark.asyncio
    async def test_escalada_antiga_continua_na_fila(self, ambiente):
        _, maker = ambiente
        cid = await _conversa(maker, "5531900000001", dados_coletados={"nome_completo": "Ana"})
        antiga = datetime.now(timezone.utc) - timedelta(days=45)
        async with maker() as s:
            s.add(Escalada(conversa_id=cid, motivo="gratuidade", criada_em=antiga))
            await s.commit()

        async with maker() as s:
            fila = await hoje.listar_pendencias(s)

        assert [i["nome"] for i in fila] == ["Ana"]
        assert fila[0]["tipo"] == "escalada"

    @pytest.mark.asyncio
    async def test_escalada_resolvida_sai_da_fila(self, ambiente):
        _, maker = ambiente
        cid = await _conversa(maker, "5531900000002")
        async with maker() as s:
            s.add(
                Escalada(
                    conversa_id=cid,
                    motivo="preco",
                    resolvida_em=datetime.now(timezone.utc),
                )
            )
            await s.commit()
        async with maker() as s:
            assert await hoje.listar_pendencias(s) == []

    @pytest.mark.asyncio
    async def test_conversa_arquivada_nao_aparece(self, ambiente):
        _, maker = ambiente
        cid = await _conversa(maker, "5531900000003", arquivada_em=datetime.now(timezone.utc))
        async with maker() as s:
            s.add(Escalada(conversa_id=cid, motivo="crise"))
            await s.commit()
        async with maker() as s:
            assert await hoje.listar_pendencias(s) == []


class TestUmaLinhaPorConversa:
    @pytest.mark.asyncio
    async def test_escalada_vence_cobranca_travada_na_mesma_conversa(self, ambiente):
        """Comprovante no meio da cobrança abre escalada E marca o status.

        Sem dedupe a mesma pessoa apareceria duas vezes e o contador da aba
        diria 2 onde há 1 atendimento pra tratar.
        """
        _, maker = ambiente
        cid = await _conversa(
            maker,
            "5531900000004",
            cobranca_status="sem_janela",
            cobranca_iniciada_em=datetime.now(timezone.utc),
        )
        async with maker() as s:
            s.add(Escalada(conversa_id=cid, motivo="anexo_recebido"))
            await s.commit()

        async with maker() as s:
            fila = await hoje.listar_pendencias(s)

        assert len(fila) == 1
        assert fila[0]["tipo"] == "escalada"

    @pytest.mark.asyncio
    async def test_contador_bate_com_o_tamanho_da_fila(self, ambiente):
        _, maker = ambiente
        for n in range(3):
            cid = await _conversa(maker, f"553190000001{n}")
            async with maker() as s:
                s.add(Escalada(conversa_id=cid, motivo="pedido_humano"))
                await s.commit()
        async with maker() as s:
            assert await hoje.contar_pendencias(s) == 3


class TestTiposDePendencia:
    @pytest.mark.asyncio
    async def test_cadastro_pendente_entra(self, ambiente):
        _, maker = ambiente
        await _conversa(maker, "5531900000021", estado="cadastro_pendente")
        async with maker() as s:
            fila = await hoje.listar_pendencias(s)
        assert [i["tipo"] for i in fila] == ["cadastro_falhou"]

    @pytest.mark.asyncio
    async def test_cobranca_travada_entra_mas_em_curso_nao(self, ambiente):
        _, maker = ambiente
        agora = datetime.now(timezone.utc)
        await _conversa(
            maker, "5531900000022", cobranca_status="erro_link", cobranca_iniciada_em=agora
        )
        # "enviada" é cobrança andando: a Sofia ainda vai mandar o lembrete.
        await _conversa(
            maker, "5531900000023", cobranca_status="enviada", cobranca_iniciada_em=agora
        )
        async with maker() as s:
            fila = await hoje.listar_pendencias(s)
        assert [i["numero"] for i in fila] == ["5531900000022"]

    @pytest.mark.asyncio
    async def test_cobranca_em_curso_cai_no_de_olho(self, ambiente):
        _, maker = ambiente
        await _conversa(
            maker,
            "5531900000024",
            cobranca_status="pix",
            cobranca_iniciada_em=datetime.now(timezone.utc),
        )
        async with maker() as s:
            dados = await hoje.montar_hoje(s, hamilton=_hamilton_vazio())
        assert [d["tipo"] for d in dados["de_olho"]] == ["cobranca_sem_resposta"]
        assert dados["pendencias"] == []


class TestHamiltonForaNaoDerruba:
    @pytest.mark.asyncio
    async def test_fila_local_sobrevive_a_falha_do_hamilton(self, ambiente):
        _, maker = ambiente
        cid = await _conversa(maker, "5531900000031", paciente_hamilton_id=99)
        async with maker() as s:
            s.add(Escalada(conversa_id=cid, motivo="crise"))
            await s.commit()

        quebrado = AsyncMock()
        quebrado.status_primeira_consulta = AsyncMock(
            side_effect=hamilton_client.HamiltonError("500")
        )
        async with maker() as s:
            dados = await hoje.montar_hoje(s, hamilton=quebrado)

        assert len(dados["pendencias"]) == 1
        assert dados["erro"]


class TestNumerosDaJanela:
    @pytest.mark.asyncio
    async def test_lead_e_cadastro_sao_do_mesmo_grupo(self, ambiente):
        """Quem chegou há 30 dias e foi cadastrado ontem não entra em nenhum dos
        dois números: antes ele inflava "cadastradas" sem estar em "novas"."""
        _, maker = ambiente
        agora = datetime.now(timezone.utc)
        async with maker() as s:
            s.add(Conversa(numero_whatsapp="5531900000041", paciente_hamilton_id=1))
            s.add(Conversa(numero_whatsapp="5531900000042"))
            s.add(Conversa(numero_whatsapp="5531900000043", arquivada_em=agora))
            s.add(Conversa(numero_whatsapp="5531900000044", eh_terapeuta=True))
            s.add(
                Conversa(
                    numero_whatsapp="5531900000045",
                    paciente_hamilton_id=2,
                    criada_em=agora - timedelta(days=30),
                    cadastrado_em=agora - timedelta(days=1),
                )
            )
            await s.commit()
        async with maker() as s:
            dados = await hoje.montar_hoje(s, hamilton=_hamilton_vazio())
        # Lead = todo contato que não é terapeuta, arquivado ou não.
        assert dados["numeros"]["funil"] == {
            "leads": 3,
            "cadastrados": 1,
            "sem_cadastro": 2,
            "taxa": 33,
        }
        assert dados["numeros"]["frase_funil"] == (
            "Das 3 pessoas novas dos últimos 7 dias, 1 já está cadastrada no Hamilton e 2 ainda não."
        )


class TestSemResposta:
    """26/11: a Thainá assumiu sem escalada, o paciente respondeu às 18h12 e a
    Hoje dizia "0 esperando você"."""

    async def _humana(self, maker, numero, falas):
        async with maker() as s:
            c = Conversa(numero_whatsapp=numero, modo="humano", estado="novo")
            s.add(c)
            await s.flush()
            for origem in falas:
                s.add(
                    Mensagem(
                        conversa_id=c.id,
                        direcao="recebida" if origem == "paciente" else "enviada",
                        origem=origem,
                        texto="...",
                    )
                )
            await s.commit()
            return c.id

    @pytest.mark.asyncio
    async def test_paciente_falou_por_ultimo_entra_na_fila(self, ambiente):
        _, maker = ambiente
        cid = await self._humana(maker, "5531900000071", ["paciente", "thaina", "paciente"])
        async with maker() as s:
            fila = await hoje.listar_pendencias(s)
        assert [(i["conversa_id"], i["tipo"]) for i in fila] == [(cid, "sem_resposta")]

    @pytest.mark.asyncio
    async def test_aviso_automatico_nao_conta_como_resposta(self, ambiente):
        _, maker = ambiente
        cid = await self._humana(maker, "5531900000072", ["thaina", "paciente", "bot"])
        async with maker() as s:
            fila = await hoje.listar_pendencias(s)
        assert [i["conversa_id"] for i in fila] == [cid]

    @pytest.mark.asyncio
    async def test_equipe_respondeu_ou_bot_atendendo_nao_entra(self, ambiente):
        _, maker = ambiente
        await self._humana(maker, "5531900000073", ["paciente", "thaina"])
        await _conversa(maker, "5531900000074")
        async with maker() as s:
            assert await hoje.listar_pendencias(s) == []


class TestPagina:
    @pytest.mark.asyncio
    async def test_home_do_painel_e_a_tela_hoje(self, ambiente):
        client, maker = ambiente
        await _login(client)
        cid = await _conversa(
            maker, "5531900000051", dados_coletados={"nome_completo": "Marina Prado"}
        )
        async with maker() as s:
            s.add(Escalada(conversa_id=cid, motivo="gratuidade"))
            await s.commit()

        with patch.object(hamilton_client, "get_hamilton_client", return_value=_hamilton_vazio()):
            resp = await client.get("/painel/")

        assert resp.status_code == 200
        assert "Precisa de você agora" in resp.text
        assert "Marina Prado" in resp.text
        assert "não tem como pagar" in resp.text

    @pytest.mark.asyncio
    async def test_sem_pendencia_mostra_estado_vazio(self, ambiente):
        client, _ = ambiente
        await _login(client)
        with patch.object(hamilton_client, "get_hamilton_client", return_value=_hamilton_vazio()):
            resp = await client.get("/painel/")
        assert "Nada esperando por você" in resp.text

    @pytest.mark.asyncio
    async def test_ia_fora_aparece_no_topo(self, ambiente, monkeypatch):
        """25/11: a IA ficou ~24h sem crédito e a tela não dizia nada."""
        from datetime import datetime, timezone

        from app.services import llm_client

        client, _ = ambiente
        await _login(client)
        with patch.object(hamilton_client, "get_hamilton_client", return_value=_hamilton_vazio()):
            resp = await client.get("/painel/")
            assert "A IA da Sofia está fora" not in resp.text

            monkeypatch.setattr(
                llm_client, "bloqueada_desde", datetime(2026, 11, 25, 17, 37, tzinfo=timezone.utc)
            )
            resp = await client.get("/painel/")
        assert "A IA da Sofia está fora desde 25/11 14:37" in resp.text

    @pytest.mark.asyncio
    async def test_contador_aparece_na_aba_de_outra_tela(self, ambiente):
        """O contador precisa ser visto de FORA da home — é a graça dele."""
        client, maker = ambiente
        await _login(client)
        cid = await _conversa(maker, "5531900000061")
        async with maker() as s:
            s.add(Escalada(conversa_id=cid, motivo="pedido_humano"))
            await s.commit()

        resp = await client.get("/painel/conversas")
        assert '<span class="pino">1</span>' in resp.text
