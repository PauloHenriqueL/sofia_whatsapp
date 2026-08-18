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
from app.services import captacao, config_negocio, config_prompt

logger = logging.getLogger(__name__)

# O texto dos prompts (fluxo + base de conhecimento) vem do `config_prompt`
# (editável no painel; o arquivo em prompt/ é o padrão). Ver carregar_system_prompt.


class LLMError(Exception):
    """Falha ao gerar resposta no provedor LLM."""


# Valores aceitos pelo `reasoning_effort` dos modelos de raciocínio (gpt-5.x).
# Serve de allowlist: um valor com erro de digitação no env viraria 400 em TODO
# turno, então preferimos ignorá-lo e usar o padrão do modelo.
ESFORCOS_VALIDOS = {"none", "low", "medium", "high", "xhigh", "max"}


def _esforco_valido(valor: str | None) -> str | None:
    """Normaliza o esforço de raciocínio; devolve None se não der pra usar."""
    texto = (valor or "").strip().lower()
    if not texto or texto in ("default", "padrao", "padrão", "off"):
        return None
    if texto not in ESFORCOS_VALIDOS:
        logger.warning("reasoning_effort inválido (%r); usando o padrão do modelo.", valor)
        return None
    return texto


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


def carregar_system_prompt(captacoes: list[dict] | None = None) -> str:
    """System prompt: prompt de fluxo + base de conhecimento, com tokens injetados.

    O texto vem do `config_prompt` (editável pela Thainá no painel; o arquivo em
    `prompt/` é o padrão). Não é cacheado no nível final de propósito: prompt e
    valores podem mudar em runtime, e a substituição é barata.

    `captacoes` é a lista de origens do Hamilton (ver `services.captacao`). Ela
    entra no prompt pra que o modelo escolha um ID real em vez de descrever a
    origem por escrito. Sem ela, o token vira uma instrução pra omitir o campo.
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
    return texto.replace("{{LISTA_CAPTACOES}}", captacao.linhas_para_prompt(captacoes or []))


class LLMClient(ABC):
    """Interface de geração de resposta. Implementação atual: OpenAIClient."""

    @abstractmethod
    async def gerar_resposta(
        self,
        historico: list[dict],
        tools: list[dict] | None = None,
        captacoes: list[dict] | None = None,
        system_prompt: str | None = None,
        esforco: str | None = None,
    ) -> LLMResposta:
        """Gera o próximo turno da Sofia.

        Args:
            historico: mensagens em ordem cronológica no formato da API
                (role/content e, no round-trip pós-tool, também mensagens
                'assistant' com tool_calls e 'tool' com resultados). O system
                prompt é adicionado pela implementação.
            tools: schemas de ferramentas (function calling). Se None, o modelo
                só pode responder em texto.
            captacoes: origens do Hamilton pro modelo escolher no cadastro.
            system_prompt: substitui o prompt padrão da Sofia. Usado pelos fluxos
                que não são a conversa de acolhimento (ex.: conduzir a pesquisa
                de satisfação, extrair as respostas dela).
            esforco: sobrescreve o `reasoning_effort` só nesta chamada. Existe pra
                a extração da pesquisa poder pensar mais que a conversa, que roda
                com o paciente esperando do outro lado.

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
        esforco: str | None = None,
    ) -> None:
        self._model = model or settings.openai_model
        self._client = client or AsyncOpenAI(api_key=settings.openai_api_key)
        # temperature opcional: None = não envia o parâmetro (usa o padrão do modelo).
        self._temperature = temperature
        # Esforço de raciocínio padrão desta instância (None = não envia).
        self._esforco = _esforco_valido(esforco)
        # Viram True se o modelo rejeitar o parâmetro; aí paramos de enviar.
        self._omitir_temperature = False
        self._omitir_esforco = False

    async def gerar_resposta(
        self,
        historico: list[dict],
        tools: list[dict] | None = None,
        captacoes: list[dict] | None = None,
        system_prompt: str | None = None,
        esforco: str | None = None,
    ) -> LLMResposta:
        mensagens = [
            {"role": "system", "content": system_prompt or carregar_system_prompt(captacoes)},
            *historico,
        ]
        kwargs: dict = {"model": self._model, "messages": mensagens}
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        if self._temperature is not None and not self._omitir_temperature:
            kwargs["temperature"] = self._temperature
        # `esforco` da chamada tem precedência sobre o da instância.
        efetivo = _esforco_valido(esforco) or self._esforco
        if efetivo is not None and not self._omitir_esforco:
            kwargs["reasoning_effort"] = efetivo

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

    # Parâmetros que dependem da geração do modelo: o `_criar` os remove e reenvia
    # se o modelo reclamar deles. `temperature` os modelos de raciocínio só aceitam
    # no padrão; `reasoning_effort` os modelos antigos (gpt-4o e afins) não conhecem.
    # Trocar de modelo não pode virar conversa derrubada por causa de um parâmetro.
    _PARAMS_OPCIONAIS = ("temperature", "reasoning_effort")

    def _marcar_omissao(self, param: str) -> None:
        if param == "temperature":
            self._omitir_temperature = True
        elif param == "reasoning_effort":
            self._omitir_esforco = True

    async def _criar(self, kwargs: dict):
        """Chama a API; se o modelo rejeitar um parâmetro opcional, reenvia sem ele.

        Modelos de raciocínio só aceitam a temperature padrão; modelos antigos não
        conhecem `reasoning_effort`. Em vez de derrubar a conversa pro fallback,
        removemos o parâmetro reclamado, reenviamos e **lembramos disso** — as
        próximas chamadas desta instância já saem sem ele.

        O laço tem teto (um reenvio por parâmetro opcional): sem ele, um erro cuja
        mensagem por acaso citasse um parâmetro viraria retry infinito.
        """
        for _ in range(len(self._PARAMS_OPCIONAIS) + 2):
            try:
                return await self._client.chat.completions.create(**kwargs)
            except OpenAIError as exc:
                texto = str(exc).lower()

                # 🔴 Caso especial, e NÃO é detalhe: com function calling, o
                # gpt-5.6 em /v1/chat/completions recusa qualquer esforço que não
                # seja "none":
                #
                #   Function tools with reasoning_effort are not supported for
                #   gpt-5.6-terra in /v1/chat/completions. To use function tools,
                #   use /v1/responses or set reasoning_effort to 'none'.
                #
                # A mensagem cita `reasoning_effort`, então a regra genérica
                # abaixo o REMOVERIA — e sem o parâmetro o modelo usa o padrão
                # (`medium`), que é exatamente o que ele acabou de recusar. O
                # "conserto" automático deixaria a Sofia muda em todo turno.
                # Aqui a gente faz o que o erro pede: força `none`.
                if "reasoning_effort" in texto and "'none'" in texto:
                    if kwargs.get("reasoning_effort") != "none":
                        logger.warning(
                            "Modelo %s exige reasoning_effort='none' com tools; forçando.",
                            self._model,
                        )
                        kwargs["reasoning_effort"] = "none"
                        continue
                    logger.error(f"OpenAI falhou ao gerar resposta: {exc}")
                    raise LLMError("Falha ao gerar resposta no OpenAI") from exc

                culpado = next(
                    (p for p in self._PARAMS_OPCIONAIS if p in kwargs and p in texto), None
                )
                if culpado is None:
                    logger.error(f"OpenAI falhou ao gerar resposta: {exc}")
                    raise LLMError("Falha ao gerar resposta no OpenAI") from exc
                logger.warning(
                    "Modelo %s não aceitou %s=%s; reenviando sem ele.",
                    self._model,
                    culpado,
                    kwargs.get(culpado),
                )
                self._marcar_omissao(culpado)
                kwargs.pop(culpado, None)
        raise LLMError("Falha ao gerar resposta no OpenAI")


@lru_cache(maxsize=1)
def get_llm_client() -> LLMClient:
    """Retorna o cliente LLM padrão (singleton). Ponto único de troca/mocking."""
    return OpenAIClient(
        temperature=settings.openai_temperature,
        esforco=settings.openai_reasoning_effort,
    )
