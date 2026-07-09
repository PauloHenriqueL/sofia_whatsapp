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

    @pytest.mark.asyncio
    async def test_pagina_conversa_mostra_dados_coletados(self, ambiente):
        client, maker = ambiente
        await _login(client)
        async with maker() as s:
            c = Conversa(
                numero_whatsapp="5531900000000",
                modo="bot",
                estado="novo",
                dados_coletados={
                    "nome_completo": "Maria Silva",
                    "endereco": "Barroca, BH",
                    "horarios_disponiveis": "manhãs",
                    "como_conheceu": "Instagram",
                    "observacoes": "prefere atendimento online",
                },
            )
            s.add(c)
            await s.commit()
            cid = c.id
        resp = await client.get(f"/painel/conversas/{cid}/")
        assert resp.status_code == 200
        assert "Dados coletados" in resp.text
        assert "Como conheceu a Allos" in resp.text
        assert "Instagram" in resp.text
        # Horários e Observações aparecem (checklist completo)
        assert "Horários disponíveis" in resp.text
        assert "manhãs" in resp.text
        assert "Observações" in resp.text
        assert "prefere atendimento online" in resp.text
        # Campos não coletados aparecem como "não informado"
        assert "não informado" in resp.text

    @pytest.mark.asyncio
    async def test_pagina_metricas_renderiza(self, ambiente):
        client, maker = ambiente
        await _login(client)
        await _seed_conversa(maker)
        resp = await client.get("/painel/metricas")
        assert resp.status_code == 200
        assert "Resultados da Sofia" in resp.text

    @pytest.mark.asyncio
    async def test_pagina_prompts_renderiza(self, ambiente):
        client, _ = ambiente
        await _login(client)
        resp = await client.get("/painel/prompts")
        assert resp.status_code == 200
        assert "Prompts da Sofia" in resp.text

    @pytest.mark.asyncio
    async def test_pagina_acompanhamento_renderiza(self, ambiente):
        client, maker = ambiente
        await _login(client)
        # Conversa sem paciente_hamilton_id -> Hamilton nem é chamado (lista vazia).
        await _seed_conversa(maker)
        resp = await client.get("/painel/acompanhamento")
        assert resp.status_code == 200
        assert "Acompanhamento" in resp.text
        assert "Pronto pra cobrança" in resp.text

    @pytest.mark.asyncio
    async def test_pagina_config_renderiza_e_salva(self, ambiente):
        from app.services import config_negocio

        original = dict(config_negocio._cache)
        client, _ = ambiente
        await _login(client)
        try:
            resp = await client.get("/painel/config")
            assert resp.status_code == 200
            assert "Configurações" in resp.text

            resp2 = await client.post(
                "/painel/config",
                data={
                    "preco_terapia_mensal": "250",
                    "preco_neuro": "1500",
                    "parcelas_max": "6",
                    "followup_horas": "18",
                },
                follow_redirects=False,
            )
            assert resp2.status_code == 303
            assert config_negocio.valor("preco_neuro") == 1500
        finally:
            config_negocio._cache.clear()
            config_negocio._cache.update(original)


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

    @pytest.mark.asyncio
    async def test_reiniciar_apaga_conversa_e_mensagens(self, ambiente):
        client, maker = ambiente
        await _login(client)
        cid = await _seed_conversa(maker)

        resp = await client.post(f"/painel/conversas/{cid}/reiniciar", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/painel/"

        async with maker() as s:
            assert await s.get(Conversa, cid) is None
            msgs = (
                (await s.execute(select(Mensagem).where(Mensagem.conversa_id == cid)))
                .scalars()
                .all()
            )
            assert msgs == []


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


class TestBuscaEOrdenacao:
    """P1: a Thainá filtra, ordena e busca pra se localizar na lista."""

    @pytest.mark.asyncio
    async def test_busca_por_nome_numero_e_mensagem(self, ambiente):
        client, maker = ambiente
        async with maker() as s:
            a = Conversa(
                numero_whatsapp="5531911112222",
                dados_coletados={"nome_completo": "Amanda Soares"},
                modo="bot",
                estado="novo",
            )
            b = Conversa(numero_whatsapp="5531933334444", modo="bot", estado="novo")
            s.add_all([a, b])
            await s.flush()
            s.add(
                Mensagem(
                    conversa_id=b.id,
                    direcao="recebida",
                    origem="paciente",
                    tipo="texto",
                    texto="quero neuroavaliação",
                )
            )
            await s.commit()

        async def nomes(**kw):
            async with maker() as s:
                return [
                    c["numero_whatsapp"] for c in await painel_service.listar_conversas(s, **kw)
                ]

        assert await nomes(busca="amanda") == ["5531911112222"]  # nome (dentro do JSON)
        assert await nomes(busca="3333") == ["5531933334444"]  # número
        assert await nomes(busca="neuro") == ["5531933334444"]  # texto de mensagem
        assert await nomes(busca="inexistente") == []
        assert len(await nomes()) == 2  # sem busca, traz tudo

    @pytest.mark.asyncio
    async def test_ordena_por_coluna_nos_dois_sentidos(self, ambiente):
        client, maker = ambiente
        await _seed_conversa(maker, numero="5531900000001")
        await _seed_conversa(maker, numero="5531900000002")

        async with maker() as s:
            asc_ = await painel_service.listar_conversas(
                s, ordem="numero_whatsapp", descendente=False
            )
            desc_ = await painel_service.listar_conversas(
                s, ordem="numero_whatsapp", descendente=True
            )
        assert [c["numero_whatsapp"] for c in asc_] == ["5531900000001", "5531900000002"]
        assert [c["numero_whatsapp"] for c in desc_] == ["5531900000002", "5531900000001"]

    @pytest.mark.asyncio
    async def test_ordem_invalida_nao_injeta_sql_e_cai_no_padrao(self, ambiente):
        client, maker = ambiente
        await _seed_conversa(maker)
        async with maker() as s:
            r = await painel_service.listar_conversas(s, ordem="'; DROP TABLE conversa;--")
        assert len(r) == 1  # não explodiu, não apagou nada

    @pytest.mark.asyncio
    async def test_pagina_aceita_busca_e_ordem_na_query(self, ambiente):
        client, maker = ambiente
        await _login(client)
        await _seed_conversa(maker)
        resp = await client.get("/painel/?busca=oi&ordem=nome&dir=asc&filtro=todas")
        assert resp.status_code == 200


class TestAssumirParaDigitar:
    """P2: sem assumir, não há campo de digitar; ao sair, oferece devolver ao bot."""

    @pytest.mark.asyncio
    async def test_sem_assumir_mostra_botao_e_esconde_campo(self, ambiente):
        client, maker = ambiente
        await _login(client)
        cid = await _seed_conversa(maker, modo="bot")
        html = (await client.get(f"/painel/conversas/{cid}/")).text
        assert "Assumir controle pra responder" in html
        assert "<textarea" not in html

    @pytest.mark.asyncio
    async def test_com_controle_mostra_campo(self, ambiente):
        client, maker = ambiente
        await _login(client)
        cid = await _seed_conversa(maker, modo="humano")
        html = (await client.get(f"/painel/conversas/{cid}/")).text
        assert "<textarea" in html
        assert "Quer que o bot assuma daqui pra frente?" in html

    @pytest.mark.asyncio
    async def test_devolver_ao_bot_redireciona_pro_destino(self, ambiente):
        client, maker = ambiente
        await _login(client)
        cid = await _seed_conversa(maker, modo="humano")
        resp = await client.post(
            f"/painel/conversas/{cid}/devolver-bot",
            data={"proximo": "/painel/acompanhamento"},
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/painel/acompanhamento"
        async with maker() as s:
            assert (await s.get(Conversa, cid)).modo == "bot"

    @pytest.mark.asyncio
    async def test_destino_externo_e_ignorado(self, ambiente):
        """Open redirect: `proximo` só pode ser caminho interno."""
        client, maker = ambiente
        await _login(client)
        cid = await _seed_conversa(maker, modo="humano")
        for malicioso in ("https://evil.example", "//evil.example"):
            resp = await client.post(
                f"/painel/conversas/{cid}/devolver-bot", data={"proximo": malicioso}
            )
            assert resp.headers["location"] == f"/painel/conversas/{cid}/"
