# Demanda F — O painel como app instalável (e, depois, com notificações)

**Data do fechamento do desenho:** 13/11/2026.
**Status:** entrega 1 **implementada e validada no navegador** (13/11); falta o
teste em Android real depois do deploy. Entrega 2 só desenhada.
**Mexe só na Sofia.** O Hamilton não é tocado em nenhuma das duas entregas.

> Este documento é o **registro de decisão**. As alternativas descartadas estão
> aqui de propósito, pra ninguém redescobrir por que não fizemos de outro jeito.

---

## O que é

A pessoa da equipe entra no painel pelo navegador do celular, toca em
**"Instalar"** e a Sofia passa a morar na tela inicial, abrindo em tela cheia
como um aplicativo. Numa segunda entrega, esse app instalado recebe
**notificações** dos mesmos eventos que hoje chegam por WhatsApp.

## O que já existe (e por isso a demanda é pequena)

O painel **já é um PWA instalável**. Conferido no código em 13/11:

| Peça | Onde | Estado |
|---|---|---|
| Manifest (`display: standalone`, `start_url: /painel/`, ícones 192/512 e maskable) | `app/static/manifest.webmanifest`, servido da raiz por `app/main.py` | pronto |
| Service worker com `fetch`, escopo `/` | `app/static/sw.js`, servido da raiz | pronto |
| Registro do SW e metatags | `app/templates/base.html` | pronto |
| Navegação de celular (barra embaixo) | `_topbar.html` + `allos.css` | pronto |
| Teste | `tests/test_pwa.py` | existe |

**O que falta é só a porta de entrada.** Hoje, pra instalar, a pessoa precisa
saber que existe "Adicionar à tela inicial" escondido no menu de três pontos do
Chrome. Ninguém descobre isso sozinho.

---

## Decisões (grilling de 13/11)

| # | Decisão | Escolha |
|---|---|---|
| 1 | O que é "virar app web" | Botão de instalar o painel que já existe. **Sem trocar a stack** (segue Jinja + HTMX) e sem sidebar: o botão vai na barra do topo. |
| 2 | Quem instala | **Só a equipe do painel** (quem está na tabela `usuario`). Terapeuta e paciente seguem só no WhatsApp. |
| 3 | Até onde vai agora | **Entrega 1 = só o botão.** A entrega 2 (notificações) fica desenhada aqui, sem código. |
| 4 | Onde o botão aparece | **Barra do topo** (todas as telas do painel) **e tela de login**. |
| 5 | iPhone | **Fora por enquanto.** O botão só aparece onde o navegador oferece instalação com um toque. |
| 6 | Computador | **Aparece também** no Chrome/Edge do PC, onde o navegador oferecer. |
| 7 | Notificação x WhatsApp | **Somam.** Mesmos eventos nos dois canais; cada pessoa liga/desliga cada um. |
| 8 | Texto da notificação | **Sem nome de paciente.** Só o tipo do evento; o nome aparece depois de abrir o app, logado. |

---

## Entrega 1 — O botão "Instalar"

### Como funciona

O Chrome (Android e desktop) e o Edge disparam o evento `beforeinstallprompt`
quando a página cumpre os critérios de instalação. O desenho inteiro se apoia
nele:

1. O botão nasce **escondido** (`hidden`) no HTML.
2. Um script pequeno escuta `beforeinstallprompt`, chama `preventDefault()`,
   guarda o evento e **mostra o botão**.
3. No toque, chama `evento.prompt()`: abre a janela de instalação do próprio
   sistema. Depois de usado o evento não serve mais, então o botão se esconde
   de novo (se a pessoa recusou, ele volta no próximo carregamento de página).
4. O evento `appinstalled` esconde o botão de vez.

**Não há detecção de aparelho por user agent.** iPhone e Firefox nunca disparam
o evento, então o botão simplesmente nunca aparece neles: a decisão 5 sai de
graça e não precisa de regra. Com o app já instalado o Chrome também não
dispara, e o botão some sozinho.

### O que muda, arquivo por arquivo

- **`app/static/instalar.js`** (novo) — a lógica acima. Carregado no `<head>` do
  `base.html`, **sem `defer`**: o evento pode chegar cedo, e listener registrado
  tarde perde o disparo. Procura o botão por atributo (`[data-instalar]`) na
  hora de mostrar, porque a página pode ter um (topo) ou outro (login).
