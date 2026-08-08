# Demandas — Sofia (ciclo pós-P6)

> Documento de implementação, fechado em grilling com o Paulo em 05/08/2026.
> Substitui o `00-ORIGINAL-com-premissas-erradas.md`, que foi escrito por quem não
> conhecia o fluxo real e errou várias premissas (registradas abaixo).

## Status (06/08/2026)

| # | Entrega | Status |
|---|---|---|
| 1 | **Demanda A** — origem real do paciente + parceria + fluxo de prefeitura | ✅ **entregue** |
| 2 | **Demanda B** — neuro/Amanda + aviso único pós-escalada | ✅ **entregue** |
| 3 | **Ajuste da tabela `Avaliacao`** | 🔴 **não existe no Hamilton** — modelo redesenhado, ver abaixo |
| 4 | **Demanda C** — pesquisas de satisfação | 🔴 **não funciona** — o lado Sofia existe, o lado Hamilton não |
| 5 | **Demanda D** — cobrança (Pix + Stripe) | ❌ **não iniciada** |

> 🔴 **Correção de 06/08/2026.** Este documento afirmava que os campos da `Avaliacao`,
> a migration `0005` e os endpoints de avaliação estavam prontos e testados no Hamilton.
> **Nada disso existe no repositório** (verificado em `../hamilton-api`, `main`, working
> tree limpo — sem `nota_sofia`/`nota_terapeuta`/`sofia_enviada_em`, migrations param na
> `0004`, sem rota `avaliacoes/`). Provável causa: o `.gitignore` do Hamilton ignorava as
> migrations. **Consequência: a Demanda C não roda hoje**, mesmo com o cron ligado — o
> `pesquisa.py` da Sofia fala com uma API que não existe.
>
> O modelo de perguntas foi **inteiramente redesenhado** em grilling (06/08/2026, Q1–Q38):
> quatro questionários, ORS como bloco fechado, baseline **antes** da primeira sessão,
> tool de registro incremental e alertas pra Thainá. **Fonte de verdade:**
> [`02-modelo-de-avaliacao.md`](02-modelo-de-avaliacao.md). As seções C.2, C.5 e C.6
> abaixo descrevem o desenho **antigo** e foram substituídas por ele.

**Testes:** Sofia **375** passando; Hamilton **23** passando
(`principais.tests_sofia_api`). Lint (ruff/black/isort) limpo nos dois.

Cobertura nova deste ciclo: `tests/test_captacao.py` (23), `tests/test_pesquisa.py`
(55), captação e modo pesquisa no `test_webhook.py` (7), follow-up × pesquisa no
`test_seguimento.py` (2), e os signals do Hamilton (4). O teste dos signals foi
**validado por sabotagem**: revertendo o fix, ele falha com o mesmo
`ForeignKeyViolation` do bug original — um teste que nunca falhou não protege nada.

**O que falta, e é o que outra pessoa vai continuar:**

1. **Demanda D (Stripe/Pix)** — nada foi feito. Nenhuma linha de Stripe foi
   tocada, nem na Sofia nem no Hamilton. Ver a seção "Demanda D" abaixo, que
   tem o desenho fechado no grilling.
2. **Modelo de avaliação / planilha de qualidade** — os campos novos da
   `Avaliacao` **já existem no banco** e a pesquisa já grava neles, mas o
   **modelo de perguntas ainda vai ser discutido** com o Paulo (quais perguntas
   ficam, o que é texto e o que é estruturado, e como isso se reflete na
   planilha que o time de Qualidade usa). Ver `02-modelo-de-avaliacao.md`.

Os detalhes do que mudou em cada arquivo estão em **"O que foi implementado"**,
no fim deste documento.

---

## Contexto que o documento original errou

Registrado pra ninguém reintroduzir as premissas erradas:

1. **Quem lança primeira consulta e alta/desistência é o TERAPEUTA**, no Hamilton.
   Não é a Thainá.
2. **O time de Qualidade é a Thainá + a Juju.** A Juju mandava a pesquisa na mão
   pelo WhatsApp e **sai dessa função**. A Sofia herda o trabalho da Juju; a
   Thainá supervisiona.
3. **A `Avaliacao` do Hamilton já é criada sozinha**, por signal, nos dois
   gatilhos. A fila de trabalho da Juju **já existe no banco** (`status='pendente'`).
   Não precisamos inventar gatilho: precisamos *responder* os pendentes.
4. **O Stripe "já integrado" é do Hamilton, e o webhook dele está quebrado**
   desde sempre (21 assinaturas, 0 faturas, 64 pagamentos reais que nunca
   entraram). Ver `hamilton-api/docs/pagamentos-cartao-stripe.md`.
5. **A Sofia é o local de trabalho da Thainá.** Por isso pagamento entra na
   Sofia, não no Hamilton.
6. **Dados moram no Hamilton.** A Sofia lê e escreve via API e mostra pra
   Thainá. Sem tabela paralela pro que é dado clínico/cadastral.

---

## Ordem de implementação

Cada passo é entregável e testável sozinho. A ordem respeita dependências reais.

