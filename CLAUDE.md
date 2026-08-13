# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

---

# ⏭️ ESTADO ATUAL E PRÓXIMO PASSO (leia primeiro)

**Data deste registro:** 08/08/2026.

O sistema está **em produção** e o MVP (P0–P6) foi entregue há tempos. As Demandas
A, B, C e D estão **implementadas e testadas**. O que falta é **ligar** e conferir
o ambiente — nenhuma delas exige código novo.

> ⚠️ **Aviso de leitura.** As seções de status deste arquivo já ficaram velhas duas
> vezes (diziam "Demanda D não iniciada" com o Stripe inteiro implementado, e
> "a Demanda C não roda" com o Hamilton pronto). **Antes de acreditar em qualquer
> "não existe" aqui, confira no código.** As seções de arquitetura são confiáveis;
> as de status envelhecem.

### 1. Ligar a pesquisa e a cobrança (nenhuma linha de código)
- `SOFIA_PESQUISAS_ATIVAS=true` nas env vars do **Hamilton**. Default é `false`, e
  com ela desligada `GET /api/v1/avaliacoes/pendentes/` devolve **lista vazia** —
  o cron roda e não sai pesquisa nenhuma. Há mais duas travas lá
  (`SOFIA_PESQUISAS_IDADE_MAXIMA_DIAS=7`, `SOFIA_PESQUISAS_LIMITE=5`).
  ⚠️ **Isso já não vale pra pesquisa de ENTRADA (ORS de linha de base)**: desde
  10/08 ela emenda no cadastro, tem interruptor próprio (`pesquisa_entrada_ativa`
  em `/painel/config`, **nasce ligada**) e não passa pela fila de pendentes.
- **`cobranca_ativa`** em `/painel/config`. Mesmo desenho: nasce desligada.
- **Crons** no cron-job.org, mesmo `TASKS_TOKEN`: `POST /tasks/pesquisas`,
  `POST /tasks/cobrancas` e **`POST /tasks/stripe`** (diário; encerra o parcelado
  do neuro na última parcela — sem ele a assinatura cobra pra sempre, e isso já
  aconteceu com 18 pacientes). **Sem cron, nada sai.**
- `alembic upgrade head` (head atual: **`a7b8c9d0e1f2`**).

### 2. Modelo da tabela de avaliação + planilha de qualidade
📄 [`docs/demandas/02-modelo-de-avaliacao.md`](docs/demandas/02-modelo-de-avaliacao.md)

O modelo de perguntas foi fechado em grilling (Q1–Q38) e **está implementado nos
dois lados**: quatro questionários (entrada / 1ª sessão / reencaminhamento /
encerramento), ORS como bloco fechado colhido antes da primeira sessão,
`qualidade_geral` reusado como nota do terapeuta, tool de registro incremental e
alertas pra Thainá em nota < 6. O que resta é **decisão de negócio, não código**:
revisar o conteúdo das perguntas com o Paulo e decidir o destino da planilha que
o time de Qualidade usa hoje.

### ⚠️ Antes de qualquer deploy
1. **Migrations do Hamilton no `.gitignore`.** O `.gitignore` do `hamilton-api`
   ignora `**/migrations/**`. Elas **existem no repo local** (até a `0007`), mas
   confirme que chegaram ao GitHub antes de subir — sem elas as Demandas A e C
   quebram com erro de coluna inexistente.
2. 🔴 **`SOFIA_API_DATABASE_URL` no serviço do Hamilton no Render.** Se estiver
   setada lá, **todo cadastro vindo da Sofia — inclusive paciente real — vai pro
   banco de teste** (ver "Ambientes e bancos" abaixo). Confira o dashboard.
3. Resto em **"Pendências que bloqueiam o deploy"** no
   [`01-EM-ANDAMENTO.md`](docs/demandas/01-EM-ANDAMENTO.md).

### Onde está o resto
- **O que foi feito, bugs corrigidos e riscos aceitos:** [`docs/demandas/01-EM-ANDAMENTO.md`](docs/demandas/01-EM-ANDAMENTO.md)
- **Índice da documentação:** [`docs/README.md`](docs/README.md)
- **Como o sistema funciona no dia a dia:** [`docs/referencia/workflow.md`](docs/referencia/workflow.md)

> Este repo é a **Sofia**. O **Hamilton** (`../hamilton-api`, Django) é o sistema
> clínico. **Parte do trabalho recente mexeu nos dois** — inclusive um bug nos
> signals do Hamilton que gravava no banco errado.

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

**Integração externa crítica**: Hamilton API REST (`../hamilton-api`, Django).

> ⚠️ **Isto mudou.** Por muito tempo o Hamilton era só consumido, nunca alterado. Desde o
> ciclo de 08/2026 **a Sofia exige mudanças lá** (flag `is_parceria` em `Captacao`, campos de
> resposta na `Avaliacao`, endpoints de avaliação, e o intake aceitando captação e valor).
> Ao mexer numa demanda que envolva dado do Hamilton, **conte com editar os dois repos** —
> e lembre que o Hamilton é usado por terapeutas e coordenação, então mudança lá tem
> blast radius maior que aqui.

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
pytest --cov --cov-report=term-missing            # cobertura (config no pyproject)

# Lint / format (config em pyproject.toml: line-length 100, profile black)
black .
isort .
ruff check .
mypy app
```

Skills do projeto: **`/test`** (suite) e **`/security-review`** (audit antes de PR).

> ⚠️ **Gotcha — cobertura precisa de `concurrency = ["thread", "greenlet"]`.** Já está no
> `pyproject.toml`. O SQLAlchemy async roda dentro de **greenlets**; sem isso o coverage
> perde esses frames e reporta rotas cobertas como **não** cobertas (o router do painel
> aparecia com 63% quando a cobertura real é 90%). Não "conserte" cobertura sem antes
> checar se a linha realmente não executa. Use `--cov` (sem `=app`), que lê a config.

> 🔴 **A suite não fala com a internet — `tests/conftest.py` trava o transporte.**
> Ele existe por um motivo só, e não tem fixture compartilhada (o desenho de "cada
> teste sobe o seu SQLite e mocka o que precisa" continua valendo). O motivo: um
> teste de pagamento mockava `criar_checkout_session`; quando o serviço passou a
> chamar `criar_payment_link`, o mock deixou de cobrir o caminho e o `pytest`
> **criou quatro Payment Links na conta LIVE do Stripe**, com a chave real do
> `.env`. A mesma trava revelou que 13 testes do webhook faziam `GET` de verdade em
> `/api/v1/captacoes/` do Hamilton a cada turno.
> A trava é em `httpx.AsyncHTTPTransport`/`HTTPTransport`, **não** no `Client` — o
> `TestClient` do FastAPI também é httpx, sobre `ASGITransport`, e travar o cliente
> derruba a suite do painel inteira. `RedeBloqueada` herda de `httpx.TransportError`
> pra que o código exercite a degradação que já sabe fazer em vez de estourar.
> Teste que precise mesmo de rede: `@pytest.mark.rede`.

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

> ⚠️ Esta lista é o escopo **do MVP**, congelada. Quatro itens **saíram** dela
> desde então (marcados abaixo) — não use este bloco pra concluir que algo "não
> deve existir". O escopo atual está nas demandas.

- ~~NPS~~ → **existe** (`nota_indicacao`, Demanda C)
- ~~Transcrição de áudio~~ → **existe** (`transcricao.py`, toggle no painel)
- Match automático terapeuta-paciente
- Detecção avançada de crise *(há só a heurística de palavras do debounce)*
- ~~Cobrança/Stripe~~ → **existe** (`pagamentos.py` no painel, `cobranca.py` no bot)
- ~~Lembretes~~ → **existe** parcialmente (follow-up de lead, lembrete de pesquisa
  e de cobrança). Lembrete de sessão em 2h continua fora.
- Mercado Pago (o gateway é Stripe; Pix é chave fixa, sem API)
- Comunicação em grupo
- Cardápio editável de respostas *(mas os prompts são editáveis em `/painel/prompts`)*

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
- **`prompt/sofia_v01.txt`** — ensino a Sofia **quando** e **como** usar a
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
   └─ Tasks: POST /tasks/seguimentos (cron → follow-up de lead parado)
             POST /tasks/pesquisas   (cron → pesquisa de satisfação)
             POST /tasks/cobrancas   (cron → mensalidade pós-1ª sessão)
             POST /tasks/stripe      (cron → encerra o parcelado do neuro na Nª parcela)
      ↕
[Thainá: PC ou celular]
```

