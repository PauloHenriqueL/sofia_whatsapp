"""A equipe inicia a conversa pelo painel (Demanda G — docs/demandas/08).

Até aqui só dava para falar com quem tinha escrito nas últimas 24h: fora dessa
janela a Meta só aceita template aprovado, e o painel não tinha nenhum. Agora:

- **Na conversa**: o painel mostra até quando a janela vai. Fechada, o compositor
  dá lugar ao botão "Iniciar contato", que manda o template
  (`template_inicio_contato` no /painel/config) com o nome da pessoa.
- **Número novo**: nome + celular brasileiro + a confirmação de que a pessoa
  passou o número para a Allos. Vira uma conversa sem mensagens e segue o mesmo
  caminho. Número que já existe não duplica: abre a conversa dele.

**Quem atende a resposta é a equipe.** Iniciar é assumir (`painel.assumir`:
modo humano, interrompe pesquisa e cobrança). A 1ª resposta abre a escalada
`respondeu_contato` e dispara o `alerta_thaina` (`registrar_resposta`); a Sofia
não faz triagem nem manda o aviso de "a equipe vai retornar" — foi a equipe que
chamou.

**Nono dígito.** O identificador do WhatsApp de um celular brasileiro às vezes
vem sem o 9. Se a equipe grava `5531988887777` e a resposta chega de
`553188887777`, nasceria uma segunda conversa, em modo bot, e a Sofia faria
triagem com quem a equipe chamou. Duas defesas: o `wa_id` que a Meta devolve no
envio do template substitui o número digitado, e o webhook casa as duas formas
antes de criar conversa nova (`reconciliar_numero`).
"""

import logging
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Conversa, Mensagem
from app.services import config_negocio, contato, conversation, escalation, whatsapp_client
from app.utils import mascarar_telefone

logger = logging.getLogger(__name__)

JANELA = timedelta(hours=24)


class NumeroInvalido(ValueError):
    """O que foi digitado não é um celular brasileiro."""


class SemTemplate(RuntimeError):
    """O nome do template está em branco no /painel/config."""


# --- Número -----------------------------------------------------------------


def normalizar_numero_br(texto: str | None) -> str:
    """DDD + número (com ou sem 55, máscara, espaço) → `55DDXXXXXXXXX`.

    Só celular brasileiro: 11 dígitos começando em 9 depois do DDD, ou 10
    (o formato antigo, sem o nono dígito, que o WhatsApp ainda usa às vezes).
    """
    digitos = re.sub(r"\D", "", texto or "")
    if len(digitos) in (12, 13) and digitos.startswith("55"):
        digitos = digitos[2:]
    if len(digitos) not in (10, 11):
        raise NumeroInvalido("Digite o DDD e o número do celular, ex.: (31) 98888-7777.")
    ddd, resto = digitos[:2], digitos[2:]
    if ddd[0] == "0" or ddd[1] == "0":
        raise NumeroInvalido("O DDD não parece válido.")
    if len(resto) == 9 and resto[0] != "9":
        raise NumeroInvalido("Celular com 9 dígitos começa com 9.")
    if len(resto) == 8 and resto[0] not in "6789":
        raise NumeroInvalido("Esse número parece de telefone fixo; o WhatsApp precisa de celular.")
    return "55" + digitos


def variantes(numero: str) -> list[str]:
    """O mesmo celular com e sem o nono dígito (o próprio número primeiro)."""
    formas = [numero]
    if len(numero) == 13 and numero.startswith("55") and numero[4] == "9":
        formas.append(numero[:4] + numero[5:])
    elif len(numero) == 12 and numero.startswith("55"):
        formas.append(numero[:4] + "9" + numero[4:])
    return formas


