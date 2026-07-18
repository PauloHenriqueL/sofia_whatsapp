"""Webhook do WhatsApp - Passo 5: LLM com tool calling (escalada / cadastro)"""

import asyncio
import hashlib
import hmac
import json
import logging
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.config import settings
from app.database import async_session
from app.services import (
    cadastro,
    config_negocio,
    conversation,
    escalation,
    llm_client,
    midia,
    saida,
    serializacao,
    tools,
    transcricao,
    whatsapp_client,
)
from app.utils import mascarar_telefone

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhook", tags=["webhook"])

# Resposta de degradação quando o LLM falha. Revisada manualmente (não passa
# pelo prompt da Sofia); segue o estilo dela: sem travessões, acolhedora.
FALLBACK_RESPOSTA = (
    "Oi, tive um probleminha técnico aqui pra te responder agora. "
    "Pode me mandar de novo daqui a pouco?"
)

# Áudio: a Sofia não transcreve; escala imediatamente pra Thainá.
AUDIO_RECEBIDO = "Recebi seu áudio. Vou chamar a Thainá pra te responder direito."

# Imagem/documento: a Sofia guarda o anexo e chama a Thainá, que abre no painel.
ANEXO_RECEBIDO = "Recebi seu arquivo. Vou chamar a Thainá pra dar uma olhada e te responder."

# Resposta fixa pros outros tipos sem texto (vídeo, sticker, localização...).
PEDIR_TEXTO = "Por enquanto consigo ler só mensagens de texto. Pode me escrever?"

# Sinais de crise que NÃO devem esperar a janela de agrupamento (Demanda 2 NFR):
# ao detectá-los, processa a mensagem na hora. Isto é só um gatilho de urgência;
# o acolhimento e a escalada de fato continuam sendo decididos pelo LLM (prompt).
SINAIS_CRISE = (
    "suicíd",
    "suicid",
    "me matar",
    "vou me matar",
    "quero morrer",
    "não quero mais viver",
    "nao quero mais viver",
    "tirar minha vida",
    "acabar com tudo",
    "me cortar",
    "me cortei",
    "automutil",
    "me machucar",
    "sumir do mundo",
)


def _contem_sinal_de_crise(texto: str | None) -> bool:
    t = (texto or "").lower()
    return any(sinal in t for sinal in SINAIS_CRISE)


class WebhookPayload(BaseModel):
    """Estrutura simplificada do payload do webhook Meta"""

    entry: list[dict[str, Any]] = Field(default_factory=list)


@router.get("/whatsapp")
async def verify_webhook(
    hub_mode: str = Query(..., alias="hub.mode"),
    hub_verify_token: str = Query(..., alias="hub.verify_token"),
    hub_challenge: str = Query(..., alias="hub.challenge"),
):
    """GET /webhook/whatsapp - Validação inicial do webhook (Meta)

    Meta envia um desafio na primeira configuração do webhook.
    Devemos responder com o challenge se o verify_token bater.

    Docs: https://developers.facebook.com/docs/whatsapp/cloud-api/webhooks
    """
    logger.info(f"Webhook verification: mode={hub_mode}")

    if hub_mode != "subscribe":
        logger.warning(f"Invalid hub.mode: {hub_mode}")
        return JSONResponse({"error": "Invalid mode"}, status_code=403)

    if hub_verify_token != settings.whatsapp_verify_token:
        logger.warning("Invalid verify token")
        return JSONResponse({"error": "Invalid token"}, status_code=403)

    logger.info("Webhook verified successfully")
    return Response(content=hub_challenge)


@router.post("/whatsapp")
async def receive_webhook(request: Request, background_tasks: BackgroundTasks):
    """POST /webhook/whatsapp - Recebe mensagens do WhatsApp

    Valida assinatura, responde 200 imediatamente e processa a mensagem em
    background (persistência + resposta via LLM).

    Validação de assinatura: X-Hub-Signature-256 header com HMAC SHA256
    """
    # Validar assinatura
    x_hub_signature = request.headers.get("X-Hub-Signature-256", "")
    body = await request.body()

    if not verify_signature(body, x_hub_signature):
        logger.warning("Invalid webhook signature")
        return JSONResponse({"error": "Invalid signature"}, status_code=403)

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        logger.error("Invalid JSON payload")
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    # LGPD: não logamos o conteúdo das mensagens (dado de saúde sensível),
    # apenas metadados (quantidade, tipos e ids).
    logger.info("Webhook recebido: %s", _resumo_payload(payload))

    # Responde 200 imediatamente (Meta exige <3s). Processa async.
    background_tasks.add_task(processar_payload, payload)
    return JSONResponse({"status": "received"})


