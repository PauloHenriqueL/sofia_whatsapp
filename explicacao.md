# O que mudou com as duas branches — e o que quebrou no caminho

**Data:** 18/08/2026
**Escopo:** merge de `feat/avaliacao-pesquisas-sofia` (08/08) e `feat/contrato-autentique`
(18/08) no `hamilton-api`, mais o que isso exige do lado da Sofia.

---

## Resumo em uma frase

Os dois merges **já estavam no GitHub** (`origin/main` de ambos os repos), na ordem certa,
mas o merge deixou **quatro defeitos concretos** que faziam a aplicação do Hamilton
não subir e a suíte de testes não rodar. Os quatro foram corrigidos agora, com os
dois repos locais atualizados e as duas suítes passando limpo (Hamilton: 96/96,
Sofia: 737/737). Nada foi feito em banco de produção.

---

## Estado que encontrei

- **hamilton-api**: `origin/main` já tinha os PRs #133, #134 e #135 mergeados
  (`feat/sofia-captacao-e-avaliacao` → `feat/avaliacao-pesquisas-sofia` →
  `feat/contrato-autentique`, nessa ordem, com merge-back de `main` entre eles).
  O checkout local estava no branch `feat/sofia-captacao-e-avaliacao`, 26 commits
  atrás do `main` remoto.
- **sofia**: o `main` local estava 22 commits atrás do `origin/main`. O trabalho de
  integração do lado da Sofia (inclusive a Demanda E — contrato pelo celular) já
  estava todo lá.
- Ambos os diretórios locais foram atualizados (`git checkout main && git pull
  --ff-only`) — sem merge manual, porque o merge já tinha acontecido no GitHub.

## Por que a suíte "verde" das branches não pegou os problemas

O documento `docs/demandas/06-SUBIDA-EM-PRODUCAO.md` (que o time já tinha escrito
antes deste ciclo) é preciso sobre a arquitetura e as decisões de produto, mas foi
escrito **olhando o conteúdo de cada branch isoladamente** — não o resultado do merge
já feito no GitHub. Os quatro bugs abaixo só existem *depois* do merge; nenhuma das
duas branches, sozinha, os tinha.

---

## Os quatro defeitos encontrados e corrigidos

### 1. Rota da API duplicada — a aplicação não subia

`principais/urls.py` tinha a rota `avaliacoes/pendentes/` **declarada duas vezes**:
uma vinda de `feat/avaliacao-pesquisas-sofia` (linha 70, apontando pra uma view
`AvaliacaoPendenteListAPIView` que **não existe** em `views.py`) e outra vinda de
`feat/contrato-autentique` (linha 94, apontando pra `AvaliacaoPendentesAPIView`, que
existe, com um comentário explícito sobre ordem de rotas). O merge manteve as duas
em vez de escolher uma.

**Efeito:** `python manage.py check`, `showmigrations`, `test` — qualquer comando
Django — quebrava na hora de importar `urls.py`, com `AttributeError`. A aplicação
inteira não subia.

**Correção:** removida a entrada morta (linha 70), mantida a que referencia a view
que de fato existe.

### 2. Migration de dado chamando função que não existe

`acessorios/migrations/0002_captacao_is_parceria.py` tinha uma linha
`migrations.RunPython(marcar_parcerias, desmarcar_parcerias)`, mas essas duas
funções **não estavam definidas em lugar nenhum do arquivo**. Investigando o
histórico: a versão original de `0002` definia essas funções inline; um commit de
correção posterior (`e43c67e`) separou o trabalho de dado num arquivo novo
(`0003_marcar_parcerias_existentes.py`, com funções `marcar`/`desmarcar` — mesma
lógica, nomes diferentes), mas ao enxugar o `0002` esqueceram de remover a chamada
antiga.

**Efeito:** qualquer comando que carregasse o grafo de migrations completo (`test`,
`migrate`) quebrava com `NameError`.

**Correção:** removida a linha órfã do `0002`. A lógica de fato já roda no `0003`,
que depende do `0002` e está correto.

### 3. Duas migrations concorrentes numeradas `0005`, sem religar a cadeia

A descrição do PR de `feat/contrato-autentique` **já avisava** sobre isso: a
migration do contrato ficou em disco como `principais/0005_contratopaciente.py` e
colidiria com `0005_terapeuta_sentinela.py`, vinda da outra branch. A instrução
registrada era clara — renomear para `0008` e trocar a dependência para
`0007_permissao_api_sofia` — **mas isso não foi feito antes do merge**. O arquivo
seguiu como `0005_contratopaciente.py`, com a dependência antiga
(`0004_backfill_vinculo_stripe`).

**Efeito:** `python manage.py test` recusava rodar: *"Conflicting migrations
detected; multiple leaf nodes"*.

**Correção:** renomeado e a dependência religada, seguindo exatamente a instrução
que já estava documentada.

### 4. Um segundo arquivo de migration órfão, não documentado em lugar nenhum

