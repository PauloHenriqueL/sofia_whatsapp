# Demandas encontradas testando manualmente (pós-merge)

Registro corrido dos bugs achados enquanto testávamos as duas branches
mergeadas (`feat/avaliacao-pesquisas-sofia` e `feat/contrato-autentique`)
conversando de verdade com a Sofia, não só rodando a suíte automatizada.
Complementa o `explicacao.md` (que cobre o que já foi achado e corrigido antes
de começar os testes manuais).

Formato: um item por bug. Corrigido = já tem fix aplicado e validado.
Aberto = ainda não resolvido.

---

## 1. ✅ CORRIGIDO — `GET /api/v1/avaliacoes/{pk}/` devolvia 405

**Como apareceu:** conversa de teste (paciente Ana, número `...201`) travou
depois de responder "sim, aceito" ao convite da pesquisa de entrada. A Sofia
ficou 90s sem responder e o `conversar.py` teve que ser interrompido.

**Causa:** `hamilton_client.obter_avaliacao()` na Sofia faz `GET
/api/v1/avaliacoes/{pk}/` a cada turno de conversa em modo pesquisa (é assim
que ela sabe se a pesquisa ainda está em curso e monta o texto certo). Mas a
view `AvaliacaoRespostaAPIView` no Hamilton (`principais/views.py`) foi escrita
só com `http_method_names = ["patch", "options"]`, herdando de
`generics.UpdateAPIView` — que não implementa `GET`. Toda tentativa de leitura
voltava **405 Method Not Allowed**.

O `06-SUBIDA-EM-PRODUCAO.md` já documentava a rota como `GET · PATCH
/api/v1/avaliacoes/{pk}/` — a intenção sempre foi suportar os dois métodos,
só não foi implementada.

**Por que a suíte não pegou:** nenhum teste do Hamilton chama `GET` nessa
rota — só `PATCH` (a via de escrita, coberta pelos testes de
`AvaliacaoRespostaAPIView`).

**Correção aplicada:** troquei `generics.UpdateAPIView` por
`generics.RetrieveUpdateAPIView`, adicionei `"get"` a `http_method_names`, e
`get_serializer_class()` escolhe `AvaliacaoPendenteSerializer` (leitura, já
tem todos os campos que a Sofia usa: `paciente_nome`, `momento`, `status`,
`tipo_saida`, `cancelador`, `sofia_enviada_em`, `sofia_lembrete_em`) para
`GET` e mantém `AvaliacaoRespostaSerializer` (allowlist de escrita) para
`PATCH`.

**Validado:** suíte completa do Hamilton (96 testes) continua passando.
`GET /api/v1/avaliacoes/400/` agora devolve `401` (falta de auth) em vez de
`405` — confirma que o método é aceito.

**✅ Validado ponta a ponta em 19/08:** conversa completa da pesquisa de
entrada (5 perguntas) rodou sem travar. Confirmado no Hamilton: `status =
avaliado`, ORS completo (`individual=8, interpessoal=7, social=9, geral=8`,
os quatro presentes — nenhum faltando), `nota_sofia=9`, `fk_terapeuta=73` (o
sentinela, correto pra essa fase), `momento` certo. `/estado` na Sofia mostra
`pesquisa_avaliacao_id` voltando a `None` (pesquisa finalizada).

---

## 2. Comportamento observado, não é bug — a Sofia não perguntou o endereço

Na mesma conversa, a Sofia pulou direto pra confirmação sem perguntar
endereço. **Isso é esperado**: `tools.py` só exige `nome_completo` e
`data_nascimento` como campos obrigatórios da tool de cadastro — endereço é
opcional de propósito (documentado no código: evitar o modelo inventar dado
tipo "[SEU_NÚMERO]" só pra satisfazer um campo obrigatório). O LLM decide, por
turno, quais perguntas fazer; variação aqui é esperada, não é regressão do
merge.

---

---

## 3. ✅ Validado sem bug — captação real + detecção de parceria (Demanda A)

**Testado em 19/08**, dois cadastros:

- **Paciente comum** (Ana, `...201`, veio pelo Google): `captacao_id`
  resolvido pra um ID real (não mais fixo em "WhatsApp (Sofia)"),
  `is_parceria: false`.
- **Paciente de parceria** (Bia, `...202`, declarou ser servidora da
  Prefeitura de Materlândia): a Sofia identificou sozinha a captação certa
  (`captacao_id=46`, "Prefeitura Materlandia"), sem eu precisar apontar o ID.
  Confirmado no Hamilton: `vlr_sessao = 0.00`, `tipo_pagamento = parceria`,
  `fk_captacao.is_parceria = True`, e a declaração da paciente foi registrada
  em `Paciente.observacao` — a auditoria que o design pede pra caso a
  prefeitura questione um atendimento.
- A pesquisa de entrada foi criada normalmente pra ela (`status=pendente`,
  `fk_terapeuta=73`/sentinela) — confirma que **parceria de terapia comum
  recebe a pesquisa**; só neuro seria pulado (não testado ainda).

