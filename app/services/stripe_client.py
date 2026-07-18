"""Cliente da API do Stripe (REST, form-encoded), no padrão dos outros clientes.

A Sofia só GERA links e LÊ status: o checkout, o cartão, PCI e antifraude ficam
com o Stripe (páginas hospedadas por ele). Sem webhook e sem tabela local por
escolha — o Stripe é a única fonte de verdade, mesma decisão do painel do site
da Allos (ver docs/ do repo Allos-site). Se o Stripe cair, a tela avisa.

Chave vazia = feature desligada (`configurado()` é o gate; as rotas mostram
aviso em vez de quebrar). A chave dá controle total da conta financeira:
NUNCA logar, NUNCA commitá-la.
"""

import logging
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

BASE_URL = "https://api.stripe.com/v1"
# Versão fixada: um upgrade do Stripe não muda o formato das respostas em
# produção sem a gente pedir.
API_VERSION = "2025-09-30.clover"
TIMEOUT_SEGUNDOS = 20.0


class StripeError(Exception):
    """Falha ao falar com o Stripe (rede, auth, 4xx/5xx)."""


def configurado() -> bool:
    return bool(settings.stripe_secret_key)


def _achatar(dados: dict, prefixo: str = "") -> dict[str, Any]:
    """Achata dict/list aninhados pra notação de colchetes do Stripe.

    {"a": {"b": 1}, "c": [{"d": 2}]} -> {"a[b]": 1, "c[0][d]": 2}
    (a API do Stripe é form-encoded, não JSON).
    """
    plano: dict[str, Any] = {}
    for chave, valor in dados.items():
        k = f"{prefixo}[{chave}]" if prefixo else str(chave)
        if isinstance(valor, dict):
            plano.update(_achatar(valor, k))
        elif isinstance(valor, list):
            for i, item in enumerate(valor):
                ki = f"{k}[{i}]"
                if isinstance(item, dict):
                    plano.update(_achatar(item, ki))
                else:
                    plano[ki] = item
        elif isinstance(valor, bool):
            plano[k] = "true" if valor else "false"
        elif valor is not None:
            plano[k] = valor
    return plano


async def _requisicao(
    metodo: str, caminho: str, dados: dict | None = None, params: dict | None = None
) -> dict:
    if not configurado():
        raise StripeError("Stripe não configurado (STRIPE_SECRET_KEY vazia)")
    headers = {
        "Authorization": f"Bearer {settings.stripe_secret_key}",
        "Stripe-Version": API_VERSION,
    }
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SEGUNDOS) as client:
            resp = await client.request(
                metodo,
                f"{BASE_URL}{caminho}",
                headers=headers,
                data=_achatar(dados) if dados else None,
                params=params,
            )
    except httpx.HTTPError as exc:
        logger.error("Stripe inacessível em %s %s: %s", metodo, caminho, type(exc).__name__)
        raise StripeError("Stripe inacessível") from exc

    if resp.status_code >= 400:
        # Loga o detalhe no servidor; quem chama recebe só o genérico.
        detalhe = ""
        try:
            detalhe = resp.json().get("error", {}).get("message", "")
        except Exception:  # noqa: BLE001 - corpo não-JSON não pode quebrar o log
            pass
        logger.error("Stripe %s %s -> %s: %s", metodo, caminho, resp.status_code, detalhe)
        raise StripeError(f"Stripe retornou {resp.status_code}")
    return resp.json()


# ── Criação (gerar cobranças) ─────────────────────────────────────────────────


async def criar_preco(dados: dict) -> dict:
    """POST /prices — preço avulso ou recorrente (valores em CENTAVOS)."""
    return await _requisicao("POST", "/prices", dados=dados)


async def criar_payment_link(dados: dict) -> dict:
    """POST /payment_links — link reutilizável de pagamento único."""
    return await _requisicao("POST", "/payment_links", dados=dados)


async def criar_checkout_session(dados: dict) -> dict:
    """POST /checkout/sessions — checkout de assinatura (parcelado/terapia)."""
    return await _requisicao("POST", "/checkout/sessions", dados=dados)


# ── Leitura (status e listagem) ───────────────────────────────────────────────


async def obter_assinatura(assinatura_id: str) -> dict:
    return await _requisicao("GET", f"/subscriptions/{assinatura_id}")


async def listar_assinaturas(
    status: str | None = None, customer: str | None = None, limite: int = 100
) -> list[dict]:
    params: dict[str, Any] = {"limit": limite}
    if status:
        params["status"] = status
    if customer:
        params["customer"] = customer
    resposta = await _requisicao("GET", "/subscriptions", params=params)
    return resposta.get("data", [])


async def listar_faturas(assinatura_id: str, limite: int = 12) -> list[dict]:
    resposta = await _requisicao(
        "GET", "/invoices", params={"subscription": assinatura_id, "limit": limite}
    )
    return resposta.get("data", [])


async def obter_checkout_session(session_id: str) -> dict:
    return await _requisicao("GET", f"/checkout/sessions/{session_id}")


async def listar_payment_links(limite: int = 100) -> list[dict]:
    resposta = await _requisicao("GET", "/payment_links", params={"limit": limite})
    return resposta.get("data", [])


async def listar_sessions_do_payment_link(plink_id: str, limite: int = 20) -> list[dict]:
    """Sessions de checkout abertas a partir de um payment link (pra saber se pagou)."""
    resposta = await _requisicao(
        "GET", "/checkout/sessions", params={"payment_link": plink_id, "limit": limite}
    )
    return resposta.get("data", [])