async def achar_conversa(db: AsyncSession, numero: str) -> Conversa | None:
    """A conversa do número, aceitando a outra forma do nono dígito."""
    for forma in variantes(numero):
        conversa = (
            await db.execute(select(Conversa).where(Conversa.numero_whatsapp == forma))
        ).scalar_one_or_none()
        if conversa is not None:
            return conversa
    return None


async def reconciliar_numero(db: AsyncSession, numero: str) -> None:
    """Chamado pelo webhook antes de criar conversa: se não há conversa com este
    número exato, mas há com a outra forma do nono dígito, ela passa a usar o
    número que a Meta mandou (é o identificador de verdade da pessoa)."""
    formas = variantes(numero)
    if len(formas) < 2:
        return
    exata = (
        await db.execute(select(Conversa.id).where(Conversa.numero_whatsapp == numero))
    ).scalar_one_or_none()
    if exata is not None:
        return
    outra = (
        await db.execute(select(Conversa).where(Conversa.numero_whatsapp == formas[1]))
    ).scalar_one_or_none()
    if outra is None:
        return
    logger.info(
        "Conversa %s: número trocado de %s para %s (nono dígito)",
        outra.id,
        mascarar_telefone(outra.numero_whatsapp),
        mascarar_telefone(numero),
    )
    outra.numero_whatsapp = numero
    await db.flush()


# --- Janela de 24h ----------------------------------------------------------


async def janela_ate(db: AsyncSession, conversa: Conversa) -> datetime | None:
    """Até quando dá para mandar texto livre. `None` = a pessoa nunca escreveu.

    A janela conta da última mensagem RECEBIDA (o que a equipe manda não reabre).
    """
    ultima = (
        await db.execute(
            select(func.max(Mensagem.criada_em)).where(
                Mensagem.conversa_id == conversa.id, Mensagem.direcao == "recebida"
            )
        )
    ).scalar_one_or_none()
    if ultima is None:
        return None
    if ultima.tzinfo is None:  # SQLite devolve sem fuso
        ultima = ultima.replace(tzinfo=timezone.utc)
    return ultima + JANELA


def janela_aberta(ate: datetime | None, agora: datetime | None = None) -> bool:
    return ate is not None and (agora or datetime.now(timezone.utc)) < ate


def template_configurado() -> str:
    return str(config_negocio.valor("template_inicio_contato") or "").strip()


# --- Iniciar ----------------------------------------------------------------


def _primeiro_nome(conversa: Conversa) -> str:
    nome = ((conversa.dados_coletados or {}).get("nome_completo") or "").strip()
    return nome.split()[0] if nome else "tudo bem"


def texto_do_convite(nome: str) -> str:
    """Como o convite aparece no painel (o corpo do template mora na Meta)."""
    return f"[Início de contato pela equipe] Olá, {nome}. Aqui é a equipe da Allos."


async def iniciar(db: AsyncSession, conversa: Conversa, usuario: str) -> None:
    """Manda o template de início, grava e assume a conversa.

    Falha no envio propaga (`WhatsAppError`) sem gravar nada: o painel não pode
    mostrar como enviado o que não saiu.
    """
    from app.services import painel  # painel importa este módulo indiretamente

    template = template_configurado()
    if not template:
        raise SemTemplate("Configure o nome do template em Configurações.")
    nome = _primeiro_nome(conversa)
    resposta = await whatsapp_client.enviar_template(conversa.numero_whatsapp, template, [nome])

    wa_id = _wa_id(resposta)
    if wa_id and wa_id != conversa.numero_whatsapp:
        await _trocar_numero(db, conversa, wa_id)

    await conversation.registrar_mensagem_enviada(
        db,
        conversa,
        texto=texto_do_convite(nome),
        origem="thaina",
        tipo="template",
        whatsapp_message_id=whatsapp_client.id_da_resposta(resposta),
    )
    agora = datetime.now(timezone.utc)
    conversa.contato_iniciado_em = agora
    conversa.contato_iniciado_por = (usuario or "")[:50] or None
    conversa.contato_respondido_em = None
    # Conta como "já avisado": a resposta a um contato da equipe não recebe o
    # "a equipe já vai te responder" da escalada (foi a equipe que chamou).
    conversa.aviso_escalada_em = agora
    if conversa.arquivada_em is not None:
        conversa.arquivada_em = None
    await painel.assumir(db, conversa)  # modo humano + interrompe automáticos; commita
    logger.info(
        "Contato iniciado pela equipe com %s (conversa %s)",
        mascarar_telefone(conversa.numero_whatsapp),
        conversa.id,
    )


