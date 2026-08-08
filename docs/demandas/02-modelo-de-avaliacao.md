# Modelo de avaliação — questionários, campos e alertas

> **Data desta versão:** 06/08/2026. Fechado em grilling com o Paulo (Q1–Q38).
> Substitui integralmente a versão anterior deste documento.
>
> Model afetado: `hamilton-api`, `principais/models.py`, `class Avaliacao`
> (`db_table = "avaliação"` — com cedilha e acento, atenção ao escrever SQL na mão).

---

## ⚠️ Correção da versão anterior deste documento

A versão anterior afirmava que os campos de resposta **já existiam** no Hamilton, que
havia uma migration `0005_avaliacao_respostas_pesquisa.py` e que os endpoints de
avaliação estavam implementados e testados (19 testes passando).

**Nada disso existe.** Verificado em `../hamilton-api`, branch `main`, working tree
limpo:

- `nota_terapeuta`, `nota_sofia`, `feedback_livre`, `sofia_enviada_em` e os demais
  campos novos **não aparecem em lugar nenhum do repositório**;
- as migrations param na `0004_backfill_vinculo_stripe.py`;
- não há rota `avaliacoes/pendentes/` nem `PATCH /api/v1/avaliacoes/<pk>/`;
- não há nenhuma branch local ou remota com esse trabalho.

**Consequência prática:** a Demanda C **não funciona hoje**, mesmo que o cron seja
ligado. O `app/services/pesquisa.py` da Sofia está escrito contra uma API do Hamilton
que não existe. O lado bom é que **o schema está totalmente aberto** — este documento
não está consertando algo em produção, está desenhando do zero.

---

## Por que este desenho é diferente do anterior

O questionário herdado da Juliana (`03-questionario-atual-da-qualidade.md`) funde **dois
instrumentos com propósitos opostos**:

1. **Medição de desfecho clínico** — as quatro notas de 0 a 10 (Individual,
   Interpessoal, Social, Geral). Isso é o **ORS** (*Outcome Rating Scale*, Miller &
   Duncan), instrumento internacional que é a base do FIT / prática deliberada.
2. **Satisfação e qualidade de serviço** — nota do terapeuta, indicação (NPS),
   rapidez, feedback livre.

Fundir os dois num único bloco enquadra a pessoa em modo "avalie nosso atendimento" e
**enviesa a nota clínica pra cima**. E os públicos são diferentes: desfecho é o que se
mostra pra **prefeitura, parceria e edital**; satisfação é **gestão interna**.

**Decisão (Q1):** os dois instrumentos coexistem, mas em **blocos separados e nesta
ordem — ORS primeiro, satisfação depois**. O ORS é respondido antes de a pessoa entrar
em modo cliente.

### O ORS é um bloco fechado (Q2)

Os quatro itens só valem **como conjunto**: a métrica publicada é o **total 0-40**,
com corte clínico em torno de 25 (adultos) e mudança confiável a partir de ~5 pontos.
Cortar um item, reescrever a redação ou trocar a escala **destrói a comparabilidade
internacional** — vira uma escala caseira.

**Regra:** os quatro itens são congelados (redação, ordem e escala 0-10). Qualquer
alteração futura é uma **quebra de série histórica** e precisa ser tratada como tal.

Um ORS com 3 de 4 itens **não existe** — é dado descartado no relatório.

> 📊 **Já existe série histórica.** A produção tem **163 avaliações com os quatro itens
> do ORS preenchidos** (contado em 06/08/2026), colhidas à mão pela Juliana e transcritas
> no formulário do Hamilton. Não estamos começando do zero — é mais um motivo pra **não
> mexer na redação nem na escala**.

### O furo que este desenho conserta

No desenho anterior, o "baseline" era colhido **depois da primeira sessão** e dias
depois do lançamento do prontuário. Isso mede a pessoa **já tratada**: parte da melhora
já aconteceu antes da régua encostar, e qualquer ganho pré/pós sairia subestimado.

Agora o ORS-baseline é colhido **antes da primeira sessão**, logo após o cadastro.

### O que este desenho **não** é

Com o ORS aplicado só na entrada, no reencaminhamento e no encerramento, isto é
**medição de desfecho pré/pós** — não é prática deliberada, que exigiria medição
recorrente durante o processo. É uma coisa mais modesta e ainda assim rara no Brasil.
**Ajustar o vocabulário quando isso for apresentado pra fora.**

---

## Os quatro questionários

