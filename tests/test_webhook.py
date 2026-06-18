"""Testes para webhook do WhatsApp"""

import hashlib
import hmac
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base
from app.main import app
from app.routers import webhook as webhook_module
from app.routers.webhook import extrair_mensagens, processar_payload, verify_signature
from app.services import conversation, llm_client


class TestWebhookVerification:
    """Testes para GET /webhook/whatsapp (validação Meta)"""

    def test_verify_webhook_success(self):
        """Deve validar webhook quando token está correto"""
        # Use o token do .env
        from app.config import settings

        client = TestClient(app)
        response = client.get(
            "/webhook/whatsapp",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": settings.whatsapp_verify_token,
                "hub.challenge": "test-challenge-123",
            },
        )
        assert response.status_code == 200
        assert "test-challenge-123" in response.text

    def test_verify_webhook_invalid_token(self):
        """Deve rejeitar webhook com token inválido"""
        client = TestClient(app)
        response = client.get(
            "/webhook/whatsapp",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": "wrong-token-absolutely-invalid",
                "hub.challenge": "test-challenge-123",
            },
        )
        assert response.status_code == 403

    def test_verify_webhook_invalid_mode(self):
        """Deve rejeitar webhook com mode inválido"""
        from app.config import settings

        client = TestClient(app)
        response = client.get(
            "/webhook/whatsapp",
            params={
                "hub.mode": "invalid",
                "hub.verify_token": settings.whatsapp_verify_token,
                "hub.challenge": "test-challenge-123",
            },
        )
        assert response.status_code == 403


class TestSignatureVerification:
    """Testes para função verify_signature"""

    def test_valid_signature(self):
        """Deve validar assinatura correta"""
        body = b"test payload"
        secret = "test-secret"

        signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        x_hub_signature = f"sha256={signature}"

        # Mock config
        from app import config

        original_secret = config.settings.whatsapp_app_secret
        config.settings.whatsapp_app_secret = secret

        try:
            assert verify_signature(body, x_hub_signature) is True
        finally:
            config.settings.whatsapp_app_secret = original_secret

    def test_invalid_signature(self):
        """Deve rejeitar assinatura inválida"""
        body = b"test payload"
        x_hub_signature = "sha256=invalid_signature"

        assert verify_signature(body, x_hub_signature) is False

    def test_missing_signature(self):
        """Deve rejeitar request sem assinatura"""
        body = b"test payload"
        assert verify_signature(body, "") is False


def _payload_texto(numero="5531999998888", texto="olá", msg_id="wamid.abc"):
    """Monta um payload de webhook com uma mensagem de texto."""
    return {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {
                                    "from": numero,
                                    "id": msg_id,
                                    "type": "text",
                                    "text": {"body": texto},
                                }
                            ]
                        }
                    }
                ]
            }
        ]
    }


class TestExtrairMensagens:
    """Testes para o parser do payload do webhook"""

    def test_extrai_mensagem_texto(self):
        mensagens = extrair_mensagens(_payload_texto(texto="oi"))
        assert len(mensagens) == 1
        assert mensagens[0]["text"]["body"] == "oi"

    def test_ignora_evento_de_status(self):
        """Eventos de status (entregue/lido) não têm 'messages'."""
        payload = {"entry": [{"changes": [{"value": {"statuses": [{"status": "delivered"}]}}]}]}
        assert extrair_mensagens(payload) == []

    def test_payload_vazio(self):
        assert extrair_mensagens({}) == []


@pytest_asyncio.fixture
async def db_em_memoria():
    """Patcha o async_session do webhook para um SQLite em memória isolado."""
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    with patch("app.routers.webhook.async_session", maker):
        yield maker
    await engine.dispose()


class _FakeLLM:
    """LLM falso para os testes: registra o histórico e devolve texto fixo."""

    def __init__(self, resposta="Oi, sou a Sofia da Allos."):
        self.resposta = resposta
        self.historicos: list[list[dict]] = []

    async def gerar_resposta(self, historico, tools=None):
        self.historicos.append(historico)
        return llm_client.LLMResposta(texto=self.resposta)


class _FakeLLMComTool:
    """Primeiro turno pede escalada; segundo turno (round-trip) dá a fala final."""

    def __init__(self, texto_final="Vou chamar a Thainá pra você. 🩵"):
        self.texto_final = texto_final
        self.chamadas = 0

    async def gerar_resposta(self, historico, tools=None):
        self.chamadas += 1
        if self.chamadas == 1:
            return llm_client.LLMResposta(
                tool_calls=[
                    llm_client.ToolCall(
                        id="call_1",
                        name="escalar_para_thaina",
                        arguments={"motivo": "pedido_humano"},
                    )
                ],
            )
        return llm_client.LLMResposta(texto=self.texto_final)


