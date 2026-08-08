"""Hamilton falso, em processo: o laboratório nunca fala com o Hamilton de verdade.

Por que não mockar com `unittest.mock`: o valor deste falso não é impedir a
chamada de rede, é **guardar o que teria sido enviado**. `vlr_sessao`,
`fk_captacao` e `tipo_pagamento` são o resultado de uma decisão que o modelo
tomou numa conversa — se o falso só devolvesse `{"pk_paciente": 1}`, a gente
perderia justamente a evidência de que a decisão foi errada.

A lista de captações é o **snapshot real da produção** (`fixtures/captacoes.json`),
e ela é fiel de propósito, inclusive no que está quebrado: a produção não devolve
o campo `is_parceria`, então `captacao.e_parceria()` é `False` até para as
prefeituras. Servir uma lista "consertada" aqui faria o laboratório aprovar um
fluxo de parceria que não funciona no ar.
"""

import json
from pathlib import Path
from typing import Any

from app.services import hamilton_client

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def carregar_captacoes() -> list[dict]:
    doc = json.loads((FIXTURES / "captacoes.json").read_text(encoding="utf-8"))
    return doc["captacoes"]


class HamiltonFalso:
    """Implementa só o que o fluxo de acolhimento usa. O resto levanta erro.

    Levantar em vez de devolver vazio é intencional: se uma demanda nova passar
    a chamar outro endpoint, o laboratório tem que quebrar barulhento em vez de
    seguir testando um caminho que não existe.
    """

    def __init__(self, captacoes: list[dict] | None = None) -> None:
        self._captacoes = captacoes if captacoes is not None else carregar_captacoes()
        self._proximo_id = 90001
        # O diário é a evidência que vai pro relatório.
        self.chamadas: list[dict[str, Any]] = []

    def _registrar(self, metodo: str, **kwargs) -> None:
        self.chamadas.append({"metodo": metodo, **kwargs})

    async def listar_captacoes(self) -> list[dict]:
        return list(self._captacoes)

    async def buscar_paciente_por_telefone(self, telefone: str | None) -> list[dict]:
        self._registrar("buscar_paciente_por_telefone", telefone=telefone)
        return []  # laboratório sempre começa com base vazia: todo mundo é novo

    async def criar_paciente(self, dados: dict) -> dict:
        # Guarda o payload REAL que iria pro Hamilton (com vlr_sessao, captação,
        # tipo_pagamento já resolvidos), não os dados crus da conversa.
        payload = hamilton_client.mapear_dados(dados)
        pk = self._proximo_id
        self._proximo_id += 1
        self._registrar("criar_paciente", payload=payload, pk_paciente=pk)
        return {"pk_paciente": pk, **payload}

    async def atualizar_paciente(self, pid: int, payload: dict) -> dict:
        self._registrar("atualizar_paciente", pk_paciente=pid, payload=payload)
        return {"pk_paciente": pid, **payload}

    def __getattr__(self, nome: str):
        async def _nao_implementado(*_a, **_k):
            raise hamilton_client.HamiltonError(
                f"HamiltonFalso não implementa `{nome}`. Se o fluxo passou a "
                f"depender disso, implemente aqui em vez de silenciar."
            )

        return _nao_implementado
