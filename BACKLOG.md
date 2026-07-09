# Backlog — demandas maiores

Ordem de execução. As 9 demandas pequenas (prompt/base de conhecimento + "é a
Sofia de novo") já foram entregues no commit `a2a2fdf`.

---

## ✅ P0 — Sanitizar a saída do modelo antes de mandar pro paciente

**ENTREGUE.** `app/services/saida.py` + `tests/test_saida.py` (22 testes) +
regressão end-to-end em `tests/test_webhook.py`. Chamado no único choke point de
saída do bot (`webhook._enviar_em_bolhas`). Contador em `/painel/metricas`.
O histórico do problema fica abaixo, como registro.

**Gravidade: alta.** Já aconteceu duas vezes em beta.

### O que o paciente viu

1. A Sofia mandou os **dados internos do cadastro** como se fosse fala:
   ```
   {"nome_completo":"Amanda Soares Alves","data_nascimento":"2002-05-10",
    "endereco":"Praça Cairo, 44, Belo Horizonte", ...}
   Te explico sim. A terapia aqui é por chamada de vídeo...
   ```
2. A Sofia mandou **lixo de template interno** no fim de uma frase normal:
   ```
   ...e organizo os dados de quem quer começar.@endsection
   to=final code  omitted
   ```

### Causa (não é o prompt)

O modelo tem dois canais: `tool_calls` (estruturado) e `content` (fala). Em (1)
ele colocou o JSON da `cadastrar_paciente` no `content` em vez do canal de tool.
Em (2) vazou token de formatação interno.

Hoje, em `app/services/llm_client.py:154`, fazemos:

```python
texto = (msg.content or "").strip() or None
```

e esse texto vai direto pra `_enviar_em_bolhas` → `whatsapp_client.enviar_texto`.
**Não existe nenhuma inspeção entre o modelo e o paciente.** Qualquer coisa que o
modelo emitir no `content` é enviada.

Isso não se resolve com instrução no prompt: LLM não dá garantia de formato.

Os dois casos aconteceram em **beta fechado**, com um colega terapeuta simulando
um paciente: **nenhum dado real foi exposto**. Mas o mesmo caminho, com paciente
de verdade, entregaria nome, nascimento e endereço dele no WhatsApp — dado de
saúde. Por isso a correção veio antes de abrir pra público.

### O que fazer

Um sanitizador na fronteira de saída (`app/services/saida.py`, novo), aplicado em
**todo** texto que sai pro paciente (bot e, no que fizer sentido, Thainá):

1. **Bloquear estrutura de dados**: se o texto (ou uma linha dele) for JSON válido,
   ou começar com `{`/`[` e contiver chaves conhecidas do `cadastrar_paciente`
   (`nome_completo`, `data_nascimento`, `telefone_contato`...), **remover essa parte**.
   Se sobrar texto útil, manda o resto; se não sobrar nada, cai no fallback.
2. **Remover tokens internos** conhecidos: `@endsection`, `to=final`, `code omitted`,
   `<|...|>`, blocos ```` ``` ````, `[Aviso do sistema: ...]`, `[Thainá, coordenadora clínica]:`
   (este último é o prefixo que nós mesmos injetamos no histórico — se o modelo
   copiar, não pode sair).
3. **Fallback**: se o texto ficar vazio depois da limpeza, não mandar bolha nenhuma;
   se a limpeza removeu algo, **logar em WARN** (sem o conteúdo removido, só o motivo
   e o tamanho — LGPD) para a gente medir a frequência.
4. **Métrica/alerta**: contador de vazamentos bloqueados no `/painel/metricas`.
   Se subir, o modelo ou o prompt regrediram.

### Testes obrigatórios

- Texto normal passa intacto (inclusive com `{` no meio de uma frase, ex.: emoji, chaves em
  linguagem natural) — **não pode haver falso positivo que corte fala legítima**.
- JSON puro → nada é enviado, WARN é logado.
- JSON seguido de fala → só a fala é enviada.
- `...começar.@endsection\nto=final code omitted` → só `...começar.`
- Bolha que ficaria vazia não é enviada.

---

## ✅ P1 — Painel: filtro, ordenação e busca — ENTREGUE

- Lista de conversas: ordenação **no servidor** (é paginada) por número, nome, modo,
  estado e atividade. Cabeçalho clicável alterna asc/desc. `painel.ORDENS` é allowlist:
  `ordem` vem da querystring e **nunca** é interpolado em SQL.
- Busca única por **nome, número ou texto de qualquer mensagem** (`?busca=`).
- Filtro virou um menu (`<details>`, sem JS), no lugar da fileira de chips.
- Abas "Todas as conversas" ↔ "Acompanhamento" nas duas telas.
- Acompanhamento: ordenação **client-side** (`static/ordenar-tabela.js`, `<th data-sort>`),
  porque as tabelas são pequenas e já vêm inteiras. Reutilizável em tabela nova.

## ✅ P2 — Assumir controle pra digitar — ENTREGUE

- Em modo bot, o campo de texto não existe: no lugar, "Assumir controle pra responder".
- Em modo humano, o campo aparece (com `autofocus`) e o cabeçalho oferece "Devolver ao bot".
- Ao sair da conversa com o controle assumido, um `confirm()` pergunta
  **"Quer que o bot assuma daqui pra frente?"**. Aceitando, devolve ao bot e segue pro
  destino; recusando, navega mantendo o controle.
- `?proximo=` só aceita caminho interno (`_destino_seguro`) — sem open redirect.

## P3 — Imagem e documento (recebimento)

Escopo decidido: **só painel**, sem visão do modelo.
- Recebe imagem/documento, guarda a referência da mídia, mostra no painel
  (miniatura pra imagem, ícone + nome pra documento) com **botão de baixar**.
- A Sofia continua escalando pra Thainá nesses casos.
- Precisa de coluna nova em `mensagem` (ou usar o `extra` JSON) e decidir onde
  ficam os bytes (a URL da Meta expira; provavelmente baixar e guardar).

## P4 — Responder mensagem específica (reply-to)

- A Thainá marca uma mensagem e responde a ela, como no WhatsApp
  (`context.message_id` na Cloud API).
- Idealmente a Sofia também, quando responde algo pontual.

## P5 — Thainá enviar foto e documento

- Upload no painel → `POST /{phone_number_id}/media` → enviar por `id`.

## P6 — PWA (app na tela inicial da Thainá)

Escopo decidido: **PWA do painel atual**, sem push.
- `manifest.json`, service worker mínimo, ícones.
- "Adicionar à tela inicial" no celular dela; abre em tela cheia.