class TestProcessarPayload:
    """Testes para o processamento async (persistência + resposta via LLM)"""

    @pytest.mark.asyncio
    async def test_responde_texto_com_llm(self, db_em_memoria):
        fake = _FakeLLM(resposta="Oi! Como posso te ajudar?")
        with patch(
            "app.routers.webhook.whatsapp_client.enviar_texto",
            new_callable=AsyncMock,
        ) as mock_enviar, patch("app.routers.webhook.llm_client.get_llm_client", return_value=fake):
            await processar_payload(_payload_texto(numero="5531911112222", texto="oi"))

        mock_enviar.assert_awaited_once_with("5531911112222", "Oi! Como posso te ajudar?")
        # O histórico enviado ao LLM termina com a mensagem do paciente.
        assert fake.historicos[0][-1] == {"role": "user", "content": "oi"}

    @pytest.mark.asyncio
    async def test_escala_para_thaina(self, db_em_memoria):
        """Tool escalar_para_thaina: marca humano, alerta a Thainá, responde."""
        fake = _FakeLLMComTool()
        with patch(
            "app.routers.webhook.whatsapp_client.enviar_texto",
            new_callable=AsyncMock,
        ) as mock_texto, patch(
            "app.services.escalation.whatsapp_client.enviar_template",
            new_callable=AsyncMock,
        ) as mock_template, patch(
            "app.routers.webhook.llm_client.get_llm_client", return_value=fake
        ):
            await processar_payload(
                _payload_texto(
                    numero="5531977776666",
                    texto="quero falar com uma pessoa",
                    msg_id="wamid.esc",
                )
            )

        mock_template.assert_awaited_once()
        mock_texto.assert_awaited_once()
        _, texto = mock_texto.await_args.args
        assert texto == fake.texto_final

        async with db_em_memoria() as s:
            conversa = await conversation.obter_ou_criar_conversa(s, "5531977776666")
            assert conversa.modo == "humano"
            assert conversa.estado == "escalado"

    @pytest.mark.asyncio
    async def test_llm_falha_usa_fallback(self, db_em_memoria):
        class _LLMQuebra:
            async def gerar_resposta(self, historico, tools=None):
                raise llm_client.LLMError("boom")

        with patch(
            "app.routers.webhook.whatsapp_client.enviar_texto",
            new_callable=AsyncMock,
        ) as mock_enviar, patch(
            "app.routers.webhook.llm_client.get_llm_client",
            return_value=_LLMQuebra(),
        ):
            await processar_payload(_payload_texto(texto="oi"))

        _, texto = mock_enviar.await_args.args
        assert texto == webhook_module.FALLBACK_RESPOSTA

    @pytest.mark.asyncio
    async def test_modo_humano_nao_responde(self, db_em_memoria):
        """Em modo humano o bot só persiste; quem responde é a Thainá."""
        async with db_em_memoria() as s:
            conversa = await conversation.obter_ou_criar_conversa(s, "5531911112222")
            conversa.modo = "humano"
            await s.commit()

        fake = _FakeLLM()
        with patch(
            "app.routers.webhook.whatsapp_client.enviar_texto",
            new_callable=AsyncMock,
        ) as mock_enviar, patch("app.routers.webhook.llm_client.get_llm_client", return_value=fake):
            await processar_payload(
                _payload_texto(numero="5531911112222", texto="oi", msg_id="wamid.h")
            )

        mock_enviar.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_mensagem_duplicada_e_ignorada(self, db_em_memoria):
        """Mesmo wamid duas vezes: só responde uma vez (idempotência)."""
        payload = _payload_texto(msg_id="wamid.dup")
        fake = _FakeLLM()
        with patch(
            "app.routers.webhook.whatsapp_client.enviar_texto",
            new_callable=AsyncMock,
        ) as mock_enviar, patch("app.routers.webhook.llm_client.get_llm_client", return_value=fake):
            await processar_payload(payload)
            await processar_payload(payload)

        mock_enviar.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_tipo_nao_texto_pede_texto(self, db_em_memoria):
        payload = {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "messages": [
                                    {
                                        "from": "5531911112222",
                                        "id": "wamid.audio",
                                        "type": "audio",
                                    }
                                ]
                            }
                        }
                    ]
                }
            ]
        }
        with patch(
            "app.routers.webhook.whatsapp_client.enviar_texto",
            new_callable=AsyncMock,
        ) as mock_enviar:
            await processar_payload(payload)

        mock_enviar.assert_awaited_once()
        numero, texto = mock_enviar.await_args.args
        assert numero == "5531911112222"
        assert "texto" in texto.lower()


class TestHealthEndpoint:
    """Testes para health check"""

    def test_health_check(self):
        """Deve retornar 200 OK"""
        client = TestClient(app)
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"


class TestRootEndpoint:
    """Testes para root endpoint"""

    def test_root(self):
        """Deve retornar informações da app"""
        client = TestClient(app)
        response = client.get("/")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["app"] == "Sofia"
