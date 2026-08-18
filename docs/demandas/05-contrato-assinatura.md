# Demanda E — Contrato terapêutico assinado pelo paciente (Autentique)

**Data do fechamento do desenho:** 17/08/2026.
**Status:** desenho fechado em grilling, nenhuma linha escrita ainda.
**Mexe nos dois repos:** o grosso é no `hamilton-api`; a Sofia entrega e conversa.

> Este documento é o **registro de decisão**. As alternativas descartadas estão
> aqui de propósito: o custo de redescobrir por que não fizemos de outro jeito é
> maior que o custo de ler.

---

## O que é

Depois da primeira sessão, no mesmo turno em que a Sofia cobra a mensalidade,
ela também manda o **contrato terapêutico pra assinatura eletrônica**. O paciente
abre um link, confirma CPF e data de nascimento na página da Autentique, assina,
e o contrato assinado volta pro prontuário — junto com o CPF, que hoje nasce
vazio em todo lead da Sofia.

---

## Parte 1 — Troca do modelo (independente, sai primeiro)

Não tem nada a ver com contrato; entrou no mesmo ciclo e destrava a app local.

- `OPENAI_MODEL` = **`gpt-5.6-terra`** ($2 in / $12 out por 1M, contexto 1,05M,
  function calling). O `gpt-5.6-sol` (o alias `gpt-5.6` aponta pra ele) custa
  $5/$30 e foi **descartado**: o turno da Sofia é seguir roteiro e escolher tool,
  não raciocínio pesado.
- **`reasoning_effort`**: `none` no turno da conversa, `low` na extração da
  pesquisa. O default do 5.6 é `medium`, que adiciona segundos **em todo turno** e
  cobra os tokens de raciocínio como saída. Editável em `/painel/config` pra dar
  pra medir com o `laboratorio/` sem redeploy.
- **`OPENAI_TEMPERATURE` vazio.** Hoje é 0.7 e o 5.6 provavelmente rejeita — cairia
  no fallback silencioso do `llm_client._criar`. Melhor não depender disso.

### 🔴 `reasoning_effort="none"` NÃO é otimização — é obrigatório

Descoberto rodando o laboratório em 17/08. Com **function calling**, o
`gpt-5.6-terra` em `/v1/chat/completions` recusa qualquer esforço que não seja
`none`:

```
400 — Function tools with reasoning_effort are not supported for gpt-5.6-terra
in /v1/chat/completions. To use function tools, use /v1/responses or set
reasoning_effort to 'none'.
```

Como **todo** turno da Sofia manda `tools`, sem `none` ela não responde nada: cai
no texto de fallback ("tive um probleminha técnico") em 100% das mensagens.

**Duas armadilhas em volta disso:**

1. **O fallback genérico do `_criar` piorava o problema.** A mensagem de erro cita
   `reasoning_effort`, então a regra "removeu o parâmetro reclamado, reenvia"
   **removia** o parâmetro — e sem ele vale o padrão (`medium`), que é justamente
   o que a API recusou. O conserto automático deixaria a Sofia muda. Agora há um
   caso especial: quando o erro pede `'none'`, ele **força** `none` em vez de
   remover.
2. **O laboratório montava o `OpenAIClient` sem passar o `esforco`.** Resultado:
   9 conversas de fallback, zero tool calls, o modelo da Sofia sem aparecer no
   consumo — e um `resumo.md` completo, com 6 desistências, que parecia um
   resultado ruim de prompt. **Um relatório inteiro de dado falso.** Corrigido em
   `laboratorio/conversa.py`.

> Se um dia a Sofia migrar pra `/v1/responses`, esta restrição some e o esforço
> volta a ser uma escolha de custo/latência. Hoje não é.

### 🔴 Bug encontrado: o `.env` derruba a app local