| # | Entrega | Depende de |
|---|---|---|
| 1 | **Demanda A** — captação real + `is_parceria` + `vlr_sessao` + fluxo de prefeitura | — |
| 2 | **Demanda B** — neuro/Amanda + aviso único pós-escalada | — |
| 3 | **Ajuste da tabela `Avaliacao`** (ver `02-modelo-de-avaliacao.md`) | — |
| 4 | **Demanda C** — pesquisa de primeira sessão + pesquisa de encerramento | passo 3 |
| 5 | **Demanda D** — cobrança (Pix + Stripe) | passo 4 |

> ⚠️ O **passo 3 sobe antes do 4**: sem os campos novos, a pesquisa não tem onde
> gravar as respostas. A discussão do modelo de qualidade foi deixada pro fim,
> mas a *implementação* da tabela precede as pesquisas.

---

## Pendências do Paulo (não bloqueiam o código; bloqueiam o deploy)

- [ ] **Base de conhecimento de neuro** — duração, nº de sessões, o que inclui,
      laudo e prazo, pra que serve, idade mínima, online/presencial. Editável em
      `/painel/prompts`, sem deploy. **Sem isso, a Demanda B não pode ir ao ar**
      (a Sofia não teria o que responder e o critério "tira dúvidas pela base"
      fica sem lastro).
- [ ] **Confirmar a captação "Não sei" = ID 4** no Postgres de produção. As
      captações não estão versionadas (sem migration, sem fixture) e o SQLite
      local está vazio — não dá pra confirmar pelo repo.
- [ ] **Nome exato das prefeituras** na tabela `captacoes` (pra data migration
      do `is_parceria`). O comentário do código diz "Prefeitura de Bela Vista de
      Minas" (13) e "Prefeitura de Materlândia" (46). Atenção: **Materlândia/MG**
      (com "r"), não Matelândia/PR — o documento original escreveu errado.
- [ ] **Chave Pix** da Allos (vai pro `/painel/config`).
- [ ] **Credenciais Stripe da Sofia** (chave própria, nas Env Vars do Render).

---

# Demanda A — A Sofia registra a origem real do paciente

> Cresceu em relação ao documento original: não é só "fluxo de prefeitura", é
> **a Sofia passar a registrar de onde o paciente veio**. Prefeitura é um caso
> particular disso.

## A.1 — A captação "WhatsApp (Sofia)" sai

**Problema:** o `PacienteIntakeSerializer` do Hamilton **força**
`fk_captacao = Captacao.get_or_create(nome="WhatsApp (Sofia)")` em todo cadastro
que a Sofia faz (`principais/serializers.py:144,218-231`). Isso está errado: a
Sofia é o **canal**, não a **origem**. Hoje o "como conheceu" que ela coleta vira
texto solto na observação ("Origem: Instagram") e a captação real se perde.

**Decisão:** a Sofia passa a mandar o **ID de captação real do Hamilton**
(Instagram, Facebook, indicação, prefeitura...).

### Como a Sofia escolhe a captação — híbrido (opção C)

1. A Sofia lê as captações ativas em `GET /api/v1/captacoes/` (**já existe**,
   `acessorios/urls.py:21` → `CaptacaoListCreateAPIView`). Verificar se exige
   auth e se filtra `is_active` (hoje é `Captacao.objects.all()`, sem filtro).
2. A lista entra no contexto do LLM; ele casa o que a pessoa disse com uma
   captação ("vim pelo insta" → `Instagram`).
3. **O ID é validado contra a lista antes de sair.** Se o modelo devolver um ID
   que não está na lista, trata como "não identificado". Nunca confiar no ID cru
   do modelo.
4. **Na dúvida, não chuta:** vai a captação **"Não sei" (id 4)** + o texto
   literal do que a pessoa disse, na `observacao`. A Thainá corrige depois.

> **Por que não chutar:** captação errada contamina o relatório de prefeitura e a
> prestação de contas. Captação vazia é só trabalho; captação errada é dado ruim.

> **Implementação:** buscar a captação "Não sei" **por nome, com o ID 4 como
> fallback** — não o contrário. Se o ID estiver errado, o pior caso vira "não
> achei" em vez de "cadastrei todo mundo na captação errada".

### A pergunta de origem continua opcional

A Sofia **não insiste** se a pessoa não responder "como conheceu a Allos".
Aceita em branco → cai em "Não sei" → a Thainá resolve no painel.

## A.2 — `is_parceria` em `Captacao` (Hamilton)

**Problema:** existem **duas fontes de verdade divergentes** pra "isto é
prefeitura":
- `PREFEITURAS_CAPTACAO_IDS = {13, 46}` hardcoded (`principais/views.py:62`),
  duplicado como literal em `views.py:2627`;
- `nome.str.contains('Prefeitura')` no relatório
  (`principais/reports/relatorio_prefeitura_generator.py:186`).

Renomear a captação pra "Bela Vista de Minas (PMBV)" quebraria o relatório
silenciosamente. Cadastrar uma terceira prefeitura entraria no relatório mas
continuaria na cobrança e sem o badge.

**Decisão:** campo **`is_parceria`** (booleano, default `False`) em `Captacao`.
Booleano, não choice — a ideia é cobrir **qualquer órgão parceiro**, não só
prefeitura.

