# Demanda técnica — Hamilton e banco de produção

> **O que é.** A Sofia (bot do WhatsApp) ganhou três integrações novas com o
> Hamilton neste ciclo. Do lado dela está tudo pronto e testado. Do lado do
> Hamilton falta **merjar, migrar, configurar e escrever uma tela**.
>
> **O que este documento é.** A especificação do que muda no Hamilton e no banco,
> com o motivo de cada mudança e o que quebra sem ela. Não tem decisão de negócio
> aqui — essas estão em [`../MUDANCAS-AGOSTO-2026.md`](../MUDANCAS-AGOSTO-2026.md).
>
> **Ordem de leitura:** o bloco da branch primeiro (merjar a errada custa o dia),
> depois banco, backend, front, configuração. A ordem de execução está no fim.

---

## O que você recebe

| Bloco | O que é | Risco se pular |
|---|---|---|
| **Branch** | Merjar `feat/avaliacao-pesquisas-sofia` (Demandas A e C, prontas desde 08/08) | Paciente de convênio cobrado; nenhuma pesquisa sai |
| **Banco** | 6 migrations: 2 de schema, 2 de dado, 1 de permissão, 1 tabela nova | Erro de coluna inexistente no primeiro request da Sofia |
| **Backend** | Pacote `principais/contratos/` (novo) + 5 rotas | Contrato não gera |
| **Front** | `ContratoPaciente` não está em tela nenhuma | Coordenação não vê contrato nem baixa o PDF assinado |
| **Config** | 4 env vars no Hamilton, 4 na Sofia, 1 webhook externo | Feature desligada em silêncio (503), ou bot mudo |

**Nada disso exige escrever código novo, exceto o Bloco Front.**

Testes hoje: Sofia **737 passando**; Hamilton **33 do contrato** (SQLite in-memory,
1,3s — a suíte não toca no Neon desde a mudança em `app/settings.py`).

---

## 🔴 Comece por aqui: qual branch merjar

O `hamilton-api` tem **quatro** branches abertas. Duas são tentativas concorrentes
do mesmo trabalho, e **a que tem menos commits não é a mais antiga**.

| Branch | Data | Commits | |
|---|---|---|---|
| **`feat/avaliacao-pesquisas-sofia`** | 08/08 | 5 | ✅ **merjar esta** |
| `feat/sofia-captacao-e-avaliacao` | 06/08 | 1 (squash) | ❌ apagar |
| `feat/sofia-api-db-teste` | — | 1 | só um ícone, ignorar |
| `fix/db-credential-hardcoded` | — | 1 | ⚠️ ver ressalva |

**O que a de 06/08 não tem:**

| Falta | Consequência |
|---|---|
| `principais/0005_terapeuta_sentinela` | A pesquisa de entrada grava a `Avaliacao` **antes** do match. `fk_terapeuta` é obrigatória. Sem o sentinela, não há o que gravar |
| `principais/0007_permissao_api_sofia` | As rotas ficam abertas a qualquer usuário logado do Hamilton |
| `acessorios/0003_marcar_parcerias_existentes` | `is_parceria` fica `False` para as prefeituras já cadastradas → **paciente de convênio é cobrado** |
| Os dois fixes de 08/08 | O intake volta a descartar captação/valor/parceria; `POST /avaliacoes/` volta a dar 500 |

**Ressalva sobre `fix/db-credential-hardcoded`:** remove uma credencial de banco
hardcoded (bom), mas também remove `*.env` do `.gitignore` (passaria a rastrear o
`hamilton-v2.env`) e apaga o bloco do `SOFIA_API_DATABASE_URL`, do qual o código do
contrato depende (`servico.db_alias()`). **Não merjar como está** — extrair só a
remoção da credencial.

---

## Bloco 1 — Banco de dados

### Visão geral

Seis migrations, nesta ordem. **Nenhuma apaga dado.** Duas mexem em linhas
existentes (`acessorios.0003` e `principais.0005`), as duas com `reverse` escrito.

