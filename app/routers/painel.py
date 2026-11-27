"""Painel web da Thainá (HTML server-rendered + HTMX). HTTP Basic Auth."""

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db, requer_login_pagina, verificar_origem
from app.services import (
    acompanhamento,
    cadastro,
    config_negocio,
    config_prompt,
    contato,
    hamilton_client,
    llm_client,
)
from app.services import encaminhamento as encaminhamento_service
from app.services import hoje as hoje_service
from app.services import inicio_contato, metricas, midia, painel, programadas, usuarios, whatsapp_client

logger = logging.getLogger(__name__)


async def _pendencias_na_topbar(request: Request, db: AsyncSession = Depends(get_db)):
    """Abastece o contador da aba "Hoje" em qualquer tela do painel.

    Fica numa dependência do router (e não no contexto de cada rota) porque a
    graça do contador é justamente ser visto de outra tela: se ele só existisse
    na home, ninguém saberia que apareceu coisa nova enquanto lê uma conversa.
    """
    lista = await hoje_service.listar_pendencias_seguro(db)
    request.state.pendencias = len(lista)
    request.state.pendencias_lista = lista


router = APIRouter(
    prefix="/painel",
    tags=["painel"],
    dependencies=[
        Depends(requer_login_pagina),
        Depends(verificar_origem),
        Depends(_pendencias_na_topbar),
    ],
)

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


# Horário de Brasília. Offset fixo de propósito: o Brasil não tem horário de
# verão desde 2019, e `zoneinfo` exigiria o pacote `tzdata` no Windows.
_BRASILIA = timezone(timedelta(hours=-3))


def _fmt_data(valor):
    """Data/hora no horário de Brasília.

    O banco guarda em UTC, e até 11/11/2026 o painel mostrava o UTC cru: uma
    mensagem das 19h41 aparecia como 22h41. Valor sem fuso (SQLite) é UTC.
    """
    if not isinstance(valor, datetime):
        return ""
    if valor.tzinfo is None:
        valor = valor.replace(tzinfo=timezone.utc)
    return valor.astimezone(_BRASILIA).strftime("%d/%m %H:%M")


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


def _fmt_data_unix(ts):
    """Timestamp do Stripe (SEGUNDOS, não ms) -> '18/07/2026'. None -> '—'."""
    if not ts:
        return "—"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%d/%m/%Y")


def _fmt_telefone(numero) -> str:
    """5531988887777 -> (31) 98888-7777; de fora do Brasil, + e DDI.

    Recebe o número da conversa (DDI sem "+": "351912345678") ou o do Hamilton
    ("+351912345678"). O que não parecer telefone sai como veio.
    """
    bruto = str(numero or "").strip()
    digitos = "".join(c for c in bruto if c.isdigit())
    if bruto.startswith("+") and not digitos.startswith("55"):
        return f"+{digitos}"
    if not digitos.startswith("55") and 12 <= len(digitos) <= 15:
        return f"+{digitos}"
    if digitos.startswith("55") and len(digitos) in (12, 13):
        digitos = digitos[2:]
    if len(digitos) == 11:
        return f"({digitos[:2]}) {digitos[2:7]}-{digitos[7:]}"
    if len(digitos) == 10:
        return f"({digitos[:2]}) {digitos[2:6]}-{digitos[6:]}"
    return str(numero or "")


templates.env.filters["data"] = _fmt_data
templates.env.filters["telefone"] = _fmt_telefone
templates.env.filters["data_unix"] = _fmt_data_unix
templates.env.filters["desde"] = _ha_quanto_tempo
templates.env.filters["tamanho"] = _tamanho_legivel
templates.env.filters["rotulo_estado"] = painel.rotulo_estado
templates.env.filters["rotulo_motivo"] = painel.rotulo_motivo
templates.env.filters["e_imagem"] = midia.e_imagem
templates.env.filters["nome_anexo"] = midia.nome_para_download


# Cada lista do painel: rota, título e aba (Demanda G — terapeuta tem aba própria).
LISTAS = {
    "conversas": {"url": "/painel/conversas", "titulo": "Conversas", "aba": "conversas"},
    "terapeutas": {"url": "/painel/terapeutas", "titulo": "Terapeutas", "aba": "terapeutas"},
}


def _contexto_lista(
    request: Request, conversas, filtro, busca, ordem, dir_, pagina=1, tem_proxima=False,
    lista: str = "conversas",
) -> dict:
    cfg = LISTAS.get(lista, LISTAS["conversas"])
    return {
        "lista": lista,
        "base_lista": cfg["url"],
        "titulo_lista": cfg["titulo"],
        "request": request,
        "conversas": conversas,
        "pagina": pagina,
        "tem_proxima": tem_proxima,
        "filtro": filtro,
        "busca": busca,
        "ordem": ordem,
        "dir": dir_,
        "filtros": painel.FILTROS,
        "aba_ativa": cfg["aba"],
    }


POR_PAGINA = 50


async def _pagina_de_conversas(db, filtro, busca, ordem, dir_, pagina: int, lista="conversas"):
    """Uma página da lista + se existe próxima.

    Antes o router chamava `listar_conversas` sem `limite` e ficava com o default
    de 50 — **sem paginação em lugar nenhum**. Da 51ª conversa em diante o resto
    simplesmente sumia da tela, sem aviso. Pede-se um a mais que o necessário pra
    saber se há próxima sem uma segunda query de COUNT.
    """
    pagina = max(1, pagina)
    conversas = await painel.listar_conversas(
        db,
        filtro=filtro,
        busca=busca,
        ordem=ordem,
        descendente=(dir_ != "asc"),
        limite=POR_PAGINA + 1,
        offset=(pagina - 1) * POR_PAGINA,
        lista=lista if lista in LISTAS else "conversas",
    )
    tem_proxima = len(conversas) > POR_PAGINA
    return conversas[:POR_PAGINA], tem_proxima