Nenhum bug encontrado nesse fluxo.

---

---

## 4. Não é bug — resposta parcial na pesquisa da Bia (parceria)

Durante o teste, um comando `/est` truncado (digitação cortada no terminal)
foi enviado como mensagem real ao bot em vez de reconhecido como comando —
comportamento correto do `conversar.py` (compara `texto == "/estado"`
exatamente; qualquer outra coisa vira mensagem pro bot, documentado no
próprio script). **Isso não aconteceria num paciente real no WhatsApp**, é
só artefato de digitação no teste manual.

Efeito: a pesquisa da Bia foi interrompida antes da última pergunta (nota de
satisfação do acolhimento). Resultado no Hamilton: `status=avaliado`, ORS
**completo** (`8, 8, 7, 7` — os quatro itens presentes, o que importa),
`nota_sofia=None` (a única pergunta não respondida). Isso é exatamente o
comportamento esperado pra resposta parcial (decisão Q34 do
`02-modelo-de-avaliacao.md`: quem responde parte e some vira `avaliado`, não
`nao_respondeu` — perder o que já foi respondido seria pior). **Nenhum bug
aqui.**

---

---

## 5. ✅ Validado sem bug — fluxo de neuro (escalada, sem cadastro nem pesquisa)

**Testado em 19/08** (número `...203`): paciente pergunta preço da avaliação
neuropsicológica, aceita a reunião com a Amanda. Confirmado no banco da Sofia:

- `estado=escalado`, `modo=humano` — assumiu corretamente.
- `paciente_hamilton_id=None` — **não** cadastrou no Hamilton (neuro não passa
  pelo cadastro automático de terapia, vai direto pra reunião com a Amanda).
- `pesquisa_avaliacao_id=None` — nenhuma pesquisa de entrada criada, confirma
  a regra `pesquisa._e_neuro` (linha de base é só pra terapia).
- Escalada registrada com `motivo=neuro_reuniao` e contexto claro.
- A Sofia não pediu nascimento/endereço (corretamente — não fazem parte desse
  fluxo) e informou o preço certo (R$ 1.000).

Nenhum bug encontrado.

---

---

## 6. 🔴 ABERTO — extração da pesquisa mistura respostas de pesquisas diferentes na mesma conversa

**Como apareceu:** testando reencaminhamento (paciente Ana, `...201`, que já
tinha respondido a pesquisa de entrada dias — na simulação, minutos — antes).
Na pesquisa de reencaminhamento ela respondeu ORS `7, 6, 8, 7` + nota do
terapeuta `6`. O `/estado` mostrou `tool gravou {"individual": 7}` — só um
campo gravado pela tool, quando deveriam ser 4 (ORS completo) + 1
(qualidade_geral).

**O que foi parar no banco** (`Avaliacao pk=402`, reencaminhamento):
```
individual=7 interpessoal=7 social=9 geral=8 nota_sofia=9 qualidade_geral=6
```
Comparando com a `Avaliacao pk=400` (entrada, mesma conversa, respondida
antes):
```
individual=8 interpessoal=7 social=9 geral=8 nota_sofia=9 qualidade_geral=9
```
**`interpessoal`, `social`, `geral` e `nota_sofia` do reencaminhamento são
idênticos aos da entrada** — não aos valores que a Ana realmente respondeu
na pesquisa de reencaminhamento (`6, 8, 7`, sem nota_sofia porque essa
pergunta nem existe nesse questionário).

**Causa raiz confirmada:** a extração final por LLM
(`pesquisa.extrair_respostas`) recebe o histórico via
`conversation.carregar_historico(db, conversa, limite=60)` — que traz as
últimas 60 mensagens da **conversa inteira**, sem filtrar pela pesquisa em
curso. Como a mesma pessoa respondeu duas pesquisas na mesma conversa, o
histórico contém a pergunta "quanto você está satisfeita com seus
relacionamentos atuais" **duas vezes**, cada uma com uma resposta diferente
— e o modelo de extração, sem instrução no prompt para diferenciar
pesquisas ou usar só a mais recente, aparentemente pegou a resposta da
pesquisa **errada** (a mais antiga).

**Por que só `individual` foi gravado pela tool:** não investiguei a fundo
(pode ser comportamento do modelo naquele turno, ou algo a mais), mas o
efeito é que a rede de segurança (extração no fim, que deveria só
"preencher buraco" segundo o comentário em `pesquisa.py` linha ~526-528)
sobrescreveu com dado errado em vez de ficar de fora.

