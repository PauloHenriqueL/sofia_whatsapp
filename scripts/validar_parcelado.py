"""Prova, contra o Stripe de verdade, que o parcelado do neuro PARA na última.

Roda inteiro no **modo de teste** (`TEST_STRIPE_SECRET_KEY` no `.env`) e usa
*test clock*: cria a assinatura, avança o relógio mês a mês e confere quantas
faturas saíram. É a única forma de verificar o comportamento do 6º mês sem
esperar seis meses — e sem isso o limite continua sendo teoria.

    python scripts/validar_parcelado.py [--parcelas 3]

Por que existe: a versão anterior mandava `subscription_data[cancel_at]`, que o
Stripe **não aceita** (400 `parameter_unknown`), e o teste unitário "provava" que
funcionava porque mockava a chamada. Suite verde, feature morta, 18 assinaturas
cobrando pra sempre na conta real. Teste com mock não pega contrato de API
quebrado; só chamada real pega.

Não roda no pytest de propósito: fala com a rede, demora minutos e depende de
credencial. É ferramenta de conferência, para rodar à mão quando mexer aqui.
"""

import argparse
import asyncio
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402
from app.services import pagamentos, stripe_client  # noqa: E402

VERDE, VERMELHO, FIM = "\033[92m", "\033[91m", "\033[0m"


def _http(metodo: str, caminho: str, dados: dict | None = None) -> dict:
    """Chamada crua — só pros helpers de teste, que o cliente da app não expõe."""
    corpo = urllib.parse.urlencode(stripe_client._achatar(dados)).encode() if dados else None
    pedido = urllib.request.Request(
        "https://api.stripe.com/v1" + caminho,
        data=corpo,
        headers={
            "Authorization": f"Bearer {settings.stripe_secret_key}",
            "Stripe-Version": stripe_client.API_VERSION,
        },
        method=metodo,
    )
    try:
        with urllib.request.urlopen(pedido, timeout=60) as resposta:
            return json.load(resposta)
    except urllib.error.HTTPError as erro:
        detalhe = json.load(erro)
        raise SystemExit(f"Stripe {erro.code}: {detalhe.get('error', {}).get('message')}") from erro


def _avancar(relogio: str, ate: int) -> None:
    _http("POST", f"/test_helpers/test_clocks/{relogio}/advance", {"frozen_time": ate})
    for _ in range(120):
        if _http("GET", f"/test_helpers/test_clocks/{relogio}")["status"] == "ready":
            return
        time.sleep(2)
    raise SystemExit("test clock não ficou pronto")


def _faturas_pagas(assinatura: str) -> int:
    faturas = _http("GET", f"/invoices?subscription={assinatura}&limit=100")["data"]
    return sum(1 for f in faturas if f.get("status") == "paid")


