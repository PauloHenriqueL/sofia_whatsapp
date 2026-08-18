# Documentação da Sofia

## 👋 Começando agora no projeto? Leia nesta ordem

1. **[`MUDANCAS-AGOSTO-2026.md`](MUDANCAS-AGOSTO-2026.md)** — o que mudou no
   último ciclo e por quê. Se você acabou de receber este projeto, comece aqui.
2. **[`referencia/workflow.md`](referencia/workflow.md)** — como o sistema
   funciona no dia a dia: paciente, Sofia, Thainá e Hamilton. São 5 minutos e o
   resto faz sentido depois.
3. **[`../CLAUDE.md`](../CLAUDE.md)** — a arquitetura de verdade: onde mora cada
   coisa, e principalmente **os porquês não óbvios** (por que a saída do bot é
   sanitizada, por que a pesquisa faz polling em vez de webhook, por que o
   telefone cai pro número do WhatsApp). É o documento mais importante do repo.
4. **[`demandas/01-EM-ANDAMENTO.md`](demandas/01-EM-ANDAMENTO.md)** — histórico
   dos ciclos anteriores, bugs encontrados e riscos aceitos.

## ➡️ O que precisa ser feito agora

**Tudo o que falta está em
[`demandas/06-SUBIDA-EM-PRODUCAO.md`](demandas/06-SUBIDA-EM-PRODUCAO.md)** — o
runbook do ciclo de agosto/2026, na ordem de execução. Nenhuma linha de código
nova é necessária; é merjar, migrar, configurar e ligar.

Os três itens que mordem primeiro:

🔴 **O cron `POST /tasks/stripe` não existe** e é dinheiro escapando: sem ele,
todo parcelado de neuro novo cobra pra sempre. Junto dele, o resto de
[`demandas/04-PENDENCIAS-ABERTAS.md`](demandas/04-PENDENCIAS-ABERTAS.md).

🔴 **Duas branches do Hamilton fazem a mesma demanda, e a errada parece a certa.**
Merjar a de 06/08 em vez da de 08/08 deixa paciente de convênio sendo cobrado e a
pesquisa de entrada sem terapeuta pra gravar. A comparação abre o
[`06`](demandas/06-SUBIDA-EM-PRODUCAO.md).

⚠️ **Falta uma tela no Hamilton.** O contrato assinado funciona pela API, mas a
coordenação não consegue vê-lo pela interface — não está no admin nem na página
do paciente.

Depois disso, o que continua aberto e **depende de decisão, não de código**: o
modelo da tabela de avaliação e a planilha de qualidade
([`demandas/02`](demandas/02-modelo-de-avaliacao.md)) — os campos já existem e a
pesquisa já grava neles; falta decidir com o Paulo quais perguntas ficam e o que
fazer com a planilha que o time de Qualidade usa hoje.

> ⚠️ **Antes de subir qualquer coisa**: as **migrations do Hamilton** estão no
> `.gitignore` de lá e não chegam ao GitHub sozinhas (`git add -f`). E confira o
> `SOFIA_API_DATABASE_URL` no serviço do Hamilton no Render — setado em produção,
> ele manda paciente real pro banco de teste.

---

## Mapa dos arquivos

### Na raiz de `docs/`

| Arquivo | O que é |
|---|---|
| **[`MUDANCAS-AGOSTO-2026.md`](MUDANCAS-AGOSTO-2026.md)** | **O que mudou no último ciclo, em uma página, e por quê.** Contrato assinável, troca de modelo, cadastro automático, ajustes de conversa — mais as decisões que dependem do Paulo. Ponto de entrada pra quem está recebendo o projeto. |

### `demandas/` — o que foi, o que é e o que vem

| Arquivo | O que é |
|---|---|
| 🔴 **[`06-SUBIDA-EM-PRODUCAO.md`](demandas/06-SUBIDA-EM-PRODUCAO.md)** | **O runbook.** Tudo que mudou no ciclo de agosto/2026, o contrato de API entre Sofia e Hamilton, e a ordem exata de merge, migration, env e configuração. É o documento de quem vai executar. |
| 🔴 **[`04-PENDENCIAS-ABERTAS.md`](demandas/04-PENDENCIAS-ABERTAS.md)** | **Ações fora do código, ordenadas por dinheiro em risco.** O cron que falta, variável de ambiente, e as decisões pendentes sobre pagamentos de pacientes reais. |
| **[`01-EM-ANDAMENTO.md`](demandas/01-EM-ANDAMENTO.md)** | Ciclo de 08/08: o que foi entregue, bugs corrigidos, riscos aceitos e pendências de deploy. **Histórico** — o ciclo atual está no `06`. |
| **[`05-contrato-assinatura.md`](demandas/05-contrato-assinatura.md)** | **Demanda E (ciclo de 17/08/2026): contrato terapêutico assinado pelo paciente via Autentique**, junto do turno de cobrança. Desenho fechado, registro das decisões, alternativas descartadas e ordem de execução. Inclui a troca do modelo pro `gpt-5.6-terra`. |
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
