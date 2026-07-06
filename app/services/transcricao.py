"""Transcrição de áudio (speech-to-text) via OpenAI.

Usado quando o paciente manda áudio e a transcrição está ligada: a Sofia
"ouve" o áudio virando texto e responde normalmente (em texto).

LGPD: o conteúdo transcrito é dado de saúde sensível — **nunca** é logado em
claro, só metadados (tamanho).
"""

import io
import logging

from openai import AsyncOpenAI, OpenAIError

from app.config import settings

logger = logging.getLogger(__name__)


class TranscricaoError(Exception):
    """Falha ao transcrever o áudio."""


def _extensao(mime: str) -> str:
    """Extensão pro nome do arquivo (a OpenAI detecta o formato por ela).

    O WhatsApp manda voz em OGG/Opus; cobrimos os formatos mais comuns.
    """
    mime = (mime or "").lower()
    if "ogg" in mime or "opus" in mime:
        return "ogg"
    if "mpeg" in mime or "mp3" in mime:
        return "mp3"
    if "m4a" in mime or "mp4" in mime or "aac" in mime:
        return "m4a"
    if "wav" in mime:
        return "wav"
    return "ogg"


async def transcrever_audio(conteudo: bytes, mime: str) -> str:
    """Transcreve os bytes do áudio pra texto (pt-BR). Levanta TranscricaoError na falha."""
    arquivo = io.BytesIO(conteudo)
    arquivo.name = f"audio.{_extensao(mime)}"  # a OpenAI usa o nome pra detectar o formato
    client = AsyncOpenAI(api_key=settings.openai_api_key)
    try:
        resp = await client.audio.transcriptions.create(
            model=settings.openai_audio_model,
            file=arquivo,
            language="pt",
        )
    except OpenAIError as exc:
        logger.error("OpenAI falhou ao transcrever áudio: %s", exc)
        raise TranscricaoError("falha na transcrição") from exc

    texto = (getattr(resp, "text", "") or "").strip()
    logger.info("Áudio transcrito (%d caracteres)", len(texto))  # sem conteúdo (LGPD)
    return texto
