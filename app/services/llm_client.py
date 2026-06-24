"""Cliente LLM: interface abstrata + implementação OpenAI.

Passo 4: gera respostas em texto a partir do histórico da conversa.
Passo 5: suporta tool calling (`cadastrar_paciente`, `escalar_para_thaina`).

A interface `LLMClient` existe pra permitir trocar de provedor (OpenAI por
Claude ou outro) sem mexer no resto da aplicação.
"""

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from openai import AsyncOpenAI, OpenAIError

from app.config import settings
from app.services import config_negocio

logger = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "sofia_v01.txt"


class LLMError(Exception):
    """Falha ao gerar resposta no provedor LLM."""


@dataclass
class ToolCall:
    """Uma chamada de ferramenta pedida pelo modelo."""

    id: str
    name: str
    arguments: dict


@dataclass
class LLMResposta:
    """Resultado de um turno do LLM: texto e/ou chamadas de ferramenta."""

    texto: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)


def _formatar_reais(valor: int) -> str:
    """1200 -> '1.200' (separador de milhar no estilo brasileiro)."""
    return f"{valor:,}".replace(",", ".")


def _valores_prompt() -> dict[str, str]:
    """Valores de negócio injetados no prompt (editáveis no painel da Thainá)."""
    v = config_negocio.valores()
    preco_terapia = v["preco_terapia_mensal"]
    return {
        "{{PRECO_TERAPIA}}": _formatar_reais(preco_terapia),
        "{{PRECO_TERAPIA_SESSAO}}": _formatar_reais(round(preco_terapia / 4)),
        "{{PRECO_NEURO}}": _formatar_reais(v["preco_neuro"]),
        "{{PARCELAS_MAX}}": str(v["parcelas_max"]),
    }


@lru_cache(maxsize=1)
def _ler_template() -> str:
    """Lê o arquivo do prompt (cacheado; o conteúdo do arquivo não muda em runtime)."""
    return PROMPT_PATH.read_text(encoding="utf-8").strip()


def carregar_system_prompt() -> str:
    """System prompt com os valores de negócio atuais injetados.

    Não é cacheado no nível final de propósito: os valores podem mudar em runtime
    (painel) e a substituição é barata. O arquivo em si fica cacheado.
    """
    texto = _ler_template()
    for token, valor in _valores_prompt().items():
        texto = texto.replace(token, valor)
    return texto


class LLMClient(ABC):
    """Interface de geração de resposta. Implementação atual: OpenAIClient."""

    @abstractmethod
    async def gerar_resposta(
        self, historico: list[dict], tools: list[dict] | None = None
    ) -> LLMResposta:
        """Gera o próximo turno da Sofia.

        Args:
            historico: mensagens em ordem cronológica no formato da API
                (role/content e, no round-trip pós-tool, também mensagens
                'assistant' com tool_calls e 'tool' com resultados). O system
                prompt é adicionado pela implementação.
            tools: schemas de ferramentas (function calling). Se None, o modelo
                só pode responder em texto.

        Returns:
            LLMResposta com texto e/ou tool_calls.

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

    async def gerar_resposta(
        self, historico: list[dict], tools: list[dict] | None = None
    ) -> LLMResposta:
        mensagens = [
            {"role": "system", "content": carregar_system_prompt()},
            *historico,
        ]
        kwargs: dict = {
            "model": self._model,
            "messages": mensagens,
            "temperature": self._temperature,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        try:
            resposta = await self._client.chat.completions.create(**kwargs)
        except OpenAIError as exc:
            logger.error(f"OpenAI falhou ao gerar resposta: {exc}")
            raise LLMError("Falha ao gerar resposta no OpenAI") from exc

        msg = resposta.choices[0].message
        texto = (msg.content or "").strip() or None

        tool_calls: list[ToolCall] = []
        for tc in msg.tool_calls or []:
            try:
                argumentos = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                logger.error(f"Argumentos inválidos na tool {tc.function.name}")
                argumentos = {}
            tool_calls.append(ToolCall(id=tc.id, name=tc.function.name, arguments=argumentos))

        if texto is None and not tool_calls:
            raise LLMError("OpenAI retornou resposta vazia")
        return LLMResposta(texto=texto, tool_calls=tool_calls)


@lru_cache(maxsize=1)
def get_llm_client() -> LLMClient:
    """Retorna o cliente LLM padrão (singleton). Ponto único de troca/mocking."""
    return OpenAIClient()
