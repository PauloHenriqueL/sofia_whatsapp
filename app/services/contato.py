"""Paciente ou terapeuta: quem está do outro lado de cada conversa.

O número da Sofia é um só. Terapeuta que o Hamilton reconhece pelo telefone é
barrado no webhook e nem vira conversa (ver `lembretes_terapeutas`). Mas a
lista do painel tinha conversas de terapeuta mesmo assim, iguais às de um
paciente, por três caminhos: a conversa nasceu antes dessa regra, o telefone do
WhatsApp é diferente do que está no cadastro, ou a pessoa não é mais terapeuta
ativo. Nos dois últimos a Sofia ainda a tratava como lead: triagem, mensagem de
"você ainda tem interesse?", número de conversão.

A marca vive em `conversa.eh_terapeuta`. Quem a põe:

- **o Hamilton**, quando reconhece o número: na hora em que o terapeuta escreve
  (`ao_reconhecer`) e numa conferência aos poucos das conversas que ninguém
  olhou ainda (`conferir`, pendurada no cron dos lembretes);
- **uma pessoa**, pelo botão na tela da conversa (`definir` com `MANUAL`). É o
  único jeito de marcar quem o Hamilton não reconhece, e a escolha à mão não é
  desfeita pela conferência — nem a de "é paciente".

Marcar **não apaga nada**: a conversa continua na lista, com etiqueta. O que
muda é que a Sofia para de conduzi-la (o portão fica em `webhook`) e ela sai das
varreduras automáticas, dos números e da fila Hoje — cada uma filtra por
`eh_terapeuta`.
"""

import logging
import time

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Conversa
from app.services import hamilton_client
from app.utils import mascarar_telefone

logger = logging.getLogger(__name__)

HAMILTON = "hamilton"
MANUAL = "manual"

# Cada conferência é uma chamada ao Hamilton, uma depois da outra, dentro do
# request do cron — e o cron-job.org desiste em 30s. Quinze cabem com folga; a
# cada 30 min, são umas 700 conversas antigas conferidas por dia.
CONFERIDAS_POR_RODADA = 15
# E, com o Hamilton lento (início a frio), a rodada para sozinha antes disso.
SEGUNDOS_POR_RODADA = 8


async def _interromper_automaticos(db: AsyncSession, conversa: Conversa) -> None:
    """Encerra pesquisa e cobrança em curso (o que já foi respondido fica gravado).

    Mesmo cuidado de `painel.assumir`: os dois modos furam os portões do webhook,
    então só marcar a conversa não calaria a Sofia.
    """
    from app.services import cobranca, pesquisa

    if cobranca.em_cobranca(conversa):
        await cobranca.finalizar(db, conversa, "assumida")
    if pesquisa.em_pesquisa(conversa):
        try:
            avaliacao = await pesquisa.buscar_avaliacao(conversa)
            await pesquisa.finalizar(db, conversa, avaliacao or {}, recusou=True)
        except Exception as exc:  # noqa: BLE001 — marcar não pode falhar por isso
            logger.error("Não consegui encerrar a pesquisa da conversa %s: %s", conversa.id, exc)
            await pesquisa.limpar(db, conversa)


async def definir(db: AsyncSession, conversa: Conversa, eh_terapeuta: bool, por: str) -> None:
    """Grava quem é o contato e quem decidiu. Virando terapeuta, a Sofia solta a conversa."""
    # A marca vem ANTES de encerrar a pesquisa: `pesquisa.finalizar` emenda a
    # cobrança, e é a marca que faz `cobranca._elegivel` recusar.
    #
    # UPDATE explícito, e não atribuição no objeto: `atualizada_em` tem
    # `onupdate`, e a conferência do cron passaria a carimbar "atividade agora"
    # em toda conversa antiga, embaralhando a lista (que ordena por ela).
    await db.execute(
        update(Conversa)
        .where(Conversa.id == conversa.id)
        .values(
            eh_terapeuta=eh_terapeuta,
            contato_conferido_por=por,
            atualizada_em=Conversa.atualizada_em,
        )
    )
    await db.refresh(conversa)
    if eh_terapeuta:
        await _interromper_automaticos(db, conversa)
    await db.commit()


async def ao_reconhecer(db: AsyncSession, numero: str) -> bool:
    """O Hamilton disse que o número é de terapeuta. Devolve se vale tratá-lo assim.

    Aproveita para marcar a conversa que já existia com esse número. A única
    resposta `False` é a de quem foi marcado **à mão** como paciente: aí a
    escolha da pessoa vence o Hamilton e a mensagem segue o fluxo de paciente.
    """
    conversa = (
        await db.execute(select(Conversa).where(Conversa.numero_whatsapp == numero))
    ).scalar_one_or_none()
    if conversa is None:
        return True
    if conversa.contato_conferido_por == MANUAL:
        return conversa.eh_terapeuta
    if not conversa.eh_terapeuta:
        await definir(db, conversa, True, HAMILTON)
        logger.info(
            "Conversa %s (%s) marcada como de terapeuta pelo Hamilton",
            conversa.id,
            mascarar_telefone(numero),
        )
    return True


async def conferir(db: AsyncSession, limite: int = CONFERIDAS_POR_RODADA) -> dict:
    """Pergunta ao Hamilton pelas conversas que ninguém conferiu ainda.

    Cada conversa é conferida uma vez. Quem virar terapeuta depois disso é
    marcado quando escrever (`ao_reconhecer`). Hamilton fora interrompe a rodada
    sem marcar nada: "não sei" não pode virar "é paciente".
    """
    resumo = {"conferidas": 0, "terapeutas": 0}
    pendentes = (
        (
            await db.execute(
                select(Conversa)
                .where(Conversa.contato_conferido_por.is_(None))
                .order_by(Conversa.atualizada_em.desc())
                .limit(limite)
            )
        )
        .scalars()
        .all()
    )
    cliente = hamilton_client.get_hamilton_client()
    prazo = time.monotonic() + SEGUNDOS_POR_RODADA
    for conversa in pendentes:
        if time.monotonic() > prazo:
            break  # o resto fica para a próxima rodada
        try:
            achado = await cliente.terapeuta_por_telefone(conversa.numero_whatsapp)
        except hamilton_client.HamiltonError as exc:
            logger.warning("Conferência de terapeutas interrompida: %s", exc)
            break
        eh_terapeuta = isinstance(achado, dict) and achado.get("terapeuta") is True
        await definir(db, conversa, eh_terapeuta, HAMILTON)
        resumo["conferidas"] += 1
        resumo["terapeutas"] += int(eh_terapeuta)
    return resumo


async def ids_de_terapeutas(db: AsyncSession) -> set[int]:
    """Conversas marcadas, para quem monta fila ou número em cima de várias consultas."""
    q = select(Conversa.id).where(Conversa.eh_terapeuta.is_(True))
    return set((await db.execute(q)).scalars().all())
