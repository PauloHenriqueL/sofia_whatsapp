"""KPIs da Sofia pro painel da Thainá (Frente 3).

Quase tudo é derivado das tabelas existentes (conversa, mensagem, escalada). O
agrupamento por dia é feito em Python pra ficar portável entre SQLite (dev) e
Postgres (prod).

**A exceção é o tempo até a primeira sessão**, que precisa da data da consulta —
dado que só existe no Hamilton. Essa é a única métrica que faz chamada externa, e
ela degrada sozinha: Hamilton fora do ar devolve `None` e o card some, o resto da
página continua.
"""

import logging
from collections import Counter
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Conversa, Escalada, Mensagem
from app.services import hamilton_client, saida, tools

logger = logging.getLogger(__name__)


async def _scalar(db: AsyncSession, stmt) -> int:
    return int((await db.execute(stmt)).scalar() or 0)


def _para_data(valor) -> date | None:
    """Normaliza data do Hamilton (string ISO) ou datetime local pra `date`."""
    if isinstance(valor, datetime):
        return valor.date()
    if isinstance(valor, date):
        return valor
    if isinstance(valor, str):
        try:
            return date.fromisoformat(valor[:10])
        except ValueError:
            return None
    return None


def _mediana(valores: list[int]) -> int | None:
    """Mediana, não média: um paciente que demorou 90 dias distorce a média e faz
    parecer que todo mundo espera muito. A mediana responde "o caso típico"."""
    if not valores:
        return None
    ordenados = sorted(valores)
    meio = len(ordenados) // 2
    if len(ordenados) % 2:
        return ordenados[meio]
    return round((ordenados[meio - 1] + ordenados[meio]) / 2)


async def _tempo_ate_primeira_sessao(db: AsyncSession, hamilton=None) -> dict | None:
    """Dias entre a primeira mensagem do paciente e a primeira sessão realizada.

    É o número que responde "quanto tempo alguém espera pra ser atendido" — o
    ciclo inteiro, do "oi" até estar na cadeira. Cobre só quem já teve a primeira
    consulta **realizada**; quem ainda está esperando aparece na fila do
    `/painel/acompanhamento`, não aqui (senão a métrica melhoraria sozinha quando
    alguém demora, porque o caso lento ficaria de fora até terminar).

    `criada_em` é da Sofia; `dat_primeira_consulta` é do Hamilton e vem só com
    data (sem hora) — daí o resultado ser em dias inteiros.
    """
    hamilton = hamilton or hamilton_client.get_hamilton_client()
    conversas = (
        (await db.execute(select(Conversa).where(Conversa.paciente_hamilton_id.isnot(None))))
        .scalars()
        .all()
    )
    por_pid = {c.paciente_hamilton_id: c for c in conversas}
    if not por_pid:
        return None
    try:
        status = await hamilton.status_primeira_consulta(list(por_pid.keys()))
    except hamilton_client.HamiltonError as exc:
        logger.error("Não consegui calcular o tempo até a 1ª sessão: %s", exc)
        return None

    dias: list[int] = []
    for pid, conversa in por_pid.items():
        info = status.get(pid) or {}
        if not info.get("primeira_consulta_realizada"):
            continue
        inicio = _para_data(conversa.criada_em)
        consulta = _para_data(info.get("dat_primeira_consulta"))
        if inicio is None or consulta is None:
            continue
        delta = (consulta - inicio).days
        # Negativo = a consulta é anterior à conversa (paciente antigo que só
        # depois falou com a Sofia). Não é espera; contaria como 0 e puxaria a
        # mediana pra baixo, então sai da conta.
        if delta >= 0:
            dias.append(delta)

    if not dias:
        return None
    return {
        "mediana": _mediana(dias),
        "media": round(sum(dias) / len(dias)),
        "minimo": min(dias),
        "maximo": max(dias),
        "pacientes": len(dias),
    }


