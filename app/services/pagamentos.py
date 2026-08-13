"""Links de pagamento e vínculo paciente <-> Stripe (painel da Thainá).

Portado do painel de pagamentos do site da Allos (guia
`reproduzir-painel-pagamentos.md`), já com os bugs do original corrigidos:
parcelado com `cancel_at` (senão cobraria pra sempre), metadata padronizada
(`nome_cliente`), tipo via `metadata.tipo` e arredondamento único (round).

Quatro operações:
- **Link avulso/parcelado (neuro):** 1x vira um pagamento único; 2-6x vira uma
  ASSINATURA mensal do valor da parcela, limitada a N cobranças pelo
  reconciliador (o Stripe não tem parcelamento de cartão nativo no Brasil —
  explicar ao paciente que são "N cobranças mensais", não "parcelado em N vezes").
- **Mensalidade da terapia:** assinatura recorrente sem fim, valor cheio no ato
  e renovação no mesmo dia todo mês — **sem pro-rata e sem dia fixo** (o porquê
  está no bloco de decisão acima de `criar_assinatura_mensalidade`). Usada pelo
  painel E pela cobrança automática da Sofia: se cada um cobrasse do seu jeito,
  o mesmo paciente pagaria valores diferentes conforme quem gerou o link.
- **Reconciliador (`limitar_parcelado`):** põe o fim da linha nas assinaturas de
  parcelado que ainda não têm. Ver o bloco de decisão acima da função.
- **Status por referência:** dado um `sub_`/`cs_`/`cus_`/`plink_`/URL do link,
  resolve na API do Stripe em que pé está o pagamento daquele paciente.

🔴 **Tudo aqui sai como Payment Link, nunca Checkout Session.** A Session tem
duas propriedades que a tornam imprópria pra um link que vai por WhatsApp e fica
dias parado: a URL passa de 300 caracteres (`checkout.stripe.com/c/pay/cs_...#fid...`)
e **expira em 24h**. O Payment Link é `buy.stripe.com/<10 chars>` e não vence.
Em troca ele é reutilizável — por isso todo link nasce com
`restrictions.completed_sessions.limit = 1`.
"""

import asyncio
import calendar
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

# Teto por rodada do reconciliador. Ele cancela assinatura de paciente real; um
# bug meu com a conta inteira na mão vira cancelamento em massa. Passando disso,
# a rodada para e reporta — a próxima pega o resto.
LIMITE_POR_RODADA = 20

# Folga entre a última cobrança devida e o cancelamento. A fatura N sai um MÊS
# antes do corte, então um dia de folga não corre risco de matar a Nª — e sobra
# margem pra fuso e pro horário em que o Stripe roda o faturamento.
FOLGA_CANCELAMENTO = timedelta(days=1)

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


def _restricao_uso_unico() -> dict:
    """Payment Link é reutilizável; o nosso é de um paciente só.

    Sem isso, o link que a Thainá manda pro João serve pra qualquer pessoa que
    receba a URL encaminhada assinar a mesma cobrança. `completed_sessions` conta
    só checkout CONCLUÍDO — quem abre e desiste continua podendo voltar.
    """
    return {"completed_sessions": {"limit": 1}}


def _metadata_base(nome: str, email: str, tipo: str, paciente_id: str | int | None) -> dict:
    """Metadata comum. `paciente_id` é o elo com o prontuário do Hamilton.

    O painel antigo do site gravava `paciente_id`/`terapeuta_id` e é por eles que
    a contabilidade casa assinatura com prontuário. Como o painel da Sofia passa a
    ser o único, ele precisa continuar gravando — o dado não é recuperável depois.
    `terapeuta_id` não tem equivalente aqui: no cadastro pela Sofia o paciente
    ainda é lead sem terapeuta.
    """
    metadata = {"nome_cliente": nome, "email_cliente": email, "tipo": tipo}
    if paciente_id:
        metadata["paciente_id"] = str(paciente_id)
    return metadata


