"""Webhook do WhatsApp - Passo 5: LLM com tool calling (escalada / cadastro)"""

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
    conversation,
    escalation,
    hamilton_client,
    llm_client,
    tools,
    whatsapp_client,
)

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

# Resposta fixa para tipos sem texto que não são áudio (imagem, vídeo, sticker...).
PEDIR_TEXTO = "Por enquanto consigo ler só mensagens de texto. Pode me escrever?"


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
        # Guarda os dados coletados e cadastra no Hamilton (buscar antes de criar).
        conversa.dados_coletados = {**(conversa.dados_coletados or {}), **tc.arguments}
        client = hamilton_client.get_hamilton_client()
        try:
            existentes = await client.buscar_paciente_por_telefone(
                tc.arguments.get("telefone_contato")
            )
            if existentes:
                pid = existentes[0].get("pk_paciente")
                conversa.paciente_hamilton_id = pid
                conversa.estado = "cadastrado"
                await session.flush()
                return {"status": "ja_cadastrado", "paciente_id": pid}

            criado = await client.criar_paciente(tc.arguments)
            conversa.paciente_hamilton_id = criado.get("pk_paciente")
            conversa.estado = "cadastrado"
            await session.flush()
            return {"status": "cadastrado", "paciente_id": conversa.paciente_hamilton_id}
        except hamilton_client.HamiltonError:
            # Hamilton indisponível: não derruba a conversa; Thainá cadastra manual.
            logger.error(f"Hamilton falhou no cadastro da conversa {conversa.id}")
            conversa.estado = "cadastro_pendente"
            await session.flush()
            return {"status": "cadastro_pendente"}

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
    """Processa o payload do webhook em background.

    Cada mensagem é processada isoladamente: um erro inesperado em uma não
    derruba o background task nem impede as demais (apenas é logado).
    """
    for mensagem in extrair_mensagens(payload):
        try:
            await _processar_mensagem(mensagem)
        except Exception:  # resiliência: nenhuma mensagem pode matar o worker
            logger.exception(
                f"Erro processando mensagem {mensagem.get('id')} " f"de {mensagem.get('from')}"
            )


async def _processar_mensagem(mensagem: dict[str, Any]) -> None:
    """Persiste e responde uma única mensagem recebida.

    Persiste conversa e mensagem (com idempotência) e, quando a conversa está
    em modo bot, gera a resposta via LLM (que pode disparar tools: escalada ou
    cadastro). Em modo humano apenas persiste (o painel da Thainá cuida da
    resposta). Tipos sem texto recebem uma mensagem fixa pedindo texto.
    """
    numero = mensagem.get("from")
    tipo = mensagem.get("type")
    wamid = mensagem.get("id")
    if not numero:
        return

    async with async_session() as session:
        if await conversation.mensagem_ja_processada(session, wamid):
            logger.info(f"Mensagem {wamid} já processada, ignorando")
            return

        conversa = await conversation.obter_ou_criar_conversa(session, numero)

        if tipo == "text":
            texto = mensagem.get("text", {}).get("body", "")
            await conversation.registrar_mensagem_recebida(
                session,
                conversa,
                tipo="texto",
                texto=texto,
                whatsapp_message_id=wamid,
            )
        elif tipo == "audio":
            await conversation.registrar_mensagem_recebida(
                session,
                conversa,
                tipo="audio",
                texto="[áudio recebido]",
                whatsapp_message_id=wamid,
            )
        else:
            await conversation.registrar_mensagem_recebida(
                session,
                conversa,
                tipo=tipo,
                texto=None,
                whatsapp_message_id=wamid,
            )

        # Modo humano: só persiste; a Thainá responde pelo painel.
        if conversa.modo == "humano":
            await session.commit()
            return

        if tipo == "audio":
            # Áudio escala direto pra Thainá, sem passar pelo LLM.
            await escalation.registrar_escalada(session, conversa, "audio_recebido")
            await escalation.alertar_thaina(conversa, "audio_recebido")
            resposta = AUDIO_RECEBIDO
        elif tipo == "text":
            resposta = await processar_turno_bot(session, conversa)
        else:
            resposta = PEDIR_TEXTO

        await session.commit()

        # Pode não haver texto a enviar (ex.: round-trip sem fala final).
        if resposta is None:
            return

        enviado = False
        try:
            await whatsapp_client.enviar_texto(numero, resposta)
            enviado = True
        except whatsapp_client.WhatsAppError:
            # Já logado no cliente; persistência da entrada não é perdida.
            logger.error(f"Não consegui responder ao número {numero}")

        if enviado:
            await conversation.registrar_mensagem_enviada(session, conversa, texto=resposta)
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
