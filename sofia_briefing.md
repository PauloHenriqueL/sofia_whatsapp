# Sofia — Briefing Técnico do MVP

Documento de contexto pra implementação. Sofia é um bot conversacional via WhatsApp pra Associação Allos, uma clínica-escola de psicologia em Belo Horizonte.

---

## Contexto do negócio

A Allos é uma clínica-escola sem fins lucrativos. Tem cerca de 76 terapeutas (estudantes e formados) atendendo cerca de 490 pacientes ativos. Hoje, todo paciente novo entra em contato pelo WhatsApp e fala com a Thainá (coordenadora clínica), que faz qualificação, coleta dados, cadastra no sistema Hamilton (Django interno) e faz match com terapeuta.

O Hamilton é o sistema clínico já existente, em Django, com banco Postgres. É a fonte de verdade do cadastro de paciente, terapeuta, vínculo, sessão, pagamento. Não vai ser mexido por este projeto. Vamos consumir e escrever via API REST.

Sofia é um projeto separado que cria uma camada conversacional sobre o Hamilton, automatizando a parte da Thainá que é estruturada. O MVP atende apenas três funcionalidades, descritas abaixo.

---

## Escopo do MVP

### Em escopo

1. **Conversar com paciente novo via WhatsApp**: receber mensagens, responder com tom acolhedor e humano via LLM, apresentar a Allos, qualificar interesse, coletar dados pra cadastro.

2. **Cadastrar paciente no Hamilton**: quando dados suficientes foram coletados (nome completo, data de nascimento, telefone, contato de apoio, endereço, horários, preferências), fazer POST na API do Hamilton criando o registro.

3. **Escalar pra Thainá em casos específicos**: quando o paciente pede falar com humano, menciona prefeitura ou gratuidade, ou manda áudio. Escalada significa: marcar conversa como `modo = humano`, notificar Thainá no celular pessoal dela via template aprovado, e abrir a conversa pra ela responder pelo painel web.

### Fora de escopo (não implementar agora)

- NPS, em qualquer momento
- Transcrição de áudio (áudio sempre escala)
- Match automático terapeuta-paciente
- Detecção avançada de crise via classificador especializado (no MVP, qualquer pedido sensível escala)
- Cobrança, pagamento, integração com Stripe ou Mercado Pago
- Lembretes temporais (sessão 2h antes, cobrança mensal)
- Reencaminhamento entre terapeutas
- Allos PRO (plano onde paciente escolhe terapeuta)
- Mensagens automáticas pra terapeutas
- Cardápio editável de respostas
- Comunicação em grupo de WhatsApp

---

## Stack decidida

| Componente | Escolha |
|---|---|
| Linguagem | Python 3.11+ |
| Framework | FastAPI |
| ORM | SQLAlchemy 2 |
| Migrations | Alembic |
| Banco | Postgres no Neon |
| LLM | OpenAI (gpt-4o-mini pra começar) |
| Canal | WhatsApp Business Cloud API direto da Meta (sem BSP) |
| Painel web | Jinja2 + HTMX, server-rendered, mesmo serviço da app |
| Hosting | Render |
| Auth do painel | HTTP Basic Auth pra MVP (substituir depois) |

Justificativas resumidas:

- FastAPI em vez de Django porque webhook precisa de resposta rápida, async é trivial, e a estrutura é mais leve pra um serviço com escopo limitado.
- OpenAI por custo. Cliente LLM deve ser abstraído atrás de uma interface pra eventualmente trocar por Claude ou outro.
- Cloud API direta sem BSP porque não há benefício suficiente em adicionar um intermediário pago.
- HTMX em vez de SPA porque o painel é simples: lista de conversas + chat. Não precisa de framework JS complexo.

---

## Arquitetura

### Componentes

```
[Paciente WhatsApp]
        ↕
[Meta Cloud API]
        ↕
[FastAPI app no Render]
   ├── webhook recebe mensagens
   ├── persiste em Postgres Neon
   ├── decide: modo bot ou modo humano?
   │     ├── bot: chama OpenAI, gera resposta, envia via Cloud API
   │     └── humano: só persiste, fica aguardando Thainá
   ├── detecta escalada e dispara template pro celular pessoal da Thainá
   ├── consulta e escreve no Hamilton via API REST
   └── serve painel web (Jinja2 + HTMX) pra Thainá responder
        ↕
[Thainá usa o painel no PC ou no celular (responsivo)]
```

