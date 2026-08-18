"""Tela "Hoje" — a fila do que precisa de uma pessoa, num lugar só.

Antes desta tela, "o que eu tenho pra fazer agora?" não tinha resposta em lugar
nenhum do painel: escalada aberta e cadastro que falhou moravam na **lista de
conversas** (misturados com todo mundo), alerta de pesquisa e espera pela 1ª
consulta no **acompanhamento**, e falha de cobrança só aparecia como número em
**resultados**. Três telas — e a navegação pra duas delas estava invisível
(as abas nasciam debaixo da topbar fixa).

Duas regras que dão o desenho:

- **A fila não tem recorte de tempo.** Escalada de três semanas atrás continua
  precisando de alguém. Filtrar por "hoje" faria sumir exatamente o caso que a
  tela existe pra pegar — o que foi esquecido. A janela de 7 dias vale só pros
  números e pro resumo do que a Sofia fez sozinha.
- **Uma pendência por conversa.** A mesma pessoa pode disparar dois sinais (o
  comprovante que chega no meio da cobrança abre escalada *e* marca
  `cobranca_status='comprovante'`). Duas linhas pro mesmo atendimento fariam a
  fila mentir o tamanho dela, então vence o sinal mais específico (`PRIORIDADE`).

Só a lista "de olho" toca o Hamilton. Se ele cair, a fila principal e os números
continuam de pé — são todos do banco local.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Conversa, Escalada
from app.services import acompanhamento, cadastro_abandonado
from app.services import cobranca as cobranca_mod
from app.services import config_negocio
from app.services import contrato as contrato_mod
from app.services import hamilton_client, tools

logger = logging.getLogger(__name__)

JANELA_RESUMO_DIAS = 7

# Status de cobrança em que a Sofia parou e não vai sozinha adiante.
COBRANCA_TRAVADA = ("sem_janela", "erro_link")
# Status de cobrança em curso: ela já falou e está esperando a pessoa.
COBRANCA_ANDANDO = ("enviada", "cartao", "pix", "indefinido")

# Quem vence quando a mesma conversa dispara mais de um sinal. Menor = mais forte.
# Escalada ganha porque é o único caso em que a Sofia **parou de responder**:
# a pessoa está falando com um bot que não vai mais falar de volta.
PRIORIDADE = {
    "escalada": 0,
    "cadastro_falhou": 1,
    "alerta_pesquisa": 2,
    "cobranca_travada": 3,
    # Depois das travas, antes do "de olho": tem ficha esperando um clique, e
    # ninguém está bloqueado — mas a pessoa acha que já se cadastrou.
    "cadastro_a_confirmar": 4,
    # "de olho" usa o mesmo dedupe; espera longa vence cobrança sem resposta
    "primeira_consulta": 10,
    "cobranca_sem_resposta": 11,
    # Contrato por último de propósito: quem não pagou é mais urgente que quem
    # não assinou, e costuma ser a mesma pessoa. Acima, a fila mostraria
    # "contrato pendente" pra alguém cujo problema real é o pagamento.
    "contrato_pendente": 12,
}

ROTULOS = {
    "escalada": "Escalada",
    "cadastro_falhou": "Cadastro falhou",
    "alerta_pesquisa": "Alerta",
    "cobranca_travada": "Cobrança travada",
    "cadastro_a_confirmar": "Cadastro a confirmar",
    "primeira_consulta": "1ª consulta",
    "cobranca_sem_resposta": "Sem resposta",
    "contrato_pendente": "Contrato",
}


def _nome(conversa: Conversa) -> str:
    return (conversa.dados_coletados or {}).get("nome_completo") or conversa.numero_whatsapp


def _corte(agora: datetime) -> datetime:
    return agora - timedelta(days=JANELA_RESUMO_DIAS)


async def _escaladas_abertas(db: AsyncSession) -> list[dict[str, Any]]:
    q = (
        select(Escalada, Conversa)
        .join(Conversa, Escalada.conversa_id == Conversa.id)
        .where(Escalada.resolvida_em.is_(None), Conversa.arquivada_em.is_(None))
        .order_by(Escalada.criada_em.desc())
    )
    return [
        {
            "tipo": "escalada",
            "conversa_id": c.id,
            "nome": _nome(c),
            "numero": c.numero_whatsapp,
            "texto": tools.MOTIVO_LABELS.get(e.motivo, e.motivo),
            "quando": e.criada_em,
        }
        for e, c in (await db.execute(q)).all()
    ]


async def _cadastros_falhos(db: AsyncSession) -> list[dict[str, Any]]:
    q = select(Conversa).where(
        Conversa.estado == "cadastro_pendente", Conversa.arquivada_em.is_(None)
    )
    return [
        {
            "tipo": "cadastro_falhou",
            "conversa_id": c.id,
            "nome": _nome(c),
            "numero": c.numero_whatsapp,
            "texto": "a Sofia coletou tudo mas o Hamilton recusou — precisa cadastrar à mão",
            "quando": c.atualizada_em,
        }
        for c in (await db.execute(q)).scalars().all()
    ]


async def _cadastros_a_confirmar(db: AsyncSession) -> list[dict[str, Any]]:
    """Quem passou os dados e sumiu antes de confirmar (`cadastro_abandonado`).

    A Sofia releu a conversa e deixou a ficha montada; falta alguém olhar o
    histórico e clicar em "Cadastrar no Hamilton". **Não é cadastro pendente de
    erro** — o Hamilton nem foi chamado ainda, de propósito: dá pra saber que a
    pessoa passou os dados, não que ela quis ser cadastrada.
    """
    return [
        {
            "tipo": "cadastro_a_confirmar",
            "conversa_id": c.id,
            "nome": _nome(c),
            "numero": c.numero_whatsapp,
            "texto": "passou os dados e sumiu antes de confirmar — revise e cadastre",
            "quando": c.atualizada_em,
        }
        for c in await cadastro_abandonado.aguardando_confirmacao(db)
    ]


async def _cobrancas_travadas(db: AsyncSession) -> list[dict[str, Any]]:
    q = select(Conversa).where(
        Conversa.cobranca_status.in_(COBRANCA_TRAVADA),
        Conversa.cobranca_resolvida_em.is_(None),
        Conversa.arquivada_em.is_(None),
    )
    return [
        {
            "tipo": "cobranca_travada",
            "conversa_id": c.id,
            "nome": _nome(c),
            "numero": c.numero_whatsapp,
            "texto": cobranca_mod.STATUS_LABELS.get(c.cobranca_status or "", "cobrança travada"),
            "quando": c.cobranca_iniciada_em or c.atualizada_em,
        }
        for c in (await db.execute(q)).scalars().all()
    ]


async def _alertas(db: AsyncSession) -> list[dict[str, Any]]:
    return [
        {
            "tipo": "alerta_pesquisa",
            "conversa_id": a["conversa_id"],
            "nome": a["nome"],
            "numero": a["numero"],
            "texto": a["motivos"] or "a pesquisa terminou com sinal ruim",
            "quando": a["quando"],
        }
        for a in await acompanhamento.listar_alertas_pesquisa(db)
    ]


def _dedupe(itens: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Uma linha por conversa: fica o sinal mais forte (ver PRIORIDADE)."""
    melhor: dict[int, dict[str, Any]] = {}
    for item in itens:
        atual = melhor.get(item["conversa_id"])
        if atual is None or PRIORIDADE.get(item["tipo"], 99) < PRIORIDADE.get(atual["tipo"], 99):
            melhor[item["conversa_id"]] = item
    return list(melhor.values())


