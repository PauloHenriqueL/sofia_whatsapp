"""Valores editáveis pela Thainá no painel (preço, follow-up, presença, debounce).

Ficam na tabela `configuracao` (chave/valor texto). Um cache em memória evita ler
o banco a cada mensagem; é populado no startup (main.lifespan) e atualizado a
cada salvamento no painel. O Render free roda 1 instância, então o cache em
memória basta; o padrão de cada campo vem das settings (env/código), e o valor
salvo no painel tem prioridade sobre o env.
"""

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Configuracao

logger = logging.getLogger(__name__)

# chave -> (rótulo pro painel, valor padrão, tipo "int"|"bool"). Ordem = ordem na tela.
CAMPOS: dict[str, tuple[str, object, str]] = {
    "preco_terapia_mensal": ("Mensalidade da terapia (R$)", settings.preco_terapia_mensal, "int"),
    "preco_neuro": ("Orçamento da neuroavaliação (R$)", settings.preco_neuro, "int"),
    "parcelas_max": ("Parcelas máximas no cartão", settings.parcelas_max, "int"),
    "followup_horas": (
        "Horas até o follow-up automático (menos de 24)",
        settings.followup_horas,
        "int",
    ),
    "debounce_segundos": (
        "Segundos de espera antes de responder (junta mensagens em rajada)",
        int(settings.debounce_segundos),
        "int",
    ),
    "simular_digitacao": (
        "Mostrar 'digitando…' e o visto (tiques azuis) pro paciente",
        settings.simular_digitacao,
        "bool",
    ),
    "transcrever_audio": (
        "Ouvir e entender áudios do paciente (transcrever e responder em texto)",
        settings.transcrever_audio,
        "bool",
    ),
}

_cache: dict[str, object] = {chave: padrao for chave, (_, padrao, _t) in CAMPOS.items()}


def _tipo(chave: str) -> str:
    return CAMPOS[chave][2]


def _parse(chave: str, texto: str):
    """Converte o texto guardado no banco pro tipo do campo."""
    if _tipo(chave) == "bool":
        return str(texto).strip().lower() in ("true", "1", "sim", "on")
    return int(texto)


def _serialize(chave: str, valor) -> str:
    """Converte o valor pro texto que vai pro banco."""
    if _tipo(chave) == "bool":
        return "true" if valor else "false"
    return str(int(valor))


def valores() -> dict[str, object]:
    """Snapshot dos valores atuais (cópia, pra ninguém mutar o cache por engano)."""
    return dict(_cache)


def valor(chave: str):
    return _cache.get(chave, CAMPOS[chave][1])


async def carregar_do_banco(db: AsyncSession) -> None:
    """Sobrepõe os padrões com o que estiver salvo no banco. Chamado no startup."""
    rows = (await db.execute(select(Configuracao))).scalars().all()
    for r in rows:
        if r.chave in CAMPOS:
            try:
                _cache[r.chave] = _parse(r.chave, r.valor)
            except (TypeError, ValueError):
                logger.warning("Config inválida ignorada: %s=%r", r.chave, r.valor)


async def salvar(db: AsyncSession, novos: dict) -> None:
    """Persiste (upsert) os valores informados e atualiza o cache em memória."""
    for chave, valor_novo in novos.items():
        if chave not in CAMPOS:
            continue
        texto = _serialize(chave, valor_novo)
        existente = (
            await db.execute(select(Configuracao).where(Configuracao.chave == chave))
        ).scalar_one_or_none()
        if existente:
            existente.valor = texto
        else:
            db.add(Configuracao(chave=chave, valor=texto))
        _cache[chave] = _parse(chave, texto)
    await db.commit()
