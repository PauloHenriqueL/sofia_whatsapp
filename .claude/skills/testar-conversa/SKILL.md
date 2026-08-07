---
name: testar-conversa
description: Conversa com a Sofia pelo terminal, ponta a ponta, com LLM real e Hamilton local — sem mandar nada pro WhatsApp. Use para validar prompts, tools, cadastro no Hamilton e as pesquisas de satisfação; ou sempre que o usuário pedir "testar o bot", "conversar com a Sofia", "teste de ponta a ponta", "testar a pesquisa".
---

# Testar a Sofia conversando (dry run)

## Por que esta skill existe

A suite (`pytest tests/`) mocka OpenAI, Hamilton e Meta. Ela prova que o
encanamento liga — não prova **que o modelo obedece ao prompt**. Se a Sofia
chama `registrar_resposta_pesquisa` a cada resposta ou se ela conversa bonito e
não grava nada, nenhum mock responde.

Foi assim que apareceu o `ValueError: the current database router prevents this
relation` no `POST /avaliacoes/`: os testes dos dois repos passavam, porque cada
um roda com um alias de banco só.

## A regra de segurança

**Nunca rode isto sem confirmar as duas travas.** O `.env` de desenvolvimento
carrega credenciais de PRODUÇÃO:

| Trava | Como conferir | O que acontece se estiver aberta |
|---|---|---|
| WhatsApp | `settings.envio_whatsapp_bloqueado` é `True` | mensagem real pro número simulado, e alerta no celular real da Thainá |
| Hamilton | `HAMILTON_API_URL` aponta pra `localhost` | cadastro cria paciente real na base real |

O `scripts/conversar.py` **aborta sozinho** se a primeira estiver aberta, e
imprime um aviso vermelho se a segunda apontar pro `onrender.com`. Não contorne
nenhum dos dois.

Antes de subir o Hamilton, confirme o banco pelo `timeline_id` — não pelo nome
do arquivo de env:

    d816d0c21c1ca258fce8a80e07b14626  -> sofia-teste (pode escrever)
    fdb211ba56128bff6c4bf23d8c88481e  -> PRODUÇÃO (aborte)

## Roteiro

**1. Confira o banco do Hamilton.**

```bash
cd ../hamilton-api
venv/Scripts/python.exe -c "
import sys; sys.path.insert(0,'.')
import os, django; os.environ.setdefault('DJANGO_SETTINGS_MODULE','app.settings'); django.setup()
from django.db import connections
with connections['default'].cursor() as c:
    c.execute('SHOW neon.timeline_id'); tl=c.fetchone()[0]
assert tl=='d816d0c21c1ca258fce8a80e07b14626', f'BANCO ERRADO: {tl}'
print('sofia-teste OK')"
```

**2. Suba o Hamilton local.** `SOFIA_PESQUISAS_ATIVAS` precisa estar ligada, ou
`GET /avaliacoes/pendentes/` devolve lista vazia de propósito e parece quebrado.

```bash
SOFIA_PESQUISAS_ATIVAS=true SOFIA_PESQUISAS_LIMITE=5 \
  venv/Scripts/python.exe manage.py runserver 8001 --noreload
```

**3. Suba a Sofia local**, apontada pro Hamilton local. `DEBOUNCE_SEGUNDOS=1`
encurta a espera entre turnos (em produção é 6).

```bash
cd ../sofia_whatsapp
python -m alembic upgrade head
HAMILTON_API_URL=http://localhost:8001 DEBOUNCE_SEGUNDOS=1 SIMULAR_DIGITACAO=false \
  python -m uvicorn app.main:app --port 8000
```

**4. Converse.**

```bash
python scripts/conversar.py --numero 5531900000099
```

Use um número que ainda não existe no banco — cada número é uma conversa, e
reaproveitar um já cadastrado faz a Sofia entrar no fluxo de reencontro.

Comandos dentro da conversa:

| | |
|---|---|
| `/estado` | estado, dados coletados e **o que a tool gravou** |
| `/pesquisas` | roda o cron de pesquisas agora |
| `/avaliacao` | a `Avaliacao` no Hamilton, com as respostas |
| `/adiantar N` | recua `cadastrado_em` N horas (destrava a janela de 3h) |
| `/sair` | |

## Testar a pesquisa de entrada

Ela só dispara entre 3h e 5 dias depois do cadastro, e a criação acontece num
tick do cron e o convite no **seguinte** — então são dois `/pesquisas`:

```
(faça o cadastro conversando normalmente)
/adiantar 4
/pesquisas      <- cria a Avaliacao de linha de base no Hamilton
/pesquisas      <- manda o convite
(responda as 5 perguntas)
/estado         <- confira o "tool gravou"
```

## O que olhar no fim

**No `/estado`, a linha `tool gravou`.** É a única evidência de que o modelo
chamou `registrar_resposta_pesquisa` em vez de só conversar. Um ORS completo tem
os quatro: `individual`, `interpessoal`, `social`, `geral`. Faltando um, o ORS
inteiro é descartado no relatório — então três de quatro é falha, não sucesso
parcial.

**No log do uvicorn:**

- `[DRY RUN] NÃO enviado` — confirma que nada saiu pra Meta;
- `extração ignorada em ... (a tool já gravou)` — a precedência da tool funcionando;
- `PATCH de avaliação descartou campo(s) fora da allowlist` — a Sofia mandando
  campo que o Hamilton não tem.

**No Hamilton**, confira a `Avaliacao`: `momento` correto, `fk_terapeuta` = 73
(o sentinela) na de linha de base, e `continuar_*` em `None` quando não foram
perguntados.

## Limpeza

A conversa fica no `sofia_dev.db` e o paciente fica na branch `sofia-teste` —
os dois são descartáveis, não precisa apagar. Se quiser recomeçar do zero com o
mesmo número, apague a conversa pelo painel ("Reiniciar conversa").

## Custo

Uma conversa completa (cadastro + pesquisa) são ~15 chamadas ao modelo. É o
preço de descobrir se o prompt funciona, e é mais barato que descobrir em
produção.
