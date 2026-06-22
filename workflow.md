# Workflow da Sofia — como o sistema funciona

Documento explicativo do fluxo de atendimento: como um paciente interage com a
Sofia (bot) e com a Thainá (coordenadora humana), e onde o Hamilton entra.

## Os 4 atores

| Ator | Papel |
|---|---|
| 👩 **Paciente** | conversa pelo WhatsApp |
| 🤖 **Sofia** | bot — recebe, acolhe, qualifica, **coleta dados** e cadastra |
| 🗄️ **Hamilton** | sistema clínico — onde o paciente vira registro |
| 👩‍💼 **Thainá** | coordenadora humana — faz o **match com terapeuta** e o **agendamento** |

## ⚠️ Ponto-chave de escopo

**A Sofia NÃO marca a consulta nem escolhe o terapeuta.** Isso é trabalho da
**Thainá** (no Hamilton). A Sofia é a **porta de entrada**: recebe o paciente
novo, coleta os dados e cadastra como "lead". O agendamento e o match são
humanos (estão fora do MVP, de propósito).

---

## Exemplo: a Maria quer "marcar uma consulta"

**Maria manda no WhatsApp:** *"Oi, queria marcar uma consulta de terapia"*

### Fase 1 — Sofia recebe e acolhe (automático)
1. A Meta chama o `/webhook/whatsapp`. A Sofia responde 200 na hora e processa
   em segundo plano.
2. Cria a **`conversa`** da Maria (pelo número) e salva a **`mensagem`** (com
   idempotência: a mesma mensagem nunca é processada 2x).
3. Manda o histórico pro **gpt-4o-mini** com a personalidade da Sofia.
4. **Sofia responde:** *"Oi, Maria 🩵 Aqui é a Sofia, da Allos. A gente é uma
   clínica-escola de psicologia. O plano é R$ 200/mês, com sessões semanais.
   Faz sentido pra você começar?"*

### Fase 2 — Sofia coleta os dados (automático, vários turnos)
Maria diz que sim. A Sofia vai pedindo, de forma natural, um de cada vez: nome
completo, data de nascimento, melhor WhatsApp, contato de apoio, endereço,
horários disponíveis, preferência de terapeuta e motivo da busca. Cada resposta
passa pelo ciclo: webhook → salva → LLM → responde. Os dados se acumulam em
`dados_coletados`.

### Fase 3 — Sofia cadastra no Hamilton (automático)
5. Com os dados obrigatórios, a Sofia chama a ferramenta **`cadastrar_paciente`**.
6. O sistema vai no **Hamilton**: primeiro **busca pelo telefone** (pra não
   duplicar), depois **cria o paciente** como **"lead"** — *sem terapeuta ainda*,
   status **"Aguardando Início"**.
7. **Sofia confirma:** *"Pronto, Maria. Seus dados estão registrados. A Thainá,
   nossa coordenadora, vai te chamar pra combinar o terapeuta e o horário. 🩵"*

### Fase 4 — Entra a Thainá (humano)
8. No **painel da Sofia**, a Thainá vê a conversa da Maria (filtro "Cadastradas
   hoje").
9. No **Hamilton**, a Maria aparece como lead "Aguardando Início".
10. A Thainá **escolhe o terapeuta** (match) e **agenda a primeira sessão** —
    dentro do Hamilton, com olhar humano. A Sofia não faz essa parte.

---

## Quando a Sofia "passa a bola" (escalada)

A qualquer momento, se o paciente:
- pedir **falar com uma pessoa**, ou
- mencionar **gratuidade / prefeitura**, ou
- mandar um **áudio**, ou
- mandar algo **sensível** (crise, ideação suicida, etc.)

→ a Sofia **escala**: marca a conversa como **modo humano**, registra a
**`escalada`**, e dispara um **template no WhatsApp pessoal da Thainá**
(*"paciente Maria precisa de você no painel, motivo: X"*). A Thainá então
**assume a conversa pelo painel** e responde ao paciente **diretamente** (a
Sofia para de responder até a Thainá devolver pro bot).

---

## Resumo do fluxo

```
Paciente novo  →  Sofia (acolhe + coleta + cadastra lead no Hamilton)
                       │
                       ├─ caso normal → confirma e avisa que a Thainá assume
                       │                     ↓
                       │                 Thainá faz match + agenda (no Hamilton)
                       │
                       └─ sensível / pedido humano / áudio → ESCALA
                                             ↓
                                 Thainá assume no painel e responde direto
```

**Em uma frase:** a Sofia transforma "um paciente perdido no WhatsApp" em "um
cadastro organizado + a Thainá avisada", automatizando a parte repetitiva — mas
a decisão clínica (qual terapeuta, qual horário) continua humana.