### Tabelas (Modelo de Dados)

```sql
conversa
├─ id, numero_whatsapp (unique)
├─ paciente_hamilton_id, modo ('bot'/'humano')
├─ estado ('novo'/'qualificando'/'coletando_dados'/'cadastrado'/'cadastro_pendente'/'escalado')
├─ dados_coletados (JSONB: nome, nascimento, telefone, apoio, endereço, horários,
│                   captacao_id, is_parceria, vinculo_parceria...)
├─ seguimento_enviado_em (NULL = ainda não; garante 1 follow-up por conversa — Frente 2)
├─ cobranca_resolvida_em (NULL = pendente; "Marcar resolvido" tira da cobrança — Demanda 4)
├─ aviso_escalada_em (NULL = ainda não avisou; 1 aviso por escalada — Demanda B)
├─ pesquisa_avaliacao_id (NULL = sem pesquisa; pk da Avaliacao no Hamilton — Demanda C)
├─ pesquisa_iniciada_em (base do lembrete de 20h e do encerramento de 44h)
├─ cobranca_iniciada_em (NUNCA limpo: garante 1 cobrança por pessoa — Demanda D)
├─ cobranca_encerrada_em (preenchido + iniciada = modo cobrança DESLIGADO)
├─ cobranca_lembrete_em, cobranca_status (rótulo na fila do acompanhamento)
├─ stripe_ref (vínculo paciente ↔ Stripe; sub_/cs_/cus_/plink_/URL)
├─ desconto_oferecido_em, desconto_valor, desconto_motivo (auditoria)
└─ criada_em, atualizada_em

configuracao  (chave/valor — valores editáveis no painel /painel/config)
├─ id, chave (unique), valor (Text; int, "true"/"false" OU texto livre —
│                             conforme o tipo declarado em config_negocio.CAMPOS)
└─ atualizada_em

mensagem
├─ id, conversa_id
├─ direcao ('recebida'/'enviada')
├─ origem ('paciente'/'bot'/'thaina')
├─ tipo ('texto'/'audio'/'image'/'document'/'template')
├─ texto, whatsapp_message_id (único; guardado também nas ENVIADAS, p/ reply)
├─ responde_a_id (FK auto-referência: mensagem citada; SET NULL) — P4
├─ metadata
└─ criada_em

midia  (anexo do paciente; bytes no banco — a URL da Meta expira)
├─ id, mensagem_id (CASCADE)
├─ mime, nome_arquivo, tamanho
├─ conteudo (LargeBinary, deferred: o poll do painel não carrega os bytes)
└─ criada_em

escalada
├─ id, conversa_id
├─ motivo ('pedido_humano'/'neuro_reuniao'/'preco'/'prefeitura'/'gratuidade'/'presencial'/'menor_11'/'crise'/'audio_recebido'/'anexo_recebido'/'outro')
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
  (áudio→texto), `acompanhamento` (Demandas 3/4), `captacao` (origem do paciente),
  `pesquisa` (satisfação pós-1ª sessão e de encerramento).

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
  calcular idade na verificação <12/12-17/18+) em `prompt/sofia_v01.txt` com os valores do
  cache. `{{PRECO_NEURO}}`/`{{PARCELAS_MAX}}` ainda são injetados, mas o prompt v2 não os usa
  (neuro vai direto pra Thainá). O arquivo é cacheado; a substituição é refeita a cada turno.
- **Base de conhecimento**: `carregar_system_prompt()` anexa
  `prompt/sofia-base-conhecimento.md` ao system prompt. **Load-bearing em runtime, não é só doc.**
  O `prompt/contrato-terapeutico-allos.md` **não** é enviado ao modelo (só referência interna).
- **Prompts editáveis no painel (`config_prompt.py` — `/painel/prompts`)**: os 3 arquivos de
  `prompt/` são o **padrão**; a Thainá edita no painel e o texto salvo (tabela `configuracao`,
  por isso `valor` é `Text`) passa a valer — "Resetar" volta pro arquivo. `carregar_system_prompt`
  lê `config_prompt.texto("prompt_sistema")` + `("prompt_base")` (não mais os arquivos direto). O
  contrato é editável, mas **não** vai pro bot (rotulado no painel). Cache em memória (1 instância),
  carregado no `main.lifespan` junto com `config_negocio`.

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

### Origem do paciente e parceria (`captacao.py`) — Demanda A
- **A Sofia é o canal, não a origem.** Até aqui todo cadastro ia pro Hamilton com a
  captação fixa `"WhatsApp (Sofia)"`, o que apagava de onde o paciente veio. Agora ela
  pergunta na conversa, o **modelo escolhe um ID da lista real** (`GET /api/v1/captacoes/`,
  injetada no prompt via `{{LISTA_CAPTACOES}}`) e o ID é **validado** em
  `webhook._validar_captacao` antes de virar cadastro.
- **Captação errada é pior que captação vazia** (contamina relatório e prestação de contas).
  ID que não está na lista → cai na captação **"Não sei"** e a coordenação corrige. O
  `is_parceria` **nunca** vem na palavra do modelo: vem da flag do Hamilton.
- **Parceria (prefeitura/convênio)** é `Captacao.is_parceria` no Hamilton — **fonte única**.
  Antes havia duas divergentes: `PREFEITURAS_CAPTACAO_IDS = {13,46}` em `views.py` e um
  `nome contém 'Prefeitura'` no relatório. As duas saíram.
- Paciente de parceria vai com **`vlr_sessao = 0`** e `tipo_pagamento='parceria'` (o Hamilton
  zera por conta própria também — checagem dupla, porque o dado veio de uma conversa).
- **Elegibilidade é auto-declarada** (a pessoa diz que é servidora). Fica registrada na
  `observacao` — é a única evidência se a prefeitura questionar uma consulta. Ver o risco
  registrado em `docs/demandas/01-EM-ANDAMENTO.md`.

### Aviso único pós-escalada (`escalation.AVISO_EM_ATENDIMENTO`) — Demanda B
- Escalar deixa a conversa em modo humano e a Sofia **ficava totalmente muda**: quem escrevia
  "e aí, alguma novidade?" não recebia nada até alguém abrir o painel.
- Agora ela responde **uma vez** (`webhook._avisar_escalada_uma_vez`), texto fixo, **sem LLM**
  (em modo humano ela não pode retomar o fluxo nem escrever por cima da Thainá). Repetir a
  cada mensagem seria pior que o silêncio. `aviso_escalada_em` é zerado ao devolver ao bot.

### Pesquisa de satisfação (`pesquisa.py`) — Demanda C
Herda o trabalho que a Juliana fazia à mão. Exige ler `pesquisa.py` + `signals.py` do Hamilton:
- **A Sofia puxa, o Hamilton não empurra.** Os signals do Hamilton **já criavam** a `Avaliacao`
  com `status='pendente'` quando o terapeuta lança a 1ª consulta ou uma alta/desistência — a
  fila já existia no banco. Um webhook rodaria **dentro do request do terapeuta** (o Hamilton
  é 100% síncrono, sem worker) e travaria o salvamento do prontuário. Puxar dá **retry de
  graça**: o pendente fica lá até ser respondido. Cron: `POST /tasks/pesquisas`.
- **`status='pendente'` é "sem resposta", não "sem envio".** Por isso `sofia_enviada_em` e
  `sofia_lembrete_em` na `Avaliacao` — sem eles a pessoa seria abordada a cada tick.
- **Modo pesquisa**: com `conversa.pesquisa_avaliacao_id` preenchido, o turno roda com o
  prompt da pesquisa no lugar do de acolhimento (a pessoa já é paciente, não é lead).
- **O modelo conduz e o modelo extrai** (decisão consciente, ver o risco em `docs/demandas/01-EM-ANDAMENTO.md`):
  no fim, uma chamada separada vira JSON e vai por PATCH. `_normalizar_extracao` é a defesa —
  **allowlist de campos**, nota fora de 0-10 descartada, data ilegível omitida, `"sim"` não
  vira booleano. Ela não pega uma nota *lida errado*; nada pega.
- **Marcadores `[[PESQUISA_CONCLUIDA]]`/`[[PESQUISA_RECUSADA]]`** sinalizam o fim. São
  removidos antes do envio **e** filtrados no `saida.limpar()` — se o modelo puser um no meio
  da frase, o choke point do P0 pega.
- **Encerramento adapta o texto** por `tipo_saida` e `cancelador` (`_contexto_encerramento`):
  reencaminhamento **não é saída** (a pessoa continua na Allos), alta não é abandono, e quem
  foi desligado pelo terapeuta não pode ouvir "por que *você* decidiu interromper?".
- **Prazos**: lembrete único em 20h, encerra em 44h. Colado na janela de 24h da Meta (passada
  ela, só template). Recusa e silêncio caem os dois em `nao_respondeu`.
- **Quem nunca falou com a Sofia não recebe pesquisa** — sem conversa aberta e fora da janela
  de 24h, não há como abordar. São pulados em silêncio. **Em aberto** (ver `docs/demandas/01-EM-ANDAMENTO.md`).

#### A pesquisa de ENTRADA (ORS de linha de base) é a exceção de tudo acima
É a única que a Sofia **cria**, e desde 10/08 é a única que **não passa pelo cron**:
- **Emenda no cadastro** (`pesquisa.iniciar_entrada`, chamada por `webhook._responder_turno`
  logo depois de enviar a fala que confirma o cadastro). Antes ela dependia de três condições
  invisíveis em série — 3h de espera, **duas** voltas do cron (uma criava a `Avaliacao`, a
  outra abordava) e `SOFIA_PESQUISAS_ATIVAS` no Hamilton — e **na prática não acontecia**.
- **Interruptor próprio**: `pesquisa_entrada_ativa` no `/painel/config`, **nasce ligada**.
  Ela não fala de dinheiro e não tem como virar disparo em massa (só dispara em cadastro que
  acabou de acontecer); as travas do Hamilton existem pra segurar o acumulado histórico de
  pendentes, que aqui não existe.
- **Só terapia.** `pesquisa._e_neuro` pula quem tem escalada `neuro_reuniao` (mesmo resolvida)
  ou "neuro" no `motivo_busca`/`observacoes`. O Hamilton não ajuda: `Paciente` não tem tipo de
  serviço (`fk_modalidade` é online/presencial).
- **Todas as guardas num lugar só** (`pesquisa.motivo_para_pular_entrada`, usada pela emenda
  **e** pela rede): desligada no painel, cadastro não concluído, **reencontro**
  (`cadastro.CHAVE_CADASTRO_NOVO` — ficha que já existia não é alguém começando), pesquisa ou
  cobrança em curso, modo humano, conversa arquivada, **acompanhante** (sem as 4 notas não
  sobra pesquisa) e neuro. O motivo vai pro log em uma linha — é por ele que se debuga
  "por que não veio o convite?".
- **A rede** (`_abrir_entradas`, no cron) pega quem a emenda não pegou: cadastro pelo painel,
  convite que não conseguiu sair. Espera 3h, desiste em 5 dias, e agora **cria e aborda no
  mesmo tick**. Também pula quem já teve a 1ª consulta **realizada**.
- **A corrida foi fechada** (`_descartar_entradas_obsoletas`): se a mesma pessoa tem pendente
  a linha de base **e** uma pesquisa de outro momento, a de entrada é marcada `nao_respondeu`
  e sai da fila. Sem isso ela ficava pendente pra sempre e, se disparasse, perguntava "como
  você está antes de começar?" pra quem já foi atendido (foi a avaliação 393, no teste de 09/08).
- **Sem terapeuta no prompt**: na linha de base o `fk_terapeuta` é o **sentinela** (a
  coordenação ainda não fez o match). `montar_prompt` omite a linha do terapeuta nesse
  momento — senão a Sofia citaria como "o terapeuta dela" alguém que não atende ninguém.

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
  botão "Marcar resolvido" seta `conversa.cobranca_resolvida_em`.
- **Resolvido é um ESTADO, não o fim.** `cobranca_resolvida_em` é **soft-delete**: tira o
  paciente da *fila de trabalho*, nunca apaga a conversa (que segue em "Todas as conversas",
  com todo o histórico). Por isso existe a seção **Resolvidos** (recolhível) com **"Reabrir"**,
  e as três tabelas têm **"Abrir conversa"** — a Thainá precisa poder continuar falando com um
  paciente já cadastrado/cobrado. Só `painel.excluir_conversa` (botão "Reiniciar conversa")
  apaga de verdade.
- Marcar resolvido **não mexe no `modo`** da conversa: é sobre cobrança, não sobre quem atende.
- No template, nome de paciente vai pro `data-nome` (Jinja autoescapa) e o JS lê de lá. Nunca
  interpole no `onsubmit`: `| tojson` emite aspas duplas que fecham o atributo, e um nome como
  "D'Ávila" quebraria a string.
- Hamilton fora do ar → a página mostra um aviso, não quebra.

### Imagem e documento recebidos (`midia.py` + tabela `midia`)
- Paciente manda imagem/documento → baixa **na hora** (a URL da Meta expira em minutos),
  guarda os **bytes no Postgres** (o filesystem do Render é recriado a cada deploy) e
  **escala** pra Thainá (`anexo_recebido`). A Sofia **não lê** o anexo.
- `Midia.conteudo` é **`deferred`**: o painel faz poll de 5s e só precisa de metadados. Sem
  isso, cada poll arrastaria todos os blobs. Quem precisa dos bytes usa `painel.obter_midia`
  (com `undefer`).
- Teto de 8 MB (`midia.TAMANHO_MAXIMO`). Se apertar, é sinal de migrar pra bucket externo.
- **`excluir_conversa` apaga a mídia junto** (antes das mensagens, por causa da FK): sem isso
  o "Reiniciar conversa" deixaria anexo de paciente órfão no banco (LGPD).
- **Nome de arquivo e MIME vêm do paciente** e vão pra headers HTTP. `nome_para_download`
  neutraliza header injection/path traversal; `mime_seguro` é **allowlist** (png/jpeg/gif/
  webp/pdf), **não** o prefixo `image/`: `image/svg+xml` executa `<script>` e seria XSS na
  origem do painel, com a sessão da Thainá. O resto vai `attachment` + `nosniff`.
- Falha no download não perde a mensagem: a Thainá vê "[imagem recebida]" e pede de novo.

### Reply e envio de anexo pela Thainá (P4/P5)
- **Só dá pra citar mensagem que tem `whatsapp_message_id`.** Por isso passamos a guardar o
  wamid das mensagens **enviadas** (bot e Thainá) — antes só as recebidas tinham. Mensagens
  anteriores a isso não têm wamid e não mostram o botão de citar.
- `whatsapp_client.id_da_resposta()` é defensivo de propósito: só devolve `str`. A coluna tem
  índice único, e um `MagicMock`/payload torto da Meta iria pro banco e explodiria o INSERT.
- `responde_a_id` chega pelo formulário: `painel._citada()` **valida que a mensagem é da mesma
  conversa** (senão a citação vazaria mensagem de outro paciente).
- Envio de anexo: `subir_midia` (POST `/media`, devolve `media_id`) → `enviar_midia`. A Meta
  não aceita bytes inline no `/messages`. Guardamos uma cópia na tabela `midia` (o `media_id`
  da Meta expira em 30 dias e o painel precisa mostrar o que foi enviado).
- O teto de 8 MB é checado com `await anexo.read(MAX + 1)`, **antes** de materializar o arquivo:
  ler tudo e medir depois deixaria um upload de 500 MB entrar na memória.
- No template, o botão de citar vive no fragmento que o HTMX troca a cada 5s → o JS usa
  **delegação de evento** no `document`, não listener por botão.

### Painel: busca/ordenação e o gate de digitar
- **Lista de conversas** ordena e busca **no servidor** (`painel.listar_conversas`), porque é
  paginada: filtro + busca + ordem + `limit/offset` tudo no SQL. `ordem` vem da querystring e
  é resolvida contra a allowlist `painel.ORDENS` (nunca interpolada em SQL). A busca casa
  número, texto de mensagem (colunas) e nome — que mora no JSON `dados_coletados` e é casado
  via `cast(..., String) LIKE` pra ficar portável SQLite↔Postgres.
- **Acompanhamento** ordena **no cliente** (`static/ordenar-tabela.js`): tabelas pequenas, já
  carregadas inteiras. Marque a coluna com `<th data-sort>` (ou `data-sort="num"`).
- O polling HTMX da lista **repassa** filtro/busca/ordem na URL do fragmento; se esquecer, a
  tela "pula" pro padrão a cada 15s.
- **Pra digitar, a Thainá precisa assumir a conversa**: em modo bot o `<textarea>` nem é
  renderizado (senão ela escreveria por cima da Sofia, que continua respondendo). Ao sair da
  conversa em modo humano, um `confirm()` oferece devolver ao bot; o `?proximo=` do redirect
  passa por `_destino_seguro` (só caminho interno).

### PWA (o painel como app no celular da Thainá)
- `manifest.webmanifest` e `sw.js` são servidos da **raiz** por rotas em `app/main.py`, não
  pelo mount `/static`. Motivo: um service worker só controla páginas dentro do seu caminho —
  de `/static/sw.js` o escopo seria `/static/` e o navegador não ofereceria instalar o app.
- **O SW não cacheia `/painel/`, `/api/` nem `/login`** — é dado de saúde (LGPD: não pode ficar
  no disco do celular), a sessão expira, e o painel já se atualiza via HTMX. Só `/static/`.
- Ícones: "S" da **Fraunces** (`--font-display`) sobre o gradiente do `.brand .logo`. Há versões
  **maskable** (o Android recorta 20% das bordas; sem elas o S fica cortado). Regerar com
  `scripts/gerar_icones.py` (fontTools + Pillow, **não** são dependência de runtime).
- No mobile a tabela rola na horizontal **mantendo o `thead`**: o padrão do painel esconde o
  cabeçalho em telas pequenas, e sem ele a Thainá perderia a ordenação por coluna.

### Painel: auth por sessão (não é mais HTTP Basic)
- Login próprio em **`/login`** → cookie de sessão assinado (`SessionMiddleware`,
  `secret_key`). `app/dependencies.py`: `requer_login_pagina` (HTML → 303 p/ `/login`),
  `requer_login_api` (JSON → 401), `verificar_origem` (defesa CSRF por header `Origin`).
  Credenciais comparadas em tempo constante (`secrets.compare_digest`).

### Alertas pra Thainá (`escalation.py`, template `alerta_thaina`)
- **Um template só** pra tudo: `alerta_thaina` (2 params: nome do paciente, o que aconteceu).
  Reusar evita esperar aprovação da Meta a cada tipo de aviso novo.
- `alertar_thaina(conversa, motivo)` → escalada. `alertar_cadastro(conversa, resultado)` →
  cadastro novo, reencontro (ficha atualizada) ou **CADASTRO FALHOU** (o mais urgente: a
  Thainá tem que cadastrar à mão).
- Disparado na tool `cadastrar_paciente` do webhook. O botão "Cadastrar no Hamilton" do
  painel **não** alerta (a Thainá mesma clicou).
- Falha no envio **nunca** derruba a conversa: o evento já está no painel, o alerta é
  conveniência. Loga sem o nome do paciente (LGPD).
- Fora da janela de 24h só template funciona — por isso o alerta é template, não texto.

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

### Sanitização da saída do bot (`saida.py`) — rede de proteção, não cosmético
- O modelo tem 2 canais: `tool_calls` e `content`. Ele **erra o canal**: em beta fechado
  (dados fictícios) pôs o JSON do `cadastrar_paciente` no `content` e o texto foi pro
  WhatsApp; noutra vez vazou `@endsection to=final code omitted`. Com paciente real seria
  nome/nascimento/endereço dele — dado de saúde.
- **Todo texto do bot passa por `saida.limpar()`** no único choke point de saída
  (`webhook._enviar_em_bolhas`) — isso cobre fallback, escalada, `PEDIR_TEXTO` e LLM. A
  resposta da **Thainá** (`painel.responder_como_thaina`) **não** passa (é humana, de propósito).
  `seguimento` manda constante hardcoded, não precisa.
- Remove: JSON/estrutura com campo de `tools.py` (linha inteira **ou** embutido na fala),
  tokens internos (`@endsection`, `to=final`, ```` ``` ````, `<|...|>`) e os prefixos que **nós**
  injetamos no histórico (`[Thainá, coordenadora clínica]:`, `[Aviso do sistema: ...]`).