async def principal(parcelas: int) -> int:
    if not settings.test_stripe_secret_key.startswith("sk_test_"):
        raise SystemExit("TEST_STRIPE_SECRET_KEY ausente ou não é chave de teste.")
    # A app inteira lê `stripe_secret_key`; apontar pra chave de teste faz as
    # funções DE PRODUÇÃO rodarem contra a conta de teste. É esse o ponto: o que
    # está sendo validado é o código que vai pro ar, não uma cópia dele.
    settings.stripe_secret_key = settings.test_stripe_secret_key
    print(f"modo de teste · plano de {parcelas}x\n")

    print("1. gerando o link pelo código de produção (criar_link_neuro)")
    link = await pagamentos.criar_link_neuro(
        "Paciente Teste", "teste@example.com", 1200, parcelas=parcelas, paciente_id=9999
    )
    print(f"   url: {link['link']}  ({len(link['link'])} chars)")
    print(f"   ref: {link['ref']}")
    detalhe = _http("GET", f"/payment_links/{link['ref']}")
    itens = _http("GET", f"/payment_links/{link['ref']}/line_items?limit=1")["data"]
    preco = itens[0]["price"]
    metadata = detalhe["subscription_data"]["metadata"]
    print(f"   descrição no checkout: {detalhe['subscription_data']['description']}")
    print(f"   uso único: {detalhe['restrictions']}")
    print(f"   parcelas_total no metadata: {metadata.get('parcelas_total')}")

    print("\n2. simulando o pagamento (test clock + cartão de teste)")
    agora = int(time.time())
    relogio = _http("POST", "/test_helpers/test_clocks", {"frozen_time": agora})["id"]
    cliente = _http(
        "POST", "/customers", {"name": "Paciente Teste", "email": "t@x.com", "test_clock": relogio}
    )["id"]
    metodo = _http("POST", "/payment_methods", {"type": "card", "card": {"token": "tok_visa"}})[
        "id"
    ]
    _http("POST", f"/payment_methods/{metodo}/attach", {"customer": cliente})
    _http(
        "POST",
        f"/customers/{cliente}",
        {"invoice_settings": {"default_payment_method": metodo}},
    )
    assinatura = _http(
        "POST",
        "/subscriptions",
        {
            "customer": cliente,
            "items": [{"price": preco["id"]}],
            "metadata": metadata,  # idêntico ao que o Payment Link grava
        },
    )
    print(f"   assinatura {assinatura['id']} · cancel_at={assinatura.get('cancel_at')}")

    # A mensalidade da terapia entra na mesma conta, pra provar que o
    # reconciliador NÃO encosta nela — é o erro caro (cancelar quem paga em dia).
    print("\n3. criando também uma mensalidade de terapia (controle)")
    mensal = await pagamentos.criar_assinatura_mensalidade(nome="Controle", valor_mensal=200)
    itens_m = _http("GET", f"/payment_links/{mensal['ref']}/line_items?limit=1")["data"]
    controle = _http(
        "POST",
        "/subscriptions",
        {
            "customer": cliente,
            "items": [{"price": itens_m[0]["price"]["id"]}],
            "metadata": {"tipo": "clinica", "nome_cliente": "Controle"},
        },
    )["id"]
    print(f"   assinatura de controle {controle}")

    # ⚠️ Assinatura amarrada a test clock NÃO aparece em `GET /subscriptions` —
    # o Stripe esconde objetos de relógio das listagens. Então aqui roda o miolo
    # do reconciliador (`plano_de_limite` + `atualizar_assinatura`), que é o que
    # decide e escreve; a varredura/filtro da conta é lógica pura sobre dicts e
    # está coberta em tests/test_pagamentos.py, sem precisar de rede.
    print("\n4. rodando o miolo do reconciliador")
    sub_atual = _http("GET", f"/subscriptions/{assinatura['id']}")
    plano = pagamentos.plano_de_limite(sub_atual, _faturas_pagas(assinatura["id"]))
    if plano is None:
        print(f"{VERMELHO}   FALHOU: o reconciliador não viu nada a fazer{FIM}")
        return 1
    print(f"   decisão: {plano['acao']} — {plano['motivo']}")

    sub_controle = _http("GET", f"/subscriptions/{controle}")
    if pagamentos.plano_de_limite(sub_controle, _faturas_pagas(controle)) is not None:
        print(f"{VERMELHO}   FALHOU: o reconciliador quis mexer na mensalidade da terapia{FIM}")
        return 1
    print("   mensalidade da terapia: NAO tocada, como tem que ser")

    await stripe_client.atualizar_assinatura(assinatura["id"], {"cancel_at": plano["quando"]})
    depois = _http("GET", f"/subscriptions/{assinatura['id']}")
    if not depois.get("cancel_at"):
        print(f"{VERMELHO}   FALHOU: a assinatura do parcelado continua sem fim{FIM}")
        return 1
    print(f"   cancel_at gravado: {time.strftime('%d/%m/%Y', time.localtime(depois['cancel_at']))}")

    print(f"\n5. avançando o relógio {parcelas + 2} meses")
    for mes in range(1, parcelas + 3):
        _avancar(relogio, agora + int(mes * 30.5 * 86400))
        pagas = _faturas_pagas(assinatura["id"])
        estado = _http("GET", f"/subscriptions/{assinatura['id']}")["status"]
        print(f"   mês {mes}: {pagas} fatura(s) paga(s) · assinatura {estado}")

    pagas = _faturas_pagas(assinatura["id"])
    estado = _http("GET", f"/subscriptions/{assinatura['id']}")["status"]
    controle_estado = _http("GET", f"/subscriptions/{controle}")["status"]
    controle_pagas = _faturas_pagas(controle)

    print("\n" + "=" * 62)
    ok = pagas == parcelas and estado == "canceled" and controle_estado == "active"
    cor = VERDE if ok else VERMELHO
    print(f"{cor}parcelado : {pagas} cobranças de um plano de {parcelas} · {estado}{FIM}")
    print(f"{cor}terapia   : {controle_pagas} cobranças · {controle_estado} (tem que seguir){FIM}")
    print(f"{cor}{'PASSOU' if ok else 'FALHOU'}{FIM}")
    return 0 if ok else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--parcelas", type=int, default=3)
    sys.exit(asyncio.run(principal(parser.parse_args().parcelas)))
