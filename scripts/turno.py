"""Um turno de conversa com a Sofia por execução — pra quem testa SER o paciente.

`conversar.py` é interativo (uma pessoa digitando). O laboratório usa um segundo
LLM pra interpretar a persona, e isso custa token e tira o controle fino de quem
testa. Este script é o meio-termo: cada execução manda UMA mensagem (ou roda UM
comando), imprime o que a Sofia respondeu e sai. Quem conduz o paciente decide a
próxima fala olhando a resposta — sem LLM de persona.

    python scripts/turno.py 5531900000301 "oi, boa tarde"
    python scripts/turno.py 5531900000301 /estado
    python scripts/turno.py 5531900000301 /pesquisas
    python scripts/turno.py 5531900000301 /cobrancas
    python scripts/turno.py 5531900000301 /avaliacao
    python scripts/turno.py 5531900000301 /contrato
    python scripts/turno.py 5531900000301 /adiantar 4

As travas são as do `conversar.py` (mesmas funções): aborta se o envio real de
WhatsApp estiver ligado ou se o Hamilton configurado for o de produção.
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.database import async_session  # noqa: E402
from scripts import conversar  # noqa: E402


async def principal(base_url: str, numero: str, partes: list[str], espera: float) -> None:
    if not settings.envio_whatsapp_bloqueado:
        raise SystemExit("ABORTADO: o envio real de WhatsApp está LIGADO.")
    if "onrender.com" in settings.hamilton_api_url:
        raise SystemExit(
            "ABORTADO: HAMILTON_API_URL aponta pra produção. "
            "Rode com HAMILTON_API_URL=http://localhost:8001."
        )

    from app.services import config_negocio, config_prompt

    async with async_session() as db:
        await config_negocio.carregar_do_banco(db)
        await config_prompt.carregar_do_banco(db)

    texto = " ".join(partes).strip()
    desde_id = await conversar._ultimo_id(numero)

    if texto == "/estado":
        await conversar._mostrar_estado(numero)
    elif texto == "/pesquisas":
        await conversar._rodar_pesquisas()
        await conversar._esperar_resposta(numero, desde_id, espera)
    elif texto == "/cobrancas":
        await conversar._rodar_cobrancas()
        await conversar._esperar_resposta(numero, desde_id, espera)
    elif texto == "/contrato":
        await conversar._mostrar_contrato(numero)
    elif texto == "/avaliacao":
        await conversar._mostrar_avaliacao(numero)
    elif texto.startswith("/adiantar"):
        horas = float(partes[1]) if len(partes) > 1 else 4
        await conversar._adiantar(numero, horas)
    else:
        await conversar._enviar_ao_webhook(base_url, numero, texto)
        await conversar._esperar_resposta(numero, desde_id, espera)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("numero", help="número do paciente simulado")
    p.add_argument("fala", nargs="+", help="mensagem do paciente ou um /comando")
    p.add_argument("--url", default="http://localhost:8000")
    p.add_argument("--espera", type=float, default=90.0)
    a = p.parse_args()
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(principal(a.url, a.numero, a.fala, a.espera))