| | **Entrada** | **1ª sessão** | **Reencaminhamento** | **Encerramento** |
|---|---|---|---|---|
| **Gatilho** | Sofia, ≥3h após o cadastro | signal (1ª consulta lançada) | signal (Altadesistência = reencaminhamento) | signal (alta / desistência / não responde) |
| **Quem cria a `Avaliacao`** | **a Sofia**, via `POST` | Hamilton (signal) | Hamilton (signal) | Hamilton (signal) |
| **`momento`** | `Antes da primeira sessão (linha de base)` **(choice novo)** | `No início do processo (primeira sessão)` | `Durante o acompanhamento terapêutico` | `Após o encerramento da terapia` |
| **Nº de perguntas** | 5 | 4 | 6 | 9 |
| ORS ×4 | ✅ | ❌ | ✅ | ✅ |
| Nota da Sofia | ✅ | ❌ | ❌ | ❌ |
| Nota do terapeuta | ❌ | ✅ | ✅ (o anterior) | ✅ |
| Encaixe com o terapeuta | ❌ | ✅ | auto `false` | auto `false` |
| NPS Allos | ❌ | ✅ | ❌ | ✅ |
| Motivo | ❌ | ❌ | ✅ | ✅ |
| Feedback livre | ❌ | ✅ | ❌ | ✅ |
| Continuar na Allos | ❌ | ❌ | auto `true` | ✅ (+ reoferta única) |
| Encadeia cobrança (Demanda D) | ❌ | ✅ | ❌ | ❌ |

### 1. Entrada — linha de base (5 perguntas)

**Por que existe:** é o único ponto de medida **antes do tratamento**. Sem ele não há
par pré/pós, e o ORS sozinho não significa nada — o dado só é válido como
`ORS saída − ORS entrada`.

**Gatilho e guardas (Q19):** disparada pelo cron `/tasks/pesquisas`, no primeiro tick
a partir de **3h após o `cadastrar_paciente` ter dado certo**. As 3h evitam emendar na
conversa de acolhimento (que é onde mora a receita).

**Não envia** se:
- a conversa está em `modo = humano`;
- há escalada aberta não resolvida;
- `conversa.estado = cadastro_pendente`;
- não há `paciente_hamilton_id`.

Se a guarda bloquear, tenta nos ticks seguintes por até **5 dias** e desiste — baseline
velho demais deixa de ser baseline.

**Perguntas:**

| # | Pergunta | Campo |
|---|---|---|
| — | Consentimento explícito pra participar da pesquisa | `consentimento_paciente` |
| 1 | Hoje, você se sente bem com quem você é e com a vida que está levando? (0-10) | `individual` |
| 2 | Quanto você está satisfeito com seus relacionamentos atuais (família, amigos, pessoas próximas)? (0-10) | `interpessoal` |
| 3 | Você sente que consegue se comunicar bem e se relacionar com as pessoas nos seus contextos diários (trabalho, faculdade, amizades)? (0-10) | `social` |
| 4 | De forma geral, o quanto você sente que está bem com sua vida hoje (emocionalmente, fisicamente, financeiramente, profissionalmente)? (0-10) | `geral` |
| 5 | O quanto você achou bom o acolhimento e o encaminhamento até chegar no terapeuta? (0-10) | `nota_sofia` |

**Enquadramento (Q32):** pessoal-mas-verdadeiro, com uma pitada de técnico. A Sofia diz
que é **um retrato de como a pessoa está hoje, pra conseguir comparar lá na frente e
saber se o processo ajudou**, e menciona em uma frase que são perguntas de **uma escala
usada internacionalmente**. Isso compra os dois minutos dela e é literalmente verdade.

> ⚠️ **A Sofia nunca diz que o terapeuta vai ver as respostas.** Ele não vai (Q22).

**Se recusar (Q10 + Q17):** recusa não se insiste. A pessoa fica **permanentemente fora
da análise pré/pós** — não existe plano B, porque a pesquisa de 1ª sessão não tem mais
ORS, e um ORS colhido depois da primeira sessão **não é comparável** com um colhido
antes. Menos pares limpos é melhor que mais pares sujos.

### 2. Primeira sessão — satisfação (4 perguntas)

**Gatilho:** signal já existente, quando o terapeuta lança a primeira consulta.

**Não tem ORS (Q12).** Este questionário é puramente satisfação.

| # | Pergunta | Campo |
|---|---|---|
| 1 | Como você se sentiu sendo atendido(a) pelo seu terapeuta? (0-10) | `qualidade_geral` |
| 2 | Você sentiu que o terapeuta combinou com você? | `continuar_terapeuta` |
| 3 | Quanto você indicaria esse atendimento pra alguém passando por algo parecido? (0-10) | `nota_indicacao` |
| 4 | Gostaria de deixar algum feedback geral sobre os serviços da Allos? | `feedback_livre` |

**Sobre a pergunta 2 (Q26):** é a pergunta de maior alavancagem do questionário inteiro
— pegar match ruim na sessão 1 em vez de perder o paciente em silêncio. Ela é feita como
**encaixe**, não como troca: *"você sentiu que o terapeuta combinou com você?"* e nunca
*"quer trocar de terapeuta?"*. Perguntar sobre troca **planta a ideia** em quem nem tinha
pensado nisso. A formulação de encaixe captura o mesmo sinal sem oferecer a saída.

Nota 6 no terapeuta e "não combinou comigo" são coisas **diferentes** — dá pra achar o
profissional competente e ainda assim não engatar. Por isso as duas perguntas coexistem.

**Encadeia a cobrança (Demanda D).** Quando esta pesquisa termina — respondida, recusada
ou expirada — a Sofia abre o assunto da mensalidade, com transição adequada.

