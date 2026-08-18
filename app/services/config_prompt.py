"""Prompts da Sofia editáveis pela Thainá no painel (`/painel/prompts`).

Os arquivos em `prompt/` são o **padrão**. Se a Thainá editar no painel, o texto
salvo (tabela `configuracao`, mesma dos valores) passa a valer; "Resetar" volta
pro arquivo. Cache em memória (assume 1 instância no Render free), populado no
startup e atualizado a cada salvamento.

O que de fato vai pro modelo (ver `llm_client.carregar_system_prompt`): o prompt
principal (`prompt_sistema`) + a base de conhecimento (`prompt_base`).

⚠️ **`prompt_contrato` não é um prompt.** É o texto do contrato terapêutico que o
paciente **assina** (Demanda E). Ele não vai pro bot e não é referência: o Hamilton
recebe esse texto, troca os `{{MARCADORES}}` pelos dados do paciente e gera o
documento que vai pra assinatura. Ele mora aqui, e não num `.docx` guardado no
Hamilton, porque assim existe **uma fonte só** — duas cópias do mesmo contrato
divergiriam no primeiro ajuste. Por isso o `destino` de cada entrada é declarado:
a tela precisa avisar que editar este texto muda um documento jurídico.
"""

import logging
from functools import lru_cache
from pathlib import Path
from typing import NamedTuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Configuracao

logger = logging.getLogger(__name__)

_DIR = Path(__file__).resolve().parent.parent.parent / "prompt"


class Prompt(NamedTuple):
    """Metadados de um texto editável no painel.

    `NamedTuple` pelo mesmo motivo do `config_negocio.Campo`: os três primeiros
    itens continuam acessíveis por índice, que é como `padrao()` lê o arquivo.

    `destino` diz o que o texto vira, e a tela muda de acordo:
      - "bot"        -> entra no system prompt do modelo;
      - "referencia" -> ninguém consome automaticamente (ex.: a extração);
      - "documento"  -> vira um DOCUMENTO ASSINADO por paciente. Editar erra
                        pra frente, não pra trás (o PDF do que já foi assinado
                        fica guardado no Hamilton), mas ainda é jurídico.
    """

    rotulo: str
    arquivo: Path
    vai_pro_bot: bool
    destino: str = "referencia"


PROMPTS: dict[str, Prompt] = {
    "prompt_sistema": Prompt(
        "Prompt principal (roteiro da conversa)", _DIR / "sofia_v01.txt", True, "bot"
    ),
    "prompt_base": Prompt(
        "Base de conhecimento (respostas a dúvidas)",
        _DIR / "sofia-base-conhecimento.md",
        True,
        "bot",
    ),
    "prompt_contrato": Prompt(
        "Contrato terapêutico (o documento que o paciente assina)",
        _DIR / "contrato-terapeutico-allos.md",
        False,
        "documento",
    ),
    # Pesquisa de satisfação: substitui o prompt principal enquanto a conversa
    # está em modo pesquisa (a pessoa já é paciente, não é um lead a qualificar).
    "prompt_pesquisa": Prompt(
        "Pesquisa: como conduzir (tom e regras)",
        _DIR / "pesquisa-conducao.txt",
        True,
        "bot",
    ),
    # Os quatro roteiros. Qual deles vale é escolhido pelo `momento` da
    # `Avaliacao` no Hamilton (ver services/pesquisa.py).
    "prompt_pesquisa_entrada": Prompt(
        "Pesquisa: perguntas de entrada (antes da primeira sessão)",
        _DIR / "pesquisa-entrada.md",
        True,
        "bot",
    ),
    "prompt_pesquisa_primeira_sessao": Prompt(
        "Pesquisa: perguntas depois da primeira sessão",
        _DIR / "pesquisa-primeira-sessao.md",
        True,
        "bot",
    ),
    "prompt_pesquisa_reencaminhamento": Prompt(
        "Pesquisa: perguntas de troca de terapeuta (reencaminhamento)",
        _DIR / "pesquisa-reencaminhamento.md",
        True,
        "bot",
    ),
    "prompt_pesquisa_encerramento": Prompt(
        "Pesquisa: perguntas de encerramento (alta, desistência, sumiço)",
        _DIR / "pesquisa-encerramento.md",
        True,
        "bot",
    ),
    "prompt_pesquisa_extracao": Prompt(
        "Pesquisa: extração das respostas (não vai pro paciente)",
        _DIR / "pesquisa-extracao.txt",
        False,
        "referencia",
    ),
    # Cobrança da mensalidade (Demanda D): substitui o prompt principal enquanto a
    # conversa está em modo cobrança. Os valores concretos (mensalidade, chave Pix,
    # link do cartão) são injetados em runtime por `cobranca.montar_prompt` — este
    # arquivo é só a condução, e é o que a Thainá edita.
    "prompt_cobranca": Prompt(
        "Cobrança: como falar da mensalidade depois da primeira sessão",
        _DIR / "cobranca.md",
        True,
        "bot",
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