- **`app/templates/base.html`** — inclui o script.
- **`app/templates/_topbar.html`** — `<button data-instalar hidden>` em
  `.topbar-right`, como **filho direto**. Isso importa: até 760px o CSS esconde
  `.topbar-right .muted` e `.topbar-right form`, e um botão solto escapa dessas
  duas regras e continua visível no celular, que é onde ele mais interessa.
- **`app/templates/login.html`** — o mesmo botão, discreto, abaixo de "Entrar".
- **`app/static/allos.css`** — estilo do botão no topo (base `.btn .btn-sm`, na
  cor da marca; no celular precisa de área de toque de 44px) e do link no login.
  Subir o `?v=` do CSS no `base.html`.
- **`app/static/manifest.webmanifest`** — acrescentar `"id": "/painel/"`. Sem
  `id` o navegador identifica o app pelo `start_url`; se um dia ele mudar, quem
  já instalou vira "outro app". Os `shortcuts` passam a ser Conversas e
  Acompanhamento (Hoje já é a tela em que o app abre).
- **`app/static/sw.js`** — `instalar.js` entra no `SHELL` e `VERSAO` sobe pra
  `sofia-v2`. **A regra de não cachear `/painel/`, `/api/` e `/login` não muda.**
- **`tests/test_pwa.py`** — o botão existe escondido no topo e no login; o
  manifest tem `id`; `/static/instalar.js` responde.

### O que apareceu na implementação

- 🔴 **`hidden` sozinho não esconde o botão.** O `display:inline-flex` do `.btn`
  é regra de autor e vence o `hidden` da folha padrão do navegador. Sem
  `[data-instalar][hidden] { display:none !important; }` o botão apareceria
  sempre, inclusive no iPhone. Há teste pra isso.
- **Tela de 320-380px:** com o botão à mostra, a marca quebrava em duas linhas
  e encostava nele. Nessa faixa o "· painel Allos" some **só enquanto o botão
  existe** (`.topbar:has(...)`); instalado, a marca volta inteira.
- **`<meta name="mobile-web-app-capable">`** entrou ao lado da versão `apple-`,
  que o Chrome marca como obsoleta no console.

### Texto

- Topo: **"Instalar"**, com ícone.
- Login: **"Instalar o app"** (não "no celular": o botão também aparece no PC).
- Depois de instalar: nada. O botão some e o app abre; não precisa de aviso.

### Sessão: o app não pede senha toda hora

Conferido: o `SessionMiddleware` usa o padrão do Starlette, **14 dias**, e o
cookie é reemitido a cada resposta. Quem abre o app pelo menos uma vez a cada
duas semanas não vê a tela de login de novo. **Nada a mudar.**

### Como validar

1. ✅ **Playwright, desktop (1440px) e celular (390, 360 e 320px):** topo (nas
   sete telas do painel) e login. O Chromium do Playwright **dispara o evento
   de verdade** em `localhost`: o botão apareceu sozinho, o toque abriu a
   instalação do navegador, o app foi instalado, a página passou a
   `display-mode: standalone` e o botão sumiu. O cenário "navegador que não
   avisa" (iPhone, Firefox) foi simulado engolindo o evento antes dos scripts
   da página: o botão ficou escondido. Console sem erro nem aviso.
   ⚠️ Esse teste **instala o app de verdade** no perfil do Chromium de teste
   (cria um atalho "Sofia — Allos" no menu do sistema). Desinstalar depois.
2. ✅ **`pytest`**: 822 passando, 8 testes novos em `tests/test_pwa.py`.
3. ⏳ **Android de verdade, em produção** (precisa de HTTPS; o Render tem): entrar
   pelo Chrome, tocar em "Instalar", confirmar, abrir pelo ícone, ver que abre
   em tela cheia e já logado. **Esse passo não tem como automatizar** e é o
   único que prova a entrega.
4. ⏳ Abrir no Chrome do PC, em produção, e repetir.

### Riscos

- **O Chrome pode demorar a oferecer.** Ele decide quando disparar o evento; se
  a pessoa recusou a instalação há pouco, ele segura por um tempo. Nesses casos
  o botão não aparece e o caminho pelo menu de três pontos continua valendo.
- **Link aberto de dentro do WhatsApp** abre num navegador embutido que não
  instala nada. O botão não aparece lá. Orientar a equipe a abrir no Chrome.

---

## Entrega 2 — Notificações (desenhada, não codificada)

### O que dispara

Os mesmos eventos do `alerta_thaina`: escalada, cadastro novo, reencontro,
**cadastro que falhou** e nota baixa em pesquisa. Todos já passam por um ponto
só, `escalation.alertar_equipe`. A notificação entra **ao lado** do envio de
WhatsApp, com `try/except` próprio: um canal falhar não pode calar o outro, e
nenhum dos dois pode derrubar a conversa (regra que o alerta já segue).

