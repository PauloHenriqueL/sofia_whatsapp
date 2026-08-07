"""Cadastro do paciente no Hamilton. Usado pelo bot (tool) e pelo painel (botão).

Garante um telefone de contato válido (cai pro número do WhatsApp da conversa
quando a IA não coletou um número de verdade) e faz busca-antes-de-criar.
"""

import logging
import re
import unicodedata
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Conversa
from app.services import hamilton_client

logger = logging.getLogger(__name__)


def _normalizar_nome(nome: str | None) -> str:
    """Minúsculas, sem acento e espaços colapsados — pra comparar nomes."""
    n = unicodedata.normalize("NFKD", nome or "")
    n = "".join(c for c in n if not unicodedata.combining(c))
    return " ".join(n.lower().split())


def _match_por_nome(existentes: list[dict], nome: str | None) -> dict | None:
    """Entre os pacientes do MESMO telefone, acha um com o mesmo nome (mesma pessoa).

    Se o nome coletado bate com um existente, é a mesma pessoa voltando. Se não bate
    (ex.: o pai cadastrando o filho), é paciente novo — cria em vez de linkar.
    """
    alvo = _normalizar_nome(nome)
    if not alvo:
        return None
    for p in existentes:
        if _normalizar_nome(p.get("nome")) == alvo:
            return p
    return None


def _garantir_telefone(conversa: Conversa, dados: dict) -> dict:
    """Se o telefone de contato coletado for inválido/placeholder, usa o número
    do WhatsApp de onde o paciente está falando (sempre temos esse)."""
    dados = dict(dados or {})
    tel = hamilton_client.normalizar_telefone(dados.get("telefone_contato"))
    if not re.fullmatch(r"\d{10,11}", tel or ""):
        dados["telefone_contato"] = conversa.numero_whatsapp
    return dados


def _marcar_cadastro(conversa: Conversa) -> None:
    """Carimba quando o cadastro deu certo (ancora da pesquisa de linha de base).

    So na primeira vez: um reencontro meses depois nao pode reabrir a janela
    de baseline de alguem que ja e paciente ha tempos.
    """
    if conversa.cadastrado_em is None:
        conversa.cadastrado_em = datetime.now(timezone.utc)


async def cadastrar_paciente(db: AsyncSession, conversa: Conversa) -> dict:
    """Tenta cadastrar a conversa no Hamilton (busca antes de criar).

    Atualiza `conversa.estado` e `conversa.paciente_hamilton_id`. Não comita
    (quem chama decide). Devolve o resultado (status + paciente_id/erro).
    """
    dados = _garantir_telefone(conversa, conversa.dados_coletados or {})
    conversa.dados_coletados = dados  # persiste o telefone corrigido

    client = hamilton_client.get_hamilton_client()
    try:
        existentes = await client.buscar_paciente_por_telefone(dados.get("telefone_contato"))
        mesmo = _match_por_nome(existentes, dados.get("nome_completo"))
        if mesmo:
            # Mesma pessoa voltando: linka e atualiza a ficha (não trava se falhar).
            pid = mesmo.get("pk_paciente")
            conversa.paciente_hamilton_id = pid
            conversa.estado = "cadastrado"
            _marcar_cadastro(conversa)
            atualizacao = hamilton_client.mapear_dados_update(dados, mesmo)
            if atualizacao:
                try:
                    await client.atualizar_paciente(pid, atualizacao)
                except hamilton_client.HamiltonError as exc:
                    logger.warning("Não atualizei o paciente %s no reencontro: %s", pid, exc)
            await db.flush()
            return {"status": "atualizado", "paciente_id": pid}

        # Telefone novo OU nome diferente no mesmo telefone (ex.: pai cadastrando
        # o filho) -> é um paciente novo, cria um registro.
        criado = await client.criar_paciente(dados)
        conversa.paciente_hamilton_id = criado.get("pk_paciente")
        conversa.estado = "cadastrado"
        _marcar_cadastro(conversa)
        await db.flush()
        return {"status": "cadastrado", "paciente_id": conversa.paciente_hamilton_id}
    except hamilton_client.HamiltonError as exc:
        logger.error(f"Hamilton falhou no cadastro da conversa {conversa.id}: {exc}")
        conversa.estado = "cadastro_pendente"
        await db.flush()
        return {"status": "cadastro_pendente", "erro": str(exc)}
