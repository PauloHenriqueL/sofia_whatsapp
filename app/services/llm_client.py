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
from datetime import datetime
from functools import lru_cache

from openai import AsyncOpenAI, OpenAIError

from app.config import settings
from app.services import config_negocio, config_prompt

logger = logging.getLogger(__name__)

# O texto dos prompts (fluxo + base de conhecimento) vem do `config_prompt`
# (editável no painel; o arquivo em prompt/ é o padrão). Ver carregar_system_prompt.


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
    """Valores injetados no prompt (preços editáveis no painel + data de hoje).

    `{{DATA_HOJE}}` ajuda o modelo a calcular a idade a partir do nascimento (a
    verificação de idade é uma branch de segurança: <12 escala, 12-17 termo).
    Os tokens de neuro (`{{PRECO_NEURO}}`/`{{PARCELAS_MAX}}`) seguem definidos por
    compatibilidade, mas o prompt v2 não os usa (neuro vai direto pra Thainá).
    """
    v = config_negocio.valores()
    preco_terapia = v["preco_terapia_mensal"]
    return {
        "{{PRECO_TERAPIA}}": _formatar_reais(preco_terapia),
        "{{PRECO_TERAPIA_SESSAO}}": _formatar_reais(round(preco_terapia / 4)),
        "{{PRECO_NEURO}}": _formatar_reais(v["preco_neuro"]),
        "{{PARCELAS_MAX}}": str(v["parcelas_max"]),
        "{{DATA_HOJE}}": datetime.now().strftime("%d/%m/%Y"),
    }


def carregar_system_prompt() -> str:
    """System prompt: prompt de fluxo + base de conhecimento, com tokens injetados.

    O texto vem do `config_prompt` (editável pela Thainá no painel; o arquivo em
    `prompt/` é o padrão). Não é cacheado no nível final de propósito: prompt e
    valores podem mudar em runtime, e a substituição é barata.
    """
    texto = config_prompt.texto("prompt_sistema")
    kb = config_prompt.texto("prompt_base")
    if kb:
        texto = (
            f"{texto}\n\n---\n\n"
            "# Base de conhecimento (pra responder dúvidas em linguagem simples)\n\n"
            "Use o conteúdo abaixo pra responder dúvidas (valores, faltas, sigilo, online, "
            "equipe, etc.). Adapte ao contexto, não leia verbatim. Se não houver resposta "
            "aqui, diz que confirma com a Thainá e escala.\n\n"
            f"{kb}"
        )
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
        temperature: float | None = 0.7,
    ) -> None:
        self._model = model or settings.openai_model
        self._client = client or AsyncOpenAI(api_key=settings.openai_api_key)
        # temperature opcional: None = não envia o parâmetro (usa o padrão do modelo).
        self._temperature = temperature
        # Vira True se o modelo rejeitar a temperature; aí paramos de enviar.
        self._omitir_temperature = False

    async def gerar_resposta(
        self, historico: list[dict], tools: list[dict] | None = None
    ) -> LLMResposta:
        mensagens = [
            {"role": "system", "content": carregar_system_prompt()},
            *historico,
        ]
        kwargs: dict = {"model": self._model, "messages": mensagens}
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        if self._temperature is not None and not self._omitir_temperature:
            kwargs["temperature"] = self._temperature

        resposta = await self._criar(kwargs)

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

    async def _criar(self, kwargs: dict):
        """Chama a API; se o modelo rejeitar a temperature, reenvia sem ela.

        Alguns modelos novos (de raciocínio) só aceitam a temperature padrão e
        devolvem erro quando recebem um valor custom. Em vez de derrubar a
        conversa pro fallback, removemos a temperature, reenviamos uma vez e
        lembramos disso (não tenta de novo nas próximas chamadas).
        """
        try:
            return await self._client.chat.completions.create(**kwargs)
        except OpenAIError as exc:
            if "temperature" in kwargs and "temperature" in str(exc).lower():
                logger.warning(
                    "Modelo %s não aceitou temperature=%s; reenviando sem ela.",
                    self._model,
                    kwargs.get("temperature"),
                )
                self._omitir_temperature = True
                kwargs.pop("temperature", None)
                try:
                    return await self._client.chat.completions.create(**kwargs)
                except OpenAIError as exc2:
                    logger.error(f"OpenAI falhou ao gerar resposta: {exc2}")
                    raise LLMError("Falha ao gerar resposta no OpenAI") from exc2
            logger.error(f"OpenAI falhou ao gerar resposta: {exc}")
            raise LLMError("Falha ao gerar resposta no OpenAI") from exc


@lru_cache(maxsize=1)
def get_llm_client() -> LLMClient:
    """Retorna o cliente LLM padrão (singleton). Ponto único de troca/mocking."""
    return OpenAIClient(temperature=settings.openai_temperature)
