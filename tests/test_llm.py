"""Testes do cliente LLM (Passo 4)."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from openai import OpenAIError

from app.services import llm_client


def _resposta_openai(conteudo):
    """Monta um objeto no formato da resposta de chat.completions.create."""
    msg = MagicMock()
    msg.content = conteudo
    msg.tool_calls = None
    choice = MagicMock()
    choice.message = msg
    resp = MagicMock()
    resp.choices = [choice]
    return resp


class TestCarregarSystemPrompt:
    def test_carrega_prompt_da_sofia(self):
        # Checagem estável (identidade), não atrelada à redação do tom, que a
        # equipe ajusta com frequência no prompt.
        prompt = llm_client.carregar_system_prompt()
        assert "Sofia" in prompt
        assert "Allos" in prompt


class TestOpenAIClient:
    @pytest.mark.asyncio
    async def test_inclui_system_prompt_e_historico(self):
        fake_client = MagicMock()
        fake_client.chat.completions.create = AsyncMock(
            return_value=_resposta_openai("Oi, sou a Sofia.")
        )
        client = llm_client.OpenAIClient(model="gpt-test", client=fake_client)

        resposta = await client.gerar_resposta([{"role": "user", "content": "oi"}])

        assert resposta.texto == "Oi, sou a Sofia."
        kwargs = fake_client.chat.completions.create.await_args.kwargs
        assert kwargs["model"] == "gpt-test"
        assert kwargs["messages"][0]["role"] == "system"
        assert "Sofia" in kwargs["messages"][0]["content"]
        assert kwargs["messages"][1] == {"role": "user", "content": "oi"}

    @pytest.mark.asyncio
    async def test_resposta_vazia_levanta_llmerror(self):
        fake_client = MagicMock()
        fake_client.chat.completions.create = AsyncMock(return_value=_resposta_openai(""))
        client = llm_client.OpenAIClient(client=fake_client)

        with pytest.raises(llm_client.LLMError):
            await client.gerar_resposta([{"role": "user", "content": "oi"}])

    @pytest.mark.asyncio
    async def test_erro_openai_vira_llmerror(self):
        fake_client = MagicMock()
        fake_client.chat.completions.create = AsyncMock(side_effect=OpenAIError("boom"))
        client = llm_client.OpenAIClient(client=fake_client)

        with pytest.raises(llm_client.LLMError):
            await client.gerar_resposta([{"role": "user", "content": "oi"}])
