"""Lembretes de WhatsApp para os TERAPEUTAS (nunca para paciente).

Duas mensagens, ambas por template aprovado na Meta (saem fora da janela de
24h, então não há texto livre):

- `agenda_do_dia_terapeuta_v2`: todo dia, no horário que o terapeuta escolheu
  no Hamilton (padrão 9h), com os horários do dia e os botões "Agenda de hoje"
  e "Agenda da semana";
- `lembrete_semana_terapeuta`: segunda às 9h, com as contagens da semana e o
  botão "Saiba mais".

**Quem decide é o Hamilton.** Esta rodada só pergunta "quem recebe agora?",
envia e confirma. Horário de cada um, dia vazio, trava geral
(`LEMBRETES_TERAPEUTAS_ATIVOS`) e "já mandei hoje" moram lá — assim um cron
duplicado ou um bug daqui não vira disparo em massa. As variáveis do template
vêm prontas, só com números e horários: nome de paciente não vai no template,
que aparece na tela bloqueada do celular. Os códigos dos botões também vêm do
Hamilton (ex.: `agenda_hoje:2026-11-03`) e vão no envio.

O outro lado: o número da Sofia é o mesmo que atende pacientes. Quando um
terapeuta escreve, `eh_terapeuta` o reconhece antes do LLM — sem triagem, sem
cadastro como lead, sem aparecer na fila da Thainá. Texto digitado recebe uma
resposta fixa. Toque em botão (`responder_botao`) recebe os detalhes em texto
livre (a janela de 24h foi aberta pelo toque): balões montados pelo Hamilton na
hora, que levam "João S." — o nome só aparece quando o terapeuta pede.
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

ERRO_DETALHE = "Não consegui buscar suas informações agora. Tente de novo em alguns minutos."
# Toque repetido: a Meta reentrega o mesmo wamid, e o dedo nervoso toca duas
# vezes no mesmo botão. Nenhum dos dois deve virar uma segunda leva de balões.
_TOQUE_REPETIDO_SEGUNDOS = 30
_WAMID_GUARDADO_SEGUNDOS = 3600
_toques: dict[tuple[str, str], float] = {}
_wamids_vistos: dict[str, float] = {}


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


async def responder_botao(numero: str, wamid: str | None, payload: str) -> None:
    """Toque num botão do lembrete: os balões que o Hamilton montar, na ordem.

    Sem o limite de uma resposta por hora da resposta fixa: cada toque é um
    pedido. O horário vai dentro de cada balão porque a ordem de entrega não é
    garantida pela Meta.
    """
    if wamid:
        await whatsapp_client.marcar_como_lida(wamid)
    agora = time.monotonic()
    for chave, quando in list(_wamids_vistos.items()):
        if agora - quando >= _WAMID_GUARDADO_SEGUNDOS:
            del _wamids_vistos[chave]
    if wamid and wamid in _wamids_vistos:
        return
    if agora - _toques.get((numero, payload), -_TOQUE_REPETIDO_SEGUNDOS) < _TOQUE_REPETIDO_SEGUNDOS:
        return
    if wamid:
        _wamids_vistos[wamid] = agora
    _toques[(numero, payload)] = agora

    try:
        status, corpo = await hamilton_client.get_hamilton_client().detalhe_lembrete(numero, payload)
    except hamilton_client.HamiltonError as exc:
        logger.error("Detalhe do lembrete para %s falhou: %s", mascarar_telefone(numero), exc)
        # Libera um novo toque logo: o erro não foi culpa de quem tocou.
        _toques.pop((numero, payload), None)
        await whatsapp_client.enviar_texto(numero, ERRO_DETALHE)
        return
    if status in (400, 404):
        # Botão que o Hamilton não conhece, ou número que deixou de ser de
        # terapeuta ativo: vale a resposta fixa de sempre.
        await responder_terapeuta(numero, None)
        return
    if status == 409:
        logger.warning("Número %s é de mais de um terapeuta: detalhe não enviado", mascarar_telefone(numero))
    for texto in corpo.get("mensagens") or []:
        await whatsapp_client.enviar_texto(numero, texto, sigiloso=True)


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
                    telefone, fila["template"], envio["variaveis"], botoes=envio.get("botoes")
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