def _ordenar(itens: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Mais recente primeiro. `quando` nulo vai pro fim em vez de estourar."""
    antigo = datetime.min.replace(tzinfo=timezone.utc)

    def chave(i):
        q = i.get("quando")
        if not isinstance(q, datetime):
            return antigo
        return q if q.tzinfo else q.replace(tzinfo=timezone.utc)

    return sorted(itens, key=chave, reverse=True)


async def listar_pendencias(db: AsyncSession) -> list[dict[str, Any]]:
    """A fila "precisa de você agora". Só banco local — nunca depende do Hamilton."""
    itens = (
        await _escaladas_abertas(db)
        + await _cadastros_falhos(db)
        + await _alertas(db)
        + await _cobrancas_travadas(db)
        + await _cadastros_a_confirmar(db)
    )
    for i in itens:
        i["rotulo"] = ROTULOS[i["tipo"]]
    return _ordenar(_dedupe(itens))


async def contar_pendencias(db: AsyncSession) -> int:
    """Só o número, pro contador na aba. Roda em toda página do painel."""
    try:
        return len(await listar_pendencias(db))
    except Exception:  # pragma: no cover - contador nunca pode derrubar uma página
        logger.exception("Falha ao contar pendências")
        return 0


async def _de_olho(db: AsyncSession, agora: datetime, hamilton) -> tuple[list, str | None]:
    """O que ainda não travou mas tem relógio correndo."""
    itens: list[dict[str, Any]] = []

    # Cobrança em curso: a Sofia falou e a pessoa não respondeu. Não é pendência
    # (ela ainda vai mandar o lembrete sozinha), mas some da vista sem isto.
    q = select(Conversa).where(
        Conversa.cobranca_iniciada_em.isnot(None),
        Conversa.cobranca_encerrada_em.is_(None),
        Conversa.cobranca_status.in_(COBRANCA_ANDANDO),
        Conversa.arquivada_em.is_(None),
    )
    for c in (await db.execute(q)).scalars().all():
        itens.append(
            {
                "tipo": "cobranca_sem_resposta",
                "rotulo": ROTULOS["cobranca_sem_resposta"],
                "conversa_id": c.id,
                "nome": _nome(c),
                "numero": c.numero_whatsapp,
                "texto": cobranca_mod.STATUS_LABELS.get(
                    c.cobranca_status or "", "cobrança em curso"
                ),
                "quando": c.cobranca_iniciada_em,
            }
        )

    # Espera pela 1ª consulta fora da meta. Único trecho que fala com o Hamilton.
    erro = None
    try:
        dados = await acompanhamento.montar_acompanhamento(db, hamilton=hamilton, agora=agora)
        erro = dados.get("erro")
        for e in dados["espera"]:
            if not e.get("fora_da_meta"):
                continue
            texto = f"cadastrada há {e['dias']} dias e ainda não foi atendida"
            if e.get("talvez_nao_marcada"):
                texto += " — pode ser que a sessão aconteceu e ninguém marcou no Hamilton"
            itens.append(
                {
                    "tipo": "primeira_consulta",
                    "rotulo": ROTULOS["primeira_consulta"],
                    "conversa_id": e["conversa_id"],
                    "nome": e["nome"],
                    "numero": e["numero"],
                    "texto": texto,
                    "dias": e["dias"],
                    "quando": None,
                }
            )
    except hamilton_client.HamiltonError as exc:  # pragma: no cover - rede
        logger.error("Hamilton fora ao montar 'de olho': %s", exc)
        erro = "Não consegui falar com o Hamilton agora — a espera pela 1ª consulta não está aqui."

    itens.extend(await _contratos_pendentes(db, agora, hamilton))

    # Espera longa primeiro; o resto por recência.
    itens.sort(key=lambda i: i.get("dias") or 0, reverse=True)
    return _dedupe(itens), erro


async def _contratos_pendentes(db: AsyncSession, agora: datetime, hamilton) -> list[dict[str, Any]]:
    """Quem recebeu o contrato e ainda não assinou (Demanda E).

    Sem recorte de tempo, como o resto da tela: contrato esquecido há um mês é
    exatamente o que não pode sumir da vista.

    O estado mora no Hamilton, então isto é uma chamada em lote — e, como todo o
    "de olho", **degrada em silêncio**: Hamilton fora, a linha some e o resto da
    fila continua de pé. Contrato pendente não é urgência; virar erro na tela
    seria pior que não aparecer.
    """
    if not contrato_mod.ativo():
        return []
    cliente = hamilton or hamilton_client.get_hamilton_client()
    try:
        pendentes = await cliente.contratos_pendentes()
    except hamilton_client.HamiltonError as exc:
        logger.warning("Não consegui listar contratos pendentes: %s", exc)
        return []
    if not pendentes:
        return []

    por_paciente = {p["paciente_id"]: p for p in pendentes if p.get("paciente_id")}
    q = select(Conversa).where(
        Conversa.paciente_hamilton_id.in_(list(por_paciente)),
        Conversa.arquivada_em.is_(None),
    )
    itens: list[dict[str, Any]] = []
    for c in (await db.execute(q)).scalars().all():
        dado = por_paciente.get(c.paciente_hamilton_id)
        if not dado:
            continue
        enviado = _quando(dado.get("enviado_em"))
        dias = (agora - enviado).days if enviado else 0
        itens.append(
            {
                "tipo": "contrato_pendente",
                "rotulo": ROTULOS["contrato_pendente"],
                "conversa_id": c.id,
                "nome": _nome(c),
                "numero": c.numero_whatsapp,
                "texto": (
                    f"contrato enviado há {dias} dias e ainda não assinado"
                    if dias
                    else "contrato enviado e ainda não assinado"
                ),
                "quando": enviado,
            }
        )
    return itens


def _quando(iso: str | None) -> datetime | None:
    """ISO do Hamilton -> datetime aware. Data ilegível vira None, não exceção."""
    if not iso:
        return None
    try:
        valor = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    return valor if valor.tzinfo else valor.replace(tzinfo=timezone.utc)


async def _contar(db: AsyncSession, coluna, desde: datetime) -> int:
    q = select(func.count()).select_from(Conversa).where(coluna >= desde)
    return int((await db.execute(q)).scalar() or 0)


async def _resumo(db: AsyncSession, agora: datetime) -> list[dict[str, Any]]:
    """O que a Sofia resolveu sozinha na janela. Não tem ação — é só notícia.

    A janela é de 7 dias, e não do dia: num dia só quase nada acontece e o bloco
    viveria vazio, dando a impressão errada de que ela não fez nada.
    """
    desde = _corte(agora)
    linhas: list[dict[str, Any]] = []

    cadastros = await _contar(db, Conversa.cadastrado_em, desde)
    if cadastros:
        hoje = await _contar(
            db, Conversa.cadastrado_em, agora.replace(hour=0, minute=0, second=0, microsecond=0)
        )
        linhas.append(
            {
                "titulo": f"{cadastros} {'cadastro novo' if cadastros == 1 else 'cadastros novos'} no Hamilton",
                "detalhe": f"{hoje} hoje" if hoje else "nenhum hoje",
            }
        )

    pesquisas = await _contar(db, Conversa.pesquisa_iniciada_em, desde)
    if pesquisas:
        alertas = await _contar(db, Conversa.alerta_pesquisa_em, desde)
        linhas.append(
            {
                "titulo": f"{pesquisas} {'pesquisa conduzida' if pesquisas == 1 else 'pesquisas conduzidas'}",
                "detalhe": (
                    f"{alertas} {'virou alerta' if alertas == 1 else 'viraram alerta'}"
                    if alertas
                    else "nenhuma acendeu alerta"
                ),
            }
        )

    cobrancas = await _contar(db, Conversa.cobranca_encerrada_em, desde)
    if cobrancas:
        linhas.append(
            {
                "titulo": f"{cobrancas} {'cobrança conduzida' if cobrancas == 1 else 'cobranças conduzidas'} até o fim",
                "detalhe": "sem ninguém precisar entrar",
            }
        )
    return linhas


async def montar_hoje(db: AsyncSession, hamilton=None, agora: datetime | None = None) -> dict:
    agora = agora or datetime.now(timezone.utc)
    hamilton = hamilton or hamilton_client.get_hamilton_client()
    desde = _corte(agora)

    pendencias = await listar_pendencias(db)
    de_olho, erro = await _de_olho(db, agora, hamilton)

    novas = int(
        (
            await db.execute(
                select(func.count())
                .select_from(Conversa)
                .where(Conversa.criada_em >= desde, Conversa.arquivada_em.is_(None))
            )
        ).scalar()
        or 0
    )
    return {
        "pendencias": pendencias,
        "de_olho": de_olho,
        "resumo": await _resumo(db, agora),
        "erro": erro,
        "janela_dias": JANELA_RESUMO_DIAS,
        "numeros": {
            "pendentes": len(pendencias),
            "novas": novas,
            "cadastradas": await _contar(db, Conversa.cadastrado_em, desde),
            "pesquisas": await _contar(db, Conversa.pesquisa_iniciada_em, desde),
        },
        "automacoes": [
            {
                "ligada": bool(config_negocio.valor("cobranca_ativa")),
                "on": "cobrando mensalidade",
                "off": "sem cobrar mensalidade",
            },
            {
                "ligada": bool(config_negocio.valor("pesquisa_entrada_ativa")),
                "on": "pedindo o ORS de entrada",
                "off": "sem pedir o ORS de entrada",
            },
            {
                "ligada": bool(config_negocio.valor("transcrever_audio")),
                "on": "ouvindo áudios",
                "off": "sem ouvir áudios",
            },
        ],
    }
