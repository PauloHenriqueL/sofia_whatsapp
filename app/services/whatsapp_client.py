"""Cliente da WhatsApp Business Cloud API (Meta).

Wrapper fino sobre os endpoints de envio da Cloud API. Usa httpx async.

Docs: https://developers.facebook.com/docs/whatsapp/cloud-api
"""

import logging
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

GRAPH_API_VERSION = "v18.0"
GRAPH_API_BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"


class WhatsAppError(Exception):
    """Erro ao chamar a Cloud API da Meta."""


async def enviar_texto(numero: str, texto: str) -> dict[str, Any]:
    """Envia uma mensagem de texto livre para um número.

    Só funciona dentro da janela de 24h da última mensagem do paciente.
    Fora dela, é preciso usar template (ver enviar_template).

    Args:
        numero: Número do destinatário no formato internacional (ex: 5531999998888).
        texto: Corpo da mensagem.

    Returns:
        Resposta JSON da Cloud API (contém o ID da mensagem enviada).

    Raises:
        WhatsAppError: Se a API retornar erro ou a chamada falhar.
    """
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": numero,
        "type": "text",
        "text": {"body": texto},
    }
    return await _enviar(payload, descricao=f"texto para {numero}")


async def enviar_template(
    numero: str,
    template_name: str,
    parametros: list[str] | None = None,
    language_code: str = "pt_BR",
) -> dict[str, Any]:
    """Envia uma mensagem de template aprovado.

    Usado para alertar a Thainá (template `alerta_thaina`) ou para iniciar
    conversa fora da janela de 24h.

    Args:
        numero: Número do destinatário no formato internacional.
        template_name: Nome do template aprovado na Meta.
        parametros: Valores para os placeholders {{1}}, {{2}}... do corpo.
        language_code: Código do idioma do template.

    Returns:
        Resposta JSON da Cloud API.

    Raises:
        WhatsAppError: Se a API retornar erro ou a chamada falhar.
    """
    template: dict[str, Any] = {
        "name": template_name,
        "language": {"code": language_code},
    }
    if parametros:
        template["components"] = [
            {
                "type": "body",
                "parameters": [{"type": "text", "text": p} for p in parametros],
            }
        ]

    payload = {
        "messaging_product": "whatsapp",
        "to": numero,
        "type": "template",
        "template": template,
    }
    return await _enviar(payload, descricao=f"template {template_name} para {numero}")


async def _enviar(payload: dict[str, Any], descricao: str) -> dict[str, Any]:
    """Faz o POST para o endpoint de mensagens da Cloud API."""
    url = f"{GRAPH_API_BASE}/{settings.whatsapp_phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {settings.whatsapp_token}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(url, json=payload, headers=headers)
    except httpx.HTTPError as exc:
        logger.error(f"Falha de rede ao enviar {descricao}: {exc}")
        raise WhatsAppError(f"Falha de rede ao enviar {descricao}") from exc

    if response.status_code >= 400:
        logger.error(
            f"Cloud API retornou {response.status_code} ao enviar {descricao}: " f"{response.text}"
        )
        raise WhatsAppError(f"Cloud API erro {response.status_code} ao enviar {descricao}")

    data = response.json()
    logger.info(f"Enviado {descricao}: {data}")
    return data
