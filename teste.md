# Roteiro de teste — Stripe / cobrança da mensalidade

**Status:** aguardando a chave `sk_test_...` da conta Stripe da Allos (modo
Test) para configurar o ambiente local. Nada disso foi executado ainda.

---

## Por que não dá pra testar com a chave live

A Sofia tem uma trava **sem escotilha de propósito** (`app/config.py`,
`Settings.stripe_key`): fora de `ENVIRONMENT=production`, ela **sempre** usa
`test_stripe_secret_key`, nunca `stripe_secret_key` (a live) — mesmo que a
live esteja preenchida no `.env`. Isso existe porque já aconteceram **dois
acidentes reais**: um `pytest` mal mockado criou 4 Payment Links na conta
live, e a validação da cobrança em 17/08 criou mais um sem querer. Contornar
essa trava (ex.: setar `ENVIRONMENT=production` local) reproduz exatamente o
erro que ela existe pra evitar — por isso o caminho é usar a chave de teste.

## Como pegar a chave de teste

1. Acesse [dashboard.stripe.com](https://dashboard.stripe.com) com a conta da
   Allos.
2. Ative o toggle **"Test mode"** (topo ou menu lateral).
3. **Developers → API keys** ("Desenvolvedores → Chaves de API").
4. Copie a **Secret key**, formato `sk_test_...`.

---

## O que eu configuro depois de receber a chave

1. `test_stripe_secret_key` no `.env` da Sofia.
2. Ligar `cobranca_ativa` no `/painel/config` (nasce desligada por padrão).
3. Criar uma `Consulta` no Hamilton marcada como primeira sessão realizada
   pro paciente de teste (Ana, `paciente_id=636`) — é o gatilho real que
   `cobranca._elegivel` checa via `status_primeira_consulta` antes de
   abordar alguém.

---

## Passo a passo pra você rodar depois de tudo configurado

```bash
python scripts/conversar.py --numero 5531900000201
```

Dentro da conversa:
```
/cobrancas
```

Isso roda o cron manualmente. A Sofia deve abordar a Ana oferecendo o link de
pagamento (Stripe) e/ou Pix. Aceite o cartão, receba o link, **abra num
navegador** e use um cartão de teste do Stripe:

| Cenário | Número do cartão |
|---|---|
| Pagamento aprovado | `4242 4242 4242 4242` |
| Pagamento recusado | `4000 0000 0000 0002` |

Validade: qualquer data futura. CVC: qualquer 3 dígitos.

Depois de "pagar", confiro no dashboard do Stripe (modo Test) que a
assinatura foi criada, com `metadata.paciente_id` vinculado ao paciente
certo — é esse campo que casa a assinatura com o prontuário do Hamilton
(não dá pra preencher depois).

---

## O que olhar depois do pagamento

- Assinatura aparece no dashboard Stripe (Test mode), valor certo
  (mensalidade, sem pro-rata).
- `metadata.paciente_id` preenchido e correto.
- `conversa.stripe_ref` gravado no banco da Sofia.
- Card "Pagamento" na página da conversa do painel mostra o status
  resolvido/ativo.
- Se usar o cartão de recusa (`4000...0002`): confirmar que a Sofia trata o
  erro sem quebrar (oferece tentar de novo, ou cai pro Pix).

---

*(Achados de bugs durante esse teste vão para `demandas-teste-manual.md`;
pendências de produto/decisão vão para `perguntas.md`.)*
