# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

---

# Sofia — Bot WhatsApp da Allos

Automação de atendimento de pacientes novos via WhatsApp, integrando com Hamilton (sistema clínico existente em Django) e OpenAI.

> **Idioma**: todo o projeto (código, comentários, docs, commits) é em **português brasileiro**. Mantenha esse padrão ao contribuir.

## 📋 Visão Geral

**Sofia** é um bot conversacional que:
1. Recebe pacientes novos pelo WhatsApp da Allos
2. Qualifica interesse e coleta dados via conversa natural (LLM)
3. Cadastra no Hamilton quando dados suficientes são coletados
4. Escala para Thainá (humano) em casos específicos (áudio, prefeitura, gratuidade, pedido humano)

**Stack decidida**:
- **Backend**: FastAPI (async, webhook rápido)
- **Banco**: Postgres no Neon
- **LLM**: OpenAI (modelo configurável via `OPENAI_MODEL`; produção usa gpt-5.x)
- **Canal**: Meta WhatsApp Cloud API
- **Painel**: Jinja2 + HTMX (server-rendered)
- **Hosting**: Render

**Integração externa crítica**: Hamilton API REST (não é mexido por Sofia, apenas consumido)

---

## 🛠️ Comandos de Desenvolvimento

```bash
# Setup (uma vez)
python -m venv venv
venv\Scripts\activate            # Windows  (Linux/Mac: source venv/bin/activate)
pip install -r requirements.txt
copy .env.example .env            # Windows  (Linux/Mac: cp) — ver gotcha abaixo

# Banco (cria/atualiza o schema no DATABASE_URL — SQLite local ou Postgres/Neon)
alembic upgrade head
alembic revision --autogenerate -m "descrição"   # nova migration (render_as_batch p/ SQLite)

# Rodar a app (http://localhost:8000 → redireciona pro /painel/; /docs só em dev)
uvicorn app.main:app --reload

# Testes
pytest tests/ -v                                  # suite inteira
pytest tests/test_webhook.py -v                   # um arquivo
pytest tests/test_seguimento.py::TestRodarSeguimentos::test_envia_marca_e_nao_reenvia   # um teste
pytest -k "seguimento and envia"                  # por nome
pytest tests/ --cov=app --cov-report=html         # cobertura

# Lint / format (config em pyproject.toml: line-length 100, profile black)
black .
isort .
ruff check .
mypy app
```

Skills do projeto: **`/test`** (suite) e **`/security-review`** (audit antes de PR).

> ⚠️ **Gotcha — precisa de `.env` até pra rodar testes.** `app/config.py` faz
> `settings = Settings()` **no import**, e `Settings` tem campos obrigatórios sem
> default (`whatsapp_token`, `database_url`, `whatsapp_app_secret`, `openai_api_key`,
> `painel_password`, etc.). `app/database.py` cria o `engine` no import a partir
> de `settings.database_url`. Como quase tudo importa esses módulos (e `pytest`
> importa `app.main`), **sem um `.env` preenchido — ou as env vars exportadas —
> nada importa e nenhum teste coleta.** Não existe `conftest.py`: cada teste sobe
> seu próprio SQLite in-memory e mocka as chamadas externas (OpenAI/WhatsApp/
> Hamilton), então os valores do `.env` podem ser dummy; use `DATABASE_URL=sqlite:///sofia_dev.db`.

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
   ├─ Painel: Jinja2 + HTMX pra Thainá responder (login por sessão/cookie assinado)
   └─ Tasks: POST /tasks/seguimentos (cron externo → follow-up de lead parado)
      ↕
[Thainá: PC ou celular]
```

### Tabelas (Modelo de Dados)

```sql
conversa
├─ id, numero_whatsapp (unique)
├─ paciente_hamilton_id, modo ('bot'/'humano')
├─ estado ('novo'/'qualificando'/'coletando_dados'/'cadastrado'/'cadastro_pendente'/'escalado')
├─ dados_coletados (JSONB: nome, nascimento, telefone, apoio, endereço, horários...)
├─ seguimento_enviado_em (NULL = ainda não; garante 1 follow-up por conversa — Frente 2)
├─ cobranca_resolvida_em (NULL = pendente; "Marcar resolvido" tira da cobrança — Demanda 4)
└─ criada_em, atualizada_em