| # | Migration | Tipo | O que faz |
|---|---|---|---|
| 1 | `acessorios.0002_captacao_is_parceria` | schema | `+1` coluna em `captacao` |
| 2 | `acessorios.0003_marcar_parcerias_existentes` | **dado** | `UPDATE` em captações de prefeitura |
| 3 | `principais.0005_terapeuta_sentinela` | **dado** | carimba a `observacao` de 1 terapeuta |
| 4 | `principais.0006_avaliacao_respostas_pesquisa` | schema | `+6` colunas, `~3` alteradas em `avaliacao` |
| 5 | `principais.0007_permissao_api_sofia` | **dado** | cria permissão + grupo |
| 6 | `principais.0008_contratopaciente` | schema | **tabela nova** `contrato_paciente` |

---

### 1. `acessorios.0002` — `Captacao.is_parceria`

```python
AddField("captacao", "is_parceria", BooleanField(default=False))
```

**Por quê.** A Allos tem convênios (prefeituras) em que **o parceiro paga por
consulta realizada e o paciente não paga nada**. Não havia nenhuma marca disso no
banco. Existiam duas fontes divergentes, ambas em código: um set literal
`PREFEITURAS_CAPTACAO_IDS = {13, 46}` em `views.py`, e um `nome contém
'Prefeitura'` no gerador de relatório. As duas saíram; a `Captacao` passa a ser a
fonte única.

**O que quebra sem ela.** A Sofia lê `is_parceria` do payload de
`GET /api/v1/captacoes/`. Chave ausente vira `False` — silenciosamente. Resultado:
**nenhum paciente de convênio é detectado**, todos entram com `vlr_sessao` de
particular e, com a cobrança ligada, todos são cobrados.

**Risco:** nenhum. Coluna nova com default.

---

### 2. `acessorios.0003` — backfill das parcerias existentes

```python
Captacao.objects.filter(nome__icontains="prefeitura").update(is_parceria=True)
```

**Por quê.** A coluna nasce `False` para todo mundo, inclusive para as prefeituras
que já estão cadastradas há tempo. Sem este passo, a `0002` não resolve nada
retroativamente.

**⚠️ É heurística, e sabe disso.** Casa por nome porque não há flag, lista
versionada nem fixture de onde tirar a verdade. **A migration imprime no console o
que marcou** — leia a saída:

```
[acessorios.0003] N captação(ões) marcada(s) como parceria em 'default':
  - 13: Prefeitura de Bela Vista de Minas
  ...
```

**Um convênio que não tenha "prefeitura" no nome (uma empresa, por exemplo) NÃO
será marcado.** Confira a lista de captações no admin depois de rodar e marque à
mão o que faltar.

**Reversível:** sim (`desmarcar` zera todas). ⚠️ O reverse é destrutivo no sentido
de que apaga marcações feitas à mão depois — não role para trás sem olhar.

---

### 3. `principais.0005` — terapeuta sentinela

**Em produção esta migration não cria nada.** O registro já existe: é o
`pk_terapeuta = 73`, que o `signals.py` usava **hard-coded** como
`terapeuta_aguardando` (346 dos 576 pacientes da branch de teste apontam pra ele).
A migration só **carimba um marcador na `observacao`** dele, sem apagar o que já
estiver escrito.

**Por quê existe como migration.** `Terapeuta` tem **seis FKs obrigatórias**
(incluindo `fk_associado` e `fk_decano`). Em banco novo — teste, restore, ambiente
limpo — criar isso à mão é o passo que ninguém lembra, e a falha só aparece quando
a Sofia tenta criar a primeira avaliação de linha de base. A migration monta a
cadeia inteira nesse caso.

**Por que um marcador e não o id.** Para o código parar de depender do literal
`73`. `principais/sentinela.py` resolve por `SOFIA_TERAPEUTA_SENTINELA_ID` se
configurado, senão pelo marcador na observação.

**O que quebra sem ela.** A pesquisa de **entrada** (ORS de linha de base) roda
antes de a coordenação fazer o match. A `Avaliacao` exige `fk_terapeuta`. Sem um
terapeuta identificável para "ainda não alocado", a criação falha.

**Risco:** baixo. Um `UPDATE` numa coluna de texto de um registro.

---

### 4. `principais.0006` — campos de resposta da `Avaliacao`

**Seis colunas novas**, todas `null=True` / `blank=True`:

| Coluna | Tipo | Para quê |
|---|---|---|
| `feedback_livre` | Text | resposta aberta da pesquisa |
| `motivo_encerramento` | Text | por que trocou de terapeuta ou parou |
| `nota_indicacao` | Integer (0-10) | NPS |
| `nota_sofia` | Integer (0-10) | nota do acolhimento da própria Sofia |
| `sofia_enviada_em` | DateTime | quando a Sofia **abordou** |
| `sofia_lembrete_em` | DateTime | quando mandou o lembrete de 20h |

