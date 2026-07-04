"""Testes da serialização/debounce por conversa (Demanda 2)."""

import pytest

from app.services import serializacao


@pytest.fixture(autouse=True)
def _limpa():
    yield
    serializacao.limpar()


class TestDebounce:
    @pytest.mark.asyncio
    async def test_rajada_dispara_uma_vez_so(self):
        chamadas = []

        async def acao(numero):
            chamadas.append(numero)

        # Três agendamentos seguidos (rajada): cada um reseta o timer, só o
        # último dispara.
        serializacao.agendar("551", 0.02, acao)
        serializacao.agendar("551", 0.02, acao)
        serializacao.agendar("551", 0.02, acao)
        await serializacao.aguardar_pendentes()

        assert chamadas == ["551"]

    @pytest.mark.asyncio
    async def test_conversas_diferentes_disparam_cada_uma(self):
        chamadas = []

        async def acao(numero):
            chamadas.append(numero)

        serializacao.agendar("551", 0.01, acao)
        serializacao.agendar("552", 0.01, acao)
        await serializacao.aguardar_pendentes()

        assert sorted(chamadas) == ["551", "552"]


class TestLock:
    def test_lock_e_por_numero(self):
        a = serializacao.lock_da_conversa("551")
        b = serializacao.lock_da_conversa("551")
        c = serializacao.lock_da_conversa("552")
        assert a is b  # mesmo número -> mesmo lock (serializa)
        assert a is not c  # números diferentes -> locks independentes
