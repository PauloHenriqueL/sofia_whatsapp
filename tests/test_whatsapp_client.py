"""Cliente da Cloud API: o payload que sai pra Meta é contrato externo.

Usa `httpx.MockTransport` (não `unittest.mock`) pra o request ser montado de
verdade — assim o teste pega erro de forma do payload, não só de chamada.
"""

import httpx
import pytest

from app.services import whatsapp_client as wc


class _Captura:
    """Intercepta os requests e devolve uma resposta canned."""

    def __init__(self, status=200, corpo=None):
        self.status = status
        self.corpo = corpo if corpo is not None else {"messages": [{"id": "wamid.novo"}]}
        self.requests: list[httpx.Request] = []

    @property
    def payload(self) -> dict:
        import json

        return json.loads(self.requests[-1].content)


def _instalar(monkeypatch, handler) -> None:
    """Faz todo httpx.AsyncClient do módulo usar o handler dado."""
    original = httpx.AsyncClient
    monkeypatch.setattr(
        wc.httpx,
        "AsyncClient",
        lambda *a, **k: original(*a, **{**k, "transport": httpx.MockTransport(handler)}),
    )


def _instalar_captura(monkeypatch, status=200, corpo=None) -> _Captura:
    cap = _Captura(status=status, corpo=corpo)

    def handler(request: httpx.Request) -> httpx.Response:
        cap.requests.append(request)
        return httpx.Response(cap.status, json=cap.corpo)

    _instalar(monkeypatch, handler)
    return cap


@pytest.fixture(autouse=True)
def _sem_dry_run(monkeypatch):
    """Este arquivo testa o caminho REAL: o payload que sai pra Meta.

    O dry run liga sozinho fora de `production`, e como o `.env` de dev diz
    `development`, sem isto todos os testes de payload passariam a exercitar o
    atalho e nunca mais montariam um request. A trava tem classe própria
    (`TestDryRun`), que a desliga deste autouse.
    """
    monkeypatch.setattr(wc.settings, "whatsapp_dry_run", False)


@pytest.fixture
def captura(monkeypatch):
    """Cliente que responde 200 e guarda os requests enviados."""
    return _instalar_captura(monkeypatch)


class TestDryRun:
    """A trava que impede a app rodando no laptop de falar com paciente real.

    O `.env` de desenvolvimento carrega o token do número REAL da Allos. Sem
    isto, `uvicorn app.main:app` manda mensagem de verdade e o alerta cai no
    celular da Thainá, sem nenhum aviso de que aquilo era um teste.
    """

    @pytest.fixture(autouse=True)
    def _liga_dry_run(self, monkeypatch):
        monkeypatch.setattr(wc.settings, "whatsapp_dry_run", True)

    @pytest.mark.asyncio
    async def test_nao_chama_a_meta(self, monkeypatch):
        cap = _instalar_captura(monkeypatch)
        await wc.enviar_texto("5531999998888", "oi")
        assert cap.requests == []

    @pytest.mark.asyncio
    async def test_devolve_a_mesma_forma_da_resposta_da_meta(self, monkeypatch):
        """O fluxo persiste esse id e o painel usa pra citar mensagem."""
        _instalar_captura(monkeypatch)
        resposta = await wc.enviar_texto("5531999998888", "oi")
        assert isinstance(wc.id_da_resposta(resposta), str)

    @pytest.mark.asyncio
    async def test_template_tambem_e_bloqueado(self, monkeypatch):
        """O alerta da Thainá vai por template: é o que acorda um humano."""
        cap = _instalar_captura(monkeypatch)
        await wc.enviar_template("5531999998888", "alerta_thaina", parametros=["X", "Y"])
        assert cap.requests == []

    @pytest.mark.asyncio
    async def test_upload_de_midia_tem_endpoint_proprio_e_tambem_e_bloqueado(self, monkeypatch):
        cap = _instalar_captura(monkeypatch)
        media_id = await wc.subir_midia(b"conteudo", "image/png", "a.png")
        assert cap.requests == []
        assert media_id.startswith("dryrun-media-")

    @pytest.mark.asyncio
    async def test_read_receipt_nao_vaza(self, monkeypatch):
        cap = _instalar_captura(monkeypatch)
        await wc.marcar_como_lida("wamid.x", com_digitacao=True)
        assert cap.requests == []


