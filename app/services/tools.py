"""Definições das ferramentas (function calling) expostas ao LLM.

Schemas conforme docs/referencia/sofia_briefing.md. Os handlers ficam na orquestração do
turno (router webhook) e nos serviços (escalation; Hamilton entra no Passo 6).
"""

CADASTRAR_PACIENTE = "cadastrar_paciente"
ESCALAR_PARA_THAINA = "escalar_para_thaina"
OFERECER_DESCONTO = "oferecer_desconto"
REGISTRAR_RESPOSTA_PESQUISA = "registrar_resposta_pesquisa"

# Campos que a tool da pesquisa aceita gravar, e o tipo de cada um. É allowlist:
# nome fora daqui é descartado, porque o modelo escolhe o `campo` e um typo dele
# não pode virar 400 no Hamilton nem dado no lugar errado.
#
# Só numéricos e booleanos. Texto (`feedback_livre`, `motivo_encerramento`)
# continua saindo da extração no fim: errar texto custa pouco, e forçar uma tool
# pra texto longo atrapalha a conversa.
CAMPOS_PESQUISA_NOTA = (
    "individual",  # ORS 1 — bem-estar pessoal
    "interpessoal",  # ORS 2 — relacionamentos próximos
    "social",  # ORS 3 — convívio no dia a dia
    "geral",  # ORS 4 — estado geral
    "qualidade_geral",  # nota do TERAPEUTA (campo reusado no Hamilton)
    "nota_indicacao",  # NPS Allos
    "nota_sofia",  # acolhimento/encaminhamento feito pela Sofia
)
CAMPOS_PESQUISA_BOOLEANO = (
    "continuar_terapeuta",  # encaixe com o terapeuta
    "continuar_allos",  # reoferta no encerramento
    "consentimento_paciente",
)
CAMPOS_PESQUISA = CAMPOS_PESQUISA_NOTA + CAMPOS_PESQUISA_BOOLEANO

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
# Inclui 'audio_recebido' e 'anexo_recebido', detectados em código (não escolhidos
# pelo modelo) — por isso não aparecem em MOTIVOS_ESCALADA.
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
    "anexo_recebido": "mandou uma imagem ou documento (veja no painel)",
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
                    "captacao_id": {
                        "type": "integer",
                        "description": (
                            "ID da captação (origem) da lista de origens fornecida no "
                            "system prompt, escolhido a partir do que a pessoa contou. "
                            "OMITA este campo se nenhuma opção da lista corresponder "
                            "claramente ao que ela disse: captação errada é pior que "
                            "captação em branco. Nunca invente um ID."
                        ),
                    },
                    "is_parceria": {
                        "type": "boolean",
                        "description": (
                            "true somente quando a pessoa é funcionária de uma "
                            "prefeitura/órgão conveniado e confirmou isso. Nesse caso o "
                            "atendimento é custeado pelo parceiro e a pessoa não paga nada."
                        ),
                    },
                    "vinculo_parceria": {
                        "type": "string",
                        "description": (
                            "Registro da declaração de vínculo com o parceiro, quando "
                            "houver (ex.: 'Declarou ser servidor da Prefeitura de "
                            "Materlândia'). Opcional."
                        ),
                    },
                    "quem_fala": {
                        "type": "string",
                        "enum": ["paciente", "acompanhante"],
                        "description": (
                            "Quem está escrevendo neste WhatsApp: 'paciente' se é a "
                            "própria pessoa que vai ser atendida, 'acompanhante' se é "
                            "mãe, pai, responsável, cônjuge ou outra pessoa cuidando "
                            "do contato. Use o que ficou claro na conversa; OMITA se "
                            "não ficou. Isso decide se a pesquisa pode perguntar à "
                            "pessoa como ELA se sente — um acompanhante não tem como "
                            "responder isso por ela."
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
    {
        "type": "function",
        "function": {
            "name": OFERECER_DESCONTO,
            "description": (
                "Pedir autorização pra oferecer um desconto na mensalidade da terapia, "
                "quando o valor cheio é um impeditivo real pra pessoa começar. Chame "
                "SÓ depois de já ter sustentado o valor, ou quando ela disser de forma "
                "explícita que não tem como pagar. Você NÃO escolhe o valor: esta "
                "ferramenta devolve quanto você pode oferecer. Nunca invente um valor "
                "nem prometa desconto antes de chamá-la. Vale só pra terapia; na "
                "avaliação neuropsicológica quem trata valor é a Amanda, na reunião."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "motivo": {
                        "type": "string",
                        "description": (
                            "O que a pessoa disse que torna o valor inviável, nas "
                            "palavras dela (ex.: 'está desempregada desde março'). "
                            "Fica registrado pra coordenação."
                        ),
                    },
                },
                "required": ["motivo"],
            },
        },
    },
]

# Ferramentas do MODO PESQUISA. Não entram em TOOLS: durante a pesquisa a pessoa
# já é paciente, e oferecer `cadastrar_paciente` ali seria convidar o modelo a
# recadastrar quem já está cadastrado.
TOOLS_PESQUISA = [
    {
        "type": "function",
        "function": {
            "name": REGISTRAR_RESPOSTA_PESQUISA,
            "description": (
                "Registrar UMA resposta numérica ou de sim/não que o paciente "
                "acabou de dar na pesquisa. Chame esta ferramenta assim que "
                "receber cada resposta, antes de fazer a próxima pergunta. "
                "Nunca chame para uma pergunta que o paciente ainda não "
                "respondeu, e nunca invente um valor."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "campo": {
                        "type": "string",
                        "enum": list(CAMPOS_PESQUISA),
                        "description": "Qual pergunta do roteiro o paciente respondeu.",
                    },
                    "valor": {
                        "type": ["integer", "boolean"],
                        "description": (
                            "Nota inteira de 0 a 10 para os campos de nota, ou "
                            "true/false para os campos de sim/não. Se o paciente "
                            "respondeu em palavras e não deu um número, NÃO chame "
                            "esta ferramenta."
                        ),
                    },
                },
                "required": ["campo", "valor"],
            },
        },
    },
]
