"""Camada determinística: o que dá pra contar, contado sem opinião.

Isto é **piso, não veredito**. A métrica não sabe se a conversa foi boa; ela sabe
que a primeira bolha teve 940 caracteres, que a Sofia falou 6 vezes mais que a
pessoa e que "faz sentido?" apareceu três vezes. Serve pra duas coisas: apontar
onde o leitor deve olhar, e detectar regressão barato entre rodadas.

Quase tudo aqui é transcrição literal de uma regra que o próprio prompt já
escreve (`prompt/sofia_v01.txt`, seção "Palavras que você usa" e "Evite no seu
texto"). Onde a regra não é contável sem julgamento — "isso soou robótico?",
"chamou terapia de conversa?" — a métrica **não** tenta adivinhar: ela expõe o
número cru e deixa a acusação pro leitor. Métrica que chuta intenção vira ruído
com aparência de rigor.
"""

import re
import statistics
import unicodedata

# Violação objetiva: a regra do prompt é literal e o casamento é seguro.
TERMOS_PROIBIDOS = {
    "abertura_animada": r"\b(Perfeito|Ótimo|Otimo|Claro|Que bom|Com certeza|Maravilha)\s*!",
    "travessao": r"—",
    "bate_papo": r"\b(bate[- ]papo|papinho)\b",
    "jargao_clinico": r"\b(processo terapêutico|acolhimento|a demanda)\b",
    "termo_interno": r"\b(cadastro no sistema|no sistema|escalar|modo humano|registro pendente)\b",
    "terapeuta_nao_formado": r"\b(não formad|nao formad|não graduad|nao graduad)",
    "lista_numerada": r"(?m)^\s*\d+[.)]\s+\S",
    # O WhatsApp usa *asterisco simples* pra negrito. `**assim**` não renderiza:
    # a pessoa lê os asteriscos literais no meio da frase.
    "markdown_negrito_duplo": r"\*\*[^*\n]+\*\*",
}

# Sinal ambíguo: conta, mas não acusa. Vira pergunta pro leitor.
TERMOS_OLHAR = {
    "palavra_conversa": r"\buma conversa\b",
    "faz_sentido": r"\bfaz sentido\b",
    "supervis": r"\bsupervis",
    "preco": r"R\$|\bmensalidade\b|\bvalor\b",
}

EMOJI = re.compile("[\U0001f300-\U0001faff\U00002600-\U000027bf\U0001f1e6-\U0001f1ff⭐❤]")


def _sem_acento(t: str) -> str:
    n = unicodedata.normalize("NFKD", t.lower())
    return "".join(c for c in n if not unicodedata.combining(c))


def _p90(valores: list[int]) -> int:
    if not valores:
        return 0
    if len(valores) < 10:
        return max(valores)
    return int(statistics.quantiles(valores, n=10)[-1])


def _ngramas_repetidos(falas: list[str], n: int = 6) -> list[dict]:
    """Trechos de `n` palavras que a Sofia repetiu em bolhas diferentes.

    É o detector do checkpoint repetido e da frase-muleta — o "nunca repita o
    mesmo checkpoint duas vezes" da linha 62 do prompt, contado. Só conta
    repetição entre bolhas distintas: repetir dentro da mesma bolha é raro e
    quase sempre coincidência de conectivo.
    """
    vistos: dict[str, set[int]] = {}
    for i, fala in enumerate(falas):
        palavras = _sem_acento(re.sub(r"[^\w\s]", " ", fala)).split()
        for j in range(len(palavras) - n + 1):
            vistos.setdefault(" ".join(palavras[j : j + n]), set()).add(i)
    repetidos = [(t, sorted(b)) for t, b in vistos.items() if len(b) > 1]
    # Sobrepostos geram muito trecho quase igual; fica o mais longo de cada raiz.
    repetidos.sort(key=lambda x: (-len(x[1]), -len(x[0])))
    saida, cobertos = [], []
    for trecho, bolhas in repetidos:
        if any(trecho in c for c in cobertos):
            continue
        cobertos.append(trecho)
        saida.append({"trecho": trecho, "bolhas": bolhas, "vezes": len(bolhas)})
    return saida[:8]


# Frases em que a Sofia AFIRMA ter feito (ou que vai fazer agora) algo que só
# existe via tool call. Não é "vou explicar" nem "posso te ajudar": é ação com
# efeito fora da conversa — registrar no Hamilton, acionar a Thainá.
_PROMESSAS_DE_ACAO = re.compile(
    r"(vou|já|ja)\s+(registrar|cadastrar|deixar registrad|deixei registrad|anotar no|"
    r"chamar a thain|passar (isso |o seu caso )?pra thain|acionar)"
    r"|ficou tudo anotado|deixei tudo (registrado|anotado)|cadastro .{0,20}finalizado"
    r"|pronto, (anotei|ficou|deixei)",
    re.IGNORECASE,
)


