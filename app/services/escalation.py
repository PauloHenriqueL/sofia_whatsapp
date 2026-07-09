"""Lógica de escalada para a Thainá (Passo 5).

Escalar significa: marcar a conversa como `modo = humano` e `estado = escalado`,
registrar a escalada e alertar a Thainá pelo template aprovado na Meta.
"""

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Conversa, Escalada
from app.services import tools, whatsapp_client

logger = logging.getLogger(__name__)

# Texto de acolhimento usado quando o LLM escala mas não gera uma fala própria.
# Alinhado ao briefing (seção System prompt): sem travessões, acolhedor.
ESCALADA_FALLBACK = (
    "Vou chamar a Thainá pra continuar daqui contigo. Ela é a coordenadora "
    "da nossa clínica e vai te responder em pouco tempo."
)


async def registrar_escalada(
    session: AsyncSession,
    conversa: Conversa,
    motivo: str,
    contexto: str | None = None,
) -> Escalada:
    """Marca a conversa como humana/escalada e cria o registro de escalada."""
    conversa.modo = "humano"
    conversa.estado = "escalado"
    escalada = Escalada(conversa_id=conversa.id, motivo=motivo, contexto=contexto)
    session.add(escalada)
    await session.flush()
    logger.info(f"Conversa {conversa.id} escalada (motivo={motivo})")
    return escalada


async def _enviar_alerta(conversa: Conversa, rotulo: str) -> bool:
    """Manda o template `alerta_thaina` com (nome do paciente, o que aconteceu).

    Falha no envio é logada e **não** derruba a conversa: o evento já está
    registrado e aparece no painel mesmo sem o alerta chegar. O alerta é
    conveniência, não a fonte da verdade.
    """
    dados = conversa.dados_coletados or {}
    nome = dados.get("nome_completo") or conversa.numero_whatsapp
    try:
        await whatsapp_client.enviar_template(
            settings.thaina_whatsapp_number,
            settings.alert_template_name,
            parametros=[str(nome), rotulo],
        )
        return True
    except whatsapp_client.WhatsAppError:
        # Sem o nome do paciente no log (LGPD).
        logger.error("Não consegui alertar a Thainá pelo template (%s)", rotulo)
        return False


async def alertar_thaina(conversa: Conversa, motivo: str) -> bool:
    """Alerta de escalada: a conversa passou pra Thainá."""
    return await _enviar_alerta(conversa, tools.MOTIVO_LABELS.get(motivo, motivo))


# O que a Thainá lê no template quando um cadastro acontece. Reusa o
# `alerta_thaina` (o texto é genérico: "<nome> — <o que aconteceu>"), pra não
# depender de aprovar um template novo na Meta.
ROTULOS_CADASTRO = {
    "cadastrado": "paciente novo cadastrado no Hamilton (ficha {id})",
    "atualizado": "paciente já conhecido voltou; ficha {id} atualizada",
    "cadastro_pendente": "CADASTRO FALHOU — precisa cadastrar à mão no Hamilton",
}


async def alertar_cadastro(conversa: Conversa, resultado: dict) -> bool:
    """Avisa a Thainá do desfecho de um cadastro (novo, reencontro ou falha).

    `resultado` é o que `cadastro.cadastrar_paciente` devolve. Status desconhecido
    não manda nada (melhor silêncio que uma mensagem sem sentido).
    """
    modelo = ROTULOS_CADASTRO.get(resultado.get("status", ""))
    if not modelo:
        return False
    rotulo = modelo.format(id=resultado.get("paciente_id") or "?")
    return await _enviar_alerta(conversa, rotulo)
