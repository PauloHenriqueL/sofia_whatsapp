"""Cliente da WhatsApp Business Cloud API (Meta).

Wrapper fino sobre os endpoints de envio da Cloud API. Usa httpx async.

Docs: https://developers.facebook.com/docs/whatsapp/cloud-api
"""

import logging
import re
from typing import Any

import httpx

from app.config import settings
from app.utils import mascarar_telefone

logger = logging.getLogger(__name__)

# v23.0+: versão em que o indicador de "digitando…" (typing_indicator) é
# suportado pela Cloud API. Em versões antigas (v18) o campo era ignorado.
GRAPH_API_VERSION = "v23.0"
GRAPH_API_BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"

# Máximo de bolhas (mensagens) por turno, pra não floodar o paciente. Parágrafos
# além disso são reagrupados na última bolha.
MAX_BOLHAS = 5

# Ritmo das bolhas: simula ~25 caracteres "digitados" por segundo, entre 0,8s e
# 4s por bolha, pra a conversa não chegar instantânea (parecer um humano digitando).
CHARS_POR_SEGUNDO = 25
PAUSA_MIN_S = 0.8
PAUSA_MAX_S = 4.0


class WhatsAppError(Exception):
    """Erro ao chamar a Cloud API da Meta."""


def dividir_em_bolhas(texto: str | None, max_bolhas: int = MAX_BOLHAS) -> list[str]:
    """Quebra a resposta da Sofia em bolhas (mensagens) do WhatsApp.

    A Sofia separa ideias com linha em branco; cada bloco vira uma mensagem, pra
    a conversa parecer um papo e não um textão. Resposta curta (um bloco só) sai
    como bolha única, sem fragmentar à toa. Acima de `max_bolhas`, o excedente é
    reagrupado na última bolha.
    """
    if not texto:
        return []
    blocos = [b.strip() for b in re.split(r"\n\s*\n", texto.strip()) if b.strip()]
    if len(blocos) > max_bolhas:
        cabeca = blocos[: max_bolhas - 1]
        resto = "\n\n".join(blocos[max_bolhas - 1 :])
        blocos = cabeca + [resto]
    return blocos


def intervalo_digitacao(texto: str | None) -> float:
    """Segundos pra 'digitar' uma bolha, simulando ritmo humano (entre min e max)."""
    segundos = len(texto or "") / CHARS_POR_SEGUNDO
    return max(PAUSA_MIN_S, min(segundos, PAUSA_MAX_S))


async def marcar_como_lida(message_id: str | None, com_digitacao: bool = False) -> None:
    """Marca a mensagem recebida como lida (tique azul) e, se pedido, mostra
    'digitando…' pro paciente.

    Best-effort: é só UX, então falha aqui (rede, versão da API sem suporte a
    typing) nunca derruba a resposta. O indicador de digitação vai junto do read
    receipt; se a API rejeitar, cai pra um read receipt simples.
    """
    if not message_id:
        return
    base = {"messaging_product": "whatsapp", "status": "read", "message_id": message_id}
    try:
        payload = {**base, "typing_indicator": {"type": "text"}} if com_digitacao else base
        await _enviar(payload, descricao=f"read receipt {message_id}")
    except WhatsAppError:
        if not com_digitacao:
            return
        try:  # typing pode não ser suportado na versão da API; tenta só o read
            await _enviar(base, descricao=f"read receipt {message_id}")
        except WhatsAppError:
            pass


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
    return await _enviar(payload, descricao=f"texto para {mascarar_telefone(numero)}")


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
    return await _enviar(
        payload, descricao=f"template {template_name} para {mascarar_telefone(numero)}"
    )


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
