"""Cadastro do paciente no Hamilton. Usado pelo bot (tool) e pelo painel (botão).

Garante um telefone de contato válido (cai pro número do WhatsApp da conversa
quando a IA não coletou um número de verdade) e faz busca-antes-de-criar.
"""

import logging
import re

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Conversa
from app.services import hamilton_client

logger = logging.getLogger(__name__)


def _garantir_telefone(conversa: Conversa, dados: dict) -> dict:
    """Se o telefone de contato coletado for inválido/placeholder, usa o número
    do WhatsApp de onde o paciente está falando (sempre temos esse)."""
    dados = dict(dados or {})
    tel = hamilton_client.normalizar_telefone(dados.get("telefone_contato"))
    if not re.fullmatch(r"\d{10,11}", tel or ""):
        dados["telefone_contato"] = conversa.numero_whatsapp
    return dados


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
        if existentes:
            pid = existentes[0].get("pk_paciente")
            conversa.paciente_hamilton_id = pid
            conversa.estado = "cadastrado"
            await db.flush()
            return {"status": "ja_cadastrado", "paciente_id": pid}

        criado = await client.criar_paciente(dados)
        conversa.paciente_hamilton_id = criado.get("pk_paciente")
        conversa.estado = "cadastrado"
        await db.flush()
        return {"status": "cadastrado", "paciente_id": conversa.paciente_hamilton_id}
    except hamilton_client.HamiltonError as exc:
        logger.error(f"Hamilton falhou no cadastro da conversa {conversa.id}: {exc}")
        conversa.estado = "cadastro_pendente"
        await db.flush()
        return {"status": "cadastro_pendente", "erro": str(exc)}
