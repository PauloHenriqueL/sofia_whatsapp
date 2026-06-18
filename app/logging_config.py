"""Configuração de logging: texto no dev, JSON estruturado na produção.

Ativado em app.main no startup. Em produção (LOG_JSON=true) os logs saem em
JSON, fáceis de indexar/observar (Render captura stdout).
"""

import logging

from pythonjsonlogger import jsonlogger

from app.config import settings


def configurar_logging() -> None:
    nivel = getattr(logging, settings.log_level.upper(), logging.INFO)

    handler = logging.StreamHandler()
    if settings.log_json:
        handler.setFormatter(
            jsonlogger.JsonFormatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        )
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
        )

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(nivel)
