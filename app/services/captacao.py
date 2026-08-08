"""Origem do paciente (captação): a lista do Hamilton, oferecida ao modelo.

A Sofia é o **canal** por onde a pessoa chega, não a **origem** dela. Até aqui
todo cadastro ia pro Hamilton com a captação fixa "WhatsApp (Sofia)", o que
apagava a informação de onde o paciente realmente veio (Instagram, indicação,
uma prefeitura conveniada). Agora a Sofia pergunta na conversa, o modelo escolhe
uma origem da lista real do Hamilton, e o ID escolhido é **validado aqui** antes
de virar cadastro.

Duas garantias que este módulo dá:

1. **Modelo não inventa ID.** `resolver` só devolve um ID que existe na lista
   carregada do Hamilton. Um ID alucinado vira "não identificado", não um
   cadastro torto.
2. **Parceria não se declara pelo nome.** Se a captação é de um órgão parceiro
   (prefeitura/convênio) quem diz é a flag `is_parceria` que vem do Hamilton —
   não uma lista fixa aqui nem o nome da captação.

O cache é em memória e vale enquanto o processo viver (o Render free roda uma
instância só). Captação nova cadastrada no Hamilton aparece no próximo restart
ou quando o cache expira; a lista muda raríssimo.
"""

import logging
import time

from app.services import hamilton_client

logger = logging.getLogger(__name__)

# Quanto tempo a lista fica em cache antes de ser buscada de novo. Captação é
# tabela de apoio, muda muito pouco; uma hora evita ida à rede a cada turno sem
# obrigar restart pra ver uma origem nova.
TTL_SEGUNDOS = 3600

_cache: list[dict] = []
_carregado_em: float = 0.0


def _expirado() -> bool:
    return (time.monotonic() - _carregado_em) > TTL_SEGUNDOS


async def listar(forcar: bool = False) -> list[dict]:
    """Captações ativas do Hamilton (com cache).

    Se o Hamilton estiver fora do ar, devolve o que estiver em cache (mesmo
    vencido) em vez de estourar: sem a lista a Sofia ainda consegue cadastrar,
    só sem identificar a origem — degradar é melhor que travar o cadastro.
    """
    global _cache, _carregado_em
    if _cache and not forcar and not _expirado():
        return _cache
    try:
        captacoes = await hamilton_client.get_hamilton_client().listar_captacoes()
        # Filtra de novo aqui, e não só no cliente: captação desativada não pode
        # ser oferecida ao modelo (o Hamilton recusaria o cadastro depois, e a
        # pessoa é quem pagaria o pato). Barato, e não depende do endpoint
        # continuar filtrando do outro lado.
        _cache = [c for c in captacoes if c.get("is_active", True)]
        _carregado_em = time.monotonic()
    except hamilton_client.HamiltonError as exc:
        logger.warning("Não consegui listar as captações do Hamilton: %s", exc)
    return _cache


def limpar() -> None:
    """Zera o cache (usado em teste e após mudança de configuração)."""
    global _cache, _carregado_em
    _cache = []
    _carregado_em = 0.0


def _id(captacao: dict):
    return captacao.get("pk_captacao") or captacao.get("id")


def resolver(captacao_id, captacoes: list[dict]) -> dict | None:
    """Devolve a captação daquele ID, ou None se ele não existir na lista.

    É a allowlist: o ID vem de um LLM, então "não está na lista" tem que
    significar "não identificado", nunca "manda assim mesmo".
    """
    if captacao_id in (None, "", 0):
        return None
    try:
        alvo = int(captacao_id)
    except (TypeError, ValueError):
        return None
    for c in captacoes:
        if _id(c) == alvo:
            return c
    return None


# Nome da captação usada quando a origem não foi identificada. Existe no Hamilton
# (ID 4 hoje), mas é resolvida por NOME de propósito: o ID varia entre a base de
# produção e a branch de teste, e cravá-lo aqui gravaria a captação errada em um
# dos dois ambientes sem ninguém perceber.
NOME_NAO_SEI = "Não sei"


def nao_sei(captacoes: list[dict]) -> dict | None:
    """A captação "Não sei" da lista do Hamilton, se existir.

    É o destino de quem chegou sem origem identificada. Gravar isso é diferente
    de deixar em branco: em branco não dá pra distinguir "a pessoa não soube
    dizer" de "ninguém perguntou", e o relatório da ONG trata os dois igual.
    """
    alvo = NOME_NAO_SEI.strip().casefold()
    for c in captacoes:
        if str(c.get("nome") or "").strip().casefold() == alvo:
            return c
    return None


def e_parceria(captacao: dict | None) -> bool:
    """Se a origem é de um órgão parceiro (o atendimento é custeado por ele)."""
    return bool(captacao and captacao.get("is_parceria"))


def parcerias(captacoes: list[dict]) -> list[dict]:
    return [c for c in captacoes if e_parceria(c)]


def linhas_para_prompt(captacoes: list[dict]) -> str:
    """Renderiza a lista de origens pro system prompt, uma por linha.

    As de parceria são marcadas: o modelo precisa saber quais convênios existem
    pra distinguir "prefeitura conveniada" de "prefeitura que a gente não atende".
    """
    if not captacoes:
        return "(lista indisponível agora — omita o campo captacao_id)"
    linhas = []
    for c in captacoes:
        marca = " [PARCERIA/CONVÊNIO]" if e_parceria(c) else ""
        linhas.append(f"- {_id(c)}: {c.get('nome')}{marca}")
    return "\n".join(linhas)
