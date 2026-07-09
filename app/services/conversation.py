"""Orquestração da conversa e persistência.

Passo 3: cria/busca conversa por número, persiste mensagens recebidas
(com idempotência por whatsapp_message_id) e enviadas.
"""

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Conversa, Mensagem
from app.utils import mascarar_telefone

logger = logging.getLogger(__name__)


AVISO_RETOMADA = (
    "[Aviso do sistema: a Thainá assumiu a conversa e falou com a pessoa. "
    "Agora você, Sofia, voltou a atender. A pessoa não sabe disso. "
    "Comece a sua próxima mensagem dizendo, em uma frase curta, que é a Sofia de novo "
    "(ex.: 'Oi, é a Sofia aqui de novo.'), e siga a conversa a partir do que a Thainá disse. "
    "Faça isso uma vez só.]"
)


async def carregar_historico(
    session: AsyncSession, conversa: Conversa, limite: int = 20
) -> list[dict[str, str]]:
    """Carrega as últimas mensagens da conversa no formato esperado pelo LLM.

    Retorna em ordem cronológica (mais antiga primeiro), mapeando a origem:
    'paciente' -> 'user'; 'bot' e 'thaina' -> 'assistant'. Mensagens sem texto
    (áudio, imagem) são ignoradas porque não há conteúdo textual a enviar.

    A fala da Thainá é prefixada, porque no papel `assistant` o modelo não teria
    como distingui-la da sua própria. E quando a última fala da Allos foi dela,
    injetamos um aviso pra Sofia se reapresentar ao retomar (senão a pessoa não
    percebe que trocou de interlocutor de volta).
    """
    result = await session.execute(
        select(Mensagem)
        .where(Mensagem.conversa_id == conversa.id)
        .order_by(Mensagem.criada_em.desc(), Mensagem.id.desc())
        .limit(limite)
    )
    mensagens = list(result.scalars().all())
    mensagens.reverse()  # do mais antigo para o mais novo

    historico: list[dict[str, str]] = []
    ultima_da_allos: str | None = None
    for m in mensagens:
        if not m.texto:
            continue
        if m.origem == "paciente":
            historico.append({"role": "user", "content": m.texto})
            continue
        ultima_da_allos = m.origem
        conteudo = f"[Thainá, coordenadora clínica]: {m.texto}" if m.origem == "thaina" else m.texto
        historico.append({"role": "assistant", "content": conteudo})

    if ultima_da_allos == "thaina":
        historico.append({"role": "system", "content": AVISO_RETOMADA})
    return historico


async def obter_ou_criar_conversa(session: AsyncSession, numero: str) -> Conversa:
    """Retorna a conversa do número, criando-a se ainda não existir."""
    result = await session.execute(select(Conversa).where(Conversa.numero_whatsapp == numero))
    conversa = result.scalar_one_or_none()
    if conversa is None:
        conversa = Conversa(numero_whatsapp=numero)
        session.add(conversa)
        await session.flush()  # garante conversa.id disponível
        logger.info(f"Nova conversa criada para {mascarar_telefone(numero)} (id={conversa.id})")
    return conversa


async def obter_conversa_por_numero(session: AsyncSession, numero: str) -> Conversa | None:
    """Retorna a conversa do número, ou None se ainda não existir (não cria)."""
    result = await session.execute(select(Conversa).where(Conversa.numero_whatsapp == numero))
    return result.scalar_one_or_none()


async def mensagem_ja_processada(session: AsyncSession, whatsapp_message_id: str | None) -> bool:
    """Idempotência: True se a mensagem da Meta já foi persistida antes."""
    if not whatsapp_message_id:
        return False
    result = await session.execute(
        select(Mensagem.id).where(Mensagem.whatsapp_message_id == whatsapp_message_id)
    )
    return result.scalar_one_or_none() is not None


async def registrar_mensagem_recebida(
    session: AsyncSession,
    conversa: Conversa,
    tipo: str,
    texto: str | None,
    whatsapp_message_id: str | None = None,
    extra: dict | None = None,
) -> Mensagem:
    """Persiste uma mensagem recebida do paciente."""
    mensagem = Mensagem(
        conversa_id=conversa.id,
        direcao="recebida",
        origem="paciente",
        tipo=tipo,
        texto=texto,
        whatsapp_message_id=whatsapp_message_id,
        extra=extra or {},
    )
    session.add(mensagem)
    await session.flush()
    return mensagem


async def registrar_mensagem_enviada(
    session: AsyncSession,
    conversa: Conversa,
    texto: str,
    origem: str = "bot",
    tipo: str = "texto",
    whatsapp_message_id: str | None = None,
) -> Mensagem:
    """Persiste uma mensagem enviada (bot ou Thainá)."""
    mensagem = Mensagem(
        conversa_id=conversa.id,
        direcao="enviada",
        origem=origem,
        tipo=tipo,
        texto=texto,
        whatsapp_message_id=whatsapp_message_id,
    )
    session.add(mensagem)
    await session.flush()
    return mensagem