**Migration:**
- `AddField` `is_parceria` em `acessorios.Captacao`
- **Data migration** marcando as parcerias existentes **por nome**
  (`nome__icontains='prefeitura'`), **não por ID** — os IDs 13/46 foram criados à
  mão em produção, não estão versionados, e num banco novo podem ser outra coisa.
- A data migration **não pode quebrar o deploy se não achar nada** (`RunPython`
  tolerante + `reverse_code=noop`). Se der zebra, o Paulo marca na mão no admin.

**Passa a usar `is_parceria` (e o hardcode sai):**
- `views.py:2142-2145` (badge PREFEITURA no dashboard do terapeuta)
- `views.py:2627,2641` (exclusão do dropdown de pagamento manual)
- `relatorio_prefeitura_generator.py:186` (filtro do relatório) — passa a filtrar
  por `is_parceria` em vez de substring no nome

## A.3 — `vlr_sessao` passa a vir da Sofia

**Problema:** `SOFIA_DEFAULT_VLR_SESSAO` (default `"50.00"`,
`principais/serializers.py:156-160`) é o Hamilton decidindo o valor. Está errado
por dois motivos: o valor é **acordado na conversa**, e o número está
desatualizado (a mensalidade hoje é R$ 200, configurada em `/painel/config`).

**Decisão:**
- `vlr_sessao` = **valor acordado da mensalidade**, mandado pela Sofia.
- Valor padrão = **`preco_terapia_mensal`** do `/painel/config` da Sofia
  (hoje 200), não uma env do Hamilton.
- `SOFIA_DEFAULT_VLR_SESSAO` sai (ou vira só fallback se a Sofia não mandar nada).
- **Paciente de parceria → `vlr_sessao = 0`.** Ele não deve nada; a parceria paga
  por fora (R$ 110/consulta realizada, hardcoded em
  `relatorio_prefeitura_generator.py:23`, fora do nosso escopo).

## A.4 — Fluxo de conversa da prefeitura

Hoje a Sofia **escala toda menção a prefeitura** (`prefeitura`), sem explicar
nada. Passa a conduzir o cadastro **sozinha**.

```
pessoa menciona prefeitura / convênio municipal
  ├─ prefeitura conveniada (is_parceria)?
  │    ├─ SIM → "você é funcionário da prefeitura de X?"
  │    │         ├─ SIM → fluxo gratuito completo, cadastro pela Sofia
  │    │         │        captação = a prefeitura, vlr_sessao = 0
  │    │         └─ NÃO → explica que o convênio é pros servidores,
  │    │                  e oferece o fluxo normal (mensalidade)
  │    └─ NÃO → escala `prefeitura` (comportamento de hoje)
```

**O que a Sofia diz no fluxo de prefeitura:** mesmo formato do fluxo padrão
(online, sessões semanais de 50 min, troca de terapeuta sem custo), deixando
claro que o custeio é da prefeitura e **gratuito pro paciente**, **sem mencionar
nenhum valor monetário**. Coleta os mesmos dados numa mensagem só. No handoff,
informa o prazo (terapeuta chama em até 36h) **sem citar pagamento**. Se a pessoa
perguntar se precisa pagar algo, reafirma que não.

### Elegibilidade — decisão consciente do Paulo

O critério é **ser funcionário da prefeitura**, **auto-declarado**. Basta a
pessoa dizer que sim. **O convênio cobre só o servidor, não dependentes.**

> ⚠️ **Registrado, não pra reabrir a discussão:** este é um caminho aberto ao
> público no WhatsApp em que *dizer uma frase* dá atendimento gratuito e coloca
> as sessões da pessoa numa fatura de R$ 110/consulta cobrada da prefeitura.
> Não há porta: nem lista de autorizados, nem encaminhamento, nem matrícula.
> O Paulo foi confrontado com isso e confirmou duas vezes. Fica documentado
> caso apareça uso indevido depois.

**Registro da declaração:** uma linha na `observacao` do Hamilton — ex.:
`"Declarou ser servidor da Prefeitura de Materlândia"`. A captação sozinha não
guarda isso, e essa linha é a única evidência que existiria se a prefeitura
questionar uma consulta na prestação de contas.

## A.5 — Impacto no Hamilton (resumo)

| Arquivo | Mudança |
|---|---|
| `acessorios/models.py` | `Captacao.is_parceria` (bool, default False) |
| `acessorios/migrations/` | AddField + data migration por nome, tolerante a falha |
| `principais/serializers.py` | `PacienteIntakeSerializer` aceita `fk_captacao` (allowlist: só IDs existentes e ativos) e `vlr_sessao` da Sofia; sai o force de "WhatsApp (Sofia)" e do `SOFIA_DEFAULT_VLR_SESSAO` |
| `principais/views.py` | `PREFEITURAS_CAPTACAO_IDS` sai; badge e dropdown passam a usar `is_parceria` |
| `principais/reports/relatorio_prefeitura_generator.py` | filtro por `is_parceria`, não por substring |

---

# Demanda B — Neuro com a Amanda + fim do silêncio pós-escalada

## B.1 — Fluxo de neuro

Hoje a Sofia escala **qualquer** menção a neuro (`neuro_reuniao`) sem falar de
valor. Passa a:

1. Explicar em linguagem simples que a Allos faz avaliação neuropsicológica com
   a equipe, e **convidar pra uma reunião de apresentação com a Amanda,
   coordenadora de neuropsicologia** — sem mencionar valor nesse momento.
2. **Se perguntarem o preço explicitamente** ("quanto custa?", "qual o preço?"):
   informa **R$ 1.000** (não foge da pergunta) e, na sequência, **retoma o
   convite** pra reunião — leva pra próxima etapa em vez de encerrar ali.
3. **Se a pessoa aceitar a reunião:** escala `neuro_reuniao` e informa que a
   **Thainá** retorna com os horários.
4. **Se recusar mesmo depois do valor:** respeita, tira dúvidas pela base de
   conhecimento, e escala `neuro_reuniao` **só** se aparecer algo que ela não
   cobre.

**Primeira menção à Amanda** apresenta ela em meia frase — mesma regra que já
vale pra Thainá no prompt.

### Preço da neuro

- **Default muda de R$ 1.200 → R$ 1.000** (`settings.preco_neuro` /
  `config_negocio.CAMPOS["preco_neuro"]`).
- Continua **editável no `/painel/config`** pela Thainá.
- O token `{{PRECO_NEURO}}` já é injetado no prompt (`llm_client.py:67`) mas o
  prompt v2 não o usa — passa a usar.

### Quem é avisado

**Só a Thainá.** A conversa escalada cai no painel dela como qualquer outra; ela
repassa pra Amanda por fora. **Sem número novo, sem template novo, sem infra
nova.** (Foi considerado avisar a Amanda direto — descartado.)

### Dependência bloqueante

A base de conhecimento **não tem praticamente nada sobre neuro** hoje — só uma
linha dizendo "essa parte é direto com a Thainá"
(`prompt/sofia-base-conhecimento.md:74-75`), porque até agora neuro sempre
escalava. **O Paulo escreve o conteúdo** (ver pendências). Enquanto não existir,
a Sofia escala dúvida específica de neuro — comportamento de hoje, sem regressão.

## B.2 — Aviso único pós-escalada

**Problema:** hoje, quando uma conversa é escalada, ela vira `modo = humano` e a
Sofia **para completamente** (`webhook.py:329-330`). Quem escreve "e aí, alguma
novidade?" **não recebe resposta nenhuma** até a Thainá abrir o painel.

**Decisão:** **uma** resposta automática por escalada. Texto fixo, **sem LLM**
(sem risco de a Sofia retomar o fluxo ou escrever por cima da Thainá). Se a
pessoa mandar mais mensagens depois disso, a Sofia fica muda.

> "não fique repetitivo" — Paulo

**Vale pra toda escalada**, não só neuro.

**Implementação:** coluna nova em `conversa` (ex.: `aviso_escalada_enviado_em`,
`DateTime | None`), no mesmo espírito do `seguimento_enviado_em` que já existe.
NULL = ainda não avisou. Resetada quando a conversa volta pro bot.

---

# Demanda C — Pesquisas de satisfação (primeira sessão e encerramento)

> Herda o trabalho que a Juju fazia na mão. O modelo de perguntas dela está em
> `03-questionario-atual-da-qualidade.md`; o mapeamento pros campos do Hamilton está em
> `02-modelo-de-avaliacao.md`.

## C.1 — Como a Sofia descobre que tem pesquisa pra mandar (polling)

**O Hamilton não empurra nada.** Decisão revisada durante o grilling: a intenção
inicial era webhook (Hamilton → Sofia), mas isso caiu quando descobrimos que a
`Avaliacao` pendente **já é o registro do evento**.

**Por que não webhook:** o Hamilton **não tem Celery, worker nem fila** — é 100%
request/response síncrono (`Procfile`: uma linha, gunicorn). Uma chamada HTTP no
`post_save` rodaria **dentro do request do terapeuta salvando o prontuário**, e a
Sofia está no Render free (hiberna, ~50s pra acordar). Pior: o
`ConsultaCreateView` cria N consultas num loop dentro de `transaction.atomic()` —
seriam N chamadas HTTP dentro de uma transação aberta.

**Desenho escolhido (polling):**

1. Os signals do Hamilton continuam **exatamente como estão** — já criam a
   `Avaliacao` com `status='pendente'` (`principais/signals.py:54-81`).
2. A Sofia, no **cron que já existe** (`POST /tasks/seguimentos`, protegido por
   `TASKS_TOKEN`), ganha um job novo que pergunta ao Hamilton quais avaliações
   estão pendentes e ainda não foram enviadas.
3. A Sofia dispara as pesquisas.

**Vantagens:** zero risco no caminho crítico do terapeuta; **retry de graça** (o
pendente continua lá até ser respondido); reaproveita infra que já está no ar.

**Custo aceito:** a pesquisa sai no próximo tick do cron, não instantaneamente.
Irrelevante aqui — o terapeuta pode lançar o prontuário dias depois da sessão, e
às vezes de madrugada.

**Sem atraso proposital:** dispara no primeiro tick após o lançamento.

### Endpoints novos no Hamilton

- `GET /api/v1/avaliacoes/pendentes/` — lista as `Avaliacao` com
  `status='pendente'`, com o que a Sofia precisa pra montar a conversa: paciente
  (nome, telefone), `momento`, e — pro caso de encerramento — o `cancelador` e o
  `alta_desistencia` da `Altadesistencia` vinculada.
