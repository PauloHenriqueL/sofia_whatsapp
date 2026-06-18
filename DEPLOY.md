# Guia de Go-Live — Sofia

Passo a passo para colocar a Sofia no ar. O **código está pronto**; aqui é só a
parte operacional (contas, credenciais e deploy). Faça na ordem.

> Legenda: 🧑‍💻 ação sua · ⏳ depende de aprovação externa (pode levar dias).

---

## 0. Visão geral da ordem

```
1. Banco (Neon)            → DATABASE_URL
2. OpenAI                  → OPENAI_API_KEY
3. Hamilton                → mergear branch + usuário JWT + defaults + deploy
4. Meta WhatsApp           → número + token + app secret + template
5. Deploy da Sofia (Render)→ variáveis de ambiente
6. Webhook na Meta         → apontar para a URL do Render
7. Validação ponta a ponta
```

Itens 1, 2 e 3 podem ser feitos **agora**, em paralelo. O item 4 (Meta) é o que
tem prazos de aprovação (template 24–72h).

---

## 1. Banco de dados (Neon) 🧑‍💻

1. Crie um projeto Postgres no [Neon](https://neon.tech) (free tier resolve).
2. Copie a connection string (formato `postgresql://user:senha@host/db`).
3. Guarde como **`DATABASE_URL`** (vai no Render no passo 5).
   - O código converte sozinho para o driver async (`asyncpg`).

As tabelas são criadas no deploy pelo `alembic upgrade head` (já no `render.yaml`).

---

## 2. OpenAI 🧑‍💻

1. Em [platform.openai.com](https://platform.openai.com/api-keys), gere uma API key.
2. Garanta saldo/billing ativo.
3. Guarde como **`OPENAI_API_KEY`**. Modelo padrão: `gpt-4o-mini` (`OPENAI_MODEL`).

---

## 3. Hamilton (sistema clínico) 🧑‍💻

A Sofia cadastra paciente via API REST que **criamos** no Hamilton.

1. **Revisar e mergear** a branch `feat/api-paciente-sofia` no repo `hamilton-api`.
   - Ela torna `Paciente.fk_terapeuta` nulável (migration `0002`) e adiciona os
     endpoints `POST /api/v1/pacientes/` e `GET /api/v1/pacientes/buscar/?telefone=`.
2. **Criar um usuário de serviço** (Django admin) para o bot autenticar via JWT.
   - Guarde login/senha como **`HAMILTON_USERNAME`** / **`HAMILTON_PASSWORD`**.
3. **Garantir defaults**: precisa existir ao menos **1 Clínica** e **1 Modalidade**
   cadastradas. Opcionalmente, fixe quais usar com as variáveis (no ambiente do
   Hamilton): `SOFIA_DEFAULT_CLINICA_ID`, `SOFIA_DEFAULT_MODALIDADE_ID`,
   `SOFIA_DEFAULT_VLR_SESSAO` (padrão `50.00`).
   - A captação `WhatsApp (Sofia)` é criada automaticamente no primeiro cadastro.
4. **Deploy do Hamilton** (o `build.sh` roda `migrate`, aplicando a `0002`).
5. Guarde a URL pública do Hamilton como **`HAMILTON_API_URL`**.

Pacientes criados pela Sofia ficam **sem terapeuta**, status `AGUARDANDO_INICIO`,
para a coordenação fazer o match depois.

> Bug latente **corrigido** na branch: `PacienteSerializer` usava
> `source='fk_captacao.captacao'` (campo correto é `nome`) — serializar um
> Paciente levantava `AttributeError`. Corrigido + teste de regressão.

---

## 4. Meta WhatsApp Cloud API ⏳

1. **Número dedicado**: registre um número novo na Cloud API (não pode estar em
   uso em outro app). Anote o **Phone Number ID** → **`WHATSAPP_PHONE_NUMBER_ID`**.
2. **Token permanente** do system user → **`WHATSAPP_TOKEN`** (não use o de 24h).
3. **App Secret** do app → **`WHATSAPP_APP_SECRET`** (valida a assinatura do webhook).
4. **Verify Token**: defina uma string secreta sua → **`WHATSAPP_VERIFY_TOKEN`**
   (a mesma vai no Render e na config do webhook na Meta).
5. **Número da Thainá** → **`THAINA_WHATSAPP_NUMBER`** (formato `5531999998888`).
6. **Template `alerta_thaina`** ⏳: submeta para aprovação (categoria *Utility*,
   idioma `pt_BR`), texto:
   ```
   Atenção: paciente {{1}} precisa da sua atenção no painel da Sofia. Motivo: {{2}}. Acesse para responder.
   ```
   Aprovação leva 24–72h — **comece isso primeiro**.

---

## 5. Deploy da Sofia no Render 🧑‍💻

1. Conecte o repositório `sofia_whatsapp` ao Render (Blueprint via `render.yaml`).
2. Preencha as variáveis marcadas como secretas no dashboard:
   ```
   WHATSAPP_TOKEN, WHATSAPP_PHONE_NUMBER_ID, WHATSAPP_VERIFY_TOKEN,
   WHATSAPP_APP_SECRET, THAINA_WHATSAPP_NUMBER,
   OPENAI_API_KEY, DATABASE_URL,
   HAMILTON_API_URL, HAMILTON_USERNAME, HAMILTON_PASSWORD,
   PAINEL_PASSWORD, SECRET_KEY
   ```
   (gere segredos com `python -c "import secrets; print(secrets.token_urlsafe(32))"`)
3. O build roda `alembic upgrade head`; o start sobe o Uvicorn; health check `/health`.
4. Anote a URL pública (ex.: `https://sofia.onrender.com`).

---

## 6. Configurar o webhook na Meta 🧑‍💻

1. No app da Meta → WhatsApp → Configuração → Webhooks:
   - **Callback URL**: `https://SUA-URL-RENDER/webhook/whatsapp`
   - **Verify Token**: o mesmo `WHATSAPP_VERIFY_TOKEN`.
2. Assine o campo **`messages`**.
3. A Meta faz um GET de verificação; deve dar ✅ (o app responde o challenge).

---

## 7. Validação ponta a ponta ✅

- [ ] `GET /health` retorna 200.
- [ ] Abrir `https://SUA-URL/` pede usuário/senha (painel) e abre a lista.
- [ ] Mandar uma mensagem de **texto** pro número → a Sofia responde (LLM).
- [ ] Mandar **"quero falar com a Thainá"** → vira modo humano, escalada registrada,
      Thainá recebe o template, e aparece no painel.
- [ ] Mandar um **áudio** → escala automática + mensagem fixa.
- [ ] Completar os dados → conferir o **cadastro no Hamilton** (paciente sem terapeuta).
- [ ] Thainá responde pelo painel → chega no WhatsApp do paciente.

---

## Checklist de credenciais (`.env` / Render)

| Variável | De onde vem |
|---|---|
| `WHATSAPP_TOKEN` | Meta (system user, permanente) |
| `WHATSAPP_PHONE_NUMBER_ID` | Meta (número dedicado) |
| `WHATSAPP_VERIFY_TOKEN` | você define (igual na Meta) |
| `WHATSAPP_APP_SECRET` | Meta (app) |
| `THAINA_WHATSAPP_NUMBER` | número da Thainá |
| `ALERT_TEMPLATE_NAME` | `alerta_thaina` |
| `OPENAI_API_KEY` | OpenAI |
| `DATABASE_URL` | Neon |
| `HAMILTON_API_URL` | URL do Hamilton |
| `HAMILTON_USERNAME` / `HAMILTON_PASSWORD` | usuário de serviço no Hamilton |
| `PAINEL_USER` / `PAINEL_PASSWORD` | login do painel da Thainá |
| `SECRET_KEY` | aleatório |

---

## Segurança (já implementado)

- `.env` nunca é commitado (`.gitignore`); HTTPS é fornecido pelo Render.
- Webhook valida `X-Hub-Signature-256` (HMAC) — exige `WHATSAPP_APP_SECRET` correto.
- CORS fechado (sem `*`/credenciais cross-origin).
- Painel/API com HTTP Basic Auth + checagem de **Origin** (defesa CSRF).
- `/docs` e `/redoc` desabilitados quando `ENVIRONMENT=production`.
- Logs **não** registram conteúdo de mensagem (LGPD) e mascaram telefone.

**Lembretes:**
- Definir `PAINEL_PASSWORD` e `SECRET_KEY` fortes em produção
  (`python -c "import secrets; print(secrets.token_urlsafe(32))"`).
- Basic Auth do painel é adequado para o MVP; trocar por sessão/OAuth depois.
