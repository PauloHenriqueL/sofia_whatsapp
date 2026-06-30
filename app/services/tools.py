"""Definições das ferramentas (function calling) expostas ao LLM.

Schemas conforme sofia_briefing.md. Os handlers ficam na orquestração do
turno (router webhook) e nos serviços (escalation; Hamilton entra no Passo 6).
"""

CADASTRAR_PACIENTE = "cadastrar_paciente"
ESCALAR_PARA_THAINA = "escalar_para_thaina"

# Motivos válidos de escalada via LLM. 'audio_recebido' não entra aqui porque
# é detectado em código (Passo 5+), não escolhido pelo modelo.
MOTIVOS_ESCALADA = [
    "pedido_humano",
    "neuro_reuniao",
    "preco",
    "prefeitura",
    "gratuidade",
    "presencial",
    "menor_11",
    "crise",
    "outro",
]

# Rótulos legíveis pra Thainá, usados no texto do alerta (template alerta_thaina).
# Inclui 'audio_recebido', que é detectado em código (não escolhido pelo modelo).
MOTIVO_LABELS = {
    "pedido_humano": "pediu pra falar com uma pessoa",
    "neuro_reuniao": "avaliação neuropsicológica",
    "preco": "dúvida ou objeção sobre o preço",
    "prefeitura": "mencionou prefeitura / convênio municipal",
    "gratuidade": "não tem como pagar (gratuidade)",
    "presencial": "quer atendimento presencial",
    "menor_11": "paciente menor de 11 anos (online inviável)",
    "crise": "CRISE / risco — prioridade máxima",
    "audio_recebido": "mandou um áudio (a Sofia não transcreve)",
    "outro": "outro (ver contexto)",
}

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
                    "telefone_apoio": {
                        "type": "string",
                        "description": "Contato de emergência (familiar/referência), opcional",
                    },
                    "endereco": {
                        "type": "string",
                        "description": "Bairro e cidade",
                    },
                    "cep": {"type": "string", "description": "CEP (opcional)"},
                    "horarios_disponiveis": {"type": "string"},
                    "preferencia_terapeuta": {"type": "string"},
                    "motivo_busca": {"type": "string"},
                    "como_conheceu": {
                        "type": "string",
                        "description": (
                            "Como a pessoa disse que conheceu a Allos, literal "
                            "(ex.: 'Instagram', 'indicação de uma amiga', 'Google'). Opcional."
                        ),
                    },
                    "observacoes": {
                        "type": "string",
                        "description": (
                            "Observações livres: qualquer info relevante que a pessoa "
                            "mencionar e que ajude a Thainá/o terapeuta (preferências mais "
                            "detalhadas sobre o terapeuta, contexto do que busca, questões "
                            "ou sensibilidades específicas). Resumo curto e factual, sem "
                            "interpretar clinicamente. Opcional."
                        ),
                    },
                },
                # Mínimo pra registrar um lead. O resto a Thainá completa depois
                # no Hamilton (botão "Completar" no painel). Mantido alinhado ao
                # prompt: não forçar o modelo a inventar endereço/telefone/horário
                # só pra satisfazer o schema (era a origem do bug do "[SEU_NÚMERO]").
                "required": [
                    "nome_completo",
                    "data_nascimento",
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
