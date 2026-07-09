"""Testes do painel da Thainá: login por sessão, API, páginas e CSRF."""

import itertools
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
from app.models import Conversa, Mensagem, Midia
from app.services import midia as midia_service
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
        # Sem citar nada: responder_a=None (o reply é opcional — P4).
        mock_enviar.assert_awaited_once_with(
            "5531999998888", "Oi, aqui é a Thainá", responder_a=None
        )

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


class TestDownloadDeAnexo:
    """P3: a Thainá abre/baixa o anexo. É dado de saúde: exige login."""

    _proximo = itertools.count(1)

    async def _seed_midia(self, maker, mime="image/png", nome=None, conteudo=b"\x89PNG"):
        # Número único: conversa.numero_whatsapp é UNIQUE e um teste semeia 2 anexos.
        numero = f"55319999{next(self._proximo):05d}"
        async with maker() as s:
            c = Conversa(numero_whatsapp=numero, modo="humano", estado="escalado")
            s.add(c)
            await s.flush()
            m = Mensagem(
                conversa_id=c.id,
                direcao="recebida",
                origem="paciente",
                tipo="image",
                texto="[imagem recebida]",
            )
            s.add(m)
            await s.flush()
            anexo = Midia(
                mensagem_id=m.id,
                mime=mime,
                nome_arquivo=nome,
                tamanho=len(conteudo),
                conteudo=conteudo,
            )
            s.add(anexo)
            await s.commit()
            return anexo.id

    @pytest.mark.asyncio
    async def test_exige_login(self, ambiente):
        client, maker = ambiente
        mid = await self._seed_midia(maker)
        resp = await client.get(f"/painel/midia/{mid}")
        assert resp.status_code == 303  # manda pro /login

    @pytest.mark.asyncio
    async def test_serve_imagem_inline(self, ambiente):
        client, maker = ambiente
        await _login(client)
        mid = await self._seed_midia(maker)
        resp = await client.get(f"/painel/midia/{mid}")
        assert resp.status_code == 200
        assert resp.content == b"\x89PNG"
        assert resp.headers["content-type"].startswith("image/png")
        assert resp.headers["content-disposition"].startswith("inline")
        assert resp.headers["x-content-type-options"] == "nosniff"

    @pytest.mark.asyncio
    async def test_download_forcado(self, ambiente):
        client, maker = ambiente
        await _login(client)
        mid = await self._seed_midia(maker, mime="application/pdf", nome="laudo.pdf")
        resp = await client.get(f"/painel/midia/{mid}?download=1")
        assert 'attachment; filename="laudo.pdf"' in resp.headers["content-disposition"]

    @pytest.mark.asyncio
    async def test_tipo_perigoso_nunca_e_inline(self, ambiente):
        """HTML/SVG do paciente não pode rodar na origem do painel."""
        client, maker = ambiente
        await _login(client)
        for mime in ("text/html", "image/svg+xml"):
            mid = await self._seed_midia(maker, mime=mime, conteudo=b"<script>alert(1)</script>")
            resp = await client.get(f"/painel/midia/{mid}")
            assert resp.headers["content-type"].startswith("application/octet-stream")
            assert resp.headers["content-disposition"].startswith("attachment")

    @pytest.mark.asyncio
    async def test_anexo_inexistente_da_404(self, ambiente):
        client, maker = ambiente
        await _login(client)
        assert (await client.get("/painel/midia/99999")).status_code == 404


