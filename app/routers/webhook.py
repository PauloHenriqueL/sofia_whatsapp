"""Webhook do WhatsApp - Passo 4: resposta gerada por LLM (OpenAI)"""

import hashlib
import hmac
import json
import logging
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.config import settings
from app.database import async_session
from app.services import conversation, llm_client, whatsapp_client

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhook", tags=["webhook"])

# Resposta de degradação quando o LLM falha. Revisada manualmente (não passa
# pelo prompt da Sofia); segue o estilo dela: sem travessões, acolhedora.
FALLBACK_RESPOSTA = (
    "Oi, tive um probleminha técnico aqui pra te responder agora. "
    "Pode me mandar de novo daqui a pouco?"
)

# Resposta fixa para tipos sem texto (áudio/imagem/etc). A detecção de áudio
# com escalada entra no Passo 5; por ora pedimos texto.
PEDIR_TEXTO = "Por enquanto consigo ler só mensagens de texto. Pode me escrever?"


class WebhookPayload(BaseModel):
    """Estrutura simplificada do payload do webhook Meta"""

    entry: list[dict[str, Any]] = Field(default_factory=list)


@router.get("/whatsapp")
async def verify_webhook(
    hub_mode: str = Query(..., alias="hub.mode"),
    hub_verify_token: str = Query(..., alias="hub.verify_token"),
    hub_challenge: str = Query(..., alias="hub.challenge"),
):
    """GET /webhook/whatsapp - Validação inicial do webhook (Meta)

    Meta envia um desafio na primeira configuração do webhook.
    Devemos responder com o challenge se o verify_token bater.

    Docs: https://developers.facebook.com/docs/whatsapp/cloud-api/webhooks
    """
    logger.info(f"Webhook verification: mode={hub_mode}")

    if hub_mode != "subscribe":
        logger.warning(f"Invalid hub.mode: {hub_mode}")
        return JSONResponse({"error": "Invalid mode"}, status_code=403)

    if hub_verify_token != settings.whatsapp_verify_token:
        logger.warning("Invalid verify token")
        return JSONResponse({"error": "Invalid token"}, status_code=403)

    logger.info("Webhook verified successfully")
    return Response(content=hub_challenge)


@router.post("/whatsapp")
async def receive_webhook(request: Request, background_tasks: BackgroundTasks):
    """POST /webhook/whatsapp - Recebe mensagens do WhatsApp

    Valida assinatura, responde 200 imediatamente e processa a mensagem em
    background (persistência + resposta via LLM).

    Validação de assinatura: X-Hub-Signature-256 header com HMAC SHA256
    """
    # Validar assinatura
    x_hub_signature = request.headers.get("X-Hub-Signature-256", "")
    body = await request.body()

    if not verify_signature(body, x_hub_signature):
        logger.warning("Invalid webhook signature")
        return JSONResponse({"error": "Invalid signature"}, status_code=403)

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        logger.error("Invalid JSON payload")
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    logger.info(f"Webhook received: {json.dumps(payload, indent=2)}")

    # Responde 200 imediatamente (Meta exige <3s). Processa async.
    background_tasks.add_task(processar_payload, payload)
    return JSONResponse({"status": "received"})


def extrair_mensagens(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Extrai a lista de mensagens recebidas do payload do webhook.

    O webhook da Meta também envia eventos de status (entregue, lido) que
    não contêm `messages`; esses são ignorados.
    """
    mensagens: list[dict[str, Any]] = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            mensagens.extend(value.get("messages", []))
    return mensagens


async def gerar_resposta_bot(session, conversa) -> str:
    """Gera a resposta do bot via LLM a partir do histórico da conversa.

    Em caso de falha do LLM, loga e devolve uma resposta de degradação para
    não deixar o paciente sem retorno (a falha não derruba a conversa).
    """
    historico = await conversation.carregar_historico(session, conversa)
    try:
        return await llm_client.get_llm_client().gerar_resposta(historico)
    except llm_client.LLMError:
        logger.error(f"LLM falhou para conversa {conversa.id}; usando fallback")
        return FALLBACK_RESPOSTA


async def processar_payload(payload: dict[str, Any]) -> None:
    """Processa o payload do webhook em background.

    Passo 4: persiste conversa e mensagem (com idempotência) e, quando a
    conversa está em modo bot, gera a resposta via LLM. Em modo humano apenas
    persiste (o painel da Thainá cuida da resposta). Tipos sem texto recebem
    uma mensagem fixa pedindo texto (áudio com escalada vem no Passo 5).
    """
    for mensagem in extrair_mensagens(payload):
        numero = mensagem.get("from")
        tipo = mensagem.get("type")
        wamid = mensagem.get("id")
        if not numero:
            continue

        async with async_session() as session:
            if await conversation.mensagem_ja_processada(session, wamid):
                logger.info(f"Mensagem {wamid} já processada, ignorando")
                continue

            conversa = await conversation.obter_ou_criar_conversa(session, numero)

            if tipo == "text":
                texto = mensagem.get("text", {}).get("body", "")
                await conversation.registrar_mensagem_recebida(
                    session,
                    conversa,
                    tipo="texto",
                    texto=texto,
                    whatsapp_message_id=wamid,
                )
            else:
                await conversation.registrar_mensagem_recebida(
                    session,
                    conversa,
                    tipo=tipo,
                    texto=None,
                    whatsapp_message_id=wamid,
                )

            # Modo humano: só persiste; a Thainá responde pelo painel.
            if conversa.modo == "humano":
                await session.commit()
                continue

            if tipo == "text":
                resposta = await gerar_resposta_bot(session, conversa)
            else:
                resposta = PEDIR_TEXTO

            await session.commit()

            enviado = False
            try:
                await whatsapp_client.enviar_texto(numero, resposta)
                enviado = True
            except whatsapp_client.WhatsAppError:
                # Já logado no cliente; persistência da entrada não é perdida.
                logger.error(f"Não consegui responder ao número {numero}")

            if enviado:
                await conversation.registrar_mensagem_enviada(session, conversa, texto=resposta)
                await session.commit()


def verify_signature(body: bytes, x_hub_signature: str) -> bool:
    """Verifica assinatura HMAC SHA256 do webhook Meta

    Args:
        body: Request body em bytes
        x_hub_signature: Header X-Hub-Signature-256 (formato: sha256=...)

    Returns:
        True se assinatura é válida, False caso contrário
    """
    if not x_hub_signature:
        return False

    # Remover prefixo "sha256=" se estiver presente
    if x_hub_signature.startswith("sha256="):
        signature = x_hub_signature[7:]
    else:
        signature = x_hub_signature

    # Calcular HMAC esperado
    expected = hmac.new(
        settings.whatsapp_app_secret.encode(),
        body,
        hashlib.sha256,
    ).hexdigest()

    # Comparação timing-safe
    return hmac.compare_digest(signature, expected)