`API_AUTHENTIC` foi adicionado ao `.env` sem ser declarado no `Settings`, e o
pydantic-settings recusa chave desconhecida em arquivo de env (`extra_forbidden`).
**`pytest` não coleta e o `uvicorn` não sobe** neste momento. Produção não é
afetada (lá são env vars do Render, e essas o pydantic só lê pros campos
declarados). Correção: declarar o campo. Quando o token migrar pro Hamilton, a
chave sai da Sofia.

---

## Parte 2 — O contrato (texto)

O contrato é **editado em `/painel/prompts`**, como os prompts da Sofia, e o
padrão de fábrica é `prompt/contrato-terapeutico-allos.md`. O Hamilton recebe o
texto no corpo da requisição e gera o `.docx` com `python-docx`. **Não é PDF
gerado por código, nem `.docx` guardado no repo** — ver "Alternativas descartadas".

> 🔁 **Isto mudou depois de uma rodada extra de grilling (17/08).** O desenho
> original era um `.docx` versionado no repo do Hamilton, editado no Word. O
> pedido de "deixar o contrato editável no painel, igual aos prompts" derrubou
> isso — e para melhor: **matou o risco nº 2** (duas cópias do contrato
> divergindo), porque agora existe uma fonte só. O custo aceito é que o Paulo
> perde o controle de alterações do Word para revisar; em troca, ganha o botão de
> **prévia**, que gera o documento com dados fictícios pra ele ler no formato de
> sempre.

### As cinco mudanças de texto (Paulo aprova antes do primeiro contrato real)

