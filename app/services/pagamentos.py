"""Links de pagamento e vínculo paciente <-> Stripe (painel da Thainá).

Portado do painel de pagamentos do site da Allos (guia
`reproduzir-painel-pagamentos.md`), já com os bugs do original corrigidos:
parcelado com `cancel_at` (senão cobraria pra sempre), metadata padronizada
(`nome_cliente`), tipo via `metadata.tipo` e arredondamento único (round).

Três operações:
- **Link avulso/parcelado (neuro):** 1x vira Payment Link; 2-6x vira uma
  ASSINATURA mensal do valor da parcela que se cancela sozinha após N cobranças
  (o Stripe não tem parcelamento de cartão nativo no Brasil — explicar ao
  paciente que são "N cobranças mensais", não "parcelado em N vezes").
- **Mensalidade da terapia:** assinatura recorrente sem fim, valor cheio no ato
  e renovação no mesmo dia todo mês — **sem pro-rata e sem dia fixo** (o porquê
  está no bloco de decisão acima de `criar_assinatura_mensalidade`). Usada pelo
  painel E pela cobrança automática da Sofia: se cada um cobrasse do seu jeito,
  o mesmo paciente pagaria valores diferentes conforme quem gerou o link.
- **Status por referência:** dado um `sub_`/`cs_`/`cus_`/`plink_`/URL do link,
  resolve na API do Stripe em que pé está o pagamento daquele paciente.
"""

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone

from app.services import stripe_client
from app.services.stripe_client import StripeError  # re-export pros routers

__all__ = ["StripeError", "ErroValidacao"]

logger = logging.getLogger(__name__)

# Limites dos formulários (iguais no form HTML, senão o erro só aparece depois).
VALOR_MIN, VALOR_MAX = 5, 5000  # link avulso/parcelado (R$)
TERAPIA_MIN, TERAPIA_MAX = 50, 2000  # assinatura mensal (R$)
PARCELAS_MIN, PARCELAS_MAX = 1, 6
DESCONTO_MAX = 30  # %

_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")

# Status do Stripe -> rótulo em português (o usuário não deve ver `past_due`).
STATUS_ASSINATURA = {
    "active": "Ativa",
    "past_due": "Atrasada",
    "canceled": "Cancelada",
    "trialing": "Teste",
    "unpaid": "Não paga",
    "incomplete": "Incompleta",
    "incomplete_expired": "Expirada",
}
STATUS_FATURA = {
    "paid": "Paga",
    "open": "Em aberto",
    "draft": "Rascunho",
    "void": "Anulada",
    "uncollectible": "Incobrável",
}

# Estado unificado do vínculo paciente <-> Stripe (badge no painel).
ROTULO_ESTADO = {
    "pago": "Pago",
    "ativa": "Assinatura ativa",
    "atrasada": "Pagamento atrasado",
    "aguardando": "Aguardando pagamento",
    "cancelada": "Cancelada",
    "sem_assinatura": "Sem assinatura",
    "nao_encontrado": "Não encontrado no Stripe",
    "erro": "Stripe indisponível",
}


class ErroValidacao(ValueError):
    """Entrada inválida do formulário; a mensagem é mostrada à Thainá."""


def fmt_centavos(centavos: int) -> str:
    """Centavos (como o Stripe trabalha) -> 'R$ 1.234,56'."""
    reais = f"{centavos / 100:.2f}".replace(".", ",")
    inteiro, _, decimais = reais.partition(",")
    inteiro = re.sub(r"\B(?=(\d{3})+(?!\d))", ".", inteiro)
    return f"R$ {inteiro},{decimais}"


def _validar_email(email: str) -> str:
    email = (email or "").strip().lower()
    if not _EMAIL_RE.match(email):
        raise ErroValidacao("E-mail inválido.")
    return email


def _validar_nome(nome: str) -> str:
    nome = (nome or "").strip()
    if not nome:
        raise ErroValidacao("Nome do paciente é obrigatório.")
    return nome


# ── Link avulso / parcelado (neuro) ───────────────────────────────────────────


