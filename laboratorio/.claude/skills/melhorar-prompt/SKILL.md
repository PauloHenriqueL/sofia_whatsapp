---
name: melhorar-prompt
description: Parte dos achados de uma rodada do laboratório e muda o prompt da Sofia com grilling antes e verificação depois. Use quando o usuário pedir "melhorar o prompt", "arrumar isso no prompt", "aplicar os achados", "mudar a Sofia" depois de uma simulação. Uma mudança por vez, e nada é dado por bom sem repetir a persona.
---

# Melhorar o prompt da Sofia

Vem depois de `simular-pacientes`, nunca antes. Se não houver uma rodada recente
com achados, rode aquela primeiro: mudar o prompt olhando uma conversa é como o
prompt chegou a 30 KB.

## As quatro travas

**1. Uma mudança por vez.** Um achado, um trecho do prompt, um diff pequeno.
Reescrever a Fase 0 inteira porque a abertura ficou ruim torna impossível saber o
que causou o quê na rodada seguinte.

**2. Grilling antes de editar.** Quase toda mudança de prompt é uma decisão de
negócio disfarçada de texto. "Deixar a pessoa falar antes de oferecer o menu" é
escolher perder roteamento rápido em troca de acolhimento — e quem escolhe é o
Paulo, não você. Abra a rodada de grilling do `~/.claude/CLAUDE.md`.

**3. Toda proposta diz o que REMOVE.** `prompt/sofia_v01.txt` tem 398 linhas.
Boa parte do "pouco humanizado" é inchaço: regra demais brigando com regra
demais faz o modelo obedecer à literal em vez do espírito. Se a sua proposta só
acrescenta texto, justifique por que não dá para fazer cortando. Na dúvida,
corte.

**4. Nada é resolvido sem verificação.** Depois de editar:

```bash
python laboratorio/rodar.py --persona <a-persona-do-achado> --repetir 3
```

Três conversas, não uma: os dois lados são estocásticos, e a violação sumir uma
vez pode ser sorte. Se possível, rode `--repetir 3` **antes** da edição também —
sem o antes, você não sabe se estava consertando algo que acontecia sempre ou de
vez em quando.

## Procedimento

1. Leve o achado com a citação literal para o Paulo. Comece pelo trecho da
   transcrição, não pela sua interpretação.
2. Localize o que no prompt produziu aquilo. Cite `arquivo:linha`. Se você não
   encontra a linha responsável, provavelmente o problema não é do prompt — pode
   ser o modelo, o histórico ou a base de conhecimento, e a mudança seria
   superstição.
3. Grilhe a decisão. O que se ganha, o que se perde, quem decide.
4. Edite. Diff pequeno e legível.
5. Verifique com `--repetir 3`.
6. Rode a rodada completa (`python laboratorio/rodar.py`) e olhe os deltas do
   `resumo.md`: a pergunta que importa agora não é "melhorou aqui", é **"piorou
   em outro lugar"**.

## Onde mexer

- `prompt/sofia_v01.txt` — fluxo, tom, fases, regras de vocabulário.
- `prompt/sofia-base-conhecimento.md` — fatos que a Sofia responde.
- `laboratorio/rubrica.md` — se a definição de "bom" mudou, ela muda junto. Uma
  regra removida do prompt tem que sair da rubrica, senão a rodada seguinte acusa
  algo que já não é mais regra.

Os arquivos são o padrão; a Thainá pode ter sobrescrito o texto em
`/painel/prompts`, e nesse caso o painel manda. Se um achado não bate com o
arquivo, é a primeira coisa a checar.
