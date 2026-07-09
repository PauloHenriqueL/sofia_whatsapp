"""Testes para webhook do WhatsApp"""

import asyncio
import hashlib
import hmac
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base
from app.main import app
from app.models import Conversa, Mensagem, Midia
from app.routers import webhook as webhook_module
from app.routers.webhook import extrair_mensagens, processar_payload, verify_signature
from app.services import config_negocio, conversation, llm_client, serializacao, whatsapp_client


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


def _payload_audio(numero="5531944443333", msg_id="wamid.a", media_id="MID"):
    """Monta um payload de webhook com uma mensagem de áudio (com media id)."""
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
                                    "type": "audio",
                                    "audio": {"id": media_id, "mime_type": "audio/ogg"},
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


@pytest.fixture(autouse=True)
def _debounce_rapido_e_isolado():
    """Janela de debounce curta (testes rápidos), presença desligada e limpeza.

    debounce/simular_digitacao agora vêm do config_negocio (editáveis no painel),
    então os testes ajustam o cache em memória em vez das settings.
    """
    orig = dict(config_negocio._cache)
    config_negocio._cache["debounce_segundos"] = 0.05
    config_negocio._cache["simular_digitacao"] = False
    yield
    config_negocio._cache.clear()
    config_negocio._cache.update(orig)
    serializacao.limpar()


