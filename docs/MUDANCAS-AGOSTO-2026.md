# O que mudou em agosto de 2026

> **Contexto do ciclo, em uma página.** Se você vai *executar* (merge, migration,
> deploy, configuração), o documento é
> [`demandas/06-SUBIDA-EM-PRODUCAO.md`](demandas/06-SUBIDA-EM-PRODUCAO.md) — este
> aqui explica o porquê das coisas e o que ficou pendente de decisão.

---

## Em uma página

| | O que | Estado |
|---|---|---|
| 1 | **O paciente assina o contrato pelo celular** | pronto, desligado |
| 2 | **A Sofia trocou de modelo** (`gpt-5.6-terra`) | pronto |
| 3 | **Quem some antes de confirmar vira ficha na fila da Thainá** | pronto |
| 4 | **Oito ajustes no jeito de conversar**, medidos | pronto |
| 5 | **A coordenação ver o contrato no Hamilton** | ⚠️ **falta escrever** |

E uma dívida antiga que precisa ser paga junto: **as Demandas A e C (origem do
paciente, convênio de prefeitura, pesquisas de satisfação) nunca foram merjadas no
Hamilton.** Estão prontas há semanas numa branch. Sem elas, paciente de convênio é
cobrado e nenhuma pesquisa sai.

---

## 1. O contrato assinável

**O que a pessoa vê.** Depois da primeira sessão, quando a Sofia manda a cobrança
da mensalidade, ela manda junto um link. A pessoa abre no celular, lê o contrato,
digita nome e CPF, assina. Acabou.

**O que acontece por trás.** O Hamilton monta o documento com os dados dela, manda
pra Autentique (assinatura eletrônica), e guarda o PDF assinado no prontuário —
com o CPF, o IP e a hora, que é o que dá validade jurídica ao ato.

**Três decisões que valem saber:**

- **O contrato vai na mesma mensagem que o pagamento.** São a mesma decisão pra
  quem recebe: fechar. Em duas etapas separadas, empilharia uns quatro dias de
  abordagem depois de uma única sessão.
- **Assinar não trava o atendimento.** Quem não assinar aparece na tela **Hoje** do
  painel e alguém resolve. A Sofia não insiste mais de uma vez.
- **O texto do contrato se edita no painel da Sofia**, em *Prompts* → *Contrato
  terapêutico*, com um botão de prévia que baixa o `.docx` como o paciente vai
  receber. Editar vale pra frente: quem já assinou tem o PDF guardado com a
  redação que assinou.

**O contrato mudou.** Saíram as assinaturas do terapeuta e do supervisor — ficaram
só Allos e paciente. Saiu o horário fixo do documento. A cláusula de pagamento
passou a descrever a cobrança real (no cartão renova na data de adesão; no Pix
vence dia 10) e a dizer **mensalidade integral, sem pro rata**. Entrou uma cláusula
de assinatura eletrônica.

**Quem não recebe contrato:** paciente de convênio (não paga mensalidade), quem
veio pra avaliação neuropsicológica, quem já pediu gratuidade, e conversas
arquivadas. Além disso, o Hamilton recusa qualquer valor fora da faixa de R$ 100 a
R$ 5.000 — no cadastro antigo o mesmo campo às vezes guarda valor por sessão
(R$ 50), e sem esse piso sairia contrato de "mensalidade de R$ 50".

---

## 2. A Sofia trocou de modelo

Agora roda em **`gpt-5.6-terra`**, com o raciocínio desligado na conversa.

Foram medidos três modelos da mesma família — 108 conversas simuladas contra
personas de controle:

- **Sol** (topo de linha) custa 3× mais e foi **pior**: prometeu coisas que não fez
  em 4 conversas, contra 1 do Terra.
- **Luna** (o mais barato) despeja pra humano cedo demais: 9 de 36 conversas,
  incluindo prefeitura, mãe de adolescente e sofrimento que o Terra resolve
  sozinho.
- **Terra** ficou melhor nas duas pontas, e é o do meio no preço.

O raciocínio fica desligado porque o turno da Sofia é seguir roteiro e escolher
ação, não pensar — ligado, adiciona segundos de latência em toda mensagem,
cobrados como saída. Na extração das respostas da pesquisa (transcrição → dados),
onde ninguém está esperando, fica no mínimo.

