"""Tela de pagamentos do painel (Stripe) + páginas públicas de retorno.

Três partes na mesma página (`?aba=`): gerar link avulso/parcelado (neuro),
assinatura mensal da terapia (dia 10) e a listagem das assinaturas ao vivo.
A regra de negócio mora em `services/pagamentos.py`; aqui é só orquestração.

As páginas /pagamento-sucesso e /pagamento-cancelado são PÚBLICAS (o paciente
cai nelas ao voltar do checkout) e não confirmam pagamento — são cortesia
visual; a verdade é sempre a API do Stripe.
"""

import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db, requer_login_pagina, verificar_origem

# Reusa o ambiente Jinja do painel (filtros `data`, `desde` etc. já registrados).
from app.routers.painel import templates
from app.services import config_negocio, pagamentos, painel, stripe_client

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/painel/pagamentos",
    tags=["pagamentos"],
    dependencies=[Depends(requer_login_pagina), Depends(verificar_origem)],
)

# Páginas de retorno do checkout: públicas de propósito (o paciente não tem login).
publico = APIRouter(tags=["pagamentos"])

ABAS = ("gerar", "terapia", "assinaturas")


async def _contexto(
    request: Request,
    db: AsyncSession,
    aba: str,
    *,
    status: str = "all",
    tipo: str = "all",
    **extras,
) -> dict:
    ctx = {
        "request": request,
        "aba": aba if aba in ABAS else "gerar",
        "configurado": stripe_client.configurado(),
        "pacientes": await painel.opcoes_de_pacientes(db),
        "preco_terapia": config_negocio.valor("preco_terapia_mensal"),
        "status": status,
        "tipo": tipo,
        "limites": {
            "valor_min": pagamentos.VALOR_MIN,
            "valor_max": pagamentos.VALOR_MAX,
            "parcelas_max": pagamentos.PARCELAS_MAX,
            "desconto_max": pagamentos.DESCONTO_MAX,
            "terapia_min": pagamentos.TERAPIA_MIN,
            "terapia_max": pagamentos.TERAPIA_MAX,
        },
        "erro": None,
        "resultado": None,
        "assinaturas": None,
        "form": {},
        **extras,
    }
    if ctx["aba"] == "assinaturas" and ctx["configurado"]:
        try:
            ctx["assinaturas"] = await pagamentos.listar_assinaturas_painel(status, tipo)
        except pagamentos.StripeError:
            ctx["erro"] = "Não consegui falar com o Stripe agora. Tenta de novo daqui a pouco."
    return ctx


@router.get("/")
async def pagina_pagamentos(
    request: Request,
    aba: str = "gerar",
    status: str = "all",
    tipo: str = "all",
    conversa_id: int | None = None,
    nome: str = "",
    db: AsyncSession = Depends(get_db),
):
    """`conversa_id`/`nome` pré-preenchem o form (link "Gerar link" na conversa)."""
    ctx = await _contexto(request, db, aba, status=status, tipo=tipo)
    ctx["form"] = {"conversa_id": conversa_id, "nome": nome}
    return templates.TemplateResponse("painel_pagamentos.html", ctx)


async def _vincular(db: AsyncSession, conversa_id: int | None, ref: str) -> None:
    """Link criado já sai amarrado ao paciente escolhido (se escolhido)."""
    if not conversa_id:
        return
    conversa = await painel.obter_conversa(db, conversa_id)
    if conversa is None:
        return
    conversa.stripe_ref = ref
    await db.commit()


@router.post("/criar-link")
async def criar_link(
    request: Request,
    nome: str = Form(""),
    email: str = Form(""),
    valor_total: float = Form(0),
    parcelas: int = Form(1),
    desconto: int = Form(0),
    conversa_id: int | None = Form(None),
    db: AsyncSession = Depends(get_db),
):
    form = {
        "nome": nome,
        "email": email,
        "valor_total": valor_total,
        "parcelas": parcelas,
        "desconto": desconto,
        "conversa_id": conversa_id,
    }
    try:
        resultado = await pagamentos.criar_link_neuro(nome, email, valor_total, parcelas, desconto)
        await _vincular(db, conversa_id, resultado["ref"])
        ctx = await _contexto(request, db, "gerar", resultado=resultado)
    except pagamentos.ErroValidacao as exc:
        ctx = await _contexto(request, db, "gerar", erro=str(exc), form=form)
    except pagamentos.StripeError:
        ctx = await _contexto(
            request, db, "gerar", erro="Erro ao criar o link no Stripe. Tenta de novo.", form=form
        )
    return templates.TemplateResponse("painel_pagamentos.html", ctx)


@router.post("/assinatura")
async def criar_assinatura(
    request: Request,
    nome: str = Form(""),
    email: str = Form(""),
    valor_mensal: float = Form(0),
    conversa_id: int | None = Form(None),
    db: AsyncSession = Depends(get_db),
):
    form = {"nome": nome, "email": email, "valor_mensal": valor_mensal, "conversa_id": conversa_id}
    try:
        resultado = await pagamentos.criar_assinatura_terapia(nome, email, valor_mensal)
        await _vincular(db, conversa_id, resultado["ref"])
        ctx = await _contexto(request, db, "terapia", resultado=resultado)
    except pagamentos.ErroValidacao as exc:
        ctx = await _contexto(request, db, "terapia", erro=str(exc), form=form)
    except pagamentos.StripeError:
        ctx = await _contexto(
            request,
            db,
            "terapia",
            erro="Erro ao criar a assinatura no Stripe. Tenta de novo.",
            form=form,
        )
    return templates.TemplateResponse("painel_pagamentos.html", ctx)


# ── Vínculo manual na página da conversa ──────────────────────────────────────


@router.post("/vincular/{conversa_id}")
async def vincular_conversa(
    conversa_id: int, ref: str = Form(""), db: AsyncSession = Depends(get_db)
):
    """Salva (ou limpa, se vazio) a referência Stripe colada pela Thainá."""
    conversa = await painel.obter_conversa(db, conversa_id)
    if conversa is None:
        raise HTTPException(status_code=404, detail="Conversa não encontrada")
    ref = ref.strip()
    if not ref:
        conversa.stripe_ref = None
        await db.commit()
        return RedirectResponse(f"/painel/conversas/{conversa_id}/", status_code=303)
    try:
        pagamentos.interpretar_referencia(ref)
    except pagamentos.ErroValidacao:
        return RedirectResponse(
            f"/painel/conversas/{conversa_id}/?pagamento=invalido", status_code=303
        )
    conversa.stripe_ref = ref
    await db.commit()
    return RedirectResponse(f"/painel/conversas/{conversa_id}/", status_code=303)


# ── Páginas públicas de retorno do checkout ───────────────────────────────────


@publico.get("/pagamento-sucesso", include_in_schema=False)
async def pagamento_sucesso(request: Request):
    return templates.TemplateResponse(
        "pagamento_retorno.html",
        {
            "request": request,
            "titulo": "Pagamento efetuado!",
            "mensagem": "Você vai receber a confirmação por e-mail em alguns minutos.",
            "ok": True,
        },
    )


@publico.get("/pagamento-cancelado", include_in_schema=False)
async def pagamento_cancelado(request: Request):
    return templates.TemplateResponse(
        "pagamento_retorno.html",
        {
            "request": request,
            "titulo": "Pagamento cancelado",
            "mensagem": "Nenhum valor foi cobrado. Se quiser tentar de novo, é só usar o mesmo link.",
            "ok": False,
        },
    )
