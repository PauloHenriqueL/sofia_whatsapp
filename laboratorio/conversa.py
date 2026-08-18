"""Roda UMA conversa persona↔Sofia e escreve o resultado em JSON.

Este arquivo é executado como **subprocesso**, um por conversa. O motivo é chato
e decisivo: `app.database.engine` é criado no import a partir de
`settings.database_url`, e `config_negocio`/`captacao` guardam cache em variável
de módulo. Rodar N conversas em corrotinas no mesmo processo faria elas
disputarem o mesmo banco e o mesmo cache. Um processo por conversa dá isolamento
de graça, paralelismo de verdade e um banco inspecionável por conversa.

Nada aqui fala com a Meta nem com o Hamilton real:
  - a Meta é bloqueada pela própria app (`settings.envio_whatsapp_bloqueado`,
    que cobre `enviar_texto` E `enviar_template` — a Thainá não recebe alerta);
  - o Hamilton é o `HamiltonFalso`, injetado no singleton;
  - `HAMILTON_API_URL` ainda é apontado pra um endereço morto, pra que um
    caminho que escape do singleton falhe barulhento em vez de vazar.

Uso (normalmente chamado pelo `rodar.py`):
    python laboratorio/conversa.py --persona personas/01-oi-e-nada-mais.yaml \
        --saida execucoes/2026-08-07T12-00/01-oi-e-nada-mais-r1.json --db .../x.db
"""

import argparse
import os
import sys
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
LAB = Path(__file__).resolve().parent

# Antes disto, um "consegui o que queria" do paciente é descartado (ver o uso).
MIN_TURNOS_PARA_ENCERRAR_OK = 5


def _preparar_ambiente(db_path: str) -> None:
    """Fixa o ambiente ANTES de importar `app` (o engine nasce no import)."""
    os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"
    os.environ["HAMILTON_API_URL"] = "http://127.0.0.1:1"  # endereço morto de propósito
    os.environ["ENVIRONMENT"] = "development"  # garante o dry-run da Meta
    os.environ["SIMULAR_DIGITACAO"] = "false"  # não dormir entre bolhas
    os.environ.pop("WHATSAPP_DRY_RUN", None)  # nada pode reabrir a trava


async def _rodar(persona: dict, saida: Path, turnos_max: int, modelo_paciente: str) -> dict:
    import json

    from sqlalchemy import select

    from app.config import settings
    from app.database import Base, async_session, engine
    from app.models import Escalada, Mensagem  # noqa: F401  (registra o metadata)
    from app.routers import webhook
    from app.services import captacao, conversation, hamilton_client, llm_client
    from app.services import saida as saida_mod

    if not settings.envio_whatsapp_bloqueado:
        raise SystemExit("ABORTADO: o envio real de WhatsApp está ligado.")

    # Guarda o texto que a sanitização removeu. Só é seguro aqui: os pacientes são
    # fictícios. Contar quantas vezes cortou não diz se foi um espaço em branco ou
    # um vazamento de tool call, e essa diferença é o sinal de degeneração do modelo.
    saida_mod.gravar_amostras(True)

    sys.path.insert(0, str(LAB))
    from contador import Contador, envolver_openai  # noqa: E402
    from hamilton_falso import HamiltonFalso  # noqa: E402
    from paciente import Paciente  # noqa: E402

    contador = Contador()

    # Injeta o Hamilton falso e o cliente LLM contabilizado nos dois singletons.
    # `app/` não é tocado: a costura é o `lru_cache`, que existe justamente pra
    # ser o ponto único de troca.
    falso = HamiltonFalso()
    hamilton_client.get_hamilton_client.cache_clear()
    hamilton_client.get_hamilton_client = lambda: falso  # type: ignore[assignment]
    captacao.limpar()  # o cache de captações é de módulo; começa limpo

    llm_client.get_llm_client.cache_clear()
    cliente_contado = llm_client.OpenAIClient(
        temperature=settings.openai_temperature,
        # 🔴 O `esforco` PRECISA vir junto. Sem ele o laboratório não é a Sofia:
        # com function calling, o gpt-5.6 em /v1/chat/completions recusa qualquer
        # reasoning_effort diferente de "none" (400), e todo turno cai no texto
        # de fallback — nove conversas de "tive um probleminha técnico", com zero
        # tool calls e o modelo da Sofia sem aparecer no consumo. O relatório sai
        # inteiro e parece um resultado ruim de prompt, não uma configuração
        # diferente da produção. Aconteceu em 17/08.
        esforco=settings.openai_reasoning_effort,
        client=envolver_openai(settings.openai_api_key, contador),
    )
    llm_client.get_llm_client = lambda: cliente_contado  # type: ignore[assignment]

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    numero = str(persona["numero"])
    paciente = Paciente(persona, modelo_paciente, contador, settings.openai_api_key)

    resultado: dict = {
        "persona": persona["nome"],
        "titulo": persona.get("titulo", ""),
        "numero": numero,
        "turnos": [],
        "motivo_parada": "teto_turnos",
        "erro": None,
    }
    # A conversa como o PACIENTE a enxerga: a fala da Sofia é `user` pra ele.
    historico_paciente: list[dict] = []

    async with async_session() as db:
        conversa = await conversation.obter_ou_criar_conversa(db, numero)
        await db.commit()

        for n in range(1, turnos_max + 1):
            t0 = time.monotonic()
            falas, encerramento = await paciente.falar(historico_paciente, n)
            # Modelo de paciente adora dar a conversa por encerrada assim que
            # recebe um número. Encerramento satisfeito cedo demais quase sempre
            # é o modelo sendo educado, não a pessoa estando resolvida — então
            # ele é ignorado e a conversa segue. Desistência NÃO é ignorada:
            # desistir no turno 2 é justamente o achado que interessa.
            if encerramento == "ok" and n < MIN_TURNOS_PARA_ENCERRAR_OK:
                encerramento = None

            for fala in falas:
                historico_paciente.append({"role": "assistant", "content": fala})
                await conversation.registrar_mensagem_recebida(
                    db,
                    conversa,
                    tipo="texto",
                    texto=fala,
                    whatsapp_message_id=f"lab.{numero}.{n}.{len(historico_paciente)}",
                )
            await db.commit()

            if encerramento and not falas:
                resultado["motivo_parada"] = (
                    "objetivo_atingido" if encerramento == "ok" else "desistiu"
                )
                break

            antes = await _ultimo_id(db, conversa.id)
            tools_antes = len(contador.tool_calls)
            await webhook._responder_turno(db, conversa, numero)
            await db.commit()
            bolhas = await _bolhas_novas(db, conversa.id, antes)

            for b in bolhas:
                historico_paciente.append({"role": "user", "content": b})

            resultado["turnos"].append(
                {
                    "n": n,
                    "paciente": falas,
                    "sofia": bolhas,
                    "tool_calls": contador.tool_calls[tools_antes:],
                    "segundos": round(time.monotonic() - t0, 1),
                }
            )

            if encerramento:
                resultado["motivo_parada"] = (
                    "objetivo_atingido" if encerramento == "ok" else "desistiu"
                )
                break
            await db.refresh(conversa)
            if conversa.modo == "humano":
                resultado["motivo_parada"] = "escalada"
                break
            if conversa.paciente_hamilton_id:
                resultado["motivo_parada"] = "cadastro"
                break
            if not bolhas:
                resultado["motivo_parada"] = "sofia_calou"
                break

        await db.refresh(conversa)
        escaladas = (
            (await db.execute(select(Escalada).where(Escalada.conversa_id == conversa.id)))
            .scalars()
            .all()
        )
        resultado["estado_final"] = {
            "estado": conversa.estado,
            "modo": conversa.modo,
            "paciente_hamilton_id": conversa.paciente_hamilton_id,
            "dados_coletados": conversa.dados_coletados or {},
        }
        resultado["escaladas"] = [{"motivo": e.motivo, "contexto": e.contexto} for e in escaladas]

    resultado["hamilton"] = falso.chamadas
    resultado["saida_bloqueios"] = saida_mod.bloqueios()
    resultado["saida_amostras"] = saida_mod.amostras()
    resultado["uso"] = contador.resumo()
    saida.write_text(json.dumps(resultado, ensure_ascii=False, indent=2), encoding="utf-8")
    return resultado