- **Falso positivo > falso negativo**: cortar fala legítima quebra a conversa. O casamento é
  conservador de propósito (`"Ele disse {isso}"` passa intacto). Há bateria de teste pra isso.
- Se limpar tudo, **nenhuma bolha é enviada**. Loga WARN (sem o conteúdo — LGPD) e conta em
  `saida.bloqueios()`, exposto em `/painel/metricas` (card só aparece se > 0). Contador é em
  memória (zera no restart); o registro permanente é o log.
- **Não é substituível por prompt** (o prompt reforça, mas LLM não garante formato).

### Pagamentos Stripe (`pagamentos.py` + `stripe_client.py`, `/painel/pagamentos`)

🔴 **Leia isto antes de mexer: `cancel_at` NÃO existe na criação.** Nem em Checkout
Session nem em Payment Link — a API responde `400 parameter_unknown: Received unknown
parameter: subscription_data[cancel_at]`. Ele só existe em `POST /subscriptions/{id}`,
ou seja, **depois** que a pessoa paga.
- **O estrago (descoberto em 13/08):** o código mandava esse parâmetro achando que
  limitava o parcelado, e o teste unitário "provava" que funcionava porque **mockava**
  `criar_checkout_session`. Suite verde, feature morta: nenhum link parcelado jamais
  foi gerado pelo painel da Sofia (todo POST dava 400). Na conta real, as 18
  assinaturas de parcelado vindas do painel do site **cobravam pra sempre** — uma
  delas cobrou 5 parcelas num plano de 4 (R$ 250 a mais, estornado por Pix à parte).
