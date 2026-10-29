"""Cria na Meta os dois templates dos lembretes dos terapeutas (conta Sofia).

    WHATSAPP_WABA_ID=<id da conta> python scripts/criar_templates_lembretes.py            # só mostra
    WHATSAPP_WABA_ID=<id da conta> python scripts/criar_templates_lembretes.py --enviar   # cria

Lê `WHATSAPP_TOKEN` do `.env`. O ID da conta (WABA) aparece no Gerenciador do
WhatsApp, conta "Sofia": é o `waba_id=` da barra de endereço.

Template aprovado não se edita: mudou uma vírgula, é template novo. Por isso o
script pula os que já existem com o mesmo nome. Os textos precisam bater com as
variáveis que o Hamilton monta (principais/lembretes.py).
"""

import json
import os
import sys

import httpx
from dotenv import load_dotenv

API = "https://graph.facebook.com/v21.0"
RODAPE = {"type": "FOOTER", "text": "Mensagem automática do Hamilton"}

TEMPLATES = [
    {
        "name": "agenda_do_dia_terapeuta",
        "language": "pt_BR",
        "category": "UTILITY",
        "components": [
            {
                "type": "BODY",
                "text": "Bom dia, {{1}}! Sua agenda de hoje: {{2}} e {{3}}. Os detalhes estão no Hamilton.",
                "example": {"body_text": [["Paulo", "4 sessões (08:00, 10:00, 14:00 e 17:00)", "1 atividade"]]},
            },
            RODAPE,
        ],
    },
    {
        "name": "pendencias_semana_terapeuta",
        "language": "pt_BR",
        "category": "UTILITY",
        "components": [
            {
                "type": "BODY",
                "text": (
                    "Olá, {{1}}! Resumo da semana passada: {{2}} sessões realizadas e {{3}} faltas "
                    "registradas. Pendências: {{4}} pagamentos do mês em aberto, {{5}} pacientes sem "
                    "sessão realizada na semana e {{6}} pacientes sem a 1ª sessão registrada. "
                    "Acesse o Hamilton para ver os detalhes."
                ),
                "example": {"body_text": [["Paulo", "12", "2", "1", "3", "1"]]},
            },
            RODAPE,
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
