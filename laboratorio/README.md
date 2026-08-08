# Laboratório — testar o que a Sofia *diz*

Oito personas conversam com a Sofia de verdade e o resultado vira achados com
citação. Tudo desta pasta é isolado: nada aqui roda em produção, nada aqui é
importado pela app.

```bash
python laboratorio/rodar.py                              # a rodada
python laboratorio/rodar.py --persona acha-caro --repetir 3   # confirmar um achado
```

Saída em `relatorios/<carimbo>/`: `resumo.md` (números + deltas),
`transcricoes.md` (as conversas) e `dados.json` (o placar entre versões).

## Por que existe

`pytest tests/` mocka OpenAI, Hamilton e Meta: prova que o encanamento liga, não
que o modelo obedece ao prompt. A skill `testar-conversa` prova ponta a ponta,
mas uma conversa por vez, com uma pessoa digitando. Nenhuma das duas responde a
pergunta que decide a operação: **uma pessoa de verdade sairia dessa conversa
querendo se cadastrar?**

## As três camadas

| camada | quem faz | o que responde |
|---|---|---|
| métricas | `metricas.py`, determinístico | quanto, quantas vezes, onde olhar |
| detecção | subagente de **contexto limpo** | o que está errado, com citação |
| julgamento | Claude, com o Paulo | o que fazer a respeito |

A detecção roda num subagente que **nunca vê `prompt/sofia_v01.txt`**. Quem
conhece o prompt lê o defeito como conformidade — "ela ofereceu as três opções
porque o prompt manda" — e para de reportar justamente o que interessa. A
`rubrica.md` existe para ser o contrato do que "bom" significa sem depender do
prompt.

Não há juiz-LLM. Oito transcrições cabem inteiras no contexto do Claude; um juiz
intermediário seria complexidade paga para não ser confiada.

## Como uma conversa roda

Um **subprocesso por conversa**, cada um com seu SQLite próprio. Não é
sofisticação: `app.database.engine` nasce no import a partir de
`settings.database_url`, e `config_negocio`/`captacao` guardam cache de módulo —
conversas concorrentes no mesmo processo disputariam os dois.

Dentro do subprocesso, a Sofia é o código real: `webhook._responder_turno`, que
passa por `saida.limpar()` e por `dividir_em_bolhas()` e **persiste uma linha por
bolha**. As métricas de tamanho saem dessas linhas, não de estimativa — é este
texto, e não o texto bruto do modelo, que a pessoa vê no WhatsApp.

## As travas de segurança

| | como |
|---|---|
| Meta | `ENVIRONMENT=development` forçado no subprocesso → `envio_whatsapp_bloqueado`. Cobre `enviar_texto` **e** `enviar_template`: a Thainá não recebe alerta. |
| Hamilton | `HamiltonFalso` injetado no singleton, e `HAMILTON_API_URL` apontado para `127.0.0.1:1` — um caminho que escape do singleton falha barulhento em vez de vazar. |
| Prompt | a skill `simular-pacientes` é proibida de editar `prompt/`. |

## `app/` não é tocado

O laboratório usa três costuras que já existiam como pontos de mock:
`llm_client.get_llm_client`, `hamilton_client.get_hamilton_client` (ambos
`lru_cache`) e `webhook._responder_turno`.

⚠️ **A dívida:** `contador.py` embrulha o cliente da OpenAI para contar tokens,
e depende de `cliente.chat.completions.create(...)` — a forma que
`app/services/llm_client.py` usa hoje. Se aquele arquivo migrar para a Responses
API ou trocar de SDK, é aqui que quebra. Quebra com `AttributeError`, não em
silêncio. A alternativa era expor `usage` no `LLMClient`, o que foi recusado:
instrumentar código que está no ar por causa de uma ferramenta de teste é como
ferramenta de teste vira dívida em produção.

## Os arquivos

```
personas/*.yaml        8 personas. Cada uma existe pra quebrar uma regra.
rubrica.md             o contrato do que "bom" significa. Editável pelo Paulo.
fixtures/captacoes.json  snapshot real da produção (ver a nota abaixo)
rodar.py               orquestra; não importa `app`
conversa.py            uma conversa, um subprocesso
paciente.py            o LLM que interpreta a persona
hamilton_falso.py      guarda o payload que teria ido pro Hamilton
contador.py            tokens e tool calls, sem tocar em app/
metricas.py            o que dá pra contar
relatorio.py           transcricoes.md, resumo.md, dados.json
execucoes/             bruto, gitignored
relatorios/            resumo + dados + transcrição, versionados
```

### Sobre o snapshot das captações

`fixtures/captacoes.json` é o retorno real de `GET /api/v1/captacoes/` da
produção, e é fiel **inclusive no que está quebrado**: a produção não devolve o
campo `is_parceria`, então `captacao.e_parceria()` é `False` até para as
prefeituras (IDs 13 e 46) e um paciente de parceria seria cadastrado com valor
cheio. É a migration perdida no `.gitignore` do `hamilton-api`, registrada no
`CLAUDE.md`. Servir uma lista "consertada" aqui faria o laboratório aprovar um
fluxo que não funciona no ar.