- **Lição, de novo a mesma:** mock não pega contrato de API quebrado. Por isso existe
  `scripts/validar_parcelado.py` — roda o ciclo inteiro no modo de teste com **test
  clock**, avança 6 meses e confere que a cobrança parou. Rode depois de mexer aqui.
- **Como o limite funciona agora:** a criação só grava `parcelas_total` no metadata;
  quem encerra é **`limitar_parcelado`** (`POST /tasks/stripe`, cron diário, flag
  `limitar_parcelado_ativo` que **nasce ligada**). Ele calcula `âncora + N meses − 1
  dia` e grava `cancel_at`. Latência não importa: a cobrança indevida só viria um mês
  depois do checkout.
- ⚠️ **O discriminador é `metadata.parcelas_total`, e só ele.** Na conta real separa
  com precisão total (18/18 neuro têm; 0/30 mensalidades de terapia têm). Confundir
  cancela a terapia contínua de quem paga em dia — o erro oposto e mais caro. O que
  parece neuro e não tem o campo **não é tocado**: vira alerta (`_neuro_sem_plano`).
  Teto de 20 por rodada, pra um bug não virar cancelamento em massa.

- **Tudo sai como Payment Link, nunca Checkout Session.** A Session tem URL de 300+
  caracteres (`checkout.stripe.com/c/pay/cs_...#fid...`) e **expira em 24h** — num
  link que vai por WhatsApp e fica dias parado, isso é uma cobrança que não acontece
  e ninguém fica sabendo. Payment Link é `buy.stripe.com/<10 chars>` e não vence; em
  troca é reutilizável, então todo link nasce com `restrictions.completed_sessions.limit=1`.
  Consequências: **não há `customer_email`** (o checkout pergunta) e **não há
  `cancel_url`** (quem desiste fecha a aba; `/pagamento-cancelado` só serve pros
  links antigos). O `ref` agora é sempre `plink_...`.
