# Ajuste da tabela `Avaliacao` (Hamilton)

> Model: `principais/models.py`, `db_table = "avaliação"` (com cedilha e acento —
> atenção ao escrever SQL na mão).

## Status (06/08/2026)

**Os campos foram criados e a pesquisa já grava neles.** Foi adotada a **opção
(a)** — espelho fiel, campos novos, nenhum campo existente ressignificado.

Migration: `principais/migrations/0005_avaliacao_respostas_pesquisa.py`.

**O que continua em aberto e vai ser discutido com o Paulo** (junto da Demanda D,
ver `01-EM-ANDAMENTO.md` §D.0):

1. **O modelo de perguntas em si** — quais perguntas ficam no questionário
   definitivo. O que está implementado hoje é o modelo que a Juliana usava
   (`03-questionario-atual-da-qualidade.md`) mais a pergunta nova sobre o atendimento da Sofia.
2. **A planilha de avaliação** que o time de Qualidade usa — precisa ser editada
   pra refletir o modelo novo e o fato de que quem coleta agora é a Sofia. Como
   as respostas passam a viver na `Avaliacao`, é preciso decidir se a planilha
   vira um export do Hamilton ou continua em paralelo.
3. **Texto livre × estruturado** nas três perguntas ambíguas (ver a seção
   "Tipos de resposta" abaixo). Hoje estão implementadas como **texto + booleano
   derivado**, que foi a recomendação — mas não foi confirmado.

---

## Por que este documento existe

A `Avaliacao` do Hamilton **já é criada sozinha** por signal
(`principais/signals.py:54-81`), nos dois gatilhos:

- terapeuta lança a **primeira consulta** → `momento='No início do processo (primeira sessão)'`, `status='pendente'`
- terapeuta lança a **alta/desistência** → `momento='Após o encerramento da terapia'`, `status='pendente'`

Ou seja: **a fila de trabalho da Juju já existe no banco.** O problema é que o
model **não tem onde guardar metade das respostas** que ela coletava
(`03-questionario-atual-da-qualidade.md`).

---

## Confronto: perguntas da Juju × campos existentes

| Pergunta (modelo da Juju) | Campo hoje | Situação |
|---|---|---|
| INDIVIDUAL — bem-estar pessoal (0-10) | `individual` | ✅ ok |
| INTERPESSOAL — relacionamentos (0-10) | `interpessoal` | ✅ ok |
| SOCIAL — comunicação/convívio (0-10) | `social` | ✅ ok |
| QUALIDADE GERAL — estado geral de bem-estar (0-10) | `geral` | ✅ ok |
| Consentimento pra começar a pesquisa | `consentimento_paciente` | ✅ ok |
| **Nota do terapeuta (0-10)** | — | ❌ **não existe** |
| **Nota de indicação / NPS Allos (0-10)** | `qualidade_geral`? | ⚠️ **ambíguo** |
| **Nota da Sofia (0-10)** — pergunta nova | — | ❌ **não existe** |
| **Data da última sessão** | — | ❌ **não existe** |
| **Feedback livre** | `observacao`? | ⚠️ **rotulado "Definição de objetivo de trabalho"** |
| **Foi atendido rápido?** | — | ❌ **não existe** |
| **Indicaria a Allos?** | `continuar_allos`? | ⚠️ é booleano de *continuar*, não de *indicar* |
| **Motivo da interrupção** (só encerramento) | — | ❌ **não existe** |

### Campos do model que NÃO estão na pesquisa

Existem no `Avaliacao` e não têm pergunta correspondente:
`continuar_terapeuta`, `continuar_allos`, `qualidade_geral`.

E o `momento` tem um terceiro choice — `'Durante o acompanhamento terapêutico'` —
que **nenhum signal cria** hoje. Ninguém dispara avaliação de meio de processo.

---

## Decisão tomada: (a) — espelho fiel ✅