**Três colunas alteradas:** `continuar_allos`, `continuar_terapeuta` (viram
nulláveis — "não perguntado" é diferente de "não") e `momento` (novos valores).

**Por que `sofia_enviada_em` e `sofia_lembrete_em` são indispensáveis.** O
`status='pendente'` da `Avaliacao` significa **"sem resposta"**, não "sem envio".
O cron da Sofia roda de hora em hora lendo os pendentes. Sem essas duas colunas
ela **abordaria a mesma pessoa a cada tick**.

**O que quebra sem ela.** `PATCH /api/v1/avaliacoes/{pk}/` estoura erro de coluna
inexistente na primeira resposta de pesquisa.

**Risco:** nenhum para os dados existentes. As três `AlterField` afrouxam
restrição (`NOT NULL` → nullable), não apertam.

---

### 5. `principais.0007` — permissão `acessar_api_sofia`

Cria a permissão `principais.acessar_api_sofia` e o grupo **"API Sofia"** já com
ela.

**Por quê.** Antes, **qualquer usuário logado do Hamilton** acessava as rotas
`/api/v1/` consumidas pela Sofia, com token válido por um dia.

**Por que a permissão é criada à mão dentro da migration:** o `create_permissions`
do Django só roda no `post_migrate`, **depois** desta migration. Sem criar à mão, o
grupo nasceria vazio.

**Por que o grupo já vem montado:** para que rebaixar o usuário `sofia-bot` (que
**hoje é superusuário**) seja um clique no admin, e não um roteiro que alguém
precisa lembrar de executar.

> ⚠️ **Enquanto o `sofia-bot` for superusuário, esta permissão não restringe nada
> para ele** — `has_perm()` sempre devolve `True` para superusuário. Rebaixá-lo é
> um passo à parte, opcional agora, recomendado depois.

**Risco:** nenhum.

---

### 6. `principais.0008` — tabela `contrato_paciente`

Tabela nova. Modelo `ContratoPaciente` em `principais/models.py`.

| Coluna | Tipo | Nota |
|---|---|---|
| `pk_contrato` | AutoField | |
| `fk_paciente` | FK → `Paciente`, **CASCADE**, `related_name='contratos'` | |
| `status` | varchar(20) | `pendente` \| `assinado` \| `recusado` \| `expirado` \| `substituido` |
| `valor_mensal` | Decimal(10,2) | o valor **escrito no contrato assinado** |
| `autentique_id` | varchar(64) **UNIQUE** | id do documento na Autentique |
| `public_id_signatario` | varchar(64) | 🔴 ver nota |
| `link_assinatura` | URL(500) | |
| `sandbox` | bool | documento de teste; não vale como contrato |
| `texto_usado` | Text | a redação exata que a pessoa assinou |
| `pdf_assinado` | **BinaryField** (`editable=False`) | bytes |
| `cpf_informado` | varchar(14) | colhido pela Autentique no ato |
| `ip_assinatura` | GenericIPAddress | |
| `enviado_em` / `assinado_em` / `atualizado_em` | DateTime | |

Índice: `(fk_paciente, status)`. Ordering: `-enviado_em`.

**Quatro decisões de modelagem que não são óbvias:**

1. **É histórico, nunca substituição.** Contrato é documento jurídico. Apagar o
   anterior destruiria a prova de qual condição valia em qual período — que é
   exatamente o que alguém pergunta num conflito. É também o que faz a renovação
   de 12 meses (cláusula 9.1) funcionar sem ninguém lembrar.
2. **`pdf_assinado` é bytes no Postgres, não arquivo.** O `MEDIA_ROOT` do Render é
   efêmero e some a cada deploy. São ~20 contratos/mês de ~300 KB — uns **6 MB por
   ano**. Mesma decisão que a tabela `midia` da Sofia já tinha tomado.
3. **`texto_usado` não é redundante.** O texto do contrato é editável em runtime
   (no painel da Sofia). Sem esta coluna, ninguém consegue responder *"qual era a
   redação que essa pessoa assinou?"* depois de o texto mudar.