async def criar_link_neuro(
    nome: str,
    email: str,
    valor_total: float,
    parcelas: int = 1,
    desconto: int = 0,
    paciente_id: str | int | None = None,
) -> dict:
    """Cria o link de cobrança e devolve {link, ref, resumo}.

    `ref` é a referência pra vincular ao paciente: sempre o `plink_...` do
    Payment Link (antes era a URL no 1x e o `cs_...` no parcelado).
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

    # O Stripe trabalha em CENTAVOS, sempre. round() aqui e no preview.
    total_centavos = round(valor_total * (1 - desconto / 100) * 100)
    parcela_centavos = round(total_centavos / parcelas)

    metadata = _metadata_base(nome, email, "neuro", paciente_id)
    metadata["desconto_percentual"] = str(desconto)

    payload: dict = {
        "metadata": metadata,
        "restrictions": _restricao_uso_unico(),
        "after_completion": {
            "type": "redirect",
            "redirect": {"url": f"{settings.base_url}/pagamento-sucesso"},
        },
    }

    if parcelas == 1:
        preco = await stripe_client.criar_preco(
            {
                "unit_amount": total_centavos,
                "currency": "brl",
                "product_data": {"name": f"Avaliação Neuropsicológica - {nome}"},
            }
        )
        payload["line_items"] = [{"price": preco["id"], "quantity": 1}]
    else:
        # "Parcelado" = assinatura mensal do valor da parcela. O fim da linha NÃO
        # é definido aqui: `cancel_at` não existe na criação (nem em Payment Link
        # nem em Checkout Session — a API responde 400 `parameter_unknown`). Quem
        # põe o limite é `limitar_parcelado`, lendo `parcelas_total` do metadata.
        # É por isso que esse campo é obrigatório: sem ele a assinatura vira
        # cobrança perpétua e o reconciliador não tem como adivinhar o N.
        preco = await stripe_client.criar_preco(
            {
                "unit_amount": parcela_centavos,
                "currency": "brl",
                "recurring": {"interval": "month", "interval_count": 1},
                "product_data": {
                    "name": f"Neuro Avaliação Neuropsicológica - {nome} ({parcelas}x)"
                },
            }
        )
        payload["line_items"] = [{"price": preco["id"], "quantity": 1}]
        payload["subscription_data"] = {
            # O paciente vê "R$ 200,00 por mês" e um botão de assinar; nada na
            # tela do Stripe diz que acaba na 5ª. Esta linha é o único lugar em
            # que ele lê o combinado antes de autorizar. Sem data absoluta de
            # propósito: o link é criado antes do pagamento, e o mês final
            # depende de quando a pessoa paga.
            "description": (
                f"{parcelas} cobranças mensais de {fmt_centavos(parcela_centavos)} "
                f"(total {fmt_centavos(total_centavos)}). "
                f"Encerra automaticamente após a {parcelas}ª."
            ),
            "metadata": {
                **metadata,
                "parcelas_total": str(parcelas),
                "valor_total_centavos": str(total_centavos),
            },
        }

    link_obj = await stripe_client.criar_payment_link(payload)

    return {
        "link": link_obj["url"],
        "ref": link_obj["id"],
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
    paciente_id: str | int | None = None,
) -> dict:
    """Assinatura mensal da terapia: valor cheio hoje, valor cheio todo mês.

    `email` é OPCIONAL de propósito. A Sofia nunca coletou e-mail (não está na tool
    `cadastrar_paciente`), e pedir mais um dado por WhatsApp pra pré-preencher um
    campo que o Stripe coleta de qualquer jeito só adiciona atrito. Sem e-mail, o
    Checkout pergunta — e o que a pessoa digita lá é mais confiável que um e-mail
    ditado numa conversa. O painel continua mandando (a Thainá tem o dado), mas
    agora ele só vai pro metadata: Payment Link não tem `customer_email`, e o
    pré-preenchimento não vale um link que expira em 24h.
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

    metadata = _metadata_base(nome, email, "clinica", paciente_id)
    link_obj = await stripe_client.criar_payment_link(
        {
            "line_items": [recorrente],
            "metadata": metadata,
            "restrictions": _restricao_uso_unico(),
            "subscription_data": {
                # Sem `parcelas_total`: é exatamente esse campo que o
                # reconciliador usa pra decidir quem tem fim. A mensalidade da
                # terapia NÃO tem, e não pode ganhar um por engano.
                "description": (
                    f"Mensalidade da terapia na Allos — {fmt_centavos(valor_centavos)} "
                    f"por mês, cobrados automaticamente no mesmo dia de cada mês."
                ),
                "metadata": metadata,
            },
            "after_completion": {
                "type": "redirect",
                "redirect": {"url": f"{settings.base_url}/pagamento-sucesso"},
            },
        }
    )
    return {
        "link": link_obj["url"],
        "ref": link_obj["id"],
        "valor_mensal": fmt_centavos(valor_centavos),
        "valor_entrada": fmt_centavos(valor_centavos),
    }


