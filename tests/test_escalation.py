"""Testes da escalada: motivos novos + rótulo legível no alerta da Thainá."""

from unittest.mock import AsyncMock, patch

import pytest

from app.models import Conversa
from app.services import escalation, tools


def test_todo_motivo_da_escalada_tem_rotulo():
    for motivo in tools.MOTIVOS_ESCALADA:
        assert motivo in tools.MOTIVO_LABELS, f"motivo sem rótulo: {motivo}"


def test_motivos_novos_disponiveis_pro_modelo():
    # neuro_reuniao e preco entram no enum que o LLM pode escolher.
    assert "neuro_reuniao" in tools.MOTIVOS_ESCALADA
    assert "preco" in tools.MOTIVOS_ESCALADA


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