async def calcular_metricas(db: AsyncSession, agora: datetime | None = None, hamilton=None) -> dict:
    """Calcula os KPIs da Sofia. `agora` é injetável pra facilitar os testes."""
    agora = agora or datetime.now(timezone.utc)
    inicio_hoje = agora.replace(hour=0, minute=0, second=0, microsecond=0)
    inicio_7d = (agora - timedelta(days=6)).replace(hour=0, minute=0, second=0, microsecond=0)

    total = await _scalar(db, select(func.count(Conversa.id)))
    leads_hoje = await _scalar(
        db, select(func.count(Conversa.id)).where(Conversa.criada_em >= inicio_hoje)
    )
    cadastrados = await _scalar(
        db, select(func.count(Conversa.id)).where(Conversa.paciente_hamilton_id.isnot(None))
    )
    humano = await _scalar(db, select(func.count(Conversa.id)).where(Conversa.modo == "humano"))
    pendentes = await _scalar(
        db, select(func.count(Conversa.id)).where(Conversa.estado == "cadastro_pendente")
    )
    escalados = await _scalar(
        db, select(func.count(Conversa.id)).where(Conversa.estado == "escalado")
    )
    followups = await _scalar(
        db, select(func.count(Conversa.id)).where(Conversa.seguimento_enviado_em.isnot(None))
    )
    # Recuperados: levou follow-up e o paciente voltou a responder depois.
    recuperados = await _scalar(
        db,
        select(func.count(func.distinct(Conversa.id)))
        .select_from(Conversa)
        .join(Mensagem, Mensagem.conversa_id == Conversa.id)
        .where(
            Conversa.seguimento_enviado_em.isnot(None),
            Mensagem.direcao == "recebida",
            Mensagem.criada_em > Conversa.seguimento_enviado_em,
        ),
    )

    taxa_conversao = round(cadastrados / total * 100) if total else 0
    # Autonomia: % de conversas que a Sofia resolveu sem precisar de uma pessoa.
    autonomia = round((total - humano) / total * 100) if total else 0

    # Escaladas por motivo (com rótulo legível), mais frequentes primeiro.
    # Só as ABERTAS: `resolvida_em` passou a ser preenchido de verdade quando a
    # Thainá devolve ao bot ou arquiva, e misturar aberto com fechado faz um
    # motivo já resolvido continuar parecendo problema ativo.
    linhas = (
        await db.execute(
            select(Escalada.motivo, func.count(Escalada.id))
            .where(Escalada.resolvida_em.is_(None))
            .group_by(Escalada.motivo)
        )
    ).all()
    escaladas_por_motivo = sorted(
        ({"motivo": m, "rotulo": tools.MOTIVO_LABELS.get(m, m), "qtd": int(q)} for m, q in linhas),
        key=lambda x: x["qtd"],
        reverse=True,
    )

    # ── Pesquisa de satisfação (Demanda C) ────────────────────────────────────
    pesquisas_em_curso = await _scalar(
        db, select(func.count(Conversa.id)).where(Conversa.pesquisa_avaliacao_id.isnot(None))
    )
    alertas_abertos = await _scalar(
        db,
        select(func.count(Conversa.id)).where(
            Conversa.alerta_pesquisa_em.isnot(None), Conversa.alerta_resolvido_em.is_(None)
        ),
    )
    alertas_total = await _scalar(
        db, select(func.count(Conversa.id)).where(Conversa.alerta_pesquisa_em.isnot(None))
    )

    # ── Cobrança da mensalidade (Demanda D) ───────────────────────────────────
    cobrancas = (
        await db.execute(
            select(Conversa.cobranca_status, func.count(Conversa.id))
            .where(Conversa.cobranca_iniciada_em.isnot(None))
            .group_by(Conversa.cobranca_status)
        )
    ).all()
    from app.services import cobranca as cobranca_service

    cobrancas_por_status = sorted(
        (
            {
                "status": s,
                "rotulo": cobranca_service.STATUS_LABELS.get(s or "", s or "sem status"),
                "qtd": int(q),
                # Estes dois são FALHA operacional, não desfecho: a Sofia não
                # conseguiu cobrar e ninguém fica sabendo se não olhar aqui.
                "falha": s in ("sem_janela", "erro_link"),
            }
            for s, q in cobrancas
        ),
        key=lambda x: x["qtd"],
        reverse=True,
    )
    cobrancas_com_falha = sum(c["qtd"] for c in cobrancas_por_status if c["falha"])

    # Leads por dia nos últimos 7 dias (bucket em Python -> portável).
    datas = (
        (await db.execute(select(Conversa.criada_em).where(Conversa.criada_em >= inicio_7d)))
        .scalars()
        .all()
    )
    contagem: Counter = Counter(d.date() for d in datas if d is not None)
    leads_por_dia = [
        {
            "dia": (inicio_7d + timedelta(days=i)).strftime("%d/%m"),
            "qtd": contagem.get((inicio_7d + timedelta(days=i)).date(), 0),
        }
        for i in range(7)
    ]

    return {
        "total": total,
        "leads_hoje": leads_hoje,
        "cadastrados": cadastrados,
        "taxa_conversao": taxa_conversao,
        "humano": humano,
        "pendentes": pendentes,
        "escalados": escalados,
        "autonomia": autonomia,
        "followups": followups,
        "recuperados": recuperados,
        "escaladas_por_motivo": escaladas_por_motivo,
        "leads_por_dia": leads_por_dia,
        # Rede de proteção da saída (P0): se subir, o modelo/prompt regrediu.
        # Em memória: zera a cada restart do processo. O registro permanente é o log.
        "saidas_bloqueadas": saida.bloqueios(),
        # Pesquisa e cobrança (Demandas C e D).
        "pesquisas_em_curso": pesquisas_em_curso,
        "alertas_abertos": alertas_abertos,
        "alertas_total": alertas_total,
        "cobrancas_por_status": cobrancas_por_status,
        "cobrancas_com_falha": cobrancas_com_falha,
        # None quando o Hamilton está fora ou ninguém teve a 1ª sessão ainda.
        "tempo_primeira_sessao": await _tempo_ate_primeira_sessao(db, hamilton),
    }