4. **🔴 `public_id_signatario` é obrigatório para ler a resposta certa.** A
   Autentique insere **a conta dona do token** como primeira entrada de
   `signatures`. Ler o signatário por índice pega a pessoa errada — e o erro é
   silencioso.

**Risco:** nenhum. Tabela nova.

**Crescimento:** ~6 MB/ano em `pdf_assinado`. Se um dia incomodar, é o mesmo
gatilho da `midia`: migrar para bucket externo.

---

### 🔴 Colisão de numeração — resolver ANTES de migrar

A migration do contrato está no disco como **`principais/0005_contratopaciente.py`**
e colide com a `0005_terapeuta_sentinela.py` que vem no merge.

Depois de merjar:

1. renomear o arquivo para `0008_contratopaciente.py`;
2. dentro dele, trocar
   `dependencies = [("principais", "0004_backfill_vinculo_stripe")]`
   por `dependencies = [("principais", "0007_permissao_api_sofia")]`.

Alternativa: `python manage.py makemigrations --merge`. Prefira renomear à mão —
a cadeia fica linear e legível.

---

### 🔴 As migrations não estão no git

`.gitignore:23` do `hamilton-api` tem `**/migrations/**`. As `0001` são rastreadas
porque foram adicionadas antes da regra; **tudo acima da `0004` o git ignora**.
Elas existem no disco e **não vão pro GitHub sozinhas**.

```bash
git add -f principais/migrations/ acessorios/migrations/
git status   # confirme: principais 0005..0008 e acessorios 0002, 0003
```

Este acidente já aconteceu neste repositório antes — foi assim que as Demandas A e
C ficaram "prontas" sem funcionar.

---

### 🔴 Conferir o banco antes de escrever

**`SOFIA_API_DATABASE_URL` no serviço do Hamilton no Render.** Se estiver setada,
**todos os endpoints da Sofia leem e gravam nela** — em produção isso manda
paciente real pro banco de teste. `render.yaml:87` avisa; o `DEPLOY.md` que ele
referencia não fala disso.

Confirme o banco pelo `timeline_id`, **nunca pelo nome do arquivo de env**:

```bash
python -c "
import os, django; os.environ.setdefault('DJANGO_SETTINGS_MODULE','app.settings'); django.setup()
from django.db import connections
with connections['default'].cursor() as c:
    c.execute('SHOW neon.timeline_id'); print(c.fetchone()[0])"
```

| timeline_id | Banco |
|---|---|
| `d816d0c2…` | branch `sofia-teste` — pode escrever |
| `fdb211ba…` | **PRODUÇÃO** |

---

## Bloco 2 — Backend

### 2.1 O intake estava descartando dado (vem no merge)

`PacienteIntakeSerializer.create()` sobrescrevia o que a Sofia mandava:

```python
validated_data["fk_captacao"] = defaults["captacao"]   # get_or_create("WhatsApp (Sofia)")
validated_data["vlr_sessao"]  = defaults["vlr_sessao"]
```

Pior: `fk_captacao` **nem estava em `Meta.fields`**, então o valor da Sofia era
descartado antes de chegar ao `create`.

**Efeito medido** num dry run de ponta a ponta: três pacientes com origens
diferentes entraram os três como `"WhatsApp (Sofia)"` e `vlr_sessao = 50,00`.

**Correção (já na branch):** os três campos passam a ser aceitos e o default vira
*fallback*. O ID da captação é resolvido à mão como `IntegerField` cru — **não**
como `PrimaryKeyRelatedField`, porque um queryset de FK leria o banco `default` e o
router recusaria a relação quando `SOFIA_API_DATABASE_URL` estiver ativo.

**Validação esperada** (feita na branch `sofia-teste`):
Google → captação 15, R$ 200, `manual` · Prefeitura → captação 13 `[PARCERIA]`,
**R$ 0,00**, `parceria`.

### 2.2 `signals.py` (vem no merge)

Dois pontos:

- sai o `Terapeuta.objects.get(pk_terapeuta=73)` hard-coded, entra
  `sentinela.obter()`;
- entram os signals que criam a `Avaliacao` com `status='pendente'` quando o
  terapeuta lança a 1ª consulta ou uma alta/desistência. **A fila de pesquisas é
  isso** — a Sofia faz *polling* dela.

> **Por que polling e não webhook:** o Hamilton é 100% síncrono, sem worker. Um
> webhook rodaria **dentro do request do terapeuta** e travaria o salvamento do
> prontuário. Puxar dá retry de graça.

