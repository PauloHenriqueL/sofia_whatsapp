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
    """Há chave utilizável **neste ambiente**?

    Fora de produção isso pergunta pela chave de TESTE — ver `settings.stripe_key`.
    Em dev sem `TEST_STRIPE_SECRET_KEY`, devolve False e o Stripe fica desligado:
    a tela de Pagamentos mostra o aviso e a cobrança oferece só o Pix.
    """
    return bool(settings.stripe_key)


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
        qual = "TEST_STRIPE_SECRET_KEY" if settings.stripe_modo_teste else "STRIPE_SECRET_KEY"
        raise StripeError(f"Stripe não configurado ({qual} vazia)")
    # 🔴 `settings.stripe_key`, NUNCA `settings.stripe_secret_key`. Fora de
    # produção a primeira é a chave de teste; a segunda é a live que está no
    # `.env` de desenvolvimento e cria coisa de verdade na conta da Allos.
    headers = {
        "Authorization": f"Bearer {settings.stripe_key}",
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


async def obter_preco(preco_id: str) -> dict:
    return await _requisicao("GET", f"/prices/{preco_id}")


async def obter_assinatura(assinatura_id: str) -> dict:
    return await _requisicao("GET", f"/subscriptions/{assinatura_id}")


async def atualizar_assinatura(assinatura_id: str, dados: dict) -> dict:
    """POST /subscriptions/{id} — é AQUI que `cancel_at` existe.

    O parâmetro não é aceito na criação via Checkout Session nem via Payment
    Link (a API responde 400 `parameter_unknown`), só na assinatura já criada.
    Por isso o limite do parcelado é reconciliação, não criação.
    """
    return await _requisicao("POST", f"/subscriptions/{assinatura_id}", dados=dados)


async def listar_assinaturas(
    status: str | None = None,
    customer: str | None = None,
    limite: int = 100,
    expand: list[str] | None = None,
    paginas: int = 1,
) -> list[dict]:
    """`paginas=1` é a página que a tela mostra; o reconciliador pede TODAS.

    Assinatura que ele não enxerga é paciente que continua sendo cobrado depois da
    última parcela — o exato bug que ele existe pra consertar. Por isso `paginas`
    alto lá e não aqui: a listagem do painel não precisa varrer a conta inteira a
    cada carregamento de página.
    """
    todas: list[dict] = []
    depois: str | None = None
    for _ in range(max(1, paginas)):
        params: dict[str, Any] = {"limit": limite}
        if status:
            params["status"] = status
        if customer:
            params["customer"] = customer
        if expand:
            params["expand[]"] = expand
        if depois:
            params["starting_after"] = depois
        resposta = await _requisicao("GET", "/subscriptions", params=params)
        dados = resposta.get("data", [])
        todas.extend(dados)
        if not resposta.get("has_more") or not dados:
            break
        depois = dados[-1]["id"]
    return todas


async def listar_produtos(limite: int = 100) -> dict[str, str]:
    """{product_id: nome}, paginado. Só o reconciliador usa (1x por rodada)."""
    nomes: dict[str, str] = {}
    depois: str | None = None
    while True:
        params: dict[str, Any] = {"limit": limite}
        if depois:
            params["starting_after"] = depois
        resposta = await _requisicao("GET", "/products", params=params)
        dados = resposta.get("data", [])
        for produto in dados:
            nomes[produto["id"]] = produto.get("name") or ""
        if not resposta.get("has_more") or not dados:
            return nomes
        depois = dados[-1]["id"]


async def listar_faturas(assinatura_id: str, limite: int = 12) -> list[dict]:
    resposta = await _requisicao(
        "GET", "/invoices", params={"subscription": assinatura_id, "limit": limite}
    )
    return resposta.get("data", [])


async def obter_checkout_session(session_id: str) -> dict:
    return await _requisicao("GET", f"/checkout/sessions/{session_id}")


async def obter_payment_link(plink_id: str) -> dict:
    return await _requisicao("GET", f"/payment_links/{plink_id}")


async def listar_payment_links(limite: int = 100, paginas: int = 10) -> list[dict]:
    """Payment links, paginado.

    Só serve pra resolver referência antiga guardada como URL (a URL não carrega
    o `plink_`). Como agora nasce um link por paciente, a conta passa de 100
    rápido — sem paginar, um vínculo antigo viraria "não encontrado" em silêncio.
    """
    todos: list[dict] = []
    depois: str | None = None
    for _ in range(paginas):
        params: dict[str, Any] = {"limit": limite}
        if depois:
            params["starting_after"] = depois
        resposta = await _requisicao("GET", "/payment_links", params=params)
        dados = resposta.get("data", [])
        todos.extend(dados)
        if not resposta.get("has_more") or not dados:
            break
        depois = dados[-1]["id"]
    return todos


async def listar_sessions_do_payment_link(plink_id: str, limite: int = 20) -> list[dict]:
    """Sessions de checkout abertas a partir de um payment link (pra saber se pagou)."""
    resposta = await _requisicao(
        "GET", "/checkout/sessions", params={"payment_link": plink_id, "limit": limite}
    )
    return resposta.get("data", [])
