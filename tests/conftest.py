"""Trava de rede da suite. **Não** tem fixture compartilhada de propósito.

Cada teste continua subindo o seu próprio SQLite in-memory e mockando o que
precisa (esse desenho não mudou). Este arquivo existe por um motivo só: impedir
que um mock esquecido vire chamada de verdade pra fora.

🔴 O acidente que motivou isto: o teste do link parcelado mockava
`stripe_client.criar_checkout_session`. Quando o serviço passou a chamar
`criar_payment_link`, o mock deixou de cobrir o caminho — e `pytest` criou
**quatro Payment Links na conta LIVE do Stripe**, com a chave real do `.env`.
Nada foi cobrado por sorte: os links nunca saíram da máquina. Com um teste de
neuro parcelado, o mesmo deslize criaria cobrança recorrente de verdade.

O mesmo vale pro Hamilton e pra OpenAI, que também falam httpx: um teste que
"passa" fazendo request real é um teste que depende de rede, de credencial de
produção e de dado de paciente.

Um teste que precise mesmo de rede marca-se com `@pytest.mark.rede`.
"""

import httpx
import pytest


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "rede: permite requisição HTTP real (use com muito critério)"
    )


class RedeBloqueada(httpx.TransportError):
    """Alguém tentou falar com a internet no meio da suite.

    Herda de `TransportError` de propósito: pro código da app isto é
    indistinguível de "o provedor caiu", que é um caminho que todo cliente daqui
    já trata (Hamilton fora → segue sem captação; Stripe fora → a tela avisa).
    Assim a trava bloqueia a chamada real **e** o teste passa a exercitar a
    degradação em vez de estourar num erro que ninguém pega.

    Foi isso que revelou que 13 testes do webhook faziam GET de verdade em
    `/api/v1/captacoes/` — a cada turno, contra o Hamilton configurado no `.env`.
    """


def _erro(request: httpx.Request) -> RedeBloqueada:
    return RedeBloqueada(
        f"Requisição real bloqueada na suite: {request.method} {request.url}.\n"
        "Falta um mock — cheque se o serviço mudou de função (foi assim que quatro "
        "Payment Links foram parar na conta live). Se a chamada for mesmo "
        "necessária, marque o teste com @pytest.mark.rede."
    )


@pytest.fixture(autouse=True)
def _sem_rede(request, monkeypatch):
    if request.node.get_closest_marker("rede"):
        return

    async def _async(self, req, *args, **kwargs):
        raise _erro(req)

    def _sync(self, req, *args, **kwargs):
        raise _erro(req)

    # Trava no TRANSPORTE, não no cliente: o `TestClient` do FastAPI também é
    # httpx, só que sobre `ASGITransport` (roda a app em memória, sem socket).
    # Bloquear `Client.send` derrubaria a suite inteira do painel junto.
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", _async)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", _sync)