> ⚠️ **Detalhe que derruba tudo se passar batido:** `OPENAI_REASONING_EFFORT=none`
> é **obrigatória**. Sem ela o bot responde "tive um probleminha técnico" para todo
> mundo, sem erro visível em lugar nenhum. Detalhes no `06`, Bloco 4.

---

## 3. Quem some antes de confirmar

**O problema, medido:** em 2 de 3 conversas de uma persona que chega falando do
sofrimento, a pessoa passa nome, nascimento e horários, entende o próximo passo, e
**vai embora satisfeita exatamente no "tá tudo certo?"**. Do lado dela, está
encaminhada. Do lado de dentro, não existia ficha nenhuma.

Agora, depois de **24 horas de silêncio**, a Sofia relê a conversa, extrai o que a
pessoa contou e **deixa a ficha montada na tela Hoje**, com o botão "Cadastrar no
Hamilton" que já existia. Se não achar nome completo **e** data de nascimento, não
monta nada.

> **Ela não cadastra sozinha, de propósito.** Esse era o desenho inicial e estava
> errado por um motivo que o código não cobre: dá pra saber que a pessoa *passou*
> os dados, não que ela *quis* ser cadastrada. Quem disse nome e nascimento e
> depois escreveu "vou pensar melhor" é indistinguível, aqui dentro, de quem só
> perdeu o wi-fi. Um humano lê o histórico em cinco segundos e sabe. O ganho —
> ninguém se perde — fica inteiro; o risco de escrever no prontuário sem ninguém
> olhar sai de cena.

A ficha vem com uma observação dizendo o que é:

> *Dados extraídos da conversa: a pessoa passou as informações mas sumiu antes de
> confirmar. Confira nome e nascimento antes de cadastrar.*

---

## 4. Oito ajustes no jeito de conversar

Todos saíram de conversas simuladas e revisadas uma a uma. **Quatro deles eram o
prompt se contradizendo**, não o modelo errando.

Os quatro que mudam o que a pessoa ouve:

**Preço vem primeiro.** Quem pergunta "quanto custa?" recebe o número na primeira
mensagem, sem apresentação antes. Quem pergunta preço está comparando, e qualquer
coisa na frente da resposta é lida como enrolação. "Somos uma ONG que forma
terapeutas" virou resposta pra *"quem são vocês?"* — não pra *"quanto custa?"*.

**O cético mudou de argumento — e agora tem um caminho pra dar.** Quem pergunta "é
psicólogo ou estagiário?" não está curioso: está auditando, já concluiu que
cortaram alguma coisa. Responder com "mas tem supervisão" concede a premissa.
Agora a ordem inverte: primeiro a razão (a Allos é uma **ONG que oferece formação
gratuita**, e o valor acessível é o que essa escola produz), depois a evidência
(**menos de 5% passam na primeira tentativa** da avaliação prática por banca). Só
sai quando perguntam — explicar isso pra quem não levantou o assunto **planta** a
dúvida.

E quando a pessoa insiste em saber quem vai atendê-la, a Sofia passou a ter uma
resposta concreta em vez de só "não posso prometer": *quem faz o encaixe é a
Thainá, ela entra em contato antes da primeira sessão pra combinar o horário e
dizer quem é, e nessa conversa você tira as dúvidas com ela.* Vale pra **qualquer**
preferência (terapeuta mulher, experiência em luto, horário) — a Sofia anota, diz
que anotou, e diz quando a pessoa vai poder tratar disso.

**Correção junto:** o prompt vinha dizendo que **o terapeuta** entra em contato em
até 36h. Quem faz o match e o agendamento é a **Thainá**
([`referencia/workflow.md`](referencia/workflow.md)). Corrigido nos três lugares.

**Presencial deixou de escalar.** A Sofia oferece online sempre. Se a pessoa pedir
presencial, ela diz que existe na sede, que as vagas são poucas e que é preciso
morar em BH, anota o pedido e **segue o cadastro normalmente** — registrando nas
observações.

