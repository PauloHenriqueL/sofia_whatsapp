"""Testes da escalada: motivos novos + rótulo legível no alerta da Thainá."""

from unittest.mock import AsyncMock, patch

import pytest

from app.models import Conversa
from app.services import escalation, tools, whatsapp_client


def test_todo_motivo_da_escalada_tem_rotulo():
    for motivo in tools.MOTIVOS_ESCALADA:
        assert motivo in tools.MOTIVO_LABELS, f"motivo sem rótulo: {motivo}"


def test_motivos_novos_disponiveis_pro_modelo():
    # Motivos que o LLM pode escolher (inclui os roteamentos da v2).
    for motivo in ("neuro_reuniao", "preco", "presencial", "menor_11", "crise"):
        assert motivo in tools.MOTIVOS_ESCALADA


@pytest.mark.asyncio
async def test_alerta_envia_rotulo_legivel_e_nao_o_codigo():
    conversa = Conversa(
        numero_whatsapp="5531999990000",
        dados_coletados={"nome_completo": "Ana"},
    )
    with patch(
        "app.services.escalation.whatsapp_client.enviar_template",
        new_callable=AsyncMock,
    ) as mock_template:
        ok = await escalation.alertar_thaina(conversa, "neuro_reuniao")

    assert ok is True
    parametros = mock_template.await_args.kwargs["parametros"]
    assert parametros == ["Ana", tools.MOTIVO_LABELS["neuro_reuniao"]]


class TestAlertarCadastro:
    """A Thainá precisa saber que entrou paciente, sem abrir o painel."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "status,esperado",
        [
            ("cadastrado", "paciente novo cadastrado no Hamilton (ficha 42)"),
            ("atualizado", "paciente já conhecido voltou; ficha 42 atualizada"),
            ("cadastro_pendente", "CADASTRO FALHOU — precisa cadastrar à mão no Hamilton"),
        ],
    )
    async def test_manda_o_rotulo_certo_por_status(self, status, esperado):
        conversa = Conversa(
            numero_whatsapp="5531999998888", dados_coletados={"nome_completo": "Maria"}
        )
        with patch(
            "app.services.escalation.whatsapp_client.enviar_template", new_callable=AsyncMock
        ) as mock_tpl:
            ok = await escalation.alertar_cadastro(conversa, {"status": status, "paciente_id": 42})
        assert ok is True
        assert mock_tpl.await_args.kwargs["parametros"] == ["Maria", esperado]

    @pytest.mark.asyncio
    async def test_status_desconhecido_nao_manda_nada(self):
        conversa = Conversa(numero_whatsapp="5531999998888")
        with patch(
            "app.services.escalation.whatsapp_client.enviar_template", new_callable=AsyncMock
        ) as mock_tpl:
            assert await escalation.alertar_cadastro(conversa, {"status": "sei_la"}) is False
        mock_tpl.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_sem_nome_usa_o_numero(self):
        conversa = Conversa(numero_whatsapp="5531999998888", dados_coletados={})
        with patch(
            "app.services.escalation.whatsapp_client.enviar_template", new_callable=AsyncMock
        ) as mock_tpl:
            await escalation.alertar_cadastro(conversa, {"status": "cadastrado", "paciente_id": 1})
        assert mock_tpl.await_args.kwargs["parametros"][0] == "5531999998888"

    @pytest.mark.asyncio
    async def test_falha_do_template_nao_derruba_o_cadastro(self):
        """O cadastro já aconteceu; o alerta é conveniência."""
        conversa = Conversa(numero_whatsapp="5531999998888")
        with patch(
            "app.services.escalation.whatsapp_client.enviar_template",
            new_callable=AsyncMock,
            side_effect=whatsapp_client.WhatsAppError("fora do ar"),
        ):
            assert await escalation.alertar_cadastro(conversa, {"status": "cadastrado"}) is False

    @pytest.mark.asyncio
    async def test_nao_loga_o_nome_do_paciente(self, caplog):
        import logging

        conversa = Conversa(
            numero_whatsapp="5531999998888", dados_coletados={"nome_completo": "Joana Prado"}
        )
        with caplog.at_level(logging.ERROR), patch(
            "app.services.escalation.whatsapp_client.enviar_template",
            new_callable=AsyncMock,
            side_effect=whatsapp_client.WhatsAppError("x"),
        ):
            await escalation.alertar_cadastro(conversa, {"status": "cadastrado"})
        assert "Joana" not in " ".join(r.getMessage() for r in caplog.records)