@router.get("/")
async def pagina_hoje(
    request: Request, db: AsyncSession = Depends(get_db), proposta: str = "", aviso: str = "",
):
    """A home do painel: o que precisa de uma pessoa, num lugar só.

    Era a lista de conversas. A lista continua existindo em `/painel/conversas`,
    mas ela responde "quem falou com a Sofia", não "o que eu tenho pra fazer" —
    e essa segunda pergunta não tinha tela nenhuma.
    """
    dados = await hoje_service.montar_hoje(db, pendencias=request.state.pendencias_lista)
    return templates.TemplateResponse(
        "painel_hoje.html",
        {
            "request": request,
            "aba_ativa": "hoje",
            **dados,
            "encaminhar": await encaminhamento_service.fila_para_painel(),
            "proposta_enviada": proposta,
            "aviso_encaminhamento": aviso,
            "ia_bloqueada_desde": llm_client.bloqueada_desde,
        },
    )


# --- Encaminhamento assistido ----------------------------------------------
# A Thainá escolhe o terapeuta aqui; quem calcula os candidatos, guarda o
# estado e segura o horário é o Hamilton (ver services/encaminhamento.py).

_ERROS_RESERVA = {
    "slot_tomado": "Esse horário acabou de ser ocupado. A lista abaixo já está atualizada: escolha outro.",
    "transicao": "Este paciente já tem uma proposta em andamento.",
    "dados": "A data da primeira sessão precisa cair no mesmo dia da semana do horário escolhido.",
}


async def _contexto_encaminhamento(request: Request, enc_id: int, erro: str = "") -> dict | None:
    cliente = hamilton_client.get_hamilton_client()
    item = await encaminhamento_service.item_da_fila(enc_id, cliente)
    if item is None:
        return None
    paciente = item.get("paciente") or {}
    criterios = item.get("criterios") or {}
    abordagens = await encaminhamento_service._mapa_abordagens(cliente)
    resposta = await cliente.candidatos_encaminhamento(paciente.get("id"))
    return {
        "request": request,
        "aba_ativa": "hoje",
        "enc_id": enc_id,
        "paciente": paciente,
        "criterios": criterios,
        "etiquetas": encaminhamento_service.etiquetas(criterios, abordagens),
        "abordagens": sorted(abordagens.items(), key=lambda a: a[1]),
        "recusa": item.get("ultima_recusa"),
        "candidatos": encaminhamento_service.preparar_candidatos(resposta.get("candidatos") or []),
        "dias": list(enumerate(encaminhamento_service.DIAS_LONGOS)),
        "erro": _ERROS_RESERVA.get(erro, ""),
    }


@router.get("/encaminhamentos/{enc_id}")
async def pagina_encaminhamento(request: Request, enc_id: int, erro: str = ""):
    """Escolher o terapeuta de um paciente da fila."""
    try:
        contexto = await _contexto_encaminhamento(request, enc_id, erro)
    except hamilton_client.HamiltonError as exc:
        logger.error("Tela de encaminhamento %s sem Hamilton: %s", enc_id, exc)
        return RedirectResponse("/painel/?aviso=hamilton", status_code=303)
    if contexto is None:
        # Já escolhido (ou o paciente ganhou terapeuta por outra tela).
        return RedirectResponse("/painel/?aviso=fora_da_fila", status_code=303)
    return templates.TemplateResponse("painel_encaminhamento.html", contexto)


@router.post("/encaminhamentos/{enc_id}/candidatos")
async def buscar_candidatos(
    request: Request,
    enc_id: int,
    modalidade: str = Form(""),
    abordagem_id: str = Form(""),
    dia: list[str] = Form([]),
    hora: list[str] = Form([]),
):
    """Refaz a lista com os critérios que a Thainá ajustou (HTMX) e grava o
    ajuste no pedido, para uma recusa futura já reabrir com ele."""
    cliente = hamilton_client.get_hamilton_client()
    try:
        item = await encaminhamento_service.item_da_fila(enc_id, cliente)
        if item is None:
            return Response('<div class="aviso aviso--erro">Este paciente saiu da fila.</div>')
        paciente_id = (item.get("paciente") or {}).get("id")
        criterios = encaminhamento_service.criterios_do_formulario(
            modalidade, abordagem_id, dia, hora, item.get("criterios") or {})
        await cliente.registrar_pedido_encaminhamento(paciente_id, criterios)
        resposta = await cliente.candidatos_encaminhamento(paciente_id, criterios)
    except hamilton_client.HamiltonError as exc:
        logger.error("Busca de candidatos do encaminhamento %s falhou: %s", enc_id, exc)
        return Response('<div class="aviso aviso--erro">Não consegui falar com o Hamilton agora.</div>')
    return templates.TemplateResponse("_candidatos_fragment.html", {
        "request": request,
        "enc_id": enc_id,
        "candidatos": encaminhamento_service.preparar_candidatos(resposta.get("candidatos") or []),
    })


