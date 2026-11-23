"""Testes do cliente Hamilton (funções puras de normalização/mapeamento)."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import config_negocio, hamilton_client


class TestNormalizarTelefone:
    def test_remove_ddi_55(self):
        assert hamilton_client.normalizar_telefone("5531999998888") == "31999998888"

    def test_remove_mascara(self):
        assert hamilton_client.normalizar_telefone("(31) 99999-8888") == "31999998888"

    def test_mantem_sem_ddi(self):
        assert hamilton_client.normalizar_telefone("31999998888") == "31999998888"

    def test_none_vira_vazio(self):
        assert hamilton_client.normalizar_telefone(None) == ""


class TestMapearDados:
    def test_mapeia_campos_e_monta_observacao(self):
        dados = {
            "nome_completo": "Maria Silva",
            "telefone_contato": "5531999998888",
            "data_nascimento": "1990-05-15",
            "telefone_apoio": "5531888887777",
            "endereco": "Rua das Flores, 123",
            "cep": "30431-058",
            "motivo_busca": "ansiedade",
            "preferencia_terapeuta": "mulher",
            "horarios_disponiveis": "noites",
            "como_conheceu": "Instagram",
            "observacoes": "queria alguém com experiência em luto",
        }
        payload = hamilton_client.mapear_dados(dados)
        assert payload["nome"] == "Maria Silva"
        assert payload["telefone"] == "31999998888"
        assert payload["dat_nascimento"] == "1990-05-15"
        assert payload["contato_apoio"] == "31888887777"
        assert payload["endereco"] == "Rua das Flores, 123"
        assert "Motivo: ansiedade" in payload["observacao"]
        assert "Preferência: mulher" in payload["observacao"]
        assert "Horários: noites" in payload["observacao"]
        # Campos novos surfados pra Thainá pela observação.
        assert "CEP: 30431-058" in payload["observacao"]
        assert "Origem: Instagram" in payload["observacao"]
        assert "Obs: queria alguém com experiência em luto" in payload["observacao"]
        # Terapia (motivo não-neuro): anota o valor configurado da mensalidade.
        assert "Mensalidade" in payload["observacao"]

    def test_parceria_vai_com_valor_zero_e_tipo_parceria(self):
        """Parceria decide DINHEIRO: a prefeitura custeia, o paciente não deve nada.

        `tipo_pagamento='parceria'` é o que tira a pessoa do dropdown de cobrança
        manual do Hamilton — sem ele ela entra como 'manual' e reaparece na fila
        de quem deve mensalidade.
        """
        payload = hamilton_client.mapear_dados(
            {
                "nome_completo": "Márcia",
                "telefone_contato": "31977776666",
                "is_parceria": True,
                "captacao_id": 13,
            }
        )
        assert payload["vlr_sessao"] == "0.00"
        assert payload["tipo_pagamento"] == "parceria"
        assert payload["fk_captacao"] == 13
        # Paciente de parceria não recebe anotação de mensalidade nenhuma.
        assert "Mensalidade" not in payload.get("observacao", "")

    def test_mensalidade_anotada_mesmo_sem_outros_dados(self, monkeypatch):
        # O preço vem do /painel/config. Fixado aqui: antes o teste dependia de
        # outro ter deixado 200 no cache, e falhava sozinho depois que a
        # mensalidade passou a R$ 250 (17/11).
        monkeypatch.setitem(config_negocio._cache, "preco_terapia_mensal", 250)
        # Sem motivo de neuro, a observação carrega ao menos a mensalidade.
        payload = hamilton_client.mapear_dados(
            {"nome_completo": "João", "telefone_contato": "31977776666"}
        )
        assert payload["nome"] == "João"
        assert payload["telefone"] == "31977776666"
        assert "Mensalidade" in payload["observacao"]
        # A mensalidade acordada vai sempre (quem sabe o valor é quem conversou).
        assert payload["vlr_sessao"] == "250.00"
        assert payload["tipo_pagamento"] == "manual"
        # Nenhum outro campo opcional foi enviado.
        assert set(payload) == {"nome", "telefone", "observacao", "vlr_sessao", "tipo_pagamento"}

    def test_neuro_nao_anota_mensalidade(self):
        payload = hamilton_client.mapear_dados(
            {
                "nome_completo": "Ana",
                "telefone_contato": "31966665555",
                "motivo_busca": "neuroavaliação, suspeita de TDAH",
            }
        )
        assert "Mensalidade" not in payload.get("observacao", "")
        assert "Motivo: neuroavaliação, suspeita de TDAH" in payload["observacao"]


class TestTelefoneInternacional:
    """Brasileiro sem DDI, estrangeiro com + e DDI (o formato do Hamilton)."""

    @pytest.mark.parametrize(
        "digitado,esperado",
        [
            ("+351 912 345 678", "+351912345678"),
            ("00351912345678", "+351912345678"),
            ("+55 (31) 99999-8888", "31999998888"),
            # Sem "+", não vira estrangeiro: um BR com dígito a mais seria Holanda.
            ("319999988881", "319999988881"),
        ],
    )
    def test_normalizar_digitado(self, digitado, esperado):
        assert hamilton_client.normalizar_telefone(digitado) == esperado

    @pytest.mark.parametrize(
        "wa_id,esperado",
        [
            ("5531999998888", "31999998888"),
            ("553199998888", "3199998888"),
            ("351912345678", "+351912345678"),
            ("14155551234", "+14155551234"),  # EUA: o WhatsApp sempre manda DDI
        ],
    )
    def test_numero_do_whatsapp(self, wa_id, esperado):
        assert hamilton_client.telefone_do_whatsapp(wa_id) == esperado

    @pytest.mark.parametrize(
        "telefone,esperado",
        [
            ("31999998888", "5531999998888"),
            ("+351912345678", "351912345678"),
            ("351912345678", None),
            ("", None),
        ],
    )
    def test_para_wa_id(self, telefone, esperado):
        assert hamilton_client.para_wa_id(telefone) == esperado

    def test_valido(self):
        assert hamilton_client.telefone_valido("31999998888")
        assert hamilton_client.telefone_valido("+351912345678")
        assert not hamilton_client.telefone_valido("+5531999998888")
        assert not hamilton_client.telefone_valido("351912345678")

    @pytest.mark.asyncio
    async def test_busca_codifica_o_mais(self):
        cliente = hamilton_client.HamiltonClient.__new__(hamilton_client.HamiltonClient)
        resposta = MagicMock(status_code=200)
        resposta.json.return_value = []
        cliente._request = AsyncMock(return_value=resposta)
        await cliente.buscar_paciente_por_telefone("+351 912 345 678")
        _, kwargs = cliente._request.await_args
        assert kwargs["params"] == {"telefone": "+351912345678"}