async def _rodar(payload):
    """Ingere o payload e aguarda a janela de debounce fechar (só nos testes)."""
    await processar_payload(payload)
    await serializacao.aguardar_pendentes()


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
            await _rodar(_payload_texto(numero="5531911112222", texto="oi"))

        mock_enviar.assert_awaited_once_with("5531911112222", "Oi! Como posso te ajudar?")
        # O histórico enviado ao LLM termina com a mensagem do paciente.
        assert fake.historicos[0][-1] == {"role": "user", "content": "oi"}

    @pytest.mark.asyncio
    async def test_resposta_longa_vai_em_bolhas(self, db_em_memoria):
        """Resposta com parágrafos separados por linha em branco vira N bolhas,
        enviadas e persistidas em ordem."""
        fake = _FakeLLM(resposta="Primeira ideia.\n\nSegunda ideia.\n\nTerceira.")
        with patch(
            "app.routers.webhook.whatsapp_client.enviar_texto",
            new_callable=AsyncMock,
        ) as mock_enviar, patch("app.routers.webhook.llm_client.get_llm_client", return_value=fake):
            await _rodar(
                _payload_texto(numero="5531900001111", texto="me explica", msg_id="wamid.bolhas")
            )

        enviados = [c.args[1] for c in mock_enviar.await_args_list]
        assert enviados == ["Primeira ideia.", "Segunda ideia.", "Terceira."]

        async with db_em_memoria() as s:
            conversa = await conversation.obter_ou_criar_conversa(s, "5531900001111")
            historico = await conversation.carregar_historico(s, conversa)
            enviadas = [m for m in historico if m["role"] == "assistant"]
            assert len(enviadas) == 3

    @pytest.mark.asyncio
    async def test_nao_envia_json_interno_vazado_pelo_modelo(self, db_em_memoria):
        """Regressão do incidente de beta: o modelo pôs o JSON do cadastro no
        `content` em vez do canal de tool, e ele foi parar no WhatsApp da paciente.
        A fala legítima ainda tem que sair; o JSON, não (nem no banco)."""
        vazamento = (
            '{"nome_completo":"Amanda Soares Alves","data_nascimento":"2002-05-10",'
            '"endereco":"Praça Cairo, 44"}\n'
            "Te explico sim. A terapia aqui é por chamada de vídeo."
        )
        fake = _FakeLLM(resposta=vazamento)
        with patch(
            "app.routers.webhook.whatsapp_client.enviar_texto",
            new_callable=AsyncMock,
        ) as mock_enviar, patch("app.routers.webhook.llm_client.get_llm_client", return_value=fake):
            await _rodar(
                _payload_texto(numero="5531955556666", texto="me explica", msg_id="wamid.vaza")
            )

        enviados = [c.args[1] for c in mock_enviar.await_args_list]
        assert enviados == ["Te explico sim. A terapia aqui é por chamada de vídeo."]
        assert not any("Amanda" in e or "nome_completo" in e for e in enviados)

        # E o vazamento também não pode ficar persistido como mensagem enviada.
        async with db_em_memoria() as s:
            conversa = await conversation.obter_ou_criar_conversa(s, "5531955556666")
            hist = await conversation.carregar_historico(s, conversa)
            assert not any("nome_completo" in m["content"] for m in hist)

    @pytest.mark.asyncio
    async def test_nao_envia_nada_quando_a_resposta_e_so_lixo(self, db_em_memoria):
        fake = _FakeLLM(resposta='{"motivo":"crise","contexto":"x"}')
        with patch(
            "app.routers.webhook.whatsapp_client.enviar_texto",
            new_callable=AsyncMock,
        ) as mock_enviar, patch("app.routers.webhook.llm_client.get_llm_client", return_value=fake):
            await _rodar(_payload_texto(numero="5531944445555", msg_id="wamid.solixo"))

        mock_enviar.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_presenca_humana_desligada_por_padrao(self, db_em_memoria):
        """Com simular_digitacao=False (padrão), não marca lida nem dá pausa."""
        fake = _FakeLLM(resposta="Bloco um.\n\nBloco dois.")
        with patch(
            "app.routers.webhook.whatsapp_client.enviar_texto", new_callable=AsyncMock
        ), patch(
            "app.routers.webhook.whatsapp_client.marcar_como_lida", new_callable=AsyncMock
        ) as mock_lida, patch(
            "app.routers.webhook.asyncio.sleep", new_callable=AsyncMock
        ) as mock_sleep, patch(
            "app.routers.webhook.llm_client.get_llm_client", return_value=fake
        ):
            await _rodar(_payload_texto(msg_id="wamid.semdig"))

        mock_lida.assert_not_awaited()
        mock_sleep.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_presenca_humana_ligada_marca_lida_e_pausa(self, db_em_memoria):
        """Com simular_digitacao=True: marca lida com digitação e pausa por bolha."""
        config_negocio._cache["simular_digitacao"] = True  # editável no painel agora
        fake = _FakeLLM(resposta="Bloco um.\n\nBloco dois.\n\nBloco três.")
        with patch(
            "app.routers.webhook.whatsapp_client.enviar_texto", new_callable=AsyncMock
        ), patch(
            "app.routers.webhook.whatsapp_client.marcar_como_lida", new_callable=AsyncMock
        ) as mock_lida, patch(
            "app.routers.webhook.asyncio.sleep", new_callable=AsyncMock
        ) as mock_sleep, patch(
            "app.routers.webhook.llm_client.get_llm_client", return_value=fake
        ):
            await _rodar(_payload_texto(msg_id="wamid.comdig"))

        mock_lida.assert_awaited_once()
        assert mock_lida.await_args.kwargs.get("com_digitacao") is True
        assert mock_sleep.await_count == 3  # uma pausa por bolha

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
            await _rodar(
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
            await _rodar(_payload_texto(texto="oi"))

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
            await _rodar(_payload_texto(numero="5531911112222", texto="oi", msg_id="wamid.h"))

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
            await _rodar(payload)
            await _rodar(payload)

        mock_enviar.assert_awaited_once()

    @pytest.mark.asyncio
    def _payload_tipo(self, tipo, numero="5531911112222", msg_id="wamid.x"):
        return {
            "entry": [
                {
                    "changes": [
                        {"value": {"messages": [{"from": numero, "id": msg_id, "type": tipo}]}}
                    ]
                }
            ]
        }

    @pytest.mark.asyncio
    async def test_audio_escala_para_thaina(self, db_em_memoria):
        """Áudio escala imediatamente (sem LLM): marca humano, alerta, responde."""
        with patch(
            "app.routers.webhook.whatsapp_client.enviar_texto", new_callable=AsyncMock
        ) as mock_texto, patch(
            "app.services.escalation.whatsapp_client.enviar_template",
            new_callable=AsyncMock,
        ) as mock_template:
            await _rodar(self._payload_tipo("audio", numero="5531944443333", msg_id="wamid.a"))

        mock_template.assert_awaited_once()
        _, texto = mock_texto.await_args.args
        assert "áudio" in texto.lower()

        async with db_em_memoria() as s:
            conversa = await conversation.obter_ou_criar_conversa(s, "5531944443333")
            assert conversa.modo == "humano"
            assert conversa.estado == "escalado"

    @pytest.mark.asyncio
    def _payload_midia(self, tipo, numero="5531911112222", msg_id="wamid.x", **extra):
        bloco = {"id": "media-123", **extra}
        return {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "messages": [
                                    {"from": numero, "id": msg_id, "type": tipo, tipo: bloco}
                                ]
                            }
                        }
                    ]
                }
            ]
        }

    @pytest.mark.asyncio
    async def test_video_pede_texto(self, db_em_memoria):
        """Tipo sem suporte (vídeo) pede texto, sem escalar nem chamar o LLM."""
        with patch(
            "app.routers.webhook.whatsapp_client.enviar_texto", new_callable=AsyncMock
        ) as mock_texto:
            await _rodar(self._payload_tipo("video", numero="5531955554444", msg_id="wamid.v"))

        _, texto = mock_texto.await_args.args
        assert "texto" in texto.lower()
        async with db_em_memoria() as s:
            conversa = await conversation.obter_ou_criar_conversa(s, "5531955554444")
            assert conversa.modo == "bot"

    @pytest.mark.asyncio
    async def test_imagem_guarda_anexo_e_escala(self, db_em_memoria):
        """P3: imagem é baixada, guardada e escalada pra Thainá (não pede texto)."""
        with patch(
            "app.routers.webhook.whatsapp_client.enviar_texto", new_callable=AsyncMock
        ) as mock_texto, patch(
            "app.services.midia.whatsapp_client.baixar_midia",
            new_callable=AsyncMock,
            return_value=(b"\x89PNG-bytes", "image/png"),
        ), patch(
            "app.routers.webhook.escalation.alertar_thaina", new_callable=AsyncMock
        ) as mock_alerta:
            await _rodar(self._payload_midia("image", numero="5531955551111", msg_id="wamid.img"))

        _, texto = mock_texto.await_args.args
        assert "arquivo" in texto.lower()
        mock_alerta.assert_awaited_once()

        async with db_em_memoria() as s:
            conversa = await conversation.obter_ou_criar_conversa(s, "5531955551111")
            assert conversa.modo == "humano"  # escalou
            # Há 2 mensagens: a imagem recebida e a resposta do bot. Queremos a 1ª.
            msg = (
                await s.execute(
                    select(Mensagem).where(
                        Mensagem.conversa_id == conversa.id, Mensagem.tipo == "image"
                    )
                )
            ).scalar_one()
            anexo = (await s.execute(select(Midia).where(Midia.mensagem_id == msg.id))).scalar_one()
            assert anexo.mime == "image/png"
            assert anexo.tamanho == len(b"\x89PNG-bytes")

    @pytest.mark.asyncio
    async def test_documento_guarda_nome_do_arquivo(self, db_em_memoria):
        with patch(
            "app.routers.webhook.whatsapp_client.enviar_texto", new_callable=AsyncMock
        ), patch(
            "app.services.midia.whatsapp_client.baixar_midia",
            new_callable=AsyncMock,
            return_value=(b"%PDF-1.4", "application/pdf"),
        ), patch(
            "app.routers.webhook.escalation.alertar_thaina", new_callable=AsyncMock
        ):
            await _rodar(
                self._payload_midia(
                    "document", numero="5531955552222", msg_id="wamid.doc", filename="laudo.pdf"
                )
            )

        async with db_em_memoria() as s:
            anexo = (await s.execute(select(Midia))).scalar_one()
            assert anexo.nome_arquivo == "laudo.pdf"

    @pytest.mark.asyncio
    async def test_download_falho_ainda_registra_a_mensagem(self, db_em_memoria):
        """Se a Meta falhar, a Thainá ainda vê que veio um anexo (e pede de novo)."""
        with patch(
            "app.routers.webhook.whatsapp_client.enviar_texto", new_callable=AsyncMock
        ), patch(
            "app.services.midia.whatsapp_client.baixar_midia",
            new_callable=AsyncMock,
            side_effect=whatsapp_client.WhatsAppError("expirou"),
        ), patch(
            "app.routers.webhook.escalation.alertar_thaina", new_callable=AsyncMock
        ):
            await _rodar(self._payload_midia("image", numero="5531955553333", msg_id="wamid.f"))

        async with db_em_memoria() as s:
            conversa = await conversation.obter_ou_criar_conversa(s, "5531955553333")
            msg = (
                await s.execute(
                    select(Mensagem).where(
                        Mensagem.conversa_id == conversa.id, Mensagem.tipo == "image"
                    )
                )
            ).scalar_one()
            assert msg.texto == "[imagem recebida]"  # a Thainá vê que veio algo
            assert (await s.execute(select(Midia))).scalar_one_or_none() is None  # sem anexo


