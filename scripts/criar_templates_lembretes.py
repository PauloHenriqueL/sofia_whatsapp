"""Cria na Meta os dois templates dos lembretes dos terapeutas (conta Sofia).

    WHATSAPP_WABA_ID=<id da conta> python scripts/criar_templates_lembretes.py            # só mostra
    WHATSAPP_WABA_ID=<id da conta> python scripts/criar_templates_lembretes.py --enviar   # cria

Lê `WHATSAPP_TOKEN` do `.env`. O ID da conta (WABA) aparece no Gerenciador do
WhatsApp, conta "Sofia": é o `waba_id=` da barra de endereço.

Template aprovado não se edita: mudou uma vírgula, é template novo. Por isso o
script pula os que já existem com o mesmo nome. Os textos precisam bater com as
variáveis que o Hamilton monta (principais/lembretes.py).

Versão 2 (30/10/2026): texto com quebra de linha e botões de resposta rápida.
Os da versão 1 (`agenda_do_dia_terapeuta` e `pendencias_semana_terapeuta`)
nunca foram usados e ficam parados na Meta. Quebra de linha só no texto fixo:
a Meta recusa variável com quebra (erro 132018), por isso cada linha variável
é uma variável. O detalhe por paciente não cabe em template (lista de tamanho
variável) e chega em texto livre depois do toque no botão; o código de cada
botão vai no envio (ver `whatsapp_client.enviar_template`), não aqui.
"""

import json
import os
import sys

import httpx
from dotenv import load_dotenv

API = "https://graph.facebook.com/v21.0"
RODAPE = {"type": "FOOTER", "text": "Mensagem automática do Hamilton"}


def _botoes(*rotulos: str) -> dict:
    return {"type": "BUTTONS", "buttons": [{"type": "QUICK_REPLY", "text": r} for r in rotulos]}


TEMPLATES = [
    {
        "name": "agenda_do_dia_terapeuta_v2",
        "language": "pt_BR",
        "category": "UTILITY",
        "components": [
            {
                "type": "BODY",
                "text": "Olá, {{1}}!\n\nHoje você tem:\n{{2}}\n{{3}}\n\nToque abaixo para ver os detalhes.",
                "example": {"body_text": [["Tácia", "2 sessões (9h e 10h)", "1 atividade (14h)"]]},
            },
            RODAPE,
            _botoes("Agenda de hoje", "Agenda da semana"),
        ],
    },
    {
        "name": "lembrete_semana_terapeuta",
        "language": "pt_BR",
        "category": "UTILITY",
        "components": [
            {
                "type": "BODY",
                "text": (
                    "Olá, {{1}}!\n\nNa semana passada:\nPacientes com sessão: {{2}}\n"
                    "Faltaram: {{3}}\nSem registro de sessão: {{4}}\n"
                    "1ª sessão não registrada: {{5}}\n\n"
                    "Pagamentos sem registro neste mês: {{6}}\n\n"
                    "Toque abaixo para ver paciente por paciente."
                ),
                "example": {"body_text": [["Tácia", "3", "1", "1", "1", "2"]]},
            },
            RODAPE,
            _botoes("Saiba mais"),
        ],
    },
]


def main() -> int:
    load_dotenv()
    token = os.environ.get("WHATSAPP_TOKEN")
    waba = os.environ.get("WHATSAPP_WABA_ID")
    if not token or not waba:
        print("Falta WHATSAPP_TOKEN (no .env) ou WHATSAPP_WABA_ID (id da conta Sofia).")
        return 1
    enviar = "--enviar" in sys.argv
    headers = {"Authorization": f"Bearer {token}"}
    with httpx.Client(timeout=20) as http:
        existentes = http.get(f"{API}/{waba}/message_templates", headers=headers,
                              params={"fields": "name,status", "limit": 200})
        existentes.raise_for_status()
        ja = {t["name"]: t["status"] for t in existentes.json().get("data", [])}
        for tpl in TEMPLATES:
            if tpl["name"] in ja:
                print(f"= {tpl['name']}: já existe ({ja[tpl['name']]}), pulando")
                continue
            if not enviar:
                print(f"+ {tpl['name']} (rode com --enviar para criar):")
                print(json.dumps(tpl, ensure_ascii=False, indent=2))
                continue
            resp = http.post(f"{API}/{waba}/message_templates", headers=headers, json=tpl)
            print(f"+ {tpl['name']}: {resp.status_code} {resp.text}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