### Fluxo de uma mensagem do paciente

1. Meta envia POST pra `/webhook/whatsapp` com payload da mensagem
2. App responde 200 imediatamente, processa async
3. App busca ou cria `conversa` no banco pelo número
4. App persiste a mensagem em `mensagem`
5. App checa `conversa.modo`:
   - Se `humano`: termina aqui. Painel mostra mensagem nova.
   - Se `bot`: continua.
6. App carrega histórico recente da conversa (últimas 20 mensagens)
7. App chama OpenAI com system prompt + histórico + nova mensagem
8. OpenAI retorna texto da resposta E (opcionalmente) ações estruturadas via tool calling: `cadastrar_paciente`, `escalar`
9. App processa ações:
   - `escalar(motivo)`: marca `conversa.modo = humano`, registra `escalada`, dispara template pra Thainá
   - `cadastrar_paciente(dados)`: faz POST no Hamilton, atualiza `conversa.paciente_hamilton_id`
10. App envia o texto da resposta via Cloud API pro paciente
11. App persiste a resposta enviada em `mensagem`

### Fluxo de uma resposta da Thainá pelo painel

1. Thainá digita resposta no painel e clica enviar
2. Painel faz POST pra `/api/conversas/{id}/responder` com o texto
3. App persiste mensagem em `mensagem` com `origem = thaina`
4. App envia via Cloud API pro paciente

### Detecção de áudio

Quando webhook recebe mensagem do tipo `audio`:

1. App persiste mensagem com `texto = '[áudio recebido]'`
2. App marca `conversa.modo = humano`
3. App registra `escalada` com `motivo = audio_recebido`
4. App envia template de alerta pra Thainá
5. App envia ao paciente: "Recebi seu áudio. Vou chamar a Thainá pra te responder direito."

---

## Modelo de dados

Três tabelas pro MVP.

```sql
CREATE TABLE conversa (
  id SERIAL PRIMARY KEY,
  numero_whatsapp VARCHAR(20) NOT NULL UNIQUE,
  paciente_hamilton_id INTEGER,
  modo VARCHAR(10) NOT NULL DEFAULT 'bot',  -- 'bot' ou 'humano'
  estado VARCHAR(30) NOT NULL DEFAULT 'novo', -- 'novo', 'qualificando', 'coletando_dados', 'cadastrado', 'escalado'
  dados_coletados JSONB NOT NULL DEFAULT '{}',
  criada_em TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  atualizada_em TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_conversa_numero ON conversa(numero_whatsapp);
CREATE INDEX idx_conversa_modo ON conversa(modo);

CREATE TABLE mensagem (
  id SERIAL PRIMARY KEY,
  conversa_id INTEGER NOT NULL REFERENCES conversa(id) ON DELETE CASCADE,
  direcao VARCHAR(10) NOT NULL,  -- 'recebida' ou 'enviada'
  origem VARCHAR(30) NOT NULL,   -- 'paciente', 'bot', 'thaina'
  tipo VARCHAR(20) NOT NULL DEFAULT 'texto', -- 'texto', 'audio', 'imagem', 'documento', 'template'
  texto TEXT,
  whatsapp_message_id VARCHAR(100), -- ID da mensagem na Meta, pra idempotência
  metadata JSONB DEFAULT '{}',
  criada_em TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_mensagem_conversa ON mensagem(conversa_id, criada_em);
CREATE UNIQUE INDEX idx_mensagem_whatsapp_id ON mensagem(whatsapp_message_id) WHERE whatsapp_message_id IS NOT NULL;

CREATE TABLE escalada (
  id SERIAL PRIMARY KEY,
  conversa_id INTEGER NOT NULL REFERENCES conversa(id) ON DELETE CASCADE,
  motivo VARCHAR(50) NOT NULL, -- 'pedido_humano', 'prefeitura', 'gratuidade', 'audio_recebido', 'outro'
  contexto TEXT,
  criada_em TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  resolvida_em TIMESTAMPTZ
);

CREATE INDEX idx_escalada_conversa ON escalada(conversa_id);
```

Idempotência: o índice único em `whatsapp_message_id` garante que a mesma mensagem da Meta nunca é processada duas vezes (Meta pode reenviar webhook em caso de timeout).

Sobre `dados_coletados`: JSONB que acumula durante a conversa. Estrutura esperada:

```json
{
  "nome_completo": "Maria Silva",
  "data_nascimento": "1990-05-15",
  "telefone_contato": "+5531999998888",
  "telefone_apoio": "+5531888887777",
  "endereco": {
    "rua": "Rua das Flores, 123",
    "bairro": "Savassi",
    "cidade": "Belo Horizonte",
    "cep": "30000-000"
  },
  "horarios_disponiveis": "Segundas e quartas à noite",
  "preferencia_terapeuta": "feminino, não importa a abordagem",
  "motivo_busca": "ansiedade e questões com trabalho"
}
```

---

## Integrações externas

### WhatsApp Business Cloud API

Documentação oficial: https://developers.facebook.com/docs/whatsapp/cloud-api

Pré-requisitos a ter prontos antes do deploy:

- Conta no Meta Business Manager verificada (empresa Allos)
- App criado em developers.facebook.com com produto "WhatsApp" adicionado
- Número de telefone dedicado registrado na Cloud API (não pode estar em outro app)
- Token permanente do system user (não usar token temporário de 24h)
- ID do número de telefone (Phone Number ID)
- Verify Token (string secreta pra validação do webhook, definida por nós)

Endpoints da Cloud API que vamos usar:

**Enviar mensagem texto livre** (apenas dentro da janela de 24h):
```
POST https://graph.facebook.com/v18.0/{phone_number_id}/messages
Authorization: Bearer {token}
Body: {
  "messaging_product": "whatsapp",
  "to": "{numero_paciente}",
  "type": "text",
  "text": { "body": "..." }
}
```

**Enviar template** (fora da janela de 24h, ou pra alertar Thainá):
```
POST https://graph.facebook.com/v18.0/{phone_number_id}/messages
Authorization: Bearer {token}
Body: {
  "messaging_product": "whatsapp",
  "to": "{numero}",
  "type": "template",
  "template": {
    "name": "alerta_thaina",
    "language": { "code": "pt_BR" },
    "components": [
      { "type": "body", "parameters": [
        { "type": "text", "text": "{nome_paciente}" },
        { "type": "text", "text": "{motivo}" }
      ]}
    ]
  }
}
```

**Webhook de recepção** (Meta envia pra gente):

A Meta faz GET com challenge na primeira configuração (responder com o challenge), e POST com payload de mensagem em uso normal.

Estrutura simplificada do payload POST:

```json
{
  "entry": [{
    "changes": [{
      "value": {
        "messages": [{
          "from": "5531999998888",
          "id": "wamid.xxx",
          "timestamp": "1234567890",
          "type": "text",
          "text": { "body": "olá" }
        }]
      }
    }]
  }]
}
```

Validação: webhook precisa validar `X-Hub-Signature-256` header com HMAC SHA256 usando app secret.

### Template a aprovar na Meta antes do go-live

Nome: `alerta_thaina`
Categoria: Utility
Idioma: pt_BR
Texto:

```
Atenção: paciente {{1}} precisa da sua atenção no painel da Sofia. Motivo: {{2}}. Acesse para responder.
```

Submissão via developers.facebook.com. Aprovação leva 24-72h. Iniciar isso em paralelo ao desenvolvimento.

### OpenAI

Cliente padrão da SDK oficial Python. Modelo recomendado pra começar: `gpt-4o-mini`.

Usar tool calling pra permitir que o LLM dispare ações estruturadas:

```python
tools = [
  {
    "type": "function",
    "function": {
      "name": "cadastrar_paciente",
      "description": "Cadastrar o paciente no sistema Hamilton quando todos os dados necessários foram coletados",
      "parameters": {
        "type": "object",
        "properties": {
          "nome_completo": {"type": "string"},
          "data_nascimento": {"type": "string", "description": "Formato YYYY-MM-DD"},
          "telefone_contato": {"type": "string"},
          "telefone_apoio": {"type": "string"},
          "endereco": {"type": "string"},
          "horarios_disponiveis": {"type": "string"},
          "preferencia_terapeuta": {"type": "string"},
          "motivo_busca": {"type": "string"}
        },
        "required": ["nome_completo", "data_nascimento", "telefone_contato", "telefone_apoio", "endereco", "horarios_disponiveis"]
      }
    }
  },
  {
    "type": "function",
    "function": {
      "name": "escalar_para_thaina",
      "description": "Escalar a conversa pra coordenadora humana (Thainá) em casos específicos",
      "parameters": {
        "type": "object",
        "properties": {
          "motivo": {
            "type": "string",
            "enum": ["pedido_humano", "prefeitura", "gratuidade", "outro"]
          },
          "contexto": {
            "type": "string",
            "description": "Resumo curto do que aconteceu pra Thainá saber o contexto"
          }
        },
        "required": ["motivo"]
      }
    }
  }
]
```

