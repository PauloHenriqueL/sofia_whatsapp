# Backlog de Melhorias — Assistente Sofia (Associação Allos)

> Documento de demandas (User Stories) refinadas para desenvolvimento.
> Formato: User Story + Critérios de Aceite (BDD) + Requisitos Não-Funcionais.

---

## Demanda 1 — Fluxo de acolhimento de pacientes de Prefeitura

### 1. User Story
Como paciente encaminhado por uma prefeitura conveniada, quero ser acolhido e cadastrado pelo WhatsApp com a informação clara de que o atendimento é custeado pela prefeitura (gratuito pra mim), para que eu inicie a terapia sem dúvidas sobre custo.

### 2. Critérios de Aceite
- Dado que a pessoa menciona uma das prefeituras conveniadas (**Matelândia** ou **Bela Vista de Minas**), quando a Sofia identificar isso, então ela segue o fluxo de prefeitura normalmente, sem escalar por incerteza.
- Dado que a pessoa cita uma prefeitura fora dessa lista, quando a Sofia identificar isso, então ela avisa que vai confirmar com a Thainá e escala com motivo `prefeitura`.
- Dado que a pessoa confirmou interesse em seguir, quando a Sofia explicar o funcionamento, então ela apresenta o mesmo formato do fluxo padrão (online, sessões semanais de 50 min, troca de terapeuta sem custo), deixando claro que o custo é da prefeitura e **gratuito para o paciente**, sem mencionar nenhum valor monetário.
- Dado que a pessoa topou o cadastro, quando a Sofia coletar os dados, então ela pede os mesmos dados do fluxo padrão numa única mensagem, acrescentando qual das duas prefeituras é o convênio, e o cadastro é registrado com essa marcação.
- Dado que o cadastro foi confirmado, quando a Sofia fizer o handoff, então ela informa o próximo passo e o prazo (terapeuta chama em até 36h) sem citar pagamento.
- Dado que a pessoa pergunta se precisa pagar algo, quando a Sofia responder, então ela reafirma que o custeio é da prefeitura, sem custo pro paciente.

### 3. Requisitos Não-Funcionais
Tom e regras de escrita já existentes no prompt da Sofia; tratamento de dados pessoais conforme LGPD.

---

## Demanda 2 — Fluxo de avaliação neuropsicológica com reunião de apresentação

### 1. User Story
Como interessado em avaliação neuropsicológica, quero entender que a Allos oferece esse serviço e ter a opção de uma reunião de apresentação com a coordenadora de neuropsicologia, para decidir com clareza antes de contratar.

### 2. Critérios de Aceite
- Dado que a pessoa procurou avaliação neuropsicológica, quando a Sofia responder, então ela explica em linguagem simples que a Allos realiza a avaliação com a equipe e convida pra uma reunião com a **Amanda, coordenadora de neuropsicologia**, sem mencionar valor nesse momento.
- Dado que a pessoa pergunta o valor de forma explícita ("quanto custa?", "qual o preço?"), quando a Sofia responder, então ela informa que o valor é **R$ 1.000** (não foge da pergunta) e, na sequência, retoma o convite pra reunião com a Amanda, tentando levar a pessoa pra próxima etapa em vez de encerrar a conversa ali.
- Dado que a pessoa demonstrou interesse na reunião, quando ela confirmar, então a Sofia registra o interesse, marca o atendimento como escalado e informa que a Thainá vai retornar com os horários disponíveis.
- Dado que o atendimento está escalado, quando a pessoa mandar novas mensagens, então a Sofia não retoma o fluxo de terapia e reforça que a equipe já vai retornar.
- Dado que a pessoa diz que não quer a reunião mesmo após saber o valor, quando ela recusar, então a Sofia respeita, tira dúvidas gerais pela base de conhecimento e escala com `neuro_reuniao` só se houver algo que ela não cobre.

### 3. Requisitos Não-Funcionais
Primeira menção à Amanda deve apresentá-la em meia frase (mesma regra já existente para a Thainá).

---

## Demanda 3 — Pesquisa de satisfação disparada pelo registro da primeira sessão

### 1. User Story
Como time de Qualidade da Allos, quero que, ao registrar a primeira sessão realizada no sistema, seja disparado automaticamente um convite para uma pesquisa de satisfação respondida pergunta a pergunta no WhatsApp, para medir bem-estar e qualidade do atendimento desde o início da jornada.