configuracao  (chave/valor — valores editáveis no painel /painel/config)
├─ id, chave (unique), valor (texto; int OU "true"/"false" conforme o tipo do campo)
└─ atualizada_em

mensagem
├─ id, conversa_id
├─ direcao ('recebida'/'enviada')
├─ origem ('paciente'/'bot'/'thaina')
├─ tipo ('texto'/'audio'/'imagem'/'documento'/'template')
├─ texto, whatsapp_message_id (único), metadata
└─ criada_em

escalada
├─ id, conversa_id
├─ motivo ('pedido_humano'/'neuro_reuniao'/'preco'/'prefeitura'/'gratuidade'/'presencial'/'menor_11'/'crise'/'audio_recebido'/'outro')
├─ contexto
├─ criada_em, resolvida_em
```

Idempotência: índice único parcial em `whatsapp_message_id` evita processar mesma msg 2x.
A coluna ORM `metadata` da `mensagem` é mapeada como atributo `extra` (`metadata` é reservado no SQLAlchemy).

---

## 🧠 Arquitetura atual (além do MVP) — módulos e comportamentos não óbvios

O MVP (Passos 1–8) está pronto. Depois dele entraram 3 frentes; estes são os
pontos que **exigem ler vários arquivos** pra entender:

### Onde mora cada coisa (camadas)
- **`app/routers/webhook.py`** — orquestra o turno do bot: chama o LLM com `tools.TOOLS`,
  executa as tools (`_executar_tool`) e faz o **round-trip** (reenvia o resultado da tool
  ao modelo pra ele gerar a fala final). Áudio: com transcrição ligada, vira texto e passa
  pelo LLM; senão (ou se falhar), escala **sem passar pelo LLM**.
- **`app/services/`** — toda regra de negócio fica aqui, **nunca no router**:
  `conversation` (persistência + idempotência + histórico), `llm_client` (abstração
  `LLMClient` + `OpenAIClient`, singleton via `get_llm_client()`), `tools` (schemas de
  function calling), `escalation`, `cadastro`, `hamilton_client`, `whatsapp_client`,
  `config_negocio`, `seguimento`, `metricas`, `painel`, `serializacao`, `transcricao`
  (áudio→texto), `acompanhamento` (Demandas 3/4).

### Serialização + debounce por conversa (Demanda 2 — `serializacao.py`)
Ponto **não óbvio** que exige ler webhook + serializacao juntos:
- O webhook **não responde por mensagem**. `ingerir_mensagem` persiste sob um **lock por
  número** (`serializacao.lock_da_conversa`) — isso serializa a conversa (sem chamadas
  concorrentes ao LLM) e mata a corrida de criar 2x a conversa na primeira mensagem.
- Texto normal **não é respondido na hora**: `serializacao.agendar` (re)agenda um timer de
  `settings.debounce_segundos` (`DEBOUNCE_SEGUNDOS`, prod=6). Cada mensagem nova **reseta o
  timer**, então uma rajada vira **uma** chamada ao LLM e **uma** resposta (o histórico já
  inclui todas as mensagens da rajada). `_turno_agendado` roda depois da janela, sob o lock.
- **Não espera a janela**: áudio (escala na hora), tipos sem texto (pede texto) e **texto de
  crise** (`_contem_sinal_de_crise` — heurística de palavras; o acolhimento/escalada em si
  continua no LLM). Idempotência por `whatsapp_message_id` **continua** como defesa contra
  reentrega — o lock/debounce é camada adicional, não substituto.
- **Premissa: 1 instância** (Render free). Locks/timers são em memória; múltiplas instâncias
  exigiriam lock distribuído. Nos testes: `serializacao.aguardar_pendentes()` espera os
  timers e `limpar()` isola o estado global; o `_dormir` do debounce é ligado à função real
  pra não ser afetado por mocks de `asyncio.sleep`.
- **Singletons trocáveis/mockáveis**: `llm_client.get_llm_client()` e
  `hamilton_client.get_hamilton_client()` são `@lru_cache` — ponto único de troca de
  provedor e de mock nos testes.

### Valores editáveis no painel (`config_negocio.py` — `/painel/config`)
- Editáveis pela Thainá em **`/painel/config`**, **sem mexer no código nem no Render**:
  preço terapia, preço neuro, parcelas, horas do follow-up (`followup_horas`), **segundos de
  debounce** (`debounce_segundos`), **"digitando…/visto"** (`simular_digitacao`, bool) e
  **ouvir áudio** (`transcrever_audio`, bool).
- `CAMPOS` é **tipado**: `(rótulo, padrão, "int"|"bool")`. Campo bool vira checkbox no painel
  e é guardado como `"true"/"false"`. O `webhook` lê `simular_digitacao`/`debounce_segundos`
  via `config_negocio.valor(...)` (não mais `settings.*`).
- Há um **cache em memória** (`_cache`) populado no startup (`main.lifespan` →
  `config_negocio.carregar_do_banco`) e atualizado a cada `salvar()`. Lê-se via
  `config_negocio.valor(chave)` / `valores()`. Assume **1 instância** no Render free.
- O default de cada campo vem das `settings` (env/código) e o valor salvo no painel (banco)
  **tem prioridade**. Se a config não carregar no startup (ex.: tabela ainda não migrada), o
  app sobe com os padrões.
- **Injeção no prompt**: `llm_client.carregar_system_prompt()` substitui tokens
  `{{PRECO_TERAPIA}}`, `{{PRECO_TERAPIA_SESSAO}}` e `{{DATA_HOJE}}` (data do dia, pra Sofia
  calcular idade na verificação <12/12-17/18+) em `app/prompts/sofia_v01.txt` com os valores do
  cache. `{{PRECO_NEURO}}`/`{{PARCELAS_MAX}}` ainda são injetados, mas o prompt v2 não os usa
  (neuro vai direto pra Thainá). O arquivo é cacheado; a substituição é refeita a cada turno.
- **Base de conhecimento (prompt v2)**: `carregar_system_prompt()` anexa
  `docs/sofia-base-conhecimento.md` ao system prompt (cacheada). **Esse arquivo é load-bearing
  em runtime, não é só doc — não mover/apagar.** O `docs/contrato-terapeutico-allos.md` **não** é
  carregado de propósito (só referência interna; a Sofia nunca cita verbatim).

### Follow-up de lead parado (Frente 2 — `seguimento.py` + `routers/tasks.py`)
- Um **cron externo** bate em `POST /tasks/seguimentos` (protegido por `TASKS_TOKEN`,
  header `X-Tasks-Token` ou `?token=`; token vazio = endpoint **desligado**, 403).
- `rodar_seguimentos()` acha leads que pararam de responder dentro da janela
  `[followup_horas, 24h)` (ainda no bot, sem cadastro, sem follow-up prévio) e manda **uma**
  mensagem de texto livre. Depois de 24h da última msg do paciente a Meta exige template,
  por isso o follow-up tem que sair antes. `seguimento_enviado_em` garante 1 por conversa.

### Dashboard de KPIs (Frente 3 — `metricas.py`, `/painel/metricas`)
- Métricas (conversão, autonomia, escaladas por motivo, leads/dia, recuperados) são
  **derivadas das tabelas existentes**. O agrupamento por dia é feito **em Python**
  (não em SQL) pra ficar portável entre SQLite (dev) e Postgres (prod).

### Áudio: a Sofia ouve e responde em texto (`transcricao.py` + webhook)
- Ligado pela flag `transcrever_audio` (painel). Quando ligada, `ingerir_mensagem` baixa a
  mídia (`whatsapp_client.baixar_midia` — GET `/{media_id}` → URL → bytes, mesmo token JWT do
  WhatsApp) e transcreve (`transcricao.transcrever_audio`, OpenAI Whisper, `OPENAI_AUDIO_MODEL`).
- A transcrição vira o **texto** da mensagem (tipo continua `audio`) e o áudio passa a valer
  como texto: entra no histórico, respeita debounce/serialização/crise, e a **resposta sai em
  texto** (a Sofia **nunca manda áudio de volta**). A transcrição também **aparece no painel**
  pra Thainá ler.
- **Fallback**: se baixar/transcrever falhar (ou a flag estiver off), mantém o comportamento
  antigo — escala pra Thainá (`audio_recebido`). **LGPD**: o conteúdo transcrito **não é
  logado**, só o tamanho.

### Acompanhamento pós-cadastro (Demandas 3/4 — `acompanhamento.py`, `/painel/acompanhamento`)
- Cruza as conversas cadastradas pela Sofia (com `paciente_hamilton_id`) com o status da 1ª
  consulta no Hamilton (endpoint novo `GET /api/v1/pacientes/status-primeira-consulta/?ids=`,
  consumido por `hamilton_client.status_primeira_consulta`).
- **Demanda 3 — espera pela 1ª consulta**: quem ainda não teve a 1ª consulta realizada
  (`is_primeira_consulta` + `is_realizado` no Hamilton), com dias desde o cadastro, ordenado
  do mais urgente, destaque em vermelho > 7 dias (a meta).
- **Demanda 4 — pronto pra cobrança**: quem já teve a 1ª consulta e ainda não foi resolvido;
  botão "Marcar resolvido" seta `conversa.cobranca_resolvida_em` (tira da lista).
- Hamilton fora do ar → a página mostra um aviso, não quebra.

### Painel: auth por sessão (não é mais HTTP Basic)
- Login próprio em **`/login`** → cookie de sessão assinado (`SessionMiddleware`,
  `secret_key`). `app/dependencies.py`: `requer_login_pagina` (HTML → 303 p/ `/login`),
  `requer_login_api` (JSON → 401), `verificar_origem` (defesa CSRF por header `Origin`).
  Credenciais comparadas em tempo constante (`secrets.compare_digest`).

### Cadastro no Hamilton (`cadastro.py` + `hamilton_client.py`)
- **Busca-antes-de-criar** por telefone; cria um **lead sem terapeuta** (a coordenação faz
  o match depois). Falha do Hamilton → `estado = cadastro_pendente` (não propaga erro pro
  paciente; a Thainá re-tenta pelo botão em `/painel/conversas/{id}/cadastrar`).
- `cadastrar_paciente` exige só `nome_completo` + `data_nascimento` (ver `tools.py`); se o
  telefone coletado for inválido/placeholder, cai pro número do WhatsApp da conversa
  (`_garantir_telefone`). Isso foi o fix do bug do `"[SEU_NÚMERO]"` — **não volte a tornar
  campos obrigatórios só pra satisfazer o schema.**
- Auth do Hamilton é **JWT** (username/password → Bearer; re-autentica 1x no 401).

### Portabilidade SQLite↔Postgres (`database.py`)
- `_async_url()` converte `postgres://`/`postgresql://` → `postgresql+asyncpg://` e
  **remove** params libpq que o asyncpg não aceita (`sslmode`, `channel_binding` que o Neon
  adiciona); o TLS é ligado via `connect_args={"ssl": True}`. SQLite vira `sqlite+aiosqlite`.