- `PATCH /api/v1/avaliacoes/<pk>/` — grava as respostas e muda o `status`.

Autenticação: mesmo JWT que a Sofia já usa.

> ⚠️ **Nota de segurança (fora do escopo, mas registrada):** as 4 rotas atuais da
> Sofia usam só `IsAuthenticated` — qualquer usuário logado do Hamilton pode
> criar/ler/alterar qualquer paciente por elas, e o token vale 1 dia. Os
> endpoints novos vão expor **respostas de pesquisa de saúde**. Vale restringir
> por grupo/permissão, não só "está logado".

## C.2 — Como a pesquisa é conduzida

**LLM conduzindo, com a lista de perguntas no prompt.** Sem máquina de estado,
sem tool de registro.

**Decisão consciente do Paulo, após ser confrontado duas vezes com o risco.**

> ⚠️ **Risco aceito, documentado:** sem máquina de estado e sem tool, **nada
> garante** que as 11 perguntas sejam feitas nem que as respostas sejam extraídas
> corretamente. Diferente do P0, **o erro aqui é invisível** — ninguém confere, e
> as notas vão pro `Avaliacao` embasando decisão do time de Qualidade. Foram
> oferecidas duas alternativas mais seguras (máquina de estado híbrida; e LLM
> livre + `registrar_resposta_pesquisa` por tool, praticamente o mesmo custo de
> implementação) e ambas foram recusadas. **Se aparecer dado incompleto ou
> impreciso no `Avaliacao`, esta é a causa** — e o conserto é adicionar a tool.

**Consequências no comportamento:**
- A Sofia **não se reapresenta** — a conversa é continuidade do mesmo atendimento.
- Tom e formato de escrita já usados por ela (sem emoji, frases curtas).
- Resposta fora do formato (ex.: nota fora de 0-10): pede de novo com gentileza
  **uma única vez**; se persistir, registra "não informado" e segue.
- A Sofia **não comenta nem interpreta clinicamente** as respostas.

## C.3 — Extração e gravação

**No fim da pesquisa, uma chamada ao LLM** com a transcrição, pedindo o JSON
estruturado das respostas → a Sofia faz o `PATCH` na `Avaliacao`.

(A alternativa — a Thainá ler e preencher à mão — foi descartada: transferiria
pra ela exatamente o trabalho manual que se quer tirar da Juju.)

## C.4 — Prazos e encerramento

| Evento | Quando |
|---|---|
| Convite | primeiro tick do cron após o lançamento |
| **Lembrete único** | **20h** sem resposta |
| **Encerra como não respondido** | **44h** sem resposta |

Funciona dentro da janela de 24h da Meta: o lembrete de 20h sai como texto livre;
se a pessoa responder, a janela reabre; se não, o encerramento em 44h é só
marcação interna (não precisa mandar nada).

**Recusa e silêncio caem no mesmo `status='nao_respondeu'`.** O choice já existe
no Hamilton; não se cria campo pra distinguir.

**Sem insistência** além do lembrete único.

## C.5 — Pesquisa de primeira sessão (era "Demanda 3")

**Gatilho:** `Avaliacao` pendente com `momento='No início do processo (primeira
sessão)'` — criada pelo signal quando o terapeuta lança a primeira consulta.

**Perguntas:** as do `03-questionario-atual-da-qualidade.md` (bloco de acompanhamento) **+ uma
nova, de 0 a 10, sobre a qualidade do acolhimento e encaminhamento feito pela
própria Sofia**.

Cobertura: bem-estar individual, satisfação interpessoal, comunicação social,
estado geral de bem-estar, nota do terapeuta, nota de indicação da Allos, data da
última sessão, feedback livre, rapidez do atendimento, indicação, **nota da
Sofia**.

**Pede consentimento antes de começar** (`Avaliacao.consentimento_paciente`).

## C.6 — Pesquisa de encerramento (era "Demanda 5")

**Gatilho:** `Avaliacao` pendente com `momento='Após o encerramento da terapia'` —
criada pelo signal quando o terapeuta lança a `Altadesistencia`.

**Todos os quatro casos recebem a pesquisa.** O que muda é **a comunicação**.
A Sofia lê `alta_desistencia` e `cancelador` do Hamilton e adapta a fala:

| `alta_desistencia` | Como a Sofia fala |
|---|---|
| `alta` | O processo terminou bem. **Não** pergunta "por que interrompeu". |
| `desistencia` | Pergunta o motivo, como no modelo da Juju. |
| `não responde` | Vai mesmo assim. Se não responder, cai em `nao_respondeu` naturalmente. |
| `Solicitação de reencaminhamento` | **Deixa claro que a pessoa continua na Allos e vai trocar de terapeuta.** Pergunta sobre a experiência com o **terapeuta anterior**. |

**Se `cancelador = 'terapeuta'`:** não pergunta "por que **você** decidiu
interromper" — não foi ela quem decidiu.

**No reencaminhamento**, a Sofia confirma a troca e avisa que **a Thainá entra em
contato pra combinar e tirar dúvidas** — **sem prometer prazo específico**
(diferente do cadastro, que promete 36h).