### 3. Reencaminhamento — troca de terapeuta (6 perguntas)

**Por que é separado (Q5):** reencaminhamento **não é saída** — a pessoa continua na
Allos. Jogar isso na base de encerramento mistura desistência com troca de terapeuta na
hora de calcular churn, e faz a Sofia perguntar "por que você interrompeu" pra quem não
interrompeu.

**Vai pro `momento = 'Durante o acompanhamento terapêutico'`**, choice que já existe no
Hamilton e **nunca teve gatilho automático** (a produção tem 8 registros, todos
`pendente`, criados à mão). Ganho de brinde: passa a existir medição de meio de processo.

| # | Pergunta | Campo |
|---|---|---|
| 1 | O que levou à troca de terapeuta, do seu ponto de vista? | `motivo_encerramento` |
| 2-5 | ORS ×4 (mesma redação da entrada) | `individual`, `interpessoal`, `social`, `geral` |
| 6 | Como você se sentiu sendo atendido(a) pelo terapeuta anterior? (0-10) | `qualidade_geral` |
| — | preenchido automaticamente | `continuar_terapeuta = false` |
| — | preenchido automaticamente | `continuar_allos = true` |

**Sem NPS e sem feedback livre.** NPS aqui mede lealdade a uma instituição que a pessoa
**não está deixando** — respondido no meio de uma troca, é ruído. Feedback livre se
sobrepõe ao motivo da troca.

**O ORS do reencaminhamento vira o baseline do terapeuta novo.** É exatamente a
comparação entre terapeutas que se quer, e é a única forma honesta de fazê-la: mesmo
paciente, mesma régua, dois terapeutas.

### 4. Encerramento — alta, desistência, sumiço (9 perguntas)

**Gatilho:** signal, quando o terapeuta lança a `Altadesistencia` com `alta`,
`desistencia` ou `não responde`.

| # | Pergunta | Campo |
|---|---|---|
| 1 | Motivo do encerramento (**adaptado**, ver abaixo) | `motivo_encerramento` |
| 2-5 | ORS ×4 | `individual`, `interpessoal`, `social`, `geral` |
| 6 | Como você se sentiu sendo atendido(a) pelo seu terapeuta no período? (0-10) | `qualidade_geral` |
| 7 | Quanto você indicaria esse atendimento pra alguém passando por algo parecido? (0-10) | `nota_indicacao` |
| 8 | Gostaria de deixar algum feedback geral? | `feedback_livre` |
| 9 | Você gostaria de continuar sendo atendido(a) na Allos, com outro terapeuta? | `continuar_allos` |
| — | preenchido automaticamente | `continuar_terapeuta = false` |

**Adaptação da pergunta 1** pelo `alta_desistencia` e pelo `cancelador`:

| `alta_desistencia` | Como a Sofia aborda |
|---|---|
| `alta` | O processo se concluiu bem. Pergunta **como foi a experiência**, nunca "por que interrompeu". |
| `desistencia` | Pergunta o motivo diretamente. |
| `não responde` | Vai mesmo assim (Q6 — **pesquisa completa**). É o grupo com menor taxa de resposta e o **mais valioso**: é a única chance de saber por que as pessoas evadem. |

**Se `cancelador = 'terapeuta'`:** nunca pergunta por que **ela** decidiu interromper —
não foi ela quem decidiu. Pergunta como foi a experiência no período.

**Fica em 9 perguntas (Q35).** É o questionário mais longo aplicado ao público com menor
disposição de responder. Se em 30 dias a taxa de abandono no meio for alta, **o primeiro
a sair é o NPS** — ele já existe na pesquisa de 1ª sessão, e a nota de indicação de quem
está saindo é a menos acionável das nove.

---

## Reoferta no encerramento (Q27 + Q31)

**A regra antiga estava errada.** O prompt de encerramento dizia *"nunca tente reverter a
saída, convencer a pessoa a voltar, oferecer desconto ou reter o paciente"*. Perguntar
uma vez, aceitar a resposta e não voltar ao assunto **não é retenção — é encerrar
direito**. A pergunta 9 é a que recupera desistência virando reencaminhamento, e é
comercialmente a mais valiosa do encerramento.

**A regra nova:**