def _prometeu_sem_agir(bolhas: list[str], tools: list[str]) -> list[str]:
    """Frases que afirmam uma ação numa conversa em que NENHUMA tool foi chamada.

    Lista vazia é o normal. Qualquer item é um caso pra abrir a transcrição.
    """
    if tools:
        return []
    return [b.strip() for b in bolhas if _PROMESSAS_DE_ACAO.search(_sem_acento(b))][:3]


# Até que turno uma escalada é "precoce". A rubrica (E6) diz que o caso mais caro
# é escalar no PRIMEIRO turno, "deixando a pessoa sem preço, sem funcionamento e
# sem próximo passo". Dois turnos é a margem: dá pra pessoa falar uma vez e a
# Sofia responder uma vez antes de decidir que não é com ela.
TURNO_ESCALADA_PRECOCE = 2


def _escaladas_com_turno(turnos: list[dict]) -> list[dict]:
    """Cada escalada com o turno em que aconteceu e o motivo declarado.

    Existe porque a contagem de tool calls não distingue **escalar porque era o
    certo** de **escalar porque desistiu** — e os dois somam igual em
    "conversas que agiram". Medido em 17/08: o gpt-5.6-luna escalou 7 de 27
    conversas, quase todas no turno 2 e com motivo `outro`, em casos que o
    gpt-5.6-terra resolveu 3 de 3 (prefeitura conveniada, mãe de menor de idade,
    pessoa em sofrimento pedindo pra começar). O balde `outro` é onde a desistência
    se esconde: é o motivo que não compromete com nada.
    """
    achados = []
    for i, t in enumerate(turnos, start=1):
        for tc in t.get("tool_calls", []):
            if tc["nome"] == "escalar_para_thaina":
                achados.append(
                    {
                        "turno": i,
                        "motivo": (tc.get("argumentos") or {}).get("motivo", "?"),
                        "precoce": i <= TURNO_ESCALADA_PRECOCE,
                    }
                )
    return achados


def _marco(turnos: list[dict], teste) -> int | None:
    for t in turnos:
        if any(teste(b) for b in t.get("sofia", [])):
            return t["n"]
    return None


def calcular(conversa: dict) -> dict:
    """Métricas de UMA conversa. Não julga; conta."""
    turnos = conversa.get("turnos", [])
    bolhas = [b for t in turnos for b in t.get("sofia", [])]
    falas_paciente = [p for t in turnos for p in t.get("paciente", [])]

    chars = [len(b) for b in bolhas]
    linhas = [b.count("\n") + 1 for b in bolhas]
    chars_sofia = sum(chars)
    chars_paciente = sum(len(p) for p in falas_paciente)

    texto_sofia = "\n".join(bolhas)
    proibidos = {}
    for nome, padrao in TERMOS_PROIBIDOS.items():
        achados = re.findall(padrao, texto_sofia, re.IGNORECASE)
        if achados:
            proibidos[nome] = len(achados)
    if EMOJI.search(texto_sofia):
        proibidos["emoji"] = len(EMOJI.findall(texto_sofia))
    excesso_exclamacao = sum(1 for b in bolhas if b.count("!") > 1)
    if excesso_exclamacao:
        proibidos["mais_de_uma_exclamacao"] = excesso_exclamacao

    olhar = {
        nome: len(re.findall(padrao, texto_sofia, re.IGNORECASE))
        for nome, padrao in TERMOS_OLHAR.items()
    }
    olhar = {k: v for k, v in olhar.items() if v}

    primeiro_nome = ""
    ident = (conversa.get("_persona_identidade") or {}).get("nome_completo", "")
    if ident:
        primeiro_nome = ident.split()[0]

    tools = [tc["nome"] for t in turnos for tc in t.get("tool_calls", [])]

    return {
        # 🔴 Ela DISSE que fez, e não fez.
        #
        # O defeito mais caro que este laboratório já produziu, e o único que
        # não aparecia em métrica nenhuma: a Sofia narra a ação no texto
        # ("vou registrar", "já deixei anotado", "vou chamar a Thainá") e **não
        # emite a tool call**. A conversa parece perfeita; a pessoa vai embora
        # achando que foi cadastrada ou que alguém vai retornar, e não há
        # registro em lugar nenhum. Do lado de fora é indistinguível de mentira.
        #
        # Visto duas vezes em 17/08 — `ja-foi-paciente` e `mae-do-adolescente` —
        # as duas nas conversas mais LONGAS da rodada. Só foi percebido porque
        # alguém abriu o JSON à mão.
        #
        # Isto não julga: conta. Falso positivo existe (ela pode dizer "vou
        # registrar" e chamar a tool no turno seguinte, que é o certo), por isso
        # a conta só acusa quando **nenhuma** tool foi chamada na conversa
        # inteira.
        "prometeu_e_nao_fez": _prometeu_sem_agir(bolhas, tools),
        "turnos": len(turnos),
        "motivo_parada": conversa.get("motivo_parada"),
        # O sintoma nº 1 relatado: a primeira coisa que a pessoa recebe.
        "primeira_bolha_chars": chars[0] if chars else 0,
        "primeiro_turno_chars": sum(len(b) for b in (turnos[0]["sofia"] if turnos else [])),
        "bolhas_total": len(bolhas),
        "bolhas_por_turno_media": round(len(bolhas) / len(turnos), 1) if turnos else 0,
        "bolhas_por_turno_max": max((len(t.get("sofia", [])) for t in turnos), default=0),
        "chars_por_bolha_media": int(statistics.fmean(chars)) if chars else 0,
        "chars_por_bolha_p90": _p90(chars),
        "chars_por_bolha_max": max(chars, default=0),
        "linhas_por_bolha_max": max(linhas, default=0),
        # Despejo: se a Sofia escreve muito mais do que a pessoa, é monólogo.
        "razao_volume_sofia_paciente": (
            round(chars_sofia / chars_paciente, 1) if chars_paciente else None
        ),
        "perguntas_por_bolha_max": max((b.count("?") for b in bolhas), default=0),
        "bolhas_com_2mais_perguntas": sum(1 for b in bolhas if b.count("?") > 1),
        "nome_repetido_em_bolhas": (
            sum(1 for b in bolhas if primeiro_nome and primeiro_nome.lower() in b.lower())
        ),
        "termos_proibidos": proibidos,
        "sinais_para_olhar": olhar,
        "repeticoes": _ngramas_repetidos(bolhas),
        "turno_primeiro_preco": _marco(turnos, lambda b: bool(re.search(r"R\$", b))),
        "tools_chamadas": tools,
        # Escalar cedo demais é E6 na rubrica, e some dentro de "chamou tool".
        "escaladas_detalhe": _escaladas_com_turno(turnos),
        "escaladas": [e["motivo"] for e in conversa.get("escaladas", [])],
        "saida_bloqueios": conversa.get("saida_bloqueios", 0),
        "erro": conversa.get("erro"),
    }