# ── Reconciliador: o parcelado que precisa acabar ─────────────────────────────
#
# 🔴 O bug que isto conserta: `cancel_at` NÃO é parâmetro de criação. Nem em
# Checkout Session nem em Payment Link — a API responde
# `400 parameter_unknown: Received unknown parameter: subscription_data[cancel_at]`.
# A versão anterior mandava esse campo e "provava" o limite com um teste que
# mockava a chamada ao Stripe: suite verde, feature morta, e no painel do site
# (que nunca mandou nada) 18 assinaturas de parcelado cobrando pra sempre — uma
# delas cobrou 5 parcelas num plano de 4.
#
# Como `cancel_at` só existe em `POST /subscriptions/{id}`, o limite só pode ser
# posto DEPOIS que a pessoa paga. Sem webhook (decisão do projeto), isso vira
# reconciliação por cron. A latência não importa: a cobrança indevida só viria um
# mês depois do checkout, e a rodada é diária — 30 dias de margem.
#
# ⚠️ **O discriminador é `metadata.parcelas_total`, e só ele.** Na conta real ele
# separa com precisão total: 18/18 parcelados de neuro têm, 0/30 mensalidades de
# terapia têm. Confundir os dois aqui cancela a terapia contínua de um paciente
# que está pagando em dia — o erro oposto, e pior. Assinatura que parece neuro e
# não tem o campo NÃO é tocada: vira alerta pra alguém olhar.


def _somar_meses(quando: datetime, meses: int) -> datetime:
    """Soma meses de calendário, ancorando no dia original (como o Stripe faz).

    Sempre a partir da âncora ORIGINAL, nunca iterando mês a mês: 31/01 + 1 mês é
    28/02, mas 31/01 + 2 meses é 31/03, e não 28/03.
    """
    total = quando.month - 1 + meses
    ano, mes = quando.year + total // 12, total % 12 + 1
    return quando.replace(
        year=ano, month=mes, day=min(quando.day, calendar.monthrange(ano, mes)[1])
    )


def _ancora(sub: dict) -> datetime | None:
    marca = sub.get("billing_cycle_anchor") or sub.get("start_date") or sub.get("created")
    return datetime.fromtimestamp(marca, timezone.utc) if marca else None


def plano_de_limite(sub: dict, pagas: int) -> dict | None:
    """O que fazer com esta assinatura. `None` = não mexer.

    Devolve `{acao, quando, motivo}`. `acao` é `cancel_at` (data futura, ainda
    faltam parcelas) ou `nao_renovar` (já pagou tudo: corta na virada do período,
    sem gerar fatura nova).
    """
    metadata = sub.get("metadata") or {}
    total = int(metadata.get("parcelas_total") or 0)
    if total <= 0:
        return None
    if sub.get("cancel_at") or sub.get("cancel_at_period_end"):
        return None  # já tem fim; não sobrescreve o que alguém definiu à mão

    if pagas >= total:
        motivo = f"{pagas} de {total} parcelas pagas"
        if pagas > total:
            motivo += f" — JÁ COBROU {pagas - total} A MAIS"
        return {"acao": "nao_renovar", "quando": None, "motivo": motivo}

    ancora = _ancora(sub)
    if ancora is None:
        return None
    # Fatura 1 sai na âncora, fatura N em âncora+(N-1) meses, fatura N+1 em
    # âncora+N meses. Cortar um dia antes dessa última mata só a indevida.
    corte = _somar_meses(ancora, total) - FOLGA_CANCELAMENTO
    return {
        "acao": "cancel_at",
        "quando": int(corte.timestamp()),
        "motivo": f"{pagas} de {total} parcelas pagas; encerra em {corte:%d/%m/%Y}",
    }