**O resultado, em conteúdo.** As promessas não cumpridas ("vou registrar isso" e
não registrar) foram de 3 para 0 nas duas baterias. E a Sofia acerta o mais difícil
— responder o não-dito:

> *"eu não sei nem como pedir isso direito"*
> → **"Você não precisa saber pedir do jeito certo."**

O que ainda incomoda: ela usa a mesma frase de fechamento em quase toda conversa
("Só uma última: como você chegou até a Allos?"), e o ritual de confirmação é o
momento mais burocrático da conversa, logo depois do mais humano. Nenhum dos dois
justifica continuar mexendo agora.

---

## 5. A tela que falta

O contrato funciona ponta a ponta pela API — mas **a coordenação não consegue ver
um contrato pela interface do Hamilton**. Não está no admin nem na página do
paciente, e não há como baixar o PDF assinado.

É o único item deste ciclo que precisa de código novo. Especificação, com arquivos
e linhas, no [`06`](demandas/06-SUBIDA-EM-PRODUCAO.md), Bloco 3.

---

## Uma coisa que não é novidade, mas custa dinheiro

O cron `POST /tasks/stripe` **continua não existindo**. Sem ele, toda assinatura de
parcelado de avaliação neuropsicológica **cobra pra sempre** — já aconteceu com 18
pacientes. As antigas foram travadas à mão em 13/08; o risco é para as novas.

Está em [`demandas/04-PENDENCIAS-ABERTAS.md`](demandas/04-PENDENCIAS-ABERTAS.md),
junto da assinatura duplicada da Tatiane (~R$ 388 cobrados a mais) e de duas
faturas em aberto.

---

## O que ainda depende de decisão

### 1. O texto do contrato — precisa de aprovação

Sete mudanças, listadas na seção 1. Sem assinatura de terapeuta nem de supervisor.
**Ninguém deve receber esse documento pra assinar antes de alguém ler o texto
final.** Dá pra ler no formato em que ele sai: painel da Sofia → *Prompts* →
*Contrato terapêutico* → botão de prévia (baixa um `.docx`).

### 2. A conta da Autentique — precisa virar institucional

A integração foi validada com uma assinatura real, de ponta a ponta, mas contra uma
**conta pessoal**. No volume previsto (20 pacientes novos/mês) isso precisa virar
conta institucional. Enquanto não trocar, os contratos ficam num CNPJ que não é o
da Allos.

---

## O que a Thainá precisa saber

Duas mudanças alteram a rotina dela e não aparecem em tela nenhuma se ninguém
avisar:

1. **A fila "Cadastro a confirmar"** na tela Hoje. São pessoas que passaram os
   dados e sumiram; a ficha está montada, falta ela revisar o histórico e clicar em
   *Cadastrar no Hamilton*. Os dados **não foram confirmados pela pessoa**.
2. **"Quer presencial" agora só existe nas observações da ficha.** Deixou de virar
   escalada. Precisa entrar na rotina de quem faz o match — se ninguém ler, o
   pedido morre em silêncio, que é pior que a escalada de antes.

E uma que a Sofia passou a prometer em nome dela: **quando o encaixe for feito, a
Thainá entra em contato antes da primeira sessão** pra combinar o horário, dizer
quem vai atender e responder o que a pessoa quiser saber sobre o terapeuta. É o que
já acontecia; agora está dito na conversa, e a Sofia anota as preferências pra ela.

---

## Onde está o resto

| | |
|---|---|
| **Como executar tudo isso** | [`demandas/06-SUBIDA-EM-PRODUCAO.md`](demandas/06-SUBIDA-EM-PRODUCAO.md) |
| **Por que o contrato foi feito assim** (alternativas descartadas, riscos aceitos) | [`demandas/05-contrato-assinatura.md`](demandas/05-contrato-assinatura.md) |
| **O que já estava pendente e custa dinheiro** | [`demandas/04-PENDENCIAS-ABERTAS.md`](demandas/04-PENDENCIAS-ABERTAS.md) |
| **Como o sistema funciona no dia a dia** | [`referencia/workflow.md`](referencia/workflow.md) |
| **A arquitetura e os porquês não óbvios** | [`../CLAUDE.md`](../CLAUDE.md) |
