# Sofia — Bot WhatsApp para Allos

Bot conversacional de WhatsApp para a Allos (clínica-escola de psicologia em Belo Horizonte).

Automação de atendimento de pacientes novos:
- Qualificação via conversa natural (LLM)
- Coleta de dados estruturada
- Cadastro no Hamilton (sistema clínico existente)
- Escalada para Thainá em casos específicos

**Stack**: FastAPI + Postgres/SQLite + OpenAI + Meta WhatsApp Cloud API

**Status**: 🚧 MVP em desenvolvimento (Passo 3 concluído: persistência)

---

## 📋 Setup Local

### Pré-requisitos
- Python 3.11+
- Banco: **SQLite** já funciona no dev local (sem instalar nada). Postgres/Neon é usado em produção.

### 1. Clonar repositório
```bash
git clone https://github.com/allos/sofia.git
cd sofia
```

### 2. Criar virtual environment
```bash
python3 -m venv venv
source venv/bin/activate  # Linux/Mac
# ou
venv\Scripts\activate  # Windows
```

### 3. Instalar dependências
```bash
pip install -r requirements.txt
```

### 4. Configurar .env
```bash
cp .env.example .env
# Editar .env com seus valores (veja commentários no .env.example)
```

**Nota**: Muitos valores estão vazios até a Meta Business Account estar pronta. Você pode deixar placeholders no .env local. O `DATABASE_URL` padrão local é `sqlite:///sofia_dev.db`.

### 5. Criar/atualizar o banco (migrations)
```bash
alembic upgrade head
```
Roda as migrations no banco apontado por `DATABASE_URL` (SQLite local ou Postgres/Neon).

### 6. Rodar aplicação
```bash
uvicorn app.main:app --reload
```

Acessa: `http://localhost:8000`

- API docs: `http://localhost:8000/docs`
- Healthcheck: `http://localhost:8000/health`

---

## 🧪 Testes

Rodar suite de testes:
```bash
pytest tests/ -v
```

Com cobertura:
```bash
pytest tests/ --cov=app --cov-report=html
```

Ou use o skill Claude Code:
```
/test
```

---

## 🔒 Segurança

Antes de fazer push:
```bash
/security-review
```

Checklist:
- ✅ Nenhuma credencial hardcoded em código
- ✅ `.env` está em `.gitignore`
- ✅ Validação de assinatura webhook (`X-Hub-Signature-256`)
- ✅ HTTPS em produção (Render fornece)

---

## 📚 Documentação

