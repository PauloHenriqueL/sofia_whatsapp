"""Webhook do WhatsApp - Passo 1: Modo eco"""

import hashlib
import hmac
import json
import logging
from typing import Any

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.config import settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhook", tags=["webhook"])


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
        logger.warning(f"Invalid verify token")
        return JSONResponse({"error": "Invalid token"}, status_code=403)

    logger.info("Webhook verified successfully")
    return Response(content=hub_challenge)


@router.post("/whatsapp")
async def receive_webhook(request: Request):
    """POST /webhook/whatsapp - Recebe mensagens do WhatsApp

    Passo 1 (MVP): Apenas validar assinatura e logar payload.
    Futuros passos vão processar a mensagem.

    Validação de assinatura: X-Hub-Signature-256 header com HMAC SHA256
    """
    # Validar assinatura
    x_hub_signature = request.headers.get("X-Hub-Signature-256", "")
    body = await request.body()

    if not verify_signature(body, x_hub_signature):
        logger.warning("Invalid webhook signature")
        return JSONResponse({"error": "Invalid signature"}, status_code=403)

    # Log do payload (por enquanto, apenas eco)
    try:
        payload = json.loads(body)
        logger.info(f"Webhook received: {json.dumps(payload, indent=2)}")

        # Responder imediatamente (Meta quer 200 em <3s)
        # Processamento async vai acontecer depois
        return JSONResponse({"status": "received"})

    except json.JSONDecodeError:
        logger.error("Invalid JSON payload")
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)


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
