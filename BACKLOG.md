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

## ✅ P3 — Imagem e documento (recebimento) — ENTREGUE

Escopo: **só painel**, sem visão do modelo.
- Tabela `midia` (migration `f9a0b1c2d3e4`): bytes no Postgres, porque a URL da Meta
  expira em minutos e o filesystem do Render é recriado a cada deploy. Teto de 8 MB
  por arquivo (`midia.TAMANHO_MAXIMO`) — se ficar apertado, é hora do bucket externo.
- `conteudo` é `deferred`: o poll de 5s do painel lê os metadados sem arrastar blobs.
- Imagem vira miniatura clicável; documento vira ícone + nome. Ambos com "Baixar".
- A Sofia guarda o anexo e escala (`anexo_recebido`). Download falho ainda registra a
  mensagem, pra Thainá ver que veio algo e pedir de novo.
- `excluir_conversa` apaga a mídia junto (senão o anexo ficaria órfão — LGPD).
- **Segurança** (o nome e o MIME vêm do paciente e vão pra headers HTTP):
  `nome_para_download` neutraliza header injection e path traversal; `mime_seguro` é
  uma **allowlist** de formatos raster + PDF, não um prefixo `image/` — `image/svg+xml`
  executa `<script>` e seria XSS na origem do painel. O resto vai como `attachment`
  + `X-Content-Type-Options: nosniff`. A rota exige login.

## ✅ P4 — Responder mensagem específica (reply-to) — ENTREGUE

- Coluna `mensagem.responde_a_id` (migration `a0b1c2d3e4f5`), auto-referência com
  `ON DELETE SET NULL`. O painel renderiza a citação acima da mensagem.
- No WhatsApp, sai como reply de verdade (`context.message_id` da Cloud API).
- **Passamos a guardar o wamid das mensagens que ENVIAMOS** (bot e Thainá). Sem
  isso não dava pra citar a própria fala da Sofia — só o que o paciente mandou.
  Mensagens antigas não têm wamid: o botão de citar não aparece nelas.
- `responde_a_id` vem do form, então o serviço valida que a mensagem citada é
  **desta** conversa (senão vazaria mensagem de outro paciente).
- A Sofia (bot) ainda não cita; só a Thainá. O encanamento está pronto se quisermos.

## ✅ P5 — Thainá enviar foto e documento — ENTREGUE

- Botão de clipe no compositor. Upload → `subir_midia` (`POST /media`) → `enviar_midia`.
- Imagem vai como `image` (com legenda); o resto como `document` (com `filename`).
- Cópia guardada na tabela `midia`, então o painel mostra o que foi enviado.
- Teto de 8 MB checado **na leitura** (`read(MAX+1)`), não depois: um upload de
  500 MB não chega a entrar na memória do processo. Erro 413 se passar.
- Texto + anexo na mesma mensagem = legenda. Só texto = mensagem simples.
  Nem texto nem anexo = nada é enviado (validado no cliente e no servidor).

## ✅ P6 — PWA (app na tela inicial da Thainá) — ENTREGUE

Escopo: **PWA do painel atual**, sem push.
- `manifest.webmanifest` + `sw.js`, ambos servidos da **raiz** (`app/main.py`). O SW
  precisa estar na raiz: servido de `/static/sw.js` seu escopo seria `/static/` e o
  navegador não ofereceria instalar o app.
- Ícones: **"S" da Fraunces** (a `--font-display` do design system) sobre o gradiente
  teal do `.brand .logo`. 192/512 normais + 192/512 *maskable* (o Android recorta 20%
  das bordas) + apple-touch 180 + favicon. Gerados por script com `fontTools` +
  `Pillow` — **não** são dependência de runtime.
- **O SW não cacheia `/painel/`, `/api/` nem `/login`**: dado de saúde não pode ficar no
  disco do celular (LGPD), a sessão expira, e o painel já se atualiza via HTMX. Só o
  `/static/` é cacheado (network-first).
- Responsivo: toolbar empilha, abas deslizam, tabela rola na horizontal mantendo o
  `thead` (senão a Thainá perderia a ordenação no celular), safe-area do iPhone.

### Como a Thainá instala
Abre https://sofia-whatsapp.onrender.com no celular → menu do navegador →
"Adicionar à tela inicial" (Android: aparece o convite sozinho; iPhone: Compartilhar →
Adicionar à Tela de Início).

---

## Ideias fora do backlog atual
- Testes mais completos das entregas P4/P5/P6 (o Paulo pediu pra priorizar
  desenvolvimento; P0–P3 têm cobertura, P4/P5 têm o essencial, P6 não tem).
- Notificação push quando chega escalada nova (exige VAPID; no iPhone só depois de
  instalado).
- A Sofia (bot) citar mensagem específica — o encanamento do P4 já suporta.
