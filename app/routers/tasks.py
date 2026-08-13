"""Endpoint de tarefas agendadas, disparadas por um cron externo.

Protegido por token (settings.tasks_token), via header X-Tasks-Token ou query
?token=. São três rodadas independentes: follow-up de lead parado (Frente 2),
pesquisas de satisfação (Demanda C) e cobrança da mensalidade (Demanda D).
"""

import hmac
import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.dependencies import get_db
from app.services import cobranca, config_negocio, pagamentos, pesquisa, seguimento, stripe_client

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


@router.post("/pesquisas")
async def disparar_pesquisas(request: Request, db: AsyncSession = Depends(get_db)):
    """Dispara as pesquisas de satisfação (chamado pelo cron externo).

    Uma rodada: aborda quem tem avaliação pendente no Hamilton, manda o lembrete
    de quem está em silêncio e encerra quem passou do prazo. A fila é do
    Hamilton — se ele estiver fora do ar, a rodada não faz nada e tenta de novo
    na próxima (nada se perde).
    """
    if not _token_valido(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    return await pesquisa.rodar_pesquisas(db)


@router.post("/cobrancas")
async def disparar_cobrancas(request: Request, db: AsyncSession = Depends(get_db)):
    """Dispara as cobranças da mensalidade (chamado pelo cron externo).

    Uma rodada: aborda quem já teve a primeira consulta **realizada** e ainda não
    foi cobrado, manda o lembrete único de quem está em silêncio e encerra quem
    passou do prazo. Desligada em `/painel/config` (`cobranca_ativa`), a rodada
    não aborda ninguém — o endpoint continua respondendo 200.

    Pode rodar no mesmo cron das pesquisas; quem está em pesquisa é pulado aqui e
    cobrado por `pesquisa.finalizar` quando ela terminar.
    """
    if not _token_valido(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    return await cobranca.rodar_cobrancas(db)


@router.post("/stripe")
async def disparar_stripe(request: Request, simular: bool = False):
    """Põe o fim da linha nas assinaturas de parcelado (chamado pelo cron externo).

    Endpoint SEPARADO do `/tasks/cobrancas` de propósito: aquele só faz algo com
    `cobranca_ativa` ligada, que hoje está desligada — pendurar isto lá
    significaria que o conserto do parcelado não roda até alguém ligar a cobrança
    automática, que é uma decisão sem relação nenhuma com esta.

    `?simular=1` devolve exatamente o que faria, sem escrever no Stripe.
    """
    if not _token_valido(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    if not stripe_client.configurado():
        return {"desligado": "stripe"}
    if not simular and not config_negocio.valor("limitar_parcelado_ativo"):
        return {"desligado": "limitar_parcelado_ativo"}
    try:
        return await pagamentos.limitar_parcelado(simular=simular)
    except stripe_client.StripeError:
        logger.error("Rodada de limite do parcelado falhou ao falar com o Stripe")
        return JSONResponse({"error": "stripe_indisponivel"}, status_code=503)
