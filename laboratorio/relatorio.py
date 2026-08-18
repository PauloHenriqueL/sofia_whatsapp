"""Monta o que sai de uma rodada: transcrições legíveis, resumo e dados.

Três artefatos, com públicos diferentes:

- `transcricoes.md` — o que o **subagente detector** e o Claude leem. Ele carrega
  a persona pelo **ponto de vista dela** (quem é, o que quer), e omite de
  propósito os campos `pressao` e `o_que_observar`: esses são a expectativa de
  quem desenhou o teste, e entregá-los ao detector é dizer a resposta antes da
  prova. Também não há uma linha sequer do `sofia_v01.txt` aqui — leitor que
  conhece o prompt racionaliza o defeito como conformidade.
- `resumo.md` — uma página pra você, com os deltas contra a rodada anterior.
- `dados.json` — o histórico versionado (é o placar entre versões do prompt).
"""

import hashlib
import json
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
LAB = Path(__file__).resolve().parent

ARQUIVOS_VERSIONADOS = [
    RAIZ / "prompt" / "sofia_v01.txt",
    RAIZ / "prompt" / "sofia-base-conhecimento.md",
    LAB / "rubrica.md",
]

# O detector não pode ver o que o desenhista do teste esperava encontrar.
CAMPOS_OCULTOS_DO_LEITOR = {"pressao", "o_que_observar", "falas_obrigatorias", "comportamento"}


def hashes() -> dict[str, str]:
    out = {}
    for caminho in ARQUIVOS_VERSIONADOS:
        if caminho.exists():
            digest = hashlib.sha256(caminho.read_bytes()).hexdigest()[:12]
            out[caminho.name] = digest
    return out


def escrever_transcricoes(resultados: list[dict], personas: dict[str, dict], destino: Path) -> None:
    partes = [
        "# Transcrições da rodada\n",
        f"{len(resultados)} conversas de WhatsApp entre uma pessoa que procurou uma ONG "
        "de psicologia e a atendente dela. Leia como quem recebeu as mensagens.\n"
        "\n"
        "A mesma persona pode aparecer mais de uma vez, em execuções separadas: quando "
        "isso acontece, tratamento muito diferente para a mesma pessoa é, por si só, um "
        "achado.\n",
    ]
    for r in resultados:
        p = personas.get(r["persona"], {})
        visivel = {k: v for k, v in p.items() if k not in CAMPOS_OCULTOS_DO_LEITOR}
        partes.append(f"\n---\n\n## {r['persona']} — {r.get('titulo','')}\n")
        partes.append("**Quem escreveu:**\n")
        for chave, valor in (visivel.get("identidade") or {}).items():
            partes.append(f"- {chave.replace('_',' ')}: {valor}")
        partes.append(f"\n**Como ela é:** {(visivel.get('personalidade') or '').strip()}\n")
        partes.append(f"**Por que escreveu:** {(visivel.get('objetivo') or '').strip()}\n")
        if visivel.get("encerra_quando"):
            partes.append(
                f"**O que ela precisava pra sair satisfeita:** "
                f"{visivel['encerra_quando'].strip()}\n"
            )
        if r.get("erro"):
            partes.append(f"\n> ⚠️ A conversa não rodou: `{r['erro']}`\n")
            continue
        partes.append("\n### A conversa\n")
        for t in r["turnos"]:
            for fala in t["paciente"]:
                partes.append(f"**Ela:** {fala}\n")
            for i, bolha in enumerate(t["sofia"], 1):
                marca = f" _(bolha {i}/{len(t['sofia'])}, {len(bolha)} caracteres)_"
                partes.append(f"**Atendente:**{marca}\n\n{bolha}\n")
            for tc in t.get("tool_calls", []):
                partes.append(
                    f"> _[ação interna: {tc['nome']} {json.dumps(tc['argumentos'], ensure_ascii=False)}]_\n"
                )
        partes.append(f"\n_Fim: {r['motivo_parada']}._\n")
    destino.write_text("\n".join(partes), encoding="utf-8")


def _delta(atual, anterior) -> str:
    if (
        anterior is None
        or not isinstance(atual, (int, float))
        or not isinstance(anterior, (int, float))
    ):
        return ""
    d = round(atual - anterior, 1)
    if d == 0:
        return " (=)"
    return f" ({'+' if d > 0 else ''}{d})"


