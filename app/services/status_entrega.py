"""Avisos de entrega da Meta (`statuses` do webhook): registra as FALHAS no log.

A Meta aceita o envio na hora (devolve o wamid) e só depois avisa, pelo mesmo
webhook das mensagens recebidas, se entregou ou não. Sem isto, falha de entrega
é invisível: em 05/11/2026 o 1º lembrete real dos terapeutas foi aceito para 10
números e não chegou a nenhum (template reclassificado como MARKETING), e o
único jeito de saber foi alguém reclamar.

Só registra; não muda nada no fluxo nem no banco. LGPD: o aviso de status não
traz conteúdo de mensagem; vai para o log só o código e a descrição do erro, o
id da mensagem, a categoria cobrada e o telefone mascarado.
"""

import logging
from typing import Any

from app.utils import mascarar_telefone

logger = logging.getLogger(__name__)


def extrair_status(payload: dict[str, Any]) -> list[dict[str, Any]]:
    status: list[dict[str, Any]] = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            status.extend(change.get("value", {}).get("statuses", []))
    return status


def registrar_falhas(payload: dict[str, Any]) -> int:
    """Uma linha de ERROR por erro de cada status `failed`. Devolve quantas.

    Nunca levanta: é log de diagnóstico dentro do webhook, que precisa
    responder 200 à Meta de qualquer jeito.
    """
    falhas = 0
    try:
        for st in extrair_status(payload):
            if st.get("status") != "failed":
                continue
            categoria = (st.get("pricing") or {}).get("category") or "?"
            for erro in st.get("errors") or [{}]:
                falhas += 1
                logger.error(
                    "Entrega FALHOU para %s (msg %s, categoria %s): erro %s — %s. %s",
                    mascarar_telefone(st.get("recipient_id")),
                    st.get("id"),
                    categoria,
                    erro.get("code"),
                    erro.get("title") or erro.get("message") or "sem descrição",
                    (erro.get("error_data") or {}).get("details") or "",
                )
    except Exception:  # payload fora do formato: não pode derrubar o webhook
        logger.exception("Não deu para ler os avisos de entrega do webhook")
    return falhas
