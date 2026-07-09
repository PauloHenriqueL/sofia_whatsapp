"""Sanitização da fala do bot antes de ir pro paciente (rede de proteção).

Por que isto existe (P0 do BACKLOG.md): o modelo tem dois canais de saída,
`tool_calls` (estruturado) e `content` (a fala). Ele pode errar o canal. Em beta
a Sofia mandou pro WhatsApp da paciente o JSON do `cadastrar_paciente` (nome,
nascimento e endereço dela) e, noutra vez, lixo de template
(`@endsection to=final code omitted`).

Nenhuma instrução de prompt garante formato de saída, então a garantia tem que
ser em código, na fronteira. Tudo que o **bot** envia passa por `limpar()`, no
único choke point de saída (`webhook._enviar_em_bolhas`). A resposta da Thainá
(`painel.responder_como_thaina`) **não** passa: é humana, e ela pode legitimamente
escrever chaves ou colar um trecho técnico.

Princípio: **falso negativo é preferível a falso positivo**. Cortar uma fala
legítima da Sofia quebra a conversa; deixar passar um lixo raro é feio mas
recuperável. Por isso o casamento é conservador: só remove o que é claramente
estrutura de dados ou token interno, nunca "texto que parece estranho".

LGPD: o conteúdo removido **nunca** é logado (pode conter dado de saúde). Loga-se
só o motivo e o tamanho.

Limites conhecidos (de propósito, pelo princípio acima):
- Pega JSON com uma chave nossa dentro; não pega estrutura inventada pelo modelo
  com campos que não existem em `tools.py`.
- Não pega JSON aninhado (`{"a": {"b": 1}}`) — nunca vimos, e o casamento simples
  é o que evita falso positivo.
- O contador `bloqueios()` é em memória (zera no restart). O registro permanente
  é o log em WARN.
"""

import json
import logging
import re

logger = logging.getLogger(__name__)

# Quantas vezes a rede de proteção pegou algo, desde que o processo subiu.
# Em memória de propósito: 1 instância (Render free), e o registro permanente é o
# log (WARN). Serve pra Thainá/Paulo verem no painel se o modelo regrediu.
_bloqueios = 0


def bloqueios() -> int:
    """Total de saídas sanitizadas desde o start do processo."""
    return _bloqueios


def zerar_bloqueios() -> None:
    """Só pros testes (o estado é global)."""
    global _bloqueios
    _bloqueios = 0


# Chaves dos schemas em `tools.py`. Uma linha que traga qualquer uma delas em
# formato de campo JSON ("chave":) é estrutura de dados, não fala.
CAMPOS_INTERNOS = (
    "nome_completo",
    "data_nascimento",
    "telefone_contato",
    "telefone_apoio",
    "horarios_disponiveis",
    "motivo_busca",
    "como_conheceu",
    "preferencia_terapeuta",
    "observacoes",
    "endereco",
    "cep",
    "motivo",
    "contexto",
)

_CAMPO_JSON = re.compile(
    r"[\"']({})[\"']\s*:".format("|".join(CAMPOS_INTERNOS)),
    re.IGNORECASE,
)

# Objeto/lista JSON *embutido* numa linha de fala ('Anotei. {"nome_completo":"X"}').
# Exige uma chave conhecida dentro, pra não casar com texto entre chaves.
_JSON_EMBUTIDO = re.compile(
    r"[\{\[]"  # abre
    r"(?=[^\{\[\}\]]*[\"'](?:" + "|".join(CAMPOS_INTERNOS) + r")[\"']\s*:)"  # campo nosso dentro
    r"[^\{\}\[\]]*"  # corpo (sem aninhamento; basta pro nosso caso)
    r"[\}\]]",  # fecha
    re.IGNORECASE,
)

# Lixo de template/protocolo que já vazou ou que vaza em modelos parecidos.
# Removido *inline* (a fala antes dele é preservada).
_TOKENS_INTERNOS = re.compile(
    r"""
      @endsection\b.*$        # '@endsection' e o que vier depois na linha
    | \bto=final\b            # literal que vazou (não `to=` genérico: comeria 'to=email@x')
    | \bcode\s+omitted\b
    | <\|[^>]*\|>             # <|im_end|> e afins
    | ^\s*```.*$              # cercas de bloco de código
    """,
    re.IGNORECASE | re.MULTILINE | re.VERBOSE,
)

# Prefixos que nós mesmos injetamos no histórico (ver conversation.py). Se o
# modelo copiar de volta, não podem sair pro paciente.
_PREFIXOS_NOSSOS = re.compile(
    r"^\s*\[(Aviso do sistema|Thainá, coordenadora clínica)[^\]]*\]:?\s*",
    re.IGNORECASE,
)


def _e_estrutura_de_dados(linha: str) -> bool:
    """True se a linha é JSON/estrutura, e não fala da Sofia.

    Dois sinais, ambos conservadores:
    1. a linha parseia como objeto ou lista JSON; ou
    2. começa com `{`/`[` e traz um campo conhecido do nosso schema.

    Uma frase com chave no meio ("ele disse {isso}") não casa em nenhum dos dois.
    """
    t = linha.strip()
    if not t:
        return False
    if _CAMPO_JSON.search(t) and t[0] in "{[":
        return True
    if t[0] in "{[" and t[-1] in "}]":
        try:
            return isinstance(json.loads(t), (dict, list))
        except (json.JSONDecodeError, ValueError):
            return False
    return False


def limpar(texto: str | None) -> str:
    """Devolve o texto pronto pra enviar; string vazia se nada sobrou.

    Quem chama trata o vazio como "não enviar bolha nenhuma".
    """
    if not texto:
        return ""

    linhas = []
    removeu_estrutura = False
    for linha in texto.splitlines():
        if _e_estrutura_de_dados(linha):
            removeu_estrutura = True
            continue
        linhas.append(linha)

    limpo = "\n".join(linhas)
    # JSON grudado numa linha de fala ('Anotei. {"nome_completo":"X"}'): a linha
    # inteira não é estrutura, mas o trecho é. Tira só o trecho.
    limpo, embutidos = _JSON_EMBUTIDO.subn("", limpo)
    removeu_estrutura = removeu_estrutura or bool(embutidos)
    limpo = _TOKENS_INTERNOS.sub("", limpo)
    limpo = _PREFIXOS_NOSSOS.sub("", limpo)
    # Sobrou linha em branco no lugar do que saiu: colapsa (mas mantém a linha em
    # branco dupla, que é o separador de bolhas).
    limpo = re.sub(r"[ \t]+$", "", limpo, flags=re.MULTILINE)
    limpo = re.sub(r"\n{3,}", "\n\n", limpo).strip()

    if removeu_estrutura or len(limpo) != len(texto.strip()):
        global _bloqueios
        _bloqueios += 1
        motivo = "estrutura_de_dados" if removeu_estrutura else "token_interno"
        # NUNCA logar o conteúdo removido (LGPD: pode conter dado de saúde).
        logger.warning(
            "Saída do modelo sanitizada (motivo=%s, antes=%d chars, depois=%d chars)",
            motivo,
            len(texto),
            len(limpo),
        )
    if not limpo:
        logger.error("Saída do modelo virou vazia após sanitização; nada foi enviado")
    return limpo
