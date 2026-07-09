"""Sanitização da fala do bot (P0): o modelo não pode vazar dado interno.

Casos reais de beta fechado (dados fictícios) que motivaram isto:
- o JSON do `cadastrar_paciente` foi enviado pro WhatsApp como fala;
- '...organizo os dados de quem quer começar.@endsection\\nto=final code omitted'.

O risco oposto (cortar fala legítima) é pior, então há bateria de falso positivo.
"""

import logging

import pytest

from app.services import saida


@pytest.fixture(autouse=True)
def _isola_contador():
    """O contador de bloqueios é global (1 instância). Isola entre testes."""
    saida.zerar_bloqueios()
    yield
    saida.zerar_bloqueios()


JSON_CADASTRO = (
    '{"nome_completo":"Amanda Soares Alves","data_nascimento":"2002-05-10",'
    '"endereco":"Praça Cairo, 44, Belo Horizonte","horarios_disponiveis":"quinta às 8h",'
    '"como_conheceu":"Instagram"}'
)


class TestBloqueiaEstruturaDeDados:
    def test_json_puro_nao_sai_nada(self):
        assert saida.limpar(JSON_CADASTRO) == ""

    def test_json_seguido_de_fala_deixa_so_a_fala(self):
        texto = f"{JSON_CADASTRO}\nTe explico sim. A terapia aqui é por chamada de vídeo."
        assert saida.limpar(texto) == "Te explico sim. A terapia aqui é por chamada de vídeo."

    def test_fala_seguida_de_json_deixa_so_a_fala(self):
        texto = f"Já anotei tudo.\n{JSON_CADASTRO}"
        assert saida.limpar(texto) == "Já anotei tudo."

    def test_json_de_escalada(self):
        assert saida.limpar('{"motivo":"crise","contexto":"paciente relatou risco"}') == ""

    def test_lista_json(self):
        assert saida.limpar('["a", "b"]') == ""

    def test_json_multilinha_indentado(self):
        texto = '  {"nome_completo": "X"}  \nOi, tudo bem?'
        assert saida.limpar(texto) == "Oi, tudo bem?"

    def test_json_grudado_na_mesma_linha_da_fala(self):
        # Achado na sondagem: só olhar linha inteira deixava isto passar.
        assert saida.limpar('Anotei. {"nome_completo":"X"}') == "Anotei."


class TestRemoveTokensInternos:
    def test_endsection_e_code_omitted(self):
        texto = "...e organizo os dados de quem quer começar.@endsection\nto=final code  omitted"
        assert saida.limpar(texto) == "...e organizo os dados de quem quer começar."

    def test_cerca_de_codigo(self):
        assert saida.limpar("```\nOi\n```") == "Oi"

    def test_token_de_chat_template(self):
        assert saida.limpar("Oi, tudo bem?<|im_end|>") == "Oi, tudo bem?"

    def test_prefixo_que_nos_injetamos_no_historico(self):
        assert saida.limpar("[Thainá, coordenadora clínica]: Oi, é a Thainá.") == "Oi, é a Thainá."

    def test_aviso_de_sistema(self):
        assert saida.limpar("[Aviso do sistema: retomada]: Oi, é a Sofia.") == "Oi, é a Sofia."


class TestNaoQuebraFalaLegitima:
    """Falso positivo é pior que falso negativo: nada aqui pode ser tocado."""

    def test_fala_normal_passa_intacta(self):
        t = "Oi! Aqui é a Sofia, da Allos.\n\nA gente é uma ONG de Belo Horizonte."
        assert saida.limpar(t) == t

    def test_chave_no_meio_da_frase_nao_e_json(self):
        t = "Ele me disse {isso} ontem, e eu achei estranho."
        assert saida.limpar(t) == t

    def test_lista_de_dados_em_portugues_passa(self):
        t = "Me passa esses dados:\n\n• Nome completo\n• Data de nascimento\n• Endereço"
        assert saida.limpar(t) == t

    def test_palavra_motivo_em_frase_passa(self):
        t = "Qual o motivo: você quer começar agora ou depois?"
        assert saida.limpar(t) == t

    def test_valor_com_chaves_de_moeda_passa(self):
        t = "A mensalidade é R$ 200 e cobre as sessões semanais."
        assert saida.limpar(t) == t

    def test_email_com_to_nao_e_token_interno(self):
        # Achado na sondagem: `\bto=\S*` comia isto.
        t = "Mande um e-mail to=suporte@allos.org.br"
        assert saida.limpar(t) == t

    def test_separador_de_bolhas_preservado(self):
        t = "Primeira bolha.\n\nSegunda bolha."
        assert saida.limpar(t) == t

    def test_texto_vazio_e_none(self):
        assert saida.limpar("") == ""
        assert saida.limpar(None) == ""


class TestObservabilidade:
    def test_loga_warning_sem_o_conteudo(self, caplog):
        with caplog.at_level(logging.WARNING):
            saida.limpar(f"{JSON_CADASTRO}\nOi.")
        registros = " ".join(r.getMessage() for r in caplog.records)
        assert "sanitizada" in registros
        # LGPD: nem o nome da paciente nem o endereço podem aparecer no log.
        assert "Amanda" not in registros
        assert "Cairo" not in registros

    def test_loga_erro_quando_nada_sobra(self, caplog):
        with caplog.at_level(logging.ERROR):
            saida.limpar(JSON_CADASTRO)
        assert any("vazia" in r.message for r in caplog.records)

    def test_contador_sobe_so_quando_bloqueia(self):
        assert saida.bloqueios() == 0
        saida.limpar("Oi, tudo bem?")  # fala legítima não conta
        assert saida.bloqueios() == 0
        saida.limpar(JSON_CADASTRO)
        saida.limpar("Fim.@endsection")
        assert saida.bloqueios() == 2