def agregar(metricas: list[dict]) -> dict:
    """Cabeçalho da rodada. É isto que compara com a rodada anterior."""
    ok = [m for m in metricas if not m.get("erro")]
    if not ok:
        return {"conversas": len(metricas), "todas_com_erro": True}

    def med(chave):
        vals = [m[chave] for m in ok if isinstance(m.get(chave), (int, float))]
        return round(statistics.fmean(vals), 1) if vals else 0

    paradas: dict[str, int] = {}
    for m in ok:
        paradas[m["motivo_parada"]] = paradas.get(m["motivo_parada"], 0) + 1

    proibidos: dict[str, int] = {}
    for m in ok:
        for termo, n in m["termos_proibidos"].items():
            proibidos[termo] = proibidos.get(termo, 0) + n

    return {
        "conversas": len(metricas),
        "com_erro": len(metricas) - len(ok),
        "primeira_bolha_chars_media": med("primeira_bolha_chars"),
        "primeiro_turno_chars_media": med("primeiro_turno_chars"),
        "chars_por_bolha_media": med("chars_por_bolha_media"),
        "chars_por_bolha_max": max((m["chars_por_bolha_max"] for m in ok), default=0),
        "bolhas_por_turno_media": med("bolhas_por_turno_media"),
        "razao_volume_media": med("razao_volume_sofia_paciente"),
        "turnos_media": med("turnos"),
        "conversas_com_repeticao": sum(1 for m in ok if m["repeticoes"]),
        "conversas_com_termo_proibido": sum(1 for m in ok if m["termos_proibidos"]),
        "termos_proibidos_total": proibidos,
        "motivos_parada": paradas,
        "desistencias": paradas.get("desistiu", 0),
        # 🔴 Zero é o único valor aceitável. Ver `_prometeu_sem_agir`.
        "conversas_prometeu_e_nao_fez": sum(1 for m in ok if m.get("prometeu_e_nao_fez")),
        # Escalou nos 2 primeiros turnos.
        # ⚠️ Zero NÃO é a meta, ao contrário do "prometeu e não fez". Existe
        # persona cujo desfecho CERTO é escalar cedo: a `ja-foi-paciente`
        # pergunta se o cadastro antigo dela vale, e isso a Sofia não tem como
        # responder — escalar no turno 2 é acerto. Em 18/08 este número subiu
        # de 1 pra 3 no controle **porque melhorou**: antes ela dizia "vou
        # chamar a Thainá" e não chamava (aparecia em `prometeu_e_nao_fez`).
        # Serve pra comparar modelos NAS MESMAS personas — foi assim que
        # separou Luna (9 em 36) de Terra (1 em 36). Não serve como alarme
        # absoluto: sempre abra a transcrição antes de chamar de defeito.
        "conversas_escalada_precoce": sum(
            1 for m in ok if any(e["precoce"] for e in m.get("escaladas_detalhe", []))
        ),
        # `outro` é o balde genérico — onde a desistência se disfarça de escalada.
        "escaladas_motivo_outro": sum(
            1 for m in ok for e in m.get("escaladas_detalhe", []) if e["motivo"] == "outro"
        ),
        "saida_bloqueios_total": sum(m["saida_bloqueios"] for m in ok),
    }