def _resumo_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Resumo do payload sem conteúdo de mensagem (para logging seguro/LGPD)."""
    mensagens = extrair_mensagens(payload)
    return {
        "qtd_mensagens": len(mensagens),
        "tipos": [m.get("type") for m in mensagens],
        "ids": [m.get("id") for m in mensagens],
    }


def extrair_mensagens(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Extrai a lista de mensagens recebidas do payload do webhook.

    O webhook da Meta também envia eventos de status (entregue, lido) que
    não contêm `messages`; esses são ignorados.
    """
    mensagens: list[dict[str, Any]] = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            mensagens.extend(value.get("messages", []))
    return mensagens


async def processar_turno_bot(session, conversa) -> str | None:
    """Executa um turno do bot: chama o LLM (com tools), aplica as ações e
    devolve o texto a enviar ao paciente (ou None se não há nada a enviar).

    Falha do LLM cai em resposta de degradação, sem derrubar a conversa.
    """
    historico = await conversation.carregar_historico(session, conversa)
    try:
        resp = await llm_client.get_llm_client().gerar_resposta(historico, tools=tools.TOOLS)
    except llm_client.LLMError:
        logger.error(f"LLM falhou para conversa {conversa.id}; usando fallback")
        return FALLBACK_RESPOSTA

    # Conversa normal, sem ações: devolve o texto gerado.
    if not resp.tool_calls:
        return resp.texto

    # Executa cada ferramenta e coleta os resultados pro round-trip.
    resultados = []
    for tc in resp.tool_calls:
        resultado = await _executar_tool(session, conversa, tc)
        resultados.append((tc, resultado))

    # Segundo turno: devolve os resultados ao modelo pra ele gerar a fala final.
    texto_final = await _finalizar_apos_tools(historico, resp, resultados)
    if texto_final:
        return texto_final

    # Sem texto do modelo: usa um default seguro conforme a ação tomada.
    if any(tc.name == tools.ESCALAR_PARA_THAINA for tc in resp.tool_calls):
        return escalation.ESCALADA_FALLBACK
    return resp.texto


async def _executar_tool(session, conversa, tc: llm_client.ToolCall) -> dict:
    """Executa uma chamada de ferramenta e devolve o resultado estruturado."""
    if tc.name == tools.ESCALAR_PARA_THAINA:
        motivo = tc.arguments.get("motivo", "outro")
        if motivo not in tools.MOTIVOS_ESCALADA:
            motivo = "outro"
        contexto = tc.arguments.get("contexto")
        await escalation.registrar_escalada(session, conversa, motivo, contexto)
        alertada = await escalation.alertar_thaina(conversa, motivo)
        return {"status": "escalado", "thaina_alertada": alertada}

    if tc.name == tools.CADASTRAR_PACIENTE:
        # Mescla os dados coletados e delega pro serviço de cadastro (que garante
        # um telefone válido e faz busca-antes-de-criar no Hamilton).
        conversa.dados_coletados = {**(conversa.dados_coletados or {}), **tc.arguments}
        resultado = await cadastro.cadastrar_paciente(session, conversa)
        # Avisa a Thainá: sem isto ela só descobre o cadastro se abrir o painel.
        # (Cadastro feito pelo botão do painel não alerta: ela mesma clicou.)
        await escalation.alertar_cadastro(conversa, resultado)
        return resultado

    logger.warning(f"Tool desconhecida pedida pelo modelo: {tc.name}")
    return {"status": "desconhecida"}


async def _finalizar_apos_tools(historico, resp, resultados) -> str | None:
    """Round-trip: reenvia ao modelo os resultados das tools pra fala final."""
    assistant_msg = {
        "role": "assistant",
        "content": resp.texto or "",
        "tool_calls": [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.name,
                    "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                },
            }
            for tc, _ in resultados
        ],
    }
    tool_msgs = [
        {
            "role": "tool",
            "tool_call_id": tc.id,
            "content": json.dumps(res, ensure_ascii=False),
        }
        for tc, res in resultados
    ]
    mensagens = [*historico, assistant_msg, *tool_msgs]
    try:
        final = await llm_client.get_llm_client().gerar_resposta(mensagens)
        return final.texto
    except llm_client.LLMError:
        logger.error("LLM falhou no round-trip pós-tool")
        return None


async def processar_payload(payload: dict[str, Any]) -> None:
    """Ingere cada mensagem do payload do webhook (em background).

    Um erro inesperado em uma mensagem não derruba as demais (apenas é logado).
    A resposta em si pode sair aqui (áudio/crise/tipo sem texto) ou depois da
    janela de agrupamento (texto normal) — ver `ingerir_mensagem`.
    """
    for mensagem in extrair_mensagens(payload):
        try:
            await ingerir_mensagem(mensagem)
        except Exception:  # resiliência: nenhuma mensagem pode matar o worker
            logger.exception(
                f"Erro processando mensagem {mensagem.get('id')} "
                f"de {mascarar_telefone(mensagem.get('from'))}"
            )