> ⚠️ **Dívida conhecida:** `criar_avaliacao_consulta` **não respeita o alias de
> banco**. Enquanto `SOFIA_API_DATABASE_URL` estiver vazia em produção não faz
> mal, mas é uma armadilha armada. Não bloqueia esta subida.

### 2.3 Pacote `principais/contratos/` (não commitado)

~1.100 linhas, cinco arquivos.

| Arquivo | O que faz |
|---|---|
| `documento.py` | texto + dados → `.docx` (python-docx). Valida marcadores |
| `autentique.py` | cliente da API GraphQL v2: criar, consultar, baixar PDF, HMAC |
| `servico.py` | guardas, idempotência, gravação, sincronização |
| `views.py` | as cinco rotas |
| `LEIA-ME.md` | documentação do pacote |

**⚠️ O texto do contrato NÃO fica neste repositório.** Ele é editado no painel da
Sofia (`/painel/prompts`) e **chega no corpo da requisição**. Este pacote é um
renderizador: recebe texto + paciente, devolve documento. Uma cópia guardada aqui
divergiria da do painel no primeiro ajuste, e ninguém saberia qual das duas o
paciente assinou.

Marcadores: `{{PAC_NOME}}`, `{{PAC_ENDERECO}}`, `{{FIN_MENSAL}}`, `{{VIG_DATA}}`.

**As guardas do `servico.py`, na ordem em que rodam:**

1. **Valor, antes de qualquer chamada externa.** Faixa aceita **R$ 100 a R$
   5.000**; zero e negativo recusados. Motivo: o `vlr_sessao` do Hamilton carrega
   **dois significados** — mensalidade (R$ 200, o que a Sofia grava) e valor por
   sessão (R$ 50, R$ 60 — herança dos pacientes antigos). Sem piso, um paciente
   antigo receberia contrato de *"mensalidade de R$ 50"*. Recusar depois de criar
   o documento gastaria crédito e deixaria lixo na conta.
2. **Idempotência, antes de gerar.** A Sofia remonta o link a cada turno da
   cobrança. Sem isso, cada turno criaria um contrato novo e o paciente receberia
   três links diferentes pro mesmo combinado.
3. **O contrato atualiza o `vlr_sessao` do paciente.** Se a Sofia negociou
   desconto e o cadastro ficasse pra trás, a NF sairia por um valor e o documento
   assinado diria outro — e o documento assinado é prova **contra** a Allos.

**O CPF vem da assinatura.** A Autentique colhe CPF e nascimento no ato, em
`user_data { cpf birthday }` — **não** em `user`; a query precisa pedir
explicitamente. É o único dado que este fluxo devolve, e costuma ser o único CPF
que existe (lead da Sofia nasce sem). **Nunca sobrescreve um CPF já preenchido.**

**Dependência:** o pacote usa `requests`, não `httpx`. Motivo: `httpx` está no
`requirements.txt` do Hamilton mas **não está instalado** no ambiente. Ou instala,
ou tira do requirements — do jeito que está, o arquivo mente.

### 2.4 As rotas (`principais/urls.py`, prefixo `/api/v1/`)

| Rota | Auth | O que faz |
|---|---|---|
| `POST /api/v1/contratos/` | JWT | gera (ou reaproveita) e devolve o link |
| `GET /api/v1/contratos/?paciente_id=` | JWT | estado; sincroniza com a Autentique sob demanda |
| `GET /api/v1/contratos/pendentes/` | JWT | todos os não assinados, em lote |
| `POST /api/v1/contratos/previa/` | JWT | renderiza com dados fictícios, **sem tocar na Autentique** |
| `POST /api/v1/contratos/webhook/` | **HMAC** | a Autentique avisando assinatura/recusa |

O webhook é público por natureza (quem chama é a Autentique) e é o único lugar que
valida HMAC. **Sem `AUTENTIQUE_WEBHOOK_SECRET`, nenhum webhook passa** — webhook
não autenticado escrevendo em contrato é pior que webhook nenhum, e o `GET`
sincroniza sob demanda de qualquer forma.

`GET /contratos/pendentes/` existe para a tela **Hoje** da Sofia. Não sincroniza
com a Autentique de propósito: é leitura do nosso banco. Uma tela em polling de 15s
não pode disparar dezenas de chamadas externas.

