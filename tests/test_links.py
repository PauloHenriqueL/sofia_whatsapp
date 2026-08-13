"""Encurtador dos links de pagamento (`allos.org.br/p/xxxxxxx`)."""

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.database import Base, get_db
from app.main import app
from app.models import Conversa, LinkCurto
from app.services import links

DESTINO = "https://buy.stripe.com/aBc123XyZ"


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


@pytest_asyncio.fixture
async def client_e_maker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def _db():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_db] = _db
    transporte = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transporte, base_url="http://t") as c:
        yield c, maker
    app.dependency_overrides.clear()
    await engine.dispose()


class TestSlug:
    def test_sem_caracteres_ambiguos(self):
        """O link é lido em voz alta e digitado à mão mais do que se imagina."""
        assert not (set(links.ALFABETO) & set("0o1liOI"))

    def test_tamanho_e_aleatorio(self):
        gerados = {links._novo_slug() for _ in range(200)}
        assert len(gerados) == 200  # sem colisão em 200 sorteios
        assert all(len(s) == links.TAMANHO for s in gerados)


class TestEncurtar:
    @pytest.mark.asyncio
    async def test_gera_link_no_dominio_configurado(self, session):
        original = settings.link_curto_base
        settings.link_curto_base = "https://allos.org.br/p"
        try:
            url = await links.encurtar(session, DESTINO)
        finally:
            settings.link_curto_base = original
        assert url.startswith("https://allos.org.br/p/")
        assert len(url.rsplit("/", 1)[-1]) == links.TAMANHO

    @pytest.mark.asyncio
    async def test_sem_dominio_configurado_aponta_pra_propria_sofia(self, session):
        """Degrada sozinho: o deploy daqui não fica preso ao deploy do site."""
        original = settings.link_curto_base
        settings.link_curto_base = ""
        try:
            url = await links.encurtar(session, DESTINO)
        finally:
            settings.link_curto_base = original
        assert url.startswith(f"{settings.base_url}/l/")

    @pytest.mark.asyncio
    async def test_e_idempotente_por_destino(self, session):
        """A Sofia remonta o link a cada turno da cobrança.

        Slug novo por turno faria ela mandar endereços diferentes pro mesmo
        pagamento — a pessoa não saberia qual vale.
        """
        primeiro = await links.encurtar(session, DESTINO)
        segundo = await links.encurtar(session, DESTINO)
        assert primeiro == segundo
        assert len((await session.execute(select(LinkCurto))).scalars().all()) == 1

    @pytest.mark.asyncio
    async def test_destino_vazio_passa_direto(self, session):
        assert await links.encurtar(session, "") == ""

    @pytest.mark.asyncio
    async def test_falha_no_banco_devolve_a_url_original(self, session, monkeypatch):
        """Link feio que funciona > cobrança que não sai."""

        async def _explode(*a, **kw):
            raise RuntimeError("banco fora")

        monkeypatch.setattr(session, "scalar", _explode)
        assert await links.encurtar(session, DESTINO) == DESTINO


class TestResolver:
    @pytest.mark.asyncio
    async def test_devolve_destino_e_conta_o_clique(self, session):
        url = await links.encurtar(session, DESTINO)
        await session.commit()
        slug = url.rsplit("/", 1)[-1]

        assert await links.resolver(session, slug) == DESTINO
        assert await links.resolver(session, slug) == DESTINO

        link = await session.scalar(select(LinkCurto).where(LinkCurto.slug == slug))
        await session.refresh(link)
        assert link.cliques == 2
        assert link.ultimo_clique_em is not None

    @pytest.mark.asyncio
    async def test_slug_desconhecido_e_none(self, session):
        assert await links.resolver(session, "naoexiste") is None


class TestRotasPublicas:
    @pytest.mark.asyncio
    async def test_redireciona_sem_login(self, client_e_maker):
        client, maker = client_e_maker
        async with maker() as s:
            s.add(LinkCurto(slug="abcdefg", destino=DESTINO))
            await s.commit()
        resp = await client.get("/l/abcdefg")
        # 302 e não 301: 301 fica cacheado no navegador pra sempre e o destino
        # de um slug pode mudar.
        assert resp.status_code == 302
        assert resp.headers["location"] == DESTINO

    @pytest.mark.asyncio
    async def test_slug_morto_vai_pro_site(self, client_e_maker):
        client, _ = client_e_maker
        resp = await client.get("/l/naoexiste")
        assert resp.status_code == 302
        assert "allos.org.br" in resp.headers["location"]

    @pytest.mark.asyncio
    async def test_api_json_para_o_site(self, client_e_maker):
        """É o que o `/p/[codigo]` do site consome pra fazer UM redirect só."""
        client, maker = client_e_maker
        async with maker() as s:
            s.add(LinkCurto(slug="hijklmn", destino=DESTINO))
            await s.commit()
        resp = await client.get("/api/links/hijklmn")
        assert resp.status_code == 200
        assert resp.json() == {"destino": DESTINO}

    @pytest.mark.asyncio
    async def test_api_nao_vaza_nada_alem_do_destino(self, client_e_maker):
        """Nome do paciente, valor e conversa ficam na Sofia."""
        client, maker = client_e_maker
        async with maker() as s:
            conversa = Conversa(numero_whatsapp="5531999", dados_coletados={"nome_completo": "Ana"})
            s.add(conversa)
            await s.flush()
            s.add(LinkCurto(slug="opqrstu", destino=DESTINO, conversa_id=conversa.id))
            await s.commit()
        corpo = (await client.get("/api/links/opqrstu")).json()
        assert set(corpo) == {"destino"}

    @pytest.mark.asyncio
    async def test_api_404_em_slug_desconhecido(self, client_e_maker):
        client, _ = client_e_maker
        assert (await client.get("/api/links/naoexiste")).status_code == 404


class TestApagarConversa:
    @pytest.mark.asyncio
    async def test_link_sobrevive_a_conversa(self, session):
        """ "Reiniciar conversa" não pode matar link que já está no WhatsApp."""
        conversa = Conversa(numero_whatsapp="5531988")
        session.add(conversa)
        await session.flush()
        url = await links.encurtar(session, DESTINO, conversa.id)
        await session.commit()

        await session.delete(conversa)
        await session.commit()

        assert await links.resolver(session, url.rsplit("/", 1)[-1]) == DESTINO
