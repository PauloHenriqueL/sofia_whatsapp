"""Sanitização da fala do bot antes de ir pro paciente (rede de proteção).

Por que isto existe (P0 do docs/demandas/99-backlog-entregue.md): o modelo tem dois canais de saída,
`tool_calls` (estruturado) e `content` (a fala). Ele pode errar o canal. Em beta
fechado (dados fictícios) a Sofia mandou pro WhatsApp o JSON do
`cadastrar_paciente` (nome, nascimento, endereço) e, noutra vez, lixo de template
(`@endsection to=final code omitted`). Com paciente real, isso é dado de saúde.

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


# Texto que foi removido, pra quando saber *o quê* importa mais que *quantos*.
# `None` = desligado, e é o padrão: em produção o trecho removido pode conter nome,
# endereço ou o que a pessoa contou (dado de saúde), e por isso nem o log guarda.
# Só o laboratório liga, porque lá os pacientes são fictícios — e sem isso não dá
# pra distinguir "o modelo degenerou" de "o sanitizador tirou um espaço em branco".
_amostras: list[dict[str, str]] | None = None


def gravar_amostras(ativo: bool) -> None:
    """Liga/desliga o registro do texto removido. NUNCA ligue em produção (LGPD)."""
    global _amostras
    _amostras = [] if ativo else None


def amostras() -> list[dict[str, str]]:
    """O que a sanitização removeu, se o registro estiver ligado."""
    return list(_amostras or ())


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
    "captacao_id",
    "is_parceria",
    "vinculo_parceria",
    "quem_fala",
)
# Deixados DE FORA de propósito: `campo` e `valor`, da tool da pesquisa. São
# palavras comuns demais em português ("o valor: R$ 200") e cortar fala legítima
# é pior que deixar passar um JSON sem dado sensível nenhum.

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
    | \w*assistant\s+to=functions\b.*$
                              # (`\w*` e não `\S*`: `\S*` atravessaria a pontuação e comeria
                              #  a fala legítima antes do lixo)
    | \bto=functions\b.*$
                              # irmão do `to=final`: o cabeçalho do canal de tool call
                              # ('assistant to=functions.escalar_para_thaina') vazando no
                              # texto. Quando isso acontece o modelo degenerou, e o que vem
                              # depois na linha é lixo (spam CJK, 'json_schema', raciocínio
                              # solto), então corta a linha inteira. `functions` é literal:
                              # não come 'to=email@x', que o `to=` genérico comeria.
    | [【】].*$                # colchete CJK. Não existe em fala pt-BR e nas duas vezes que
                              # apareceu veio grudado no lixo acima ('…pra você.】【：】【').
                              # Corta dele em diante.
    | \bcode\s+omitted\b
    | <\|[^>]*\|>             # <|im_end|> e afins
    | ^\s*```.*$              # cercas de bloco de código
    | \[\[[A-Z_]+\]\]         # marcadores internos ([[PESQUISA_CONCLUIDA]] e afins):
                              # sinalizam fim de fluxo pro nosso código e são
                              # removidos antes do envio, mas se o modelo colocar
                              # um no meio da frase (ou numa bolha inesperada) ele
                              # chegaria ao paciente. Aqui é o último filtro.
    """,
    re.IGNORECASE | re.MULTILINE | re.VERBOSE,
)

