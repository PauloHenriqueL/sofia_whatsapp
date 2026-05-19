# Sofia — Bot WhatsApp da Allos

Automação de atendimento de pacientes novos via WhatsApp, integrando com Hamilton (sistema clínico existente em Django) e OpenAI.

## 📋 Visão Geral

**Sofia** é um bot conversacional que:
1. Recebe pacientes novos pelo WhatsApp da Allos
2. Qualifica interesse e coleta dados via conversa natural (LLM)
3. Cadastra no Hamilton quando dados suficientes são coletados
4. Escala para Thainá (humano) em casos específicos (áudio, prefeitura, gratuidade, pedido humano)

**Stack decidida**:
- **Backend**: FastAPI (async, webhook rápido)
- **Banco**: Postgres no Neon
- **LLM**: OpenAI (gpt-4o-mini)
- **Canal**: Meta WhatsApp Cloud API
- **Painel**: Jinja2 + HTMX (server-rendered)
- **Hosting**: Render

**Integração externa crítica**: Hamilton API REST (não é mexido por Sofia, apenas consumido)

---

## 🎯 MVP (Escopo)

### ✅ Em escopo
1. Conversa com paciente novo via WhatsApp com LLM
2. Coleta de dados estruturada (`dados_coletados` em JSONB)
3. Cadastro automático no Hamilton via API REST
4. Escalada para Thainá (áudio, prefeitura, gratuidade, pedido humano)
5. Painel web simples pra Thainá responder

### ❌ Fora de escopo (não implementar)
- NPS
- Transcrição de áudio
- Match automático terapeuta-paciente
- Detecção avançada de crise
- Cobrança/Stripe/Mercado Pago
- Lembretes (sessão em 2h, cobrança mensal)
- Comunicação em grupo
- Cardápio editável de respostas

---

## 🚀 Como Trabalhar

### Fluxo Recomendado

```
1. 🤔 Discussão de arquitetura
   → Converse comigo sobre estrutura, decisões de design
   → Use Plan Mode se for algo grande

2. 💻 Implementação incremental
   → Siga roadmap em passo-a-passo (webhook eco → enviar → persistência → LLM → tools → Hamilton → painel)
   → Cada passo é testável antes do próximo

3. ✅ Rodar `/test`
   → Validar testes passam
   → Cobertura dos handlers principais

4. 🔒 Rodar `/security-review`
   → Credenciais não hardcoded
   → Validação de assinatura webhook
   → Inputs sanitizados
   → Injections evitadas

5. 📦 Commit & Push
```

### Agentes Disponíveis

#### `/test` — Suite de Testes
Valida testes:
- Webhook payload parsing
- Tool calling (cadastro, escalada)
- Conversation flow
- Integração Hamilton (mock)

**Use**: Após implementar handler, antes de fazer commit

#### `/security-review` — Audit de Segurança
Verifica:
- Credenciais em `.env` (não hardcoded)
- Validação de `X-Hub-Signature-256`
- SQL injection, XSS, prompt injection
- HTTPS em tudo
- Auth do painel

**Use**: Antes de cada PR, especialmente webhooks e API

#### `Claude main` — Discussão
Para:
- Arquitetura e design
- Roadmap e planejamento
- Decisões trade-off
- Refatorações

---

## 📊 Arquitetura Rápida

```
[Paciente WhatsApp] 
   ↕ (Meta Cloud API)
[FastAPI App - Render]
   ├─ Webhook: recebe + valida assinatura
   ├─ LLM: OpenAI com tool calling
   ├─ Persistência: Postgres (Neon)
   ├─ Escalada: marca modo humano + alerta template
   ├─ Hamilton: POST cadastro quando pronto
   └─ Painel: Jinja2 + HTMX pra Thainá responder
      ↕ (HTTP Basic Auth)
[Thainá: PC ou celular]
```

### Tabelas (Modelo de Dados)

```sql
conversa
├─ id, numero_whatsapp (unique)
├─ paciente_hamilton_id, modo ('bot'/'humano')
├─ estado ('novo'/'qualificando'/'coletando_dados'/'cadastrado'/'escalado')
├─ dados_coletados (JSONB: nome, nascimento, telefone, apoio, endereço, horários...)
└─ criada_em, atualizada_em

mensagem
├─ id, conversa_id
├─ direcao ('recebida'/'enviada')
├─ origem ('paciente'/'bot'/'thaina')
├─ tipo ('texto'/'audio'/'imagem'/'documento'/'template')
├─ texto, whatsapp_message_id (único), metadata
└─ criada_em

escalada
├─ id, conversa_id
├─ motivo ('pedido_humano'/'prefeitura'/'gratuidade'/'audio_recebido'/'outro')
├─ contexto
├─ criada_em, resolvida_em
```