- **`subscription_data.description`** é o único lugar em que o paciente lê o combinado
  antes de autorizar — a tela do Stripe mostra só "R$ 200,00 por mês" e um botão de
  assinar. Sem data absoluta de propósito: o link nasce antes do pagamento.
- **`paciente_id` no metadata** é o elo com o prontuário do Hamilton (o painel do site
  já gravava; 27 assinaturas têm). Não dá pra preencher depois — tem que ir na criação.
  `terapeuta_id` não tem equivalente: no cadastro pela Sofia o paciente é lead sem terapeuta.
- **Sem webhook e sem tabela local por escolha**: o Stripe é a única fonte de verdade;
  a listagem chama a API ao vivo. `stripe_client.py` é httpx puro (form-encoded,
  `_achatar` gera a notação de colchetes). `STRIPE_SECRET_KEY` vazia = tela desligada
  (aviso), nada quebra.
- 3 partes na mesma página (`?aba=`): **gerar link** neuro 1x (pagamento único) ou
  2-6x (assinatura mensal da parcela — NÃO é parcelamento de cartão, que o Stripe não
  oferece no Brasil; explicar ao paciente que são N cobranças mensais), **assinatura
  terapia** (recorrente, sem fim, valor cheio na entrada, sem pro-rata e sem dia fixo)
  e **listagem** com faturas.
- **A listagem entende o legado.** `metadata.tipo` só existe nas 3 criadas pela Sofia;
  `tipo_da_assinatura` cai pra `parcelas_total` (senão as 18 de neuro apareciam como
  "Terapia" e o filtro vinha vazio) e `nome_do_cliente` cai pra `customer.name` (que
  exige `expand=data.customer`). Cobre 47 das 51; 4 ficam "(sem nome)". **Nada é
  escrito no Stripe pra isso** — é tudo fallback de leitura.
- **Vínculo paciente ↔ Stripe**: `conversa.stripe_ref` aceita `sub_...`, `cs_...`,
  `cus_...`, `plink_...` ou a URL do link (buy.stripe.com) — `interpretar_referencia`
  normaliza, `status_da_referencia` resolve ao vivo num estado unificado (pago/ativa/
  atrasada/aguardando/...). Aparece na página da conversa (card Pagamento) e na fila
  "Pronto pra cobrança" do acompanhamento (`anotar_pagamentos`, tolerante a falha).
  Link criado com "Vincular ao paciente" já sai amarrado. Parcelado cancelado após
  quitar as N parcelas conta como **pago**, não "cancelada". ⚠️ Payment Link de
  **assinatura** precisa do desvio pra `_status_da_assinatura` — sem ele todo
  parcelado apareceria "Pago" já na 1ª parcela.
- `/pagamento-sucesso` e `/pagamento-cancelado` são **públicas** (o paciente cai nelas
  ao voltar do checkout) e **não confirmam pagamento** — cortesia visual; a verdade é
  a API. Timestamps do Stripe são em SEGUNDOS (filtro Jinja `data_unix`); valores em
  CENTAVOS (`fmt_centavos`).

### Cobrança da mensalidade (`cobranca.py` + `/tasks/cobrancas`) — Demanda D
A Sofia cobra a mensalidade sozinha depois da primeira sessão. Exige ler
`cobranca.py` + `pesquisa.finalizar` + o portão do `webhook` juntos:
- **O gatilho é `is_realizado`, não a pesquisa.** `status-primeira-consulta` (Hamilton)
  só devolve `primeira_consulta_realizada=True` com o checkbox marcado. O signal que
  cria a pesquisa dispara na **criação** da consulta e **ignora** `is_realizado` — quem
  **faltou** recebe a pesquisa e **não** pode ser cobrado. Os dois sinais divergem no
  mesmo registro; usar o da pesquisa cobraria quem não foi atendido.
- **A pesquisa vem antes por sequência, não por dependência.** Quem está em pesquisa é
  pulado no tick; `pesquisa.finalizar` chama `cobranca.encadear()` nos **três** desfechos
  (respondida / recusada / expirada), que revalida tudo no Hamilton. Sem pesquisa, o cron
  cobra direto.
- **A Sofia retoma o controle mesmo em modo humano** (decisão do Paulo, contra
  recomendação). O portão de `webhook.ingerir_mensagem` abre exceção pra pesquisa **e**
  cobrança — sem isso ela perguntaria e ignoraria a resposta. O `modo` **não** é
  alterado: a escalada aberta continua valendo pra Thainá no painel. Por isso os dois
  modos **têm** `escalar_para_thaina`: é a rede que trata o caso de borda (cobrar quem
  escalou por `gratuidade`).
- **Entrada = mensalidade cheia, sem pro-rata, idêntica no Pix e no cartão.**
  `criar_assinatura_mensalidade` é a **única** função de mensalidade: a Sofia e o
  painel usam a mesma, senão o mesmo paciente pagaria valores diferentes conforme
  quem gerou o link. Assinatura mensal simples, **sem dia fixo** — renova no dia em
  que a pessoa assinou. O dia 10 vale só pro **Pix**, onde é uma data que alguém
  precisa lembrar; no cartão a cobrança é automática e a data não muda nada.
  Duas alternativas foram descartadas e é bom não redescobrir: **`billing_cycle_anchor`**
  só aceita datas dentro de um ciclo e, com `proration_behavior: none`, não cobra
  nada na entrada (`no_payment_required`); **`trial_end` + item avulso** cobra certo
  mas faz o checkout exibir **"avaliação gratuita"**, texto não customizável.
- **Comprovante só no Pix** — no cartão o Stripe confirma sozinho. Anexo em cobrança
  marca `cobranca_status='comprovante'` e **escala pra Thainá**: a Sofia nunca confirma
  vaga ao receber uma imagem que não leu.
