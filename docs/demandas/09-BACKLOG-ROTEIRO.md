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

✅ **RESOLVIDO em 17/11** (Hamilton `cacdc33`, Sofia `92afbac`): a captação ganhou
`vlr_sessao` opcional — preço fechado da origem. A **Rede Cidadã (28) está com
R$ 200,00** no cadastro de produção, segue com `is_parceria=False`, e a Sofia
carimba esse valor no cadastro pelo mesmo caminho da flag de parceria (dado do
sistema, não palavra do modelo). Origem sem valor próprio segue no padrão;
parceria continua zerando. ⏰ Falta **deploy** dos dois lados.

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
- ~~Qual Amanda?~~ **Respondido (Victor, 17/11): é a 88, Amanda Soares Alves**
  (tel. 31974002976). A outra homônima, 56 Amanda Borges de Souza, não entra.
- ⚠️ **A agenda da 88 já é de atendimento.** Ela é terapeuta com grade cheia
  (terça, quarta, quinta, sexta e sábado) e seis pacientes de teto. Se a reunião
  de neuro sair dessa mesma grade, cada reunião marcada tira um horário de
  terapia dela — e o encaminhamento assistido deixa de oferecer esse slot. Ou a
  reunião tem grade própria, ou isso precisa ser aceito de propósito.
- A reunião **ocupa** o horário na grade dela (impedindo que vire sessão) ou é só
  um aviso?
- Se não houver horário livre, o que a Sofia faz — escala como hoje?
- Quantos horários oferecer, e com que antecedência mínima?

---

## 5. Ciclo de 27/11/2026: conversa mais gentil, nomes da equipe e encaminhamento manual

**Origem:** lista da Thainá e do Victor (27/11), discutida ponto a ponto antes de
implementar. Cada linha é uma decisão do Victor.

✅ **Implementado em 27/11/2026** (suíte com 1083 testes passando; botão do painel
conferido no navegador em desktop e celular). Ainda **sem commit e sem deploy**.
Seis conversas do laboratório rodaram com o modelo de produção e levaram a três
ajustes: teto de 3 bolhas em código (`MAX_BOLHAS`), resposta a quem só devolve o
cumprimento, e apresentação da Allos adiada um turno para quem chega contando
sofrimento. **Não confirmado com o modelo real:** se ele escreve o marcador
`[[CONFIRMAR_DADOS]]` (o laboratório grava a fala já sem ele). Sem o marcador a
confirmação sai como texto, igual a antes.

| # | Ponto | Decisão | Onde |
|---|---|---|---|
| 5.1 | Enviar ao encaminhamento à mão | Botão "Enviar para o encaminhamento" na página da conversa. Serve para quem a equipe atendeu à mão (nunca entrou na fila) e para quem precisa voltar a ela. Cadastra se faltar ficha, registra o pedido de novo e devolve a conversa à Sofia. Sem nome e nascimento, diz o que falta. Se o Hamilton responder que a pessoa já tem terapeuta, só mostra o aviso. **O Hamilton não muda**: o pedido lá já é idempotente. | `encaminhamento.enviar_manual`, `routers/painel.py`, `painel_conversa.html` |
| 5.2 | Deficiência | Só quando a pessoa menciona. A Sofia pergunta uma vez qual é e se precisa de adaptação nas sessões, anota em `observacoes` e diz que anotou. Não promete terapeuta com preparo específico. | `prompt/sofia_v01.txt` |
| 5.3 | Nomes da equipe | Nas falas ao paciente a Thainá é "nossa coordenadora clínica" (depois "a coordenadora") e a Amanda é "nossa diretora de neuropsicologia". Painel, alertas internos e documentação seguem com os nomes. Trava em código só para "Thainá" (trocar "Amanda" sozinho estragaria o nome de uma paciente Amanda). | prompts, falas fixas, `saida.sem_nome_da_equipe`, `escalation._PROMESSA_DE_ACIONAR` |
| 5.4 | Call fria (follow-up de lead parado) | O texto termina em "Você ainda tem interesse?", sem oferecer a coordenadora. Quando a pessoa responde, a Sofia retoma de onde parou. | `seguimento.py`, `prompt/sofia_v01.txt` |
| 5.5 | Tom | Primeira mensagem com "Como posso te ajudar?". Toda resposta reconhece o que a pessoa disse, com o fato que ela contou e sem espelhar emoção. Cortesias entram. "Perfeito!" e "Ótimo!" liberados, variando a abertura. Emoji segue proibido. Teto de duas bolhas, três quando a pessoa pede a explicação completa. | `prompt/sofia_v01.txt` |
| 5.6 | Apresentar a Allos | Sempre, uma vez, assim que a Sofia souber que a pessoa busca terapia. Quem pergunta o preço primeiro recebe o preço e a apresentação logo depois. | `prompt/sofia_v01.txt` |
| 5.7 | Botões de toque | Na call fria ("Sim, quero" / "Agora não") e na confirmação do cadastro ("Tudo certo" / "Corrigir"). Botão não precisa de template, só da janela de 24h. O toque entra na conversa como se a pessoa tivesse digitado o rótulo. | `whatsapp_client.enviar_botoes`, `webhook.py`, `seguimento.py` |
| 5.8 | Dizer por que pede os dados | Uma frase antes de pedir nome, nascimento e horários. | `prompt/sofia_v01.txt` |

**Recusado pelo Victor (27/11):** prazo de resposta da coordenadora variando com
o horário ("amanhã cedo" à noite e no fim de semana). Fica "em pouco tempo".

⚠️ **Deploy.** Os itens de roteiro só entram em produção se não houver texto salvo
em `/painel/prompts`: o que está salvo lá tem prioridade sobre o arquivo. Se
houver, é preciso "Resetar" na tela (perde a edição feita pelo painel). O botão,
a call fria, as falas fixas e a trava do nome valem de qualquer jeito.

⚠️ **Depois de subir**, ler uma amostra de conversas reais. O laboratório mede com
paciente simulado, e simulação não prevê bem a percepção de gente de verdade.

---

## 6. Ideias da pesquisa de 27/11/2026 (a fazer depois)

Levantadas em pesquisa na web. Nenhuma está decidida.

- **Lista "conversas para revisar" no painel**, marcadas por sinais automáticos:
  a pessoa repetiu a mesma pergunta, pediu humano, ou sumiu logo depois de uma
  resposta longa.
- **Orientação e lembrete antes da primeira sessão.** A espera é o fator mais
  ligado a falta e desistência; um lembrete a mais reduziu faltas em 11% em
  consultas de saúde mental num estudo grande (Kaiser Permanente, 2019). Exige
  template aprovado na Meta.
- **Aviso no meio da espera**: se o prazo de 36 horas estourar, a Sofia avisa que
  a busca continua.
- **Medir quantos chegam fora do horário comercial.** Num serviço inglês de
  terapia, 38% dos pedidos entraram fora do expediente.
- **Formulário dentro do WhatsApp (Flows)** para nome, data e horários. Obra
  maior, exige empresa verificada na Meta, e pacientes do serviço inglês
  disseram preferir a conversa ao formulário. Registrado, não recomendado agora.
