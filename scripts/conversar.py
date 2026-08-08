"""Conversa com a Sofia pelo terminal, como se fosse um paciente no WhatsApp.

Por que existe: os 521 testes mockam OpenAI, Hamilton e Meta, então provam que o
encanamento liga — mas não respondem a pergunta que sobra depois de tudo pronto,
que é **se o modelo obedece ao prompt**. Se a Sofia chama
`registrar_resposta_pesquisa` a cada resposta ou se ela conversa bonito e não
grava nada, nenhum mock diz.

Este script monta o payload da Meta, assina com o `WHATSAPP_APP_SECRET` (a Sofia
valida a assinatura de verdade — nada aqui é bypass), POSTa no webhook e lê a
resposta direto do banco. O LLM é o de verdade; o Hamilton é o que você apontar.

    # 1) Hamilton local, contra a branch sofia-teste
    cd ../hamilton-api && venv/Scripts/python.exe manage.py runserver 8001

    # 2) Sofia local, apontada pro Hamilton local e SEM falar com a Meta
    HAMILTON_API_URL=http://localhost:8001 uvicorn app.main:app --port 8000

    # 3) esta conversa
    python scripts/conversar.py --numero 5531900000001

⚠️ **A trava do WhatsApp é `settings.envio_whatsapp_bloqueado`**, não este
script: a Sofia se recusa a chamar a Meta fora de `ENVIRONMENT=production`. Este
script confere isso no começo e se recusa a rodar se a trava estiver aberta —
mandar mensagem de verdade pra um número inventado durante um teste é o tipo de
erro que só se descobre pelo outro lado.

Comandos dentro da conversa:
    /estado      o que a Sofia sabe desta conversa (estado, dados, pesquisa)
    /pesquisas   roda o cron de pesquisas agora (cria a de entrada, envia, lembra)
    /avaliacao   mostra a Avaliacao no Hamilton, com todas as respostas gravadas
    /adiantar N  finge que o cadastro foi há N horas (destrava a janela das 3h)
    /sair
"""

import argparse
import asyncio
import hashlib
import hmac
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.config import settings  # noqa: E402
from app.database import async_session  # noqa: E402
from app.models import Conversa, Mensagem  # noqa: E402

VERDE, AMARELO, CINZA, VERMELHO, RESET = "\033[92m", "\033[93m", "\033[90m", "\033[91m", "\033[0m"


def _assinar(corpo: bytes) -> str:
    """A mesma assinatura que a Meta manda — o webhook valida de verdade."""
    mac = hmac.new(settings.whatsapp_app_secret.encode(), corpo, hashlib.sha256)
    return f"sha256={mac.hexdigest()}"


def _payload(numero: str, texto: str, wamid: str) -> dict:
    """Formato do webhook da Cloud API (só o que o `webhook.py` lê)."""
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "0",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "contacts": [{"wa_id": numero, "profile": {"name": "Teste"}}],
                            "messages": [
                                {
                                    "from": numero,
                                    "id": wamid,
                                    "timestamp": str(int(time.time())),
                                    "type": "text",
                                    "text": {"body": texto},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


async def _enviar_ao_webhook(base_url: str, numero: str, texto: str) -> None:
    wamid = f"wamid.teste.{int(time.time() * 1000)}"
    corpo = json.dumps(_payload(numero, texto, wamid)).encode()
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{base_url}/webhook/whatsapp",
            content=corpo,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": _assinar(corpo),
            },
        )
    if resp.status_code != 200:
        raise SystemExit(f"{VERMELHO}webhook devolveu {resp.status_code}: {resp.text}{RESET}")


async def _conversa(db, numero: str) -> Conversa | None:
    return (
        await db.execute(select(Conversa).where(Conversa.numero_whatsapp == numero))
    ).scalar_one_or_none()


async def _novas_falas(numero: str, desde_id: int) -> tuple[list[Mensagem], int]:
    """Mensagens que a Sofia enviou depois de `desde_id`."""
    async with async_session() as db:
        conversa = await _conversa(db, numero)
        if conversa is None:
            return [], desde_id
        linhas = (
            (
                await db.execute(
                    select(Mensagem)
                    .where(
                        Mensagem.conversa_id == conversa.id,
                        Mensagem.direcao == "enviada",
                        Mensagem.id > desde_id,
                    )
                    .order_by(Mensagem.id)
                )
            )
            .scalars()
            .all()
        )
    return list(linhas), (linhas[-1].id if linhas else desde_id)