Abstrair o cliente atrás de uma interface tipo `LLMClient.gerar_resposta(historico, mensagem)` pra permitir trocar de modelo/provedor sem mexer no resto.

### Hamilton (API REST)

API do Hamilton precisa expor pelo menos dois endpoints pra esse MVP:

**Buscar paciente por telefone** (consulta antes de cadastrar):
```
GET {HAMILTON_API_URL}/api/pacientes/?telefone={numero}
Authorization: Bearer {HAMILTON_API_KEY}
Response 200: lista de pacientes ou array vazio
```

**Cadastrar paciente**:
```
POST {HAMILTON_API_URL}/api/pacientes/
Authorization: Bearer {HAMILTON_API_KEY}
Body: {
  "nome_completo": "...",
  "data_nascimento": "YYYY-MM-DD",
  "telefone": "...",
  "telefone_apoio": "...",
  "endereco": "...",
  "horarios_disponiveis": "...",
  "preferencia_terapeuta": "...",
  "motivo_busca": "...",
  "origem": "sofia_whatsapp"
}
Response 201: { "id": 123, ... }
```

Esses endpoints devem ser criados no Hamilton em paralelo ao desenvolvimento do bot. Não fazer dependência rígida: se Hamilton falhar, a app deve logar e marcar conversa como `estado = cadastro_pendente` pra Thainá fazer manualmente.

### Neon Postgres

URL de conexão padrão Postgres. Free tier resolve pro MVP. Conexão via SQLAlchemy assíncrona (`asyncpg`).

### Render

Deploy via repo Git. Configurar:

- Web Service: roda a FastAPI app com Uvicorn
- Variáveis de ambiente: listadas abaixo
- Health check: `GET /health` retornando 200

---

## Endpoints da aplicação

### Webhook

`GET /webhook/whatsapp` — validação inicial da Meta. Aceita query params `hub.mode`, `hub.verify_token`, `hub.challenge`. Se token bate com `WHATSAPP_VERIFY_TOKEN`, responde 200 com o challenge.

`POST /webhook/whatsapp` — recebe mensagens. Valida assinatura. Responde 200 imediatamente. Enfileira processamento async via background task.

### API interna (consumida pelo painel)

`GET /api/conversas/` — lista conversas, paginadas, filtráveis por modo (?modo=humano)

`GET /api/conversas/{id}/` — detalhes da conversa + lista de mensagens

`POST /api/conversas/{id}/responder/` — Thainá envia resposta. Body `{"texto": "..."}`. App envia via Cloud API e persiste.

`POST /api/conversas/{id}/devolver-bot/` — Thainá termina o atendimento e devolve a conversa pro bot. Marca `modo = bot`.

`POST /api/conversas/{id}/assumir/` — Thainá assume conversa que tava em modo bot. Marca `modo = humano`.

### Painel web (HTML server-rendered)

`GET /` — redireciona pra painel ou login

`GET /painel/` — lista conversas, com filtros visuais e badges de novas escaladas

`GET /painel/conversas/{id}/` — view individual de conversa, com chat e campo de resposta

Autenticação: HTTP Basic Auth via FastAPI dependency. Substituir por algo melhor pós-MVP.

### Saúde

`GET /health` — retorna 200 sempre que o app está rodando. Render usa pra health check.

---

## System prompt da Sofia

Versão inicial, ajustar conforme operação real. Salvar como arquivo de configuração pra versionar separadamente.