- Tipo JSON portável: `JSON().with_variant(JSONB(), "postgresql")`.

### LGPD / logs
- **Nunca logar conteúdo de mensagem** (dado de saúde sensível) — só metadados
  (qtd, tipos, ids). Telefones em log passam por `utils.mascarar_telefone` (`***8888`).
- `logging_config.py`: texto no dev, JSON na prod (`LOG_JSON=true`).

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
   - Quebra a resposta em **bolhas** (parágrafos separados por linha em branco) e envia em ordem via Cloud API, persistindo cada uma
   - Com `SIMULAR_DIGITACAO=true`: marca a mensagem como lida, mostra "digitando…" e espaça as bolhas no tempo (ritmo humano)

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
│   ├── config.py             # Settings (pydantic-settings) — instanciado no import
│   ├── database.py           # Engine async, session, Base; URL portável SQLite/Postgres
│   ├── models.py             # Conversa, Mensagem, Configuracao, Escalada
│   ├── dependencies.py       # Auth por sessão, CSRF (verificar_origem), get_db
│   ├── logging_config.py     # Logging texto (dev) / JSON (prod)
│   ├── utils.py              # mascarar_telefone (LGPD)
│   │                         # (não há schemas.py — WebhookPayload é inline no webhook)
│   │
│   ├── routers/
│   │   ├── webhook.py        # GET/POST /webhook/whatsapp (orquestra o turno do bot)
│   │   ├── auth.py           # GET/POST /login, /logout (sessão)
│   │   ├── api.py            # API JSON do painel
│   │   ├── painel.py         # /painel, /painel/config, /painel/metricas, conversas
│   │   ├── tasks.py          # POST /tasks/seguimentos (cron externo, X-Tasks-Token)
│   │   └── health.py         # GET /health
│   │
│   ├── services/
│   │   ├── conversation.py   # Persistência, idempotência, histórico p/ LLM
│   │   ├── whatsapp_client.py # Wrapper Cloud API (enviar_texto, enviar_template, dividir_em_bolhas, marcar_como_lida)
│   │   ├── llm_client.py     # LLMClient abstrato + OpenAI + injeção de valores no prompt
│   │   ├── hamilton_client.py # Wrapper API Hamilton (JWT)
│   │   ├── cadastro.py       # Cadastro no Hamilton (busca-antes-de-criar)
│   │   ├── escalation.py     # Lógica de escalada + alerta à Thainá
│   │   ├── config_negocio.py # Valores de negócio editáveis (cache + tabela configuracao)
│   │   ├── seguimento.py     # Follow-up de lead parado (Frente 2)
│   │   ├── metricas.py       # KPIs do painel (Frente 3)
│   │   └── painel.py         # Queries/ações do painel da Thainá
│   │
│   ├── prompts/
│   │   └── sofia_v01.txt     # System prompt versionado
│   │
│   ├── templates/            # Jinja2 (HTMX via CDN)
│   │   ├── base.html, _topbar.html, login.html
│   │   ├── painel_lista.html, painel_conversa.html
│   │   ├── painel_config.html, painel_metricas.html
│   │   └── _conversas_fragment.html, _mensagens_fragment.html
│   │
│   └── static/
│       ├── allos.css         # Allos Design System (paleta Hamilton)
│       └── style.css
│
├── alembic/
│   ├── env.py
│   └── versions/             # Migration files
│
├── tests/                    # sem conftest.py; cada teste sobe SQLite in-memory e mocka externos
│   ├── test_webhook.py, test_conversation.py, test_escalation.py
│   ├── test_cadastro.py, test_hamilton.py, test_llm.py
│   ├── test_painel.py, test_metricas.py, test_seguimento.py
│   └── test_config_negocio.py, test_utils.py
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
- Endpoints `/api/conversas`, `/painel` (auth por sessão/cookie a partir do Passo 8; antes Basic Auth)
- Thainá assume/responde/devolve ao bot

