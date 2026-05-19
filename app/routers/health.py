"""Health check endpoint"""

import logging

from fastapi import APIRouter

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/health")
async def health():
    """Health check - usado pelo Render e monitoramento"""
    logger.debug("Health check")
    return {"status": "ok", "service": "sofia"}
