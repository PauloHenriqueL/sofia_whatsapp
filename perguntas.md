# Perguntas para os gerentes de produto

**Data:** 18/08/2026.
**Contexto:** depois de verificar tecnicamente o merge das branches
`feat/avaliacao-pesquisas-sofia` e `feat/contrato-autentique` (detalhes em
`explicacao.md`), sobram decisões que não são técnicas — são de dinheiro,
prazo e dono. Esta lista é o resultado de um grilling sobre o que ainda está
genuinamente em aberto.

**O que NÃO está aqui:** o modelo de perguntas da pesquisa de satisfação
(`docs/demandas/02-modelo-de-avaliacao.md`) e o desenho do contrato via
Autentique (`docs/demandas/05-contrato-assinatura.md`) já foram fechados em
grilling anteriores (Q1-Q39 e riscos aceitos, respectivamente) e estão
implementados. Não são mais perguntas — são decisões registradas.

---

## ✅ 1. Assinatura duplicada da Tatiane — resolvido (fora de escopo)

**Fato:** duas assinaturas Stripe criadas com 3min35s de diferença, mesmo
e-mail (`tatiane_ads@yahoo.com.br`), 2 pagamentos cada. Total pago: **R$
775,79**, quando deveria ser R$ 387,90 (uma assinatura).

**Status (21/08):** marcado como resolvido / fora do escopo deste ciclo, a
pedido do Paulo. Não requer mais ação da Sofia ou deste time.

---

## ✅ 2. Cron `/tasks/stripe` — resolvido

**Status (21/08):** criado no cron-job.org (`Sofia stripe`, diário às 4h,
header `X-Tasks-Token`). Testado manualmente com `?simular=1` antes de criar
— respondeu `200`, `{"ja_limitadas": 11, "planejadas": []}`. Não requer mais
ação.

---

## ✅ 3. Régua de inadimplência — decidido: não vale, foco é outro

**Decisão do Paulo (21/08):** não justifica investir numa régua de
inadimplência manual/lembrete. O que importa é garantir que a cobrança
automática do parcelado (5x no cartão, por exemplo) **pare sozinha** na
última parcela — sem isso, o cliente seguiria sendo cobrado indefinidamente,
o que é o problema real.

**Isso já funciona.** Confirmado nesta sessão: o mecanismo
(`app/services/pagamentos.py`, `limitar_parcelado`) já lê `parcelas_total`
do metadata da assinatura Stripe e marca `cancel_at` quando o número de
parcelas pagas bate o total. O cron `POST /tasks/stripe` (item 2, criado
hoje) roda diariamente e aciona isso — testado com `?simular=1`:
`{"ja_limitadas": 11, "planejadas": []}`, confirmando que as 11 assinaturas
antigas já foram travadas e nenhuma nova está pendente. Não requer mais
ação.

---

## ✅ 4. Sequenciamento de ir ao ar — dono definido: Victor

**Decisão do Paulo (21/08):** o Victor é o gerente de produto e decide
quando cada flag liga (`cobranca_ativa` já está ligada, confirmado nesta
sessão; `contrato_ativo` fica pra quando ele decidir). Não é mais uma
pergunta em aberto sobre "quem decide" — o dono está definido.

**O que muda o foco agora:** não é mais "quem decide ligar", e sim "o que
falta construir/testar pra que, quando o Victor decidir ligar, funcione de
verdade". Isso está nos itens 6 e 7 (Autentique + tela do contrato) —
**adiados a pedido do Paulo, retomar depois.**

---

## ✅ 5. `LINK_CURTO_BASE` no Render — confirmado, já estava setada

**Status (21/08):** conferido no `.env` de produção — `LINK_CURTO_BASE=
https://allos.org.br/p` já está configurada. Não requer ação.

---

## ⏸️ 6. Token da Autentique (sandbox) — ADIADO, retomar depois

**Fato:** não há `AUTENTIQUE_TOKEN` configurado em nenhum `.env` do
Hamilton. Sem ele, toda chamada de `/api/v1/contratos/` volta **503**
(comportamento esperado e seguro — não é bug), então o fluxo do contrato
ainda não foi testado de ponta a ponta neste ambiente local.