- Pergunta neutra no fim de tudo, depois das outras oito.
- Se a resposta for não: **uma** reoferta curta e leve ("se mudar de ideia, é só me
  chamar"). Argumentação leve é permitida.
- **Acabou.** Nunca um terceiro turno sobre o assunto.

**Três bloqueios duros** — nesses casos a Sofia nem faz a pergunta:

1. `cancelador = 'terapeuta'` — não foi a pessoa que saiu; insistir é constrangedor.
2. **O motivo menciona experiência ruim ou reclamação** — aqui o certo é **escalar pra
   Thainá**, não reofertar. Este caso é mais valioso que a reoferta em si: é onde se
   perde paciente e reputação, e é onde **um humano precisa aparecer**.
3. `alta_desistencia = 'alta'` — o processo se concluiu bem. Reofertar terapia pra quem
   recebeu alta é contra-indicado clinicamente, não só chato.

### ❌ Desconto: proibido

A Sofia **nunca** oferece desconto, nem "levemente". Três motivos:

1. Preço é **decisão comercial**, e quem decide são as pessoas da Allos — não um LLM
   interpretando o clima da conversa.
2. Já existe faixa gratuita estruturada (parceria / `is_parceria`). Desconto ad-hoc por
   WhatsApp cria dois pacientes pagando valores diferentes pelo mesmo serviço, **sem
   registro de quem autorizou**.
3. Colide com a Demanda D, que assume **preço fixo** vindo do `/painel/config`.

Se um dia existir desconto de retenção, ele será **um valor único definido no painel**, e
a Sofia só oferece esse — nunca negocia.

---

## Quem está respondendo (Q18 + Q28)

**Regra:** se quem responde **não é a pessoa atendida**, não se aplica o ORS.

Motivo: o número de WhatsApp cadastrado é frequentemente o do **responsável ou do
cônjuge**. Sem esse cuidado, a Sofia pergunta "o quanto você se sente bem com quem você
é" pra mãe do paciente e grava como nota do filho.

**Como a Sofia sabe:**

1. **No acolhimento (principal).** A Sofia já conversa com quem escreve — é barato
   registrar em `dados_coletados` se está falando com **o próprio paciente** ou com um
   **acompanhante**. A pesquisa lê de lá.
2. **Na pesquisa (rede).** Quando o dado não existir (paciente cadastrado à mão, conversa
   antiga) ou quando houver sinal — paciente menor de idade, ou nome do contato diferente
   do nome do paciente — a Sofia pergunta no começo.

**Se for acompanhante:** pula o bloco ORS, aplica só o bloco de satisfação. Os quatro
campos ficam `null`, o que **já exclui aquela avaliação da série** sem precisar de flag
nova.

**Menor de idade:** abaixo de 12 anos a conversa é escalada pra Thainá de qualquer jeito
(regra que já existe) — e nessa faixa quem está no WhatsApp é quase certamente um
acompanhante. **Adolescente de 13 a 17 em contato direto responde o ORS normal.**

---

## Modelo de dados — `Avaliacao` no Hamilton

### Campos novos

| Campo | Tipo | Uso |
|---|---|---|
| `nota_sofia` | `IntegerField(null=True, blank=True, 0..10)` | nota do acolhimento/encaminhamento (só na entrada) |
| `nota_indicacao` | `IntegerField(null=True, blank=True, 0..10)` | NPS Allos (1ª sessão e encerramento) |
| `feedback_livre` | `TextField(null=True, blank=True)` | comentário aberto |
| `motivo_encerramento` | `TextField(null=True, blank=True)` | motivo da troca **ou** da saída (Q36) |
| `sofia_enviada_em` | `DateTimeField(null=True, blank=True)` | NULL = convite ainda não saiu |
| `sofia_lembrete_em` | `DateTimeField(null=True, blank=True)` | NULL = lembrete ainda não saiu |

**`motivo_encerramento` é um campo só pros dois casos (Q36).** O `momento` já diz se foi
troca ou saída, então não há ambiguidade real — e dois campos duplicariam a lógica de
extração.

### Choice novo em `momento`

```python
('Antes da primeira sessão (linha de base)', 'Antes da primeira sessão (linha de base)')
```

Os três choices existentes continuam. Reusar `'No início do processo (primeira sessão)'`
pra entrada colidiria com o signal da consulta.

### Campos alterados

| Campo | Hoje | Passa a ser | Por quê |
|---|---|---|---|
| `continuar_terapeuta` | `BooleanField(default=False)` | `BooleanField(null=True, blank=True, default=None)` | hoje "não" e "não perguntado" são o **mesmo valor** |
| `continuar_allos` | `BooleanField(default=False)` | `BooleanField(null=True, blank=True, default=None)` | idem |

⚠️ A migration **não deve** fazer backfill: os registros antigos continuam `False`, que é
o que eles sempre significaram no formulário do Hamilton. Só os novos usam `NULL`.

### Campos reusados sem migration

| Campo | Passa a receber |
|---|---|
| `qualidade_geral` | **nota do terapeuta** (0-10) |
| `individual`, `interpessoal`, `social`, `geral` | os quatro itens do ORS |
| `consentimento_paciente` | consentimento (ver regra abaixo) |

> ⚠️ **Risco registrado sobre o `qualidade_geral` (Q24/Q30).** Foi decidido **não
> renomear nada** e tratar o campo como nota do terapeuta.
>
> **O que a produção mostra** (contado em 06/08/2026): 367 avaliações, **162 com
> `qualidade_geral` preenchido** e **163 com os quatro itens do ORS preenchidos** —
> praticamente as mesmas linhas. Ou seja, quem preenchia o formulário do Hamilton estava
> **transcrevendo a pesquisa da Juliana inteira**, e o único campo de nota de atendimento
> disponível recebia a nota do atendimento. Isso sustenta a leitura adotada e reduz muito
> o risco.
>
> **Mitigação que continua valendo:** as linhas da Sofia são identificáveis por
> `sofia_enviada_em IS NOT NULL`. Todo relatório que usar `qualidade_geral` deve declarar
> de qual recorte está falando.

### Campos **não** criados (decisão)

`dat_ultima_sessao`, `atendimento_rapido` (+ booleano), `indicaria_allos` (+ booleano) e
`ors_total`.

**Por quê (Q3):** três das onze perguntas originais eram **deriváveis ou duplicadas**:

- **Data da última sessão** — o Hamilton tem `Consulta.dat_consulta` + `is_realizado`.
  Ele **sabe**. A pergunta era resquício de quando a Juliana não tinha acesso ao sistema.
- **"Foi atendido rápido?"** — `Paciente.created_at` → 1ª consulta realizada dá o
  intervalo **objetivo, em dias, pra 100% dos pacientes**, inclusive os que não respondem.
- **"Indicaria a Allos?" (sim/não)** — mede o mesmo construto que `nota_indicacao`. Era
  NPS perguntado duas vezes.

**As duas métricas derivadas ficam no Hamilton** (Q23), que é onde as consultas moram.
Puxar isso pra Sofia via API só pra exibir seria acoplar por estética.

**`ors_total` não é gravado (Q21):** o Hamilton só armazena os quatro itens; a soma é
feita no relatório, e só quando os quatro existem.

### Campos intocados

`observacao` (tem dono: *"Definição de objetivo de trabalho"*), `dat_avaliacao`, `status`,
`fk_consulta`, `fk_altadesistencia`.

### `continuar_terapeuta` / `continuar_allos` são resposta, não estado (Q25)

Este foi o ponto mais sutil do grilling e vale ficar escrito.

**`Avaliacao` é um retrato de um momento.** Se um booleano de uma avaliação antiga for
reescrito meses depois, apaga-se o que a pessoa disse naquele dia — que é justamente o
dado histórico que dá valor à série.

**Regra: resposta imutável, uma por `Avaliacao`, nunca reescrita.**

| Momento | `continuar_terapeuta` | `continuar_allos` |
|---|---|---|
| Entrada | `null` (não se aplica) | `null` |
| 1ª sessão | **resposta do paciente** | `null` |
| Reencaminhamento | `false` (por definição — trocou) | `true` (por definição — ficou) |
| Encerramento | `false` | **resposta do paciente** (pergunta 9) |

**Estado atual do paciente continua onde sempre esteve:** `Paciente.status_atendimento`
(`AGUARDANDO_INICIO` / `ATIVO` / `PAUSADO` / `FINALIZADO`), `Paciente.origem_paciente`
(`NOVO` / `REENCAMINHADO`) e a própria `Altadesistencia`.

### `status` — sem choice novo

Choices atuais: `pendente`, `avaliado`, `nao_respondeu`, `legado`.

```
pendente ──(qualquer resposta capturada)──────► avaliado
    │
    ├────(recusou)───────────────────────────► nao_respondeu
    └────(44h sem resposta)──────────────────► nao_respondeu
```

**Resposta parcial vira `avaliado` (Q34).** Quem respondeu 2 de 8 perguntas e sumiu não é
`nao_respondeu` — marcar assim apagaria essas 2 do radar. **Quem precisa de rigor é o
relatório**, e lá o filtro é "os quatro itens do ORS presentes", não o `status`.

Recusa e silêncio continuam caindo os dois em `nao_respondeu` (decisão anterior mantida).

---

## O terapeuta-sentinela (Q38)

A `Avaliacao` exige `fk_terapeuta`, e **na entrada ainda não há terapeuta** — o cadastro
da Sofia cria um lead sem match, e a coordenação faz o match depois.

**Decisão:** em vez de tornar `fk_terapeuta` nullable, cria-se **um terapeuta "Sem
terapeuta"** que existe só pra marcar; a Thainá realoca depois.

**Por que não nullable:** `fk_terapeuta` é obrigatório desde sempre. Telas, filtros e
relatórios usados por terapeutas e coordenação assumem que ele existe — é a única
alteração da lista com chance real de quebrar produção.

**O que é preciso criar** (é tarefa de dados, não de código — `Terapeuta` tem seis FKs
obrigatórias):

- um `Associado` "Sem terapeuta";
- um `Terapeuta` apontando pra ele, com `fk_decano`, `fk_abordagem`, `fk_nucleo`,
  `fk_clinica` e `fk_modalidade` preenchidos com valores neutros;
- **`is_active = False`**, pra ele não aparecer nos dropdowns de match.

> ⚠️ **A conferir na implementação:** (1) se `is_active=False` não o exclui de alguma
> query que a criação da `Avaliacao` precise; (2) que ele **aparece nas listagens e
> relatórios por terapeuta** como se fosse gente. A média dele não fica poluída (a
> avaliação de entrada não tem nota de terapeuta), mas **a contagem fica** — todo
> relatório por terapeuta deve excluí-lo explicitamente.

---

## Mudanças de comportamento no Hamilton

### `Avaliacao.clean()`

Hoje o método **hard-coda** `momento = 'Após o encerramento da terapia'` sempre que há
`fk_altadesistencia`. Passa a olhar `alta_desistencia`:

```
Solicitação de reencaminhamento  →  'Durante o acompanhamento terapêutico'
alta | desistencia | não responde →  'Após o encerramento da terapia'
```

E precisa **aceitar avaliação sem consulta e sem alta** (a de entrada), caso hoje não
previsto — o `momento` vem preenchido de fora e deve ser respeitado.

### Signals

`criar_avaliacao_alta` passa a gravar o `momento` correto conforme o `alta_desistencia`
(hoje grava sempre encerramento). `criar_avaliacao_consulta` fica como está.

Nenhum signal novo: a avaliação de entrada é criada **pela Sofia**, via endpoint (Q11).

> **Por que não um signal no `Paciente`:** dispararia pra **todo** paciente cadastrado no
> Hamilton, inclusive os que nunca falaram com a Sofia e os que a coordenação cadastra à
> mão. Encheria a fila de pendentes que nunca serão respondidos e sujaria a taxa de
> resposta. **Quem sabe que houve encaminhamento pela Sofia é a Sofia.**

### Endpoints

| Método | Rota | Uso |
|---|---|---|
| `POST` | `/api/v1/avaliacoes/` | **novo** — a Sofia cria a avaliação de entrada |
| `GET` | `/api/v1/avaliacoes/pendentes/` | fila; filtra `sofia_enviada_em__isnull=True` por padrão, `?enviadas=1` traz as já enviadas (lembrete/expiração) |
| `PATCH` | `/api/v1/avaliacoes/<pk>/` | grava respostas + `status`, com **allowlist** de campos (o canal não reescreve vínculo) |

O `GET` precisa devolver: paciente (nome, telefone, **`dat_nascimento`**), `momento`,
`alta_desistencia` e `cancelador` da `Altadesistencia` vinculada.

> 🔒 **Restringir por grupo/permissão, não só `IsAuthenticated`.** São respostas de
> pesquisa de saúde, e hoje **qualquer usuário logado do Hamilton** acessa as rotas da
> Sofia — com token válido por 1 dia. Vale pras 4 rotas antigas também: é problema
> pré-existente, mas agora com dado bem mais sensível trafegando.

---

## Mudanças na Sofia

### `app/services/pesquisa.py`

Passa de **2 para 4 fluxos**: entrada, 1ª sessão, reencaminhamento, encerramento. O
seletor é o `momento` (mais o `alta_desistencia`, pra adaptar o texto no encerramento).

Ganha a criação da avaliação de entrada: cron → guardas → `POST` no Hamilton → dispara a
conversa.

### Prompts

| Arquivo | O que acontece |
|---|---|
| `prompt/pesquisa-entrada.md` | **novo** — ORS + nota da Sofia, com o enquadramento da Q32 |
| `prompt/pesquisa-primeira-sessao.md` | **reescrito** — sai o ORS, entra o encaixe; cai de 11 pra 4 perguntas |
| `prompt/pesquisa-reencaminhamento.md` | **novo** — motivo da troca + ORS + nota do terapeuta anterior |
| `prompt/pesquisa-encerramento.md` | **reescrito** — 9 perguntas, reoferta única com os 3 bloqueios, sem desconto |
| `prompt/pesquisa-conducao.txt` | ajustado — regra de quem responde, e a regra de reoferta que substitui o "nunca reverter" |
| `prompt/pesquisa-extracao.txt` | reduzido — só os campos de texto (o resto vem pela tool) |

### Tool `registrar_resposta_pesquisa` (Q16 + Q33)

**O risco aceito no §C.2 da Demanda C foi reaberto e revertido.** Ele foi aceito quando
eram 11 perguntas soltas; agora o ORS é **um bloco que se invalida inteiro se um dos
quatro itens for extraído errado ou omitido** — e é o número que vai pra prefeitura. Sem
time de qualidade (Q8), **ninguém conferiria**.

**Desenho:** a Sofia chama a tool **a cada resposta** (`campo` + `valor`), gravando
incrementalmente **só os campos numéricos e booleanos**:

`individual`, `interpessoal`, `social`, `geral`, `qualidade_geral`, `nota_indicacao`,
`nota_sofia`, `continuar_terapeuta`, `continuar_allos`, `consentimento_paciente`.

**Por que incremental e não uma chamada no fim:** resposta parcial é o caso comum — a
pessoa responde 3 perguntas e some. Gravando na hora, o que ela disse fica.

**Os campos de texto** (`feedback_livre`, `motivo_encerramento`) continuam saindo da
extração por LLM no fim: errar texto custa pouco, e forçar tool pra texto longo atrapalha
a conversa. O `PATCH` no Hamilton continua no fim, com o acumulado.

### Alertas pra Thainá (Q14 + Q20 + Q29 + Q37)

**Por que isto existe:** não haverá ninguém fazendo controle de qualidade além da Sofia
(Q8). Sem alerta, o desenho **coleta e arquiva** — um `qualidade_geral = 2` ou um
feedback relatando algo grave entrariam no banco e ninguém saberia. **É o alerta que
transforma a pesquisa de custo em produto.**

Usa o template `alerta_thaina`, que já existe.

| Gatilho | Limiar |
|---|---|
| Nota do terapeuta (`qualidade_geral`) | **< 6** |
| Nota da Sofia (`nota_sofia`) | **< 6** |
| NPS Allos (`nota_indicacao`) | **< 6** |
| Encaixe (`continuar_terapeuta = false`) | sempre |
| Reclamação / experiência ruim no motivo do encerramento | sempre (escala) |
| Pessoa aceita continuar na Allos com outro terapeuta | sempre |

**O ORS não gera alerta nenhum.** Decisão explícita: a Sofia **não se intromete** com
nota de ORS — ela segue o fluxo e encaminha pro terapeuta competente. **Crise se detecta
pela descrição clara do paciente**, com o modelo de crise que já existe, **nunca por nota
de ORS**.

**Os limiares ficam em `config_negocio`** (editáveis no `/painel/config`, como preço e
debounce), pra apertar sem deploy caso o volume incomode.

**Sobre o NPS < 6 (Q37):** NPS baixo **sem** nota de terapeuta baixa é o sinal de que o
problema **não é o terapeuta** — é fila, preço ou atendimento inicial. É o único jeito de
isso chegar em alguém.

### Consentimento (Q15)

O paciente passa a ser abordado até **4 vezes**. Pedir "tudo bem te fazer umas
perguntas?" toda vez custa um turno em cada pesquisa — e é o que mais derruba taxa de
conclusão.

**Regra:** consentimento explícito **na entrada**; nas seguintes, **saída fácil** ("se
não quiser responder agora, é só me dizer"), não nova permissão. O
`consentimento_paciente` de cada `Avaliacao` continua sendo gravado — na entrada pelo
aceite explícito, nas seguintes por não ter havido recusa.

### Prazos (mantidos)

| Evento | Quando |
|---|---|
| Convite | primeiro tick do cron após o gatilho (entrada: ≥3h após o cadastro) |
| Lembrete único | 20h sem resposta |
| Encerra como `nao_respondeu` | 44h sem resposta |

Colado na janela de 24h da Meta: o lembrete de 20h sai como texto livre; se a pessoa
responder, a janela reabre.

---

## A planilha da Qualidade (Q8)

**Não vai haver ninguém fazendo controle de qualidade além da Sofia.** A planilha do time
de Qualidade **deixa de existir como fonte** — o Hamilton é a fonte única, e qualquer
planilha vira **relatório descartável exportado de lá**.

Se a Qualidade precisar de coluna que a planilha tinha e o banco não tem, isso é sinal de
**campo faltando no modelo** — e o momento de descobrir isso é agora, antes da migration.

---

## Riscos aceitos e registrados

1. **`qualidade_geral` com duas origens.** Ver o box acima. Mitigação: recorte por
   `sofia_enviada_em`.
2. **A nota da Sofia é auto-avaliação** (Q4). Quem faz a pergunta é a avaliada, e o efeito
   conhecido é nota inflada. **Serve como alarme de desastre, não como medida de
   qualidade** — e **não entra em nenhum relatório externo**. Só na entrada; no
   encerramento a memória é de meses atrás e a nota vira ruído.
3. **O ORS é invisível pro terapeuta** (Q22). Consequência: ele **nunca poderá ajudar o
   paciente que o respondeu** — é exclusivamente indicador agregado de gestão. Decisão
   consciente (evita o terapeuta "trabalhar pra nota"). O desenho fica coerente: **ORS =
   agregado pra gestão e prefeitura; notas de satisfação = alerta individual pra Thainá.**
4. **Quem recusa a entrada fica fora da série pra sempre** (Q17). Não há plano B, por
   escolha: misturar ORS pré e pós-sessão contaminaria exatamente o número que se quer
   defender.
5. **Não é prática deliberada.** Sem medição recorrente durante o processo, é pré/pós.
6. **O terapeuta-sentinela aparece em listagens por terapeuta.** Ver o box acima.

---

## Desistência de quem nunca teve sessão (Q39)

`Altadesistencia.momento` aceita `'Antes da primeira sessão'` — existe encerramento de
paciente que, pelo sistema, nunca foi atendido. Chegou-se a considerar uma versão curta
do questionário pra esse caso.

**Decisão: aplicar as 9 perguntas normalmente**, sem versão especial. Dois motivos:

1. **O `momento` é preenchido por gente e pode estar errado.** Confiar nele pra decidir
   quais perguntas fazer é confiar num dado que ninguém audita.
2. **"Sem sessão lançada" ≠ "sem contato com o terapeuta".** Pode ter havido conversa,
   remarcação, contato fora de sessão — e essa experiência é justamente a que explica a
   desistência precoce.

Se não houver referente, a pessoa responde "não cheguei a ser atendida" — **isso é um
resultado**. Não perguntar é que seria um buraco.

---

## Ordem de implementação sugerida

1. **Terapeuta-sentinela no Hamilton** e varredura de onde ele pode aparecer indevidamente
   — é o item com maior chance de estragar produção; melhor descobrir antes de tudo.
2. **Migration da `Avaliacao`** (campos novos, `continuar_*` nullable, choice novo).
3. **`clean()` + signals** (separar reencaminhamento de encerramento).
4. **Endpoints** (`POST`, `GET pendentes`, `PATCH`) + restrição por grupo + testes.
5. **Sofia:** tool `registrar_resposta_pesquisa`, 4 fluxos em `pesquisa.py`, prompts.
6. **Alertas** + limiares no `config_negocio`.
7. **Métricas derivadas no Hamilton** (última sessão, tempo até o atendimento).
8. **Relatório de ORS pré/pós** — o entregável que justifica tudo isso.

---

## Pendências de ambiente

- ✅ **Branch de teste com dados: existe.** É a branch **`sofia-teste`** do Neon
  (`ep-green-pine-a5wxjxwi-pooler`, database **`hamiton`**), apontada pela
  `DATABASE_HAMILTON_TESTE` do `.env`. Produção é `ep-soft-bread-a5rn2ris-pooler`.
  Timelines distintos, confirmado (`d816d0c2…` × `fdb211ba…`).
  - O database `test_hamiton`, que vive dentro dessa mesma branch, está **vazio e com
    schema atrasado** (25 migrations contra 62). É lixo — apagar, pra ninguém apontar
    pro lugar errado.
  - ⚠️ **A branch é uma cópia de ~07/07/2026**, um mês defasada (576 pacientes contra
    598 em produção). Rodar *Reset from parent* no Neon **antes** de testar a migration.
- ⚠️ **Anonimizar antes de testar o fluxo de pesquisa.** A branch tem telefone real de
  576 pacientes. O objetivo do teste é justamente rodar o cron de pesquisa — com telefone
  real, **a Sofia manda mensagem de verdade pra paciente de verdade**. Scrub obrigatório
  de telefone (e recomendado de nome, e-mail e CPF) logo após cada reset, antes de
  qualquer execução. Dado clínico fica intacto: é ele que precisa ser testado.
- O `.gitignore` do `hamilton-api` **ignorava as migrations** — provável causa de o
  trabalho anterior ter sumido. Conferir antes de commitar a migration nova.
- O código do Hamilton está em `main` no working tree local. **Criar branch própria antes
  de mexer.**

---

## Log das decisões (grilling de 06/08/2026)

| # | Decisão |
|---|---|
| Q1 | Dois instrumentos, blocos separados, ORS primeiro |
| Q2 | ORS intocável — 4 itens congelados |
| Q3 | Cortadas as 3 perguntas deriváveis/duplicadas |
| Q4 | Nota da Sofia colhida na entrada (após o encaminhamento) |
| Q5 | Reencaminhamento vira questionário próprio, `momento` de meio de processo |
| Q6 | Quem sumiu recebe a pesquisa completa |
| Q7 | Refazer o lado Hamilton do zero; corrigir este documento |
| Q8 | Sem time de qualidade — Hamilton é fonte única, planilha vira export |
| Q9 | Entrada dispara após o cadastro (não no acolhimento, não após o match) |
| Q10 | Recusa não se insiste |
| Q11 | Sofia cria a avaliação de entrada por endpoint; choice novo de `momento` |
| Q12 | Sem ORS na pesquisa de 1ª sessão |
| Q13 | Reencaminhamento: 6 perguntas, sem NPS e sem feedback livre |
| Q14 | Alerta pra Thainá **e** fila no painel |
| Q15 | Consentimento na entrada; nas seguintes, saída fácil |
| Q16 | Tool de registro reaberta e aprovada |
| Q17 | Quem recusou a entrada fica fora da série |
| Q18 | ORS só se quem responde é a pessoa atendida; 13-17 responde normal |
| Q19 | ≥3h após o cadastro, com guardas |
| Q20 | Alertas por nota; **ORS não alerta**; crise pelo modelo existente |
| Q21 | Sem `ors_total` — só armazenar |
| Q22 | Terapeuta **não** vê o ORS |
| Q23 | Métricas derivadas ficam no Hamilton |
| Q24/Q30 | `qualidade_geral` = nota do terapeuta, **sem renomear** |
| Q25 | `continuar_*` são resposta imutável, não estado |
| Q26 | Pergunta de **encaixe**, não de troca |
| Q27/Q31 | Reoferta única no encerramento, com 3 bloqueios; **sem desconto** |
| Q28 | Quem responde: capturado no acolhimento, com pergunta de reserva |
| Q29 | Alertas < 6, limiares editáveis no painel |
| Q32 | Enquadramento pessoal-mas-verdadeiro + pitada de técnico |
| Q33 | Tool incremental, só numéricos e booleanos |
| Q34 | Resposta parcial → `avaliado` |
| Q35 | Encerramento fica com 9 perguntas |
| Q36 | `motivo_encerramento` — um campo só |
| Q37 | NPS < 6 também alerta |
| Q38 | Terapeuta-sentinela em vez de `fk_terapeuta` nullable |
| Q39 | Desistência sem sessão recebe as **9 perguntas normais** |