def _wa_id(resposta: object) -> str | None:
    """O identificador que a Meta devolve no envio (`contacts[0].wa_id`)."""
    if not isinstance(resposta, dict):
        return None
    contatos = resposta.get("contacts")
    if isinstance(contatos, list) and contatos and isinstance(contatos[0], dict):
        wa_id = contatos[0].get("wa_id")
        if isinstance(wa_id, str) and wa_id.isdigit():
            return wa_id
    return None


async def _trocar_numero(db: AsyncSession, conversa: Conversa, wa_id: str) -> None:
    ocupado = (
        await db.execute(select(Conversa.id).where(Conversa.numero_whatsapp == wa_id))
    ).scalar_one_or_none()
    if ocupado is not None:
        # Já existe conversa com o número real: não dá para fundir as duas aqui.
        logger.warning("wa_id de %s já tem conversa (%s); número mantido", conversa.id, ocupado)
        return
    conversa.numero_whatsapp = wa_id


# --- Número novo ------------------------------------------------------------


async def criar_contato(
    db: AsyncSession,
    nome: str,
    numero_digitado: str,
    eh_terapeuta: bool,
    nota: str,
    usuario: str,
) -> tuple[Conversa, bool]:
    """Cria a conversa de um número novo. Devolve `(conversa, criada)`.

    Número que já existe (em qualquer forma do nono dígito) não cria nada:
    devolve a conversa que já estava lá, com `criada=False`.
    """
    numero = normalizar_numero_br(numero_digitado)
    nome = " ".join((nome or "").split())
    if not nome:
        raise ValueError("Informe o nome.")
    existente = await achar_conversa(db, numero)
    if existente is not None:
        return existente, False
    conversa = Conversa(
        numero_whatsapp=numero,
        dados_coletados={"nome_completo": nome[:120]},
        eh_terapeuta=eh_terapeuta,
        contato_conferido_por=contato.MANUAL,
        contato_consentimento_por=(usuario or "")[:50] or None,
        contato_nota=(nota or "").strip()[:500] or None,
    )
    db.add(conversa)
    await db.commit()
    logger.info("Contato novo adicionado pelo painel: %s", mascarar_telefone(numero))
    return conversa, True


# --- Resposta ---------------------------------------------------------------


def aguardando_resposta(conversa: Conversa) -> bool:
    return conversa.contato_iniciado_em is not None and conversa.contato_respondido_em is None


async def registrar_resposta(db: AsyncSession, conversa: Conversa) -> bool:
    """1ª mensagem depois do contato iniciado pela equipe: escalada + alerta.

    Devolve se tratou. Só a 1ª — as seguintes aparecem na conversa como sempre.
    Vale também para terapeuta (a fila Hoje abre exceção para este motivo).
    """
    if not aguardando_resposta(conversa):
        return False
    conversa.contato_respondido_em = datetime.now(timezone.utc)
    partes = []
    if conversa.contato_iniciado_por:
        partes.append(f"Contato iniciado por {conversa.contato_iniciado_por}")
    if conversa.contato_nota:
        partes.append(f"Nota: {conversa.contato_nota}")
    await escalation.registrar_escalada(
        db, conversa, "respondeu_contato", contexto=". ".join(partes) or None
    )
    await db.commit()
    await escalation.alertar_thaina(db, conversa, "respondeu_contato")
    return True