**Status (21/08):** discutido — o token se gera em
[autentique.com.br](https://www.autentique.com.br), na conta do Victor
(`victorabdallah6@gmail.com`), em Configurações → Token de acesso/API,
usando o ambiente sandbox. **Adiado a pedido do Paulo** — retomar quando a
Demanda E (contrato) voltar a ser prioridade.

## ⏸️ 7. Frontend do `ContratoPaciente` — ADIADO, decidido onde construir

**Fato:** a API do contrato funciona ponta a ponta, mas não existe nenhuma
tela (nem admin do Django, nem template) — ninguém vê o contrato assinado
sem entrar direto no banco.

**Decisão (21/08):** construir **no Hamilton**, não na Sofia — o
`ContratoPaciente` (PDF assinado em bytes, CPF colhido na assinatura,
`vlr_sessao` atualizado) mora só no banco do Hamilton, e é lá que vive o
resto do prontuário/contabilidade que a coordenação já usa. A Sofia só teria
acesso via API REST (dois endpoints já prontos:
`GET /api/v1/contratos/pendentes/` e `GET /api/v1/contratos/?paciente_id=`),
sem acesso direto ao banco. **Adiado a pedido do Paulo** — retomar junto do
item 6.

---

## ✅ 7. Bug real: a extração da pesquisa mistura respostas de pesquisas diferentes na mesma conversa — CORRIGIDO

**Fato:** testando manualmente o fluxo de reencaminhamento (19/08), um
paciente que já tinha respondido a pesquisa de entrada antes teve o ORS do
reencaminhamento **gravado com os valores errados** — três dos quatro itens
(`interpessoal`, `social`, `geral`) e a `nota_sofia` saíram idênticos aos da
pesquisa de entrada anterior, não aos que a pessoa realmente respondeu dessa
vez. Detalhe completo, com os números e a causa raiz confirmada, em
`demandas-teste-manual.md` (item 6).

**Causa:** a extração final por LLM (`pesquisa.extrair_respostas`) lê até 60
mensagens da conversa **inteira**, sem filtrar pela pesquisa em curso. Se a
mesma pergunta aparece duas vezes no histórico (uma por pesquisa), o modelo
pode responder com o valor da ocorrência errada — e não há instrução no
prompt (`prompt/pesquisa-extracao.txt`) dizendo pra usar só a mais recente.

**Por que isso não é uma pergunta de produto, e sim uma decisão técnica:**
não é "se" corrigir — isso contamina dado real de ORS, que é justamente o
número que vai pra relatório de prefeitura/edital (`02-modelo-de-avaliacao.md`
é explícito sobre a gravidade de errar esse dado). A pergunta é **qual
abordagem** seguir:

1. Cortar o histórico da extração pra só mensagens depois de
   `conversa.pesquisa_iniciada_em` (a coluna já existe).
2. Só ajustar o prompt de extração pra instruir "use a ocorrência mais
   recente".
3. As duas — cortar o histórico é a correção mais robusta (não depende do
   modelo "entender" a regra certo toda vez); o ajuste de prompt seria reforço.

**Status (19/08):** implementada a opção 3 (cortar histórico por
`pesquisa_iniciada_em` + reforço no prompt). Suíte completa passando, já em
produção. Não requer mais decisão.

---

## 🔴 8. Limitação de design: um número de WhatsApp só pode ter UM paciente vinculado por vez

**Fato (achado em 27/08, investigando dúvida do Paulo):** `conversa.numero_whatsapp`
é **único** no banco (`unique=True`) — existe só uma linha `Conversa` por número
de WhatsApp, com um único campo `paciente_hamilton_id`.

**O que acontece no cenário real (pai + filha, mesmo número):**
1. Pai marca terapia → `conversa.paciente_hamilton_id` aponta pro pai.
2. Meses depois, mesma pessoa liga de novo pra marcar terapia pra filha (nome
   diferente) → `cadastro.cadastrar_paciente` **sobrescreve**
   `conversa.paciente_hamilton_id` pro paciente novo (filha). O vínculo com o
   pai se perde **nessa conversa**.
3. Se o pai for reencaminhado depois e tiver uma nova "primeira consulta"
   marcada no Hamilton, **a Sofia não fica sabendo** — ela só consulta
   `status_primeira_consulta` pro `paciente_hamilton_id` atual da conversa
   (a filha). O pai fica sem conversa própria vinculada: não recebe cobrança
   automática, não recebe pesquisa de satisfação, não aparece nos alertas
   "precisa de você agora" pra esse caso.

**Não é alucinação nem bug de execução** — é limitação de design do modelo
(`conversa` 1:1 `paciente`, mas 1 número de WhatsApp pode representar uma
família inteira falando por várias pessoas). Provavelmente afeta qualquer
família que usa o mesmo número pra mais de uma pessoa (comum: pais
marcando pra filhos menores).

**Pergunta:** vale a pena tratar isso agora, ou é caso raro o suficiente pra
ficar como risco aceito (documentado, resolvido manualmente pela Thainá
quando aparecer)? Se for tratar, a solução exigiria repensar o modelo — por
exemplo, permitir múltiplos `paciente_hamilton_id` por conversa (WhatsApp),
com a Sofia perguntando "é sobre você ou sobre outra pessoa?" a cada novo
assunto — mudança de escopo maior, não é ajuste pequeno.

---

## Como usar este documento

**Resolvidos (não precisam mais de ação):** 1 (Tatiane), 2 (cron Stripe),
5 (`LINK_CURTO_BASE` — já estava setada em produção), 7 (bug da extração).

**Ainda em aberto:** o item 3 e 4 são de prazo/prioridade — não bloqueiam
nada tecnicamente, mas sem uma decisão explícita tendem a ficar paradas
indefinidamente (como já aconteceu antes neste projeto). O item 6 bloqueia
só o teste local do contrato, não a operação em si.