class TestSerializacaoDebounce:
    """Demanda 2: agrupamento por rajada, serialização por conversa e crise."""

    @pytest.mark.asyncio
    async def test_rajada_vira_uma_unica_resposta(self, db_em_memoria):
        """Várias mensagens em rajada -> uma chamada ao LLM e uma resposta."""
        fake = _FakeLLM(resposta="Resposta única.")
        with patch(
            "app.routers.webhook.whatsapp_client.enviar_texto", new_callable=AsyncMock
        ) as mock_enviar, patch("app.routers.webhook.llm_client.get_llm_client", return_value=fake):
            n = "5531900007777"
            await processar_payload(_payload_texto(numero=n, texto="oi", msg_id="w.b1"))
            await processar_payload(_payload_texto(numero=n, texto="tudo bem?", msg_id="w.b2"))
            await processar_payload(_payload_texto(numero=n, texto="queria marcar", msg_id="w.b3"))
            await serializacao.aguardar_pendentes()

        # Um único turno do modelo e uma única resposta pro paciente.
        assert len(fake.historicos) == 1
        mock_enviar.assert_awaited_once()
        # As três mensagens da rajada foram ao modelo, em ordem.
        usuarios = [m["content"] for m in fake.historicos[0] if m["role"] == "user"]
        assert usuarios == ["oi", "tudo bem?", "queria marcar"]

    @pytest.mark.asyncio
    async def test_primeira_mensagem_sem_corrida_cria_uma_conversa(self, db_em_memoria):
        """Duas mensagens simultâneas de um número novo -> uma só conversa."""
        fake = _FakeLLM()
        with patch(
            "app.routers.webhook.whatsapp_client.enviar_texto", new_callable=AsyncMock
        ), patch("app.routers.webhook.llm_client.get_llm_client", return_value=fake):
            n = "5531900008888"
            await asyncio.gather(
                processar_payload(_payload_texto(numero=n, texto="oi", msg_id="w.r1")),
                processar_payload(_payload_texto(numero=n, texto="tem vaga?", msg_id="w.r2")),
            )
            await serializacao.aguardar_pendentes()

        async with db_em_memoria() as s:
            total = await s.scalar(
                select(func.count(Conversa.id)).where(Conversa.numero_whatsapp == n)
            )
        assert total == 1

    @pytest.mark.asyncio
    async def test_crise_responde_sem_esperar_a_janela(self, db_em_memoria):
        """Mensagem de crise é processada na hora, mesmo com a janela enorme."""
        fake = _FakeLLM(resposta="Tô aqui com você. Já estou avisando a Thainá.")
        with patch.object(webhook_module.settings, "debounce_segundos", 999), patch(
            "app.routers.webhook.whatsapp_client.enviar_texto", new_callable=AsyncMock
        ) as mock_enviar, patch("app.routers.webhook.llm_client.get_llm_client", return_value=fake):
            # Sem aguardar_pendentes: se dependesse do debounce (999s), não responderia agora.
            await processar_payload(
                _payload_texto(
                    numero="5531900009999", texto="não quero mais viver", msg_id="w.crise"
                )
            )

        mock_enviar.assert_awaited_once()
        assert len(fake.historicos) == 1