**Se a pessoa emendar "e quando começo com o novo?"** → **escala pra Thainá**.

> Contexto técnico: o signal `processar_alta_ou_desistencia`
> (`principais/signals.py:11-35`) já move o paciente pro terapeuta "aguardando"
> (ID **73 hardcoded**) e marca `REENCAMINHADO` + `PAUSADO`. Se o terapeuta 73
> não existir, o signal **falha silenciosamente** (só imprime alerta). Fora do
> nosso escopo, mas é frágil.

**Nenhuma tentativa de reverter a saída ou "reter" o paciente** durante a
pesquisa.

---

# Demanda D — Cobrança (Pix + Stripe)

> **Última entrega da sequência**, por ser a mais complexa (decisão do Paulo).
>
> ❌ **NÃO INICIADA.** Nenhuma linha de Stripe foi escrita, nem na Sofia nem no
> Hamilton. O que está abaixo é o desenho fechado no grilling, não código
> existente. Quem pegar esta demanda começa do zero — e o desenho já está
> decidido, não precisa ser rediscutido.

## D.0 — Discutir junto: modelo de avaliação e planilha de qualidade

Esta demanda é o momento de fechar **também** o que ficou em aberto sobre
qualidade, porque as duas coisas acontecem no mesmo ponto da jornada (a
cobrança é encadeada no fim da pesquisa):

- **O modelo de avaliação** — quais perguntas ficam no questionário definitivo,
  quais viram campo estruturado e quais ficam como texto livre. Os campos já
  existem no banco (ver `02-modelo-de-avaliacao.md`), mas o **conteúdo** da
  pesquisa ainda vai ser revisto com o Paulo.
- **A planilha de avaliação** que o time de Qualidade usa hoje — precisa ser
  editada pra refletir o modelo novo e o fato de que quem coleta agora é a
  Sofia, não uma pessoa. Como as respostas passam a viver na `Avaliacao` do
  Hamilton, é preciso decidir se a planilha vira um relatório/export a partir de
  lá ou se continua existindo em paralelo.

**Fechar isso antes de codar a cobrança**, porque a transição da pesquisa pra
cobrança (D.1) depende de saber onde a pesquisa termina.

## D.1 — Gatilho

**Automático, encadeado na pesquisa de primeira sessão** (C.5): quando a pesquisa
termina — respondida, recusada ou expirada — a Sofia manda a cobrança.

**Com transição adequada.** A Sofia **não** emenda "obrigada pelas respostas"
direto em "agora paga". Ela fecha a pesquisa, agradece, e só então abre o assunto
da mensalidade como próximo passo natural (garantir a vaga).

**Paciente de parceria nunca recebe cobrança.** Com o `is_parceria` da Demanda A,
a Sofia sabe disso sozinha.

## D.2 — O que a Sofia manda

- **Chave Pix fixa** — **editável no `/painel/config`** (junto de preço e
  parcelas). É o tipo de coisa que muda sem deploy.
- **Link de pagamento no cartão**, gerado por paciente.
- **O valor destacado sozinho na linha** (regra de legibilidade que o prompt já
  tem).
- **Nenhuma menção a parcelamento.** Linguagem sem tom de cobrança agressiva.

## D.3 — Stripe: a Sofia fala direto (opção B)

**Chave própria da Sofia**, sem passar pelo Hamilton.

> ⚠️ **Consequência registrada, decidida conscientemente:** a Allos passa a ter
> **duas integrações Stripe** (Hamilton e Sofia). As assinaturas criadas pela
> Sofia **não aparecerão no Hamilton**, que é onde a contabilidade e a emissão de
> NFS-e moram. Se depois se quiser unificar, o caminho é fazer a Sofia chamar
> `stripe/gerar-link/<paciente_id>/` do Hamilton — mas isso exigiria **consertar
> o webhook quebrado** do Hamilton primeiro.
>
> **O webhook `invoice.paid` do Hamilton está quebrado desde sempre** (a API do
> Stripe moveu `invoice.subscription` → `invoice.parent.subscription_details.subscription`
> em 2025-03-31): 21 assinaturas, 0 faturas registradas, 64 pagamentos reais que
> nunca entraram. Ver `hamilton-api/docs/pagamentos-cartao-stripe.md`. **Fora do
> nosso escopo, mas continua lá.**

## D.4 — Comprovante

A chave Pix é **fixa** — sem valor embutido, sem identificação de quem pagou.
O comprovante é o único jeito de saber quem pagou.

**Decisão: comprovante → escala pra Thainá.** Mantém o comportamento atual de
anexo (`anexo_recebido`): a Sofia guarda a imagem, escala, e a **Thainá confere e
confirma**.

**A Sofia NÃO confirma a vaga automaticamente** ao receber uma imagem. Confirmar
"sua vaga está garantida" ao receber uma foto que ela não leu é arriscado — pode
ser qualquer imagem, ou um comprovante de R$ 5.

