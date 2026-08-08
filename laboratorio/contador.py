"""Contabiliza tokens e chamadas de ferramenta sem tocar em `app/`.

Decisão de desenho (grilling, Q19): o `LLMClient` da Sofia não expõe `usage`, e
adicionar isso a `app/services/llm_client.py` seria instrumentar código que está
no ar por causa de uma ferramenta de teste. Então o laboratório embrulha o
cliente da OpenAI no **próprio processo** e conta ali.

⚠️ **Esta é a costura que quebra.** `envolver_openai` depende do formato
`cliente.chat.completions.create(...)` que o `OpenAIClient` usa hoje. Se aquele
arquivo migrar pra Responses API ou trocar de SDK, é aqui que quebra — e quebra
barulhento, com AttributeError, não em silêncio.

Bônus: como todo turno da Sofia passa por aqui, este é também o lugar onde as
`tool_calls` são registradas. É a evidência de que o modelo *agiu*, e não só
falou que ia agir.
"""

import json

from openai import AsyncOpenAI


class Contador:
    """Acumula uso por modelo e o histórico de tool calls do processo."""

    def __init__(self) -> None:
        self.uso: dict[str, dict[str, int]] = {}
        self.tool_calls: list[dict] = []

    def registrar(self, modelo: str | None, usage) -> None:
        if usage is None:
            return
        alvo = self.uso.setdefault(
            modelo or "?", {"chamadas": 0, "entrada": 0, "entrada_cache": 0, "saida": 0}
        )
        alvo["chamadas"] += 1
        alvo["entrada"] += getattr(usage, "prompt_tokens", 0) or 0
        alvo["saida"] += getattr(usage, "completion_tokens", 0) or 0
        detalhe = getattr(usage, "prompt_tokens_details", None)
        alvo["entrada_cache"] += getattr(detalhe, "cached_tokens", 0) or 0

    def registrar_tools(self, mensagem) -> None:
        for tc in getattr(mensagem, "tool_calls", None) or []:
            try:
                argumentos = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                argumentos = {"_argumentos_invalidos": tc.function.arguments}
            self.tool_calls.append({"nome": tc.function.name, "argumentos": argumentos})

    def resumo(self) -> dict:
        return {
            "por_modelo": self.uso,
            "total_entrada": sum(v["entrada"] for v in self.uso.values()),
            "total_saida": sum(v["saida"] for v in self.uso.values()),
            "total_cacheado": sum(v["entrada_cache"] for v in self.uso.values()),
        }


class _Completions:
    def __init__(self, real, contador: Contador) -> None:
        self._real = real
        self._contador = contador

    async def create(self, **kwargs):
        resp = await self._real.create(**kwargs)
        self._contador.registrar(kwargs.get("model"), getattr(resp, "usage", None))
        try:
            self._contador.registrar_tools(resp.choices[0].message)
        except (IndexError, AttributeError):
            pass
        return resp


class _Chat:
    def __init__(self, real, contador: Contador) -> None:
        self.completions = _Completions(real.chat.completions, contador)


class _ClienteContado:
    """Proxy fino no lugar do `AsyncOpenAI`.

    Proxy em vez de monkeypatch do método: `AsyncCompletions` é objeto do SDK e
    não há garantia de que aceite atribuição de atributo. Proxy funciona em
    qualquer versão e falha claro se a superfície mudar.
    """

    def __init__(self, real: AsyncOpenAI, contador: Contador) -> None:
        self._real = real
        self.chat = _Chat(real, contador)

    def __getattr__(self, nome: str):
        return getattr(self._real, nome)


def envolver_openai(api_key: str, contador: Contador) -> _ClienteContado:
    return _ClienteContado(AsyncOpenAI(api_key=api_key), contador)
