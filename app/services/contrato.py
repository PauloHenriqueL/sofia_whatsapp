"""Contrato terapêutico assinado pelo paciente (Demanda E) — lado da Sofia.

A Sofia faz três coisas aqui, e nenhuma delas é gerar o documento:

  1. **decide quem recebe** (`motivo_para_pular`) — é ela que tem a conversa, e
     portanto o único lugar onde dá pra saber que a pessoa é de parceria, é menor
     de idade, veio pra neuro ou já escalou por não poder pagar;
  2. **encurta o link** (`assina.ae/xxxxx` → `allos.org.br/p/xxxxxxx`) — cobrança
     chegando de domínio desconhecido no WhatsApp tem o formato exato de golpe,
     e o encurtador já existe aqui;
  3. **conta pro modelo em que pé está o contrato** (`linhas_para_prompt`), pra
     ela conseguir responder "assinei" ou "não consegui abrir" sem inventar.

Quem gera o `.docx`, fala com a Autentique e guarda o assinado é o **Hamilton** —
é lá que moram o prontuário e a contabilidade. O texto do contrato, porém, mora
aqui (`/painel/prompts`), e vai no corpo da requisição: uma fonte só.

Desligado por padrão (`contrato_ativo`), como a cobrança e pelo mesmo motivo:
fluxo automático que manda documento jurídico pra paciente sobe dark e é ligado
por ato explícito de quem opera.
"""

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Conversa
from app.services import config_negocio, config_prompt, hamilton_client, links

logger = logging.getLogger(__name__)

# Status que a Sofia entende, vindos do Hamilton.
ASSINADO = "assinado"
PENDENTE = "pendente"


def ativo() -> bool:
    return bool(config_negocio.valor("contrato_ativo"))


def _e_parceria(conversa: Conversa) -> bool:
    """Flag vinda da `Captacao` do Hamilton, gravada no cadastro.

    ⚠️ Ela só é confiável se o Hamilton expuser `is_parceria` — hoje isso vive
    numa branch não mergeada (ver `docs/demandas/05-contrato-assinatura.md`).
    Sem ela, todo mundo parece particular. A guarda de VALOR do Hamilton é a
    segunda linha de defesa justamente por isso: paciente de parceria tem
    `vlr_sessao = 0` e o contrato é recusado lá.
    """
    return bool((conversa.dados_coletados or {}).get("is_parceria"))


async def motivo_para_pular(db: AsyncSession, conversa: Conversa) -> str | None:
    """Por que NÃO mandar contrato pra esta pessoa (ou `None` se pode mandar).

    Uma lista só, no espírito do `pesquisa.motivo_para_pular_entrada`: duas
    divergiriam na primeira mudança. O motivo vai pro log em uma linha — é por
    ele que se debuga "por que não veio o contrato?".
    """
    # Importado aqui: `pesquisa` importa `cadastro`, que importa este módulo em
    # nenhum ponto hoje, mas o ciclo já mordeu antes neste repo.
    from app.services import pesquisa

    if not ativo():
        return "contrato desligado no painel"
    if not conversa.paciente_hamilton_id:
        return "sem paciente no Hamilton"
    if _e_parceria(conversa):
        return "paciente de parceria (não paga mensalidade)"
    if conversa.arquivada_em is not None:
        return "conversa arquivada"
    if await pesquisa._e_neuro(db, conversa):
        return "avaliação neuropsicológica (o contrato é de terapia)"
    if await _escalou_por_gratuidade(db, conversa):
        return "escalada de gratuidade aberta (decisão humana)"
    return None


async def _escalou_por_gratuidade(db: AsyncSession, conversa: Conversa) -> bool:
    """Quem disse que não pode pagar não recebe contrato de mensalidade.

    A cobrança já trata isso escalando; o contrato precisa da mesma guarda,
    senão a Sofia manda o documento no mesmo turno em que a Thainá está
    decidindo se a pessoa entra por gratuidade.
    """
    from sqlalchemy import select

    from app.models import Escalada

    achou = await db.execute(
        select(Escalada.id)
        .where(
            Escalada.conversa_id == conversa.id,
            Escalada.motivo.in_(("gratuidade", "prefeitura")),
            Escalada.resolvida_em.is_(None),
        )
        .limit(1)
    )
    return achou.scalar_one_or_none() is not None