Ao corrigir o nº 3, o mesmo erro apareceu de novo — só que num arquivo que **nenhum
documento do projeto menciona**: `principais/0005_avaliacao_respostas_pesquisa.py`.
Também dependia direto do `0004`, fora da cadeia.

Investigando o conteúdo: esse arquivo cria 11 colunas na `Avaliacao`. Cinco delas
(`feedback_livre`, `nota_indicacao`, `nota_sofia`, `sofia_enviada_em`,
`sofia_lembrete_em`) **também são criadas** por `0006_avaliacao_respostas_pesquisa.py`
— que já estava na cadeia correta. As outras seis (`nota_terapeuta`,
`atendimento_rapido`, `atendimento_rapido_bool`, `indicaria_allos`,
`indicaria_allos_bool`, `motivo_interrupcao`, `dat_ultima_sessao`) só existiam
nesse arquivo órfão — mas as colunas correspondentes **existem em `models.py`**, ou
seja, precisavam ser criadas por alguém.

Isso tem cheiro de duas pessoas trabalhando na mesma tabela em momentos diferentes
do ciclo (um migration mais antigo, de 06/08, e um mais novo — mesmo nome-base,
gerado de novo — de outra data), sem que um soubesse do outro.

**Efeito:** mesmo erro do nº 3 — dois leaf nodes conflitantes, suíte recusava rodar.
Sem essa correção, se alguém tivesse resolvido *só* o conflito do contrato (nº 3)
sem notar este, o próximo `migrate` teria tentado criar as mesmas 5 colunas duas
vezes e quebrado com `column already exists`.

**Correção:** o arquivo órfão foi reescrito para conter só as 6 colunas exclusivas
(sem duplicar as 5 que já existem no 0006) e encaixado na cadeia logo após o
`0005_terapeuta_sentinela`, empurrando a numeração de tudo depois dele. A cadeia
final:

```
0004 → 0005_terapeuta_sentinela
     → 0006_avaliacao_campos_qualidade   (era o "0005" órfão, com 6 campos)
     → 0007_avaliacao_respostas_pesquisa (era o "0006", 6 campos + 3 AlterField)
     → 0008_permissao_api_sofia          (era o "0007")
     → 0009_contratopaciente             (era o "0005_contratopaciente" / "0008")
     → 0010_alter_avaliacao_...          (gerada agora — ver abaixo)
```

Depois de reencadear, `makemigrations --check` acusou uma divergência real e
pré-existente entre `models.py` e as migrations: três campos (`feedback_livre`,
`nota_indicacao`, `nota_sofia`) tinham `verbose_name`/`help_text` diferentes entre a
versão do arquivo órfão (mais descritiva, é a que ficou valendo em `models.py`) e a
versão do `0006/0007` (mais enxuta). Não é um problema que eu introduzi — já existia
antes, escondido pelo conflito de leaf nodes. Gerei a migration `0010` que o próprio
Django propôs para alinhar os dois.

### 5. (Achado testando de verdade, não pela suíte) `fk_captacao` quebrava todo cadastro com captação real

Depois de corrigir os quatro bugs acima e migrar a branch `sofia-teste` do Neon,
testei uma conversa real via `scripts/conversar.py` — LLM de verdade, Hamilton
local de verdade. **O cadastro falhava com 500** assim que a Sofia informava uma
captação (o caso comum: praticamente todo cadastro tem captação).

**Causa:** `principais/serializers.py`, `PacienteIntakeSerializer`. O campo
`fk_captacao` é `IntegerField` cru de propósito (documentado: evita problema de
router com o alias `SOFIA_DB_ALIAS`). Mas o `create()` tinha **duas resoluções
concorrentes**:

1. Um `get_fields()` que setava `.queryset` no campo — instrução que
   `IntegerField` simplesmente ignora (isso é atributo de
   `PrimaryKeyRelatedField`). Código morto, não fazia nada.
2. Logo em seguida, `captacao = validated_data.get("fk_captacao") or
   defaults["captacao"]` seguido de `captacao.is_parceria` — tratando o valor
   como se já fosse o objeto `Captacao`, quando na verdade ainda era o `int` cru.
3. Só **depois**, seis linhas abaixo, vinha a resolução manual correta
   (`Captacao.objects.using(using).filter(pk=captacao_id).first()`) — tarde
   demais, o `AttributeError` já tinha estourado antes de chegar lá.

Tem cheiro de refatoração incompleta: alguém trocou `PrimaryKeyRelatedField`
por `IntegerField` (pelo motivo certo, documentado no comentário) mas não
atualizou as duas partes do código que ainda tratavam o valor como objeto já
resolvido.

**Por que a suíte não pegou:** os 96 testes do Hamilton passam porque nenhum
deles testa `POST /pacientes/` com uma captação real preenchida — só com o
campo ausente, caso em que `captacao = None or defaults["captacao"]` pula
direto pro objeto default e nunca passa pelo `int`. É o mesmo padrão que os
documentos do projeto (`05-contrato-assinatura.md`, `06-SUBIDA-EM-PRODUCAO.md`)
já registraram várias vezes: "mock não pega API quebrada", "a suíte estava
verde e a feature morta".

