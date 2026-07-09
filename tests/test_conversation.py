"""Testes da persistência e orquestração da conversa (Passo 3)."""

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base
from app.services import conversation


@pytest_asyncio.fixture
async def session():
    """Sessão async sobre um SQLite em memória, com as tabelas criadas."""
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


class TestObterOuCriarConversa:
    async def test_cria_quando_nao_existe(self, session):
        conversa = await conversation.obter_ou_criar_conversa(session, "553199999")
        assert conversa.id is not None
        assert conversa.numero_whatsapp == "553199999"
        assert conversa.modo == "bot"
        assert conversa.estado == "novo"
        assert conversa.dados_coletados == {}

    async def test_reutiliza_conversa_existente(self, session):
        c1 = await conversation.obter_ou_criar_conversa(session, "553199999")
        await session.commit()
        c2 = await conversation.obter_ou_criar_conversa(session, "553199999")
        assert c1.id == c2.id


class TestIdempotencia:
    async def test_mensagem_nao_processada_ainda(self, session):
        assert await conversation.mensagem_ja_processada(session, "wamid.novo") is False

    async def test_sem_wamid_nunca_e_duplicata(self, session):
        assert await conversation.mensagem_ja_processada(session, None) is False

    async def test_detecta_mensagem_ja_processada(self, session):
        conversa = await conversation.obter_ou_criar_conversa(session, "553199999")
        await conversation.registrar_mensagem_recebida(
            session, conversa, tipo="texto", texto="oi", whatsapp_message_id="wamid.1"
        )
        await session.commit()
        assert await conversation.mensagem_ja_processada(session, "wamid.1") is True


class TestCarregarHistorico:
    async def test_mapeia_roles_e_ordena_cronologicamente(self, session):
        conversa = await conversation.obter_ou_criar_conversa(session, "553199999")
        await conversation.registrar_mensagem_recebida(
            session, conversa, tipo="texto", texto="oi", whatsapp_message_id="w1"
        )
        await conversation.registrar_mensagem_enviada(session, conversa, texto="olá, sou a Sofia")
        await conversation.registrar_mensagem_recebida(
            session,
            conversa,
            tipo="texto",
            texto="quero terapia",
            whatsapp_message_id="w2",
        )
        await session.commit()

        hist = await conversation.carregar_historico(session, conversa)
        assert hist == [
            {"role": "user", "content": "oi"},
            {"role": "assistant", "content": "olá, sou a Sofia"},
            {"role": "user", "content": "quero terapia"},
        ]

    async def test_fala_da_thaina_e_marcada_e_pede_reapresentacao(self, session):
        # Thainá falou por último: a Sofia tem que saber quem falou e se reapresentar.
        conversa = await conversation.obter_ou_criar_conversa(session, "553199999")
        await conversation.registrar_mensagem_recebida(
            session, conversa, tipo="texto", texto="oi", whatsapp_message_id="w1"
        )
        await conversation.registrar_mensagem_enviada(
            session, conversa, texto="oi, aqui é a Thainá", origem="thaina"
        )
        await session.commit()

        hist = await conversation.carregar_historico(session, conversa)
        assert hist[1]["role"] == "assistant"
        assert hist[1]["content"].startswith("[Thainá, coordenadora clínica]:")
        assert hist[-1] == {"role": "system", "content": conversation.AVISO_RETOMADA}

    async def test_sem_aviso_quando_a_sofia_falou_por_ultimo(self, session):
        conversa = await conversation.obter_ou_criar_conversa(session, "553199999")
        await conversation.registrar_mensagem_enviada(
            session, conversa, texto="oi, aqui é a Thainá", origem="thaina"
        )
        await conversation.registrar_mensagem_enviada(session, conversa, texto="voltei, é a Sofia")
        await session.commit()

        hist = await conversation.carregar_historico(session, conversa)
        assert all(m["role"] != "system" for m in hist)

    async def test_ignora_mensagens_sem_texto(self, session):
        conversa = await conversation.obter_ou_criar_conversa(session, "553199999")
        await conversation.registrar_mensagem_recebida(
            session, conversa, tipo="audio", texto=None, whatsapp_message_id="wa"
        )
        await conversation.registrar_mensagem_recebida(
            session, conversa, tipo="texto", texto="oi", whatsapp_message_id="wt"
        )
        await session.commit()

        hist = await conversation.carregar_historico(session, conversa)
        assert hist == [{"role": "user", "content": "oi"}]

    async def test_respeita_limite_e_pega_as_mais_recentes(self, session):
        conversa = await conversation.obter_ou_criar_conversa(session, "553199999")
        for i in range(25):
            await conversation.registrar_mensagem_recebida(
                session,
                conversa,
                tipo="texto",
                texto=f"msg {i}",
                whatsapp_message_id=f"w{i}",
            )
        await session.commit()

        hist = await conversation.carregar_historico(session, conversa, limite=20)
        assert len(hist) == 20
        assert hist[0]["content"] == "msg 5"
        assert hist[-1]["content"] == "msg 24"


class TestRegistroDeMensagens:
    async def test_registra_recebida(self, session):
        conversa = await conversation.obter_ou_criar_conversa(session, "553199999")
        msg = await conversation.registrar_mensagem_recebida(
            session, conversa, tipo="texto", texto="olá", whatsapp_message_id="wamid.9"
        )
        assert msg.id is not None
        assert msg.direcao == "recebida"
        assert msg.origem == "paciente"
        assert msg.texto == "olá"

    async def test_registra_enviada(self, session):
        conversa = await conversation.obter_ou_criar_conversa(session, "553199999")
        msg = await conversation.registrar_mensagem_enviada(
            session, conversa, texto="ok, recebi: olá"
        )
        assert msg.direcao == "enviada"
        assert msg.origem == "bot"
        assert msg.texto == "ok, recebi: olá"
