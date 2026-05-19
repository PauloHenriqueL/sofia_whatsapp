# Sofia — Bot WhatsApp para Allos

Bot conversacional de WhatsApp para a Allos (clínica-escola de psicologia em Belo Horizonte).

Automação de atendimento de pacientes novos:
- Qualificação via conversa natural (LLM)
- Coleta de dados estruturada
- Cadastro no Hamilton (sistema clínico existente)
- Escalada para Thainá em casos específicos

**Stack**: FastAPI + Postgres + OpenAI + Meta WhatsApp Cloud API

**Status**: 🚧 MVP em desenvolvimento (Passo 1: Webhook eco)

---

## 📋 Setup Local

### Pré-requisitos
- Python 3.11+
- PostgreSQL (ou conexão Neon)

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

**Nota**: Muitos valores estão vazios até a Meta Business Account estar pronta. Você pode deixar placeholders no .env local.

### 5. Rodar aplicação
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

- [x] Passo 1: Esqueleto + Webhook eco ← **Estamos aqui**
- [ ] Passo 2: Enviar mensagens
- [ ] Passo 3: Persistência (Postgres)
- [ ] Passo 4: OpenAI integration
- [ ] Passo 5: Tool calling (cadastro, escalada)
- [ ] Passo 6: Hamilton integration
- [ ] Passo 7: Painel web
- [ ] Passo 8: Polimento + produção

---

## 🤝 Contribuindo

- Siga o fluxo em [CLAUDE.md](./CLAUDE.md)
- Rode `/test` antes de commit
- Rode `/security-review` antes de PR
- Code style: Black, isort, Ruff

---

## 📝 Licença

MIT (Allos)
