# Documentação da Sofia

## 👋 Começando agora no projeto? Leia nesta ordem

1. **[`referencia/workflow.md`](referencia/workflow.md)** — como o sistema
   funciona no dia a dia: paciente, Sofia, Thainá e Hamilton. Comece por aqui,
   são 5 minutos e o resto faz sentido depois.
2. **[`../CLAUDE.md`](../CLAUDE.md)** — a arquitetura de verdade: onde mora cada
   coisa, e principalmente **os porquês não óbvios** (por que a saída do bot é
   sanitizada, por que a pesquisa faz polling em vez de webhook, por que o
   telefone cai pro número do WhatsApp). É o documento mais importante do repo.
3. **[`demandas/01-EM-ANDAMENTO.md`](demandas/01-EM-ANDAMENTO.md)** — o que foi
   feito neste ciclo, o que falta, os bugs encontrados e os riscos aceitos.

## ➡️ O que precisa ser feito agora

🔴 **Antes de tudo: [`demandas/04-PENDENCIAS-ABERTAS.md`](demandas/04-PENDENCIAS-ABERTAS.md).**
Nada ali é código — é clicar, configurar e decidir. O primeiro item (o cron
`/tasks/stripe`) é dinheiro escapando: sem ele, todo parcelado de neuro novo
cobra pra sempre.

Depois, o resto está em **[`demandas/01-EM-ANDAMENTO.md`](demandas/01-EM-ANDAMENTO.md)**,
seção "Status". São duas frentes, nesta ordem:

**1. Modelo da tabela de avaliação + planilha de qualidade**
→ [`demandas/02-modelo-de-avaliacao.md`](demandas/02-modelo-de-avaliacao.md)

Os campos já existem no banco e a pesquisa já grava neles. Falta **decidir com o
Paulo** quais perguntas ficam no questionário definitivo, o que é texto e o que é
estruturado, e **editar a planilha** que o time de Qualidade usa (ela pressupõe
uma pessoa coletando; agora quem coleta é a Sofia).

**2. Stripe + Pix (Demanda D)**
→ [`demandas/01-EM-ANDAMENTO.md`](demandas/01-EM-ANDAMENTO.md), seção "Demanda D"

**Nada foi feito** — nenhuma linha de Stripe foi tocada. O desenho já está
**fechado** (a Sofia fala direto com o Stripe, chave Pix fixa no painel, cobrança
encadeada no fim da pesquisa, comprovante escala pra Thainá, parceria nunca é
cobrada). Não precisa ser rediscutido, só implementado.

> ⚠️ **Antes de subir qualquer coisa**, leia "Pendências que bloqueiam o deploy"
> em `01-EM-ANDAMENTO.md`. Em especial a das **migrations do Hamilton**, que o
> `.gitignore` de lá ignora e por isso não chegam ao GitHub sozinhas.

---

## Mapa dos arquivos

### `demandas/` — o que foi, o que é e o que vem

| Arquivo | O que é |
|---|---|
| 🔴 **[`04-PENDENCIAS-ABERTAS.md`](demandas/04-PENDENCIAS-ABERTAS.md)** | **Ações fora do código, ordenadas por dinheiro em risco.** O cron que falta, variável de ambiente, e as decisões pendentes sobre pagamentos de pacientes reais. |
| **[`01-EM-ANDAMENTO.md`](demandas/01-EM-ANDAMENTO.md)** | **O documento de trabalho.** Ciclo atual: o que foi entregue, o que falta, bugs corrigidos, riscos aceitos e pendências de deploy. |
| [`02-modelo-de-avaliacao.md`](demandas/02-modelo-de-avaliacao.md) | Campos da tabela `Avaliacao` no Hamilton: o que foi criado e o que ainda vai ser decidido. |
| [`03-questionario-atual-da-qualidade.md`](demandas/03-questionario-atual-da-qualidade.md) | O questionário que a Juliana usava à mão no WhatsApp. É a base do que a Sofia pergunta hoje. |
| [`00-ORIGINAL-com-premissas-erradas.md`](demandas/00-ORIGINAL-com-premissas-erradas.md) | O pedido original. **Não implemente a partir dele:** foi escrito por quem não conhecia o fluxo real e errou várias premissas (elas estão listadas e corrigidas no `01`). Fica só como histórico. |
| [`99-backlog-entregue.md`](demandas/99-backlog-entregue.md) | Backlog P0–P6, todo entregue. Útil pra entender decisões antigas (o incidente do P0 explica o `saida.py`). |

### `referencia/` — como o sistema funciona

| Arquivo | O que é |
|---|---|
| [`workflow.md`](referencia/workflow.md) | O fluxo de atendimento em linguagem de gente. **Melhor ponto de partida.** |
| [`DEPLOY.md`](referencia/DEPLOY.md) | Render, env vars, cron dos follow-ups e das pesquisas. |
| [`sofia_briefing.md`](referencia/sofia_briefing.md) | Especificação original do MVP. Histórico: o sistema andou bastante desde então. |

### `juridico/`

Política de privacidade e termo de consentimento (LGPD — a Sofia lida com dado
de saúde).

---

## Os dois sistemas

Este repo é a **Sofia** (FastAPI, bot do WhatsApp). Ela conversa com o
**Hamilton** (`../hamilton-api`, Django, sistema clínico), que é onde o paciente
vira registro.

**Boa parte deste ciclo mexeu nos dois.** Se você for continuar as demandas, vai
precisar dos dois repos abertos — o `01-EM-ANDAMENTO.md` diz exatamente que
arquivo mudou em cada um.
