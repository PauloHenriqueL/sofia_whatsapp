# Backlog — demandas maiores

Ordem de execução. As 9 demandas pequenas (prompt/base de conhecimento + "é a
Sofia de novo") já foram entregues no commit `a2a2fdf`.

---

## P0 — Sanitizar a saída do modelo antes de mandar pro paciente

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

Isso não se resolve com instrução no prompt: LLM não dá garantia de formato, e o
custo do vazamento é alto (JSON com nome, nascimento e endereço de uma paciente
real foi entregue no WhatsApp dela — incidente de LGPD, dado de saúde).

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

## P1 — Painel: filtro, ordenação e busca

- Ordenar por qualquer coluna, em todas as tabelas (lista de conversas e acompanhamento).
- Trocar os balõezinhos de filtro por um **botão de filtro** onde a Thainá monta o filtro
  que quiser (modo, estado, período).
- **Campo de pesquisa** (nome, número, conteúdo de mensagem) nas tabelas.
- Navegação: balão "Todas as conversas" ao lado de "Acompanhamento", pra alternar fácil.

## P2 — Assumir controle pra digitar

- O campo de texto da conversa fica **coberto** por um botão "Assumir controle".
- Ao clicar, o botão some e o campo aparece.
- Ao sair da conversa (com controle assumido), perguntar:
  **"Quer que o bot assuma daqui pra frente?"** com Sim / Não.

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
