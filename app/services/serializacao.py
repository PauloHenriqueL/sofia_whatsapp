"""Serialização e debounce por conversa (Demanda 2).

Garante duas coisas por número de WhatsApp:

1. **Serialização**: no máximo um processamento por vez para uma mesma conversa
   (um `asyncio.Lock` por número). Isso evita chamadas concorrentes ao modelo,
   respostas sobrepostas e corrida na criação da conversa/cadastro.
2. **Debounce (agrupamento)**: quando o paciente manda uma rajada de mensagens,
   a Sofia espera uma janela de silêncio e responde uma vez só. Cada mensagem
   nova reseta o timer, então só a última dispara o processamento.

Assume **1 instância/processo** (Render free): usa locks e tasks em memória. Com
múltiplas instâncias, isso exigiria um lock distribuído (ex.: Redis). A
idempotência por id de mensagem (na camada de persistência) continua sendo a
defesa contra reentrega de webhook — esta camada é adicional, não a substitui.
"""

import asyncio
import logging
from asyncio import sleep as _dormir  # ligado à função real: imune a mocks de asyncio.sleep
from collections import defaultdict
from collections.abc import Awaitable, Callable

from app.utils import mascarar_telefone

logger = logging.getLogger(__name__)

# Lock por número: serializa o processamento de uma mesma conversa.
_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
# Timer de debounce por número (a rajada reseta o timer).
_timers: dict[str, asyncio.Task] = {}


def lock_da_conversa(numero: str) -> asyncio.Lock:
    """Lock que serializa o processamento da conversa desse número."""
    return _locks[numero]


def agendar(numero: str, segundos: float, acao: Callable[[str], Awaitable[None]]) -> None:
    """(Re)agenda o processamento da conversa após `segundos` de silêncio.

    Cada mensagem nova cancela o timer anterior e cria um novo, então só a
    última mensagem da rajada dispara `acao(numero)` — uma única vez.
    """
    antigo = _timers.get(numero)
    if antigo and not antigo.done():
        antigo.cancel()
    _timers[numero] = asyncio.create_task(_disparar(numero, segundos, acao))


async def _disparar(numero: str, segundos: float, acao: Callable[[str], Awaitable[None]]) -> None:
    try:
        await _dormir(segundos)
    except asyncio.CancelledError:
        return  # chegou mensagem nova; o novo timer assume
    try:
        await acao(numero)
    except Exception:
        logger.exception("Falha no processamento agendado de %s", mascarar_telefone(numero))
    finally:
        if _timers.get(numero) is asyncio.current_task():
            _timers.pop(numero, None)


async def aguardar_pendentes() -> None:
    """Aguarda os timers pendentes terminarem. Uso principal: testes."""
    tarefas = [t for t in list(_timers.values()) if not t.done()]
    if tarefas:
        await asyncio.gather(*tarefas, return_exceptions=True)


def limpar() -> None:
    """Cancela timers e zera locks. Uso principal: isolar testes."""
    for t in _timers.values():
        t.cancel()
    _timers.clear()
    _locks.clear()
