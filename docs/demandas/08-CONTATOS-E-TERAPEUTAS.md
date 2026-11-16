# Demanda G — Terapeutas em aba própria e iniciar conversa pelo painel

**Aberta em:** 16/11/2026 · **Status:** código pronto e testado (16/11); falta o template na Meta e o deploy · **Card:** sem card

## História

- **Como** usuário do painel da Sofia, **quero** iniciar uma conversa com quem
  ainda não escreveu (paciente ou terapeuta, já conhecido ou número novo),
  **para** não depender de a pessoa mandar a primeira mensagem.
- **Como** usuário do painel, **quero** uma aba Terapeutas separada de
  Conversas, com o histórico completo (lembretes inclusive), **para** ver o que
  a Sofia mandou para a Amanda ou o Victor e poder assumir essa conversa.

## Decisões fechadas (grilling de 16/11/2026)

1. **Primeira mensagem fora da janela de 24h = um template fixo** aprovado na
   Meta, com o nome da pessoa e botão de resposta. Texto livre só depois que ela
   responder. Nome do template fica em configuração, não no código.
2. **Contato = a própria `conversa`.** Sem tabela nova. Adicionar número cria
   uma conversa sem mensagens. "Reiniciar conversa" também tira da agenda
   (aceito: o botão é de teste).
3. **Quem atende a resposta é a equipe.** Iniciar contato = assumir: modo
   humano, interrompe pesquisa/cobrança em curso, a Sofia não faz triagem nem
   manda o aviso de "a equipe vai retornar".
4. **Aviso da resposta = fila Hoje + `alerta_thaina`.** A 1ª resposta ao convite
   abre escalada com motivo novo `respondeu_contato`; o contexto leva quem
   iniciou e a nota interna.
5. **Formulário de número novo: nome + número BR obrigatórios.** DDD + número,
   o 55 entra sozinho. Opcionais: tipo (paciente/terapeuta) e nota interna.
   Número estrangeiro fora de escopo. Número repetido abre a conversa existente.
6. **Só para quem deu o número para a Allos.** Checkbox obrigatório "esta
   pessoa passou o número para a Allos", gravado com quem confirmou. Template
   escrito como retorno de contato esperado (categoria utilidade, não
   marketing). Vale para paciente **e** terapeuta.
7. **Conversa de terapeuta é um chat normal.** Tudo é gravado: lembretes,
   respostas, toques em botão, resposta fixa, avisos de encaminhamento **e os
   balões de agenda completos, com nome de paciente** (decisão consciente; ver
   riscos). A equipe pode "Assumir controle"; em modo humano a Sofia para de
   responder ao terapeuta (resposta fixa e balões incluídos).

## Fora de escopo

- Envio em massa / campanha. Um contato por vez.
- Prospecção fria (número que não deu consentimento).
- Vários templates por finalidade (dá pra crescer a partir do item 1).
- Sofia conduzindo a resposta ao convite (reativação de lead por bot).
- Recuperar lembretes antigos: o histórico começa no deploy.

---

## Parte 1 — Gravar a troca com terapeuta

- [x] `lembretes_terapeutas.rodar`: depois de cada template enviado, obter/criar
      a conversa do número (`eh_terapeuta=True`, `contato_conferido_por='hamilton'`)
      e gravar a mensagem `origem='bot'`, `tipo='template'`, com o wamid e um
      texto legível do lembrete (montado das variáveis, como
      `encaminhamento` já faz para `proposta_horario_paciente`).
- [x] Gravar também os avisos de encaminhamento ao terapeuta
      (`encaminhamento.py`, template `encaminhamento_terapeuta`).
- [x] Webhook: no portão de terapeuta, **gravar a mensagem recebida antes** de
      responder (texto, botão com o rótulo, mídia), com a mesma idempotência
      por wamid.
- [x] `responder_terapeuta` e `responder_botao`: gravar o que sai (resposta
      fixa, balões de agenda completos, `ERRO_DETALHE`).
- [x] Gravar **não** pode mexer em `atualizada_em` de forma a embaralhar a
      lista (mesmo cuidado de `contato.definir`) — conferir.
- [x] Testes: lembrete gera conversa + mensagem; resposta do terapeuta e toque
      em botão aparecem; reentrega não duplica; dry run não grava como enviado.

## Parte 2 — Modo humano para terapeuta

- [x] Portão do webhook: conversa de terapeuta em `modo='humano'` → só grava,
      não responde (nem resposta fixa, nem balões, nem `capturar_motivo`?).
      ⚠️ Decidir na implementação: toque em Aceito/Não aceito de
      encaminhamento **continua** gravando o vínculo no Hamilton mesmo em
      modo humano (é ação de negócio, não conversa).
- [x] "Devolver ao bot" volta ao comportamento automático.
- [x] Testes do portão nos dois modos.

## Parte 3 — Aba Terapeutas

- [x] Nova aba "Terapeutas" no `_topbar.html` (desktop e `nav-baixo`, ao lado
      de Conversas), rota `/painel/terapeutas`.
- [x] Reaproveitar `listar_conversas` com filtro fixo `eh_terapeuta=True`
      (busca, ordem, paginação, polling HTMX iguais).
- [x] Aba Conversas passa a mostrar só pacientes; saem os filtros
      "Só pacientes" / "Só terapeutas" de `painel.FILTROS`.
- [x] Tela da conversa: mesmo template; ajustar textos que assumem paciente
      (cadastro, cobrança, pesquisa não aparecem para terapeuta).
- [x] Validar com Playwright (desktop 1440 / mobile 390, console limpo).

