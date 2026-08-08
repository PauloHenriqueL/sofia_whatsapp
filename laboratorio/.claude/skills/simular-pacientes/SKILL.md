---
name: simular-pacientes
description: Roda a bateria de pacientes simulados contra a Sofia (laboratorio/) e relata os problemas de comunicação com evidência. Use quando o usuário pedir "testar a Sofia", "simular pacientes", "rodar o laboratório", "testar o prompt", "rodar as personas", ou quiser saber se uma mudança no prompt melhorou ou piorou a conversa. NÃO edita prompt — para no achado.
---

# Simular pacientes

Oito personas conversam com a Sofia de verdade (LLM real, código real, Hamilton
falso, WhatsApp bloqueado) e o resultado vira achados com citação.

## A regra que não se negocia

**Esta skill não edita nada dentro de `prompt/`.** Ela para no relatório de
achados. Consertar é a outra skill (`melhorar-prompt`), e a separação existe
porque o erro clássico aqui é achar um problema e já sair reescrevendo o prompt
antes de o Paulo ter visto a evidência.

Se ele pedir "e já arruma", relate primeiro, depois chame a outra skill.

## 1. Rode

```bash
python laboratorio/rodar.py
```

São ~8 conversas em paralelo, alguns minutos. Variações:

| | |
|---|---|
| `--persona acha-caro --repetir 3` | confirmar um achado antes de mexer no prompt |
| `--sequencial` | depurar uma conversa com calma |
| `--turnos-max 15` | encurtar |

Nomes: `oi-e-nada-mais`, `chega-com-a-dor`, `preco-primeiro`, `acha-caro`,
`mae-do-adolescente`, `cetica`, `ambivalente`, `prefeitura`.

Se alguma conversa vier com `motivo_parada: erro`, leia o `traceback` no JSON
dela em `laboratorio/execucoes/<carimbo>/` — normalmente é `.env` faltando ou a
costura do `contador.py` tendo quebrado.

## 2. Leia as métricas

`laboratorio/relatorios/<carimbo>/resumo.md`. É **piso, não veredito**: ele diz
onde olhar, não o que concluir. As linhas que mais importam são a primeira bolha
em caracteres, a razão Sofia:pessoa e o número de desistências — e o delta contra
a rodada anterior, que é o único jeito de ver regressão.

## 3. Detecte com contexto limpo

**Dispare um subagente** (`Explore` ou `general-purpose`) e mande ele ler
`laboratorio/relatorios/<carimbo>/transcricoes.md` e `laboratorio/rubrica.md`.

**Ele não pode ver `prompt/sofia_v01.txt`.** Diga isso explicitamente no prompt
dele. Esse é o ponto inteiro do subagente: você já leu o prompt nesta conversa e
está contaminado — vai ler "ofereceu as três opções" como conformidade em vez de
defeito. Ele lê como a pessoa que recebeu as mensagens leu.

Peça o formato de acusação da rubrica: categoria, persona, turno, **citação
literal** e por que dói. Peça também o que funcionou.

## 4. Leia você mesmo

Não repasse os achados do subagente sem ler. Leia `transcricoes.md` inteiro — são
8 conversas, cabe. O subagente tem alto recall e baixo julgamento; você é quem
distingue "isso é chato" de "isso perdeu a venda".

## 5. Relate

Para o Paulo, em português, nesta ordem:

1. **O que mais custa dinheiro** — o achado com maior impacto em venda, com o
   trecho colado.
2. **O padrão por trás dos achados**, não a lista deles. Seis violações que saem
   da mesma causa são um problema, não seis.
3. **O que regrediu** desde a rodada anterior, se houver.
4. **O que ficou bom**, com trecho. Rodada que só acusa é rodada que perdeu a
   régua.
5. **O que você não sabe dizer com N=1** e valeria `--repetir 3` antes de mexer.

Não proponha mudança de prompt aqui. Termine oferecendo a `melhorar-prompt`.

## O que este laboratório não testa

Encanamento (é `pytest tests/`), ponta a ponta com Hamilton real (é a skill
`testar-conversa`), pesquisas de satisfação, áudio, painel e webhook.
