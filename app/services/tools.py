"""Definições das ferramentas (function calling) expostas ao LLM.

Schemas conforme sofia_briefing.md. Os handlers ficam na orquestração do
turno (router webhook) e nos serviços (escalation; Hamilton entra no Passo 6).
"""

CADASTRAR_PACIENTE = "cadastrar_paciente"
ESCALAR_PARA_THAINA = "escalar_para_thaina"

# Motivos válidos de escalada via LLM. 'audio_recebido' não entra aqui porque
# é detectado em código (Passo 5+), não escolhido pelo modelo.
MOTIVOS_ESCALADA = ["pedido_humano", "prefeitura", "gratuidade", "outro"]

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": CADASTRAR_PACIENTE,
            "description": (
                "Cadastrar o paciente no sistema Hamilton quando todos os "
                "dados necessários foram coletados"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "nome_completo": {"type": "string"},
                    "data_nascimento": {
                        "type": "string",
                        "description": "Formato YYYY-MM-DD",
                    },
                    "telefone_contato": {"type": "string"},
                    "telefone_apoio": {"type": "string"},
                    "endereco": {"type": "string"},
                    "horarios_disponiveis": {"type": "string"},
                    "preferencia_terapeuta": {"type": "string"},
                    "motivo_busca": {"type": "string"},
                },
                "required": [
                    "nome_completo",
                    "data_nascimento",
                    "telefone_contato",
                    "telefone_apoio",
                    "endereco",
                    "horarios_disponiveis",
                ],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": ESCALAR_PARA_THAINA,
            "description": (
                "Escalar a conversa pra coordenadora humana (Thainá) em casos " "específicos"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "motivo": {"type": "string", "enum": MOTIVOS_ESCALADA},
                    "contexto": {
                        "type": "string",
                        "description": (
                            "Resumo curto do que aconteceu pra Thainá saber o " "contexto"
                        ),
                    },
                },
                "required": ["motivo"],
            },
        },
    },
]