1. **Qualificação de terapeuta e supervisor sai.** Os blocos `[ter_nome]`,
   `[ter_cpf]`, `[sup_nome]`, `[sup_crp]`, `[sup_cpf]` deixam de existir. Motivo
   duplo: **LGPD** (CPF de terapeuta e de decano indo pro WhatsApp de paciente) e
   **independência do match** (esses dados só existem depois que a coordenação
   escolhe o terapeuta).
   ⚠️ **As cláusulas 1.2 e 1.3 ficam**, em termos genéricos ("terapeuta vinculado
   à CONTRATADA, sob supervisão de psicólogo(a) com registro ativo"). Elas são o
   consentimento informado sobre o modelo da Allos — tirá-las não simplificaria o
   contrato, removeria a prova de que a Allos avisou.
   **Consequência:** o campo de CRP que ia ser criado no `Associado` do Hamilton
   **não será criado**. A decisão foi revogada.
2. **CPF do paciente sai do corpo** → "informado no ato da assinatura eletrônica,
   conforme manifesto anexo". **Nacionalidade e profissão saem de vez** — não têm
   função jurídica ali, são praxe de cartório.
3. **Cláusula 1.4 sem horário concreto**: "dia e horário fixo semanal, definidos
   com o(a) terapeuta e reservados exclusivamente ao(à) CONTRATANTE". O marcador
   `[srv_horario]` some.
   ⚠️ **Manter a cláusula (sem o dado) e não apagá-la** foi decisão consciente: as
   cláusulas 3.4, 4.2, 4.3, 6.2 e 7.3 todas se apoiam em "horário reservado". Apagar
   a 1.4 obrigaria a reescrever cinco cláusulas que sustentam a cobrança da
   mensalidade.
4. **Cláusula 3.1 só com a mensalidade.** O "correspondente a R$ X por sessão" sai,
   porque **contradiz a 3.5** ("não se confunde com pagamento avulso por sessão") no
   mesmo documento. Essa contradição é o gancho exato pra "faltei duas sessões, me
   devolve R$ 100".
5. **Cláusula 3.3: pro rata → mensalidade cheia.** Hoje o contrato promete
   "pagamento de entrada proporcional (pro rata)" e o sistema cobra a mensalidade
   cheia (decisão registrada no `CLAUDE.md`, seção de cobrança). **Assinado, isso
   vira um documento em que a Allos promete uma coisa e cobra outra, e o paciente
   tem a prova.** O texto muda pra refletir a cobrança real — não o contrário.

### Escopo de quem recebe

**Escopo 1: adulto, terapia, particular, online, paciente novo.** Não recebem
contrato pela Sofia (viram alerta pra Thainá):

| quem | por quê |
|---|---|
| Parceria / prefeitura | paga R$ 0; as cláusulas 3.x não fazem sentido |
| Menor de idade | quem assina é o responsável, que é outra pessoa e outro número |
| Neuroavaliação | não é mensalidade, é pacote com a Amanda |
| Presencial | a cláusula 2 inteira é sobre Google Meet |
| Paciente antigo | o `vlr_sessao` deles é heterogêneo (ver abaixo) |

⚠️ **A trava do valor.** No banco de teste, `vlr_sessao` ("Valor Acordado") tem
R$ 200 (160 pacientes), R$ 0 (108, parceria), R$ 100 (50), R$ 50 (26), R$ 60 (25).
O campo carrega **dois significados** — mensalidade nos novos, valor por sessão
nos antigos. Contrato com valor 0 ou fora da faixa esperada **não é gerado**:
alerta. Sem isso, um contrato de "mensalidade de R$ 50" sai por acidente.

---

## Parte 3 — O que a Autentique faz (e o que não faz)

Verificado com spikes reais em **sandbox** (documentos criados e apagados).

**Faz:**
- `createDocument` via multipart, aceita **PDF e DOCX** (os dois testados).
- Signatário por `name` → devolve `link.short_link` (`https://assina.ae/xxxxx`),
  **sem notificar ninguém** — é a gente que entrega.
- Colhe **CPF e data de nascimento no ato** (flags `ignore_cpf`/`ignore_birthdate`
  existem justamente pra desligar isso). É o único dado que o fluxo devolve.
- Registra IP de visualização e de assinatura, timestamps, hash, e anexa um
  manifesto ao PDF assinado. Devolve `files { original signed pades }`.
- Webhooks com HMAC no header `x-autentique-signature`, eventos
  `signature.accepted` / `signature.rejected` / `document.finished`.
- `sandbox: true` não consome crédito e o documento se apaga em poucos dias.

**Não faz:**
- 🔴 **Não coleta dado personalizado.** O `SignerInput` inteiro é `name, email,
  phone, delivery_method, positions, action, type, configs, variable_id,
  security_verifications` — e `configs` só tem `cpf`. Não há campo livre nem
  formulário. Coletar endereço ou contato de apoio por ali **não é possível**
  (é pedido aberto no board de feedback deles).
- ⚠️ O `geolocation.zipcode` que ela devolve é **derivado de IP** — acerta cidade
  e erra o CEP. **Não usar pra nota fiscal**: não é dado faltando, é dado errado.

### ⚠️ Duas armadilhas descobertas no spike

1. **`signatures[0]` é a conta dona, não o paciente.** A Autentique adiciona o
   dono da conta como primeira entrada do array. Código ingênuo lendo o índice 0
   pega a pessoa errada. **Filtre sempre pelo `public_id` que você guardou.**
2. **O `link.short_link` volta nulo na resposta do `createDocument`** — ele
   aparece na query `document` depois. (Existe a mutation `createLinkToSignature`,
   mas ela falha com `without_action_in_document` se chamada com o `public_id` do
   dono.)

### A conta

Hoje o token é de uma conta **pessoal**: `victorabdallah6@gmail.com`, 0
documentos. Volume previsto: **~20 contratos/mês**. Migrar pra conta
institucional da Allos depois do período de testes — contrato da Associação
emitido de conta pessoal significa que o acervo e a cobrança do plano ficam
atrelados a uma pessoa.

---

## Parte 4 — Arquitetura

### Onde mora cada coisa

| Hamilton (`hamilton-api`) | Sofia (este repo) |
|---|---|
| Renderiza texto → `.docx` (`python-docx`) | **Dono do texto** (`/painel/prompts`) |
| Cliente da Autentique (sandbox-aware) | Chama o Hamilton no turno da cobrança |
| `ContratoPaciente` (histórico + PDF em bytes) | Encurta o `assina.ae` → `allos.org.br/p` |
| `POST`/`GET`/`previa`/`webhook` em `/api/v1/contratos/` | Injeta o estado do contrato no prompt |
| Webhook HMAC | Guardas de quem não recebe |
| Grava `Paciente.cpf` e `vlr_sessao` | Linha na tela **Hoje** (prioridade 12) |
| — | Flag `contrato_ativo` (nasce desligada) |

**Por que o grosso é no Hamilton:** é onde moram o prontuário, a contabilidade e
o storage do documento assinado. **Por que a entrega é na Sofia:** o encurtador
(`link_curto` + `/l/{slug}` + o `/p/[codigo]` do site) já existe aqui, e o link
sai como `assina.ae/xxxxx` — tão "cara de golpe" quanto o `buy.stripe.com` que já
foi resolvido. O Hamilton devolve o link cru; a Sofia encurta e manda.

**O token `API_AUTHENTIC` migra pro ambiente do Hamilton.**

### O fluxo

Endpoints do Hamilton (`/api/v1/contratos/`): `POST` gera (idempotente), `GET`
consulta um paciente, `pendentes/` lista em lote (é o que a tela Hoje usa — uma
requisição por conversa faria o painel disparar dezenas a cada polling),
`previa/` renderiza com dados fictícios sem tocar na Autentique (é o botão **"Ver
como fica"** em `/painel/prompts`), e `webhook/` recebe a Autentique com HMAC.

```
1ª sessão realizada
   → pesquisa de satisfação (como já é hoje)
   → pesquisa.finalizar → cobranca.encadear
   → TURNO DE COBRANÇA DA SOFIA:
        POST /api/v1/contratos/ {paciente_id, valor_mensal}   (idempotente)
        ← {contrato_id, link_assinatura, status}
        Sofia encurta o link e manda CONTRATO + PAGAMENTO na mesma mensagem
   → paciente assina na Autentique
   → webhook no Hamilton (HMAC) → baixa o PDF assinado, grava bytes,
        grava Paciente.cpf, marca assinado_em
```

**Contrato e pagamento saem juntos, na mesma mensagem** — são a mesma decisão pro
paciente (fechar). Separar em duas etapas de 44h cada empilharia ~4 dias de
abordagem depois de uma única sessão. A pesquisa continua antes e sozinha (ela
não fala de dinheiro).

**A assinatura não bloqueia atendimento.** O "resolvido" da cobrança continua
sendo o pagamento. Quem pagou e não assinou vira pendência da Thainá. Bloquear
sessão por assinatura pendente transformaria uma formalidade em briga.

### Decisões de implementação

- **Assinatura simples**, sem biometria. `refusable: true` (recusa explícita é
  informação, e é diferente de sumiço), `deadline_at` de 30 dias (não deixar link
  de contrato vivo pra sempre no WhatsApp de alguém — mesmo motivo do
  `completed_sessions.limit=1` dos Payment Links).
- **Só o paciente assina.** Contra-assinatura da Allos foi descartada: viraria um
  passo humano em cada um dos 20 contratos/mês, e enquanto não acontecesse o
  paciente veria "aguardando assinaturas" — parece que a clínica não fechou.
- **Webhook + leitura sob demanda**, sem cron novo. O webhook é o caminho normal;
  a leitura sob demanda (quando a Sofia precisa do estado no turno) elimina a
  classe de "assinou e o sistema não soube". Precedente: o webhook do Stripe no
  Hamilton está quebrado desde sempre — 21 assinaturas, 0 faturas, e ninguém
  percebeu.
- **Idempotência é do Hamilton**, não da Sofia. Ela remonta o link a cada turno
  (padrão do `links.py`); sem trava, cada turno criaria contrato novo, gastando
  crédito e deixando o paciente com três links. Valor diferente **gera contrato
  novo** (é outro combinado) e o antigo vira `substituido`.
- **Histórico, nunca substituição.** É documento jurídico: apagar é perder a prova
  de qual condição valia em qual período. E a vigência de 12 meses (cláusula 9.1)
  vai precisar disso em agosto de 2027.
- **PDF assinado em bytes no Postgres.** `MEDIA_ROOT = BASE_DIR/media` no Render é
  **filesystem efêmero** — arquivo salvo ali some no próximo deploy. Mesma lição
  que a tabela `midia` da Sofia já aprendeu. 20/mês × ~300 KB ≈ 6 MB/ano.
- **A Sofia manda o valor e o Hamilton atualiza o `vlr_sessao`.**
  `cobranca.valor_mensal()` já é a autoridade (devolve o desconto negociado, senão
  o preço de tabela) e **nunca lê o Hamilton**. Se o contrato disser R$ 180 e o
  `vlr_sessao` continuar 200, a nota fiscal sai errada e o documento assinado é a
  prova contra a Allos.
- **Estado do contrato injetado no prompt** da cobrança a cada turno, como o link
  de pagamento já é (`_link_atual`). **Sem tool nova** — é mais um caminho pro
  modelo errar, e a Demanda D já ensinou que a Sofia precisa usar a resposta que
  ela mesma provocou.
- **Flag `contrato_ativo` própria, nasce desligada**, independente da
  `cobranca_ativa`. A cobrança nunca rodou em produção; estrear duas features não
  testadas no mesmo turno que fala de dinheiro é como o parcelado do Stripe passou
  18 assinaturas cobrando pra sempre.
- **`AUTENTIQUE_SANDBOX`** no Hamilton, com a mesma lógica do
  `envio_whatsapp_bloqueado` da Sofia: `None` = liga sozinho fora de `production`.
  Seguro por omissão. O `.env` local carrega credencial real — foi assim que um
  `pytest` criou 4 Payment Links na conta LIVE do Stripe.

---

## ✅ Validado ponta a ponta com assinatura real (17/08/2026)

Contrato criado em sandbox, **assinado de verdade pelo Victor**, e conferido por
`manage.py validar_contrato conferir`:

```
status: assinado
assinado_em ....... 2026-08-17 23:12:32+00:00
ip ................ 177.55.226.169
cpf da assinatura . 11592775675
pdf ............... 278505 bytes
cpf no prontuário . 11592775675
idempotência ...... sincronizar duas vezes não muda nada
```

A assinatura só passou depois de dois defeitos que **nenhum mock pegaria**:

### 🔴 O CPF mora em `user_data`, não em `user`

A query pedia `user { id name email }` — que é a **conta da Autentique** do
signatário. O que a pessoa digita no ato fica em **`user_data { cpf birthday }`**,
em formato brasileiro (`115.927.756-75`, `21/11/2002`). Como a query nunca pediu
esse campo, `cpf_informado` ficava vazio **em silêncio** — e a premissa inteira
da decisão "não pedimos CPF na conversa porque a Autentique colhe" ia por água
abaixo sem ninguém notar.

### 🔴 Backfill não pode depender da transição de status

O preenchimento de CPF e PDF estava dentro do `if` que muda o status para
`assinado`. Parece econômico e é uma armadilha: **corrigir o código não bastou**
para preencher o CPF, porque o contrato já estava `assinado` e a transição nunca
mais aconteceria. Qualquer falha na primeira leitura (campo mudou, download caiu,
webhook chegou antes de o PDF existir) congelava o campo vazio para sempre.
Agora CPF e PDF são preenchidos **sempre que estiverem faltando**.

## 🔴 O prazo de assinatura não funciona na Autentique

Estava no desenho: `deadline_at` de 30 dias, pra não deixar link de contrato vivo
pra sempre. **Nenhuma das duas formas funciona nesta conta.** Medido contra a API
de verdade em 17/08:

| tentativa | resultado |
|---|---|
| `deadline_at: "2026-09-16"` | `invalid_date` |
| `deadline_at` com hora, ISO com `T`, com `Z`, com offset | `invalid_date` |
| `deadline_at: "16/09/2026"` | **Internal server error** |
| `expiration: {days_before, notify_at: "16/09/2026"}` | **aceito** — e o documento volta com `expiration_at: null` e `deadline_at: null` |

O último é o perigoso: **aceito e ignorado**, exatamente o formato do acidente do
`cancel_at` do Stripe. Um teste com mock teria ficado verde pra sempre.

**O que ficou:** nada é enviado à Autentique; o prazo é aplicado do nosso lado
por `servico.expirar_vencidos()`. O link segue vivo lá; o contrato é que deixa de
valer aqui. Se alguém tentar de novo (plano diferente, conta institucional), a
prova de que pegou é `document { expiration_at deadline_at }` voltar
**preenchido** — não é a criação devolver 200.

## Alternativas descartadas (não redescobrir)

| Descartado | Por quê |
|---|---|
| **PDF via WeasyPrint** | Não está instalado no Hamilton (embora as deps estejam órfãs no `requirements.txt`) e exige pango/cairo no Render — risco de build |
| **PDF via ReportLab/fpdf2** | O texto do contrato viraria código: mudar uma cláusula seria um deploy |
| **DOCX gerado a partir do texto do painel (escolhido)** | `python-docx` já está instalado; mudar o contrato é editar uma textarea, e existe **uma fonte só** |
| **`.docx` versionado no repo, editado no Word** | Era o desenho original. Caiu porque `.docx` é binário: não dá pra editar numa textarea, e "editar" viraria baixar-editar-subir — com duas cópias vivas de novo |
| **Upload do `.docx` pelo painel** | Mesma coisa: preserva o Word, mas não é "igual aos prompts" e mantém a duplicação |
| **Markdown ou HTML na textarea** | Uma sintaxe a mais pra errar num documento jurídico. O texto plano já é o formato do arquivo |
| **Guardar uma cópia do texto no Hamilton** | Recria exatamente a divergência que a edição no painel resolveu |
| **Entregar pelo WhatsApp da Autentique** (`DELIVERY_METHOD_WHATSAPP`) | Sai de um número desconhecido; tira o controle e a visibilidade da entrega |
| **Coletar CPF na conversa** | A Autentique já colhe melhor, vinculado ao ato, e economiza 3 perguntas |
| **Coletar endereço/contato de apoio pela Autentique** | Tecnicamente impossível — não há campo personalizado |
| **Verificação biométrica** (`PF_FACIAL`, `LIVE`) | Otimiza contra um risco que a Allos nunca teve, pagando com desistência num momento crítico |
| **Contrato no momento do cadastro** | Não existem terapeuta, valor definitivo nem vínculo; sairia metade em branco |
| **Contrato como 3ª etapa encadeada** | ~4 dias de abordagem depois de uma sessão |
| **Criar campo CRP no `Associado`** | Revogado: sem qualificação nominal no contrato, não há pra que |
| **Polling em vez de webhook** | Exigiria cron novo; a leitura sob demanda já cobre o buraco |

---

## Riscos aceitos

1. **O domínio da Autentique aparece na barra depois do clique.** O link curto é
   redirect, não proxy. Mesma limitação já aceita no Stripe.
2. ~~**Duas cópias do mesmo texto.**~~ **Resolvido** pela rodada 5: o texto passou
   a ser editado no painel e existe uma fonte só. O arquivo em `prompt/` virou o
   **padrão de fábrica** (o que o "Resetar" restaura), como o dos outros prompts.
   Em troca, entrou um risco novo e menor: **quem tem o painel pode editar um
   documento jurídico sem revisão**. Mitigações: aviso destacado na tela, e o PDF
   assinado guardado por contrato — um erro contamina o futuro, nunca o passado.
3. **O contrato assinado fica fora do Stripe e da NFS-e.** Nada aqui conserta o
   webhook do Stripe quebrado no Hamilton, nem faz as assinaturas criadas pela
   Sofia aparecerem na contabilidade.
4. **A conta da Autentique é pessoal até a migração.** Contratos reais só depois
   dela.

---

## 🔴 Achados durante a implementação (não são da Demanda E)

### 1. A Demanda A **não está na `main` do Hamilton**

Descoberto em 17/08 ao criar a migration. `is_parceria` **não existe em lugar
nenhum** do working copy nem da `main`: nem o campo em `acessorios/models.py`,
nem a migration, nem o intake aceitando captação e valor. Tudo isso está num
**único commit numa branch nunca mergeada**:

```
origin/feat/sofia-captacao-e-avaliacao
932ac19  feat(sofia): captação real no intake, flag de parceria e API de avaliação
```

O `CLAUDE.md` da Sofia dá a Demanda A como "✅ entregue" e validada. **Se a
produção roda a `main`, ela não tem nada disso**, e as consequências são as que
o próprio `CLAUDE.md` descreve: a Sofia lê `is_parceria` de um payload onde a
chave nunca vem, `e_parceria()` devolve `False` pra todo mundo, e nenhum paciente
de convênio é detectado. Com a cobrança ligada, todos seriam cobrados. Com o
contrato ligado, todos receberiam contrato de mensalidade.

⚠️ Essa branch também traz `principais/migrations/0005_avaliacao_respostas_pesquisa.py`,
e a migration do contrato nasceu como `0005_contratopaciente.py`. **As duas
dependem da `0004`** — no merge, o Django vai reclamar de dois nós-folha. Não é
grave, mas não se resolve sozinho: `manage.py makemigrations --merge`.

### 2. A suíte do Hamilton criava e apagava banco no Neon

`manage.py test` cria um `test_<nome>` no servidor do `DATABASE_URL` e o apaga no
fim. Como o `.env` local aponta pro Neon, a suíte fazia isso **remotamente** — e
bastaria alguém estar com o `.env` de produção na máquina pra fazê-lo no servidor
de produção. **Corrigido**: `app/settings.py` força SQLite in-memory quando
`sys.argv[1:2] == ['test']`, e zera junto o `SOFIA_DB_ALIAS` (senão as rotas da
Sofia continuariam consultando o Neon mesmo com o `default` local). O
comportamento antigo continua acessível com `DJANGO_TEST_DB=remoto`.

Efeito colateral bom: a suíte saiu de ~23 s com rede para rodar offline.

### 🔴 3. A Sofia NÃO tem trava de dry-run pro Stripe

Rodando a validação ponta a ponta em 17/08, o fluxo de cobrança **criou um
Payment Link de verdade na conta LIVE** (`plink_1U5ZSr…`, R$ 200/mês, paciente
fictícia) — desativado em seguida, sem assinatura e sem cobrança.

A causa é estrutural, não um descuido pontual: existe trava pro **WhatsApp**
(`envio_whatsapp_bloqueado`, liga sozinha fora de produção) e agora pro
**Autentique** (`AUTENTIQUE_SANDBOX`, idem). **Não existe pro Stripe.** O `.env`
de desenvolvimento carrega `sk_live_`, e `cobranca._criar_link` chama a API antes
de qualquer outra coisa. É o mesmo buraco que já produziu os 4 Payment Links
acidentais registrados no `CLAUDE.md` — ele continua aberto, e qualquer pessoa
que rodar o runbook de conversa com a cobrança ligada vai cair nele de novo.

**✅ Corrigido em 17/08.** `settings.stripe_key` é agora a única fonte da chave:
fora de `production` devolve a `TEST_STRIPE_SECRET_KEY`; se ela estiver vazia,
devolve **vazio** e o Stripe fica desligado — a tela mostra o aviso e a cobrança
oferece só o Pix, caminhos que a app já sabia percorrer. **Nunca cai pra live.**

**Sem escotilha, de propósito.** A do WhatsApp existe porque há um caso legítimo
(mandar mensagem pro próprio número num teste de ponta a ponta); aqui não há —
pra ver dado real existe o dashboard do Stripe. Escotilha acaba ligada e
esquecida, e foi assim que o link de hoje nasceu.

Coberto por `TestChaveDoAmbiente` em `tests/test_pagamentos.py`, inclusive um
teste que confere o **header da requisição de verdade** — não basta a
propriedade estar certa, é ela que tem que chegar no `Authorization`.

⚠️ Quem escrever código novo: **`settings.stripe_key`, nunca
`settings.stripe_secret_key`.**

### 4. O signal `criar_avaliacao_consulta` ignora o alias de banco

Criar uma `Consulta` no alias `sofia_api` estoura:

```
IntegrityError: insert or update on table "avaliação" violates foreign key
Key (fk_consulta_id)=(8214) is not present in table "consultas"
```

O signal grava a `Avaliacao` sem `using`, ou seja, sempre no `default`, e a outra
conexão ainda não enxerga a linha. Com `SOFIA_API_DATABASE_URL` setada, **toda
consulta criada pela API da Sofia quebra**. É a mesma família do
`ValueError: the current database router prevents this relation` que o runbook já
registra. Fora do escopo desta demanda, mas está no caminho.

### 5. O Hamilton local precisa de `DEBUG=True`

`SECURE_SSL_REDIRECT` liga com `DEBUG=False` e devolve **301 em `POST
/authentication/token/`** — a Sofia registra `Auth Hamilton falhou (301)` e cai
pra `cadastro_pendente`, o que parece bug de cadastro e não é.

### 6. `httpx` está no `requirements.txt` mas não instalado

O `acessorios/webmania.py` (NFS-e) usa `requests`, que está instalado. O cliente
da Autentique foi escrito em `requests` por isso. Vale conferir se mais alguma
coisa no `requirements.txt` está declarada e ausente — as dependências do
WeasyPrint estão lá **órfãs** (`pydyf`, `tinycss2`, `tinyhtml5`, `cssselect2`,
`pyphen`), sem o WeasyPrint em si.

---

## Ordem de execução

| # | Parte | Onde | Depende de |
|---|---|---|---|
| 1 | Modelo `gpt-5.6-terra` + fix do `Settings` | Sofia | — |
| 2 | `.docx` com as 5 mudanças | Hamilton | — |
| 3 | `ContratoPaciente`, cliente Autentique, endpoints, webhook | Hamilton | 2 |
| 4 | Chamada, prompt, guardas, tela Hoje, flag | Sofia | 3 |
| 5 | `scripts/validar_contrato.py` — ciclo real em sandbox | ambos | 4 |

**Fora do código, com o Victor / Paulo:**

- [ ] **Paulo aprova o texto** do `.docx` antes do primeiro contrato real.
- [ ] **Cadastrar o webhook no painel da Autentique** — não dá pra fazer por API.
      Apontar pro host que está no `ALLOWED_HOSTS` (`hamilton-v2.onrender.com`).
- [ ] **Migrar pra conta institucional** e escolher o plano (~20 contratos/mês;
      free tem teto de 5 MB por documento, profissional 20 MB).
- [ ] **Victor assina um contrato de verdade em sandbox** — o passo de abrir o
      link e assinar não dá pra automatizar, e é onde as surpresas moram. Foi um
      dry run manual que revelou que a captação da Demanda A era descartada no
      intake, com as duas suítes verdes.
- [ ] **Ligar a `cobranca_ativa` sozinha** por algumas semanas antes da
      `contrato_ativo`.