### Passo 8: Polimento + produção ✅
- Áudio→escalada automática; imagem/vídeo pedem texto
- Logging seguro (LGPD: sem conteúdo de mensagem) + estruturado (JSON no prod)
- Handler global de erro 500 + degradação graciosa (OpenAI/Hamilton/Cloud API)
- `render.yaml` (build com `alembic upgrade head`, health check `/health`)
- Painel repaginado (design do Hamilton) + tela de login por sessão

### Frentes pós-MVP ✅ (já no `main`)
- **Frente 1 — Neuro + valores configuráveis** (`config_negocio.py`): fluxo de neuro
  (v2 escala `neuro_reuniao`; objeção de preço escala `preco`) e valores editáveis no painel.
- **Frente 2 — Follow-up de lead parado**: `seguimento.py` + `POST /tasks/seguimentos`
  (cron externo, `TASKS_TOKEN`). Uma mensagem dentro da janela de 24h da Meta.
- **Frente 3 — Dashboard de KPIs**: `metricas.py` + `/painel/metricas`.
- **Demanda 2 — Serialização + debounce** (`serializacao.py`): rajada vira 1 resposta; sem
  corrida na 1ª msg; crise não espera a janela.
- **Presença humana**: "digitando…" + visto (tiques azuis) via `whatsapp_client.marcar_como_lida`
  (Graph API **v23**, senão o typing é ignorado). Toggle `simular_digitacao` no painel.
