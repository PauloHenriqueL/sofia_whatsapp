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
        # O preço da terapia entra no prompt; nenhum placeholder {{...}} pode
        # sobrar sem substituir (data e preços são injetados em runtime).
        # Obs.: o preço da neuro saiu do prompt na v2 (neuro vai direto pra Thainá).
        prompt = llm_client.carregar_system_prompt()
        assert "{{" not in prompt
        assert llm_client._formatar_reais(settings.preco_terapia_mensal) in prompt

    def test_carrega_base_de_conhecimento(self):
        # A base de conhecimento (prompt/sofia-base-conhecimento.md) é anexada ao
        # system prompt. "quinta semana" só aparece na base, não no fluxo.
        prompt = llm_client.carregar_system_prompt()
        assert "Base de conhecimento" in prompt
        assert "quinta semana" in prompt


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

    @pytest.mark.asyncio
    async def test_esforco_vai_como_reasoning_effort(self):
        fake_client = MagicMock()
        fake_client.chat.completions.create = AsyncMock(return_value=_resposta_openai("oi"))
        client = llm_client.OpenAIClient(client=fake_client, esforco="none")

        await client.gerar_resposta([{"role": "user", "content": "oi"}])

        assert fake_client.chat.completions.create.await_args.kwargs["reasoning_effort"] == "none"

    @pytest.mark.asyncio
    async def test_esforco_da_chamada_vence_o_da_instancia(self):
        # É como a extração da pesquisa pensa mais que a conversa, sem instância própria.
        fake_client = MagicMock()
        fake_client.chat.completions.create = AsyncMock(return_value=_resposta_openai("oi"))
        client = llm_client.OpenAIClient(client=fake_client, esforco="none")

        await client.gerar_resposta([{"role": "user", "content": "oi"}], esforco="low")

        assert fake_client.chat.completions.create.await_args.kwargs["reasoning_effort"] == "low"

    @pytest.mark.asyncio
    async def test_esforco_invalido_e_ignorado(self):
        # Erro de digitação no env viraria 400 em TODO turno; melhor cair no padrão.
        fake_client = MagicMock()
        fake_client.chat.completions.create = AsyncMock(return_value=_resposta_openai("oi"))
        client = llm_client.OpenAIClient(client=fake_client, esforco="baixo")

        await client.gerar_resposta([{"role": "user", "content": "oi"}])

        assert "reasoning_effort" not in fake_client.chat.completions.create.await_args.kwargs

    @pytest.mark.asyncio
    async def test_reenvia_sem_reasoning_effort_quando_modelo_nao_conhece(self):
        # Modelo antigo (gpt-4o e afins) não conhece o parâmetro. Trocar de modelo
        # não pode virar conversa derrubada.
        fake_client = MagicMock()
        erro = OpenAIError("Unrecognized request argument supplied: reasoning_effort")
        fake_client.chat.completions.create = AsyncMock(
            side_effect=[erro, _resposta_openai("respondi sem esforço")]
        )
        client = llm_client.OpenAIClient(
            model="gpt-4o-mini", client=fake_client, temperature=None, esforco="none"
        )

        resposta = await client.gerar_resposta([{"role": "user", "content": "oi"}])

        assert resposta.texto == "respondi sem esforço"
        chamadas = fake_client.chat.completions.create.await_args_list
        assert chamadas[0].kwargs.get("reasoning_effort") == "none"
        assert "reasoning_effort" not in chamadas[1].kwargs
        assert client._omitir_esforco is True

    @pytest.mark.asyncio
    async def test_forca_none_quando_o_modelo_exige_com_tools(self):
        """O gpt-5.6 recusa tools com reasoning != none em /v1/chat/completions.

        A mensagem de erro cita `reasoning_effort`, então a regra genérica o
        REMOVERIA — e sem o parâmetro vale o padrão (`medium`), que é justamente
        o que foi recusado. O conserto automático deixaria a Sofia muda.
        """
        fake_client = MagicMock()
        erro = OpenAIError(
            "Error code: 400 - Function tools with reasoning_effort are not supported "
            "for gpt-5.6-terra in /v1/chat/completions. To use function tools, use "
            "/v1/responses or set reasoning_effort to 'none'."
        )
        fake_client.chat.completions.create = AsyncMock(
            side_effect=[erro, _resposta_openai("respondi com none")]
        )
        client = llm_client.OpenAIClient(
            model="gpt-5.6-terra", client=fake_client, temperature=None, esforco="medium"
        )

        resposta = await client.gerar_resposta([{"role": "user", "content": "oi"}])

        assert resposta.texto == "respondi com none"
        chamadas = fake_client.chat.completions.create.await_args_list
        assert chamadas[0].kwargs["reasoning_effort"] == "medium"
        # Forçou 'none' em vez de remover o parâmetro.
        assert chamadas[1].kwargs["reasoning_effort"] == "none"

    @pytest.mark.asyncio
    async def test_nao_insiste_se_ja_estava_em_none(self):
        fake_client = MagicMock()
        erro = OpenAIError(
            "Function tools with reasoning_effort are not supported. "
            "set reasoning_effort to 'none'."
        )
        fake_client.chat.completions.create = AsyncMock(side_effect=erro)
        client = llm_client.OpenAIClient(
            model="gpt-5.6-terra", client=fake_client, temperature=None, esforco="none"
        )

        with pytest.raises(llm_client.LLMError):
            await client.gerar_resposta([{"role": "user", "content": "oi"}])
        assert fake_client.chat.completions.create.await_count == 1

    @pytest.mark.asyncio
    async def test_erro_sem_culpado_nao_vira_retry_infinito(self):
        # O laço do _criar tem teto: erro genérico sobe como LLMError na hora.
        fake_client = MagicMock()
        fake_client.chat.completions.create = AsyncMock(side_effect=OpenAIError("503 upstream"))
        client = llm_client.OpenAIClient(client=fake_client, temperature=0.7, esforco="low")

        with pytest.raises(llm_client.LLMError):
            await client.gerar_resposta([{"role": "user", "content": "oi"}])

        assert fake_client.chat.completions.create.await_count == 1
