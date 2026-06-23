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

## 🧩 Como adicionar novas funcionalidades (roteiro)

A Sofia é um bot que **conversa via LLM** e **age através de ferramentas**
(function calling). Quase toda funcionalidade nova é uma **nova ferramenta**
que o modelo aprende a chamar na hora certa (como `cadastrar_paciente` e
`escalar_para_thaina`). Algumas são automáticas (ex.: áudio → escala) ou de
painel (ex.: lista de cadastrados). Este roteiro existe pra agilizar essas
conversas.

### 1. Você me especifica a função

Não precisa ser formal. Quanto mais claro, melhor. Tente cobrir:

1. **Objetivo** — o que faz, em uma frase.
   Ex.: "Quando o paciente já tem consulta marcada no Hamilton, pedir o
   comprovante de pagamento."
2. **Gatilho** — o que dispara? O paciente pede? A Sofia percebe na conversa?
   Roda sozinho em algum momento?
3. **Dados/sistemas** — precisa consultar o Hamilton? Outra API? Qual
   informação ela usa ou grava?
4. **O que a Sofia faz e diz** — a ação concreta e o tom da resposta.
5. **Casos de borda** — e se não houver consulta? Se já pagou? Quando escalar
   pra Thainá?
6. **Credenciais novas (se você já souber)** — alguma API, login ou token novo?

### 2. Eu (Claude) implemento nos lugares certos

- **`app/services/tools.py`** — defino a ferramenta (nome + campos). Regra de
  ouro: **só o essencial como obrigatório**, pra não forçar o modelo a inventar
  dado (foi o que quebrou o cadastro da Maria com `"[SEU_NÚMERO]"`).
- **`app/routers/webhook.py`** (`_executar_tool`) — ligo o nome da ferramenta
  ao código que executa a ação.
- **`app/services/<novo>.py`** — a regra de negócio de verdade (fica no
  serviço, não no router).
- **`app/services/hamilton_client.py`** (ou um cliente novo) — se a função fala
  com o Hamilton ou outra API, o acesso vai aqui.
- **`app/prompts/sofia_v01.txt`** — ensino a Sofia **quando** e **como** usar a
  ferramenta. Sem isso o modelo não usa direito.
- **`app/config.py`** — se precisar de credencial/URL nova, adiciono a
  configuração (e te digo o nome exato da variável).
- **`tests/test_<novo>.py`** — testes pra garantir que funciona.

Rodo os testes e **sempre te falo, no final, o que falta você fazer do lado de
fora** (a parte que eu não consigo sozinho).

### 3. O que VOCÊ talvez precise providenciar

Depende da função. Os casos comuns:

- **Credencial/API nova** (ex.: gateway de pagamento) → você pega a key e
  coloca nas **Env Vars do Render** (e me passa pra eu testar no dev). Eu te
  digo o nome exato da variável.
- **Dado que o Hamilton ainda não expõe** → o Hamilton é outro sistema (repo
  `hamilton-api`). Se a Sofia precisa de algo que a API dele não tem (ex.:
  "listar consultas marcadas de um paciente"), alguém precisa **criar esse
  endpoint lá primeiro**. Eu te aviso e posso ajudar a fazer.
- **Mensagem proativa fora da conversa** (a Sofia falar com o paciente sem ele
  ter escrito nas últimas 24h) → exige um **template aprovado pela Meta**, que
  demora pra aprovar. Eu monto, você submete e espera a aprovação.
- **Receber arquivo/imagem** (ex.: comprovante) → hoje a Sofia só lê texto;
  imagem e áudio têm tratamento próprio. Se a função depende de receber
  arquivo, eu te explico o que muda.
- **Decisões de regra** — quando escalar, o que fazer em caso ambíguo. Melhor
  combinar antes.

### 4. Depois de pronto

`git commit` + `git push` → o Render redeploya sozinho. Credencial nova é a
única coisa que você mexe **no painel do Render** (Environment), não no código.
Configs simples do dia a dia (preço, frases prontas, etc.) você mesmo altera —
peça que eu te lembro onde fica cada uma.

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

### Passo 2: Enviar mensagens ✅
- `whatsapp_client.py` com `enviar_texto(numero, texto)` e `enviar_template(...)`
- Webhook responde 200 na hora e processa em BackgroundTasks (<3s)
- Bot responde "ok, recebi: <msg>"
- **Validar**: paciente → eco (depende do desbloqueio da Meta — ver README)

### Passo 3: Persistência ✅
- Engine async portável: SQLite (aiosqlite) no dev, Postgres (asyncpg) na produção/Neon
- Modelos SQLAlchemy (Conversa, Mensagem, Escalada)
- Alembic migrations (template async, render_as_batch p/ SQLite)
- Webhook persiste antes de responder; idempotência por whatsapp_message_id
- **Validar**: várias msgs → banco atualiza (21 testes passando)

### Passo 4: OpenAI ✅
- `llm_client.py` abstrato + impl OpenAI
- System prompt de arquivo
- Carregar últimas 20 msgs, enviar ao LLM
- Bot responde com texto gerado

### Passo 5: Tool calling + escalada ✅
- Tools `cadastrar_paciente` e `escalar_para_thaina` + handlers + round-trip
- Envio de template `alerta_thaina` pra Thainá

### Passo 6: Hamilton ✅
- `hamilton_client.py` (JWT) com buscar/criar paciente; busca-antes-de-criar
- Endpoint REST criado no `hamilton-api` (branch `feat/api-paciente-sofia`)

### Passo 7: Painel web ✅
- Jinja2 + HTMX (lista 15s, conversa 5s)
- Endpoints `/api/conversas`, `/painel` + HTTP Basic Auth
- Thainá assume/responde/devolve ao bot

### Passo 8: Polimento + produção ✅
- Áudio→escalada automática; imagem/vídeo pedem texto
- Logging seguro (LGPD: sem conteúdo de mensagem) + estruturado (JSON no prod)
- Handler global de erro 500 + degradação graciosa (OpenAI/Hamilton/Cloud API)
- `render.yaml` (build com `alembic upgrade head`, health check `/health`)
- Painel repaginado (design do Hamilton) + tela de login por sessão

### Status de produção (go-live em andamento)
- **No ar**: https://sofia-whatsapp.onrender.com (Render). Login painel: `thaina`.
- **Neon** Postgres com tabelas criadas; **Hamilton** integrado (usuário `sofia-bot`, validado).
- **Número real** registrado na Meta (`+55 31 8667-3359`); credenciais nas Env Vars do Render
  (e em `render.env`, gitignored).
- **Falta (manual)**: configurar o webhook na Meta + assinar `messages`, publicar o app,
  submeter o template `alerta_thaina`, e garantir crédito na OpenAI. Ver `DEPLOY.md`.

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
