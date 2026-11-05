"""Avisos de falha de entrega da Meta vão para o log (sem conteúdo nem telefone)."""

import logging

from app.services import status_entrega


def _payload(*statuses):
    return {"entry": [{"changes": [{"value": {"statuses": list(statuses)}}]}]}


FALHA = {
    "id": "wamid.X1",
    "status": "failed",
    "recipient_id": "5531988553344",
    "pricing": {"category": "marketing"},
    "errors": [{
        "code": 131049,
        "title": "This message was not delivered to maintain healthy ecosystem engagement.",
        "error_data": {"details": "In order to maintain a healthy ecosystem engagement, the message failed to be delivered."},
    }],
}


def test_falha_vai_para_o_log_com_codigo_e_telefone_mascarado(caplog):
    with caplog.at_level(logging.ERROR, logger="app.services.status_entrega"):
        assert status_entrega.registrar_falhas(_payload(FALHA)) == 1
    linha = caplog.records[0].getMessage()
    assert "131049" in linha and "wamid.X1" in linha and "marketing" in linha
    assert "***3344" in linha and "5531988553344" not in linha


def test_entregue_e_lido_nao_geram_log(caplog):
    with caplog.at_level(logging.ERROR, logger="app.services.status_entrega"):
        n = status_entrega.registrar_falhas(_payload(
            {"id": "a", "status": "sent", "recipient_id": "5531900000000"},
            {"id": "b", "status": "delivered", "recipient_id": "5531900000000"},
            {"id": "c", "status": "read", "recipient_id": "5531900000000"},
        ))
    assert n == 0 and not caplog.records


def test_falha_sem_lista_de_erros_ainda_registra(caplog):
    with caplog.at_level(logging.ERROR, logger="app.services.status_entrega"):
        assert status_entrega.registrar_falhas(_payload({"id": "d", "status": "failed"})) == 1
    assert "sem descrição" in caplog.records[0].getMessage()


def test_payload_de_mensagem_e_payload_quebrado_nao_levantam():
    assert status_entrega.registrar_falhas({"entry": [{"changes": [{"value": {"messages": [{}]}}]}]}) == 0
    assert status_entrega.registrar_falhas({"entry": "lixo"}) == 0