**Correção:** reordenei o `create()` pra resolver `fk_captacao` (ID → objeto,
com `using=` certo) **antes** de qualquer uso de `.is_parceria`, e removi o
`get_fields()` morto. Suíte completa (96 testes) continua passando depois da
correção — como esperado, já que nada testava esse caminho antes.

**Validado com cadastro real** na branch `sofia-teste`: paciente criado
(`paciente_id=635`), `captacao_id` resolvido certo, `is_parceria=false`.

### Bônus: um teste com nome de variável indefinida

`principais/tests_sofia_api.py`, `test_cria_lead_sem_terapeuta_com_defaults`, usava
`Paciente.objects.using(_DB).get(...)` — `_DB` não existe em lugar nenhum do
arquivo (todo o resto do arquivo usa `Paciente.objects.get(...)` direto). Parece
sobra de copiar-e-colar de outro contexto. Troquei para `Paciente.objects.get(...)`,
consistente com o resto do arquivo.

---

## O que **não** quebrou

- **Lógica de negócio de nenhuma das duas Demandas.** Os quatro problemas são todos
  de "encaixe do merge" (rotas duplicadas, cadeia de migration, teste com nome
  errado) — nenhum é uma regra de negócio errada. Captação real, `is_parceria`,
  fila de avaliação, contrato via Autentique: o código de cada feature, isolado,
  está como cada branch entregou.
- **A suíte da Sofia** (737 testes) passou sem nenhuma mudança — o problema era
  inteiro do lado do Hamilton.
- **`.gitignore` de migrations.** Já estava corrigido antes deste ciclo (o
  `13b3751 fix(git): para de ignorar as migrations` já tinha entrado). Todas as
  migrations renomeadas agora aparecem como `git status` normal, sem precisar de
  `git add -f`.
- **Nenhum banco de produção foi tocado.** Todos os comandos rodados contra o Neon
  (`check`, `showmigrations`) são somente leitura. `migrate` **não** foi executado
  em lugar nenhum. A suíte de testes roda inteira em SQLite in-memory
  (`manage.py test` não usa `DJANGO_TEST_DB=remoto`).

## Nota sobre o CLAUDE.md — o que veio no meu contexto estava desatualizado

O texto do `CLAUDE.md` que eu tinha em cache no início desta sessão (via
system-reminder) dizia "Demanda D não iniciada — nada de Stripe foi tocado". Isso
está **errado desde a última rodada de commits**: a Demanda D (Stripe + Pix) já
está implementada (`app/services/cobranca.py`, commit `bb22140` e outros), com a
flag `cobranca_ativa` nascendo desligada por padrão. O `CLAUDE.md` **no disco**,
depois do `git pull` feito nesta sessão, já está correto — inclusive já se
autocorrige duas vezes no topo, avisando que as seções de status "já ficaram
velhas duas vezes" e recomendando conferir no código antes de confiar num "não
existe" registrado ali. Não editei nada nele: só registro aqui que a versão em
cache que eu tinha era antiga, e a lição de "confira no código, não no
CLAUDE.md" já está documentada pelo próprio time.

## O que ainda está em aberto (não é bug — é decisão)

Os desenhos de produto (modelo de avaliação e contrato via Autentique) **já foram
fechados em grilling** e estão implementados — não são mais "em aberto" no
sentido de decisão de negócio pendente. O que resta é operacional/financeiro; ver
`perguntas.md` para a lista levada aos gerentes de produto. Resumo rápido:

1. **Frontend do contrato** — `ContratoPaciente` não aparece em tela nenhuma do
   Hamilton (admin, card na página do paciente, download do PDF). É o único bloco
   que precisa de código novo, não só configuração.
2. **Env vars, webhook da Autentique e crons** — nada disso foi ligado ainda.
   `AUTENTIQUE_SANDBOX`, `AUTENTIQUE_WEBHOOK_SECRET`, o webhook no painel da
   Autentique, e os crons `/tasks/stripe`, `/tasks/pesquisas`, `/tasks/cobrancas`
   continuam pendentes — ver `docs/demandas/06-SUBIDA-EM-PRODUCAO.md`, Bloco 4.
3. **`SOFIA_API_DATABASE_URL`** precisa ser conferida no Render antes de qualquer
   `migrate` real — se estiver setada, os endpoints da Sofia leem/gravam num banco
   diferente do `default`.
4. **Dinheiro real parado** — cron do Stripe possivelmente não configurado,
   assinatura duplicada da Tatiane (~R$388), faturas em aberto. Ver
   `docs/demandas/04-PENDENCIAS-ABERTAS.md` e `perguntas.md`.

## Próximos passos técnicos (não incluídos aqui)

- Rodar `git diff` fino nos quatro arquivos corrigidos antes de commitar, pra
  confirmar que nada além do descrito mudou.
- Commitar as correções (renomeações de migration + fixes) — ainda não commitado
  nesta sessão.
- Aplicar as migrations no banco real, seguindo a ordem de execução do
  `06-SUBIDA-EM-PRODUCAO.md`, com o `timeline_id` conferido antes.