def escrever_resumo(
    agregado: dict,
    metricas: list[dict],
    resultados: list[dict],
    anterior: dict | None,
    destino: Path,
) -> None:
    ant = (anterior or {}).get("agregado", {})
    L = [
        "# Resumo da rodada\n",
        f"`{destino.parent.name}` · prompt `{json.dumps(agregado.get('hashes', {}))}`\n",
        "> Isto é o piso, não o veredito. O julgamento está no relatório de achados.\n",
        "## Números\n",
        "| | valor | vs. rodada anterior |",
        "|---|---:|---|",
    ]
    for rotulo, chave in [
        ("Conversas", "conversas"),
        ("Com erro", "com_erro"),
        ("**1ª bolha (caracteres)**", "primeira_bolha_chars_media"),
        ("**1º turno inteiro (caracteres)**", "primeiro_turno_chars_media"),
        ("Bolha média", "chars_por_bolha_media"),
        ("Maior bolha da rodada", "chars_por_bolha_max"),
        ("Bolhas por turno", "bolhas_por_turno_media"),
        ("**Sofia fala N× mais que a pessoa**", "razao_volume_media"),
        ("Turnos por conversa", "turnos_media"),
        ("Conversas com repetição", "conversas_com_repeticao"),
        ("Conversas com termo proibido", "conversas_com_termo_proibido"),
        ("**Desistências**", "desistencias"),
        # Zero é o único valor aceitável: a Sofia afirmou ter registrado ou
        # acionado alguém, e não chamou tool nenhuma. Ver `_prometeu_sem_agir`.
        ("🔴 **Prometeu e não fez**", "conversas_prometeu_e_nao_fez"),
        # Escalar nos 2 primeiros turnos. Some dentro de "chamou tool", por isso
        # tem linha própria — mas ZERO NÃO É A META: há persona cujo desfecho
        # certo é escalar cedo. É número de comparação entre modelos, não alarme.
        ("🔴 **Escalada precoce** (≤2 turnos)", "conversas_escalada_precoce"),
        ("Escaladas com motivo `outro`", "escaladas_motivo_outro"),
        ("Vezes que a saida.limpar() cortou algo", "saida_bloqueios_total"),
    ]:
        v = agregado.get(chave)
        L.append(f"| {rotulo} | {v} |{_delta(v, ant.get(chave))} |")

    L.append("\n## Como cada conversa terminou\n")
    for motivo, n in sorted(agregado.get("motivos_parada", {}).items(), key=lambda x: -x[1]):
        L.append(f"- **{motivo}**: {n}")

    if agregado.get("termos_proibidos_total"):
        L.append("\n## Termos proibidos pelo próprio prompt\n")
        for termo, n in sorted(agregado["termos_proibidos_total"].items(), key=lambda x: -x[1]):
            L.append(f"- `{termo}`: {n}×")

    L.append("\n## Por conversa\n")
    L.append(
        "| persona | fim | turnos | 1ª bolha | bolha média | maior | Sofia:pessoa | repet. | tools |"
    )
    L.append("|---|---|---:|---:|---:|---:|---:|---:|---|")
    for m, r in zip(metricas, resultados):
        if m.get("erro"):
            L.append(f"| {r['persona']} | ⚠️ erro | | | | | | | |")
            continue
        L.append(
            f"| {r['persona']} | {m['motivo_parada']} | {m['turnos']} | "
            f"{m['primeira_bolha_chars']} | {m['chars_por_bolha_media']} | "
            f"{m['chars_por_bolha_max']} | {m['razao_volume_sofia_paciente']} | "
            f"{len(m['repeticoes'])} | {', '.join(m['tools_chamadas']) or '—'} |"
        )

    uso = agregado.get("uso", {})
    if uso:
        L.append("\n## Consumo\n")
        for modelo, v in uso.items():
            L.append(
                f"- `{modelo}`: {v['chamadas']} chamadas · "
                f"{v['entrada']:,} tokens de entrada ({v['entrada_cache']:,} em cache) · "
                f"{v['saida']:,} de saída".replace(",", ".")
            )
        L.append(
            "\n_Sem conversão pra reais: preencha `fixtures/precos.json` se quiser "
            "custo em dinheiro. Token é fato; preço eu teria que chutar._"
        )

    L.append("\n## Próximo passo\n")
    L.append(
        "As transcrições estão em `transcricoes.md`, no mesmo diretório. "
        "O detector as lê **sem** ver o prompt da Sofia."
    )
    destino.write_text("\n".join(L), encoding="utf-8")


def rodada_anterior(pasta_relatorios: Path, atual: Path) -> dict | None:
    anteriores = sorted(p for p in pasta_relatorios.glob("*/dados.json") if p.parent != atual)
    if not anteriores:
        return None
    return json.loads(anteriores[-1].read_text(encoding="utf-8"))