**Gravidade:** alta. Isso contamina dado real de ORS — exatamente o cenário
que os docs do projeto tratam como grave ("é o número que vai pra
prefeitura", "ninguém confere"). Uma pessoa respondendo duas pesquisas ao
longo do tempo (entrada + reencaminhamento, ou entrada + encerramento) é
cenário normal, não extremo.

**Hipóteses de correção a avaliar** (não implementei ainda, quero validar
antes qual abordagem é a certa):
1. Limitar `carregar_historico` no fluxo de extração a só as mensagens
   **depois** de `conversa.pesquisa_iniciada_em` (timestamp já existe na
   coluna).
2. Adicionar ao prompt de extração uma instrução explícita: "considere só a
   pesquisa mais recente/em andamento; se uma pergunta aparecer respondida
   mais de uma vez, use a última ocorrência".
3. As duas juntas — cortar o histórico é mais robusto (não depende do LLM
   "entender" a instrução) e devia ser a correção principal.

**✅ CORRIGIDO em 19/08.** Apliquei a opção 3 (a combinada):

1. `conversation.carregar_historico` ganhou parâmetro opcional `desde:
   datetime | None` — filtra `Mensagem.criada_em >= desde` na query.
2. As duas chamadas em `pesquisa.py` (condução do turno e extração final)
   passam `desde=conversa.pesquisa_iniciada_em`, cortando qualquer mensagem
   de pesquisas anteriores já concluídas.
3. `prompt/pesquisa-extracao.txt` ganhou uma regra 7 explícita: "use só a
   pesquisa mais recente" — reforço, caso outra fonte de mistura apareça no
   futuro (ex.: histórico maior que o `limite=60` em conversas muito longas).

Suíte completa (737 testes) segue passando depois da mudança. **Ainda não
retestei manualmente** o cenário exato (Ana com entrada + reencaminhamento)
pra confirmar que os valores corretos são gravados agora — próximo passo.

---

---

## 7. ✅ CORRIGIDO (config, não código) — `SOFIA_API_DATABASE_URL` de produção apontava pro banco de teste

**Gravidade: alta.** Achado testando pelo celular contra a Sofia de
**produção** real (26/08). Dois cadastros (nome "Ze pequeno", telefone
`553183055118`; nome "Victor Abdallah Rogana", telefone `553180530059`)
apareceram na tela "Editar Paciente" do Hamilton como pessoas completamente
diferentes (Pedro Henrique Silva de Paiva #639, João Vítor de Araújo Bastos
#632) — parecia alucinação do LLM.

**Causa raiz confirmada:** não é o LLM. `SOFIA_API_DATABASE_URL` no `.env` de
**produção** do Hamilton (Render) estava setada apontando pro banco de
**teste** (`ep-green-pine`), em vez de vazia/produção (`ep-soft-bread`). Isso
faz `SOFIA_DB_ALIAS = "sofia_api"`, e a maioria dos endpoints que a Sofia
consome (busca por telefone, atualização, status de primeira consulta) passa
a ler/escrever no banco de teste — só `PacienteCreateAPIView` não respeita o
alias (usa `queryset = models.Paciente.objects.all()` sem `.using()`), então
a criação em si ia parar em outro lugar dependendo do caminho. Resultado:
pacientes reais cadastrados pela Sofia foram parar no banco de teste, com
IDs que colidem com pacientes reais diferentes no banco de produção — dois
sequenciais de PK independentes, mesmo número, pessoas diferentes.

Confirmado com certeza: os pacientes #639 ("Ze pequeno") e #632 ("Victor
Abdallah Rogana") existem exatamente com esses nomes **no banco de teste**
(`ep-green-pine`) — provando que o cadastro da Sofia funcionou certo, só que
gravou no banco errado.

Isso é exatamente o risco que o `CLAUDE.md` do projeto já documentava: *"Se
[`SOFIA_API_DATABASE_URL`] estiver setada lá, todo cadastro vindo da Sofia —
inclusive paciente real — vai pro banco de teste."* Estava setada, e
aconteceu.

**Efeito colateral, mesma causa:** a cobrança automática (Stripe) também
parou de funcionar — `cobranca._elegivel()` consulta
`status_primeira_consulta` no Hamilton pra saber se pode cobrar, e essa
consulta ia pro banco de teste (onde não há sessão real marcada pra
ninguém), então a condição nunca batia e a Sofia nunca cobrava.

**Correção:** o Paulo apagou `SOFIA_API_DATABASE_URL` no Render do Hamilton
(deploy automático). Confirmado funcionando: `POST /tasks/cobrancas` voltou
a enviar cobrança real (`{"enviadas": 1}`) logo depois da correção.

**Decisão do Paulo:** os dois cadastros de teste ("Ze pequeno", "Victor
Abdallah Rogana") ficam como estão no banco de teste — não precisam ser
recriados em produção, eram testes vazios.

**Pendência residual:** a conversa 34 (o teste do "Ze pequeno") continua
aparecendo como candidata a cobrança no banco da Sofia
(`paciente_hamilton_id=639` aponta pro paciente de teste), mas nunca vai
disparar de verdade porque o Hamilton de produção não confirma primeira
consulta pra esse paciente inexistente lá. Não é bug — só não foi
arquivada/resolvida manualmente no painel.

---

*(Próximos achados vão sendo adicionados aqui conforme os testes continuam.)*