async def garantir(db: AsyncSession, conversa: Conversa, valor_mensal: int) -> dict:
    """Pede o contrato ao Hamilton e devolve `{status, link}` já encurtado.

    Devolve `{}` quando não há contrato pra oferecer — por guarda, por falha do
    Hamilton ou por a feature estar desligada. **Nunca levanta**: a cobrança não
    pode parar porque o contrato não saiu; ela continua com o pagamento, e a
    falta do contrato aparece na fila da Thainá.
    """
    motivo = await motivo_para_pular(db, conversa)
    if motivo:
        logger.info("Contrato pulado (conversa %s): %s", conversa.id, motivo)
        return {}

    texto = config_prompt.texto("prompt_contrato")
    if not texto.strip():
        logger.error("Texto do contrato vazio — nada foi enviado pra assinatura.")
        return {}

    try:
        resposta = await hamilton_client.get_hamilton_client().gerar_contrato(
            paciente_id=conversa.paciente_hamilton_id,
            valor_mensal=valor_mensal,
            texto=texto,
        )
    except hamilton_client.HamiltonError as exc:
        logger.error("Não consegui gerar o contrato da conversa %s: %s", conversa.id, exc)
        return {}

    return await _com_link_curto(db, conversa, resposta)


async def estado(db: AsyncSession, conversa: Conversa) -> dict:
    """Estado atual do contrato, sem criar nada. `{}` se não houver."""
    if not conversa.paciente_hamilton_id:
        return {}
    try:
        resposta = await hamilton_client.get_hamilton_client().status_contrato(
            conversa.paciente_hamilton_id
        )
    except hamilton_client.HamiltonError as exc:
        logger.warning("Não consegui ler o status do contrato (conversa %s): %s", conversa.id, exc)
        return {}
    if not resposta or not resposta.get("status") or resposta.get("status") == "nenhum":
        return {}
    return await _com_link_curto(db, conversa, resposta)


async def _com_link_curto(db: AsyncSession, conversa: Conversa, resposta: dict) -> dict:
    """Troca o `assina.ae/...` pelo domínio da Allos.

    O encurtador é idempotente por destino, então o slug é o mesmo em todos os
    turnos — a pessoa não recebe dois endereços diferentes pro mesmo contrato.
    """
    dados = dict(resposta)
    bruto = (dados.get("link") or "").strip()
    if bruto:
        curto = await links.encurtar(db, bruto, conversa.id)
        dados["link"] = curto or bruto
    return dados


def linhas_para_prompt(dados: dict) -> list[str]:
    """O que o modelo precisa saber sobre o contrato, em texto.

    Texto e não tool, de propósito: o estado como texto está sempre atualizado e
    não depende de o modelo lembrar de chamar nada. A Demanda D já ensinou que a
    Sofia precisa **usar** a resposta que ela mesma provocou.
    """
    if not dados:
        # Silêncio: sem contrato, o prompt não fala do assunto. Uma instrução
        # negativa ("não mencione contrato") é convite pra ele mencionar.
        return []

    status = dados.get("status")
    link = (dados.get("link") or "").strip()

    if status == ASSINADO:
        return [
            "- Contrato: JÁ ASSINADO por esta pessoa. Não mande o link de novo. "
            "Se ela perguntar, confirme que está tudo certo e siga pro pagamento."
        ]
    if status == PENDENTE and link:
        return [
            f"- Contrato pra assinar: {link}",
            "- Mande o contrato e o pagamento na MESMA mensagem: são a mesma decisão "
            "pra ela. Fale do contrato primeiro, em uma linha, sem juridiquês — é o "
            "documento que formaliza o acompanhamento.",
            "- Ela assina pelo próprio celular, com nome e CPF, ali no link. Não peça "
            "CPF na conversa: é o site da assinatura que pergunta.",
            "- Assinar NÃO é condição pra continuar o atendimento. Se ela travar no "
            "contrato, siga com o pagamento e não insista mais de uma vez.",
        ]
    if status == PENDENTE:
        return [
            "- Contrato: existe um pendente, mas o link NÃO está disponível nesta "
            "conversa. NÃO invente um link e NÃO prometa mandar depois — siga só "
            "com o pagamento."
        ]
    # recusado / expirado / substituído: assunto encerrado, quem trata é gente.
    return [
        "- Contrato: não há um contrato ativo pra assinar agora. NÃO fale de "
        "contrato; siga só com o pagamento."
    ]