class TestReplyEAnexoDaThaina:
    """P4 (citar mensagem) e P5 (enviar foto/documento)."""

    async def _conversa_com_mensagem(self, maker, numero, wamid="wamid.paciente"):
        async with maker() as s:
            c = Conversa(numero_whatsapp=numero, modo="humano", estado="escalado")
            s.add(c)
            await s.flush()
            m = Mensagem(
                conversa_id=c.id,
                direcao="recebida",
                origem="paciente",
                tipo="texto",
                texto="quanto custa?",
                whatsapp_message_id=wamid,
            )
            s.add(m)
            await s.commit()
            return c.id, m.id

    @pytest.mark.asyncio
    async def test_responder_citando_manda_context_e_persiste(self, ambiente):
        client, maker = ambiente
        await _login(client)
        cid, mid = await self._conversa_com_mensagem(maker, "5531900001111")

        with patch(
            "app.services.painel.whatsapp_client.enviar_texto",
            new_callable=AsyncMock,
            return_value={"messages": [{"id": "wamid.thaina"}]},
        ) as mock_enviar:
            resp = await client.post(
                f"/painel/conversas/{cid}/responder",
                data={"texto": "São R$ 200", "responde_a_id": str(mid)},
            )
        assert resp.status_code == 200
        # Citou a mensagem do paciente pelo wamid dela.
        assert mock_enviar.await_args.kwargs["responder_a"] == "wamid.paciente"

        async with maker() as s:
            enviada = (
                await s.execute(select(Mensagem).where(Mensagem.origem == "thaina"))
            ).scalar_one()
            assert enviada.responde_a_id == mid
            assert enviada.whatsapp_message_id == "wamid.thaina"  # dá pra citar depois

    @pytest.mark.asyncio
    async def test_nao_cita_mensagem_de_outra_conversa(self, ambiente):
        """`responde_a_id` vem do form: não pode vazar mensagem de outro paciente."""
        client, maker = ambiente
        await _login(client)
        cid_a, mid_a = await self._conversa_com_mensagem(maker, "5531900002222", "wamid.a")
        cid_b, _ = await self._conversa_com_mensagem(maker, "5531900003333", "wamid.b")

        with patch(
            "app.services.painel.whatsapp_client.enviar_texto",
            new_callable=AsyncMock,
            return_value={"messages": [{"id": "wamid.x"}]},
        ) as mock_enviar:
            # Na conversa B, tenta citar uma mensagem da conversa A.
            await client.post(
                f"/painel/conversas/{cid_b}/responder",
                data={"texto": "oi", "responde_a_id": str(mid_a)},
            )
        assert mock_enviar.await_args.kwargs["responder_a"] is None  # ignorou a citação

        async with maker() as s:
            enviada = (
                await s.execute(select(Mensagem).where(Mensagem.origem == "thaina"))
            ).scalar_one()
            assert enviada.responde_a_id is None

    @pytest.mark.asyncio
    async def test_enviar_imagem_sobe_envia_e_guarda(self, ambiente):
        client, maker = ambiente
        await _login(client)
        cid, _ = await self._conversa_com_mensagem(maker, "5531900004444")

        with patch(
            "app.services.painel.whatsapp_client.subir_midia",
            new_callable=AsyncMock,
            return_value="media-999",
        ) as mock_subir, patch(
            "app.services.painel.whatsapp_client.enviar_midia",
            new_callable=AsyncMock,
            return_value={"messages": [{"id": "wamid.img"}]},
        ) as mock_enviar:
            resp = await client.post(
                f"/painel/conversas/{cid}/responder",
                data={"texto": "olha o comprovante"},
                files={"anexo": ("foto.png", b"\x89PNG", "image/png")},
            )
        assert resp.status_code == 200
        mock_subir.assert_awaited_once()
        assert mock_enviar.await_args.args[2] == "image"  # tipo
        assert mock_enviar.await_args.kwargs["legenda"] == "olha o comprovante"

        async with maker() as s:
            anexo = (await s.execute(select(Midia))).scalar_one()
            assert anexo.mime == "image/png"
            enviada = await s.get(Mensagem, anexo.mensagem_id)
            assert enviada.origem == "thaina" and enviada.tipo == "image"

    @pytest.mark.asyncio
    async def test_pdf_vai_como_documento(self, ambiente):
        client, maker = ambiente
        await _login(client)
        cid, _ = await self._conversa_com_mensagem(maker, "5531900005555")

        with patch(
            "app.services.painel.whatsapp_client.subir_midia",
            new_callable=AsyncMock,
            return_value="media-1",
        ), patch(
            "app.services.painel.whatsapp_client.enviar_midia",
            new_callable=AsyncMock,
            return_value={"messages": [{"id": "w"}]},
        ) as mock_enviar:
            await client.post(
                f"/painel/conversas/{cid}/responder",
                files={"anexo": ("contrato.pdf", b"%PDF", "application/pdf")},
            )
        assert mock_enviar.await_args.args[2] == "document"
        assert mock_enviar.await_args.kwargs["nome"] == "contrato.pdf"

    @pytest.mark.asyncio
    async def test_arquivo_grande_demais_e_recusado(self, ambiente):
        client, maker = ambiente
        await _login(client)
        cid, _ = await self._conversa_com_mensagem(maker, "5531900006666")
        gigante = b"x" * (midia_service.TAMANHO_MAXIMO + 10)

        resp = await client.post(
            f"/painel/conversas/{cid}/responder",
            files={"anexo": ("grande.png", gigante, "image/png")},
        )
        assert resp.status_code == 413
        async with maker() as s:
            assert (await s.execute(select(Midia))).scalar_one_or_none() is None

    @pytest.mark.asyncio
    async def test_mensagem_vazia_nao_envia_nada(self, ambiente):
        client, maker = ambiente
        await _login(client)
        cid, _ = await self._conversa_com_mensagem(maker, "5531900007777")
        with patch(
            "app.services.painel.whatsapp_client.enviar_texto", new_callable=AsyncMock
        ) as mock_enviar:
            resp = await client.post(f"/painel/conversas/{cid}/responder", data={"texto": "   "})
        assert resp.status_code == 200
        mock_enviar.assert_not_awaited()