```
Você é Sofia, a assistente digital de atendimento da Associação Allos,
uma clínica-escola de psicologia em Belo Horizonte. Você trabalha com
a Thainá, coordenadora clínica, ajudando a receber novos pacientes
pelo WhatsApp.

# Sua identidade

Você tem nome próprio e tom próprio. Você é assistente digital, não
finge ser humana. Quando alguém pergunta diretamente se você é IA,
robô ou pessoa, confirma sem dramatizar que é assistente digital. Em
qualquer outra situação, simplesmente trabalha sem mencionar o fato.

# Como você fala

Português brasileiro coloquial, formal sem ser duro. Texto soa
falado. Frases curtas.

NUNCA use travessões. Use vírgulas, pontos, parênteses.
NUNCA comece respostas com "Perfeito!", "Ótimo!", "Claro!", "Que
bom!", "Com certeza!". Vá direto ao conteúdo.
NUNCA use mais de uma exclamação por mensagem.
NUNCA use listas numeradas em conversa fluida com paciente.
NUNCA use jargão clínico. Linguagem comum.

Use contrações naturais: "tá", "pra", "tô".
Use emoji 🩵 com moderação, mais em mensagens de acolhimento.
Mantenha mensagens curtas: 80 a 120 palavras no máximo, exceto
quando estiver listando dados a coletar (aí pode ser maior).

# O que você faz

Recebe pacientes novos que chegam pelo WhatsApp da Allos. Sua
sequência típica:

1. Apresenta a Allos brevemente
2. Pergunta se faz sentido
3. Apresenta o plano (Allos básico, R$ 200/mês, sessões semanais)
4. Coleta os dados necessários pra cadastro:
   - Nome completo
   - Data de nascimento
   - Melhor número de WhatsApp pra contato
   - Número de uma pessoa de apoio
   - Endereço completo
   - Horários disponíveis pra terapia
   - Preferência sobre terapeuta (sexo, abordagem) — opcional
   - Motivo de busca por terapia — opcional

5. Quando você tiver todos os dados obrigatórios coletados, chama a
   ferramenta `cadastrar_paciente` com os dados estruturados.

# O que você NUNCA faz

- Não dá conselho clínico, psicológico, pessoal, religioso, político.
- Não diagnostica nem comenta sintomas.
- Não recomenda nem menciona medicamentos.
- Não responde sobre conteúdo de sessões.
- Não promete resultado terapêutico.
- Não inventa informação sobre a Allos. Se não souber, escala.
- Não fala sobre dados de outros pacientes.

# Quando escalar pra Thainá (use a ferramenta `escalar_para_thaina`)

- Paciente pede falar com humano, atendente, pessoa, Thainá (motivo: pedido_humano)
- Paciente menciona prefeitura, convênio com prefeitura, parceria municipal (motivo: prefeitura)
- Paciente menciona gratuidade, "não posso pagar", "é gratuito pra mim", "tem desconto pra mim" (motivo: gratuidade)
- Pergunta sobre algo que você não cobre e não é trivial (motivo: outro)
- Sinais sensíveis: ideação suicida, automutilação, abuso, crise aguda (motivo: outro, mas com contexto detalhado)

Quando for escalar, responda ao paciente algo curto e acolhedor antes
de chamar a ferramenta:

"Vou chamar a Thainá pra continuar daqui contigo, [nome]. Ela é a
coordenadora da nossa clínica e vai te responder em pouco tempo."

Se for crise/sinal sensível, inclua suporte imediato:

"[Nome], eu li o que você me mandou. Antes de qualquer coisa, você
não tá sozinha. Estou avisando agora a Thainá pra entrar em contato
com você o quanto antes. Se for muito urgente e não puder esperar,
ligue 188 (CVV). Eles atendem 24 horas, gratuito. Fica comigo aqui."

# Sobre o valor

Allos custa R$ 200/mês fixo. Sessões semanais de uma hora. Sai
menos de R$ 50 por sessão. Se o paciente questionar o preço,
explique de forma direta o que ele recebe pelo investimento.
Se ele disser que não consegue pagar, escala (motivo: gratuidade).
```

---

## Lógica de detecção complementar (fora do LLM)

Algumas detecções são feitas em código antes mesmo de chamar o LLM, pra economizar token e reagir mais rápido:

1. **Mensagem de áudio**: tipo `audio` no payload. Não chama LLM. Marca escalada direto.
2. **Mensagem de vídeo, sticker**: pede texto via mensagem fixa, sem LLM.
3. **Documento e imagem sem contexto**: pergunta via mensagem fixa.
4. **Mensagem do mesmo número em até 5 segundos depois da última recebida**: tratar como continuação, não disparar novo turno (evita troca dupla).

---

## Painel web

Funcionalidades mínimas:

**Página de lista** (`/painel/`):
- Tabela com colunas: número, nome (se cadastrado), modo, última mensagem (preview), tempo desde última atividade, status
- Badge destacando conversas que estão em modo humano não respondidas há mais de 30 min
- Filtros: "Todas", "Em modo humano", "Em escalada", "Cadastradas hoje"
- Refresh automático a cada 15s via HTMX

**Página de conversa** (`/painel/conversas/{id}/`):
- Header: número, nome (se cadastrado), modo, motivo da escalada se houver
- Histórico: scroll de mensagens, alternando bot/paciente/Thainá visualmente
- Campo de resposta + botão enviar
- Botões: "Assumir conversa", "Devolver ao bot" (alternam o modo)
- Refresh automático do histórico a cada 5s via HTMX

Layout simples, responsivo, pra Thainá usar do PC ou do celular. Sem CSS framework pesado. Pode usar Pico.css ou Simple.css pra ficar com aparência decente sem trabalho.

---

## Variáveis de ambiente

Esperadas no `.env` (local) e em variáveis do Render (produção):

```bash
# WhatsApp Cloud API
WHATSAPP_TOKEN=                    # Token permanente do system user
WHATSAPP_PHONE_NUMBER_ID=          # ID do número na Cloud API
WHATSAPP_VERIFY_TOKEN=             # String secreta pra validar webhook (definida por nós)
WHATSAPP_APP_SECRET=               # Pra validar X-Hub-Signature-256

# Número e template da Thainá
THAINA_WHATSAPP_NUMBER=            # Ex: 5531999998888
ALERT_TEMPLATE_NAME=alerta_thaina  # Nome do template aprovado

# OpenAI
OPENAI_API_KEY=
OPENAI_MODEL=gpt-4o-mini

# Banco
DATABASE_URL=                      # postgres://... no Neon

# Hamilton
HAMILTON_API_URL=                  # Ex: https://hamilton.allos.org.br
HAMILTON_API_KEY=

# Painel
PAINEL_USER=thaina
PAINEL_PASSWORD=                   # Senha aleatória

# Geral
LOG_LEVEL=INFO
ENVIRONMENT=production             # ou 'development'
```

---

## Estrutura de pastas sugerida

```
sofia/
├── app/
│   ├── __init__.py
│   ├── main.py                    # FastAPI app, montagem de rotas
│   ├── config.py                  # Settings via pydantic-settings
│   ├── database.py                # Engine async, session, Base
│   ├── models.py                  # SQLAlchemy: Conversa, Mensagem, Escalada
│   ├── schemas.py                 # Pydantic: WebhookPayload, etc
│   ├── dependencies.py            # Auth do painel, db session
│   ├── routers/
│   │   ├── __init__.py
│   │   ├── webhook.py             # GET/POST /webhook/whatsapp
│   │   ├── api.py                 # API interna (consumida pelo painel)
│   │   ├── painel.py              # Páginas HTML do painel
│   │   └── health.py              # GET /health
│   ├── services/
│   │   ├── __init__.py
│   │   ├── conversation.py        # Orquestrador: processa mensagem entrada
│   │   ├── whatsapp_client.py     # Wrapper Cloud API (enviar texto, template)
│   │   ├── llm_client.py          # Interface LLMClient + impl OpenAIClient
│   │   ├── hamilton_client.py     # Wrapper API Hamilton
│   │   └── escalation.py          # Lógica de escalada
│   ├── templates/                 # Jinja2
│   │   ├── base.html
│   │   ├── login.html
│   │   ├── painel_lista.html
│   │   └── painel_conversa.html
│   └── static/
│       ├── htmx.min.js
│       └── style.css
├── alembic/
│   ├── env.py
│   └── versions/
├── tests/
│   ├── __init__.py
│   ├── test_webhook.py
│   ├── test_conversation.py
│   └── test_escalation.py
├── .env.example
├── .gitignore
├── alembic.ini
├── pyproject.toml
├── README.md
└── render.yaml                    # Deploy config
```

---

## Roadmap de implementação

Ordem sugerida pra implementar incrementalmente, com cada passo validável antes de seguir:

### Passo 1: Esqueleto e webhook em modo eco

- Setup do projeto (pyproject.toml, FastAPI, structure)
- Configuração de variáveis de ambiente via pydantic-settings
- Endpoint `GET /webhook/whatsapp` validando challenge
- Endpoint `POST /webhook/whatsapp` validando assinatura, logando payload
- Endpoint `GET /health`
- Deploy no Render
- Configurar webhook na Meta apontando pra URL do Render
- Validar: mandar mensagem pelo WhatsApp, ver log no Render mostrando o payload

