"""Revisão da fala antes do envio (services/revisao.py).

O que precisa ficar garantido:
- o nome errado sai por código, sem depender do modelo;
- o revisor corrige o "ficou registrado" quando nenhuma ferramenta registrou;
- qualquer falha do revisor deixa a fala original sair (a Sofia nunca fica muda);
- desligada no painel, nada muda.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.services import config_negocio, revisao
from app.services.llm_client import LLMError, LLMResposta, ToolCall


def _conversa(**dados):
    return SimpleNamespace(id=106, eh_terapeuta=False, dados_coletados=dados)


@pytest.fixture(autouse=True)
def _limpo():
    revisao.zerar()
    config_negocio._cache["revisar_respostas"] = True
    yield
    revisao.zerar()


def _revisor(resposta: str | Exception):
    cliente = AsyncMock()
    if isinstance(resposta, Exception):
        cliente.gerar_resposta.side_effect = resposta
    else:
        cliente.gerar_resposta.return_value = LLMResposta(texto=resposta, tool_calls=[])
    return patch.object(revisao, "get_revisor", return_value=cliente), cliente


class TestNomeErrado:
    def test_tira_o_nome_de_paciente_usado_como_vocativo(self):
        conversa = _conversa(nome_terapeuta="Amanda Borges de Souza")
        texto = "Desculpa, Maria. Me referi à conversa anterior."
        assert revisao.corrigir_nome(texto, conversa) == "Desculpa. Me referi à conversa anterior."
        assert revisao.correcoes()["NOME_ERRADO"] == 1

    def test_vocativo_no_fim_da_frase(self):
        conversa = _conversa(nome_terapeuta="Amanda")
        assert revisao.corrigir_nome("Obrigada por confirmar, Maria!", conversa) == (
            "Obrigada por confirmar!"
        )

    def test_aposto_com_ponto_final_nao_e_vocativo(self):
        conversa = _conversa(nome_terapeuta="Amanda")
        texto = "Entendi, quem paga é a mãe, Tatiana."
        assert revisao.corrigir_nome(texto, conversa) == texto

    def test_nome_certo_fica(self):
        conversa = _conversa(nome_completo="Amanda Borges de Souza")
        assert revisao.corrigir_nome("Oi, Amanda! Tudo bem?", conversa) == "Oi, Amanda! Tudo bem?"
        assert revisao.corrigir_nome("Obrigada, Souza.", conversa) == "Obrigada, Souza."

    def test_sem_acento_e_sem_caixa(self):
        conversa = _conversa(nome_completo="Tácia Ramos")
        assert revisao.corrigir_nome("Oi, Tacia!", conversa) == "Oi, Tacia!"

    def test_nome_desconhecido_nao_mexe(self):
        # Sem saber o nome, quem julga é o revisor, não a regex.
        assert revisao.corrigir_nome("Oi, Maria!", _conversa()) == "Oi, Maria!"

    def test_terceiro_citado_no_meio_da_frase_nao_e_vocativo(self):
        conversa = _conversa(nome_terapeuta="Amanda")
        texto = "Entendi, a mãe da Maria Alice paga no cartão."
        assert revisao.corrigir_nome(texto, conversa) == texto


class TestInterpretar:
    def test_aprovada(self):
        assert revisao.interpretar('{"aprovada": true}', "oi") == ("oi", [])

    def test_reprovada_com_reescrita(self):
        resp = json.dumps(
            {
                "aprovada": False,
                "problemas": ["ACAO_INEXISTENTE"],
                "reescrita": "Anotei aqui na conversa.",
            }
        )
        assert revisao.interpretar(resp, "Ficou registrado.") == (
            "Anotei aqui na conversa.",
            ["ACAO_INEXISTENTE"],
        )

    @pytest.mark.parametrize(
        "resposta",
        [
            None,
            "não sei",
            "{quebrado",
            '{"aprovada": false}',  # sem reescrita
            '{"aprovada": false, "problemas": ["OUTRA"], "reescrita": "x"}',  # problema fora da lista
            '{"aprovada": false, "problemas": ["REPETICAO"], "reescrita": "   "}',
        ],
    )
    def test_ilegivel_ou_incompleta_vale_a_original(self, resposta):
        assert revisao.interpretar(resposta, "original") == ("original", [])

    def test_reescrita_muito_maior_e_descartada(self):
        resp = json.dumps({"aprovada": False, "problemas": ["REPETICAO"], "reescrita": "x" * 1000})
        assert revisao.interpretar(resp, "curta") == ("curta", [])


class TestRevisar:
    @pytest.mark.asyncio
    async def test_caso_da_amanda(self):
        """23-24/11: a Sofia disse "Ficou registrado assim" e chamou a terapeuta de Maria."""
        conversa = _conversa(nome_terapeuta="Amanda Borges de Souza")
        historico = [
            {"role": "user", "content": "Maria Alice a mãe dela Tatiana paga no cartão de crédito"},
            {"role": "user", "content": "Correto"},
        ]
        corrigida = "Obrigada por confirmar! Não consigo registrar isso por aqui."
        p, cliente = _revisor(
            json.dumps(
                {"aprovada": False, "problemas": ["ACAO_INEXISTENTE"], "reescrita": corrigida}
            )
        )
        with p:
            final = await revisao.revisar(
                conversa, historico, "Obrigada por confirmar, Maria! Ficou registrado assim."
            )

        assert final == corrigida
        pedido = cliente.gerar_resposta.await_args.args[0][0]["content"]
        # O revisor recebe o nome certo, que nada foi feito, e a fala JÁ sem o nome errado.
        assert "NOME DA PESSOA: Amanda Borges de Souza" in pedido
        assert "AÇÕES DESTE TURNO:\nnenhuma" in pedido
        assert "Obrigada por confirmar! Ficou registrado assim." in pedido
        assert revisao.total_corrigidas() == 1  # uma resposta, mesmo com duas correções
        assert revisao.correcoes()["NOME_ERRADO"] == revisao.correcoes()["ACAO_INEXISTENTE"] == 1

    @pytest.mark.asyncio
    async def test_acao_real_vai_para_o_revisor(self):
        tc = ToolCall(id="1", name="cadastrar_paciente", arguments={})
        p, cliente = _revisor('{"aprovada": true}')
        with p:
            final = await revisao.revisar(
                _conversa(), [], "Pronto, cadastrei você!", [(tc, {"status": "cadastrado"})]
            )
        assert final == "Pronto, cadastrei você!"
        assert (
            "- cadastrar_paciente: cadastrado"
            in cliente.gerar_resposta.await_args.args[0][0]["content"]
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("erro", [LLMError("fora"), TimeoutError(), RuntimeError("bug")])
    async def test_falha_do_revisor_deixa_a_fala_sair(self, erro):
        p, _ = _revisor(erro)
        with p:
            assert await revisao.revisar(_conversa(), [], "Oi! Tudo bem?") == "Oi! Tudo bem?"

    @pytest.mark.asyncio
    async def test_desligada_nao_chama_ninguem(self):
        config_negocio._cache["revisar_respostas"] = False
        p, cliente = _revisor('{"aprovada": false}')
        with p:
            assert (
                await revisao.revisar(_conversa(), [], "Ficou registrado.") == "Ficou registrado."
            )
        cliente.gerar_resposta.assert_not_called()

    @pytest.mark.asyncio
    async def test_fala_vazia_passa_direto(self):
        p, cliente = _revisor('{"aprovada": true}')
        with p:
            assert await revisao.revisar(_conversa(), [], None) is None
        cliente.gerar_resposta.assert_not_called()