### 2.5 Tudo que a Sofia chama no Hamilton

| Método | Rota | Estado hoje |
|---|---|---|
| GET | `/api/v1/captacoes/` | ✅ em `main` (fica em `acessorios/urls.py`) |
| GET | `/api/v1/pacientes/buscar/?telefone=` | ✅ em `main` |
| POST | `/api/v1/pacientes/` | ⚠️ existe, mas **descarta captação/valor/parceria** — ver 2.1 |
| PATCH | `/api/v1/pacientes/{pk}/atualizar/` | ✅ em `main` |
| GET | `/api/v1/pacientes/status-primeira-consulta/?ids=` | ✅ em `main` |
| GET | `/api/v1/avaliacoes/pendentes/` | ❌ só na branch |
| POST | `/api/v1/avaliacoes/` | ❌ só na branch |
| GET · PATCH | `/api/v1/avaliacoes/{pk}/` | ❌ só na branch |
| POST · GET | `/api/v1/contratos/` | ❌ não commitado |
| GET | `/api/v1/contratos/pendentes/` | ❌ não commitado |
| POST | `/api/v1/contratos/previa/` | ❌ não commitado |

Auth: **JWT** (usuário `sofia-bot`). O cliente re-autentica uma vez no 401.

> **Nota sobre `POST /api/v1/avaliacoes/`:** em `main` essa rota é a **tela HTML**
> de avaliações (`AvaliacaoListView`), que não aceita POST. Na branch ela vira um
> dispatcher (`avaliacoes_raiz`): GET continua servindo a tela da coordenação, POST
> vai pra API da Sofia. Convivem na mesma rota porque o contrato acordado com a
> Sofia é `POST /api/v1/avaliacoes/`, e a tela não aceita POST — não há ambiguidade.

> **A Sofia não precisa de migration.** `app/models.py` dela não mudou; o contrato
> mora inteiro no Hamilton. O `alembic upgrade head` dela só aplica o que já estava
> pendente (head `c9e1f4a7b3d8`, o link curto).

---

## Bloco 3 — Frontend (⚠️ é o único que precisa de código novo)

O contrato funciona ponta a ponta pela API, mas **a coordenação não vê um contrato
pela interface do Hamilton**. Duas coisas, em ordem de importância:

### 3.1 Registrar `ContratoPaciente` no admin

`principais/admin.py` já registra `Avaliacao`, `Pagamento`, `Altadesistencia` e
outros via `BaseAdmin`. O contrato ficou de fora.

- `list_display` sugerido: paciente, status, `valor_mensal`, `enviado_em`,
  `assinado_em`
- `list_filter`: `status`, `sandbox`
- `search_fields`: nome do paciente
- ⚠️ `pdf_assinado` é `editable=False` — **não pode entrar em `fields`**
- ⚠️ `texto_usado` e `cpf_informado` merecem `readonly_fields`: são prova, não
  campo editável

### 3.2 Card "Contrato" na página do paciente

`principais/templates/pacientes/paciente_detail.html` (154 linhas) já é uma grade
de cards Bootstrap: *Informações Gerais*, *Status da Terapia*, *Classificação e
Origem*, *Contato de Apoio*, *Observações*. Um card novo seguindo o mesmo padrão.

Mostrar: status, `valor_mensal`, `enviado_em`, `assinado_em`, e um botão para
baixar o PDF.

`PacienteDetailView` (`principais/views.py:3846`) já faz `select_related` dos
outros relacionamentos — acrescentar `.prefetch_related('contratos')` no
`get_queryset()`.

Existe um atalho pronto, que **não é rota**:
`principais.contratos.views.contratos_ativos_do_paciente(paciente_id)` — devolve o
estado já resolvido.

### 3.3 View de download do PDF (não existe)

Ler `contrato.pdf_assinado` e devolver com:

```python
Content-Type: application/pdf
Content-Disposition: attachment; filename="contrato-<pk>.pdf"
X-Content-Type-Options: nosniff
```

Atrás de `LoginRequiredMixin` + `PermissionRequiredMixin`.

> 🔴 **É documento com CPF e IP de paciente.** LGPD: não pode ficar em rota aberta
> nem em URL adivinhável sem permissão.

---

## Bloco 4 — Configuração

### Hamilton (Render)

