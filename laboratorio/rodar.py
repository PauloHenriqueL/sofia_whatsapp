"""Roda a rodada: dispara uma conversa por persona, em paralelo, e monta o relatório.

    python laboratorio/rodar.py                          # as 8 personas, 1 conversa cada
    python laboratorio/rodar.py --persona acha-caro --repetir 3
    python laboratorio/rodar.py --sequencial             # uma de cada vez, pra depurar

Cada conversa é um **subprocesso** com o seu próprio SQLite (ver o cabeçalho de
`conversa.py`). O orquestrador não importa `app` em momento nenhum: ele só
dispara processos, junta JSON e escreve markdown. Isso é de propósito — assim um
travamento numa conversa não leva a rodada junto, e o `Ctrl+C` mata tudo limpo.

O que ele NUNCA faz: falar com a Meta, falar com o Hamilton real, ou editar
qualquer coisa dentro de `prompt/`.
"""

import argparse
import asyncio
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

import yaml

LAB = Path(__file__).resolve().parent
RAIZ = LAB.parent
sys.path.insert(0, str(LAB))

# Console do Windows abre em cp1252 e engasga em acento. Os relatórios são
# escritos em UTF-8 de todo jeito; isto é só pro que vai pra tela.
for fluxo in (sys.stdout, sys.stderr):
    if hasattr(fluxo, "reconfigure"):
        fluxo.reconfigure(encoding="utf-8", errors="replace")

import metricas as mod_metricas  # noqa: E402
import relatorio as mod_relatorio  # noqa: E402


def carregar_personas(filtro: list[str] | None, controle: bool = False) -> list[dict]:
    """As de `personas/` são de TREINO; as de `personas-controle/`, de controle.

    A separação existe porque quem ajusta o prompt lendo as mesmas personas toda
    rodada acaba decorando o gabarito: o placar sobe sem o bot melhorar. Decida
    mudança olhando as de treino; leia o resultado nas de controle, que ninguém
    usa pra decidir nada. Se as duas melhoram, melhorou de verdade.
    """
    pasta = "personas-controle" if controle else "personas"
    todas = []
    for caminho in sorted((LAB / pasta).glob("*.yaml")):
        p = yaml.safe_load(caminho.read_text(encoding="utf-8"))
        p["_arquivo"] = str(caminho)
        todas.append(p)
    if not filtro:
        return todas
    escolhidas = [p for p in todas if p["nome"] in filtro]
    faltando = set(filtro) - {p["nome"] for p in escolhidas}
    if faltando:
        disponiveis = ", ".join(p["nome"] for p in todas)
        raise SystemExit(
            f"Persona não encontrada: {', '.join(faltando)}\nDisponíveis: {disponiveis}"
        )
    return escolhidas


async def _rodar_uma(persona: dict, rep: int, pasta: Path, turnos_max: int, sem: asyncio.Semaphore):
    rotulo = f"{persona['nome']}-r{rep}"
    saida = pasta / f"{rotulo}.json"
    async with sem:
        print(f"  ▶ {rotulo}", flush=True)
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            str(LAB / "conversa.py"),
            "--persona",
            persona["_arquivo"],
            "--saida",
            str(saida),
            "--db",
            str(pasta / f"{rotulo}.db"),
            "--turnos-max",
            str(turnos_max),
            cwd=str(RAIZ),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, err = await proc.communicate()
    if not saida.exists():
        # Nem o handler de erro do subprocesso rodou: falhou antes de subir.
        detalhe = (err or b"").decode("utf-8", "replace")[-1500:]
        saida.write_text(
            json.dumps(
                {
                    "persona": persona["nome"],
                    "titulo": persona.get("titulo", ""),
                    "turnos": [],
                    "motivo_parada": "erro",
                    "erro": "subprocesso morreu",
                    "traceback": detalhe,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    r = json.loads(saida.read_text(encoding="utf-8"))
    r["repeticao"] = rep
    r["_persona_identidade"] = persona.get("identidade", {})
    print(f"  {'✗' if r.get('erro') else '✓'} {rotulo} — {r.get('motivo_parada')}", flush=True)
    return r


async def principal(args) -> None:
    personas = carregar_personas(args.persona, controle=args.controle)
    carimbo = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    pasta = LAB / "execucoes" / carimbo
    pasta.mkdir(parents=True, exist_ok=True)

    total = len(personas) * args.repetir
    print(f"\nRodada {carimbo} — {len(personas)} persona(s) × {args.repetir} = {total} conversa(s)")
    print(f"Saída: {pasta}\n")

    sem = asyncio.Semaphore(1 if args.sequencial else args.concorrencia)
    tarefas = [
        _rodar_uma(p, rep, pasta, args.turnos_max, sem)
        for p in personas
        for rep in range(1, args.repetir + 1)
    ]
    resultados = await asyncio.gather(*tarefas)

    por_nome = {p["nome"]: p for p in personas}
    lista_metricas = [mod_metricas.calcular(r) for r in resultados]
    agregado = mod_metricas.agregar(lista_metricas)
    agregado["hashes"] = mod_relatorio.hashes()
    agregado["uso"] = _somar_uso(resultados)

    mod_relatorio.escrever_transcricoes(resultados, por_nome, pasta / "transcricoes.md")

    destino = LAB / "relatorios" / carimbo
    destino.mkdir(parents=True, exist_ok=True)
    anterior = mod_relatorio.rodada_anterior(LAB / "relatorios", destino)
    mod_relatorio.escrever_resumo(
        agregado, lista_metricas, resultados, anterior, destino / "resumo.md"
    )
    (destino / "dados.json").write_text(
        json.dumps(
            {
                "rodada": carimbo,
                "agregado": agregado,
                "por_conversa": [
                    dict(m, persona=r["persona"]) for m, r in zip(lista_metricas, resultados)
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    # A transcrição também vai pro relatório: sem ela, o resumo é número sem prova.
    shutil.copy(pasta / "transcricoes.md", destino / "transcricoes.md")

    print(f"\n{'='*60}")
    print((destino / "resumo.md").read_text(encoding="utf-8").split("## Por conversa")[0])
    print(f"Resumo:       {destino / 'resumo.md'}")
    print(f"Transcrições: {destino / 'transcricoes.md'}")


def _somar_uso(resultados: list[dict]) -> dict:
    total: dict[str, dict[str, int]] = {}
    for r in resultados:
        for modelo, v in (r.get("uso", {}).get("por_modelo") or {}).items():
            alvo = total.setdefault(
                modelo, {"chamadas": 0, "entrada": 0, "entrada_cache": 0, "saida": 0}
            )
            for k in alvo:
                alvo[k] += v.get(k, 0)
    return total


if __name__ == "__main__":
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--persona", action="append", help="roda só esta persona (pode repetir a flag)")
    p.add_argument(
        "--controle",
        action="store_true",
        help="roda as personas de CONTROLE (personas-controle/) em vez das de treino. "
        "Elas existem pra medir, não pra ajustar: não mude o prompt olhando pra elas",
    )
    p.add_argument(
        "--repetir",
        type=int,
        default=1,
        help="conversas por persona. Repetição é pra DECISÃO (confirmar um achado "
        "antes de mexer no prompt), não pra rodada de rotina.",
    )
    p.add_argument("--turnos-max", type=int, default=25)
    p.add_argument("--concorrencia", type=int, default=4)
    p.add_argument("--sequencial", action="store_true", help="uma conversa por vez")
    try:
        asyncio.run(principal(p.parse_args()))
    except KeyboardInterrupt:
        print("\ninterrompido")