Idempotência: índice único em `whatsapp_message_id` evita processar mesma msg 2x.

---

## 🔄 Fluxos Principais

### Fluxo de Mensagem Paciente

1. Meta envia POST `/webhook/whatsapp`
2. App responde 200 **imediatamente**, processa async
3. Cria ou busca `conversa` por número
4. Persiste mensagem
5. **Se `modo = humano`**: pára (painel mostra mensagem)
6. **Se `modo = bot`**:
   - Carrega últimas 20 mensagens
   - Chama OpenAI com system prompt + histórico
   - OpenAI retorna: texto + tool calls opcionais
   - Processa tool calls:
     - `escalar_para_thaina(motivo)`: marca humano, registra escalada, envia template
     - `cadastrar_paciente(dados)`: POST Hamilton, atualiza `paciente_hamilton_id`
   - Envia resposta via Cloud API
   - Persiste mensagem enviada

### Fluxo de Resposta Thainá (Painel)

1. Thainá digita no painel e clica enviar
2. Painel POST `/api/conversas/{id}/responder`
3. App persiste com `origem = thaina`
4. App envia via Cloud API pro paciente

### Detecção de Áudio (Escalada Imediata)

1. Webhook recebe `type = audio`
2. Persiste com `texto = '[áudio recebido]'`
3. Marca `modo = humano`
4. Registra escalada com `motivo = audio_recebido`
5. Envia template de alerta pra Thainá
6. Responde ao paciente: "Vou chamar a Thainá..."

---

## ⚠️ Considerações Críticas de Segurança

### Credenciais
```python
# ❌ NUNCA:
WHATSAPP_TOKEN = "EAABa..."  # hardcoded!

# ✅ SIM:
import os
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
```

### Validação de Webhook
```python
# Toda requisição POST /webhook/whatsapp DEVE validar:
from hmac import compare_digest
import hashlib

X_Hub_Signature = request.headers.get("X-Hub-Signature-256")
expected = f"sha256={hmac.new(
    APP_SECRET.encode(), 
    body.encode(), 
    hashlib.sha256
).hexdigest()}"

if not compare_digest(X_Hub_Signature, expected):
    return 403  # Rejeita
```

### OpenAI Prompt Injection
- Input do paciente vai pro LLM via histórico estruturado, não concatenado
- LLM tem instruções claras sobre quando escalar (sensibilidades, sinais)
- Tool calling vinculado a motivos específicos, não livre

### Hamilton Falha
- Se Hamilton retornar erro, marca `conversa.estado = cadastro_pendente`
- Loga erro detalhado
- **Não propaga erro pro paciente** (user-facing)
- Thainá resolve manualmente

---

## 📁 Estrutura de Pastas

```
sofia/
├── CLAUDE.md                  # Este arquivo
├── sofia_briefing.md          # Especificação completa (referência)
├── .claude/
│   └── settings.json         # Config de agentes
├── .env.example              # Template
├── .gitignore                # *.env, __pycache__, .venv, etc
├── pyproject.toml            # Dependências + config
├── README.md                 # Setup e deploy
├── alembic.ini               # Config migrations
├── render.yaml               # Deploy config
│
├── app/
│   ├── __init__.py
│   ├── main.py               # FastAPI app + rotas
│   ├── config.py             # Settings (pydantic-settings)
│   ├── database.py           # Engine async, session, Base SQLAlchemy
│   ├── models.py             # SQLAlchemy models (Conversa, Mensagem, Escalada)
│   ├── schemas.py            # Pydantic (WebhookPayload, etc)
│   ├── dependencies.py       # Auth, db session
│   │
│   ├── routers/
│   │   ├── __init__.py
│   │   ├── webhook.py        # GET/POST /webhook/whatsapp
│   │   ├── api.py            # GET /api/conversas, POST /api/conversas/{id}/responder
│   │   ├── painel.py         # GET /painel, /painel/conversas/{id}
│   │   └── health.py         # GET /health
│   │
│   ├── services/
│   │   ├── __init__.py
│   │   ├── conversation.py   # Orquestrador principal
│   │   ├── whatsapp_client.py # Wrapper Cloud API (enviar_texto, enviar_template)
│   │   ├── llm_client.py     # Interface abstrata + impl OpenAI
│   │   ├── hamilton_client.py # Wrapper API Hamilton
│   │   └── escalation.py     # Lógica de escalada
│   │
│   ├── prompts/
│   │   └── sofia_v01.txt     # System prompt versionado
│   │
│   ├── templates/            # Jinja2
│   │   ├── base.html
│   │   ├── painel_lista.html
│   │   ├── painel_conversa.html
│   │   └── login.html
│   │
│   └── static/
│       ├── htmx.min.js
│       └── style.css
│
├── alembic/
│   ├── env.py
│   └── versions/             # Migration files
│
├── tests/
│   ├── __init__.py
│   ├── test_webhook.py       # Validação payload
│   ├── test_conversation.py  # Fluxo mensagem
│   └── test_escalation.py    # Tool calling
│
└── logs/                     # Local dev (ignorar em git)
```

