"""Valores de negócio editáveis pela Thainá no painel (preço, parcelas, follow-up).

Ficam na tabela `configuracao` (chave/valor). Um cache em memória evita ler o
banco a cada mensagem; é populado no startup (main.lifespan) e atualizado a cada
salvamento no painel. O Render free roda 1 instância, então o cache em memória
é suficiente; o padrão de cada campo vem das settings (env/código).
"""

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Configuracao

logger = logging.getLogger(__name__)

# chave -> (rótulo pro painel, valor padrão). Ordem = ordem na tela.
CAMPOS: dict[str, tuple[str, int]] = {
    "preco_terapia_mensal": ("Mensalidade da terapia (R$)", settings.preco_terapia_mensal),
    "preco_neuro": ("Orçamento da neuroavaliação (R$)", settings.preco_neuro),
    "parcelas_max": ("Parcelas máximas no cartão", settings.parcelas_max),
    "followup_horas": ("Horas até o follow-up automático (menos de 24)", settings.followup_horas),
}

_cache: dict[str, int] = {chave: padrao for chave, (_, padrao) in CAMPOS.items()}


def valores() -> dict[str, int]:
    """Snapshot dos valores atuais (cópia, pra ninguém mutar o cache por engano)."""
    return dict(_cache)


def valor(chave: str) -> int:
    return _cache.get(chave, CAMPOS[chave][1])


async def carregar_do_banco(db: AsyncSession) -> None:
    """Sobrepõe os padrões com o que estiver salvo no banco. Chamado no startup."""
    rows = (await db.execute(select(Configuracao))).scalars().all()
    for r in rows:
        if r.chave in CAMPOS:
            try:
                _cache[r.chave] = int(r.valor)
            except (TypeError, ValueError):
                logger.warning("Config inválida ignorada: %s=%r", r.chave, r.valor)


async def salvar(db: AsyncSession, novos: dict[str, int]) -> None:
    """Persiste (upsert) os valores informados e atualiza o cache em memória."""
    for chave, valor_novo in novos.items():
        if chave not in CAMPOS:
            continue
        existente = (
            await db.execute(select(Configuracao).where(Configuracao.chave == chave))
        ).scalar_one_or_none()
        if existente:
            existente.valor = str(int(valor_novo))
        else:
            db.add(Configuracao(chave=chave, valor=str(int(valor_novo))))
        _cache[chave] = int(valor_novo)
    await db.commit()