@router.post("/encaminhamentos/{enc_id}/reservar")
async def reservar_encaminhamento(
    enc_id: int,
    db: AsyncSession = Depends(get_db),
    terapeuta_id: int = Form(...),
    dia_semana: int = Form(...),
    hora_inicio: str = Form(...),
    dat_primeira_sessao: str = Form(""),
    alt_terapeuta_id: str = Form(""),
    alt_dia_semana: str = Form(""),
    alt_hora_inicio: str = Form(""),
    alt_dat_primeira_sessao: str = Form(""),
):
    """A Thainá escolheu: o Hamilton segura o horário e a Sofia manda a
    proposta ao paciente. O vínculo só é gravado no "Aceito" do terapeuta.

    Os campos `alt_*` são uma 2ª opção (opcional): o paciente recebe as duas
    propostas e escolhe uma."""
    alternativa = None
    if alt_terapeuta_id.strip():
        if not (alt_terapeuta_id.isdigit() and alt_dia_semana.isdigit() and alt_hora_inicio):
            return RedirectResponse(f"/painel/encaminhamentos/{enc_id}?erro=dados", status_code=303)
        alternativa = {
            "terapeuta_id": int(alt_terapeuta_id),
            "dia_semana": int(alt_dia_semana),
            "hora_inicio": alt_hora_inicio,
            "dat_primeira_sessao": alt_dat_primeira_sessao or None,
        }
    cliente = hamilton_client.get_hamilton_client()
    try:
        status, corpo = await cliente.reservar_encaminhamento(
            enc_id, terapeuta_id, dia_semana, hora_inicio, dat_primeira_sessao or None,
            alternativa=alternativa)
    except hamilton_client.HamiltonError as exc:
        logger.error("Reserva do encaminhamento %s falhou: %s", enc_id, exc)
        return RedirectResponse("/painel/?aviso=hamilton", status_code=303)
    if status != 200:
        codigo = corpo.get("codigo") if status == 409 else "dados"
        return RedirectResponse(f"/painel/encaminhamentos/{enc_id}?erro={codigo}", status_code=303)
    enc = corpo.get("encaminhamento") or {}
    nome = (enc.get("paciente") or {}).get("nome") or "o paciente"
    if not await encaminhamento_service.propor_ao_paciente(db, enc):
        return RedirectResponse("/painel/?aviso=proposta_nao_saiu", status_code=303)
    return RedirectResponse(f"/painel/?proposta={quote(nome)}", status_code=303)


async def _pagina_lista(request, db, lista, filtro, busca, ordem, dir_, feito, pagina):
    """`feito` é o aviso pós-ação (ex.: 'arquivada'), pra ação não sumir em silêncio."""
    conversas, tem_proxima = await _pagina_de_conversas(
        db, filtro, busca, ordem, dir_, pagina, lista
    )
    return templates.TemplateResponse(
        "painel_lista.html",
        {
            **_contexto_lista(
                request, conversas, filtro, busca, ordem, dir_, max(1, pagina), tem_proxima, lista
            ),
            "feito": feito,
        },
    )


@router.get("/conversas")
async def pagina_lista(
    request: Request,
    filtro: str = "todas",
    busca: str = "",
    ordem: str = "atualizada_em",
    dir: str = "desc",
    feito: str = "",
    pagina: int = 1,
    db: AsyncSession = Depends(get_db),
):
    """Todas as conversas: paciente e terapeuta (com etiqueta). A aba Terapeutas
    é o recorte só deles."""
    return await _pagina_lista(request, db, "conversas", filtro, busca, ordem, dir, feito, pagina)


@router.get("/terapeutas")
async def pagina_terapeutas(
    request: Request,
    filtro: str = "todas",
    busca: str = "",
    ordem: str = "atualizada_em",
    dir: str = "desc",
    feito: str = "",
    pagina: int = 1,
    db: AsyncSession = Depends(get_db),
):
    """Conversas de terapeuta: lembretes, respostas e o que a equipe falou (Demanda G)."""
    return await _pagina_lista(request, db, "terapeutas", filtro, busca, ordem, dir, feito, pagina)


@router.get("/config")
async def pagina_config(request: Request, salvo: int = 0):
    return templates.TemplateResponse(
        "painel_config.html",
        {
            "request": request,
            "grupos": config_negocio.GRUPOS,
            "campos_do_grupo": config_negocio.campos_do_grupo,
            "valores": config_negocio.valores(),
            "salvo": salvo,
            "aba_ativa": "config",
        },
    )


@router.post("/config")
async def salvar_config(request: Request, db: AsyncSession = Depends(get_db)):
    form = await request.form()
    novos: dict[str, object] = {}
    for chave, campo in config_negocio.CAMPOS.items():
        if campo.tipo == "bool":
            novos[chave] = chave in form  # checkbox marcado = presente no form
            continue
        if campo.tipo == "texto":
            bruto = form.get(chave)
            if bruto is None:  # campo ausente do form != campo apagado
                continue
            # String vazia É um valor válido: apagar a chave Pix desliga a oferta
            # de Pix. Por isso não há filtro de "vazio" aqui, ao contrário do int.
            novos[chave] = str(bruto).strip()
            continue
        try:
            n = int(form.get(chave))
        except (TypeError, ValueError):
            continue
        # `>= 0`, não `> 0`: quatro campos documentam "0 desliga"
        # (`desconto_maximo_pct` e os três `alerta_nota_*`) e o filtro antigo
        # tornava esses desligamentos INATINGÍVEIS pelo painel — nem forçando o
        # POST. Negativo continua descartado (não significa nada em nenhum campo).
        if n >= 0:
            novos[chave] = n
    # Os dois prazos que falam com o paciente têm que caber na janela de 24h da
    # Meta: passada ela, texto livre não sai e só template resolve.
    for chave_horas in ("followup_horas", "cobranca_lembrete_horas"):
        if chave_horas in novos:
            novos[chave_horas] = min(int(novos[chave_horas]), 23)
    await config_negocio.salvar(db, novos)
    return RedirectResponse("/painel/config?salvo=1", status_code=303)


