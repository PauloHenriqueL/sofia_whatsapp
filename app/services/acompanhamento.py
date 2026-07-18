"""Acompanhamento pós-cadastro (Demandas 3 e 4).

Cruza as conversas cadastradas pela Sofia (que têm `paciente_hamilton_id`) com o
status da 1ª consulta no Hamilton e monta três listas pro painel da Thainá:

- **Espera pela 1ª consulta (Demanda 3):** pacientes cadastrados cuja 1ª consulta
  ainda NÃO foi realizada, com os dias desde o cadastro (meta de 7 dias).
- **Pronto pra cobrança (Demanda 4):** pacientes cuja 1ª consulta JÁ foi realizada
  e que a Thainá ainda não marcou como cobrança resolvida.
- **Resolvidos:** os que ela já marcou. Ficam visíveis (com "Reabrir") porque
  resolvido é um **estado**, não o fim da conversa: a Thainá pode ter clicado por
  engano, ou precisar voltar a falar com o paciente depois.

A conversa **nunca é apagada** por nenhuma dessas ações — `cobranca_resolvida_em`
é um soft-delete que só tira o paciente da fila de trabalho. Ele continua em
"Todas as conversas", com todo o histórico.

Se o Hamilton estiver fora do ar, devolve as listas vazias com uma mensagem de
erro (o painel mostra um aviso em vez de quebrar).
"""

import logging
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Conversa
from app.services import hamilton_client

logger = logging.getLogger(__name__)

META_DIAS = 7  # meta pra 1ª consulta acontecer após o cadastro


def _para_data(valor) -> date | None:
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


def _dias_desde(agora: datetime, cadastro) -> int:
    d = _para_data(cadastro)
    return (agora.date() - d).days if d else 0


async def montar_acompanhamento(
    db: AsyncSession, hamilton=None, agora: datetime | None = None
) -> dict:
    """Monta as listas de espera (Demanda 3), cobrança (Demanda 4) e resolvidos."""
    agora = agora or datetime.now(timezone.utc)
    hamilton = hamilton or hamilton_client.get_hamilton_client()

    conversas = (
        (await db.execute(select(Conversa).where(Conversa.paciente_hamilton_id.isnot(None))))
        .scalars()
        .all()
    )
    por_pid = {c.paciente_hamilton_id: c for c in conversas}

    erro = None
    status: dict[int, dict] = {}
    if por_pid:
        try:
            status = await hamilton.status_primeira_consulta(list(por_pid.keys()))
        except hamilton_client.HamiltonError as exc:
            logger.error("Falha ao consultar status no Hamilton: %s", exc)
            erro = "Não consegui falar com o Hamilton agora. Tenta de novo daqui a pouco."

    espera: list[dict[str, Any]] = []
    cobranca: list[dict[str, Any]] = []
    resolvidos: list[dict[str, Any]] = []
    for pid, c in por_pid.items():
        st = status.get(pid)
        if st is None:
            continue  # sem status (Hamilton offline, ou paciente removido de lá)

        nome = st.get("nome") or (c.dados_coletados or {}).get("nome_completo") or c.numero_whatsapp
        # Data de cadastro: preferimos a do Hamilton; caímos pra criação da conversa.
        dias = _dias_desde(agora, st.get("created_at") or c.criada_em)
        item: dict[str, Any] = {
            "conversa_id": c.id,
            "paciente_id": pid,
            "nome": nome,
            "numero": c.numero_whatsapp,
            "dias": dias,
            "modo": c.modo,  # a Thainá vê se a conversa está com ela ou com o bot
        }
        if not st.get("primeira_consulta_realizada"):
            item["fora_da_meta"] = dias > META_DIAS
            espera.append(item)
        elif c.cobranca_resolvida_em is None:
            item["dat_primeira_consulta"] = st.get("dat_primeira_consulta")
            # Referência Stripe do paciente (se houver): o router anota o status
            # de pagamento ao vivo em cima dela (pagamentos.anotar_pagamentos).
            item["stripe_ref"] = c.stripe_ref
            cobranca.append(item)
        else:
            # Resolvido não é fim: fica visível, dá pra abrir a conversa e reabrir
            # a cobrança se a Thainá tiver clicado por engano.
            item["resolvida_em"] = c.cobranca_resolvida_em
            resolvidos.append(item)

    espera.sort(key=lambda x: x["dias"], reverse=True)  # mais urgentes primeiro
    resolvidos.sort(key=lambda x: x["resolvida_em"], reverse=True)  # recentes primeiro
    return {
        "espera": espera,
        "cobranca": cobranca,
        "resolvidos": resolvidos,
        "erro": erro,
        "meta_dias": META_DIAS,
    }


async def marcar_cobranca_resolvida(db: AsyncSession, conversa: Conversa) -> None:
    """Tira o paciente da fila de cobrança (a conversa continua existindo).

    Soft-delete: guarda **quando** foi resolvido, então dá pra desfazer
    (`reabrir_cobranca`) e auditar. Não mexe no `modo` da conversa: resolver
    cobrança é sobre dinheiro, não sobre quem atende.
    """
    conversa.cobranca_resolvida_em = datetime.now(timezone.utc)
    await db.commit()


async def reabrir_cobranca(db: AsyncSession, conversa: Conversa) -> None:
    """Desfaz o "marcar resolvido": o paciente volta pra fila de cobrança."""
    conversa.cobranca_resolvida_em = None
    await db.commit()
