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

---

## 3. São **três** acertos de parceria diferentes, não um (corrige o item 2)

**Decisão (Victor, 17/11):** cada parceiro tem um acerto próprio, e a Sofia precisa
saber distinguir os três. Hoje o roteiro trata todo `[PARCERIA/CONVÊNIO]` como
"custeado e gratuito", e é por isso que ela erraria em dois dos três.

| Origem | ID | Acerto | `is_parceria` hoje |
|---|---|---|---|
| Prefeitura de Bela Vista de Minas | 13 | **Gratuito** — custeado pela prefeitura | ✅ true |
| Prefeitura Materlândia | 46 | **Gratuito** — custeado pela prefeitura | ✅ true |
| Profectum | 57 | A Sofia **não fala de dinheiro**: manda ao financeiro do Profectum. Cadastro com `vlr_sessao = 0` + parceria, e segue para o encaminhamento | ✅ true |
| **Rede Cidadã** | **28** | **R$ 200,00** — não é gratuito, é preço fechado menor | ❌ **false** |

**O preço normal da terapia é R$ 250,00** (`preco_terapia_mensal` = 250 em
produção). A Rede Cidadã é R$ 200,00 — um desconto de parceria, não gratuidade.

⚠️ **O modelo de dados não expressa isso.** `is_parceria` é um booleano: serve
para "gratuito" e para "Profectum", mas **não** para "R$ 200". Marcar a Rede
Cidadã como parceria zeraria o valor de quem deve pagar R$ 200. Duas saídas a
decidir: um `vlr_sessao` próprio por origem no Hamilton, ou a Sofia aplicar o
valor da parceria no cadastro (e `is_parceria` seguir significando só "custeado
por terceiro").

**Também a decidir:** se a Sofia pode citar o R$ 200 da Rede Cidadã (ao contrário
do Profectum, aqui o valor é conhecido e dela).

---

## 4. Neuro: a Sofia marca a reunião com a Amanda, olhando a agenda dela

**Decisão (Victor, 17/11):** hoje a Sofia só convida para a reunião e escala
`neuro_reuniao` — quem marca a data é uma pessoa. Passa a ser dela:

1. **Consultar os horários da Amanda no Hamilton** e escolher as opções livres.
2. **Oferecer mais de um horário** ao paciente ("dia X às Y, ou dia Z às W").
3. **Coletar o que a Amanda precisa**: nome completo, data de nascimento e
   telefone da pessoa.
4. **Marcar** a reunião no horário escolhido.
5. **Avisar a Amanda por WhatsApp**, com a data e os dados do paciente.

**O que já existe:** a grade (`HorarioDisponivel` + `SessaoSemanal`) e o motor de
slots livres do encaminhamento (`principais/encaminhamento.py`:
`slots_livres_terapeuta`, `slot_disponivel`) — é a mesma conta, com outro dono.
A Sofia já sabe mandar template para terapeuta e tratar a resposta.

**O que falta:** endpoint no Hamilton para os horários livres da Amanda; gravar a
reunião (é uma `SessaoSemanal`? um `Plantaoevento`? algo novo — a reunião **não**
é sessão de terapia); **template novo na Meta** para o aviso à Amanda (aprovação
leva tempo); e a coleta dos três dados no roteiro.

**Decisões em aberto:**
- **Qual Amanda?** Há duas terapeutas com esse nome no Hamilton: **56 Amanda
  Borges de Souza** e **88 Amanda Soares Alves**. Não dá para adivinhar — a
  mensagem vai para o WhatsApp de uma pessoa.
- A reunião **ocupa** o horário na grade dela (impedindo que vire sessão) ou é só
  um aviso?
- Se não houver horário livre, o que a Sofia faz — escala como hoje?
- Quantos horários oferecer, e com que antecedência mínima?
