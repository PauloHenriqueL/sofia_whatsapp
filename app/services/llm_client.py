"""Cliente LLM: interface abstrata + implementação OpenAI.

Passo 4: gera respostas em texto a partir do histórico da conversa.
Tool calling (`cadastrar_paciente`, `escalar_para_thaina`) entra no Passo 5.

A interface `LLMClient` existe pra permitir trocar de provedor (OpenAI por
Claude ou outro) sem mexer no resto da aplicação.
"""

import logging
from abc import ABC, abstractmethod
from functools import lru_cache
from pathlib import Path

from openai import AsyncOpenAI, OpenAIError

from app.config import settings

logger = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "sofia_v01.txt"


class LLMError(Exception):
    """Falha ao gerar resposta no provedor LLM."""


@lru_cache(maxsize=1)
def carregar_system_prompt() -> str:
    """Lê o system prompt versionado do disco (cacheado em memória)."""
    return PROMPT_PATH.read_text(encoding="utf-8").strip()


class LLMClient(ABC):
    """Interface de geração de resposta. Implementação atual: OpenAIClient."""

    @abstractmethod
    async def gerar_resposta(self, historico: list[dict[str, str]]) -> str:
        """Gera a próxima resposta da Sofia.

        Args:
            historico: mensagens em ordem cronológica, cada uma no formato
                {"role": "user" | "assistant", "content": "..."}. O system
                prompt é adicionado pela implementação, não vem no histórico.

        Returns:
            Texto da resposta gerada.

        Raises:
            LLMError: se o provedor falhar ou devolver resposta vazia.
        """


class OpenAIClient(LLMClient):
    """Implementação usando a API de Chat Completions da OpenAI (async)."""

    def __init__(
        self,
        model: str | None = None,
        client: AsyncOpenAI | None = None,
        temperature: float = 0.7,
    ) -> None:
        self._model = model or settings.openai_model
        self._client = client or AsyncOpenAI(api_key=settings.openai_api_key)
        self._temperature = temperature

    async def gerar_resposta(self, historico: list[dict[str, str]]) -> str:
        mensagens = [
            {"role": "system", "content": carregar_system_prompt()},
            *historico,
        ]
        try:
            resposta = await self._client.chat.completions.create(
                model=self._model,
                messages=mensagens,
                temperature=self._temperature,
            )
        except OpenAIError as exc:
            logger.error(f"OpenAI falhou ao gerar resposta: {exc}")
            raise LLMError("Falha ao gerar resposta no OpenAI") from exc

        conteudo = resposta.choices[0].message.content
        if not conteudo or not conteudo.strip():
            raise LLMError("OpenAI retornou resposta vazia")
        return conteudo.strip()


@lru_cache(maxsize=1)
def get_llm_client() -> LLMClient:
    """Retorna o cliente LLM padrão (singleton). Ponto único de troca/mocking."""
    return OpenAIClient()