async def ingerir_mensagem(mensagem: dict[str, Any]) -> None:
    """Persiste a mensagem e decide como responder, serializando por conversa.

    Sob o lock do número (Demanda 2): checa idempotência, cria/obtém a conversa
    (sem corrida) e persiste. Depois:
      - modo humano -> só persiste (a Thainá responde);
      - áudio -> escala na hora; tipo sem texto -> pede texto na hora;
      - texto de crise -> responde na hora (não espera a janela);
      - texto normal -> agenda o turno pra depois da janela de agrupamento,
        juntando a rajada numa resposta só.
    """
    numero = mensagem.get("from")
    tipo = mensagem.get("type")
    wamid = mensagem.get("id")
    if not numero:
        return

    async with serializacao.lock_da_conversa(numero):
        async with async_session() as session:
            if await conversation.mensagem_ja_processada(session, wamid):
                # Demanda 1: reentrega/duplicata é descartada e registrada.
                logger.info("Mensagem %s já processada, duplicata descartada", wamid)
                return

            conversa = await conversation.obter_ou_criar_conversa(session, numero)

            # Mensagem nova reativa a conversa no painel: arquivada é estado da
            # lista, não do atendimento. O commit da ingestão persiste.
            if conversa.arquivada_em is not None:
                conversa.arquivada_em = None
                logger.info("Conversa %s desarquivada por mensagem nova do paciente", conversa.id)

            # Áudio com transcrição ligada: baixa e transcreve; o áudio passa a
            # valer como uma mensagem de texto (a transcrição). Se falhar (ou
            # estiver desligada), mantém o comportamento de áudio (escala).
            texto_transcrito = None
            if tipo == "audio" and config_negocio.valor("transcrever_audio"):
                texto_transcrito = await _transcrever_audio_msg(mensagem)

            if texto_transcrito:
                await conversation.registrar_mensagem_recebida(
                    session,
                    conversa,
                    tipo="audio",
                    texto=texto_transcrito,
                    whatsapp_message_id=wamid,
                )
                texto, tipo_efetivo = texto_transcrito, "text"
            else:
                texto = await _persistir_recebida(session, conversa, tipo, wamid, mensagem)
                tipo_efetivo = tipo

            # Presença humana (UX): marca lida e, se o bot responde, "digitando…".
            if config_negocio.valor("simular_digitacao"):
                await whatsapp_client.marcar_como_lida(
                    wamid, com_digitacao=(conversa.modo != "humano")
                )
            await session.commit()

            # Modo humano: só persiste; a Thainá responde pelo painel.
            if conversa.modo == "humano":
                return

            if tipo_efetivo == "audio":
                # Áudio sem transcrição (desligada ou falhou): escala pra Thainá.
                await escalation.registrar_escalada(session, conversa, "audio_recebido")
                await escalation.alertar_thaina(conversa, "audio_recebido")
                await session.commit()
                await _enviar_em_bolhas(session, conversa, numero, AUDIO_RECEBIDO)
                return

            if tipo_efetivo in midia.TIPOS_SUPORTADOS:
                # A Sofia não lê o anexo: guarda (já feito) e chama a Thainá, que
                # abre no painel.
                await escalation.registrar_escalada(session, conversa, "anexo_recebido")
                await escalation.alertar_thaina(conversa, "anexo_recebido")
                await session.commit()
                await _enviar_em_bolhas(session, conversa, numero, ANEXO_RECEBIDO)
                return

            if tipo_efetivo != "text":
                await _enviar_em_bolhas(session, conversa, numero, PEDIR_TEXTO)
                return

            # Crise não espera a janela de agrupamento: responde na hora.
            if _contem_sinal_de_crise(texto):
                await _responder_turno(session, conversa, numero)
                return

    # Texto normal: (re)agenda o turno pra depois da janela; a rajada reseta o
    # timer, então só a última mensagem dispara uma única resposta.
    serializacao.agendar(numero, config_negocio.valor("debounce_segundos"), _turno_agendado)


async def _transcrever_audio_msg(mensagem: dict[str, Any]) -> str | None:
    """Baixa e transcreve o áudio da mensagem. None se não der (aí o fluxo escala).

    Não loga o conteúdo transcrito (LGPD) — só o serviço de transcrição registra
    o tamanho.
    """
    media_id = (mensagem.get("audio") or {}).get("id")
    if not media_id:
        return None
    try:
        conteudo, mime = await whatsapp_client.baixar_midia(media_id)
        texto = await transcricao.transcrever_audio(conteudo, mime)
    except (whatsapp_client.WhatsAppError, transcricao.TranscricaoError):
        logger.error("Falha ao baixar/transcrever áudio %s; vai escalar pra Thainá", media_id)
        return None
    return texto or None