### 2. Critérios de Aceite
- Dado que o terapeuta registrou a primeira sessão no sistema (lançamento de prontuário, feito só depois da sessão já ter ocorrido), quando esse registro for salvo, então a Sofia envia automaticamente uma mensagem perguntando se a pessoa se dispõe a responder uma avaliação rápida, **sem se reapresentar** (a conversa já é continuidade do mesmo atendimento).
- Dado que a pessoa aceitou, quando a pesquisa iniciar, então as perguntas são enviadas **uma por mensagem**, aguardando a resposta antes de enviar a próxima, no tom e formato de escrita já usados pela Sofia (sem emoji, frases curtas).
- Dado que a pesquisa está em andamento, quando as perguntas forem enviadas, então elas cobrem: bem-estar individual, satisfação interpessoal, comunicação social, estado geral de bem-estar, nota do terapeuta, nota de indicação da Allos, data da última sessão, feedback livre, rapidez do atendimento, indicação, **mais uma pergunta de 0 a 10 sobre a qualidade do acolhimento e encaminhamento feito pela própria Sofia**.
- Dado que a pessoa respondeu algo fora do formato esperado (ex.: nota fora de 0 a 10), quando isso ocorrer, então a Sofia pede de novo com gentileza uma única vez e, se persistir, registra como "não informado" e segue.
- Dado que a pessoa recusou ou não respondeu dentro do prazo definido, quando isso ocorrer, então a pesquisa é encerrada sem insistência e o fluxo de cobrança (Demanda 4) segue normalmente.
- Dado que a pesquisa terminou, quando as respostas forem registradas, então elas ficam vinculadas ao paciente e disponíveis pro time de Qualidade, e a Sofia agradece a participação.

### 3. Requisitos Não-Funcionais
Tom acolhedor no padrão da Sofia; a Sofia não comenta nem interpreta clinicamente as respostas; dados sob LGPD.

---

## Demanda 4 — Envio de cobrança (Pix fixo + link Stripe) para garantir a vaga

### 1. User Story
Como paciente que fez a primeira sessão e decidiu continuar, quero receber pelo WhatsApp a chave Pix e o link de pagamento no cartão, para pagar a mensalidade e garantir minha vaga sem depender de contato manual.

### 2. Critérios de Aceite
- Dado que a pesquisa da Demanda 3 foi concluída (ou recusada/expirada), quando o fluxo seguir, então a Sofia envia uma mensagem explicando que, pra garantir a vaga, é preciso confirmar a mensalidade, informando a **chave Pix fixa** e gerando, via integração já conectada com o Stripe, o **link de pagamento no cartão**, com o valor destacado sozinho na linha.
- Dado que a pessoa enviou o comprovante, quando ele for recebido, então a Sofia confirma o recebimento e informa que a vaga está garantida.
- Dado que a pessoa disse que achou caro ou quer negociar, quando isso ocorrer, então a Sofia escala com motivo `preco`; se disser que não pode pagar, escala com `gratuidade`.
- Dado que a pessoa não respondeu no prazo definido, quando ele vencer, então é enviado **um único lembrete**, sem pressão.
- Dado que o paciente é de prefeitura, quando o gatilho ocorrer, então **nenhuma cobrança é enviada**.

### 3. Requisitos Não-Funcionais
Nenhuma menção a parcelamento; link do cartão gerado por paciente via API do Stripe já integrada; linguagem sem tom de cobrança agressiva.

---

## Demanda 5 — Pesquisa de encerramento disparada por quebra de vínculo

### 1. User Story
Como time de Qualidade da Allos, quero que o registro de quebra de vínculo no sistema Hamilton dispare automaticamente a pesquisa de encerramento, para entender os motivos da saída e medir os resultados percebidos.

### 2. Critérios de Aceite
- Dado que uma quebra de vínculo foi registrada no sistema Hamilton (desligamento, alta ou reencaminhamento), quando esse registro for salvo, então a Sofia envia o convite da pesquisa de encerramento, pedindo consentimento antes de começar, sem se reapresentar.
- Dado que a pessoa aceitou, quando a pesquisa iniciar, então segue o modelo de encerramento (motivo da interrupção + blocos de bem-estar + notas de terapeuta e indicação + feedback livre), pergunta a pergunta, com as mesmas regras de validação, prazo e registro da Demanda 3.
- Dado que a pessoa recusou ou não respondeu dentro do prazo, quando isso ocorrer, então a pesquisa é encerrada sem insistência.
- Dado que a pesquisa terminou, quando as respostas forem registradas, então elas ficam vinculadas ao paciente e disponíveis pro time de Qualidade.

### 3. Requisitos Não-Funcionais
Nenhuma tentativa de reverter a saída ou "reter" o paciente durante a pesquisa; dados sob LGPD.
