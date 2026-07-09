"""KPIs da Sofia pro painel da Thainá (Frente 3).

Tudo derivado das tabelas existentes (conversa, mensagem, escalada). O
agrupamento por dia é feito em Python pra ficar portável entre SQLite (dev) e
Postgres (prod).
"""

import logging
from collections import Counter
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Conversa, Escalada, Mensagem
from app.services import saida, tools

logger = logging.getLogger(__name__)


async def _scalar(db: AsyncSession, stmt) -> int:
    return int((await db.execute(stmt)).scalar() or 0)


async def calcular_metricas(db: AsyncSession, agora: datetime | None = None) -> dict:
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
    linhas = (
        await db.execute(select(Escalada.motivo, func.count(Escalada.id)).group_by(Escalada.motivo))
    ).all()
    escaladas_por_motivo = sorted(
        ({"motivo": m, "rotulo": tools.MOTIVO_LABELS.get(m, m), "qtd": int(q)} for m, q in linhas),
        key=lambda x: x["qtd"],
        reverse=True,
    )

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
    }