class TestDecisaoDoDryRun:
    """Seguro por omissão: liga sozinho em qualquer ambiente que não seja produção."""

    @pytest.mark.parametrize("ambiente", ["development", "test", "staging", ""])
    def test_fora_de_producao_bloqueia(self, monkeypatch, ambiente):
        monkeypatch.setattr(wc.settings, "whatsapp_dry_run", None)
        monkeypatch.setattr(wc.settings, "environment", ambiente)
        assert wc.settings.envio_whatsapp_bloqueado is True

    def test_producao_envia(self, monkeypatch):
        monkeypatch.setattr(wc.settings, "whatsapp_dry_run", None)
        monkeypatch.setattr(wc.settings, "environment", "production")
        assert wc.settings.envio_whatsapp_bloqueado is False

    def test_env_explicita_manda_nos_dois_sentidos(self, monkeypatch):
        monkeypatch.setattr(wc.settings, "environment", "production")
        monkeypatch.setattr(wc.settings, "whatsapp_dry_run", True)
        assert wc.settings.envio_whatsapp_bloqueado is True
        monkeypatch.setattr(wc.settings, "environment", "development")
        monkeypatch.setattr(wc.settings, "whatsapp_dry_run", False)
        assert wc.settings.envio_whatsapp_bloqueado is False


class TestIdDaResposta:
    """Vai pra uma coluna com índice único: só `str` pode passar."""

    def test_extrai_wamid(self):
        assert wc.id_da_resposta({"messages": [{"id": "wamid.abc"}]}) == "wamid.abc"

    def test_payload_torto_vira_none(self):
        for entrada in (
            None,
            "texto",
            {},
            {"messages": []},
            {"messages": "nao-e-lista"},
            {"messages": [{"sem_id": 1}]},
            {"messages": [{"id": 123}]},  # id não-string
            {"messages": ["nao-e-dict"]},
        ):
            assert wc.id_da_resposta(entrada) is None


class TestEnviarTexto:
    @pytest.mark.asyncio
    async def test_monta_payload_de_texto(self, captura):
        await wc.enviar_texto("5531999998888", "oi")
        p = captura.payload
        assert p["messaging_product"] == "whatsapp"
        assert p["to"] == "5531999998888"
        assert p["type"] == "text"
        assert p["text"]["body"] == "oi"
        assert "context" not in p  # sem citar

    @pytest.mark.asyncio
    async def test_reply_manda_context(self, captura):
        await wc.enviar_texto("5531999998888", "oi", responder_a="wamid.citada")
        assert captura.payload["context"] == {"message_id": "wamid.citada"}

    @pytest.mark.asyncio
    async def test_erro_da_api_vira_whatsapp_error(self, monkeypatch):
        _instalar_captura(monkeypatch, status=400, corpo={"error": {"message": "invalid"}})
        with pytest.raises(wc.WhatsAppError):
            await wc.enviar_texto("5531999998888", "oi")


class TestSubirMidia:
    @pytest.mark.asyncio
    async def test_devolve_media_id(self, monkeypatch):
        cap = _instalar_captura(monkeypatch, corpo={"id": "media-42"})
        assert await wc.subir_midia(b"\x89PNG", "image/png", "foto.png") == "media-42"
        # Sobe pro endpoint de mídia, não pro de mensagens.
        assert cap.requests[-1].url.path.endswith("/media")

    @pytest.mark.asyncio
    async def test_sem_id_na_resposta_falha(self, monkeypatch):
        _instalar_captura(monkeypatch, corpo={"sem": "id"})
        with pytest.raises(wc.WhatsAppError):
            await wc.subir_midia(b"x", "image/png", "f.png")

    @pytest.mark.asyncio
    async def test_id_nao_string_falha(self, monkeypatch):
        _instalar_captura(monkeypatch, corpo={"id": 42})
        with pytest.raises(wc.WhatsAppError):
            await wc.subir_midia(b"x", "image/png", "f.png")