def _parece_neuro(nome_produto: str) -> bool:
    return "neuro" in (nome_produto or "").lower()


async def limitar_parcelado(*, simular: bool = True, limite: int = LIMITE_POR_RODADA) -> dict:
    """Põe o fim da linha nas assinaturas de parcelado que não têm.

    `simular=True` (padrão) não escreve nada: devolve exatamente o que faria. É o
    modo usado pro relatório de conferência antes de mexer em paciente real.
    """
    # `paginas=20`: a varredura tem que ser da conta INTEIRA. Uma assinatura fora
    # da primeira página é um paciente que segue sendo cobrado depois da última
    # parcela — exatamente o que isto conserta.
    ativas = [
        s
        for s in await stripe_client.listar_assinaturas(status="all", limite=100, paginas=20)
        if s.get("status") in ("active", "past_due", "trialing")
    ]
    candidatas = [s for s in ativas if (s.get("metadata") or {}).get("parcelas_total")]

    async def _pagas(sub: dict) -> int:
        try:
            faturas = await stripe_client.listar_faturas(sub["id"], limite=100)
        except StripeError:
            return -1  # não sei contar -> não mexo
        return sum(1 for f in faturas if f.get("status") == "paid")

    contagens = await asyncio.gather(*(_pagas(s) for s in candidatas))

    planejadas: list[dict] = []
    ja_ok = 0
    for sub, pagas in zip(candidatas, contagens):
        if pagas < 0:
            continue
        plano = plano_de_limite(sub, pagas)
        if plano is None:
            ja_ok += 1
            continue
        metadata = sub.get("metadata") or {}
        planejadas.append(
            {
                "id": sub["id"],
                "nome": metadata.get("nome_cliente") or "(sem nome)",
                "parcelas_pagas": pagas,
                "parcelas_total": int(metadata["parcelas_total"]),
                "excedente": max(0, pagas - int(metadata["parcelas_total"])),
                **plano,
            }
        )

    truncado = len(planejadas) > limite
    if truncado:
        # Teto de segurança: nunca some com o resto em silêncio.
        logger.warning(
            "limitar_parcelado: %d assinaturas a ajustar, teto de %d — %d ficaram pra próxima",
            len(planejadas),
            limite,
            len(planejadas) - limite,
        )
        planejadas = planejadas[:limite]

    if not simular:
        for item in planejadas:
            corpo = (
                {"cancel_at": item["quando"]}
                if item["acao"] == "cancel_at"
                else {"cancel_at_period_end": True}
            )
            try:
                await stripe_client.atualizar_assinatura(item["id"], corpo)
                item["aplicado"] = True
                logger.info(
                    "limitar_parcelado: %s -> %s (%s)", item["id"], item["acao"], item["motivo"]
                )
            except StripeError:
                item["aplicado"] = False
                logger.error("limitar_parcelado: falhei em %s", item["id"])

    return {
        "simulado": simular,
        "planejadas": planejadas,
        "ja_limitadas": ja_ok,
        "truncado": truncado,
        "alertas": await _neuro_sem_plano(ativas),
    }


async def _neuro_sem_plano(ativas: list[dict]) -> list[dict]:
    """Assinatura que parece neuro e não diz em quantas parcelas — nunca tocada.

    Existe porque degradar em silêncio já custou uma demanda inteira neste
    projeto. Se o formato do metadata mudar, isto aparece no painel em vez de o
    reconciliador simplesmente parar de enxergar o parcelado.
    """
    suspeitas = [s for s in ativas if not (s.get("metadata") or {}).get("parcelas_total")]
    if not suspeitas:
        return []
    try:
        produtos = await stripe_client.listar_produtos()
    except StripeError:
        return []
    alertas = []
    for sub in suspeitas:
        item = ((sub.get("items") or {}).get("data") or [{}])[0]
        preco = item.get("price") or {}
        if not preco.get("recurring"):
            continue
        nome_produto = produtos.get(preco.get("product"), "")
        if _parece_neuro(nome_produto):
            alertas.append({"id": sub["id"], "produto": nome_produto, "status": sub.get("status")})
    return alertas


