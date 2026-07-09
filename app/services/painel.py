"""Operações do painel da Thainá (consultas e ações sobre conversas).

Compartilhado pelos routers de API (JSON) e de painel (HTML/HTMX) para não
duplicar a lógica de listar, responder, assumir e devolver ao bot.
"""

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import String, asc, cast, delete, desc, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Conversa, Escalada, Mensagem
from app.services import conversation, whatsapp_client


def url_hamilton_paciente(paciente_hamilton_id: int | None) -> str | None:
    """URL da tela de edição do paciente no Hamilton.

    É onde a Thainá completa os campos que a Sofia não coleta na conversa.
    Devolve None enquanto o paciente ainda não tem cadastro no Hamilton.
    """
    if not paciente_hamilton_id:
        return None
    base = settings.hamilton_api_url.rstrip("/")
    return f"{base}/api/v1/pacientes/{paciente_hamilton_id}/editar/"


# Colunas pelas quais a Thainá pode ordenar a lista, e como cada uma ordena.
# `preview` não entra: é derivado de subquery por linha, ordenar por ele não
# ajuda ninguém a se localizar.
ORDENS = {
    "numero_whatsapp": "Número",
    "nome": "Nome",
    "modo": "Modo",
    "estado": "Estado",
    "atualizada_em": "Atividade",
}

# Filtros do menu (chave -> rótulo). Ficam aqui, e não no template, porque o
# router precisa deles pra montar o contexto e os testes pra iterar.
FILTROS = {
    "todas": "Todas as conversas",
    "humano": "Em modo humano",
    "escalada": "Em escalada",
    "cadastradas_hoje": "Cadastradas hoje",
    "cadastrados": "Já no Hamilton",
}


def _aplicar_filtro(q, filtro: str):
    if filtro == "humano":
        return q.where(Conversa.modo == "humano")
    if filtro == "escalada":
        return q.where(Conversa.estado == "escalado")
    if filtro == "cadastradas_hoje":
        inicio = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        return q.where(Conversa.estado == "cadastrado", Conversa.atualizada_em >= inicio)
    if filtro == "cadastrados":
        # Pacientes que já entraram no Hamilton (têm pk lá).
        return q.where(Conversa.paciente_hamilton_id.isnot(None))
    return q


def _aplicar_busca(q, busca: str):
    """Busca por número, nome do paciente ou texto de qualquer mensagem.

    Número e texto de mensagem são colunas, resolvem no SQL. O nome mora em
    `dados_coletados` (JSON) e não dá pra filtrar de forma portável entre SQLite
    e Postgres, então casamos por `LIKE` no JSON serializado — grosseiro, mas
    suficiente pra uma busca de painel e portável. Falso positivo aqui só faz
    aparecer uma linha a mais, nunca esconde.
    """
    termo = f"%{busca}%"
    msgs = select(Mensagem.conversa_id).where(Mensagem.texto.ilike(termo))
    return q.where(
        or_(
            Conversa.numero_whatsapp.ilike(termo),
            cast(Conversa.dados_coletados, String).ilike(termo),
            Conversa.id.in_(msgs),
        )
    )


def _coluna_de_ordem(ordem: str) -> Any:
    """Coluna SQL da chave de ordenação. Chave desconhecida cai no padrão.

    Nunca interpola `ordem` em SQL (viria da querystring): só resolve contra a
    allowlist `ORDENS`. `nome` mora no JSON `dados_coletados`; ordenar pelo JSON
    serializado agrupa bem, porque o nome é a primeira chave.
    """
    if ordem == "nome":
        return cast(Conversa.dados_coletados, String)
    if ordem in ORDENS:
        return getattr(Conversa, ordem, Conversa.atualizada_em)
    return Conversa.atualizada_em


def _aplicar_ordem(q, ordem: str, descendente: bool):
    coluna = _coluna_de_ordem(ordem)
    return q.order_by(desc(coluna) if descendente else asc(coluna))