| Variável | Valor | Sem ela |
|---|---|---|
| `AUTENTIQUE_TOKEN` | token da conta | rotas de contrato devolvem **503**; nada quebra |
| `AUTENTIQUE_WEBHOOK_SECRET` | você define; tem que bater com o painel da Autentique | **nenhum webhook passa** |
| `AUTENTIQUE_SANDBOX` | `false` em produção | vazio = liga sozinho fora de `ENVIRONMENT=production` |
| `AUTENTIQUE_PRAZO_DIAS` | `30` | é o default, opcional |

> **`AUTENTIQUE_SANDBOX` é seguro por omissão de propósito.** Vazio, ele liga fora
> de produção. Motivo: o `.env` de desenvolvimento carrega o token **real**, e sem
> isso rodar a app (ou um teste mal mockado) no laptop de alguém gera contrato de
> verdade na conta da Allos. Já aconteceu com o Stripe — um `pytest` criou quatro
> Payment Links na conta LIVE.

### Sofia (Render)

| Variável | Valor | Sem ela |
|---|---|---|
| `OPENAI_MODEL` | `gpt-5.6-terra` | |
| `OPENAI_REASONING_EFFORT` | **`none`** | 🔴 ver abaixo |
| `OPENAI_REASONING_EFFORT_EXTRACAO` | `low` | extração da pesquisa menos precisa |
| `LINK_CURTO_BASE` | `https://allos.org.br/p` | link de pagamento sai apontando pro `onrender.com` |
| `API_AUTHENTIC` | **remover** | era temporária; quem fala com a Autentique é o Hamilton |

> 🔴 **`OPENAI_REASONING_EFFORT=none` é obrigatória.** Com function calling — que a
> Sofia usa em **todo turno** — o `gpt-5.6` em `/v1/chat/completions` recusa
> qualquer outro valor:
>
> ```
> Function tools with reasoning_effort are not supported for gpt-5.6-terra
> in /v1/chat/completions. To use function tools, use /v1/responses or set
> reasoning_effort to 'none'.
> ```
>
> Sem a variável, o cliente não envia o parâmetro, a API aplica o padrão
> (`medium`), e **todo turno cai no texto de fallback**: o bot responde "tive um
> probleminha técnico" para todo mundo, sem erro visível em lugar nenhum.

### Webhook na Autentique (fora dos repos)

No **painel da Autentique** — não dá por API — apontando para:

```
https://<hamilton>/api/v1/contratos/webhook/
```

Sem isso o contrato é gerado e assinado, mas o Hamilton não fica sabendo: o PDF
assinado e o CPF nunca voltam pro prontuário. (O `GET` sincroniza sob demanda, mas
só quando alguém consulta.)

### Crons (cron-job.org, header `X-Tasks-Token`)

| Endpoint | Estado | Sem ele |
|---|---|---|
| `POST /tasks/stripe` | 🔴 **não existe** | Todo parcelado de neuro **cobra pra sempre**. Já aconteceu com 18 pacientes |
| `POST /tasks/pesquisas` | não existe | nenhuma pesquisa sai |
| `POST /tasks/cobrancas` | não existe | nenhuma mensalidade é cobrada |
| `POST /tasks/seguimentos` | ✅ existe | nada a fazer |

---

## A ordem de execução

```bash
# 0. ONDE ESTOU?  (timeline_id + SOFIA_API_DATABASE_URL no Render)

# 1. merjar a branch certa
git checkout main
git merge origin/feat/avaliacao-pesquisas-sofia
git push origin --delete feat/sofia-captacao-e-avaliacao

# 2. renumerar a migration do contrato
#    principais/0005_contratopaciente.py -> 0008_contratopaciente.py
#    dependencies: 0004_backfill_vinculo_stripe -> 0007_permissao_api_sofia

# 3. forçar as migrations pro git
git add -f principais/migrations/ acessorios/migrations/

# 4. conferir o plano ANTES de aplicar
python manage.py showmigrations acessorios principais
python manage.py migrate --plan

# 5. aplicar  (leia a saída da acessorios.0003!)
python manage.py migrate

# 6. Sofia
alembic upgrade head        # head: c9e1f4a7b3d8

# 7. env vars (Bloco 4)  ->  8. webhook na Autentique  ->  9. crons
```

**Ligar as chaves é o último passo, uma por vez.** Tudo que é automático e fala de
dinheiro ou de documento jurídico sobe desligado de propósito:

