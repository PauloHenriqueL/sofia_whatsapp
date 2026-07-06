"""Testes de áudio: baixar mídia do WhatsApp + transcrever (STT) via OpenAI."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from openai import OpenAIError

from app.services import transcricao, whatsapp_client


class _FakeResp:
    def __init__(self, status=200, json_data=None, content=b""):
        self.status_code = status
        self._json = json_data or {}
        self.content = content

    def json(self):
        return self._json


class _FakeHttpClient:
    """Simula httpx.AsyncClient: devolve respostas na ordem e registra as URLs."""

    def __init__(self, respostas):
        self._respostas = list(respostas)
        self.urls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, headers=None):
        self.urls.append(url)
        return self._respostas.pop(0)


class TestBaixarMidia:
    @pytest.mark.asyncio
    async def test_faz_dois_gets_url_depois_binario(self):
        info = _FakeResp(json_data={"url": "https://cdn/abc", "mime_type": "audio/ogg"})
        binario = _FakeResp(content=b"OGGBYTES")
        fake = _FakeHttpClient([info, binario])
        with patch("app.services.whatsapp_client.httpx.AsyncClient", return_value=fake):
            conteudo, mime = await whatsapp_client.baixar_midia("MID123")
        assert conteudo == b"OGGBYTES"
        assert mime == "audio/ogg"
        assert "MID123" in fake.urls[0]  # 1o GET: pega a URL pelo id
        assert fake.urls[1] == "https://cdn/abc"  # 2o GET: baixa o binário

    @pytest.mark.asyncio
    async def test_erro_vira_whatsapperror(self):
        fake = _FakeHttpClient([_FakeResp(status=404)])
        with patch("app.services.whatsapp_client.httpx.AsyncClient", return_value=fake):
            with pytest.raises(whatsapp_client.WhatsAppError):
                await whatsapp_client.baixar_midia("MID404")


class TestTranscrever:
    @pytest.mark.asyncio
    async def test_transcreve_devolve_texto(self):
        fake_openai = MagicMock()
        fake_openai.audio.transcriptions.create = AsyncMock(
            return_value=MagicMock(text="quero marcar uma consulta")
        )
        with patch("app.services.transcricao.AsyncOpenAI", return_value=fake_openai):
            texto = await transcricao.transcrever_audio(b"OGGBYTES", "audio/ogg")
        assert texto == "quero marcar uma consulta"
        # o arquivo enviado é nomeado com a extensão do formato (detecção da OpenAI)
        enviado = fake_openai.audio.transcriptions.create.await_args.kwargs["file"]
        assert enviado.name.endswith(".ogg")

    @pytest.mark.asyncio
    async def test_erro_openai_vira_transcricaoerror(self):
        fake_openai = MagicMock()
        fake_openai.audio.transcriptions.create = AsyncMock(side_effect=OpenAIError("boom"))
        with patch("app.services.transcricao.AsyncOpenAI", return_value=fake_openai):
            with pytest.raises(transcricao.TranscricaoError):
                await transcricao.transcrever_audio(b"x", "audio/ogg")