> Isso diverge do critério original ("quando o comprovante for recebido, a Sofia
> confirma o recebimento e informa que a vaga está garantida"). Decisão do Paulo.

## D.5 — Objeções

| Situação | Ação |
|---|---|
| "achei caro", quer negociar | escala `preco` |
| "não posso pagar" | escala `gratuidade` |
| não respondeu no prazo | **um único lembrete**, sem pressão |

---

## Requisitos não-funcionais (valem pra tudo)

- **Tom e regras de escrita** já existentes no prompt da Sofia (sem emoji, sem
  travessão, frases curtas, bolhas).
- **LGPD:** nada de conteúdo de mensagem em log (dado de saúde sensível). As
  respostas de pesquisa são dado sensível — não logar valores, só metadados.
  Telefone em log passa por `utils.mascarar_telefone`.
- **A Sofia não interpreta clinicamente** nada, em nenhuma pesquisa.
- **Toda fala do bot continua passando por `saida.limpar()`** — o choke point do
  P0 vale pras pesquisas e pra cobrança também.

---

# O que foi implementado (06/08/2026)

## Hamilton (`hamilton-api`)

| Arquivo | Mudança |
|---|---|
| `acessorios/models.py` | `Captacao.is_parceria` (bool, default False) |
| `acessorios/migrations/0002_captacao_is_parceria.py` | AddField + data migration marcando as parcerias **por nome** (`nome__icontains='prefeitura'`), tolerante a não achar nada |
| `principais/models.py` | 12 campos novos em `Avaliacao`: respostas da pesquisa + `sofia_enviada_em` / `sofia_lembrete_em` |
| `principais/migrations/0005_avaliacao_respostas_pesquisa.py` | os 12 campos acima |
| `principais/serializers.py` | intake aceita `fk_captacao` e `vlr_sessao`; expõe `is_parceria`; novos `AvaliacaoPendenteSerializer` e `AvaliacaoRespostasSerializer` |
| `principais/views.py` | hardcode `{13,46}` removido; `AvaliacaoPendenteListAPIView` e `AvaliacaoRespostasAPIView` |
| `principais/urls.py` | `avaliacoes/pendentes/` e `avaliacoes/<pk>/` |
| `principais/signals.py` | **bug corrigido** (ver abaixo) |
| `principais/reports/relatorio_prefeitura_generator.py` | filtro por `is_parceria`; fallback perigoso removido |
| `principais/tests_sofia_api.py` | 19 testes (captação, valor, parceria, fila de avaliações, PATCH) |

## Sofia (`sofia`)

| Arquivo | Mudança |
|---|---|
| `app/services/captacao.py` | **novo** — lista de origens do Hamilton, cache, validação do ID |
| `app/services/pesquisa.py` | **novo** — ciclo completo da pesquisa (polling, condução, extração, prazos) |
| `app/services/hamilton_client.py` | `listar_captacoes`, `avaliacoes_pendentes`, `atualizar_avaliacao`; `mapear_dados` manda captação e valor |
| `app/services/llm_client.py` | `gerar_resposta` aceita `captacoes` e `system_prompt`; injeta `{{LISTA_CAPTACOES}}` |
| `app/services/tools.py` | `captacao_id`, `is_parceria`, `vinculo_parceria` em `cadastrar_paciente` |
| `app/services/escalation.py` | `AVISO_EM_ATENDIMENTO` |
| `app/services/config_prompt.py` | 4 prompts novos da pesquisa, editáveis no painel |
| `app/services/saida.py` | filtra marcadores internos `[[...]]` (ver bug 3) |
| `app/services/seguimento.py` | não manda follow-up de lead pra quem está em pesquisa |
| `app/services/painel.py` | devolver ao bot zera `aviso_escalada_em` |
| `app/routers/webhook.py` | valida a captação; aviso pós-escalada; turno em modo pesquisa |
| `app/routers/tasks.py` | `POST /tasks/pesquisas` |
| `app/models.py` | `aviso_escalada_em`, `pesquisa_avaliacao_id`, `pesquisa_iniciada_em` |
| `alembic/versions/b1c2d3e4f5a6_*.py` | as 3 colunas acima |
| `app/config.py` | `preco_neuro` 1200 → **1000** |
| `prompt/sofia_v01.txt` | seções "Prefeitura e convênios", "Avaliação neuropsicológica", "De onde a pessoa veio" |
| `prompt/sofia-base-conhecimento.md` | entradas de neuro e prefeitura reescritas |
| `prompt/pesquisa-*.{txt,md}` | **novos** — condução, roteiro da 1ª sessão, roteiro de encerramento, extração |

## Bugs encontrados e corrigidos no caminho

**1. Signals do Hamilton gravavam no banco errado** (`principais/signals.py`).
Os quatro receivers usavam `Model.objects.create(...)` / `.save()` sem `using=`,
então gravavam sempre no banco `default` — mesmo quando a `Consulta` ou a
`Altadesistencia` que os disparou tinha sido criada no alias `sofia_api`
(`SOFIA_API_DATABASE_URL`). Resultado: `ForeignKeyViolation` ao criar a
`Avaliacao`, e o paciente não era atualizado. **Pré-existente**, só ficou visível
porque a pesquisa depende dessas avaliações. Corrigido: cada signal grava no
banco da instância que o disparou (`instance._state.db`).

**2. `PacienteIntakeSerializer` quebrava ao devolver a captação.** Minha primeira
versão declarava `fk_captacao` como `IntegerField` e resolvia o objeto no
`validate_`, o que estourava `TypeError` na serialização de saída. Trocado por
`PrimaryKeyRelatedField` com queryset resolvido em `get_fields()` (respeita o
alias do banco e já é a allowlist).

**3. Marcador interno da pesquisa podia chegar ao paciente.** A pesquisa usa
`[[PESQUISA_CONCLUIDA]]` / `[[PESQUISA_RECUSADA]]` pra sinalizar o fim. Eles são
removidos antes do envio, mas se o modelo colocasse um no meio da frase o texto
sairia para o paciente. Adicionado ao filtro de tokens internos do
`saida.limpar()` — o mesmo choke point do P0. Validado que não corta fala
legítima com colchetes ("Ele disse [isso aqui]").

**4. Fallback do relatório de prefeitura vazava a base inteira.** Se as tabelas
de captação/pacientes viessem vazias, o gerador usava **todas as consultas do
período** ("⚠️ Usando todas as consultas"), o que colocaria pacientes de toda a
Allos num relatório enviado a uma prefeitura. Trocado por relatório vazio: um
resultado obviamente errado é melhor que um vazamento silencioso.

## Riscos aceitos (decisões conscientes, registradas)

1. **Elegibilidade de parceria é auto-declarada.** Dizer "sou funcionário da
   prefeitura X" no WhatsApp dá atendimento gratuito e coloca as sessões da
   pessoa numa fatura cobrada daquela prefeitura. Não há lista de autorizados,
   encaminhamento nem matrícula. A declaração fica registrada na `observacao`.
2. **A pesquisa é conduzida e extraída por LLM, sem tool de registro.** Nada
   garante que todas as perguntas sejam feitas nem que cada resposta seja lida
   corretamente, e **ninguém confere depois**. Há validação defensiva na
   extração (nota fora de 0-10 é descartada, campo inventado é ignorado, data
   ilegível é omitida), mas ela não detecta uma nota lida errado. Se aparecer
   dado ruim na `Avaliacao`, o conserto é registrar cada resposta por tool.
3. **Duas integrações Stripe** (quando a Demanda D for feita): as assinaturas
   criadas pela Sofia não aparecerão no Hamilton, onde a contabilidade e a
   NFS-e moram.

## Pendências que bloqueiam o deploy

- [ ] ⚠️ **AS MIGRATIONS DO HAMILTON NÃO VÃO PRO GIT.** O `.gitignore` do
      `hamilton-api` ignora `**/migrations/**` (linha 23). As duas que este ciclo
      criou existem **só no disco local**:
      - `acessorios/migrations/0002_captacao_is_parceria.py`
      - `principais/migrations/0005_avaliacao_respostas_pesquisa.py`

      O `build.sh` roda `migrate` no deploy, mas sem os arquivos versionados ele
      não tem o que aplicar — **as colunas novas nunca chegariam à produção** e
      tanto a Demanda A quanto a C quebrariam com erro de coluna inexistente.

      Três saídas: (1) commitar as duas com `git add -f`; (2) tirar
      `**/migrations/**` do `.gitignore` (o correto a longo prazo — migration é
      código, não artefato); ou (3) gerar de novo no ambiente de deploy com
      `makemigrations`. **Decidir antes de subir**, e é decisão do dono do repo.
- [ ] **Confirmar a captação "Não sei" no Postgres de produção.** O código a
      busca **por nome** (`get_or_create(nome="Não sei")`), então ela é criada
      sozinha se não existir — mas vale conferir se o nome bate com o registro
      que já está lá (o ID 4 mencionado no grilling não pôde ser verificado: as
      captações não estão versionadas e o SQLite local está vazio).
- [ ] **Rodar as migrations** nos dois sistemas (`alembic upgrade head` na
      Sofia; `migrate` no Hamilton, que roda sozinho no build).
- [ ] **Conferir a data migration do `is_parceria`.** Ela marca por
      `nome__icontains='prefeitura'`. Se não casar, o log avisa e nada quebra —
      aí é só marcar as duas no admin.
- [ ] **Base de conhecimento de neuro** — duração, nº de sessões, o que inclui,
      laudo e prazo, idade mínima, online/presencial. Editável em
      `/painel/prompts`, sem deploy. Sem isso a Sofia escala dúvida específica de
      neuro (comportamento de hoje, sem regressão).
- [ ] **Cron do `POST /tasks/pesquisas`** — mesmo `TASKS_TOKEN` do
      `/tasks/seguimentos`, no cron-job.org. Sem o cron, nenhuma pesquisa sai.

## Ponto em aberto que ninguém decidiu ainda

**Paciente que não veio pela Sofia não recebe pesquisa.** O
`pesquisa._conversa_do_paciente` procura a conversa por `paciente_hamilton_id` e,
se não achar, **por telefone** — o que já cobre quem conversou com a Sofia mas
foi cadastrado à mão. Mas quem **nunca** falou com a Sofia não tem conversa
nenhuma, e fora da janela de 24h da Meta não dá pra iniciar uma com texto livre
(exigiria um template aprovado, que demora). Esses são pulados em silêncio.

Como a Sofia é recente, **isso provavelmente é a maioria dos pacientes ativos
hoje**. Precisa ser decidido: template aprovado na Meta, ou esses ficam com a
equipe?