async def criar_link_neuro(
    nome: str,
    email: str,
    valor_total: float,
    parcelas: int = 1,
    desconto: int = 0,
    agora: datetime | None = None,
) -> dict:
    """Cria o link de cobrança e devolve {link, ref, resumo}.

    `ref` é a referência pra vincular ao paciente: a URL do Payment Link (1x)
    ou o id da checkout session `cs_...` (parcelado).
    """
    nome = _validar_nome(nome)
    email = _validar_email(email)
    if not isinstance(valor_total, (int, float)) or not (VALOR_MIN <= valor_total <= VALOR_MAX):
        raise ErroValidacao(f"Valor deve estar entre R$ {VALOR_MIN} e R$ {VALOR_MAX}.")
    if not (PARCELAS_MIN <= parcelas <= PARCELAS_MAX):
        raise ErroValidacao(f"Parcelas devem estar entre {PARCELAS_MIN} e {PARCELAS_MAX}.")
    if not (0 <= desconto <= DESCONTO_MAX):
        raise ErroValidacao(f"Desconto deve estar entre 0% e {DESCONTO_MAX}%.")

    from app.config import settings

    agora = agora or datetime.now(timezone.utc)
    # O Stripe trabalha em CENTAVOS, sempre. round() aqui e no preview.
    total_centavos = round(valor_total * (1 - desconto / 100) * 100)
    parcela_centavos = round(total_centavos / parcelas)

    metadata = {
        "nome_cliente": nome,
        "email_cliente": email,
        "tipo": "neuro",
        "desconto_percentual": str(desconto),
    }

    if parcelas == 1:
        preco = await stripe_client.criar_preco(
            {
                "unit_amount": total_centavos,
                "currency": "brl",
                "product_data": {"name": f"Avaliação Neuropsicológica - {nome}"},
            }
        )
        link_obj = await stripe_client.criar_payment_link(
            {
                "line_items": [{"price": preco["id"], "quantity": 1}],
                "metadata": metadata,
                "after_completion": {
                    "type": "redirect",
                    "redirect": {"url": f"{settings.base_url}/pagamento-sucesso"},
                },
            }
        )
        link, ref = link_obj["url"], link_obj["url"]
    else:
        # "Parcelado" = assinatura mensal do valor da parcela que se cancela
        # sozinha após N cobranças (meses de 30 dias — aproximado de propósito).
        preco = await stripe_client.criar_preco(
            {
                "unit_amount": parcela_centavos,
                "currency": "brl",
                "recurring": {"interval": "month", "interval_count": 1},
                "product_data": {"name": f"Avaliação Neuropsicológica - {nome} ({parcelas}x)"},
            }
        )
        session = await stripe_client.criar_checkout_session(
            {
                "mode": "subscription",
                "locale": "pt-BR",
                "customer_email": email,
                "line_items": [{"price": preco["id"], "quantity": 1}],
                "success_url": f"{settings.base_url}/pagamento-sucesso",
                "cancel_url": f"{settings.base_url}/pagamento-cancelado",
                "subscription_data": {
                    "cancel_at": int((agora + timedelta(days=30 * parcelas)).timestamp()),
                    "metadata": {
                        **metadata,
                        "parcelas_total": str(parcelas),
                        "valor_total_centavos": str(total_centavos),
                    },
                },
            }
        )
        link, ref = session["url"], session["id"]

    return {
        "link": link,
        "ref": ref,
        "resumo": {
            "valor_total": fmt_centavos(total_centavos),
            "parcelas": parcelas,
            "valor_parcela": fmt_centavos(parcela_centavos),
            "desconto": f"{desconto}%" if desconto else "0",
        },
    }