## Parte 4 — Template de início de contato (externo, começa cedo)

- [x] Redigir o texto (utilidade, retorno de contato esperado, `{{1}}` = nome,
      botão de resposta rápida "Pode falar").
- [ ] Paulo submete na Meta e aguarda aprovação — **caminho crítico**.
- [x] Configuração `template_inicio_contato` (nome) em `config_negocio`.

## Parte 5 — Iniciar conversa com quem já está na lista

- [x] Na conversa (paciente e terapeuta), indicador "janela aberta até HH:MM" /
      "janela fechada", calculado da última mensagem recebida.
- [x] Janela aberta → compositor normal (já existe).
- [x] Janela fechada → botão "Iniciar contato" no lugar do compositor: envia o
      template, grava como mensagem, assume a conversa (decisão 3) e marca
      `contato_iniciado_em` / `contato_iniciado_por`.
- [x] Hoje o envio fora da janela falha em silêncio (só log em
      `painel.py:663`): mostrar o erro na tela.
- [x] Migration: `contato_iniciado_em`, `contato_iniciado_por`,
      `contato_consentimento_por`, `contato_nota`.

## Parte 6 — Adicionar número novo

- [x] Botão "Nova conversa" em Conversas e em Terapeutas (tipo pré-marcado
      pela aba).
- [x] Formulário: nome*, DDD+número*, tipo, nota interna, checkbox de
      consentimento*. Validação de celular BR; normaliza para `55…`.
- [x] Número já existe → redireciona pra conversa existente (sem duplicar).
- [x] Cria conversa com `dados_coletados.nome_completo`, `eh_terapeuta` e
      `contato_conferido_por='manual'`, e segue o fluxo da Parte 5.
- [x] 🔴 **Nono dígito.** Confirmar na doc da Cloud API se a resposta do
      envio traz o `wa_id` real; se trouxer, gravar a conversa com ele. Senão,
      o webhook precisa casar `55DD9XXXXXXXX` com `55DDXXXXXXXX` antes de criar
      conversa nova — do contrário a resposta cai numa 2ª conversa em modo bot
      e a Sofia faz triagem com quem a equipe chamou.

## Parte 7 — Resposta ao convite

- [x] Webhook: 1ª mensagem recebida em conversa com `contato_iniciado_em`
      (e sem resposta ainda) → escalada `respondeu_contato` + `alerta_thaina`
      (contexto: quem iniciou + nota). Só a 1ª.
- [x] Novo motivo em `tools.MOTIVO_LABELS` e em `hoje.PRIORIDADE`.
- [x] Sem aviso "a equipe vai retornar" nesse caso.
- [x] Testes: alerta sai uma vez; terapeuta em modo humano também alerta.

## Parte 8 — Fechamento

- [x] Atualizar `CLAUDE.md` (portão de terapeuta agora grava; balões com nome
      no banco; aba Terapeutas; início de contato).
- [ ] `/test`, `/security-review`, teste de ponta a ponta com dry run.

## Riscos registrados

- **LGPD — agenda clínica no banco da Sofia (decisão 7).** Os balões de agenda
  levam nome de paciente e passam a ser visíveis a todo usuário do painel. Antes
  isso era evitado de propósito. Aceito em 16/11/2026; reavaliar se o painel
  ganhar usuários fora da coordenação.
- **Qualidade do número.** O número é o mesmo da Sofia; denúncia de spam reduz o
  limite de envio de tudo (lembretes, alertas). Mitigação: consentimento
  obrigatório e template de utilidade.
- **Reclassificação do template como marketing** (já aconteceu em 05/11 com o
  lembrete dos terapeutas). Acompanhar os avisos de `status_entrega`.

## O que foi feito (16/11/2026)

- **Partes 1–3, 5–7: implementadas e testadas** (suíte inteira verde + 23 testes
  novos em `tests/test_inicio_contato.py`; validação no navegador em desktop e
  celular, console sem erro).
- Ajustes em relação ao plano:
  - Lembrete é gravado **também em dry run** (ajuda a validar localmente; o
    dry run não confirma ao Hamilton, como antes).
  - O motivo de recusa do encaminhamento continua sendo tratado em modo humano.
  - Na conversa de terapeuta, o cartão "Dados coletados" some e o rótulo virou
    "Respostas automáticas" (ela ainda responde sozinha aos botões do lembrete).
- Migration nova: **`c7e1f5a20b48`** (5 colunas em `conversa`).
- Config nova no `/painel/config`: **`template_inicio_contato`** (padrão
  `inicio_contato_allos`; em branco desliga o botão).

## 🔴 Falta fazer fora do código (Paulo)

1. **Submeter o template na Meta** (categoria **Utilidade**, idioma `pt_BR`),
   nome `inicio_contato_allos`. Texto sugerido:
   > Olá, {{1}}! Aqui é a equipe da Allos, retornando o contato que você pediu.
   > Podemos falar por aqui?
   Botão de resposta rápida: **"Pode falar"**. Exemplo para {{1}}: `Maria`.
   Se a Meta aprovar com outro nome, troque em `/painel/config`.
2. **Deploy**: merge → `alembic upgrade head` (o build do Render já roda).
3. Conferir nos logs de `status_entrega` se o template não foi reclassificado
   como marketing (já aconteceu em 05/11 com o lembrete dos terapeutas).

## Ordem sugerida (original)

Parte 4 começa já (espera da Meta). Em paralelo: 1 → 2 → 3 (entrega a aba
Terapeutas sozinha, sem depender do template). Depois 5 → 6 → 7 → 8.
