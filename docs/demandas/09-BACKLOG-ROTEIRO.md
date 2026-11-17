# Backlog do roteiro da Sofia — decidido, a implementar

> Registrado em **17/11/2026**. Duas mudanças de comportamento **decididas pelo
> Victor** e deliberadamente **não implementadas ainda**: entram depois que
> terminarmos a rodada de testes do encaminhamento assistido, para não mexer no
> roteiro no meio das validações.
>
> O levantamento de onde mexer já foi feito — é só executar.

---

## 1. Criança deixa de ser caso especial (sai o `menor_11`)

**Decisão (Victor, 17/11):** se o paciente é criança, a Sofia trata como paciente
comum. "Nós nunca recebemos mensagem de uma criança de 11 anos querendo terapia.
É sempre uma mãe ou um pai." Quem escreve é o responsável, e isso já tem regra no
roteiro ("Quando quem escreve não é o paciente").

**O que fica de pé:** menor de 18 segue o fluxo normal, com o aviso do termo de
consentimento dos pais ou responsável.

**Onde mexer (conferido no código em 17/11):**

| Arquivo | O quê |
|---|---|
| `prompt/sofia_v01.txt` | bloco "Verificação de idade": tirar o bullet de menor de 11; "entre 11 e 17" vira "menor de 18 anos" |
| `prompt/sofia_v01.txt` | seção "Roteamento": tirar o bullet `menor_11` |
| `prompt/sofia_v01.txt` | lista "Quando escalar pra Thainá": tirar a linha `menor_11` |
| `prompt/sofia-base-conhecimento.md` | "Vocês atendem crianças?" — hoje responde "a partir dos 11 anos" |
| `app/services/tools.py` | tirar `"menor_11"` da lista de motivos e da descrição |
| `tests/test_escalation.py` | o teste varre os motivos; trocar `menor_11` por outro |
| `CLAUDE.md` | lista de motivos de escalada |

⚠️ O prompt é calibrado e tem suíte própria em `laboratorio/` — medir depois de mexer.

---

## 2. Profectum: a Sofia não fala de dinheiro, e o cadastro entra no encaminhamento

**Decisão (Victor, 17/11):** o Profectum (captação **57**, marcada como
`is_parceria=True` em 17/11) é parceiro, mas **o acerto financeiro não é o das
prefeituras**.

**O que a Sofia NÃO pode fazer:** dizer que o atendimento é gratuito, ou citar
qualquer valor — mensalidade, desconto, "você não paga nada".

**O que ela faz:**
1. Conduz o atendimento normalmente (online, sessão semanal de 50 min, troca de
   terapeuta sem custo) e segue para o cadastro.
2. **Qualquer dúvida financeira** ("é gratuito?", "quanto custa?", "quem paga?")
   → orienta a pessoa a perguntar **direto ao setor financeiro do Profectum**.
   Registra nas `observacoes` que direcionou.
3. **Cadastra no Hamilton** com `vlr_sessao = 0` e a pessoa marcada como
   **parceria** (`is_parceria` / `tipo_pagamento`).
4. Em seguida o paciente **entra no fluxo do encaminhamento assistido**: o
   sistema procura o terapeuta que serve e tenta ligar os dois, como em qualquer
   outro lead.

**Onde mexer:**

| Arquivo | O quê |
|---|---|
| `prompt/sofia_v01.txt` | "De onde a pessoa veio": hoje diz que **todo** [PARCERIA/CONVÊNIO] é custeado e gratuito — era daí que a Sofia deduzia a gratuidade |
| `prompt/sofia_v01.txt` | seção nova "Profectum", separada de "Prefeitura e convênios" |
| `prompt/sofia-base-conhecimento.md` | pergunta "vim pelo Profectum, é gratuito?" |
| `app/services/tools.py` / `hamilton_client` | garantir `vlr_sessao=0` + parceria no `cadastrar_paciente` dessa origem |

**Ponto em aberto:** com `vlr_sessao = 0`, conferir o que a cobrança
(`services/cobranca.py`) faz com esse paciente — não pode gerar link de pagamento
nem entrar em "pagamento em aberto".

---

## Já feito como apoio

As **conversas de exemplo** do painel (`scripts/exemplos_painel.py`) já mostram o
comportamento **decidido** destes dois casos, não o atual — estão marcadas com
"(a implementar)" no título justamente por isso.
