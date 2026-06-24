"""Endpoint de tarefas agendadas, disparadas por um cron externo.

Protegido por token (settings.tasks_token), via header X-Tasks-Token ou query
?token=. Hoje só tem o follow-up de lead parado (Frente 2).
"""

import hmac
import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.dependencies import get_db
from app.services import seguimento

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/tasks", tags=["tasks"])


def _token_valido(request: Request) -> bool:
    """Token configurado e batendo (header ou query). Vazio = sempre nega."""
    if not settings.tasks_token:
        return False
    enviado = request.headers.get("X-Tasks-Token") or request.query_params.get("token") or ""
    return hmac.compare_digest(enviado, settings.tasks_token)


@router.post("/seguimentos")
async def disparar_seguimentos(request: Request, db: AsyncSession = Depends(get_db)):
    """Dispara os follow-ups de leads parados (chamado pelo cron externo)."""
    if not _token_valido(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    enviados = await seguimento.rodar_seguimentos(db)
    return {"enviados": enviados}
