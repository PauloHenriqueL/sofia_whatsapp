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
from app.services import cobranca as cobranca_mod
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
            # A 1ª consulta só conta como realizada se o terapeuta marcar o
            # checkbox `is_realizado` no Hamilton. Desmarcado, o paciente fica
            # aqui pra sempre e a cobrança NUNCA dispara — sem sintoma nenhum.
            # Passado o dobro da meta, é mais provável que a sessão tenha
            # acontecido e ninguém marcou do que a pessoa estar esperando há
            # 15 dias. Este aviso é o único lugar onde isso fica visível.
            item["talvez_nao_marcada"] = dias > META_DIAS * 2
            espera.append(item)
        elif c.cobranca_resolvida_em is None:
            item["dat_primeira_consulta"] = st.get("dat_primeira_consulta")
            # Referência Stripe do paciente (se houver): o router anota o status
            # de pagamento ao vivo em cima dela (pagamentos.anotar_pagamentos).
            item["stripe_ref"] = c.stripe_ref
            # O que a Sofia já fez de cobrança nesta conversa. Sem isso a Thainá
            # não distingue "a Sofia cobrou e a pessoa sumiu" de "a Sofia nunca
            # conseguiu falar" — e trataria os dois casos igual.
            item["cobranca_status"] = c.cobranca_status
            item["cobranca_status_label"] = cobranca_mod.STATUS_LABELS.get(c.cobranca_status or "")
            item["cobranca_iniciada_em"] = c.cobranca_iniciada_em
            # Parceria paga por fora (prefeitura/convênio custeia): a pessoa não
            # deve nada, e a fila convidava a Thainá a cobrar. A cobrança
            # automática já pulava; a tela não sinalizava.
            item["is_parceria"] = bool((c.dados_coletados or {}).get("is_parceria"))
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


async def listar_alertas_pesquisa(db: AsyncSession) -> list[dict[str, Any]]:
    """Pesquisas que terminaram com sinal ruim e ainda não foram tratadas.

    Existe porque o template do WhatsApp **some na rolagem**. Sem uma fila, o
    alerta vira uma notificação que a Thainá viu no meio de outras vinte e o
    desenho volta a ser "coletar e arquivar".

    Não depende do Hamilton: os motivos são um snapshot gravado no momento do
    alerta, então a página abre mesmo com a API fora do ar.
    """
    q = (
        select(Conversa)
        .where(Conversa.alerta_pesquisa_em.isnot(None), Conversa.alerta_resolvido_em.is_(None))
        .order_by(Conversa.alerta_pesquisa_em.desc())
    )
    conversas = (await db.execute(q)).scalars().all()
    return [
        {
            "conversa_id": c.id,
            "nome": (c.dados_coletados or {}).get("nome_completo") or c.numero_whatsapp,
            "numero": c.numero_whatsapp,
            "motivos": c.alerta_pesquisa_motivos or "",
            "quando": c.alerta_pesquisa_em,
            "modo": c.modo,
        }
        for c in conversas
    ]


async def marcar_alerta_resolvido(db: AsyncSession, conversa: Conversa) -> None:
    """Tira o alerta da fila. Soft-delete: nada é apagado, e dá pra reabrir."""
    conversa.alerta_resolvido_em = datetime.now(timezone.utc)
    await db.commit()


async def reabrir_alerta(db: AsyncSession, conversa: Conversa) -> None:
    """Devolve o alerta à fila (clique por engano, ou o assunto voltou)."""
    conversa.alerta_resolvido_em = None
    await db.commit()


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
