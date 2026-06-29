"""Testes do cliente Hamilton (funções puras de normalização/mapeamento)."""

from app.services import hamilton_client


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
        # Terapia (motivo não-neuro): anota o valor configurado da mensalidade.
        assert "Mensalidade" in payload["observacao"]

    def test_mensalidade_anotada_mesmo_sem_outros_dados(self):
        # Sem motivo de neuro, a observação carrega ao menos a mensalidade.
        payload = hamilton_client.mapear_dados(
            {"nome_completo": "João", "telefone_contato": "31977776666"}
        )
        assert payload["nome"] == "João"
        assert payload["telefone"] == "31977776666"
        assert "Mensalidade" in payload["observacao"]
        # Nenhum outro campo opcional foi enviado.
        assert set(payload) == {"nome", "telefone", "observacao"}

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