### Peças

- **Web Push com VAPID.** Env vars novas: `VAPID_PUBLIC_KEY`,
  `VAPID_PRIVATE_KEY`, `VAPID_SUBJECT`. **Vazias = recurso desligado**, como o
  `TASKS_TOKEN` e o Stripe.
- **Tabela `inscricao_push`** (migration nova): `usuario_id` (CASCADE),
  `endpoint` (único), `p256dh`, `auth`, `aparelho` (rótulo pra pessoa
  reconhecer), `criada_em`, `ultimo_envio_em`. **Uma linha por aparelho**, não
  por pessoa: a Thainá pode ter celular e PC.
- **`app/services/notificacoes.py`** (novo): inscrever, remover e
  `avisar_equipe(...)`. Resposta 404/410 do serviço de push apaga a inscrição
  (aparelho desinstalou ou revogou).
- **Rotas** `POST /api/push/inscrever` e `/api/push/remover`, com
  `requer_login_api` e `verificar_origem`.
- **`sw.js`**: handler `push` (mostra a notificação) e `notificationclick`
  (abre ou foca o app na tela certa).
- **Painel**: chave "Notificações neste aparelho". O navegador só deixa pedir a
  permissão em resposta a um toque, nunca ao carregar a página.

### Regras que saem das decisões 7 e 8

- **Dois interruptores independentes.** `usuario.recebe_alertas` segue valendo
  só pro WhatsApp. No app, estar inscrito *é* o interruptor: desligar remove a
  inscrição daquele aparelho.
- **O texto vem de uma lista fixa por tipo de evento**, nunca de texto livre.
  Hoje o `rotulo` que vai pro template é montado em vários pontos e precisa ser
  auditado antes de ser reaproveitado: se algum carregar nome ou trecho de
  conversa, a decisão 8 fura sem ninguém perceber.
- **Toque abre a conversa.** `alertar_equipe(db, nome, rotulo)` hoje não recebe
  o id da conversa; vai precisar receber, pra notificação levar o destino.
- **Nada do conteúdo vai pro log** (LGPD), só tipo e quantidade de aparelhos.

### Armadilhas já conhecidas

- 🔴 **A trava de rede dos testes não cobre isto.** O `tests/conftest.py` trava
  o transporte do **httpx**. A biblioteca usual de Web Push (`pywebpush`) usa
  `requests`, que passa por fora da trava: um teste mal mockado mandaria
  notificação de verdade. Ou o envio é feito sobre httpx, ou a trava ganha uma
  segunda porta. É o mesmo acidente que criou quatro Payment Links na conta
  live do Stripe.
- **O Android atrasa ou engole notificação** de app em economia de bateria. É
  por isso que a decisão 7 é "somam": o WhatsApp continua sendo a rede.
- **iPhone.** Lá, notificação de site só funciona com o app instalado na tela
  inicial (iOS 16.4+). Enquanto a decisão 5 valer, **iPhone não recebe
  notificação**. Pra incluir, o primeiro passo é o passo a passo de instalação
  que foi deixado de fora.

### Em aberto pra quando a entrega 2 começar

- A chave de notificações aparece só no app instalado ou também no navegador?
- Vale usar o contador de pendências da tela Hoje como selo no ícone do app?

---

## Alternativas descartadas

- **Reescrever como SPA (React/Next + API JSON).** Reescreve 17 telas e a
  autenticação pra chegar no mesmo lugar: o que faz um site "virar app" no
  celular é manifest + service worker, e isso já existe.
- **App nativo ou publicado na Play Store.** Conta de desenvolvedor, revisão de
  loja e um segundo código pra manter, pra um público de poucas pessoas.
- **Sidebar.** Foi a ideia inicial; o topo resolve sem redesenhar a navegação,
  e no celular o canto direito da barra já está vazio.
- **Faixa de aviso na tela Hoje.** Ocupa o espaço da fila de trabalho e só
  existe numa tela.
- **Passo a passo pro iPhone.** Adiado, não rejeitado (ver armadilha acima).
- **Notificação substituir o WhatsApp.** Se o Android calar o app, o alerta
  some sem ninguém saber.

## Fora do escopo

Terapeutas e pacientes com login, uso offline (o service worker não guarda dado
de paciente no aparelho, e continua assim) e qualquer mudança no Hamilton.