async def listar_conversas(
    db: AsyncSession,
    filtro: str = "todas",
    limite: int = 50,
    offset: int = 0,
    busca: str = "",
    ordem: str = "atualizada_em",
    descendente: bool = True,
) -> list[dict]:
    """Lista conversas com preview da última mensagem, filtradas/ordenadas/buscadas.

    `busca` casa número, nome do paciente ou o texto de qualquer mensagem da
    conversa. `ordem` é uma chave de `ORDENS` (o resto cai no padrão). Filtro,
    busca, ordenação e paginação ficam no SQL (a lista cresce com o tempo).
    """
    busca = (busca or "").strip()
    q = _aplicar_filtro(select(Conversa), filtro)
    if busca:
        q = _aplicar_busca(q, busca)
    q = _aplicar_ordem(q, ordem, descendente).limit(limite).offset(offset)
    conversas = (await db.execute(q)).scalars().all()

    resultado = []
    for c in conversas:
        ultima = (
            await db.execute(
                select(Mensagem)
                .where(Mensagem.conversa_id == c.id)
                .order_by(desc(Mensagem.criada_em), desc(Mensagem.id))
                .limit(1)
            )
        ).scalar_one_or_none()
        resultado.append(
            {
                "id": c.id,
                "numero_whatsapp": c.numero_whatsapp,
                "nome": (c.dados_coletados or {}).get("nome_completo"),
                "modo": c.modo,
                "estado": c.estado,
                "paciente_hamilton_id": c.paciente_hamilton_id,
                "hamilton_url": url_hamilton_paciente(c.paciente_hamilton_id),
                "atualizada_em": c.atualizada_em,
                "preview": (ultima.texto[:80] if ultima and ultima.texto else None),
            }
        )
    return resultado


async def obter_conversa(db: AsyncSession, conversa_id: int) -> Conversa | None:
    return await db.get(Conversa, conversa_id)


async def carregar_mensagens(db: AsyncSession, conversa_id: int) -> list[Mensagem]:
    """Mensagens da conversa em ordem cronológica (para exibir no chat)."""
    result = await db.execute(
        select(Mensagem)
        .where(Mensagem.conversa_id == conversa_id)
        .order_by(Mensagem.criada_em, Mensagem.id)
    )
    return list(result.scalars().all())


async def responder_como_thaina(db: AsyncSession, conversa: Conversa, texto: str) -> None:
    """Envia a resposta da Thainá pelo WhatsApp e persiste com origem='thaina'."""
    await whatsapp_client.enviar_texto(conversa.numero_whatsapp, texto)
    await conversation.registrar_mensagem_enviada(db, conversa, texto=texto, origem="thaina")
    await db.commit()


async def assumir(db: AsyncSession, conversa: Conversa) -> None:
    """Thainá assume a conversa: passa para modo humano (bot para de responder)."""
    conversa.modo = "humano"
    await db.commit()


async def devolver_ao_bot(db: AsyncSession, conversa: Conversa) -> None:
    """Encerra o atendimento humano e devolve a conversa ao bot."""
    conversa.modo = "bot"
    await db.commit()


async def excluir_conversa(db: AsyncSession, conversa: Conversa) -> None:
    """Apaga a conversa e tudo ligado a ela (mensagens e escaladas).

    Usada pelo botão "Reiniciar conversa" (teste): como o `numero_whatsapp` é
    único, apagar a conversa libera o número pra começar do zero como paciente
    novo. Apaga os filhos explicitamente (portável: não depende do ON DELETE
    CASCADE do banco, que no SQLite do teste fica desligado por padrão).
    """
    await db.execute(delete(Mensagem).where(Mensagem.conversa_id == conversa.id))
    await db.execute(delete(Escalada).where(Escalada.conversa_id == conversa.id))
    await db.execute(delete(Conversa).where(Conversa.id == conversa.id))
    await db.commit()
