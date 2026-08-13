"""Resolução dos links curtos de pagamento. **Rotas públicas.**

Públicas de propósito: quem abre é o paciente, que não tem login. A proteção é o
slug ser aleatório — mesma premissa do próprio link do Stripe, que também é uma
URL não-adivinhável.

Duas portas para o mesmo dado:
- `GET /l/{slug}` — redireciona. Serve pra testar e pro caso de `LINK_CURTO_BASE`
  não estar configurada (aí o link curto aponta pra própria Sofia).
- `GET /api/links/{slug}` — devolve o destino em JSON. É o que o site consome no
  `/p/[codigo]`: assim o paciente vê **um** redirect só, direto pro Stripe, em vez
  de passar visivelmente pelo `onrender.com` no meio do caminho.

Não devolve nada além do destino: nome de paciente, valor e conversa ficam aqui.
"""

import logging

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db
from app.services import links

logger = logging.getLogger(__name__)
router = APIRouter(tags=["links"])


@router.get("/l/{slug}", include_in_schema=False)
async def abrir_link(slug: str, db: AsyncSession = Depends(get_db)):
    destino = await links.resolver(db, slug)
    if destino is None:
        # Slug morto vai pro site, não pra uma tela de erro: quem clicou num link
        # de pagamento que não existe mais precisa de um lugar pra ir.
        return RedirectResponse("https://allos.org.br/", status_code=302)
    # 302, nunca 301: 301 é cacheado pelo navegador pra sempre, e o destino de um
    # slug pode mudar (link regerado, pagamento migrado).
    return RedirectResponse(destino, status_code=302)


@router.get("/api/links/{slug}", include_in_schema=False)
async def resolver_link(slug: str, db: AsyncSession = Depends(get_db)):
    destino = await links.resolver(db, slug)
    if destino is None:
        return JSONResponse({"erro": "nao_encontrado"}, status_code=404)
    return {"destino": destino}
