"""Testes da quebra de resposta em bolhas do WhatsApp e da presença humana
(marcar como lida / digitando / ritmo das bolhas)."""

from unittest.mock import AsyncMock, patch

import pytest

from app.services import whatsapp_client
from app.services.whatsapp_client import dividir_em_bolhas, intervalo_digitacao, marcar_como_lida


class TestDividirEmBolhas:
    def test_texto_curto_vira_uma_bolha(self):
        assert dividir_em_bolhas("Oi, tudo bem?") == ["Oi, tudo bem?"]

    def test_paragrafos_viram_bolhas(self):
        texto = "Primeira ideia.\n\nSegunda ideia.\n\nTerceira."
        assert dividir_em_bolhas(texto) == ["Primeira ideia.", "Segunda ideia.", "Terceira."]

    def test_quebra_de_linha_simples_nao_divide(self):
        texto = "linha um\nlinha dois"
        assert dividir_em_bolhas(texto) == ["linha um\nlinha dois"]

    def test_tolera_espacos_e_linhas_em_branco_extras(self):
        texto = "  Bloco um.  \n\n\n   Bloco dois.  "
        assert dividir_em_bolhas(texto) == ["Bloco um.", "Bloco dois."]

    def test_vazio_ou_none_retorna_lista_vazia(self):
        assert dividir_em_bolhas("") == []
        assert dividir_em_bolhas(None) == []
        assert dividir_em_bolhas("   \n\n   ") == []

    def test_reagrupa_excedente_na_ultima_bolha(self):
        texto = "\n\n".join(f"p{i}" for i in range(1, 8))  # 7 parágrafos
        bolhas = dividir_em_bolhas(texto, max_bolhas=5)
        assert len(bolhas) == 5
        assert bolhas[:4] == ["p1", "p2", "p3", "p4"]
        assert bolhas[4] == "p5\n\np6\n\np7"

    def test_exatamente_no_limite_nao_reagrupa(self):
        texto = "\n\n".join(f"p{i}" for i in range(1, 6))  # 5 parágrafos
        assert dividir_em_bolhas(texto, max_bolhas=5) == ["p1", "p2", "p3", "p4", "p5"]

    def test_max_bolhas_padrao(self):
        assert whatsapp_client.MAX_BOLHAS == 5


class TestIntervaloDigitacao:
    def test_texto_curto_usa_pausa_minima(self):
        assert intervalo_digitacao("oi") == whatsapp_client.PAUSA_MIN_S

    def test_texto_longo_limita_na_pausa_maxima(self):
        assert intervalo_digitacao("x" * 1000) == whatsapp_client.PAUSA_MAX_S

    def test_proporcional_ao_tamanho(self):
        # 50 chars / 25 cps = 2s, dentro da faixa [0.8, 4.0].
        assert intervalo_digitacao("x" * 50) == 2.0

    def test_vazio_ou_none(self):
        assert intervalo_digitacao("") == whatsapp_client.PAUSA_MIN_S
        assert intervalo_digitacao(None) == whatsapp_client.PAUSA_MIN_S


class TestMarcarComoLida:
    @pytest.mark.asyncio
    async def test_read_receipt_com_digitacao(self):
        with patch.object(whatsapp_client, "_enviar", new_callable=AsyncMock) as mock_enviar:
            await marcar_como_lida("wamid.1", com_digitacao=True)
        payload = mock_enviar.await_args.args[0]
        assert payload["status"] == "read"
        assert payload["message_id"] == "wamid.1"
        assert payload["typing_indicator"] == {"type": "text"}

    @pytest.mark.asyncio
    async def test_read_receipt_simples(self):
        with patch.object(whatsapp_client, "_enviar", new_callable=AsyncMock) as mock_enviar:
            await marcar_como_lida("wamid.2", com_digitacao=False)
        payload = mock_enviar.await_args.args[0]
        assert payload["status"] == "read"
        assert "typing_indicator" not in payload

    @pytest.mark.asyncio
    async def test_sem_message_id_nao_chama(self):
        with patch.object(whatsapp_client, "_enviar", new_callable=AsyncMock) as mock_enviar:
            await marcar_como_lida(None)
        mock_enviar.assert_not_called()

    @pytest.mark.asyncio
    async def test_fallback_para_read_simples_se_typing_falha(self):
        mock_enviar = AsyncMock(side_effect=[whatsapp_client.WhatsAppError("400"), None])
        with patch.object(whatsapp_client, "_enviar", mock_enviar):
            await marcar_como_lida("wamid.3", com_digitacao=True)
        assert mock_enviar.await_count == 2
        ultimo = mock_enviar.await_args.args[0]
        assert "typing_indicator" not in ultimo

    @pytest.mark.asyncio
    async def test_erro_de_rede_e_engolido(self):
        mock_enviar = AsyncMock(side_effect=whatsapp_client.WhatsAppError("offline"))
        with patch.object(whatsapp_client, "_enviar", mock_enviar):
            await marcar_como_lida("wamid.4", com_digitacao=False)  # não levanta