- **Áudio (ouvir + responder texto)**: `transcricao.py` (Whisper). Toggle `transcrever_audio`.
- **Demandas 3/4 — Acompanhamento** (`acompanhamento.py`, `/painel/acompanhamento`): espera
  pela 1ª consulta + pronto pra cobrança, via endpoint novo no Hamilton.

### Status de produção (no ar e funcionando)
- **No ar**: https://sofia-whatsapp.onrender.com (Render). Login painel: `thaina`.
- **Neon** Postgres migrado; **Hamilton** integrado (usuário `sofia-bot`) e com o endpoint
  `status-primeira-consulta` deployado. **Número real** na Meta (`+55 31 8667-3359`).
- **Validado em produção**: recebe/responde texto, escala pra Thainá, presença humana
  (digitando/visto), e transcrição de áudio (o áudio vira texto no painel).
- **Config em runtime**: preço/parcelas/follow-up/debounce/digitando/áudio se mudam em
  **`/painel/config`** (sem Render). Segredos ficam nas Env Vars do Render (e em `render.env`,
  gitignored). Cron do follow-up = `TASKS_TOKEN` + job no cron-job.org (ver `DEPLOY.md`).
- **Opcionais na fila**: Demanda 1 (observabilidade de duplicatas — a duplicação em si já foi
  resolvida pela Demanda 2) e KPI distribuição terapia×neuro.

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
OPENAI_MODEL=gpt-4o-mini       # ex.: gpt-5.4 (precisa do SDK openai 2.x)
OPENAI_TEMPERATURE=0.7         # vazio/none = não envia (usa padrão do modelo)
OPENAI_AUDIO_MODEL=whisper-1   # transcrição de áudio (STT)

