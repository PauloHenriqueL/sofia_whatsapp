"""Testes do cliente LLM (Passo 4)."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from openai import OpenAIError

from app.config import settings
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

    def test_injeta_valores_de_negocio(self):
        # Os valores configuráveis (preço terapia/neuro, parcelas) entram no
        # prompt; nenhum placeholder {{...}} pode sobrar sem substituir.
        prompt = llm_client.carregar_system_prompt()
        assert "{{" not in prompt
        assert llm_client._formatar_reais(settings.preco_neuro) in prompt
        assert llm_client._formatar_reais(settings.preco_terapia_mensal) in prompt


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

    @pytest.mark.asyncio
    async def test_temperature_none_nao_envia(self):
        fake_client = MagicMock()
        fake_client.chat.completions.create = AsyncMock(return_value=_resposta_openai("oi"))
        client = llm_client.OpenAIClient(client=fake_client, temperature=None)

        await client.gerar_resposta([{"role": "user", "content": "oi"}])

        kwargs = fake_client.chat.completions.create.await_args.kwargs
        assert "temperature" not in kwargs

    @pytest.mark.asyncio
    async def test_temperature_configurada_e_enviada(self):
        fake_client = MagicMock()
        fake_client.chat.completions.create = AsyncMock(return_value=_resposta_openai("oi"))
        client = llm_client.OpenAIClient(client=fake_client, temperature=0.3)

        await client.gerar_resposta([{"role": "user", "content": "oi"}])

        kwargs = fake_client.chat.completions.create.await_args.kwargs
        assert kwargs["temperature"] == 0.3

    @pytest.mark.asyncio
    async def test_reenvia_sem_temperature_quando_modelo_rejeita(self):
        # Modelos de raciocínio rejeitam temperature custom; a Sofia reenvia sem ela.
        fake_client = MagicMock()
        erro = OpenAIError(
            "Unsupported value: 'temperature' does not support 0.7 with this model. "
            "Only the default (1) value is supported."
        )
        fake_client.chat.completions.create = AsyncMock(
            side_effect=[erro, _resposta_openai("respondi sem temperature")]
        )
        client = llm_client.OpenAIClient(model="gpt-5.5", client=fake_client, temperature=0.7)

        resposta = await client.gerar_resposta([{"role": "user", "content": "oi"}])

        assert resposta.texto == "respondi sem temperature"
        chamadas = fake_client.chat.completions.create.await_args_list
        assert chamadas[0].kwargs.get("temperature") == 0.7  # 1ª tentativa, com temperature
        assert "temperature" not in chamadas[1].kwargs  # retry, sem temperature
        assert client._omitir_temperature is True  # aprendeu a não enviar mais