async def _ultimo_id(numero: str) -> int:
    async with async_session() as db:
        conversa = await _conversa(db, numero)
        if conversa is None:
            return 0
        linha = (
            await db.execute(
                select(Mensagem.id)
                .where(Mensagem.conversa_id == conversa.id, Mensagem.direcao == "enviada")
                .order_by(Mensagem.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    return linha or 0


async def _esperar_resposta(numero: str, desde_id: int, limite_s: float) -> int:
    """Espera a Sofia falar. O debounce agrupa a rajada, então há uma janela.

    Não há sinal de "terminou": o script fica lendo o banco até parar de aparecer
    fala nova por um tempo. É o mesmo que a pessoa vê no WhatsApp.
    """
    prazo = time.monotonic() + limite_s
    silencio_desde = None
    while time.monotonic() < prazo:
        novas, desde_id = await _novas_falas(numero, desde_id)
        for m in novas:
            print(f"{VERDE}Sofia:{RESET} {m.texto}")
            silencio_desde = time.monotonic()
        if silencio_desde and time.monotonic() - silencio_desde > 2.5:
            return desde_id  # parou de falar
        await asyncio.sleep(0.5)
    if silencio_desde is None:
        print(f"{CINZA}(sem resposta em {limite_s:.0f}s — veja o log do uvicorn){RESET}")
    return desde_id


async def _mostrar_estado(numero: str) -> None:
    async with async_session() as db:
        c = await _conversa(db, numero)
    if c is None:
        print(f"{CINZA}(conversa ainda não existe){RESET}")
        return
    dados = dict(c.dados_coletados or {})
    gravados = dados.pop("pesquisa_respostas_gravadas", None)
    print(f"{AMARELO}estado{RESET}       {c.estado}   modo={c.modo}")
    print(f"{AMARELO}hamilton{RESET}     paciente_id={c.paciente_hamilton_id} cadastrado_em={c.cadastrado_em}")
    print(f"{AMARELO}pesquisa{RESET}     avaliacao_id={c.pesquisa_avaliacao_id} iniciada={c.pesquisa_iniciada_em}")
    print(f"{AMARELO}alerta{RESET}       {c.alerta_pesquisa_em} {c.alerta_pesquisa_motivos or ''}")
    print(f"{AMARELO}coletado{RESET}     {json.dumps(dados, ensure_ascii=False)}")
    if gravados is not None:
        # É isto que responde "o modelo chamou a tool?".
        print(f"{AMARELO}tool gravou{RESET}  {json.dumps(gravados, ensure_ascii=False)}")


async def _rodar_pesquisas() -> None:
    from app.services import pesquisa

    async with async_session() as db:
        resumo = await pesquisa.rodar_pesquisas(db)
    print(f"{AMARELO}cron de pesquisas{RESET} {resumo}")


async def _mostrar_avaliacao(numero: str) -> None:
    from app.services import hamilton_client

    async with async_session() as db:
        c = await _conversa(db, numero)
    if c is None or not c.paciente_hamilton_id:
        print(f"{CINZA}(sem paciente no Hamilton ainda){RESET}")
        return
    try:
        pendentes = await hamilton_client.get_hamilton_client().avaliacoes_pendentes(
            incluir_enviadas=True
        )
    except hamilton_client.HamiltonError as exc:
        print(f"{VERMELHO}Hamilton: {exc}{RESET}")
        return
    minhas = [a for a in pendentes if a.get("fk_paciente") == c.paciente_hamilton_id]
    if not minhas:
        print(f"{CINZA}(nenhuma avaliação pendente para este paciente){RESET}")
    for a in minhas:
        print(f"{AMARELO}avaliação{RESET} {json.dumps(a, ensure_ascii=False, indent=2)}")


async def _adiantar(numero: str, horas: float) -> None:
    """Recua `cadastrado_em` pra destravar a janela de 3h da pesquisa de entrada."""
    async with async_session() as db:
        c = await _conversa(db, numero)
        if c is None or c.cadastrado_em is None:
            print(f"{CINZA}(nada a adiantar: não há cadastro){RESET}")
            return
        c.cadastrado_em = datetime.now(timezone.utc) - timedelta(hours=horas)
        await db.commit()
        print(f"{AMARELO}cadastrado_em{RESET} recuado para {c.cadastrado_em}")


async def principal(base_url: str, numero: str, espera: float) -> None:
    if not settings.envio_whatsapp_bloqueado:
        raise SystemExit(
            f"{VERMELHO}ABORTADO: o envio real de WhatsApp está LIGADO.{RESET}\n"
            "Rodar assim mandaria mensagem de verdade pela Cloud API. "
            "Suba a app com ENVIRONMENT=development (ou WHATSAPP_DRY_RUN=true)."
        )

    print(f"{CINZA}Sofia em {base_url} | número simulado {numero}{RESET}")
    print(f"{CINZA}Hamilton: {settings.hamilton_api_url}{RESET}")
    if "onrender.com" in settings.hamilton_api_url:
        print(
            f"{VERMELHO}ATENÇÃO: este Hamilton é o de PRODUÇÃO. Um cadastro aqui "
            f"cria paciente de verdade. Suba o Hamilton local e aponte "
            f"HAMILTON_API_URL pra ele.{RESET}"
        )
    print(f"{CINZA}/estado /pesquisas /avaliacao /adiantar N /sair{RESET}\n")

    desde_id = await _ultimo_id(numero)
    while True:
        try:
            texto = input(f"{AMARELO}você:{RESET} ").strip()
        except (EOFError, KeyboardInterrupt):
            return
        if not texto:
            continue
        if texto == "/sair":
            return
        if texto == "/estado":
            await _mostrar_estado(numero)
            continue
        if texto == "/pesquisas":
            await _rodar_pesquisas()
            desde_id = await _esperar_resposta(numero, desde_id, espera)
            continue
        if texto == "/avaliacao":
            await _mostrar_avaliacao(numero)
            continue
        if texto.startswith("/adiantar"):
            partes = texto.split()
            await _adiantar(numero, float(partes[1]) if len(partes) > 1 else 4)
            continue

        await _enviar_ao_webhook(base_url, numero, texto)
        desde_id = await _esperar_resposta(numero, desde_id, espera)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--url", default="http://localhost:8000", help="onde a Sofia está rodando")
    p.add_argument("--numero", default="5531900000001", help="número do paciente simulado")
    p.add_argument("--espera", type=float, default=90.0, help="segundos esperando a resposta")
    args = p.parse_args()
    asyncio.run(principal(args.url, args.numero, args.espera))