| Chave | Onde | Nasce |
|---|---|---|
| `SOFIA_PESQUISAS_ATIVAS` | env do Hamilton | desligada |
| `cobranca_ativa` | `/painel/config` da Sofia | desligada |
| `contrato_ativo` | `/painel/config` da Sofia | desligada |

> A cobrança **nunca rodou em produção**. Ligue-a primeiro, sozinha, e o contrato
> semanas depois. Estrear as duas no mesmo turno que fala de dinheiro com paciente
> é o risco que este projeto já pagou uma vez.

---

## Como validar

Esta integração **degrada em silêncio** — é o modo de falha dela. Chave ausente
vira `False`, endpoint faltando vira lista vazia, e tudo continua parecendo normal.
Foi assim que a Demanda A ficou meses "pronta" sem funcionar, com as suítes dos
dois repos passando. **Confira ativamente.**

### Contrato — ciclo real, com assinatura de verdade

```bash
python manage.py validar_contrato criar      # cria e imprime o link
#   <- abra o link no navegador e assine
python manage.py validar_contrato conferir   # confere o que voltou
python manage.py validar_contrato limpar     # apaga tudo (aqui e na Autentique)
```

Este comando existe porque **mock não pega contrato de API quebrado** — foi assim
que o parcelado do Stripe teve teste verde e feature morta. Travas: só roda em
sandbox, e confere o `timeline_id` do Neon antes de escrever.

### Captação e parceria — o que estava furado

Cadastre dois pacientes de teste pela Sofia: um dizendo que veio pelo Google, outro
dizendo que é servidor de prefeitura conveniada.

| Deve entrar como | |
|---|---|
| Google | captação com o ID certo (**não** "WhatsApp (Sofia)"), `vlr_sessao = 200`, `tipo_pagamento = manual` |
| Prefeitura | captação `is_parceria`, **`vlr_sessao = 0,00`**, `tipo_pagamento = parceria` |

**Se os dois entrarem como "WhatsApp (Sofia)" com R$ 50, o merge não pegou.**

### Pesquisa

`GET /api/v1/avaliacoes/pendentes/` tem que devolver lista **não vazia** havendo
pendentes. Vazia com `SOFIA_PESQUISAS_ATIVAS` desligada é o comportamento correto,
não bug.

### Modelo

Mande uma mensagem qualquer pelo WhatsApp. Resposta genérica de erro =
`OPENAI_REASONING_EFFORT` não está em `none`.

---

## Rollback

| Se der errado em | Como voltar |
|---|---|
| Contrato | desligar `contrato_ativo` no `/painel/config`. A cobrança segue sem ele — `contrato.garantir()` já trata a ausência |
| Cobrança | desligar `cobranca_ativa` |
| Modelo | voltar `OPENAI_MODEL`. O `llm_client` remove sozinho os parâmetros que o modelo antigo não conhece |
| Migrations de schema | só **acrescentam** tabela e colunas; nenhuma apaga dado |
| `acessorios.0003` | tem `reverse`, mas ele zera **todas** as marcações — inclusive as feitas à mão depois. Prefira `UPDATE` pontual |
| `principais.0005` | o reverse não desfaz o carimbo na `observacao` (de propósito: é texto de usuário) |

---

## Dívidas que ficam abertas

1. **`signals.criar_avaliacao_consulta` ignora o alias de banco.** Mesmo tipo de
   bug que já mandou dado pro banco errado. Inofensivo enquanto
   `SOFIA_API_DATABASE_URL` estiver vazia em produção.
2. **`httpx` no `requirements.txt` mas não instalado.** Por isso o cliente da
   Autentique usa `requests`.
3. **`sofia-bot` ainda é superusuário.** A `0007` já deixou o grupo "API Sofia"
   pronto — rebaixar é um clique no admin.
4. **O webhook do Stripe no Hamilton continua quebrado** (21 assinaturas, 0
   faturas). É o motivo de a Sofia ter integração própria; a consequência aceita é
   que a Allos tem **duas integrações Stripe**, e as assinaturas criadas pela Sofia
   não aparecem onde moram a contabilidade e a NFS-e.
5. **Pendências financeiras** de [`04-PENDENCIAS-ABERTAS.md`](04-PENDENCIAS-ABERTAS.md):
   assinatura duplicada da Tatiane (~R$ 388 a mais) e duas faturas em aberto.