async def _ultimo_id(db, conversa_id: int) -> int:
    from sqlalchemy import select

    from app.models import Mensagem

    linha = (
        await db.execute(
            select(Mensagem.id)
            .where(Mensagem.conversa_id == conversa_id, Mensagem.direcao == "enviada")
            .order_by(Mensagem.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return linha or 0


async def _bolhas_novas(db, conversa_id: int, desde: int) -> list[str]:
    """As bolhas exatamente como saíram: já sanitizadas e já divididas.

    Ler do banco em vez do retorno de `processar_turno_bot` é o que faz o
    tamanho de bolha virar dado — é este texto, e não o texto bruto do modelo,
    que a pessoa vê no WhatsApp.
    """
    from sqlalchemy import select

    from app.models import Mensagem

    linhas = (
        (
            await db.execute(
                select(Mensagem)
                .where(
                    Mensagem.conversa_id == conversa_id,
                    Mensagem.direcao == "enviada",
                    Mensagem.id > desde,
                )
                .order_by(Mensagem.id)
            )
        )
        .scalars()
        .all()
    )
    return [m.texto for m in linhas if m.texto]


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--persona", required=True, help="caminho do YAML da persona")
    p.add_argument("--saida", required=True, help="arquivo JSON de resultado")
    p.add_argument("--db", required=True, help="arquivo SQLite desta conversa")
    p.add_argument("--turnos-max", type=int, default=25)
    p.add_argument("--modelo-paciente", default=os.getenv("LAB_MODELO_PACIENTE", "gpt-5.4-mini"))
    args = p.parse_args()

    _preparar_ambiente(args.db)
    sys.path.insert(0, str(RAIZ))

    import asyncio
    import json

    import yaml

    persona = yaml.safe_load(Path(args.persona).read_text(encoding="utf-8"))
    saida = Path(args.saida)
    try:
        asyncio.run(_rodar(persona, saida, args.turnos_max, args.modelo_paciente))
    except Exception as exc:  # a conversa que morre não pode matar a rodada
        import traceback

        saida.write_text(
            json.dumps(
                {
                    "persona": persona.get("nome"),
                    "titulo": persona.get("titulo", ""),
                    "turnos": [],
                    "motivo_parada": "erro",
                    "erro": f"{type(exc).__name__}: {exc}",
                    "traceback": traceback.format_exc()[-2000:],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        raise


if __name__ == "__main__":
    main()
