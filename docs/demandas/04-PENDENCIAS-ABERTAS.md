# Pendências abertas — ações fora do código

> Registrado em **13/08/2026**, depois do conserto do parcelado e do link curto.
> Tudo aqui é **operação**, não código: o código está no ar. São coisas que
> alguém precisa clicar, configurar ou decidir.
>
> Ordenado por dinheiro em risco.

---

## 🔴 1. O cron `/tasks/stripe` NÃO existe — é o que deixa dinheiro escapando

**Estado:** endpoint no ar e testado em produção, **sem nenhum agendador chamando**.

```
$ curl -X POST "https://sofia-whatsapp.onrender.com/tasks/stripe?simular=1" \
       -H "X-Tasks-Token: <TASKS_TOKEN>"
{"simulado": true, "ja_limitadas": 11, "planejadas": [], "alertas": []}
```

**Por que importa.** A avaliação neuropsicológica parcelada é, no Stripe, uma
**assinatura mensal** — o Stripe não oferece parcelamento de cartão no Brasil. E
o fim da cobrança **não pode ser marcado na criação**: `subscription_data[cancel_at]`
não existe na API (responde `400 parameter_unknown`); só `POST /subscriptions/{id}`
aceita `cancel_at`, e isso só é possível **depois** que a pessoa paga.

Quem grava esse fim é o `limitar_parcelado`, chamado por `POST /tasks/stripe`.
**Sem o cron, toda assinatura de parcelado nova cobra para sempre.** Foi
exatamente isso que aconteceu com 18 assinaturas do painel antigo do site — uma
delas cobrou 5 parcelas num plano de 4 (R$ 1.250 no lugar de R$ 1.000, estornado
por Pix à parte).

**As 11 assinaturas antigas já estão travadas** (aplicadas e conferidas em
13/08). O risco é só para as **novas**.

⚠️ **Já existe pelo menos um link novo em circulação**: `allos.org.br/p/ej3uvrc`
(Pedro Luiz Assunção de Paula, 4x de R$ 250,00). Quando ele pagar, a assinatura
nasce sem `cancel_at`. A margem é de ~30 dias — a 5ª cobrança indevida só viria
um mês depois da 4ª parcela —, então há dias de folga, não horas. Mas é a
primeira coisa a fazer.

### Como resolver

Criar um job no [cron-job.org](https://cron-job.org) (mesma conta dos outros):

| campo | valor |
|---|---|
| Método | `POST` |
| URL | `https://sofia-whatsapp.onrender.com/tasks/stripe` |
| Header | `X-Tasks-Token: <o TASKS_TOKEN do Render>` |
| Frequência | diária — `0 4 * * *` |

Não precisa ser mais frequente: a margem é de 30 dias.

Conferir depois, sem escrever nada, trocando a URL por `.../tasks/stripe?simular=1`.

**Tentei fazer isso pelo navegador em 13/08 e não deu** — o cron-job.org exige a
sessão logada do Paulo. É ação dele.

### Alternativas, se o cron externo incomodar

- **Cron do Render** (`render.yaml` suporta `type: cron`): tira a dependência de
  um terceiro, mas é serviço pago no Render.
- **Pendurar no `/tasks/seguimentos`**, que já tem cron: zero configuração nova.
  Foi descartado por acoplar duas automações sem relação, mas é melhor que não
  ter nada. ⚠️ **Não** pendurar em `/tasks/cobrancas`: aquele endpoint só faz
  algo com `cobranca_ativa` ligada, que está desligada.

---

## 2. `LINK_CURTO_BASE` no Render

**Estado:** o link curto está **funcionando ponta a ponta** (verificado em 13/08:
`allos.org.br/p/ej3uvrc` → 302 → `buy.stripe.com/...`), mas a variável ainda não
foi setada, então os links novos saem com o domínio do Render.

```
LINK_CURTO_BASE=https://allos.org.br/p
```

Não é urgente e não quebra nada: sem ela os links saem como
`sofia-whatsapp.onrender.com/l/<slug>` e funcionam igual — só não levam o domínio
da Allos, que é justamente o ponto da funcionalidade (link de pagamento vindo de
domínio desconhecido no WhatsApp tem cara de golpe).

Salvar a variável **dispara um redeploy** no Render.

---

## 3. Decisões sobre pagamentos que estão falhando

Nada disso é código. São 3 pessoas com cobrança em aberto, encontradas ao varrer
a conta em 13/08.

### 🔴 3a. TATIANE A DA SILVA — duas assinaturas duplicadas

`tatiane_ads@yahoo.com.br`

```
sub_1ThHFpCkZTsmdijr8HZPq40j   criada 11/06/2026 19:39:14   2 pagas   R$ 387,90
sub_1ThHJHCkZTsmdijrwVE05kiw   criada 11/06/2026 19:42:49   2 pagas   R$ 387,89
                                                             TOTAL:   R$ 775,79
```

Três minutos e trinta e cinco segundos de diferença, mesmo e-mail, clientes
Stripe distintos. Tem toda a cara de o link ter sido gerado duas vezes e ela ter
pago os dois. **Se for duplicata, ela pagou ~R$ 388 a mais.** As duas estão
`past_due` agora (a terceira cobrança de cada uma falhou).

**Decidir:** cancelar a duplicada e definir se estorna.

> Isso não volta a acontecer com links novos: desde 13/08 todo link nasce com
> `restrictions.completed_sessions.limit = 1`.

### 3b. Faturas em aberto

| paciente | e-mail | situação |
|---|---|---|
| jessica josefa Silva | `jjj_019@hotmail.com` | R$ 200/mês desde 18/06 · 2 pagas, 1 em aberto desde 09/08 |
| Eduardo Captein | `ecaptein@gmail.com` | R$ 200/mês desde 23/03 · 5 pagas, 1 em aberto desde 10/08 |

Não há régua de inadimplência automática — cobrança recorrente do 2º mês em
diante continua sendo trabalho manual (ver "Cobrança recorrente" no `CLAUDE.md`).

---

## 4. `paciente_id` nos links gerados sem vincular paciente

O campo "Vincular ao paciente da Sofia" no `/painel/pagamentos` não é decorativo:
é ele que grava `paciente_id` no metadata da assinatura, e **é por esse campo que
a contabilidade casa a assinatura do Stripe com o prontuário do Hamilton**. Não
dá para preencher depois que a pessoa assina.

O link do Pedro (`plink_1U47jRCkZTsmdijrpBVBWPgU`) foi gerado sem vínculo. Se ele
já tem ficha no Hamilton, vale regerar com o paciente selecionado.

---

## 5. O painel do site continua criando links no formato antigo

Decisão de 13/08: **daqui em diante os links são feitos pelo painel da Sofia**,
principalmente os de neuroavaliação. O painel de pagamentos do `allos-site` não
foi desativado nem corrigido.

Enquanto ele for usado, os links que saem de lá continuam sem `description`
(o paciente não lê quantas parcelas são), sem `restrictions` (link reutilizável)
e como Checkout Session (URL gigante que **expira em 24h**).

O `cancel_at` desses, sim, o reconciliador cobre — ele varre a conta inteira e
não olha quem criou. Mas só se o cron do item 1 existir.

**Decidir:** desativar a tela lá, ou portar as correções para o repo do site.