# Banco
DATABASE_URL=                      # postgres://... Neon (ou sqlite:///sofia_dev.db no dev)

# Hamilton (auth JWT: username/password -> Bearer)
HAMILTON_API_URL=                  # Ex: https://hamilton.allos.org.br
HAMILTON_USERNAME=                 # usuário sofia-bot
HAMILTON_PASSWORD=
HAMILTON_API_KEY=                  # legado/opcional

# Valores editáveis em runtime no /painel/config (o env é só o valor INICIAL/default)
PRECO_TERAPIA_MENSAL=200
PRECO_NEURO=1200
PARCELAS_MAX=5
FOLLOWUP_HORAS=20                  # < 24 (janela da Meta)
DEBOUNCE_SEGUNDOS=6                 # janela de agrupamento de rajada (prod=6)
TRANSCREVER_AUDIO=false            # ouvir/transcrever áudio (custo por minuto)

# Painel + sessão
PAINEL_USER=thaina
PAINEL_PASSWORD=                   # Random
SECRET_KEY=                        # assina o cookie de sessão (trocar em prod)

# Tarefas agendadas (cron externo dos follow-ups; vazio = endpoint desligado)
TASKS_TOKEN=

# Geral
LOG_LEVEL=INFO
LOG_JSON=false                     # true na produção (logs estruturados)
ENVIRONMENT=production             # ou development
SIMULAR_DIGITACAO=false            # "digitando…" + visto (tiques azuis). Editável no /painel/config
```

> **Editáveis no painel** (`/painel/config`, tabela `configuracao`): `PRECO_*`, `PARCELAS_MAX`,
> `FOLLOWUP_HORAS`, `DEBOUNCE_SEGUNDOS`, `SIMULAR_DIGITACAO`, `TRANSCREVER_AUDIO`. O env define
> só o **default inicial**; o valor salvo no painel manda. Segredos ficam **só** no Render.

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