- **Janela de 24h da Meta.** Fora dela a mensagem não sai e **não há template aprovado**:
  marca `sem_janela` e aparece na fila "Pronto pra cobrança" que já existe (não há fila
  nova). Vale pro lembrete também.
- **Desligada por padrão** (`cobranca_ativa`), igual às travas `SOFIA_PESQUISAS_*` do
  Hamilton: fluxo automático que fala de dinheiro com paciente sobe dark.
- `oferecer_desconto` **não** entra em `TOOLS_COBRANCA`. Quem não pode pagar vira
  escalada `gratuidade` — decisão humana.

### Painel: identidade e navegação (revisão de 10/08)
Três bugs de verdade e uma tela nova. O que volta a quebrar se alguém mexer sem saber:
- 🔴 **As abas ficavam invisíveis em TODA tela.** O `_topbar.html` as punha num
  `.container-app` com `padding-top:0` e a topbar é `position:fixed` com 60px —
  elas nasciam em y=0, debaixo da barra. Desde o commit `bb22140` (08/08),
  Acompanhamento, Pagamentos e Resultados só eram alcançáveis digitando a URL.
  Agora existe `.abas-wrap`, que ocupa espaço no fluxo (`margin-top` = altura da
  topbar) e gruda abaixo dela; `.abas-wrap + .container-app` corta o respiro de
  84px que só existia pra compensar a barra fixa.
- 🔴 **`.card` não tem padding** (é full-bleed por causa das tabelas), e metade
  das telas punha texto direto dentro: o texto encostava na borda e o
  `overflow:hidden` **fatiava os ícones ao meio**. Quem parecia bom era quem
  passava `style="padding:18px"` na mão. Agora há `.card-topo`/`.card-corpo`.
  **Não ponha padding no `.card`** — quebra toda tabela.
- 🔴 **`.kpis` não existia no CSS** (só `.kpi-grid`): os dois blocos de KPI
  dentro de card em `/painel/metricas` renderizavam empilhados em largura cheia.
- **Identidade v2.** O painel usava `#2E9E8F` e neutros creme; o teal da marca
  (e do Hamilton, e da própria logo) é **`#008888`**. Agora o CSS segue o
  `Allos_IdVisual - v2.pdf`: Marrs Green, Vermelho Queimado (urgência), Ouro
  Velho (atenção), cinza esverdeado. Tipografia **Montserrat + Inter** — o guia
  pede Frontage, que é paga, e ele mesmo indica Montserrat como fallback.
  A logo real (grafismo α na topbar, lockup no login, ícones do PWA) vem de
  `app/static/marca/`, com o traço raspado guardado no **canal alfa**;
  `scripts/gerar_marca.py` regera tudo.
- **A home virou a tela "Hoje"** (`hoje.py` + `painel_hoje.html`); a lista de
  conversas foi pra **`/painel/conversas`**. Motivo: "o que eu tenho pra fazer
  agora?" não tinha resposta em tela nenhuma — escalada aberta e cadastro que
  falhou moravam na lista, alerta de pesquisa e espera pela 1ª consulta no
  acompanhamento, falha de cobrança só como número em resultados.
  - **A fila não tem recorte de tempo, de propósito.** Escalada de três semanas
    continua lá. Filtrar por "hoje" faria sumir justamente o esquecido. A janela
    de 7 dias vale só pros números e pro bloco "resolveu sozinha".
  - **Uma linha por conversa** (`hoje.PRIORIDADE`): comprovante em cobrança abre
    escalada *e* marca `cobranca_status`, e duas linhas fariam o contador mentir.
  - **Só "de olho" fala com o Hamilton.** Ele fora → a fila e os números seguem.
  - O contador da aba vem da dependência `_pendencias_na_topbar`, que roda em
    **toda** página do painel — a graça é ser visto de outra tela.