async def _persistir_recebida(session, conversa, tipo, wamid, mensagem) -> str | None:
    """Persiste a mensagem recebida e devolve o texto (só faz sentido p/ texto).

    Imagem e documento: além da mensagem, baixa e guarda o anexo (a URL da Meta
    expira em minutos). Se o download falhar, a mensagem fica registrada mesmo
    assim — a Thainá vê que veio algo e pede de novo ao paciente.
    """
    if tipo == "text":
        texto = mensagem.get("text", {}).get("body", "")
        await conversation.registrar_mensagem_recebida(
            session, conversa, tipo="texto", texto=texto, whatsapp_message_id=wamid
        )
        return texto
    if tipo == "audio":
        await conversation.registrar_mensagem_recebida(
            session, conversa, tipo="audio", texto="[áudio recebido]", whatsapp_message_id=wamid
        )
        return None
    if tipo in midia.TIPOS_SUPORTADOS:
        registro = await conversation.registrar_mensagem_recebida(
            session, conversa, tipo=tipo, texto=midia.ROTULOS[tipo], whatsapp_message_id=wamid
        )
        try:
            await midia.baixar_e_guardar(session, registro, mensagem)
        except midia.MidiaError as exc:
            logger.error("Não consegui guardar o anexo da conversa %s: %s", conversa.id, exc)
        return None
    await conversation.registrar_mensagem_recebida(
        session, conversa, tipo=tipo, texto=None, whatsapp_message_id=wamid
    )
    return None


async def _turno_agendado(numero: str) -> None:
    """Processa o turno da conversa após a janela de agrupamento (sob o lock)."""
    async with serializacao.lock_da_conversa(numero):
        async with async_session() as session:
            conversa = await conversation.obter_conversa_por_numero(session, numero)
            # Pode ter virado modo humano (ex.: áudio no meio da rajada): não responde.
            if conversa is None or conversa.modo == "humano":
                return
            await _responder_turno(session, conversa, numero)


async def _responder_turno(session, conversa, numero: str) -> None:
    """Gera a resposta do bot (uma chamada ao LLM) e envia em bolhas."""
    resposta = await processar_turno_bot(session, conversa)
    await session.commit()
    if resposta is None:  # ex.: round-trip pós-tool sem fala final
        return
    await _enviar_em_bolhas(session, conversa, numero, resposta)


async def _enviar_em_bolhas(session, conversa, numero: str, resposta: str) -> None:
    """Quebra a resposta em bolhas curtas e envia em ordem, persistindo cada uma.

    Sanitiza antes de tudo: este é o único ponto por onde a fala do bot sai, então
    é aqui que a rede de proteção fica (ver `saida.limpar` e o P0 do BACKLOG.md).
    Se uma bolha falhar, pára (não adianta mandar o resto fora de ordem).
    """
    resposta = saida.limpar(resposta)
    if not resposta:
        return
    for bolha in whatsapp_client.dividir_em_bolhas(resposta):
        if config_negocio.valor("simular_digitacao"):
            await asyncio.sleep(whatsapp_client.intervalo_digitacao(bolha))
        try:
            envio = await whatsapp_client.enviar_texto(numero, bolha)
        except whatsapp_client.WhatsAppError:
            logger.error(f"Não consegui responder ao número {mascarar_telefone(numero)}")
            break
        # Guarda o wamid da nossa própria fala: é o que permite a Thainá citá-la
        # depois no painel (reply).
        await conversation.registrar_mensagem_enviada(
            session,
            conversa,
            texto=bolha,
            whatsapp_message_id=whatsapp_client.id_da_resposta(envio),
        )
        await session.commit()


def verify_signature(body: bytes, x_hub_signature: str) -> bool:
    """Verifica assinatura HMAC SHA256 do webhook Meta

    Args:
        body: Request body em bytes
        x_hub_signature: Header X-Hub-Signature-256 (formato: sha256=...)

    Returns:
        True se assinatura é válida, False caso contrário
    """
    if not x_hub_signature:
        return False

    # Remover prefixo "sha256=" se estiver presente
    if x_hub_signature.startswith("sha256="):
        signature = x_hub_signature[7:]
    else:
        signature = x_hub_signature

    # Calcular HMAC esperado
    expected = hmac.new(
        settings.whatsapp_app_secret.encode(),
        body,
        hashlib.sha256,
    ).hexdigest()

    # Comparação timing-safe
    return hmac.compare_digest(signature, expected)