---

## 🛣️ Roadmap de Implementação

Cada passo é **testável** antes do próximo. Use `/test` regularmente.

### Passo 1: Esqueleto + Webhook em modo eco ✅
- FastAPI app + config
- GET `/webhook/whatsapp` (validar challenge)
- POST `/webhook/whatsapp` (validar assinatura, logar)
- GET `/health`
- Deploy no Render
- **Validar**: mandar msg WhatsApp → ver payload no log

### Passo 2: Enviar mensagens
- `whatsapp_client.py` com `enviar_texto(numero, texto)`
- Bot responde "ok, recebi: <msg>"
- **Validar**: paciente → eco

### Passo 3: Persistência
- Neon + DATABASE_URL
- Modelos SQLAlchemy (Conversa, Mensagem, Escalada)
- Alembic migrations
- Webhook persiste antes de responder
- **Validar**: várias msgs → banco atualiza

### Passo 4: OpenAI
- `llm_client.py` abstrato + impl OpenAI
- System prompt de arquivo
- Carregar últimas 20 msgs, enviar ao LLM
- Bot responde com texto gerado
- **Validar**: conversa fluida em português

### Passo 5: Tool calling + escalada
- Adicionar tools `cadastrar_paciente` e `escalar_para_thaina`
- Handlers de tool call
- Envio de template `alerta_thaina` pra Thainá
- **Validar**: paciente "quero falar com Thainá" → escalada funciona

### Passo 6: Hamilton
- `hamilton_client.py` com buscar/criar paciente
- Handler de tool call cadastro
- **Validar**: cadastro end-to-end funciona

### Passo 7: Painel web
- Jinja2 + HTMX (lista, conversa)
- Endpoints `/api/conversas`, `/painel`
- HTTP Basic Auth
- **Validar**: Thainá assume e responde

### Passo 8: Polimento + produção
- Refresh automático HTMX (15s lista, 5s conversa)
- Logging estruturado
- Tratamento de erros (Hamilton offline, OpenAI quota)
- Submissão template na Meta (1 semana antes go-live)
- README + documentação

---

## 🔑 Variáveis de Ambiente

```bash
# WhatsApp Cloud API
WHATSAPP_TOKEN=                    # Token permanente
WHATSAPP_PHONE_NUMBER_ID=          # ID do número
WHATSAPP_VERIFY_TOKEN=             # String secreta (definida por nós)
WHATSAPP_APP_SECRET=               # Pra validar X-Hub-Signature-256

# Thainá
THAINA_WHATSAPP_NUMBER=            # Ex: 5531999998888
ALERT_TEMPLATE_NAME=alerta_thaina  # Nome do template

# OpenAI
OPENAI_API_KEY=
OPENAI_MODEL=gpt-4o-mini

# Banco
DATABASE_URL=                      # postgres://... Neon

# Hamilton
HAMILTON_API_URL=                  # Ex: https://hamilton.allos.org.br
HAMILTON_API_KEY=

# Painel
PAINEL_USER=thaina
PAINEL_PASSWORD=                   # Random

# Geral
LOG_LEVEL=INFO
ENVIRONMENT=production             # ou development
```

---

## 💡 Principles

- **Simplicidade**: cada passo do roadmap é testável
- **Sem scope creep**: nada do "fora de escopo"
- **Escalação segura**: em dúvida, escala (ação errada é risco)
- **Credenciais seguras**: tudo em `.env`, nada hardcoded
- **Async first**: webhook responde em <3s
- **Logs estruturados**: cada ação importante é logada

---

## 📚 Referências

- [sofia_briefing.md](./sofia_briefing.md) — Especificação técnica completa
- [Meta Cloud API Docs](https://developers.facebook.com/docs/whatsapp/cloud-api)
- [OpenAI API](https://platform.openai.com/docs/api-reference)
- [FastAPI](https://fastapi.tiangolo.com/)
- [SQLAlchemy Async](https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html)

---

**Dica**: Sempre rode `/test` e `/security-review` ao longo do desenvolvimento. Não deixa pra no final! 🛡️