### Passo 2: Enviar mensagens

- Implementar `whatsapp_client.py` com método `enviar_texto(numero, texto)`
- Bot responde "ok, recebi: <mensagem original>" pra cada entrada
- Validar: paciente manda mensagem, recebe eco

### Passo 3: Persistência

- Configurar Neon e DATABASE_URL
- Modelos SQLAlchemy: Conversa, Mensagem, Escalada
- Migrations Alembic
- Webhook persiste conversa e mensagem antes de responder
- Validar: enviar várias mensagens, verificar banco

### Passo 4: Integração OpenAI

- Implementar `llm_client.py` com interface abstrata + impl OpenAI
- Carregar system prompt de arquivo
- Quando mensagem chega, montar histórico (últimas 20 msgs), enviar pro LLM
- Bot responde com texto gerado
- Validar: conversa fluida em português, tom adequado

### Passo 5: Tool calling e escalada

- Adicionar tools `cadastrar_paciente` e `escalar_para_thaina` na chamada OpenAI
- Implementar handler de tool call: quando `escalar_para_thaina` é chamado, marca conversa, registra escalada
- Implementar envio de template `alerta_thaina` pra número da Thainá
- Validar: paciente diz "quero falar com a Thainá" → conversa muda pra modo humano, Thainá recebe alerta

### Passo 6: Integração Hamilton

- Implementar `hamilton_client.py` com `buscar_paciente_por_telefone` e `criar_paciente`
- Handler de tool call `cadastrar_paciente`: faz POST no Hamilton, atualiza conversa
- Validar: conversa completa onde paciente cadastra com sucesso no Hamilton

### Passo 7: Painel web

- Páginas Jinja2 + HTMX (lista, conversa)
- Endpoints da API interna
- Autenticação HTTP Basic
- Validar: Thainá assume conversa, responde, devolve ao bot

### Passo 8: Polimento e produção

- Refresh automático no painel via HTMX
- Validação de assinatura do webhook
- Logging estruturado
- Tratamento de erros (Hamilton offline, OpenAI quota, etc)
- Submissão do template `alerta_thaina` na Meta (1 semana antes do go-live)
- Documentação no README

---

## Critérios de qualidade

### Comportamento conversacional

- Sofia nunca usa travessões
- Sofia nunca abre resposta com "Perfeito!", "Ótimo!", "Claro!", "Com certeza!"
- Sofia mantém mensagens curtas, salvo quando lista dados
- Sofia varia frases de saudação, não repete formato robotizado
- Sofia se apresenta apenas no primeiro contato da conversa ou após >60 dias de inatividade

### Robustez técnica

- Webhook responde 200 em menos de 3s sempre (processamento async)
- Idempotência: mesma mensagem da Meta nunca é processada duas vezes
- Falha em chamada externa (OpenAI, Hamilton) é logada e não derruba a conversa
- Nenhuma credencial em código versionado

### Segurança

- Validação de `X-Hub-Signature-256` em todo POST do webhook
- Autenticação no painel
- HTTPS em tudo (Render fornece)
- Secrets em variáveis de ambiente

### Observabilidade

- Log estruturado pra cada mensagem recebida, resposta gerada, escalada disparada, erro de integração
- Endpoint `/health` retorna 200
- Erros 500 são logados com stack trace

---

## Notas finais pro Claude Code

- Priorizar simplicidade sobre completude. Cada passo do roadmap deve resultar em algo testável.
- Não implementar nada do "fora de escopo" mesmo que pareça útil. Cada feature adicional aumenta superfície de bug.
- Toda string em código que vai pro paciente deve passar pelo system prompt da Sofia (via LLM) ou ser revisada manualmente. Não inventar mensagens hardcoded.
- Comentários em código apenas onde não-trivial. Nome de função e estrutura devem se explicar.
- Testes mínimos: ao menos cobertura dos handlers principais (webhook, processamento de mensagem, escalada).
- Usar `httpx` async pra todas as chamadas externas.
- Quando estiver em dúvida sobre comportamento, optar pela escalada (escalada é segura, ação automática errada é arriscada).

Em caso de dúvida sobre regra de negócio, perguntar ao Paulo antes de inferir.