- **`config_negocio.CAMPOS` virou `NamedTuple` `Campo`** com `ajuda`, `grupo`,
  `prefixo`/`sufixo`. Os três primeiros itens continuam acessíveis por índice.
  A tela de configurações era uma lista plana de 15 campos com o rótulo fazendo
  as vezes de ajuda ("Desconto máximo que a Sofia pode oferecer sozinha na
  terapia (%) — 0 desliga") e o contexto amontoado num bloco no rodapé.
- **Sem confirmação ao ligar a cobrança** (decisão do Paulo, 10/08): o estado
  aparece escrito ao lado da chave e repetido na tela Hoje.

### Painel: o que cada tela mostra (revisão de 08/08)
O painel tinha ficado dois ciclos atrás do bot. O que foi corrigido, e o porquê —
os pontos que voltam a quebrar se alguém mexer sem saber:
- **Navegação vive no `_topbar.html`**, não nos templates de página. As abas eram
  **três cópias** do mesmo HTML (lista/acompanhamento/pagamentos), e por isso
  "Resultados" nunca entrou em nenhuma e conversa/config/prompts eram becos sem
  saída. Aba nova = uma linha; `aba_ativa` vem do contexto de cada router.
- **A lista é paginada** (`POR_PAGINA = 50`, `?pagina=`). Antes o router chamava
  `listar_conversas` sem `limite`, pegava o default 50 e **não havia paginação**:
  da 51ª conversa em diante o resto sumia em silêncio. O polling HTMX repassa
  `pagina` junto de filtro/busca/ordem, senão a tela volta pra página 1 a cada 15s.
- **`min="0"` nos campos numéricos do config.** Quatro campos documentam "0
  desliga" (`desconto_maximo_pct` + os três `alerta_nota_*`) e eram
  **inatingíveis**: `min="1"` no form e `if n > 0` no servidor. Hoje é `n >= 0`
  (negativo continua descartado).
- **A conversa mostra pesquisa, cobrança e escaladas.** Nada disso aparecia: dava
  pra ter uma cobrança em curso e a tela dizer só "modo bot". O histórico de
  escaladas (`motivo`/`contexto`/`resolvida_em`) nunca foi exibido em lugar nenhum.
- **"Assumir controle" interrompe de verdade.** Pesquisa e cobrança furam o portão
  do modo humano, então marcar a conversa como humana **não calava a Sofia** — o
  botão mentia e o único jeito de pará-la era esperar as 44h. Agora
  `painel.assumir` chama `cobranca.finalizar`/`pesquisa.finalizar` (o que já foi
  respondido fica gravado) e o compositor só libera quando ninguém mais está
  conduzindo (`pode_digitar` no template).
- **`cadastradas_hoje` usa `cadastrado_em`**, não `atualizada_em` — esta tem
  `onupdate`, então qualquer mensagem nova de um paciente antigo o ressuscitava
  como "cadastrado hoje".
- **`parcelas_max` vem do `/painel/config`**, não da constante: a Sofia dizia "até
  5x" (token do prompt) e a tela oferecia 6x.
- **Parceria é marcada na fila "Pronto pra cobrança"** — a fila convidava a Thainá
  a cobrar quem paga R$ 0.
- **Métricas**: `escaladas_por_motivo` conta só as **abertas** (`resolvida_em` só
  passou a ser preenchido agora); há KPIs de pesquisa e de cobrança por status,
  com destaque pra `sem_janela`/`erro_link` (falha silenciosa: a Sofia não
  conseguiu cobrar e ninguém sabe); e **tempo da 1ª mensagem até a 1ª sessão**
  (`_tempo_ate_primeira_sessao`) — **a única métrica que chama o Hamilton**, com
  mediana em vez de média e ignorando delta negativo (paciente antigo que só
  depois falou com a Sofia). Hamilton fora → o card some, o resto continua.
  ⚠️ Testes que renderizam `/painel/metricas` **precisam mockar o Hamilton**,
  senão fazem chamada de rede real (~2,3s por teste).

### 🔴 Demanda A estava furada no Hamilton (descoberto e corrigido em 08/08)
Um dry run de ponta a ponta mostrou que **toda a captação da Sofia era descartada
no intake**. Três pacientes de teste, com origens diferentes, entraram os três como
"WhatsApp (Sofia)" e `vlr_sessao = 50,00`. Duas causas independentes:

1. **`PacienteIntakeSerializer` sobrescrevia tudo.** O `create` fazia
   `validated_data["fk_captacao"] = defaults["captacao"]` (= `get_or_create("WhatsApp
   (Sofia)")`) e o mesmo com `vlr_sessao`/`tipo_pagamento`. Pior: `fk_captacao` nem
   estava em `Meta.fields`, então o valor da Sofia era descartado antes do `create`.
   **Corrigido:** os três campos são aceitos e o default virou fallback. O ID é
   resolvido à mão no alias certo (`IntegerField` cru, não `PrimaryKeyRelatedField`
   — um queryset de FK leria o banco `default` e o router recusaria a relação).
2. **`Captacao.is_parceria` NÃO EXISTIA.** Nem o campo, nem a migration —
   `acessorios/migrations/` parava na `0001`. A Sofia lia `c.get("is_parceria")` de
   um payload onde a chave nunca vinha, e `e_parceria` devolvia `False` **pra todo
   mundo, sempre**. Nenhum paciente de convênio era detectado; com a Demanda D
   ligada, todos seriam cobrados. **Corrigido:** campo + `0002` (schema) + `0003`
   (marca as prefeituras existentes, com log do que casou).

⚠️ **Lição:** é o mesmo acidente do `.gitignore` que já tinha comido migrations
antes — e passou despercebido porque a Sofia **degrada em silêncio** (chave ausente
vira `False`). O `CLAUDE.md` afirmava as duas coisas como prontas. **Um teste de
ponta a ponta com dado real é o que pega isso; a suite dos dois repos passava.**

Validação (banco `sofia-teste`): Google → captação 15, R$ 200, `manual`;
Prefeitura → captação 13 `[PARCERIA]`, **R$ 0,00**, `parceria`.

### Ambientes e bancos (`sofia-teste`) — ⚠️ não estava documentado
- **Não existe staging.** Um único serviço no Render. O "ambiente de teste" é **local**:
  Sofia local + Hamilton local apontando pra branch **`sofia-teste`** do Neon (cópia da
  produção do Hamilton, PII anonimizada em 06/08).
- **Confirme o banco pelo `timeline_id`, nunca pelo nome do arquivo de env:**
  `d816d0c2…` = teste (pode escrever) · `fdb211ba…` = **PRODUÇÃO (aborte)**.
- 🔴 **`SOFIA_API_DATABASE_URL` é do serviço do Hamilton.** Setada, os endpoints da
  Sofia leem/gravam nela — **em produção isso manda paciente real pro banco de teste**.
  O `render.yaml:87` avisa; o `DEPLOY.md` que ele referencia não fala disso.
- Travas do runbook (`.claude/skills/testar-conversa`): `envio_whatsapp_bloqueado=True`
  e `HAMILTON_API_URL` no localhost. O `.env` local carrega credenciais **reais** do
  WhatsApp — sem as travas, um teste manda mensagem pra paciente de verdade.
- `scripts/anonimizar_hamilton_teste.py` **tem que rodar** depois de todo "Reset from
  parent" no Neon (o reset traz a PII real de volta).

### LGPD / logs
- **Nunca logar conteúdo de mensagem** (dado de saúde sensível) — só metadados
  (qtd, tipos, ids). Telefones em log passam por `utils.mascarar_telefone` (`***8888`).
  Idem o conteúdo que a `saida.limpar()` removeu: loga-se só motivo e tamanho.
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

1. Thainá **assume o controle** (sem isso o campo de digitar nem é renderizado)
2. Digita e/ou anexa um arquivo; pode clicar no ícone de responder pra **citar** uma mensagem
3. Painel POST `/painel/conversas/{id}/responder` (multipart)
4. Anexo: `subir_midia` → `enviar_midia`; texto: `enviar_texto` (com `context` se citou)
5. App persiste com `origem = thaina`, guardando o **wamid do envio** (pra poder ser citada) e
   o `responde_a_id` (validado: tem que ser mensagem desta conversa)
6. Ao sair da conversa, o painel pergunta se o bot assume de volta

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
├── CLAUDE.md                  # Este arquivo (estado atual + arquitetura + porquês)
├── README.md                  # Setup e deploy
├── .claude/settings.json      # Config de agentes
├── .env.example               # Template
├── pyproject.toml             # Dependências + config
├── alembic.ini                # Config migrations
├── render.yaml                # Deploy config
│
├── docs/                      # Toda a documentação (índice em docs/README.md)
│   ├── README.md              # ← PONTO DE ENTRADA: o que ler e o que fazer agora
│   ├── demandas/              # o que foi, o que é e o que vem
│   │   ├── 01-EM-ANDAMENTO.md            # ← documento de trabalho do ciclo atual
│   │   ├── 02-modelo-de-avaliacao.md     # campos da Avaliacao (a decidir)
│   │   ├── 03-questionario-atual-da-qualidade.md
│   │   ├── 00-ORIGINAL-com-premissas-erradas.md  # histórico: NÃO implementar daqui
│   │   └── 99-backlog-entregue.md        # P0–P6, todos entregues
│   ├── referencia/            # workflow.md, DEPLOY.md, sofia_briefing.md
│   └── juridico/              # política de privacidade, termo de consentimento
│
├── prompt/                    # Tudo que a Sofia usa como referência de resposta
│   ├── sofia_v01.txt          # System prompt versionado (fluxo/tom/regras) — load-bearing
│   ├── sofia-base-conhecimento.md  # Base de conhecimento anexada ao prompt — load-bearing
│   ├── pesquisa-conducao.txt       # Pesquisa: tom e regras (substitui o prompt principal)
│   ├── pesquisa-primeira-sessao.md # Pesquisa: roteiro pós-1ª sessão
│   ├── pesquisa-encerramento.md    # Pesquisa: roteiro de alta/desistência/troca
│   ├── pesquisa-extracao.txt       # Pesquisa: conversa → JSON (NÃO vai pro paciente)
│   ├── cobranca.md                 # Cobrança: como falar da mensalidade — load-bearing
│   └── contrato-terapeutico-allos.md  # Contrato: referência interna (NÃO carregado em runtime)
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
│   │   ├── painel.py         # /painel (Hoje), /painel/conversas, /config, /metricas
│   │   ├── tasks.py          # POST /tasks/{seguimentos,pesquisas,cobrancas,stripe} (cron, X-Tasks-Token)
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
│   │   ├── saida.py          # Sanitiza a fala do bot antes de enviar (rede de proteção)
│   │   ├── seguimento.py     # Follow-up de lead parado (Frente 2)
│   │   ├── metricas.py       # KPIs do painel (Frente 3)
│   │   ├── midia.py          # Imagem/documento recebidos (baixa, guarda, serve)
│   │   ├── captacao.py       # Origem do paciente: lista do Hamilton + validação do ID
│   │   ├── hoje.py           # Fila única do que precisa de uma pessoa (home do painel)
│   │   ├── pesquisa.py       # Pesquisa de satisfação (polling, condução, extração)
│   │   ├── cobranca.py       # Cobrança da mensalidade pós-1ª sessão (Demanda D)
│   │   ├── pagamentos.py     # Regra do Stripe (links, assinaturas, status unificado)
│   │   ├── stripe_client.py  # Wrapper REST do Stripe (httpx puro, form-encoded)
│   │   └── painel.py         # Queries/ações do painel da Thainá
│   │
│   ├── templates/            # Jinja2 (HTMX via CDN)
│   │   ├── base.html, _topbar.html, login.html
│   │   ├── painel_lista.html, painel_conversa.html
│   │   ├── painel_hoje.html   # a home: fila do que precisa de gente
│   │   ├── painel_config.html, painel_metricas.html
│   │   └── _conversas_fragment.html, _mensagens_fragment.html
│   │
│   └── static/
│       ├── allos.css         # Allos Design System (guia de identidade v2: #008888 + Montserrat/Inter)
│       ├── marca/            # logo real extraída do guia (grafismo α + lockup), com a textura no alfa
│       └── ordenar-tabela.js # Ordenação client-side (<th data-sort>)
│
├── scripts/
│   ├── gerar_icones.py       # (legado) ícones do PWA com o "S" da Fraunces
│   ├── gerar_marca.py        # Regera marca + ícones do PWA a partir da logo (dev-only)
│   ├── validar_parcelado.py  # Prova com test clock que o parcelado PARA na Nª (modo de teste)
│   └── relatorio_parcelado.py # Simula/aplica o fim de linha nas assinaturas antigas
│
├── alembic/
│   ├── env.py
│   └── versions/             # Migration files
│
├── tests/                    # conftest.py SÓ trava rede; cada teste sobe seu SQLite e mocka externos
│   ├── test_webhook.py, test_conversation.py, test_escalation.py
│   ├── test_cadastro.py, test_hamilton.py, test_llm.py
│   ├── test_painel.py, test_metricas.py, test_seguimento.py
│   ├── test_captacao.py, test_pesquisa.py   # Demandas A e C
│   ├── test_cobranca.py, test_pagamentos.py # Demanda D + painel de pagamentos
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
  gitignored). Cron do follow-up = `TASKS_TOKEN` + job no cron-job.org (ver `docs/referencia/DEPLOY.md`).