# ── Mensalidade da terapia (Sofia e painel usam ESTA) ─────────────────────────
#
# Assinatura mensal simples: paga o valor cheio hoje e renova no mesmo dia todo
# mês. **Sem pro-rata e sem dia fixo.**
#
# Havia uma versão ancorada no dia 10 com pro-rata na entrada
# (`billing_cycle_anchor` + `create_prorations`). Foi removida: o valor saía
# diferente pra cada paciente — R$ 6,67 pra quem entrava dia 9 (e R$ 200 no dia
# seguinte), R$ 206,67 pra quem entrava dia 10. Isso passava no painel, onde a
# Thainá via o número antes de mandar; automatizado ninguém corrige. E como no Pix
# não existe pro-rata, a Sofia teria que anunciar um valor no Pix diferente do que
# o Stripe cobra no cartão.
#
# Alinhar ao dia 10 SEM pro-rata também foi descartado. As duas formas que o Stripe
# oferece não servem:
#   - `billing_cycle_anchor` só aceita datas dentro de UM ciclo (≤ ~31 dias num
#     plano mensal) e, com `proration_behavior: "none"`, **não cobra nada** na
#     entrada — a Session sai `no_payment_required`. O Stripe ainda proíbe item
#     avulso nessa combinação.
#   - `trial_end` + item avulso cobra certo, mas faz o checkout exibir **"avaliação
#     gratuita"** e uma linha de R$ 0,00 na fatura. Texto não customizável, e numa
#     cobrança de terapia é onde menos se pode confundir.
#
# O dia 10 continua valendo pra quem paga por **Pix** — lá é uma data que a pessoa
# precisa lembrar. No cartão a cobrança é automática e a data não muda nada.


async def criar_assinatura_mensalidade(
    nome: str,
    valor_mensal: float,
    email: str = "",
    agora: datetime | None = None,
) -> dict:
    """Assinatura mensal da terapia: valor cheio hoje, valor cheio todo mês.

    `email` é OPCIONAL de propósito. A Sofia nunca coletou e-mail (não está na tool
    `cadastrar_paciente`), e pedir mais um dado por WhatsApp pra pré-preencher um
    campo que o Stripe coleta de qualquer jeito só adiciona atrito. Sem e-mail, o
    Checkout pergunta — e o que a pessoa digita lá é mais confiável que um e-mail
    ditado numa conversa. O painel continua mandando (a Thainá tem o dado).
    """
    nome = _validar_nome(nome)
    email = (email or "").strip().lower()
    if email and not _EMAIL_RE.match(email):
        raise ErroValidacao("E-mail inválido.")
    if not isinstance(valor_mensal, (int, float)) or not (
        TERAPIA_MIN <= valor_mensal <= TERAPIA_MAX
    ):
        raise ErroValidacao(f"Valor deve estar entre R$ {TERAPIA_MIN} e R$ {TERAPIA_MAX}.")

    from app.config import settings

    valor_centavos = round(valor_mensal * 100)

    recorrente: dict = {
        "price_data": {
            "currency": "brl",
            "product_data": {"name": f"Mensalidade Terapia - {nome}"},
            "unit_amount": valor_centavos,
            "recurring": {"interval": "month"},
        },
        "quantity": 1,
    }
    # Reusa o preço do catálogo quando o valor bate, pros relatórios do Stripe não
    # ficarem com um produto novo por paciente. Valor diferente (desconto, bolsa)
    # cai no inline. Falha de leitura não bloqueia a cobrança.
    if settings.stripe_preco_mensal_id:
        try:
            preco_catalogo = await stripe_client.obter_preco(settings.stripe_preco_mensal_id)
            if preco_catalogo.get("unit_amount") == valor_centavos:
                recorrente = {"price": settings.stripe_preco_mensal_id, "quantity": 1}
        except StripeError:
            logger.warning("Não li o preço do catálogo; usando preço inline")

    payload: dict = {
        "mode": "subscription",
        "locale": "pt-BR",
        "line_items": [recorrente],
        "subscription_data": {
            "metadata": {"nome_cliente": nome, "email_cliente": email, "tipo": "clinica"},
        },
        "success_url": f"{settings.base_url}/pagamento-sucesso",
        "cancel_url": f"{settings.base_url}/pagamento-cancelado",
    }
    if email:
        payload["customer_email"] = email

    session = await stripe_client.criar_checkout_session(payload)
    return {
        "link": session["url"],
        "ref": session["id"],
        "valor_mensal": fmt_centavos(valor_centavos),
        "valor_entrada": fmt_centavos(valor_centavos),
    }


