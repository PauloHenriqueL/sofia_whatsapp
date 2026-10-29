"""Lembretes de WhatsApp para os TERAPEUTAS (nunca para paciente).

Duas mensagens, ambas por template aprovado na Meta (saem fora da janela de
24h, então não há texto livre):

- `agenda_do_dia_terapeuta`: todo dia, no horário que o terapeuta escolheu no
  Hamilton (padrão 9h), com os horários das sessões do dia;
- `pendencias_semana_terapeuta`: segunda às 9h, com as contagens de pendências.

**Quem decide é o Hamilton.** Esta rodada só pergunta "quem recebe agora?",
envia e confirma. Horário de cada um, dia vazio, trava geral
(`LEMBRETES_TERAPEUTAS_ATIVOS`) e "já mandei hoje" moram lá — assim um cron
duplicado ou um bug daqui não vira disparo em massa. As variáveis vêm prontas,
só com números e horários: nome de paciente não passa pela Meta.

O outro lado: o número da Sofia é o mesmo que atende pacientes. Quando um
terapeuta responde ao lembrete, `eh_terapeuta` o reconhece antes do LLM e ele
recebe uma resposta fixa — sem triagem, sem cadastro como lead, sem aparecer na
fila da Thainá.
"""

import logging
import time

from app.config import settings
from app.services import hamilton_client, whatsapp_client
from app.utils import mascarar_telefone

logger = logging.getLogger(__name__)

RESPOSTA_TERAPEUTA = (
    "Oi! Este número só envia lembretes automáticos do Hamilton e não lê respostas. "
    "Para qualquer dúvida, fale com o Alan ou o Victor."
)

# Cache de "é terapeuta?" por número: cada mensagem recebida passaria pelo
# Hamilton, e a lista de terapeutas muda pouco. Negativos também ficam (é a
# maioria: pacientes).
_CACHE_SEGUNDOS = 6 * 3600
_cache: dict[str, tuple[float, bool]] = {}
# Uma resposta fixa por terapeuta a cada tanto: quem manda três mensagens
# seguidas não recebe três vezes o mesmo aviso.
_RESPONDER_A_CADA = 3600
_respondidos: dict[str, float] = {}


async def eh_terapeuta(numero: str) -> bool:
    agora = time.monotonic()
    guardado = _cache.get(numero)
    if guardado and agora - guardado[0] < _CACHE_SEGUNDOS:
        return guardado[1]
    try:
        achado = await hamilton_client.get_hamilton_client().terapeuta_por_telefone(numero)
    except hamilton_client.HamiltonError as exc:
        # Hamilton fora: segue o fluxo normal (sem cache, para tentar de novo).
        logger.warning("Não deu para checar se %s é terapeuta: %s", mascarar_telefone(numero), exc)
        return False
    resultado = isinstance(achado, dict) and achado.get("terapeuta") is True
    _cache[numero] = (agora, resultado)
    return resultado


async def responder_terapeuta(numero: str, wamid: str | None) -> None:
    """Resposta fixa (dentro da janela de 24h aberta por ele: texto livre, grátis)."""
    if wamid:
        await whatsapp_client.marcar_como_lida(wamid)
    agora = time.monotonic()
    if agora - _respondidos.get(numero, -_RESPONDER_A_CADA) < _RESPONDER_A_CADA:
        return
    _respondidos[numero] = agora
    await whatsapp_client.enviar_texto(numero, RESPOSTA_TERAPEUTA)
    logger.info("Terapeuta %s escreveu para a Sofia; resposta fixa enviada", mascarar_telefone(numero))


async def rodar() -> dict:
    """Uma rodada do cron: agenda do dia e resumo semanal de quem está na vez."""
    cliente = hamilton_client.get_hamilton_client()
    resultado = {}
    for tipo in ("agenda", "semanal"):
        enviados = falhas = 0
        try:
            fila = await cliente.fila_lembretes(tipo)
        except hamilton_client.HamiltonError as exc:
            # Nada se perde: o Hamilton só marca o que foi confirmado.
            logger.error("Lembretes (%s): Hamilton fora, tenta na próxima rodada: %s", tipo, exc)
            resultado[tipo] = {"erro": "hamilton_indisponivel"}
            continue
        for envio in fila.get("envios", []):
            telefone = envio["telefone"]
            try:
                await whatsapp_client.enviar_template(
                    telefone, fila["template"], envio["variaveis"]
                )
            except whatsapp_client.WhatsAppError as exc:
                falhas += 1
                logger.error("Lembrete (%s) para %s não saiu: %s", tipo, mascarar_telefone(telefone), exc)
                continue
            enviados += 1
            # Em dry run nada saiu de verdade: confirmar faria o Hamilton achar
            # que o terapeuta já recebeu e engolir o lembrete real do dia.
            if settings.envio_whatsapp_bloqueado:
                continue
            try:
                await cliente.confirmar_lembrete(tipo, envio["terapeuta_id"], fila["data"])
            except hamilton_client.HamiltonError as exc:
                # Pior caso: o mesmo lembrete sai de novo na próxima rodada.
                logger.error("Lembrete (%s) enviado mas não confirmado: %s", tipo, exc)
        resultado[tipo] = {"enviados": enviados, "falhas": falhas}
    return resultado
