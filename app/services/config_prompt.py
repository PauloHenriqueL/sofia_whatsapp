"""Prompts da Sofia editáveis pela Thainá no painel (`/painel/prompts`).

Os arquivos em `prompt/` são o **padrão**. Se a Thainá editar no painel, o texto
salvo (tabela `configuracao`, mesma dos valores) passa a valer; "Resetar" volta
pro arquivo. Cache em memória (assume 1 instância no Render free), populado no
startup e atualizado a cada salvamento.

O que de fato vai pro modelo (ver `llm_client.carregar_system_prompt`): o prompt
principal (`prompt_sistema`) + a base de conhecimento (`prompt_base`). O contrato
(`prompt_contrato`) é só referência interna — editável aqui, mas NÃO enviado ao bot.
"""

import logging
from functools import lru_cache
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Configuracao

logger = logging.getLogger(__name__)

_DIR = Path(__file__).resolve().parent.parent.parent / "prompt"

# chave -> (rótulo pro painel, arquivo padrão, vai_pro_bot)
PROMPTS: dict[str, tuple[str, Path, bool]] = {
    "prompt_sistema": ("Prompt principal (roteiro da conversa)", _DIR / "sofia_v01.txt", True),
    "prompt_base": (
        "Base de conhecimento (respostas a dúvidas)",
        _DIR / "sofia-base-conhecimento.md",
        True,
    ),
    "prompt_contrato": (
        "Contrato terapêutico (referência interna — NÃO vai pro bot)",
        _DIR / "contrato-terapeutico-allos.md",
        False,
    ),
}

# Só as chaves customizadas (override do arquivo). Ausência = usa o padrão do arquivo.
_cache: dict[str, str] = {}


@lru_cache(maxsize=8)
def _ler_arquivo(caminho: Path) -> str:
    try:
        return caminho.read_text(encoding="utf-8").strip()
    except OSError:
        logger.exception("Não consegui ler o prompt padrão %s", caminho)
        return ""


def padrao(chave: str) -> str:
    """Texto padrão (o arquivo em `prompt/`). Vazio se o arquivo faltar."""
    return _ler_arquivo(PROMPTS[chave][1])


def texto(chave: str) -> str:
    """Texto atual: o override salvo no painel, ou o padrão do arquivo."""
    override = _cache.get(chave)
    return override if override is not None else padrao(chave)


def customizado(chave: str) -> bool:
    """True se a Thainá salvou uma versão própria (diferente do arquivo)."""
    return chave in _cache


async def carregar_do_banco(db: AsyncSession) -> None:
    """Carrega os overrides salvos pro cache. Chamado no startup."""
    rows = (
        (await db.execute(select(Configuracao).where(Configuracao.chave.in_(PROMPTS))))
        .scalars()
        .all()
    )
    for r in rows:
        _cache[r.chave] = r.valor


async def salvar(db: AsyncSession, chave: str, valor: str) -> None:
    """Salva o texto editado (upsert) e atualiza o cache."""
    if chave not in PROMPTS:
        return
    existente = (
        await db.execute(select(Configuracao).where(Configuracao.chave == chave))
    ).scalar_one_or_none()
    if existente:
        existente.valor = valor
    else:
        db.add(Configuracao(chave=chave, valor=valor))
    _cache[chave] = valor
    await db.commit()


async def resetar(db: AsyncSession, chave: str) -> None:
    """Volta pro padrão: apaga o override do banco e do cache."""
    if chave not in PROMPTS:
        return
    await db.execute(Configuracao.__table__.delete().where(Configuracao.chave == chave))
    _cache.pop(chave, None)
    await db.commit()