- [sofia_briefing.md](./sofia_briefing.md) — Especificação técnica completa
- [CLAUDE.md](./CLAUDE.md) — Workflow de desenvolvimento
- [Meta WhatsApp Cloud API](https://developers.facebook.com/docs/whatsapp/cloud-api)
- [OpenAI API Reference](https://platform.openai.com/docs/api-reference)

---

## 🚀 Deployment

### Render

Configurar no `render.yaml`:
```yaml
services:
  - type: web
    name: sofia
    env: python
    buildCommand: pip install -r requirements.txt && alembic upgrade head
    startCommand: uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

Variáveis de ambiente no dashboard do Render (todas as vars do `.env.example`).

---

## 🛣️ Roadmap

- [x] Passo 1: Esqueleto + Webhook eco
- [x] Passo 2: Enviar mensagens (`whatsapp_client.py`, eco real via Cloud API)
- [x] Passo 3: Persistência (SQLAlchemy async, Alembic, idempotência)
- [x] Passo 4: OpenAI integration (`llm_client.py`, system prompt versionado)
- [x] Passo 5: Tool calling (cadastro, escalada) + alerta à Thainá
- [x] Passo 6: Hamilton integration (cliente JWT + endpoint REST no Hamilton)
- [x] Passo 7: Painel web (Jinja2 + HTMX + Basic Auth)
- [x] Passo 8: Polimento + produção (áudio→escalada, LGPD, deploy) ← **MVP completo**

### Detalhe do que já está pronto

**Passo 2 — Enviar mensagens**
- [`app/services/whatsapp_client.py`](app/services/whatsapp_client.py): `enviar_texto()` e `enviar_template()` (httpx async, Graph API v18).
- Webhook responde 200 na hora e processa em `BackgroundTasks` (<3s exigido pela Meta).
- Parser `extrair_mensagens()` ignora eventos de status (entregue/lido).

**Passo 3 — Persistência**
- [`app/database.py`](app/database.py): engine async portável — SQLite (`aiosqlite`) no dev, Postgres (`asyncpg`) na produção/Neon. Driver derivado do esquema do `DATABASE_URL`.
- [`app/models.py`](app/models.py): `Conversa`, `Mensagem`, `Escalada` (JSON portável JSONB/JSON, timestamps com timezone).
- [`app/services/conversation.py`](app/services/conversation.py): cria/busca conversa, persiste mensagens e garante **idempotência** por `whatsapp_message_id` (índice único parcial).
- Migrations Alembic em [`alembic/`](alembic/) (template async, `render_as_batch` pra SQLite).
- 21 testes passando nesse passo.

**Passo 4 — OpenAI**
- [`app/services/llm_client.py`](app/services/llm_client.py): interface `LLMClient` (abstrata, trocável) + `OpenAIClient` async. System prompt versionado em [`app/prompts/sofia_v01.txt`](app/prompts/sofia_v01.txt).
- `conversation.carregar_historico()`: últimas 20 mensagens no formato role/content.
- Falha do LLM degrada para uma mensagem de fallback (não derruba a conversa).

**Passo 5 — Tool calling + escalada**
- [`app/services/tools.py`](app/services/tools.py): schemas de `cadastrar_paciente` e `escalar_para_thaina`.
- [`app/services/escalation.py`](app/services/escalation.py): marca `modo=humano`/`estado=escalado`, registra a escalada e alerta a Thainá via template.
- O webhook executa as tools e faz um *round-trip* ao LLM para a fala final.

**Passo 6 — Hamilton**
- [`app/services/hamilton_client.py`](app/services/hamilton_client.py): cliente HTTP async, auth **JWT** (usuário/senha → token), `buscar_paciente_por_telefone()` e `criar_paciente()` (normaliza telefone, remove DDI 55).
- `cadastrar_paciente` busca-antes-de-criar; falha do Hamilton → `estado=cadastro_pendente` (Thainá cadastra manual).
- Lado Hamilton (repo `hamilton-api`, branch `feat/api-paciente-sofia`): endpoints REST `POST /api/v1/pacientes/` e `GET /api/v1/pacientes/buscar/?telefone=`; paciente "lead" criado **sem terapeuta** até a coordenação fazer o match.

**Passo 7 — Painel web (Thainá)**
- [`app/routers/painel.py`](app/routers/painel.py) (HTML + HTMX) e [`app/routers/api.py`](app/routers/api.py) (JSON), protegidos por **HTTP Basic Auth** ([`app/dependencies.py`](app/dependencies.py)).
- Lista de conversas com filtros e auto-refresh (15s); chat por conversa com auto-refresh (5s), campo de resposta e botões "Assumir"/"Devolver ao bot".
- Acesso em `/` → `/painel/` (usuário/senha de `PAINEL_USER`/`PAINEL_PASSWORD`). Templates Jinja2 em [`app/templates/`](app/templates/), estilo Pico.css + HTMX (CDN).

**Passo 8 — Polimento + produção**
- **Áudio → escalada automática**: mensagem de áudio é persistida como `[áudio recebido]`, escala pra Thainá (`motivo=audio_recebido`, sem passar pelo LLM) e responde uma mensagem fixa. Imagem/vídeo/sticker pedem texto.
- **LGPD**: o webhook loga só metadados (quantidade, tipos e ids) — nunca o **conteúdo** das mensagens (dado de saúde sensível).
- **Logging estruturado**: [`app/logging_config.py`](app/logging_config.py) — texto no dev, JSON na produção (`LOG_JSON=true`).
- **Erros**: handler global loga stack trace e responde 500 genérico; falhas de OpenAI/Hamilton/Cloud API já degradam sem derrubar a conversa.
- **Deploy**: [`render.yaml`](render.yaml) (build com `alembic upgrade head`, start Uvicorn, health check `/health`, segredos via dashboard).

**Extra — Painel repaginado**
- Identidade visual do Hamilton (Allos Design System): paleta teal `#2E9E8F`, fontes Fraunces/DM Sans, Bootstrap Icons, cards e badges ([`app/static/allos.css`](app/static/allos.css)).
- **Tela de login** própria (no lugar do popup Basic Auth) com **sessão por cookie assinado** ([`app/routers/auth.py`](app/routers/auth.py)).
- **52 testes passando** no total.

---

## 🟢 Status de produção (go-live em andamento)

| Peça | Status |
|---|---|
| Sofia no ar (Render) | ✅ **https://sofia-whatsapp.onrender.com** |
| Banco Postgres (Neon) | ✅ tabelas criadas |
| Integração Hamilton (cadastro) | ✅ endpoints no ar + usuário `sofia-bot` (validado) |
| WhatsApp (token, número, app secret) | ✅ número real registrado (`+55 31 8667-3359`) |
| OpenAI key | ✅ (confirmar **crédito/saldo** na conta) |
| Painel + login | ✅ `https://sofia-whatsapp.onrender.com/` (login `thaina`) |
| **Webhook na Meta** | ⏳ configurar Callback + assinar `messages` |
| **Publicar o app na Meta** | ⏳ pra receber msgs de qualquer paciente |
| **Template `alerta_thaina`** | ⏳ submeter (aprovação 24–72h) |

> Variáveis de produção ficam em `render.env` (gitignored) e nas Env Vars do Render.
> O `.env` local segue em SQLite para desenvolvimento.

### Próximos passos pro go-live
1. Na Meta: **WhatsApp → Configuração → Webhooks** → Callback `https://sofia-whatsapp.onrender.com/webhook/whatsapp` + verify token → **assinar `messages`**.
2. **Publicar o app** na Meta (modo Live) para receber mensagens de qualquer número.
3. Submeter o template **`alerta_thaina`** (Utility, pt_BR) — necessário pros alertas de escalada.
4. Confirmar **crédito na OpenAI** (senão a Sofia cai na mensagem de fallback).

---

## 🤝 Contribuindo

- Siga o fluxo em [CLAUDE.md](./CLAUDE.md)
- Rode `/test` antes de commit
- Rode `/security-review` antes de PR
- Code style: Black, isort, Ruff

---

## 📝 Licença

MIT (Allos)