# ── Listagem de assinaturas (aba Assinaturas) ─────────────────────────────────


async def listar_assinaturas_painel(status: str = "all", tipo: str = "all") -> list[dict]:
    """Assinaturas ao vivo do Stripe, com faturas, no formato do painel.

    N+1 assumido (uma chamada de faturas por assinatura, em paralelo) — ok até
    ~100 assinaturas; acima disso, paginar e carregar faturas sob demanda.
    """
    assinaturas = await stripe_client.listar_assinaturas(status=None if status == "all" else status)

    async def _montar(sub: dict) -> dict | None:
        # Classifica pelo metadata (inferir do nome do produto é frágil).
        sub_tipo = "neuro" if (sub.get("metadata") or {}).get("tipo") == "neuro" else "clinica"
        if tipo != "all" and sub_tipo != tipo:
            return None
        try:
            faturas = await stripe_client.listar_faturas(sub["id"])
        except StripeError:
            faturas = []
        agora_ms = datetime.now(timezone.utc).timestamp() * 1000
        pagas = [f for f in faturas if f.get("status") == "paid"]
        atrasadas = [
            f
            for f in faturas
            if f.get("status") == "open" and f.get("due_date") and f["due_date"] * 1000 < agora_ms
        ]
        item = (sub.get("items") or {}).get("data") or [{}]
        metadata = sub.get("metadata") or {}
        return {
            "id": sub["id"],
            "nome_cliente": metadata.get("nome_cliente") or "(sem nome)",
            "status": sub.get("status"),
            "status_rotulo": STATUS_ASSINATURA.get(sub.get("status"), sub.get("status")),
            "tipo": sub_tipo,
            "valor_parcela": fmt_centavos((item[0].get("price") or {}).get("unit_amount") or 0),
            "parcelas_pagas": len(pagas),
            "parcelas_total": int(metadata.get("parcelas_total") or 0),
            "parcelas_atrasadas": len(atrasadas),
            "criado_em": sub.get("created"),
            "cancela_em": sub.get("cancel_at"),
            "faturas": [
                {
                    "numero": f.get("number"),
                    "valor": fmt_centavos(f.get("amount_due") or 0),
                    "status": f.get("status"),
                    "status_rotulo": STATUS_FATURA.get(f.get("status"), f.get("status")),
                    "criada_em": f.get("created"),
                    "url_pagamento": f.get("hosted_invoice_url"),
                    "url_pdf": f.get("invoice_pdf"),
                }
                for f in faturas
            ],
        }

    montadas = await asyncio.gather(*(_montar(s) for s in assinaturas))
    return [m for m in montadas if m]


# ── Vínculo paciente <-> Stripe (a referência) ────────────────────────────────


def interpretar_referencia(texto: str) -> tuple[str, str]:
    """Normaliza o que a Thainá colou -> (tipo, id/url).

    Aceita: `sub_...` (assinatura), `cs_...` (checkout), `cus_...` (cliente),
    `plink_...` ou a URL do link de pagamento (buy.stripe.com). URLs de checkout
    (checkout.stripe.com) contêm o `cs_...` e também são aceitas.
    """
    ref = (texto or "").strip()
    if re.fullmatch(r"sub_[A-Za-z0-9]+", ref):
        return ("assinatura", ref)
    if re.fullmatch(r"cs_[A-Za-z0-9_]+", ref):
        return ("checkout", ref)
    if re.fullmatch(r"cus_[A-Za-z0-9]+", ref):
        return ("cliente", ref)
    if re.fullmatch(r"plink_[A-Za-z0-9]+", ref):
        return ("link", ref)
    if ref.startswith("https://buy.stripe.com/"):
        return ("link_url", ref)
    achado = re.search(r"cs_[A-Za-z0-9_]+", ref)
    if ref.startswith("https://checkout.stripe.com/") and achado:
        return ("checkout", achado.group())
    raise ErroValidacao(
        "Não reconheci essa referência. Aceito o ID da assinatura (sub_...), do "
        "checkout (cs_...), do cliente (cus_...) ou a URL do link de pagamento "
        "(buy.stripe.com/...)."
    )


