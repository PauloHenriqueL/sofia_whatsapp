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
import re
import time

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Conversa
from app.services import config_negocio, hamilton_client
from app.utils import mascarar_telefone

logger = logging.getLogger(__name__)


def _formas_do_celular(texto: str) -> set[str]:
    """`55DD9XXXXXXXX` e `55DDXXXXXXXX`: o WhatsApp às vezes manda sem o nono dígito."""
    digitos = re.sub(r"\D", "", texto or "")
    if len(digitos) in (10, 11):
        digitos = "55" + digitos
    if len(digitos) not in (12, 13) or not digitos.startswith("55"):
        return set()
    if len(digitos) == 13 and digitos[4] == "9":
        return {digitos, digitos[:4] + digitos[5:]}
    if len(digitos) == 12:
        return {digitos, digitos[:4] + "9" + digitos[4:]}
    return {digitos}


def em_modo_teste(numero: str) -> bool:
    """O número está na lista de teste do painel (`numeros_modo_teste`)?

    Só esses terapeutas são atendidos como paciente. Antes era um interruptor
    para todos, e terapeuta de verdade que respondia a lembrete virava lead.
    """
    lista = str(config_negocio.valor("numeros_modo_teste") or "")
    if not lista.strip():
        return False
    de_teste: set[str] = set()
    for item in re.split(r"[,;\n]+", lista):
        de_teste |= _formas_do_celular(item)
    return bool(_formas_do_celular(numero) & de_teste)

HAMILTON = "hamilton"
MANUAL = "manual"
# Nome do terapeuta, como está no Hamilton. Vive no JSON `dados_coletados` para
# não exigir coluna nova; `nome_completo` continua sendo o do paciente.
CHAVE_NOME = "nome_terapeuta"
# "já perguntei o nome ao Hamilton". Sem isto, quem ele não conhece (marcado à
# mão) ou quem está sem nome no cadastro seria reconsultado a cada rodada do
# cron, para sempre.
CHAVE_NOME_CONFERIDO = "nome_terapeuta_conferido"

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


async def definir(db: AsyncSession, conversa: Conversa, eh_terapeuta: bool, por: str,
                  nome: str | None = None) -> None:
    """Grava quem é o contato e quem decidiu. Virando terapeuta, a Sofia solta a conversa.

    `nome` é o do cadastro no Hamilton. Guardamos porque a lista do painel só
    tinha o telefone do terapeuta, e a Thainá não reconhece ninguém por número.
    """
    # A marca vem ANTES de encerrar a pesquisa: `pesquisa.finalizar` emenda a
    # cobrança, e é a marca que faz `cobranca._elegivel` recusar.
    #
    # UPDATE explícito, e não atribuição no objeto: `atualizada_em` tem
    # `onupdate`, e a conferência do cron passaria a carimbar "atividade agora"
    # em toda conversa antiga, embaralhando a lista (que ordena por ela).
    valores = {
        "eh_terapeuta": eh_terapeuta,
        "contato_conferido_por": por,
        "atualizada_em": Conversa.atualizada_em,
    }
    if eh_terapeuta:
        dados = {**(conversa.dados_coletados or {}), CHAVE_NOME_CONFERIDO: True}
        if nome:
            dados[CHAVE_NOME] = nome
        valores["dados_coletados"] = dados
    await db.execute(update(Conversa).where(Conversa.id == conversa.id).values(**valores))
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


def numero_whatsapp(telefone: str | None) -> str:
    """O número como o webhook o recebe: só dígitos (o Hamilton manda "+55...")."""
    return re.sub(r"\D", "", telefone or "")


async def conversa_de_terapeuta(db: AsyncSession, telefone: str) -> Conversa:
    """A conversa do número de um terapeuta, criada e marcada se ainda não existir.

    Usada por quem fala com o terapeuta por iniciativa da Sofia (lembretes,
    aviso de encaminhamento): é o que faz essas mensagens aparecerem na aba
    Terapeutas. Conversa que já existe não tem a marca mexida aqui — a escolha
    à mão vence, e a do Hamilton é feita por `ao_reconhecer`/`conferir`.
    """
    numero = numero_whatsapp(telefone)
    conversa = (
        await db.execute(select(Conversa).where(Conversa.numero_whatsapp == numero))
    ).scalar_one_or_none()
    if conversa is None:
        conversa = Conversa(numero_whatsapp=numero, eh_terapeuta=True, contato_conferido_por=HAMILTON)
        db.add(conversa)
        await db.flush()
        logger.info("Conversa %s criada para o terapeuta %s", conversa.id, mascarar_telefone(numero))
    return conversa


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
        nome = (achado or {}).get("nome") if eh_terapeuta else None
        await definir(db, conversa, eh_terapeuta, HAMILTON, nome=nome)
        resumo["conferidas"] += 1
        resumo["terapeutas"] += int(eh_terapeuta)
    resumo["nomes"] = await _preencher_nomes(db, cliente, prazo)
    return resumo


async def _preencher_nomes(db: AsyncSession, cliente, prazo: float) -> int:
    """Nome de quem já estava marcado como terapeuta antes de guardarmos nomes.

    Sem isto a lista de Terapeutas seguiria só com telefone para as conversas
    antigas — e o nome só chegaria quando a pessoa escrevesse de novo.
    """
    pendentes = (
        (
            await db.execute(
                select(Conversa)
                .where(Conversa.eh_terapeuta.is_(True))
                .order_by(Conversa.atualizada_em.desc())
            )
        )
        .scalars()
        .all()
    )
    preenchidos = 0
    for conversa in pendentes:
        dados = conversa.dados_coletados or {}
        if dados.get(CHAVE_NOME) or dados.get(CHAVE_NOME_CONFERIDO):
            continue  # já tem nome, ou já perguntamos e o Hamilton não sabe
        if time.monotonic() > prazo:
            break
        try:
            achado = await cliente.terapeuta_por_telefone(conversa.numero_whatsapp)
        except hamilton_client.HamiltonError as exc:
            logger.warning("Nomes de terapeutas: rodada interrompida: %s", exc)
            break
        nome = (achado or {}).get("nome") if isinstance(achado, dict) else None
        novos = {**dados, CHAVE_NOME_CONFERIDO: True}
        if nome:
            novos[CHAVE_NOME] = nome
        await db.execute(
            update(Conversa)
            .where(Conversa.id == conversa.id)
            .values(dados_coletados=novos, atualizada_em=Conversa.atualizada_em)
        )
        await db.commit()
        preenchidos += int(bool(nome))
    return preenchidos


async def ids_de_terapeutas(db: AsyncSession) -> set[int]:
    """Conversas marcadas, para quem monta fila ou número em cima de várias consultas."""
    q = select(Conversa.id).where(Conversa.eh_terapeuta.is_(True))
    return set((await db.execute(q)).scalars().all())