- **Opcionais na fila**: Demanda 1 (observabilidade de duplicatas — a duplicação em si já foi
  resolvida pela Demanda 2) e KPI distribuição terapia×neuro.

### Ciclo atual (08/08/2026) — ver [demandas.md](docs/demandas/01-EM-ANDAMENTO.md)

| Demanda | Status |
|---|---|
| **A** — origem real do paciente (captação), `is_parceria`, `vlr_sessao` do painel, fluxo de prefeitura | ✅ entregue |
| **B** — neuro com a Amanda (R$ 1.000, editável) + aviso único pós-escalada | ✅ entregue |
| **Ajuste da `Avaliacao`** (campos das respostas) | ✅ no Hamilton (migrations até a `0007`) |
| **C** — pesquisas de satisfação | ✅ implementada — **desligada** por `SOFIA_PESQUISAS_ATIVAS` |
| **D** — cobrança da mensalidade (Pix + Stripe) | ✅ implementada — **desligada** por `cobranca_ativa` |

**Mexe nos dois repos**: a Demanda A e a infra da C exigiram mudança no `hamilton-api`
(migrations, endpoints e um bug nos signals). **A Demanda D não toca o Hamilton** — só
consome o `status-primeira-consulta`, que já existia.

### ➡️ PRÓXIMO PASSO (quem pegar o projeto começa aqui)

**1. Ligar e validar em produção.** Nada disso é código: `SOFIA_PESQUISAS_ATIVAS`,
`cobranca_ativa`, os dois crons e `alembic upgrade head`. Detalhes no topo deste
arquivo. Antes, confira o `SOFIA_API_DATABASE_URL` do Hamilton no Render.

**2. Modelo da tabela de avaliação + planilha de qualidade**
   → `docs/demandas/02-modelo-de-avaliacao.md`. O código está pronto; falta **decidir
   com o Paulo** quais perguntas ficam no questionário definitivo e **editar a planilha**
   que o time de Qualidade usa (hoje pressupõe uma pessoa coletando; agora quem coleta é
   a Sofia). Decidir também se a planilha vira export do Hamilton ou segue em paralelo.

**3. Cobrança recorrente (não feita, e é uma demanda inteira).** A Sofia cobra **só a
entrada**; do 2º mês em diante o cartão roda sozinho e **o Pix é manual** — quem paga
por Pix precisa lembrar todo dia 10, e é o terapeuta que acompanha. Automatizar isso
exige régua de inadimplência e **template aprovado na Meta** (dia 10 quase ninguém está
dentro da janela de 24h).

⚠️ **Contexto que o desenho da D assume**: o **webhook do Stripe no Hamilton está
quebrado desde sempre** (21 assinaturas, 0 faturas — ver
`hamilton-api/docs/pagamentos-cartao-stripe.md`). Fora do escopo, mas é o motivo de a
Sofia ter integração própria — e a consequência aceita é que a Allos tem **duas
integrações Stripe**, e as assinaturas criadas pela Sofia **não aparecem no Hamilton**,
onde moram a contabilidade e a NFS-e.

### 🚩 Backlog priorizado: [BACKLOG.md](docs/demandas/99-backlog-entregue.md)

Leia antes de pegar demanda nova (painel, mídia, reply-to, PWA).

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
PRECO_NEURO=1000
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

# Stripe (links de pagamento no painel; vazio = tela desligada, mostra aviso)
STRIPE_SECRET_KEY=                 # mesma conta do site da Allos; só no Render
STRIPE_PUBLISHABLE_KEY=            # pk_...; o checkout hospedado não usa (documentação)
STRIPE_PRECO_MENSAL_ID=            # price_... do catálogo; reusado se a mensalidade bater
BASE_URL=https://sofia-whatsapp.onrender.com   # monta /pagamento-sucesso|cancelado

# Geral
LOG_LEVEL=INFO
LOG_JSON=false                     # true na produção (logs estruturados)
ENVIRONMENT=production             # ou development
SIMULAR_DIGITACAO=false            # "digitando…" + visto (tiques azuis). Editável no /painel/config
```

> **Editáveis no painel** (`/painel/config`, tabela `configuracao`): `PRECO_*`, `PARCELAS_MAX`,
> `FOLLOWUP_HORAS`, `DEBOUNCE_SEGUNDOS`, `SIMULAR_DIGITACAO`, `TRANSCREVER_AUDIO`. O env define
> só o **default inicial**; o valor salvo no painel manda. Segredos ficam **só** no Render.
>
> **Só no painel** (não têm env var, padrão literal no `config_negocio.CAMPOS`):
> `desconto_maximo_pct`, `alerta_nota_*`, **`pesquisa_entrada_ativa`** (nasce
> **ligada** — ORS de linha de base emendado no cadastro), e os da cobrança —
> **`cobranca_ativa`** (nasce desligada), **`chave_pix`** (vazia = não oferece Pix),
> `cobranca_lembrete_horas`.
>
> ⚠️ **`SOFIA_PESQUISAS_ATIVAS`, `SOFIA_PESQUISAS_LIMITE`, `SOFIA_PESQUISAS_IDADE_MAXIMA_DIAS`
> e `SOFIA_API_DATABASE_URL` são env vars do HAMILTON, não da Sofia** — e a última,
> setada em produção, manda paciente real pro banco de teste.

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

- [sofia_briefing.md](docs/referencia/sofia_briefing.md) — Especificação técnica completa
- [Meta Cloud API Docs](https://developers.facebook.com/docs/whatsapp/cloud-api)
- [OpenAI API](https://platform.openai.com/docs/api-reference)
- [FastAPI](https://fastapi.tiangolo.com/)
- [SQLAlchemy Async](https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html)

---

**Dica**: Sempre rode `/test` e `/security-review` ao longo do desenvolvimento. Não deixa pra no final! 🛡️