def _estado(chave: str, detalhe: str = "") -> dict:
    return {"estado": chave, "rotulo": ROTULO_ESTADO[chave], "detalhe": detalhe}


async def _status_da_assinatura(assinatura_id: str) -> dict:
    sub = await stripe_client.obter_assinatura(assinatura_id)
    status = sub.get("status")
    metadata = sub.get("metadata") or {}
    total = int(metadata.get("parcelas_total") or 0)

    if status in ("active", "trialing"):
        try:
            faturas = await stripe_client.listar_faturas(assinatura_id)
            pagas = sum(1 for f in faturas if f.get("status") == "paid")
        except StripeError:
            pagas = 0
        detalhe = (
            f"{pagas} de {total} parcelas pagas" if total else f"{pagas} mensalidade(s) paga(s)"
        )
        return _estado("ativa", detalhe)
    if status in ("past_due", "unpaid"):
        return _estado("atrasada")
    if status == "canceled":
        # Parcelamento que se cancelou após pagar tudo = quitado, não "cancelada".
        if total:
            try:
                faturas = await stripe_client.listar_faturas(assinatura_id)
                pagas = sum(1 for f in faturas if f.get("status") == "paid")
            except StripeError:
                pagas = 0
            if pagas >= total:
                return _estado("pago", f"{pagas}x pagas")
        return _estado("cancelada")
    return _estado("aguardando")  # incomplete / incomplete_expired


async def status_da_referencia(ref: str) -> dict:
    """Estado unificado do pagamento: {estado, rotulo, detalhe}.

    Nunca levanta exceção: referência inválida ou Stripe fora do ar viram
    estados ("nao_encontrado" / "erro") — o painel sempre renderiza.
    """
    try:
        tipo, valor = interpretar_referencia(ref)
    except ErroValidacao:
        return _estado("nao_encontrado")

    try:
        if tipo == "assinatura":
            return await _status_da_assinatura(valor)

        if tipo == "checkout":
            session = await stripe_client.obter_checkout_session(valor)
            if session.get("subscription"):
                return await _status_da_assinatura(session["subscription"])
            if session.get("payment_status") == "paid":
                return _estado("pago")
            if session.get("status") == "expired":
                return _estado("cancelada", "link de pagamento expirou")
            return _estado("aguardando", "link enviado, ainda sem pagamento")

        if tipo == "cliente":
            subs = await stripe_client.listar_assinaturas(customer=valor, limite=10)
            if not subs:
                return _estado("sem_assinatura")
            return await _status_da_assinatura(subs[0]["id"])  # a mais recente

        # tipo em ("link", "link_url"): payment link de cobrança única
        plink_id = valor
        if tipo == "link_url":
            links = await stripe_client.listar_payment_links()
            plink_id = next((pl["id"] for pl in links if pl.get("url") == valor), None)
            if plink_id is None:
                return _estado("nao_encontrado")
        sessions = await stripe_client.listar_sessions_do_payment_link(plink_id)
        pagas = [s for s in sessions if s.get("payment_status") == "paid"]
        if pagas:
            return _estado("pago")
        return _estado("aguardando", "link enviado, ainda sem pagamento")
    except StripeError:
        return _estado("erro")


async def anotar_pagamentos(itens: list[dict]) -> None:
    """Anexa `item["pagamento"]` (status) aos itens de cobrança que têm ref.

    Usado pela fila "Pronto pra cobrança" do acompanhamento. Tolerante: sem
    chave configurada ou com Stripe fora do ar, os itens ficam sem a anotação.
    """
    if not stripe_client.configurado():
        return
    com_ref = [i for i in itens if i.get("stripe_ref")]
    if not com_ref:
        return
    statuses = await asyncio.gather(*(status_da_referencia(i["stripe_ref"]) for i in com_ref))
    for item, st in zip(com_ref, statuses):
        item["pagamento"] = st