Foi implementada a **(a)**. Nenhum campo existente foi ressignificado, então
nada que já usa o Hamilton mudou de comportamento. `qualidade_geral`,
`observacao`, `continuar_terapeuta` e `continuar_allos` continuam **intocados**
e com o significado original.

### (a) Espelho fiel — campos novos, 1-pra-1 com as perguntas ✅ IMPLEMENTADO

Adicionados ao model:

| Campo | Tipo | Pergunta |
|---|---|---|
| `nota_terapeuta` | `IntegerField(null, blank, 0..10)` | "Como você se sentiu sendo atendido pelo seu terapeuta?" |
| `nota_indicacao` | `IntegerField(null, blank, 0..10)` | "Quanto você indicaria esse atendimento pra alguém…?" |
| `nota_sofia` | `IntegerField(null, blank, 0..10)` | **nova** — qualidade do acolhimento/encaminhamento da Sofia |
| `dat_ultima_sessao` | `DateField(null, blank)` | "Quando foi a data da sua última sessão?" |
| `feedback_livre` | `TextField(null, blank)` | "Gostaria de deixar algum feedback geral?" |
| `atendimento_rapido` | `TextField(null, blank)` | "Você foi atendido rápido?" |
| `indicaria_allos` | `TextField(null, blank)` | "Indicaria a Allos pra outras pessoas?" |
| `motivo_interrupcao` | `TextField(null, blank)` | só encerramento — "por que decidiu interromper?" |

**Prós:** o model vira espelho fiel da pesquisa; nenhum campo existente muda de
significado; nada que já usa o Hamilton quebra.
**Contras:** migration maior; o model fica com campos que só valem pra um dos
dois `momento`s (aceitável — todos são `null=True`).

### (b) Reaproveitar campos existentes + mínimo de campos novos

Ex.: `qualidade_geral` = nota do terapeuta, `observacao` = feedback livre.

**Prós:** migration menor.
**Contras:** **ressignifica campos que já têm significado documentado** —
`observacao` está rotulado *"Definição de objetivo de trabalho"* com help_text
*"Escreva com uma frase o objetivo com a terapia"*. Quem já usa o Hamilton
passaria a ver duas coisas diferentes no mesmo campo, sem aviso.

### Por que a (a) e não a (b)

O custo extra da (a) é uma migration maior; o custo da (b) seria dado ambíguo
pra sempre, num sistema que outras pessoas usam. Os campos novos são todos
`null=True`, então nenhum registro existente foi afetado.

---

## Tipos de resposta: livre × estruturado

Três perguntas do modelo da Juju são **sim/não disfarçadas de texto**:

- "Você foi atendido(a) rápido?" → `Resposta: ______`
- "Indicaria a Allos para outras pessoas?" → `Resposta: ______`
- "Qual foi o motivo pelo qual você decidiu interromper?" → `Resposta: ______`

**Implementado (não confirmado com o Paulo — item a discutir):** as duas
primeiras como **texto livre + um booleano derivado**
(`atendimento_rapido_bool`, `indicaria_allos_bool`), preservando a fala e
permitindo métrica. `motivo_interrupcao` ficou texto livre — categorizar motivo
de saída por LLM é justamente onde um erro custaria mais.

> Isto pesa mais que o normal porque a extração é feita **por LLM, sem tool**
> (ver risco registrado em `01-EM-ANDAMENTO.md` §C.2). Quanto mais estruturado o campo,
> mais superfície pro modelo errar em silêncio.

---

## `status` — sem campo novo

Choices atuais: `pendente`, `avaliado`, `nao_respondeu`, `legado`.

**Decisão fechada no grilling:** **recusa e silêncio caem os dois em
`nao_respondeu`.** Não se cria choice `recusou`.

> "Se a pessoa diz que não quer responder é óbvio que o status é não respondeu" — Paulo

Transições que a Sofia faz:

```
pendente ──(pesquisa concluída)──────────────► avaliado
    │
    ├────(recusou)───────────────────────────► nao_respondeu
    └────(44h sem resposta)──────────────────► nao_respondeu
```

---

## Campo de controle: já foi enviada? ✅ IMPLEMENTADO

**Problema:** `status='pendente'` significa "ainda não foi respondida", **não**
"ainda não foi enviada". Sem distinguir, o cron da Sofia reenviaria o convite a
cada tick pra quem ainda não respondeu.

Criados **`sofia_enviada_em`** e **`sofia_lembrete_em`**
(`DateTimeField(null=True, blank=True)`). NULL = ainda não saiu. Mesmo padrão do
`seguimento_enviado_em` da `Conversa`, que já resolve isso no follow-up de lead.

`GET /api/v1/avaliacoes/pendentes/` filtra por `sofia_enviada_em__isnull=True`
por padrão; `?enviadas=1` traz também as já enviadas (é como a Sofia acha quem
precisa de lembrete ou de encerramento por prazo).

---

## Ligação Sofia ↔ Hamilton

A `Avaliacao` aponta pro `Paciente` (`fk_paciente`). A Sofia acha a conversa em
duas tentativas (`pesquisa._conversa_do_paciente`):

1. por `Conversa.paciente_hamilton_id` — o caminho confiável, mesmo do
   `/painel/acompanhamento`;
2. **por telefone**, comparando só os dígitos — cobre quem conversou com a Sofia
   mas foi cadastrado à mão no Hamilton (o Hamilton guarda sem DDI, `31...`, e o
   WhatsApp manda com, `5531...`).

**Caso ainda não coberto:** paciente que **nunca falou com a Sofia** não tem
conversa nenhuma. Fora da janela de 24h da Meta não dá pra iniciar uma com texto
livre, então esses são **pulados em silêncio** na rodada do cron.

**A decidir:** a Sofia abre conversa nova (exigiria **template aprovado pela
Meta**, que demora), ou esses ficam com a equipe? Como a Sofia é recente,
provavelmente é a maioria dos pacientes ativos hoje.

---

## Checklist

Feito:

- [x] Decisão **(a)** — espelho fiel, sem ressignificar campo existente
- [x] Migration: campos de resposta + `sofia_enviada_em` + `sofia_lembrete_em`
      (`principais/migrations/0005_avaliacao_respostas_pesquisa.py`)
- [x] `AvaliacaoPendenteSerializer` (leitura) e `AvaliacaoRespostasSerializer`
      (escrita no PATCH, com allowlist de campos — o canal não reescreve vínculo)
- [x] `GET /api/v1/avaliacoes/pendentes/` — inclui `tipo_saida` e `cancelador`
      da `Altadesistencia` vinculada (a Sofia adapta a fala por eles, ver
      `01-EM-ANDAMENTO.md` §C.6)
- [x] `PATCH /api/v1/avaliacoes/<pk>/`
- [x] Testes no Hamilton (`principais/tests_sofia_api.py`, 19 passando)

Em aberto:

- [ ] **Revisar o modelo de perguntas com o Paulo** e **editar a planilha de
      avaliação** do time de Qualidade (ver `01-EM-ANDAMENTO.md` §D.0)
- [ ] Confirmar as três perguntas ambíguas como texto + booleano derivado
- [ ] **Restringir os endpoints por grupo/permissão**, não só `IsAuthenticated`.
      São respostas de pesquisa de saúde, e hoje **qualquer usuário logado do
      Hamilton** acessa essas rotas (o token vale 1 dia). Vale para as 4 rotas
      antigas da Sofia também — problema pré-existente, agora com dado mais
      sensível trafegando.
- [ ] Resolver o caso do paciente que nunca falou com a Sofia
- [ ] Ninguém dispara avaliação de meio de processo — o choice
      `'Durante o acompanhamento terapêutico'` continua sem gatilho