# ── Listagem de assinaturas (aba Assinaturas) ─────────────────────────────────


def tipo_da_assinatura(sub: dict) -> str:
    """neuro (parcelado) ou clinica (mensalidade contínua).

    `metadata.tipo` só existe nas criadas pela Sofia — 3 das 51 da conta. Sem o
    fallback por `parcelas_total`, as 18 de neuro do painel antigo apareciam
    rotuladas como "Terapia" e o filtro "Neuro (parcelado)" vinha vazio.
    """
    metadata = sub.get("metadata") or {}
    if metadata.get("tipo") in ("neuro", "clinica"):
        return metadata["tipo"]
    return "neuro" if metadata.get("parcelas_total") else "clinica"


def nome_do_cliente(sub: dict) -> str:
    """Nome do paciente, com a cadeia de fallback do legado.

    `nome_cliente` cobre as 18 do painel antigo de neuro; `customer.name` (que
    exige expandir o cliente) cobre outras 29, incluindo as que vieram do
    Hamilton. Sobram 4 sem nome nenhum em toda a conta.
    """
    metadata = sub.get("metadata") or {}
    cliente = sub.get("customer") if isinstance(sub.get("customer"), dict) else {}
    return (
        metadata.get("nome_cliente")
        or metadata.get("patient_name")
        or (cliente or {}).get("name")
        or "(sem nome)"
    )


async def listar_assinaturas_painel(status: str = "all", tipo: str = "all") -> list[dict]:
    """Assinaturas ao vivo do Stripe, com faturas, no formato do painel.

    N+1 assumido (uma chamada de faturas por assinatura, em paralelo) — ok até
    ~100 assinaturas; acima disso, paginar e carregar faturas sob demanda.
    """
    assinaturas = await stripe_client.listar_assinaturas(
        status=None if status == "all" else status, expand=["data.customer"]
    )

    async def _montar(sub: dict) -> dict | None:
        sub_tipo = tipo_da_assinatura(sub)
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
        total = int(metadata.get("parcelas_total") or 0)
        return {
            "id": sub["id"],
            "nome_cliente": nome_do_cliente(sub),
            "paciente_id": metadata.get("paciente_id") or "",
            "status": sub.get("status"),
            "status_rotulo": STATUS_ASSINATURA.get(sub.get("status"), sub.get("status")),
            "tipo": sub_tipo,
            "valor_parcela": fmt_centavos((item[0].get("price") or {}).get("unit_amount") or 0),
            "parcelas_pagas": len(pagas),
            "parcelas_total": total,
            "parcelas_atrasadas": len(atrasadas),
            # Parcelado sem fim marcado é o bug do `cancel_at`: fica visível na
            # tela em vez de só no log do cron.
            "sem_limite": bool(total)
            and not (sub.get("cancel_at") or sub.get("cancel_at_period_end")),
            "nao_renova": bool(sub.get("cancel_at_period_end")),
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

        # tipo em ("link", "link_url"): payment link — pode ser pagamento único
        # (neuro 1x) OU assinatura (parcelado e mensalidade, desde a troca de
        # Checkout Session por Payment Link). Sem o desvio pra assinatura abaixo,
        # todo parcelado apareceria como "Pago" já na primeira parcela.
        plink_id = valor
        if tipo == "link_url":
            links = await stripe_client.listar_payment_links()
            plink_id = next((pl["id"] for pl in links if pl.get("url") == valor), None)
            if plink_id is None:
                return _estado("nao_encontrado")
        sessions = await stripe_client.listar_sessions_do_payment_link(plink_id)
        assinatura = next((s["subscription"] for s in sessions if s.get("subscription")), None)
        if assinatura:
            return await _status_da_assinatura(assinatura)
        if any(s.get("payment_status") == "paid" for s in sessions):
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