# Prefixos que nós mesmos injetamos no histórico (ver conversation.py). Se o
# modelo copiar de volta, não podem sair pro paciente.
_PREFIXOS_NOSSOS = re.compile(
    r"^\s*\[(Aviso do sistema|Thainá, coordenadora clínica|Coordenadora clínica)[^\]]*\]:?\s*",
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


# Abaixo disso, repetir é normal ("Oi!", "Certo."): só se mexe em trecho que dá
# uma frase de verdade.
_MIN_CHARS_REPETICAO = 20


def _normalizar(trecho: str) -> str:
    return " ".join(trecho.split()).casefold()


def sem_repeticao(texto: str) -> str:
    """Tira a fala que o modelo escreveu duas vezes na mesma resposta.

    Em 17/11/2026, em produção, a resposta a "Busco terapia" saiu como o
    parágrafo inteiro e, logo abaixo, o mesmo texto já quebrado em bolhas
    ("A. B?\\nA.\\n\\nB?"). O paciente recebeu tudo duas vezes. Nenhum prompt
    garante que isso não volte, então a garantia fica aqui, junto das outras.

    Dois casos, ambos conservadores (só texto IDÊNTICO, ignorando espaços e
    quebras de linha):
    1. a resposta inteira é X seguido de X: fica a segunda cópia, que é a que
       vem com as bolhas separadas;
    2. um bloco (bolha) igual a um bloco anterior: sai a repetição.
    """
    if not texto:
        return texto

    # 1. Resposta dobrada. Procura o ponto em que a segunda metade começa.
    palavras = list(re.finditer(r"\S+", texto))
    n = len(palavras)
    if n >= 4 and n % 2 == 0:
        meio = n // 2
        primeira = [m.group().casefold() for m in palavras[:meio]]
        segunda = [m.group().casefold() for m in palavras[meio:]]
        if primeira == segunda and len(" ".join(primeira)) >= _MIN_CHARS_REPETICAO:
            texto = texto[palavras[meio].start():]

    # 2. Bolha repetida.
    blocos = [b for b in re.split(r"\n\s*\n", texto.strip()) if b.strip()]
    vistos: set[str] = set()
    mantidos = []
    for bloco in blocos:
        chave = _normalizar(bloco)
        if len(chave) >= _MIN_CHARS_REPETICAO and chave in vistos:
            continue
        vistos.add(chave)
        mantidos.append(bloco.strip())
    return "\n\n".join(mantidos)


# Raciocínio do modelo escrito na fala (18/11/2026, em produção, conversa 91): o
# paciente recebeu "Como a pessoa aceitou, solicitar os três dados numa mensagem
# só e perguntar origem [...]. Manter curto pela urgência, sem presumir crise." e,
# na linha de baixo, a fala de verdade. O modelo de produção roda sem raciocínio
# separado (`reasoning_effort=none`, exigido com function tools) e às vezes
# planeja em voz alta. É nota interna sobre o paciente: não pode sair.
#
# Conservador como o resto: só a PRIMEIRA linha, só quando vem fala depois dela,
# só se ela não fala com a pessoa (sem "você"/"te"/"seu") e fala DELA em
# terceira pessoa ou empilha infinitivos de plano ("solicitar… perguntar…").
_SEGUNDA_PESSOA = re.compile(
    r"\b(você|voce|vc|vocês|te|teu|tua|seu|sua|seus|suas|contigo)\b", re.IGNORECASE
)
_TERCEIRA_PESSOA = re.compile(
    r"\b(a pessoa|o paciente|a paciente|o usuário|a usuária|o usuario|a usuaria|o lead|"
    r"a lead|o interlocutor|a interlocutora)\b",
    re.IGNORECASE,
)
_INFINITIVO_DE_PLANO = re.compile(
    r"(?:^|[,.;:]\s*|\b(?:e|depois|então|entao)\s+)"
    r"(solicitar|perguntar|pedir|manter|seguir|responder|oferecer|confirmar|escalar|"
    r"chamar|registrar|cadastrar|coletar|validar|acolher|explicar|reconhecer|evitar|"
    r"retomar|emendar|convidar)\b",
    re.IGNORECASE,
)


def _e_planejamento(linha: str) -> bool:
    if not linha.strip() or _SEGUNDA_PESSOA.search(linha):
        return False
    return bool(_TERCEIRA_PESSOA.search(linha)) or len(_INFINITIVO_DE_PLANO.findall(linha)) >= 2


def sem_planejamento(texto: str) -> str:
    """Tira a linha de raciocínio que o modelo escreveu antes da fala."""
    linhas = texto.strip().splitlines()
    while len(linhas) > 1 and _e_planejamento(linhas[0]):
        linhas = linhas[1:]
        while linhas and not linhas[0].strip():
            linhas = linhas[1:]
    return "\n".join(linhas)


# --- Nome de quem atende ------------------------------------------------------
#
# Decisão do Victor (27/11/2026): a Sofia não diz o nome de ninguém da equipe ao
# paciente. A Thainá é "a coordenadora clínica". O roteiro e as falas fixas já não
# trazem o nome, mas o modelo ainda o encontra (a própria coordenadora se apresenta
# no painel, o paciente escreve "quero falar com a Thainá", a ferramenta se chama
# `escalar_para_thaina`), e regra só no roteiro este projeto já viu o modelo
# ignorar. Então a troca é garantida aqui.
#
# Só a Thainá: "Amanda" é nome comum de paciente, e trocar "Oi, Amanda" por "Oi, a
# diretora de neuropsicologia" seria pior que o defeito. Pelo mesmo motivo a troca
# é pulada quando a própria pessoa se chama Thainá.
_NOME_COORDENADORA = re.compile(
    r"(?P<prep>\b(?:de|a|à|da|na|pela|pra|nossa)\s+)?\bThain[áa]\b", re.IGNORECASE
)
# Depois da troca, "a Thainá, nossa coordenadora clínica," vira "a coordenadora,
# nossa coordenadora clínica,": o aposto passa a repetir o título.
_APOSTO_REPETIDO = re.compile(
    r"coordenadora\s*(?:,\s*|\(\s*)(?:a\s+|nossa\s+)?coordenadora"
    r"(?!\s+de\b)(?P<clinica>\s+cl[ií]nica)?(?:\s+da\s+(?:Allos|nossa\s+cl[ií]nica))?\s*[,)]?",
    re.IGNORECASE,
)
_trocas_de_nome = 0


def trocas_de_nome() -> int:
    """Quantas falas tiveram o nome da coordenadora trocado desde o start."""
    return _trocas_de_nome


def sem_nome_da_equipe(texto: str, nome_da_pessoa: str | None = None) -> str:
    """Troca "Thainá" por "a coordenadora" na fala que vai pro paciente."""
    global _trocas_de_nome
    if not texto or not _NOME_COORDENADORA.search(texto):
        return texto
    if nome_da_pessoa and "thain" in _normalizar(nome_da_pessoa).replace("á", "a"):
        return texto

    def trocar(m: re.Match) -> str:
        prep = m.group("prep")
        if prep:
            # "de Thainá" não tem artigo; o resto já traz o seu.
            return "da coordenadora" if prep.strip().lower() == "de" else f"{prep}coordenadora"
        antes = texto[: m.start()].rstrip(" \t")
        inicio_de_frase = not antes or antes[-1] in ".!?:\n"
        return "A coordenadora" if inicio_de_frase else "a coordenadora"

    novo = _NOME_COORDENADORA.sub(trocar, texto)
    novo = _APOSTO_REPETIDO.sub(
        lambda m: "coordenadora" + (m.group("clinica") or " clínica"), novo
    )
    novo = re.sub(r"[ \t]{2,}", " ", novo)
    novo = re.sub(r"[ \t]+([,.!?])", r"\1", novo)
    _trocas_de_nome += 1
    # Sem o texto no log (LGPD).
    logger.warning("Saída do modelo citava a coordenadora pelo nome; trocado pelo título")
    return novo


def limpar(texto: str | None) -> str:
    """Devolve o texto pronto pra enviar; string vazia se nada sobrou.

    Quem chama trata o vazio como "não enviar bolha nenhuma".
    """
    global _bloqueios
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

    # Colapsar espaço em branco NÃO é sanitização. Comparar o limpo com o texto cru
    # contava "tirei dois espaços no fim da linha" como se fosse vazamento: na rodada
    # de 27 conversas do laboratório (07/08/2026), 27 de 27 "bloqueios" eram só isso,
    # e esse número aparece no painel como sinal de que o modelo regrediu. Compara-se
    # com o original já normalizado, pra sobrar só o que de fato foi removido.
    normalizado = re.sub(r"[ \t]+$", "", texto, flags=re.MULTILINE)
    normalizado = re.sub(r"\n{3,}", "\n\n", normalizado).strip()

    if removeu_estrutura or limpo != normalizado:
        _bloqueios += 1
        motivo = "estrutura_de_dados" if removeu_estrutura else "token_interno"
        if _amostras is not None:  # laboratório only; ver `gravar_amostras`
            _amostras.append({"motivo": motivo, "antes": texto, "depois": limpo})
        # NUNCA logar o conteúdo removido (LGPD: pode conter dado de saúde).
        logger.warning(
            "Saída do modelo sanitizada (motivo=%s, antes=%d chars, depois=%d chars)",
            motivo,
            len(texto),
            len(limpo),
        )

    # Fala repetida, à parte do resto: é outro defeito (o modelo se repetiu, não
    # vazou estrutura) e só troca o texto quando algo de fato saiu — senão a
    # reformatação dos blocos contaria como bloqueio.
    sem_rep = sem_repeticao(limpo)
    if _normalizar(sem_rep) != _normalizar(limpo):
        _bloqueios += 1
        if _amostras is not None:
            _amostras.append({"motivo": "repeticao", "antes": limpo, "depois": sem_rep})
        logger.warning(
            "Saída do modelo com fala repetida; repetição removida (antes=%d chars, depois=%d chars)",
            len(limpo),
            len(sem_rep),
        )
        limpo = sem_rep

    sem_plano = sem_planejamento(limpo) if limpo else limpo
    if _normalizar(sem_plano) != _normalizar(limpo):
        _bloqueios += 1
        if _amostras is not None:
            _amostras.append({"motivo": "planejamento", "antes": limpo, "depois": sem_plano})
        # Sem conteúdo no log: a nota fala do paciente (LGPD).
        logger.warning(
            "Saída do modelo com raciocínio na fala; linha removida (antes=%d chars, depois=%d chars)",
            len(limpo),
            len(sem_plano),
        )
        limpo = sem_plano
    if not limpo:
        logger.error("Saída do modelo virou vazia após sanitização; nada foi enviado")
    return limpo
