"""Painel web da Thainá (HTML server-rendered + HTMX). HTTP Basic Auth."""

import logging
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db, requer_login_pagina, verificar_origem
from app.services import (
    acompanhamento,
    cadastro,
    config_negocio,
    config_prompt,
    metricas,
    midia,
    painel,
    whatsapp_client,
)

logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/painel",
    tags=["painel"],
    dependencies=[Depends(requer_login_pagina), Depends(verificar_origem)],
)

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


def _fmt_data(valor):
    if not isinstance(valor, datetime):
        return ""
    return valor.strftime("%d/%m %H:%M")


def _ha_quanto_tempo(valor):
    if not isinstance(valor, datetime):
        return ""
    agora = datetime.now(timezone.utc)
    ref = valor if valor.tzinfo else valor.replace(tzinfo=timezone.utc)
    minutos = int((agora - ref).total_seconds() // 60)
    if minutos < 1:
        return "agora"
    if minutos < 60:
        return f"há {minutos} min"
    horas = minutos // 60
    if horas < 24:
        return f"há {horas}h"
    return f"há {horas // 24}d"


def _tamanho_legivel(bytes_: int) -> str:
    """1536 -> '1,5 KB'. Vírgula decimal (pt-BR)."""
    if not isinstance(bytes_, int) or bytes_ < 1024:
        return f"{bytes_ or 0} B"
    if bytes_ < 1024 * 1024:
        return f"{bytes_ / 1024:.1f} KB".replace(".", ",")
    return f"{bytes_ / (1024 * 1024):.1f} MB".replace(".", ",")


templates.env.filters["data"] = _fmt_data
templates.env.filters["desde"] = _ha_quanto_tempo
templates.env.filters["tamanho"] = _tamanho_legivel
templates.env.filters["e_imagem"] = midia.e_imagem
templates.env.filters["nome_anexo"] = midia.nome_para_download


def _contexto_lista(request: Request, conversas, filtro, busca, ordem, dir_) -> dict:
    return {
        "request": request,
        "conversas": conversas,
        "filtro": filtro,
        "busca": busca,
        "ordem": ordem,
        "dir": dir_,
        "ordens": painel.ORDENS,
        "filtros": painel.FILTROS,
    }


@router.get("/")
async def pagina_lista(
    request: Request,
    filtro: str = "todas",
    busca: str = "",
    ordem: str = "atualizada_em",
    dir: str = "desc",
    db: AsyncSession = Depends(get_db),
):
    conversas = await painel.listar_conversas(
        db, filtro=filtro, busca=busca, ordem=ordem, descendente=(dir != "asc")
    )
    return templates.TemplateResponse(
        "painel_lista.html",
        _contexto_lista(request, conversas, filtro, busca, ordem, dir),
    )


@router.get("/config")
async def pagina_config(request: Request, salvo: int = 0):
    return templates.TemplateResponse(
        "painel_config.html",
        {
            "request": request,
            "campos": config_negocio.CAMPOS,
            "valores": config_negocio.valores(),
            "salvo": salvo,
        },
    )


@router.post("/config")
async def salvar_config(request: Request, db: AsyncSession = Depends(get_db)):
    form = await request.form()
    novos: dict[str, object] = {}
    for chave, campo in config_negocio.CAMPOS.items():
        if campo[2] == "bool":
            novos[chave] = chave in form  # checkbox marcado = presente no form
            continue
        try:
            n = int(form.get(chave))
        except (TypeError, ValueError):
            continue
        if n > 0:
            novos[chave] = n
    # Follow-up tem que caber na janela de 24h da Meta.
    if "followup_horas" in novos:
        novos["followup_horas"] = min(int(novos["followup_horas"]), 23)
    await config_negocio.salvar(db, novos)
    return RedirectResponse("/painel/config?salvo=1", status_code=303)


@router.get("/metricas")
async def pagina_metricas(request: Request, db: AsyncSession = Depends(get_db)):
    m = await metricas.calcular_metricas(db)
    return templates.TemplateResponse(
        "painel_metricas.html",
        {"request": request, "m": m},
    )


@router.get("/prompts")
async def pagina_prompts(request: Request, salvo: str = ""):
    prompts = [
        {
            "chave": chave,
            "rotulo": rotulo,
            "vai_pro_bot": vai_pro_bot,
            "texto": config_prompt.texto(chave),
            "customizado": config_prompt.customizado(chave),
        }
        for chave, (rotulo, _caminho, vai_pro_bot) in config_prompt.PROMPTS.items()
    ]
    return templates.TemplateResponse(
        "painel_prompts.html",
        {"request": request, "prompts": prompts, "salvo": salvo},
    )


@router.post("/prompts/{chave}")
async def salvar_prompt(chave: str, texto: str = Form(...), db: AsyncSession = Depends(get_db)):
    if chave in config_prompt.PROMPTS:
        await config_prompt.salvar(db, chave, texto)
    return RedirectResponse(f"/painel/prompts?salvo={chave}", status_code=303)


@router.post("/prompts/{chave}/resetar")
async def resetar_prompt(chave: str, db: AsyncSession = Depends(get_db)):
    if chave in config_prompt.PROMPTS:
        await config_prompt.resetar(db, chave)
    return RedirectResponse(f"/painel/prompts?salvo={chave}", status_code=303)


@router.get("/acompanhamento")
async def pagina_acompanhamento(request: Request, db: AsyncSession = Depends(get_db)):
    dados = await acompanhamento.montar_acompanhamento(db)
    return templates.TemplateResponse(
        "painel_acompanhamento.html",
        {"request": request, **dados},
    )


@router.get("/midia/{midia_id}")
async def baixar_midia(midia_id: int, download: int = 0, db: AsyncSession = Depends(get_db)):
    """Serve o anexo que o paciente mandou. Exige login (é dado de saúde).

    `?download=1` força o "salvar como"; sem isso o navegador exibe inline (a
    miniatura da imagem no chat aponta pra cá).
    """
    registro = await painel.obter_midia(db, midia_id)
    if registro is None:
        raise HTTPException(status_code=404, detail="Anexo não encontrado")
    nome = midia.nome_para_download(registro)
    mime = midia.mime_seguro(registro)
    # O que não é imagem/PDF nunca é exibido inline (evita HTML/script rodando na
    # origem do painel, com a sessão da Thainá).
    inline = not download and mime != "application/octet-stream"
    return Response(
        content=registro.conteudo,
        media_type=mime,
        headers={
            "Content-Disposition": f'{"inline" if inline else "attachment"}; filename="{nome}"',
            # Anexo de paciente não deve ficar em cache compartilhado.
            "Cache-Control": "private, max-age=300",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/conversas/{conversa_id}/cobranca-resolvida")
async def cobranca_resolvida(conversa_id: int, db: AsyncSession = Depends(get_db)):
    conversa = await painel.obter_conversa(db, conversa_id)
    if conversa is None:
        raise HTTPException(status_code=404, detail="Conversa não encontrada")
    await acompanhamento.marcar_cobranca_resolvida(db, conversa)
    return RedirectResponse("/painel/acompanhamento", status_code=303)


@router.get("/fragment/conversas")
async def fragment_conversas(
    request: Request,
    filtro: str = "todas",
    busca: str = "",
    ordem: str = "atualizada_em",
    dir: str = "desc",
    db: AsyncSession = Depends(get_db),
):
    conversas = await painel.listar_conversas(
        db, filtro=filtro, busca=busca, ordem=ordem, descendente=(dir != "asc")
    )
    return templates.TemplateResponse(
        "_conversas_fragment.html",
        _contexto_lista(request, conversas, filtro, busca, ordem, dir),
    )


@router.get("/conversas/{conversa_id}/")
async def pagina_conversa(request: Request, conversa_id: int, db: AsyncSession = Depends(get_db)):
    conversa = await painel.obter_conversa(db, conversa_id)
    if conversa is None:
        raise HTTPException(status_code=404, detail="Conversa não encontrada")
    mensagens = await painel.carregar_mensagens(db, conversa_id)
    return templates.TemplateResponse(
        "painel_conversa.html",
        {
            "request": request,
            "conversa": conversa,
            "mensagens": mensagens,
            "hamilton_url": painel.url_hamilton_paciente(conversa.paciente_hamilton_id),
        },
    )


@router.get("/conversas/{conversa_id}/fragment/mensagens")
async def fragment_mensagens(
    request: Request, conversa_id: int, db: AsyncSession = Depends(get_db)
):
    mensagens = await painel.carregar_mensagens(db, conversa_id)
    return templates.TemplateResponse(
        "_mensagens_fragment.html",
        {"request": request, "mensagens": mensagens},
    )


@router.post("/conversas/{conversa_id}/responder")
async def responder(
    request: Request,
    conversa_id: int,
    texto: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    conversa = await painel.obter_conversa(db, conversa_id)
    if conversa is None:
        raise HTTPException(status_code=404, detail="Conversa não encontrada")
    texto = texto.strip()
    if texto:
        try:
            await painel.responder_como_thaina(db, conversa, texto)
        except whatsapp_client.WhatsAppError:
            logger.error(f"Falha ao enviar resposta da Thainá (conversa {conversa_id})")
    mensagens = await painel.carregar_mensagens(db, conversa_id)
    return templates.TemplateResponse(
        "_mensagens_fragment.html",
        {"request": request, "mensagens": mensagens},
    )


@router.post("/conversas/{conversa_id}/assumir")
async def assumir(conversa_id: int, db: AsyncSession = Depends(get_db)):
    conversa = await painel.obter_conversa(db, conversa_id)
    if conversa is None:
        raise HTTPException(status_code=404, detail="Conversa não encontrada")
    await painel.assumir(db, conversa)
    return RedirectResponse(f"/painel/conversas/{conversa_id}/", status_code=303)


def _destino_seguro(proximo: str, padrao: str) -> str:
    """Só redireciona pra caminho interno (evita open redirect via `?proximo=`).

    `//evil.com` é URL protocolo-relativa: começa com `/` mas sai do site.
    """
    if proximo.startswith("/") and not proximo.startswith("//"):
        return proximo
    return padrao


@router.post("/conversas/{conversa_id}/devolver-bot")
async def devolver_bot(
    conversa_id: int, proximo: str = Form(""), db: AsyncSession = Depends(get_db)
):
    conversa = await painel.obter_conversa(db, conversa_id)
    if conversa is None:
        raise HTTPException(status_code=404, detail="Conversa não encontrada")
    await painel.devolver_ao_bot(db, conversa)
    # `proximo` vem de quando a Thainá sai da conversa e aceita devolver ao bot.
    return RedirectResponse(
        _destino_seguro(proximo, f"/painel/conversas/{conversa_id}/"), status_code=303
    )


@router.post("/conversas/{conversa_id}/cadastrar")
async def cadastrar(conversa_id: int, db: AsyncSession = Depends(get_db)):
    """Tenta (ou re-tenta) cadastrar o paciente no Hamilton com os dados coletados."""
    conversa = await painel.obter_conversa(db, conversa_id)
    if conversa is None:
        raise HTTPException(status_code=404, detail="Conversa não encontrada")
    await cadastro.cadastrar_paciente(db, conversa)
    await db.commit()
    return RedirectResponse(f"/painel/conversas/{conversa_id}/", status_code=303)


@router.post("/conversas/{conversa_id}/reiniciar")
async def reiniciar(conversa_id: int, db: AsyncSession = Depends(get_db)):
    """Apaga a conversa inteira (teste): libera o número pra recomeçar do zero.

    Some com as mensagens e escaladas junto. Volta pra lista, já que a conversa
    deixa de existir.
    """
    conversa = await painel.obter_conversa(db, conversa_id)
    if conversa is None:
        raise HTTPException(status_code=404, detail="Conversa não encontrada")
    await painel.excluir_conversa(db, conversa)
    return RedirectResponse("/painel/", status_code=303)