class TestAudioTranscricao:
    """Áudio com transcrição ligada: ouve (transcreve) e responde em texto."""

    @pytest.mark.asyncio
    async def test_audio_transcreve_e_responde_em_texto(self, db_em_memoria):
        config_negocio._cache["transcrever_audio"] = True
        fake = _FakeLLM(resposta="Entendi, vamos marcar.")
        with patch(
            "app.routers.webhook.whatsapp_client.baixar_midia",
            new_callable=AsyncMock,
            return_value=(b"OGG", "audio/ogg"),
        ), patch(
            "app.routers.webhook.transcricao.transcrever_audio",
            new_callable=AsyncMock,
            return_value="quero marcar uma consulta",
        ), patch(
            "app.routers.webhook.whatsapp_client.enviar_texto", new_callable=AsyncMock
        ) as mock_env, patch(
            "app.routers.webhook.llm_client.get_llm_client", return_value=fake
        ):
            await _rodar(_payload_audio(numero="5531900012345", msg_id="wamid.aud1"))

        mock_env.assert_awaited_once()  # respondeu em texto (não escalou)
        # A transcrição entrou no histórico como fala do paciente.
        assert fake.historicos[0][-1] == {"role": "user", "content": "quero marcar uma consulta"}

    @pytest.mark.asyncio
    async def test_audio_falha_na_transcricao_escala(self, db_em_memoria):
        config_negocio._cache["transcrever_audio"] = True
        with patch(
            "app.routers.webhook.whatsapp_client.baixar_midia",
            new_callable=AsyncMock,
            side_effect=webhook_module.whatsapp_client.WhatsAppError("offline"),
        ), patch(
            "app.routers.webhook.whatsapp_client.enviar_texto", new_callable=AsyncMock
        ) as mock_env, patch(
            "app.services.escalation.whatsapp_client.enviar_template", new_callable=AsyncMock
        ) as mock_tpl:
            await _rodar(_payload_audio(numero="5531900067890", msg_id="wamid.aud2"))

        mock_tpl.assert_awaited_once()  # caiu no fallback: escalou pra Thainá
        _, texto = mock_env.await_args.args
        assert "áudio" in texto.lower()  # mensagem AUDIO_RECEBIDO
        async with db_em_memoria() as s:
            conversa = await conversation.obter_ou_criar_conversa(s, "5531900067890")
            assert conversa.modo == "humano"


class TestResumoPayload:
    def test_nao_vaza_conteudo_da_mensagem(self):
        from app.routers.webhook import _resumo_payload

        payload = _payload_texto(texto="ansiedade e questões pessoais")
        resumo = _resumo_payload(payload)
        assert resumo["qtd_mensagens"] == 1
        assert resumo["tipos"] == ["text"]
        # O conteúdo sensível NÃO pode aparecer no resumo de log.
        assert "ansiedade" not in str(resumo)


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

    def test_root_redireciona_pro_painel(self):
        """A raiz redireciona para o painel da Thainá."""
        client = TestClient(app)
        response = client.get("/", follow_redirects=False)
        assert response.status_code in (302, 307)
        assert response.headers["location"] == "/painel/"