@router.get("/metricas")
async def pagina_metricas(request: Request, db: AsyncSession = Depends(get_db)):
    m = await metricas.calcular_metricas(db)
    return templates.TemplateResponse(
        "painel_metricas.html",
        {"request": request, "m": m, "aba_ativa": "metricas"},
    )


@router.get("/prompts")
async def pagina_prompts(request: Request, salvo: str = ""):
    prompts = [
        {
            "chave": chave,
            "rotulo": p.rotulo,
            "vai_pro_bot": p.vai_pro_bot,
            "destino": p.destino,
            "texto": config_prompt.texto(chave),
            "customizado": config_prompt.customizado(chave),
        }
        for chave, p in config_prompt.PROMPTS.items()
    ]
    return templates.TemplateResponse(
        "painel_prompts.html",
        {"request": request, "prompts": prompts, "salvo": salvo, "aba_ativa": "config"},
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


@router.post("/prompts/{chave}/previa")
async def previa_documento(chave: str, texto: str = Form(...)):
    """Baixa o documento montado com o texto que está na tela, sem salvar nada.

    Só faz sentido pro contrato (`destino == "documento"`). Serve pra duas coisas
    com o mesmo código: quem edita vê o resultado **antes** de valer, e quem
    revisa o texto lê num `.docx`, não num campo de texto.

    Quem renderiza é o Hamilton — é ele que tem o `python-docx` e é ele que vai
    gerar o documento de verdade. Usar o mesmo caminho aqui é o que garante que a
    prévia mostre o que o paciente vai receber, e não uma aproximação.
    """
    campo = config_prompt.PROMPTS.get(chave)
    if campo is None or campo.destino != "documento":
        raise HTTPException(status_code=404, detail="Não há prévia pra este texto")
    try:
        arquivo = await hamilton_client.get_hamilton_client().previa_contrato(texto)
    except hamilton_client.HamiltonError as exc:
        logger.error("Prévia do contrato falhou: %s", exc)
        raise HTTPException(
            status_code=502, detail="Não consegui montar a prévia agora. Tente de novo."
        ) from exc
    return Response(
        content=arquivo,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={
            "Content-Disposition": 'attachment; filename="contrato-previa.docx"',
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/acompanhamento")
async def pagina_acompanhamento(
    request: Request, feito: str = "", db: AsyncSession = Depends(get_db)
):
    """`feito` é o aviso pós-ação ('resolvido'/'reaberto'), pra não sumir em silêncio."""
    from app.services import pagamentos as pagamentos_service

    dados = await acompanhamento.montar_acompanhamento(db)
    # Status de pagamento (Stripe) na fila de cobrança — tolerante a falha.
    await pagamentos_service.anotar_pagamentos(dados["cobranca"])
    # Fila de alertas de pesquisa: não passa pelo Hamilton (é snapshot local),
    # então aparece mesmo quando a API está fora.
    dados["alertas"] = await acompanhamento.listar_alertas_pesquisa(db)
    return templates.TemplateResponse(
        "painel_acompanhamento.html",
        {
            "request": request,
            "feito": feito,
            "aba_ativa": "acompanhamento",
            "cobranca_automatica": bool(config_negocio.valor("cobranca_ativa")),
            **dados,
        },
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
    """Tira o paciente da fila de cobrança. A conversa continua existindo."""
    conversa = await painel.obter_conversa(db, conversa_id)
    if conversa is None:
        raise HTTPException(status_code=404, detail="Conversa não encontrada")
    await acompanhamento.marcar_cobranca_resolvida(db, conversa)
    return RedirectResponse("/painel/acompanhamento?feito=resolvido", status_code=303)


@router.post("/conversas/{conversa_id}/alerta-resolvido")
async def alerta_resolvido(conversa_id: int, db: AsyncSession = Depends(get_db)):
    """Tira o alerta de pesquisa da fila. A conversa e a avaliação continuam lá."""
    conversa = await painel.obter_conversa(db, conversa_id)
    if conversa is None:
        raise HTTPException(status_code=404, detail="Conversa não encontrada")
    await acompanhamento.marcar_alerta_resolvido(db, conversa)
    return RedirectResponse("/painel/acompanhamento?feito=alerta_resolvido", status_code=303)


@router.post("/conversas/{conversa_id}/alerta-reabrir")
async def alerta_reabrir(conversa_id: int, db: AsyncSession = Depends(get_db)):
    """Devolve o alerta de pesquisa à fila (clique por engano, ou o assunto voltou).

    O serviço `acompanhamento.reabrir_alerta` existia desde a Demanda C mas não
    tinha rota nem botão — era código morto, e a cobrança tinha o par
    resolver/reabrir enquanto o alerta só tinha "resolver".
    """
    conversa = await painel.obter_conversa(db, conversa_id)
    if conversa is None:
        raise HTTPException(status_code=404, detail="Conversa não encontrada")
    await acompanhamento.reabrir_alerta(db, conversa)
    return RedirectResponse(f"/painel/conversas/{conversa_id}/", status_code=303)


@router.post("/conversas/{conversa_id}/cobranca-reabrir")
async def cobranca_reabrir(conversa_id: int, db: AsyncSession = Depends(get_db)):
    """Desfaz o "marcar resolvido" (clique por engano, ou a cobrança voltou)."""
    conversa = await painel.obter_conversa(db, conversa_id)
    if conversa is None:
        raise HTTPException(status_code=404, detail="Conversa não encontrada")
    await acompanhamento.reabrir_cobranca(db, conversa)
    return RedirectResponse("/painel/acompanhamento?feito=reaberto", status_code=303)


@router.get("/fragment/conversas")
async def fragment_conversas(
    request: Request,
    filtro: str = "todas",
    busca: str = "",
    ordem: str = "atualizada_em",
    dir: str = "desc",
    pagina: int = 1,
    lista: str = "conversas",
    db: AsyncSession = Depends(get_db),
):
    """O polling repassa a página (e a lista) junto do filtro/busca/ordem — sem
    isso a tela pularia de volta pra página 1 a cada 15 segundos."""
    conversas, tem_proxima = await _pagina_de_conversas(
        db, filtro, busca, ordem, dir, pagina, lista
    )
    return templates.TemplateResponse(
        "_conversas_fragment.html",
        _contexto_lista(
            request, conversas, filtro, busca, ordem, dir, max(1, pagina), tem_proxima, lista
        ),
    )


@router.get("/conversas/{conversa_id}/")
async def pagina_conversa(
    request: Request,
    conversa_id: int,
    pagamento: str = "",
    contato: str = "",
    de: str = "",
    programada: str = "",
    encaminhar: str = "",
    faltam: str = "",
    db: AsyncSession = Depends(get_db),
):
    """`pagamento=invalido` é o aviso de referência Stripe rejeitada (redirect).
    `encaminhar=`/`faltam=`: resultado do envio manual ao encaminhamento (códigos).
    `de=terapeutas`: aberta pela aba Terapeutas, o "voltar" leva pra lá.
    `programada=`: resultado de programar/cancelar mensagem (código, nunca texto)."""
    from app.services import cobranca as cobranca_service
    from app.services import pagamentos as pagamentos_service
    from app.services import stripe_client

    conversa = await painel.obter_conversa(db, conversa_id)
    if conversa is None:
        raise HTTPException(status_code=404, detail="Conversa não encontrada")
    mensagens = await painel.carregar_mensagens(db, conversa_id)
    janela_ate = await inicio_contato.janela_ate(db, conversa)
    contato_msg = contato

    # Status do pagamento vinculado, resolvido ao vivo no Stripe (tolerante:
    # Stripe fora do ar ou sem chave vira estado "erro"/ausente, nunca 500).
    pagamento_status = None
    if conversa.stripe_ref and stripe_client.configurado():
        pagamento_status = await pagamentos_service.status_da_referencia(conversa.stripe_ref)

    return templates.TemplateResponse(
        "painel_conversa.html",
        {
            "request": request,
            "conversa": conversa,
            "mensagens": mensagens,
            "escaladas": await painel.listar_escaladas(db, conversa_id),
            "de_terapeuta": conversa.eh_terapeuta,
            "voltar_terapeutas": de == "terapeutas",
            "hamilton_url": painel.url_hamilton_paciente(conversa.paciente_hamilton_id),
            "max_anexo": midia.TAMANHO_MAXIMO,
            "stripe_configurado": stripe_client.configurado(),
            "pagamento_status": pagamento_status,
            "pagamento_invalido": pagamento == "invalido",
            # A Sofia conduzindo pesquisa ou cobrança RESPONDE mesmo em modo humano
            # (exceção no portão do webhook). Sem isto na tela, "Assumir controle"
            # promete um silêncio que não acontece.
            "em_pesquisa": conversa.pesquisa_avaliacao_id is not None,
            "em_cobranca": cobranca_service.em_cobranca(conversa),
            "cobranca_label": cobranca_service.STATUS_LABELS.get(conversa.cobranca_status or ""),
            "janela_ate": janela_ate,
            "janela_aberta": inicio_contato.janela_aberta(janela_ate),
            "template_inicio": inicio_contato.template_configurado(),
            "contato_aviso": AVISOS_CONTATO.get(contato_msg),
            "encaminhar_aviso": _aviso_encaminhar(encaminhar, faltam),
            "programadas": [
                _programada_na_tela(p) for p in await programadas.da_conversa(db, conversa)
            ],
            "programada_aviso": _aviso_programada(programada),
            "hoje_brasilia": programadas.em_brasilia(programadas.agora_utc()).date().isoformat(),
        },
    )


def _programada_na_tela(p) -> dict:
    quando = programadas.em_brasilia(p.enviar_em)
    if p.cancelada_em is not None:
        situacao, classe = "Cancelada", ""
    elif p.enviada_em is not None:
        situacao = "Enviada" if p.enviada_como == "texto" else "Enviada como convite"
        classe = "badge-cadastrado"
    elif p.tentativas and p.tentativas >= programadas.TENTATIVAS_MAXIMAS:
        situacao, classe = "Não saiu", "badge-escalado"
    else:
        situacao, classe = "Programada", "badge-humano"
    return {
        "id": p.id,
        "quando": f"{quando:%d/%m/%Y} às {quando:%H:%M}",
        "texto": p.texto,
        "por": "a Sofia" if p.criada_por == programadas.SOFIA else p.criada_por,
        "situacao": situacao,
        "classe": classe,
        "pendente": p.enviada_em is None and p.cancelada_em is None,
        "erro": p.erro if p.enviada_em is None else None,
    }


def _aviso_programada(codigo: str) -> dict | None:
    if codigo == "ok":
        return {"erro": False, "texto": "Mensagem programada. A Sofia manda no dia e hora marcados."}
    if codigo == "cancelada":
        return {"erro": False, "texto": "Mensagem programada cancelada."}
    if codigo in programadas.ERROS:
        return {"erro": True, "texto": programadas.ERROS[codigo]}
    return None


@router.post("/conversas/{conversa_id}/programar")
async def programar_mensagem(
    conversa_id: int,
    request: Request,
    dia: str = Form(""),
    hora: str = Form(""),
    texto: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    """A equipe programa uma mensagem para a Sofia mandar numa data
    (services/programadas.py). Data e hora são de Brasília."""
    from datetime import date as _date
    from datetime import time as _time

    conversa = await painel.obter_conversa(db, conversa_id)
    if conversa is None:
        raise HTTPException(status_code=404, detail="Conversa não encontrada")
    destino = f"/painel/conversas/{conversa_id}/?programada="
    try:
        quando = programadas.de_brasilia(
            _date.fromisoformat(dia), _time.fromisoformat(hora) if hora else None)
    except ValueError:
        return RedirectResponse(destino + "data#programar", status_code=303)
    try:
        await programadas.programar(
            db, conversa, texto, quando, request.session.get("usuario") or "equipe")
    except programadas.ProgramacaoInvalida as exc:
        return RedirectResponse(destino + exc.codigo + "#programar", status_code=303)
    await db.commit()
    return RedirectResponse(destino + "ok#programar", status_code=303)


@router.post("/conversas/{conversa_id}/programadas/{programada_id}/cancelar")
async def cancelar_programada(
    conversa_id: int, programada_id: int, db: AsyncSession = Depends(get_db)
):
    conversa = await painel.obter_conversa(db, conversa_id)
    if conversa is None:
        raise HTTPException(status_code=404, detail="Conversa não encontrada")
    if await programadas.cancelar(db, conversa, programada_id):
        await db.commit()
    return RedirectResponse(
        f"/painel/conversas/{conversa_id}/?programada=cancelada#programar", status_code=303)


# O que a tela da conversa diz depois de iniciar contato (`?contato=`).
AVISOS_CONTATO = {
    "enviado": {"erro": False, "texto": "Convite enviado. Você está com a conversa; "
                "quando a pessoa responder, ela entra na fila Hoje e chega o alerta."},
    "criado": {"erro": False, "texto": "Contato adicionado e convite enviado."},
    "existia": {"erro": False, "texto": "Esse número já tinha conversa: abrimos a dele. "
                "Nada foi enviado ainda."},
    "erro": {"erro": True, "texto": "O convite não saiu (o WhatsApp recusou). Confira o "
             "nome do template em Configurações e tente de novo."},
    "sem_template": {"erro": True, "texto": "Falta o nome do template de início de contato "
                     "em Configurações."},
}


async def _iniciar_e_redirecionar(db, conversa, request: Request, sucesso: str):
    try:
        await inicio_contato.iniciar(db, conversa, request.session.get("usuario") or "")
    except inicio_contato.SemTemplate:
        return RedirectResponse(
            f"/painel/conversas/{conversa.id}/?contato=sem_template", status_code=303
        )
    except whatsapp_client.WhatsAppError as exc:
        logger.error("Convite de início de contato não saiu (conversa %s): %s", conversa.id, exc)
        return RedirectResponse(f"/painel/conversas/{conversa.id}/?contato=erro", status_code=303)
    return RedirectResponse(f"/painel/conversas/{conversa.id}/?contato={sucesso}", status_code=303)


@router.post("/conversas/{conversa_id}/iniciar-contato")
async def iniciar_contato(conversa_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    """Manda o template de início e assume a conversa (services/inicio_contato.py)."""
    conversa = await painel.obter_conversa(db, conversa_id)
    if conversa is None:
        raise HTTPException(status_code=404, detail="Conversa não encontrada")
    return await _iniciar_e_redirecionar(db, conversa, request, "enviado")


@router.get("/contatos/novo")
async def pagina_novo_contato(request: Request, tipo: str = "paciente"):
    return templates.TemplateResponse(
        "painel_contato_novo.html",
        {
            "request": request,
            "tipo": "terapeuta" if tipo == "terapeuta" else "paciente",
            "aba_ativa": "terapeutas" if tipo == "terapeuta" else "conversas",
            "erro": "",
            "form": {},
            "template_inicio": inicio_contato.template_configurado(),
        },
    )


@router.post("/contatos/novo")
async def criar_novo_contato(
    request: Request,
    nome: str = Form(""),
    numero: str = Form(""),
    tipo: str = Form("paciente"),
    nota: str = Form(""),
    consentimento: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    """Número novo: cria a conversa e manda o convite (ou abre a que já existia)."""
    tipo = "terapeuta" if tipo == "terapeuta" else "paciente"
    erro = ""
    if not consentimento:
        erro = "Confirme que a pessoa passou o número para a Allos."
    else:
        try:
            conversa, criada = await inicio_contato.criar_contato(
                db, nome, numero, tipo == "terapeuta", nota, request.session.get("usuario") or ""
            )
        except (inicio_contato.NumeroInvalido, ValueError) as exc:
            erro = str(exc)
    if erro:
        return templates.TemplateResponse(
            "painel_contato_novo.html",
            {
                "request": request,
                "tipo": tipo,
                "aba_ativa": "terapeutas" if tipo == "terapeuta" else "conversas",
                "erro": erro,
                "form": {"nome": nome, "numero": numero, "nota": nota},
                "template_inicio": inicio_contato.template_configurado(),
            },
            status_code=400,
        )
    if not criada:
        return RedirectResponse(f"/painel/conversas/{conversa.id}/?contato=existia", status_code=303)
    return await _iniciar_e_redirecionar(db, conversa, request, "criado")


@router.get("/conversas/{conversa_id}/fragment/mensagens")
async def fragment_mensagens(
    request: Request, conversa_id: int, db: AsyncSession = Depends(get_db)
):
    conversa = await painel.obter_conversa(db, conversa_id)
    mensagens = await painel.carregar_mensagens(db, conversa_id)
    return templates.TemplateResponse(
        "_mensagens_fragment.html",
        {
            "request": request,
            "mensagens": mensagens,
            "de_terapeuta": bool(conversa and conversa.eh_terapeuta),
        },
    )


@router.post("/conversas/{conversa_id}/responder")
async def responder(
    request: Request,
    conversa_id: int,
    texto: str = Form(""),
    responde_a_id: int | None = Form(None),
    anexo: UploadFile | None = File(None),
    db: AsyncSession = Depends(get_db),
):
    """Resposta da Thainá: texto e/ou anexo, opcionalmente citando uma mensagem.

    `responde_a_id` é validado no serviço (tem que ser mensagem desta conversa).
    """
    conversa = await painel.obter_conversa(db, conversa_id)
    if conversa is None:
        raise HTTPException(status_code=404, detail="Conversa não encontrada")

    texto = (texto or "").strip()
    tem_anexo = anexo is not None and bool(anexo.filename)

    try:
        if tem_anexo:
            assert anexo is not None  # garantido por `tem_anexo` (pro type checker)
            # Lê no máximo o teto + 1 byte: assim detectamos "grande demais" sem
            # carregar um arquivo de 500 MB na memória do processo.
            conteudo = await anexo.read(midia.TAMANHO_MAXIMO + 1)
            if len(conteudo) > midia.TAMANHO_MAXIMO:
                raise HTTPException(status_code=413, detail="Arquivo grande demais (máx. 8 MB)")
            await painel.enviar_anexo_como_thaina(
                db,
                conversa,
                conteudo,
                mime=anexo.content_type or "application/octet-stream",
                nome=anexo.filename or "anexo",
                legenda=texto,
                responde_a_id=responde_a_id,
            )
        elif texto:
            await painel.responder_como_thaina(db, conversa, texto, responde_a_id=responde_a_id)
    except whatsapp_client.WhatsAppError:
        logger.error(f"Falha ao enviar resposta da Thainá (conversa {conversa_id})")

    mensagens = await painel.carregar_mensagens(db, conversa_id)
    return templates.TemplateResponse(
        "_mensagens_fragment.html",
        {"request": request, "mensagens": mensagens, "de_terapeuta": conversa.eh_terapeuta},
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


@router.post("/conversas/{conversa_id}/contato")
async def definir_contato(
    conversa_id: int, tipo: str = Form(...), db: AsyncSession = Depends(get_db)
):
    """Marca à mão se a conversa é de terapeuta ou de paciente (services/contato.py)."""
    if tipo not in ("terapeuta", "paciente"):
        raise HTTPException(status_code=400, detail="Tipo de contato inválido")
    conversa = await painel.obter_conversa(db, conversa_id)
    if conversa is None:
        raise HTTPException(status_code=404, detail="Conversa não encontrada")
    await contato.definir(db, conversa, tipo == "terapeuta", contato.MANUAL)
    return RedirectResponse(f"/painel/conversas/{conversa_id}/", status_code=303)


@router.post("/conversas/{conversa_id}/arquivar")
async def arquivar(conversa_id: int, db: AsyncSession = Depends(get_db)):
    """Arquiva a conversa (sai da lista padrão; nada é apagado)."""
    conversa = await painel.obter_conversa(db, conversa_id)
    if conversa is None:
        raise HTTPException(status_code=404, detail="Conversa não encontrada")
    await painel.arquivar(db, conversa)
    lista = "/painel/terapeutas" if conversa.eh_terapeuta else "/painel/conversas"
    return RedirectResponse(f"{lista}?feito=arquivada", status_code=303)


@router.post("/conversas/{conversa_id}/desarquivar")
async def desarquivar(conversa_id: int, db: AsyncSession = Depends(get_db)):
    """Desfaz o arquivamento (clique por engano, ou o caso reabriu)."""
    conversa = await painel.obter_conversa(db, conversa_id)
    if conversa is None:
        raise HTTPException(status_code=404, detail="Conversa não encontrada")
    await painel.desarquivar(db, conversa)
    return RedirectResponse(f"/painel/conversas/{conversa_id}/", status_code=303)


@router.post("/conversas/{conversa_id}/cadastrar")
async def cadastrar(conversa_id: int, db: AsyncSession = Depends(get_db)):
    """Tenta (ou re-tenta) cadastrar o paciente no Hamilton com os dados coletados."""
    conversa = await painel.obter_conversa(db, conversa_id)
    if conversa is None:
        raise HTTPException(status_code=404, detail="Conversa não encontrada")
    await cadastro.cadastrar_paciente(db, conversa)
    await db.commit()
    return RedirectResponse(f"/painel/conversas/{conversa_id}/", status_code=303)


# Resultado do "Enviar para o encaminhamento" (services/encaminhamento.enviar_manual).
# O redirect leva só o código; o texto mora aqui.
AVISOS_ENCAMINHAR = {
    "enviado": (False, "Enviado para o encaminhamento. A pessoa está na fila da aba Hoje, "
                "e a Sofia voltou a responder nesta conversa."),
    "em_andamento": (False, "Esta pessoa já tem uma proposta de horário em andamento. Nada mudou."),
    "faltam_dados": (True, "Não deu para cadastrar: falta {faltam}. Peça à pessoa e tente de novo."),
    "tem_terapeuta": (True, "O Hamilton diz que esta pessoa já tem terapeuta, então ela não "
                      "entra na fila. Para trocar de terapeuta, faça a mudança no Hamilton."),
    "sem_ficha": (True, "O Hamilton não achou a ficha desta pessoa. Confira o cadastro lá."),
    "desligado": (True, "O encaminhamento está desligado no Hamilton. Ligue lá antes de enviar."),
    "hamilton": (True, "Não consegui falar com o Hamilton agora. Tente de novo em alguns minutos."),
    "terapeuta": (True, "Esta conversa é de terapeuta, não de paciente."),
}
_FALTAM_NO_AVISO = {"nome": "o nome completo", "nascimento": "a data de nascimento"}


def _aviso_encaminhar(codigo: str, faltam: str) -> dict | None:
    aviso = AVISOS_ENCAMINHAR.get(codigo)
    if aviso is None:
        return None
    erro, texto = aviso
    # `faltam` vem da URL: só entram os rótulos conhecidos, nunca o que foi digitado.
    itens = [_FALTAM_NO_AVISO[c] for c in faltam.split(",") if c in _FALTAM_NO_AVISO]
    return {"erro": erro, "texto": texto.format(faltam=" e ".join(itens) or "um dado do cadastro")}


@router.post("/conversas/{conversa_id}/encaminhar")
async def encaminhar(conversa_id: int, db: AsyncSession = Depends(get_db)):
    """Põe (ou repõe) o paciente na fila do encaminhamento e devolve a conversa à Sofia."""
    conversa = await painel.obter_conversa(db, conversa_id)
    if conversa is None:
        raise HTTPException(status_code=404, detail="Conversa não encontrada")
    resultado = await encaminhamento_service.enviar_manual(db, conversa)
    destino = f"/painel/conversas/{conversa_id}/?encaminhar={resultado['codigo']}"
    if resultado.get("faltam"):
        chaves = {v: k for k, v in _FALTAM_NO_AVISO.items()}
        destino += "&faltam=" + ",".join(chaves[r] for r in resultado["faltam"] if r in chaves)
    return RedirectResponse(destino, status_code=303)


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
    return RedirectResponse("/painel/conversas", status_code=303)


@router.get("/usuarios")
async def pagina_usuarios(request: Request, db: AsyncSession = Depends(get_db), salvo: int = 0):
    return templates.TemplateResponse(
        "painel_usuarios.html",
        {
            "request": request,
            "usuarios": await usuarios.listar(db),
            "salvo": salvo,
            "aba_ativa": "usuarios",
        },
    )


@router.post("/usuarios")
async def criar_usuario(request: Request, db: AsyncSession = Depends(get_db)):
    form = await request.form()
    nome = str(form.get("nome") or "").strip()
    username = str(form.get("username") or "").strip()
    senha = str(form.get("senha") or "")
    telefone = str(form.get("telefone_whatsapp") or "").strip()
    recebe_alertas = "recebe_alertas" in form
    if not nome or not username or not senha:
        return templates.TemplateResponse(
            "painel_usuarios.html",
            {
                "request": request,
                "usuarios": await usuarios.listar(db),
                "erro": "Nome, usuário e senha são obrigatórios.",
                "aba_ativa": "usuarios",
            },
            status_code=400,
        )
    ja_existe = any(u.username == username for u in await usuarios.listar(db))
    if ja_existe:
        return templates.TemplateResponse(
            "painel_usuarios.html",
            {
                "request": request,
                "usuarios": await usuarios.listar(db),
                "erro": f"Já existe um usuário com o login '{username}'.",
                "aba_ativa": "usuarios",
            },
            status_code=400,
        )
    await usuarios.criar(
        db,
        nome=nome,
        username=username,
        senha=senha,
        telefone_whatsapp=telefone,
        recebe_alertas=recebe_alertas,
    )
    await db.commit()
    return RedirectResponse("/painel/usuarios?salvo=1", status_code=303)


@router.get("/usuarios/{usuario_id}/editar")
async def pagina_editar_usuario(
    usuario_id: int, request: Request, db: AsyncSession = Depends(get_db)
):
    usuario = await usuarios.obter(db, usuario_id)
    if usuario is None:
        raise HTTPException(status_code=404, detail="Usuário não encontrado")
    return templates.TemplateResponse(
        "painel_usuario_editar.html",
        {"request": request, "usuario": usuario, "aba_ativa": "usuarios"},
    )


@router.post("/usuarios/{usuario_id}/editar")
async def editar_usuario(usuario_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    usuario = await usuarios.obter(db, usuario_id)
    if usuario is None:
        raise HTTPException(status_code=404, detail="Usuário não encontrado")
    form = await request.form()
    nome = str(form.get("nome") or "").strip()
    telefone = str(form.get("telefone_whatsapp") or "").strip()
    nova_senha = str(form.get("nova_senha") or "").strip() or None
    recebe_alertas = "recebe_alertas" in form
    # Ninguém se desativa: sem isso, a última pessoa logada podia se trancar
    # fora do painel sem ter como voltar (não existe reset de senha por fora).
    ativo = True if usuario.username == request.session.get("usuario") else "ativo" in form
    if not nome:
        raise HTTPException(status_code=400, detail="Nome é obrigatório.")
    await usuarios.atualizar(
        db,
        usuario,
        nome=nome,
        telefone_whatsapp=telefone,
        recebe_alertas=recebe_alertas,
        ativo=ativo,
        nova_senha=nova_senha,
    )
    await db.commit()
    return RedirectResponse("/painel/usuarios?salvo=1", status_code=303)


@router.post("/usuarios/{usuario_id}/alerta")
async def alternar_alerta(usuario_id: int, db: AsyncSession = Depends(get_db)):
    """Liga/desliga o alerta com um clique só, direto da lista."""
    usuario = await usuarios.obter(db, usuario_id)
    if usuario is None:
        raise HTTPException(status_code=404, detail="Usuário não encontrado")
    await usuarios.atualizar(
        db,
        usuario,
        nome=usuario.nome,
        telefone_whatsapp=usuario.telefone_whatsapp,
        recebe_alertas=not usuario.recebe_alertas,
        ativo=usuario.ativo,
    )
    await db.commit()
    return RedirectResponse("/painel/usuarios", status_code=303)