class TestEnviarMidia:
    @pytest.mark.asyncio
    async def test_imagem_com_legenda(self, captura):
        await wc.enviar_midia("5531999998888", "media-1", "image", legenda="olha")
        p = captura.payload
        assert p["type"] == "image"
        assert p["image"] == {"id": "media-1", "caption": "olha"}

    @pytest.mark.asyncio
    async def test_documento_leva_filename(self, captura):
        await wc.enviar_midia("5531999998888", "media-1", "document", nome="laudo.pdf")
        assert captura.payload["document"]["filename"] == "laudo.pdf"

    @pytest.mark.asyncio
    async def test_imagem_nao_leva_filename(self, captura):
        """A Cloud API rejeita `filename` em imagem."""
        await wc.enviar_midia("5531999998888", "media-1", "image", nome="foto.png")
        assert "filename" not in captura.payload["image"]

    @pytest.mark.asyncio
    async def test_midia_com_reply(self, captura):
        await wc.enviar_midia("5531999998888", "m", "image", responder_a="wamid.x")
        assert captura.payload["context"] == {"message_id": "wamid.x"}

    @pytest.mark.asyncio
    async def test_tipo_invalido_recusado(self, captura):
        with pytest.raises(wc.WhatsAppError):
            await wc.enviar_midia("5531999998888", "m", "video")


class TestBaixarMidia:
    @pytest.mark.asyncio
    async def test_dois_passos_url_e_bytes(self, monkeypatch):
        """GET /{media_id} -> URL temporária; GET nessa URL -> bytes."""
        chamadas = []

        def handler(request: httpx.Request) -> httpx.Response:
            chamadas.append(str(request.url))
            if "lookaside" in str(request.url):
                return httpx.Response(200, content=b"BYTES")
            return httpx.Response(
                200, json={"url": "https://lookaside.fbsbx.com/x", "mime_type": "image/png"}
            )

        _instalar(monkeypatch, handler)
        conteudo, mime = await wc.baixar_midia("media-1")
        assert conteudo == b"BYTES"
        assert mime == "image/png"
        assert len(chamadas) == 2

    @pytest.mark.asyncio
    async def test_sem_url_falha(self, monkeypatch):
        _instalar(monkeypatch, lambda r: httpx.Response(200, json={}))
        with pytest.raises(wc.WhatsAppError):
            await wc.baixar_midia("media-1")


class TestMarcarComoLida:
    @pytest.mark.asyncio
    async def test_read_receipt_com_digitacao(self, captura):
        await wc.marcar_como_lida("wamid.x", com_digitacao=True)
        p = captura.payload
        assert p["status"] == "read"
        assert p["typing_indicator"] == {"type": "text"}

    @pytest.mark.asyncio
    async def test_sem_message_id_nao_chama(self, captura):
        await wc.marcar_como_lida(None)
        assert captura.requests == []

    @pytest.mark.asyncio
    async def test_falha_no_typing_cai_pro_read_simples(self, monkeypatch):
        """Presença é só UX: se o typing for rejeitado, ainda marca como lida."""
        vistos = []

        def handler(request: httpx.Request) -> httpx.Response:
            import json

            corpo = json.loads(request.content)
            vistos.append(corpo)
            if "typing_indicator" in corpo:
                return httpx.Response(400, json={"error": "unsupported"})
            return httpx.Response(200, json={})

        _instalar(monkeypatch, handler)
        await wc.marcar_como_lida("wamid.x", com_digitacao=True)  # não levanta
        assert len(vistos) == 2
        assert "typing_indicator" not in vistos[1]
