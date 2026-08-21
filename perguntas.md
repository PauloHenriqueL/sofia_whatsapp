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

## 🔴 1. Assinatura duplicada da Tatiane — decisão de estorno

**Fato:** duas assinaturas Stripe criadas com 3min35s de diferença, mesmo
e-mail (`tatiane_ads@yahoo.com.br`), 2 pagamentos cada. Total pago: **R$
775,79**, quando deveria ser R$ 387,90 (uma assinatura). Tem cara de link
gerado duas vezes e ela pagou os dois, sem perceber.

**Pergunta:** cancelamos a assinatura duplicada e **estornamos** os ~R$ 388
pagos a mais, ou só cancelamos a partir de agora sem devolver o que já foi
cobrado?

⚠️ **Destacado a pedido do Paulo — decisão financeira sensível, não é para
ele resolver sozinho.** Precisa de aprovação explícita do gerente/financeiro
antes de qualquer ação no Stripe.

---

## 🔴 2. Cron `/tasks/stripe` — confirmar se está no ar

**Fato:** o endpoint que trava o parcelado do neuro (`limitar_parcelado`) está
no ar e testado desde 13/08, mas **não havia nenhum agendador chamando ele**
naquela data. Sem esse cron, toda assinatura de parcelado nova **cobra pra
sempre** — já aconteceu antes com 18 assinaturas (uma cobrou 5 parcelas num
plano de 4). Há pelo menos um link novo em circulação
(`allos.org.br/p/ej3uvrc`, Pedro Luiz, 4x de R$ 250) que corre esse risco.
Margem estimada: ~30 dias após o pagamento da 4ª parcela, não horas.

**Pergunta:** o job `POST /tasks/stripe` (diário, `X-Tasks-Token`) já foi
criado no cron-job.org depois de 13/08? Se não, **quem cria — só dá para
fazer com a sessão logada do Paulo no cron-job.org**, então não é algo que a
Sofia resolve sozinha.

**Como verificar rapidamente:**
```
curl -X POST "https://sofia-whatsapp.onrender.com/tasks/stripe?simular=1" \
     -H "X-Tasks-Token: <TASKS_TOKEN>"
```

---

## 3. Régua de inadimplência — automatizar ou manter manual?

**Fato:** hoje não existe nenhuma automação para cobrança recorrente do 2º mês
em diante — é 100% trabalho manual da Thainá/financeiro. Duas faturas em
aberto registradas em 13/08 (Jessica Josefa, desde 09/08; Eduardo Captein,
desde 10/08) ilustram isso.

**Pergunta:** vale investir em uma régua automática de inadimplência (ex.:
alerta pra Thainá em N dias de atraso, ou mensagem automática da Sofia pro
paciente), ou o volume atual (poucos casos por mês) não justifica o
investimento e o processo manual continua sendo aceitável?

---

## 4. Sequenciamento de ir ao ar — quem é dono de cada passo, e até quando

**Fato:** três flags nascem desligadas por design (`SOFIA_PESQUISAS_ATIVAS`,
`cobranca_ativa`, `contrato_ativo`), com a recomendação explícita de ligar a
cobrança sozinha por algumas semanas antes do contrato. Isso é uma decisão já
tomada — o que falta é execução, e ela depende de passos humanos sem prazo
nem dono formal:

| Passo | Quem, hoje (implícito) | Prazo |
|---|---|---|
| Aprovar o texto final do contrato (`.docx`) | Paulo | nenhum |
| Assinar um contrato real em sandbox (revela problemas que mock não pega) | Victor | nenhum |
| Migrar a conta Autentique de pessoal para institucional | Victor / Paulo | nenhum |
| Cadastrar o webhook no painel da Autentique | quem tiver acesso ao painel deles | nenhum |
| Ligar `cobranca_ativa` | ? | nenhum |
| Esperar "algumas semanas" | — | quantas, exatamente? |
| Ligar `contrato_ativo` | ? | nenhum |

**Pergunta:** quem os gerentes de produto querem como dono de cada passo
acima, e existe uma data-alvo? Sem isso, o risco registrado no próprio
`06-SUBIDA-EM-PRODUCAO.md` é repetir o que já aconteceu com a Demanda A —
ficar "pronta" por semanas sem ninguém ligar de fato.

---

## 5. `LINK_CURTO_BASE` no Render — variável ainda não setada

**Fato:** o link curto de pagamento já funciona ponta a ponta
(`allos.org.br/p/xxxxxxx` → redireciona certo), mas a variável de ambiente
`LINK_CURTO_BASE=https://allos.org.br/p` ainda não foi configurada no Render.
Sem ela, os links saem com o domínio `onrender.com` — funcionam igual, mas
têm mais cara de golpe no WhatsApp (o problema que o link curto existe pra
resolver).

**Pergunta:** não é uma decisão de produto — é só confirmar que ninguém
esqueceu. Alguém pode setar essa variável no painel do Render? (Salvar
dispara um redeploy automático, então não é uma ação "grátis" — vale fazer
junto de outra mudança, se possível.)

---

## 6. Token da Autentique (sandbox) — bloqueando o teste local do contrato

**Fato:** testando o ambiente local depois do merge, não há `AUTENTIQUE_TOKEN`
configurado em nenhum `.env` do Hamilton. Sem ele, toda chamada de
`/api/v1/contratos/` volta **503** (comportamento esperado e seguro — não é
bug), mas isso significa que **o fluxo do contrato (Demanda E) ainda não foi
testado de ponta a ponta neste ambiente local**, mesmo com o código já
corrigido e migrado.

**Pergunta:** quem tem acesso à conta da Autentique (hoje pessoal, do Victor —
ver `05-contrato-assinatura.md`) pra gerar um token de sandbox e passar pra
configuração local? Sem isso, a validação prática do contrato (que é
justamente o tipo de teste que pegou os dois bugs sérios do fluxo — CPF em
`user_data`, prazo de assinatura que a Autentique ignora silenciosamente — no
dry run de 17/08) fica bloqueada.

---

## 🔴 7. Bug real: a extração da pesquisa mistura respostas de pesquisas diferentes na mesma conversa

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

**Pergunta:** posso implementar a opção 3 (cortar o histórico + reforçar o
prompt), ou alguém prefere revisar a abordagem antes? Cenário real: qualquer
paciente que responde mais de uma pesquisa ao longo do tempo (entrada +
reencaminhamento, ou entrada + encerramento) é afetado — não é caso raro.

---

## Como usar este documento

Os itens 1, 2 e 7 pedem resposta rápida: os dois primeiros têm dinheiro real
em risco, o 7 tem dado clínico sendo gravado errado agora mesmo, em qualquer
ambiente que rodar esse fluxo. Os itens 3 e 4 são de prazo/prioridade — não
bloqueiam nada tecnicamente, mas sem uma decisão explícita tendem a ficar
paradas indefinidamente (como já aconteceu antes neste projeto). O item 5 é
uma checagem de dois minutos. O item 6 bloqueia só o teste local do contrato.
