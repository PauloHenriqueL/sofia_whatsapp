"""Testes dos pagamentos (Stripe): links, assinatura dia 10, vínculo e status."""

from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.database import Base, get_db
from app.main import app
from app.models import Conversa, LinkCurto
from app.services import links, pagamentos, stripe_client
from app.services.pagamentos import ErroValidacao
from app.services.stripe_client import StripeError, _achatar

AGORA = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)


@contextmanager
def _chaves_stripe(valor: str):
    """Fixa AS DUAS chaves do Stripe.

    As duas, e não só a live: o que a app lê é `settings.stripe_key`, que fora de
    `production` devolve a de TESTE. Mexer só na live faria o `stripe_desligado`
    não desligar nada na máquina de quem tem `TEST_STRIPE_SECRET_KEY` no `.env` —
    e o teste passaria ou falharia conforme o `.env` de cada um.
    """
    antes = (settings.stripe_secret_key, settings.test_stripe_secret_key)
    settings.stripe_secret_key = valor
    settings.test_stripe_secret_key = valor
    try:
        yield
    finally:
        settings.stripe_secret_key, settings.test_stripe_secret_key = antes


@pytest.fixture
def stripe_ligado():
    """Liga o Stripe nos testes (a chave dummy nunca é usada: o client é mockado)."""
    with _chaves_stripe("sk_test_dummy"):
        yield


@pytest.fixture
def stripe_desligado():
    """Desliga o Stripe explicitamente.

    Sem isto o teste dependeria do .env de quem roda: quem tem chave preenchida
    (o .env copiado do Render) via o teste falhar.
    """
    with _chaves_stripe(""):
        yield


class TestChaveDoAmbiente:
    """A app NUNCA pode falar com a conta live fora de produção.

    O Stripe não tem dry-run: toda chamada cria coisa de verdade. E o `.env` de
    desenvolvimento carrega uma chave `sk_live_`, porque é cópia do Render. Sem
    esta trava, rodar a app no laptop cria Payment Link e assinatura na conta da
    Allos — o que já aconteceu duas vezes, a última em 17/08.
    """

    @contextmanager
    def _ambiente(self, environment: str, live: str, teste: str):
        antes = (settings.environment, settings.stripe_secret_key, settings.test_stripe_secret_key)
        settings.environment = environment
        settings.stripe_secret_key = live
        settings.test_stripe_secret_key = teste
        try:
            yield
        finally:
            (
                settings.environment,
                settings.stripe_secret_key,
                settings.test_stripe_secret_key,
            ) = antes

    def test_fora_de_producao_usa_a_chave_de_teste(self):
        with self._ambiente("development", "sk_live_REAL", "sk_test_FAKE"):
            assert settings.stripe_key == "sk_test_FAKE"
            assert settings.stripe_modo_teste is True

    def test_em_producao_usa_a_chave_live(self):
        with self._ambiente("production", "sk_live_REAL", "sk_test_FAKE"):
            assert settings.stripe_key == "sk_live_REAL"
            assert settings.stripe_modo_teste is False

    def test_sem_chave_de_teste_o_stripe_fica_DESLIGADO(self):
        """O ponto do exercício: nunca, jamais, cair pra live."""
        with self._ambiente("development", "sk_live_REAL", ""):
            assert settings.stripe_key == ""
            assert stripe_client.configurado() is False

    @pytest.mark.parametrize("ambiente", ["development", "dev", "", "PRODUCTION_"])
    def test_qualquer_coisa_que_nao_seja_production_e_teste(self, ambiente):
        with self._ambiente(ambiente, "sk_live_REAL", "sk_test_FAKE"):
            assert settings.stripe_key == "sk_test_FAKE"

    @pytest.mark.asyncio
    async def test_a_requisicao_manda_a_chave_do_ambiente(self):
        """Não basta a propriedade estar certa: é ela que tem que ir no header."""
        capturado = {}

        class _RespostaFalsa:
            status_code = 200

            def json(self):
                return {"id": "x"}

        class _ClienteFalso:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return False

            async def request(self, metodo, url, **kwargs):
                capturado.update(kwargs.get("headers") or {})
                return _RespostaFalsa()

        with self._ambiente("development", "sk_live_REAL", "sk_test_FAKE"):
            with patch("app.services.stripe_client.httpx.AsyncClient", _ClienteFalso):
                await stripe_client._requisicao("GET", "/v1/payment_links")

        assert capturado["Authorization"] == "Bearer sk_test_FAKE"
        assert "sk_live_REAL" not in capturado["Authorization"]


class TestAchatar:
    def test_notacao_de_colchetes_do_stripe(self):
        plano = _achatar({"a": {"b": 1}, "c": [{"d": 2}], "e": True, "f": None, "g": "x"})
        assert plano == {"a[b]": 1, "c[0][d]": 2, "e": "true", "g": "x"}


class TestInterpretarReferencia:
    @pytest.mark.parametrize(
        "texto,tipo,valor",
        [
            ("sub_1AbC23", "assinatura", "sub_1AbC23"),
            ("  cs_test_a1B2_c3  ", "checkout", "cs_test_a1B2_c3"),
            ("cus_XyZ987", "cliente", "cus_XyZ987"),
            ("plink_1AbC", "link", "plink_1AbC"),
            (
                "https://buy.stripe.com/test_abc123",
                "link_url",
                "https://buy.stripe.com/test_abc123",
            ),
            ("https://checkout.stripe.com/c/pay/cs_test_a1B2#xyz", "checkout", "cs_test_a1B2"),
        ],
    )
    def test_aceita_os_quatro_formatos(self, texto, tipo, valor):
        assert pagamentos.interpretar_referencia(texto) == (tipo, valor)

    @pytest.mark.parametrize(
        "lixo", ["", "pix_123", "https://evil.com/cs_x", "sub 123", "qualquer"]
    )
    def test_rejeita_o_resto(self, lixo):
        with pytest.raises(ErroValidacao):
            pagamentos.interpretar_referencia(lixo)


class TestCriarLinkNeuro:
    @pytest.mark.asyncio
    async def test_1x_vira_payment_link(self):
        preco = AsyncMock(return_value={"id": "price_1"})
        plink = AsyncMock(return_value={"id": "plink_1", "url": "https://buy.stripe.com/x"})
        with patch("app.services.pagamentos.stripe_client.criar_preco", preco), patch(
            "app.services.pagamentos.stripe_client.criar_payment_link", plink
        ):
            r = await pagamentos.criar_link_neuro("Maria", "m@x.com", 1200, parcelas=1)

        assert r["link"] == "https://buy.stripe.com/x"
        assert r["ref"] == "plink_1"  # o id do link é a referência do vínculo
        assert preco.await_args.args[0]["unit_amount"] == 120000  # centavos
        assert "recurring" not in preco.await_args.args[0]
        assert r["resumo"]["valor_total"] == "R$ 1.200,00"
        assert plink.await_args.args[0]["restrictions"]["completed_sessions"]["limit"] == 1

    @pytest.mark.asyncio
    async def test_parcelado_vira_assinatura_mensal_com_plano_no_metadata(self):
        """3x = assinatura mensal da parcela, e o N vai no metadata.

        `cancel_at` NÃO é mandado aqui: o Stripe responde 400 `parameter_unknown`
        na criação (foi esse o bug — o teste antigo mockava a chamada e "provava"
        um parâmetro que a API nunca aceitou). Quem encerra é `limitar_parcelado`,
        e o único jeito de ele saber o N é `parcelas_total`.
        """
        preco = AsyncMock(return_value={"id": "price_1"})
        plink = AsyncMock(return_value={"id": "plink_1", "url": "https://buy.stripe.com/y"})
        with patch("app.services.pagamentos.stripe_client.criar_preco", preco), patch(
            "app.services.pagamentos.stripe_client.criar_payment_link", plink
        ):
            r = await pagamentos.criar_link_neuro("Maria", "m@x.com", 1200, parcelas=3)

        assert r["ref"] == "plink_1"
        assert preco.await_args.args[0]["unit_amount"] == 40000  # 1200/3 em centavos
        assert preco.await_args.args[0]["recurring"]["interval"] == "month"
        dados = plink.await_args.args[0]
        assert "cancel_at" not in dados["subscription_data"]
        assert dados["subscription_data"]["metadata"]["parcelas_total"] == "3"
        # O paciente vê "R$ 400,00 por mês" no checkout; esta linha é o único
        # lugar onde ele lê que são três e que acaba.
        assert "3 cobranças mensais" in dados["subscription_data"]["description"]
        assert "após a 3ª" in dados["subscription_data"]["description"]

    @pytest.mark.asyncio
    async def test_parcelado_grava_paciente_do_hamilton(self):
        preco = AsyncMock(return_value={"id": "price_1"})
        plink = AsyncMock(return_value={"id": "plink_1", "url": "u"})
        with patch("app.services.pagamentos.stripe_client.criar_preco", preco), patch(
            "app.services.pagamentos.stripe_client.criar_payment_link", plink
        ):
            await pagamentos.criar_link_neuro(
                "Maria", "m@x.com", 1200, parcelas=3, paciente_id=4321
            )
        assert plink.await_args.args[0]["metadata"]["paciente_id"] == "4321"

    @pytest.mark.asyncio
    async def test_desconto_aplicado_com_round(self):
        preco = AsyncMock(return_value={"id": "price_1"})
        plink = AsyncMock(return_value={"id": "plink_1", "url": "u"})
        with patch("app.services.pagamentos.stripe_client.criar_preco", preco), patch(
            "app.services.pagamentos.stripe_client.criar_payment_link", plink
        ):
            r = await pagamentos.criar_link_neuro("Maria", "m@x.com", 1000, desconto=10)
        assert preco.await_args.args[0]["unit_amount"] == 90000
        assert r["resumo"]["desconto"] == "10%"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "kwargs",
        [
            {"nome": "", "email": "m@x.com", "valor_total": 100},
            {"nome": "Maria", "email": "sem-arroba", "valor_total": 100},
            {"nome": "Maria", "email": "m@x.com", "valor_total": 2},  # < R$ 5
            {"nome": "Maria", "email": "m@x.com", "valor_total": 9999},  # > R$ 5.000
            {"nome": "Maria", "email": "m@x.com", "valor_total": 100, "parcelas": 7},
            {"nome": "Maria", "email": "m@x.com", "valor_total": 100, "desconto": 40},
        ],
    )
    async def test_validacoes(self, kwargs):
        with pytest.raises(ErroValidacao):
            await pagamentos.criar_link_neuro(**kwargs)


class TestAssinaturaMensalidade:
    """Assinatura mensal simples: valor cheio hoje, valor cheio todo mês.

    Antes o painel tinha uma versão própria (`criar_assinatura_terapia`) com
    pro-rata ancorado no dia 10 — quem assinava dia 9 pagava R$ 6,67 e levava
    R$ 200 no dia seguinte. Foi removida: o painel e a Sofia usam a MESMA função,
    senão o mesmo paciente pagaria valores diferentes conforme quem gerou o link.
    """

    @pytest.mark.asyncio
    async def test_sem_ancora_sem_pro_rata_sem_trial(self):
        plink = AsyncMock(return_value={"id": "plink_t1", "url": "https://buy.stripe.com/2"})
        with patch("app.services.pagamentos.stripe_client.criar_payment_link", plink):
            r = await pagamentos.criar_assinatura_mensalidade(
                nome="Ana", valor_mensal=200, email="a@x.com"
            )

        dados = plink.await_args.args[0]
        sub = dados["subscription_data"]
        assert "billing_cycle_anchor" not in sub
        assert "proration_behavior" not in sub
        assert "trial_end" not in sub  # é o que faria o checkout dizer "avaliação gratuita"
        assert len(dados["line_items"]) == 1
        assert r["valor_entrada"] == "R$ 200,00"
        assert r["valor_mensal"] == "R$ 200,00"
        assert r["ref"] == "plink_t1"

    @pytest.mark.asyncio
    async def test_mensalidade_nunca_ganha_plano_de_parcelas(self):
        """`parcelas_total` na terapia faria o reconciliador cancelar quem paga em dia.

        É o erro oposto ao do parcelado e o mais caro dos dois: a mensalidade da
        terapia é contínua por definição.
        """
        plink = AsyncMock(return_value={"id": "plink_t2", "url": "u"})
        with patch("app.services.pagamentos.stripe_client.criar_payment_link", plink):
            await pagamentos.criar_assinatura_mensalidade(nome="Ana", valor_mensal=200)
        dados = plink.await_args.args[0]
        assert "parcelas_total" not in dados["subscription_data"]["metadata"]
        assert "parcelas_total" not in dados["metadata"]
        assert dados["subscription_data"]["metadata"]["tipo"] == "clinica"
        assert pagamentos.tipo_da_assinatura({"metadata": dados["metadata"]}) == "clinica"

    @pytest.mark.asyncio
    async def test_email_e_opcional(self):
        """Payment Link não tem `customer_email`; o checkout pergunta."""
        plink = AsyncMock(return_value={"id": "plink_t0", "url": "u"})
        with patch("app.services.pagamentos.stripe_client.criar_payment_link", plink):
            await pagamentos.criar_assinatura_mensalidade(nome="Ana", valor_mensal=200)
        assert "customer_email" not in plink.await_args.args[0]

    @pytest.mark.asyncio
    async def test_email_invalido_e_rejeitado(self):
        with pytest.raises(ErroValidacao):
            await pagamentos.criar_assinatura_mensalidade(
                nome="Ana", valor_mensal=200, email="nao-e-email"
            )

    @pytest.mark.asyncio
    async def test_valor_fora_da_faixa(self):
        with pytest.raises(ErroValidacao):
            await pagamentos.criar_assinatura_mensalidade(nome="Ana", valor_mensal=10)

    @pytest.mark.asyncio
    async def test_reusa_preco_do_catalogo_quando_o_valor_bate(self):
        """STRIPE_PRECO_MENSAL_ID + mensalidade igual -> line item com o price
        do catálogo (relatórios unificados com o site da Allos)."""
        original = settings.stripe_preco_mensal_id
        settings.stripe_preco_mensal_id = "price_catalogo"
        preco = AsyncMock(return_value={"id": "price_catalogo", "unit_amount": 20000})
        sessao = AsyncMock(return_value={"id": "plink_t3", "url": "u"})
        try:
            with patch("app.services.pagamentos.stripe_client.obter_preco", preco), patch(
                "app.services.pagamentos.stripe_client.criar_payment_link", sessao
            ):
                await pagamentos.criar_assinatura_mensalidade(nome="Ana", valor_mensal=200)
        finally:
            settings.stripe_preco_mensal_id = original
        item = sessao.await_args.args[0]["line_items"][0]
        assert item == {"price": "price_catalogo", "quantity": 1}

    @pytest.mark.asyncio
    async def test_valor_diferente_do_catalogo_cai_pro_preco_inline(self):
        original = settings.stripe_preco_mensal_id
        settings.stripe_preco_mensal_id = "price_catalogo"
        preco = AsyncMock(return_value={"id": "price_catalogo", "unit_amount": 20000})
        sessao = AsyncMock(return_value={"id": "plink_t4", "url": "u"})
        try:
            with patch("app.services.pagamentos.stripe_client.obter_preco", preco), patch(
                "app.services.pagamentos.stripe_client.criar_payment_link", sessao
            ):
                await pagamentos.criar_assinatura_mensalidade(nome="Ana", valor_mensal=150)
        finally:
            settings.stripe_preco_mensal_id = original
        item = sessao.await_args.args[0]["line_items"][0]
        assert item["price_data"]["unit_amount"] == 15000  # bolsa/ajuste: preço inline

    @pytest.mark.asyncio
    async def test_catalogo_inacessivel_nao_impede_a_assinatura(self):
        original = settings.stripe_preco_mensal_id
        settings.stripe_preco_mensal_id = "price_catalogo"
        preco = AsyncMock(side_effect=StripeError("down"))
        sessao = AsyncMock(return_value={"id": "plink_t5", "url": "u"})
        try:
            with patch("app.services.pagamentos.stripe_client.obter_preco", preco), patch(
                "app.services.pagamentos.stripe_client.criar_payment_link", sessao
            ):
                r = await pagamentos.criar_assinatura_mensalidade(nome="Ana", valor_mensal=200)
        finally:
            settings.stripe_preco_mensal_id = original
        assert r["ref"] == "plink_t5"
        assert "price_data" in sessao.await_args.args[0]["line_items"][0]


class TestUrlDeRetorno:
    @pytest.mark.asyncio
    async def test_sucesso_volta_pra_pagina_da_sofia(self):
        """Payment Link tem `after_completion`, não `success_url`/`cancel_url`.

        Não existe fluxo de cancelamento num Payment Link — quem desiste fecha a
        aba. A rota /pagamento-cancelado continua de pé só pros links de checkout
        antigos, que ainda podem estar no WhatsApp de alguém.
        """
        plink = AsyncMock(return_value={"id": "plink_y", "url": "u"})
        with patch("app.services.pagamentos.stripe_client.criar_payment_link", plink):
            await pagamentos.criar_assinatura_mensalidade(nome="Ana", valor_mensal=200)
        dados = plink.await_args.args[0]
        assert dados["after_completion"]["redirect"]["url"] == (
            f"{settings.base_url}/pagamento-sucesso"
        )
        assert "cancel_url" not in dados


class TestStatusDaReferencia:
    @pytest.mark.asyncio
    async def test_assinatura_ativa(self):
        sub = AsyncMock(return_value={"status": "active", "metadata": {}})
        faturas = AsyncMock(return_value=[{"status": "paid"}, {"status": "open"}])
        with patch("app.services.pagamentos.stripe_client.obter_assinatura", sub), patch(
            "app.services.pagamentos.stripe_client.listar_faturas", faturas
        ):
            st = await pagamentos.status_da_referencia("sub_1")
        assert st["estado"] == "ativa"
        assert "1 mensalidade" in st["detalhe"]

    @pytest.mark.asyncio
    async def test_parcelado_cancelado_apos_quitar_e_pago(self):
        sub = AsyncMock(return_value={"status": "canceled", "metadata": {"parcelas_total": "3"}})
        faturas = AsyncMock(return_value=[{"status": "paid"}] * 3)
        with patch("app.services.pagamentos.stripe_client.obter_assinatura", sub), patch(
            "app.services.pagamentos.stripe_client.listar_faturas", faturas
        ):
            st = await pagamentos.status_da_referencia("sub_1")
        assert st["estado"] == "pago"

    @pytest.mark.asyncio
    async def test_checkout_pago_sem_assinatura(self):
        cs = AsyncMock(
            return_value={"subscription": None, "payment_status": "paid", "status": "complete"}
        )
        with patch("app.services.pagamentos.stripe_client.obter_checkout_session", cs):
            st = await pagamentos.status_da_referencia("cs_test_1")
        assert st["estado"] == "pago"

    @pytest.mark.asyncio
    async def test_cliente_sem_assinatura(self):
        subs = AsyncMock(return_value=[])
        with patch("app.services.pagamentos.stripe_client.listar_assinaturas", subs):
            st = await pagamentos.status_da_referencia("cus_1")
        assert st["estado"] == "sem_assinatura"

    @pytest.mark.asyncio
    async def test_url_de_link_pago(self):
        links = AsyncMock(return_value=[{"id": "plink_1", "url": "https://buy.stripe.com/a"}])
        sessions = AsyncMock(return_value=[{"payment_status": "paid"}])
        with patch("app.services.pagamentos.stripe_client.listar_payment_links", links), patch(
            "app.services.pagamentos.stripe_client.listar_sessions_do_payment_link", sessions
        ):
            st = await pagamentos.status_da_referencia("https://buy.stripe.com/a")
        assert st["estado"] == "pago"

    @pytest.mark.asyncio
    async def test_stripe_fora_do_ar_vira_estado_erro(self):
        sub = AsyncMock(side_effect=StripeError("down"))
        with patch("app.services.pagamentos.stripe_client.obter_assinatura", sub):
            st = await pagamentos.status_da_referencia("sub_1")
        assert st["estado"] == "erro"

    @pytest.mark.asyncio
    async def test_referencia_invalida_nunca_explode(self):
        st = await pagamentos.status_da_referencia("lixo")
        assert st["estado"] == "nao_encontrado"


class TestAnotarPagamentos:
    @pytest.mark.asyncio
    async def test_anota_so_quem_tem_ref(self, stripe_ligado):
        itens = [{"stripe_ref": "sub_1"}, {"stripe_ref": None}]
        status = AsyncMock(return_value={"estado": "ativa", "rotulo": "x", "detalhe": ""})
        with patch("app.services.pagamentos.status_da_referencia", status):
            await pagamentos.anotar_pagamentos(itens)
        assert itens[0]["pagamento"]["estado"] == "ativa"
        assert "pagamento" not in itens[1]

    @pytest.mark.asyncio
    async def test_sem_chave_nao_faz_nada(self, stripe_desligado):
        itens = [{"stripe_ref": "sub_1"}]
        await pagamentos.anotar_pagamentos(itens)
        assert "pagamento" not in itens[0]


# ── Rotas do painel ───────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def ambiente():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _get_db_override():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_db] = _get_db_override
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, maker
    app.dependency_overrides.clear()
    await engine.dispose()


async def _login(client):
    resp = await client.post(
        "/login", data={"usuario": settings.painel_user, "senha": settings.painel_password}
    )
    assert resp.status_code == 303


async def _seed_conversa(maker, numero="5531999998888"):
    async with maker() as s:
        c = Conversa(numero_whatsapp=numero, estado="novo")
        s.add(c)
        await s.commit()
        return c.id


class TestPaginaPagamentos:
    @pytest.mark.asyncio
    async def test_exige_login(self, ambiente):
        client, _ = ambiente
        resp = await client.get("/painel/pagamentos/")
        assert resp.status_code == 303
        assert resp.headers["location"] == "/login"

    @pytest.mark.asyncio
    async def test_sem_chave_mostra_aviso(self, ambiente, stripe_desligado):
        client, _ = ambiente
        await _login(client)
        html = (await client.get("/painel/pagamentos/")).text
        assert "STRIPE_SECRET_KEY" in html

    @pytest.mark.asyncio
    @pytest.mark.parametrize("aba", ["gerar", "terapia"])
    async def test_abas_renderizam(self, ambiente, stripe_ligado, aba):
        client, _ = ambiente
        await _login(client)
        resp = await client.get(f"/painel/pagamentos/?aba={aba}")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_aba_assinaturas_lista_do_stripe(self, ambiente, stripe_ligado):
        client, _ = ambiente
        await _login(client)
        listagem = AsyncMock(
            return_value=[
                {
                    "id": "sub_1",
                    "nome_cliente": "Maria Teste",
                    "status": "active",
                    "status_rotulo": "Ativa",
                    "tipo": "clinica",
                    "valor_parcela": "R$ 200,00",
                    "parcelas_pagas": 2,
                    "parcelas_total": 0,
                    "parcelas_atrasadas": 0,
                    "criado_em": 1750000000,
                    "cancela_em": None,
                    "faturas": [],
                }
            ]
        )
        with patch("app.routers.pagamentos.pagamentos.listar_assinaturas_painel", listagem):
            html = (await client.get("/painel/pagamentos/?aba=assinaturas")).text
        assert "Maria Teste" in html
        assert "Ativa" in html

    @pytest.mark.asyncio
    async def test_stripe_fora_do_ar_mostra_aviso_na_listagem(self, ambiente, stripe_ligado):
        client, _ = ambiente
        await _login(client)
        listagem = AsyncMock(side_effect=StripeError("down"))
        with patch("app.routers.pagamentos.pagamentos.listar_assinaturas_painel", listagem):
            resp = await client.get("/painel/pagamentos/?aba=assinaturas")
        assert resp.status_code == 200
        assert "Não consegui falar com o Stripe" in resp.text

    @pytest.mark.asyncio
    async def test_criar_link_vincula_a_conversa(self, ambiente, stripe_ligado):
        client, maker = ambiente
        await _login(client)
        cid = await _seed_conversa(maker)
        criar = AsyncMock(
            return_value={
                "link": "https://buy.stripe.com/x",
                "ref": "plink_1",
                "resumo": {
                    "valor_total": "R$ 100,00",
                    "parcelas": 1,
                    "valor_parcela": "R$ 100,00",
                    "desconto": "0",
                },
            }
        )
        with patch("app.routers.pagamentos.pagamentos.criar_link_neuro", criar):
            resp = await client.post(
                "/painel/pagamentos/criar-link",
                data={
                    "nome": "Maria",
                    "email": "m@x.com",
                    "valor_total": "100",
                    "parcelas": "1",
                    "desconto": "0",
                    "conversa_id": str(cid),
                },
            )
        assert resp.status_code == 200
        # O link em destaque é o CURTO (é o que a Thainá copia pro WhatsApp);
        # encurtar e continuar exibindo o original seria trabalho à toa. O do
        # Stripe fica só no plano B recolhido, pro caso de o site sair do ar.
        async with maker() as s:
            curto = (await s.execute(select(LinkCurto))).scalars().one()
        assert f'id="link-gerado">{links.url_de(curto.slug)}<' in resp.text
        assert "Link direto do Stripe" in resp.text
        async with maker() as s:
            assert (await s.get(Conversa, cid)).stripe_ref == "plink_1"
            curto = (await s.execute(select(LinkCurto))).scalars().all()
            assert [c.destino for c in curto] == ["https://buy.stripe.com/x"]

    @pytest.mark.asyncio
    async def test_validacao_reaparece_no_form(self, ambiente, stripe_ligado):
        client, _ = ambiente
        await _login(client)
        resp = await client.post(
            "/painel/pagamentos/criar-link",
            data={"nome": "Maria", "email": "sem-arroba", "valor_total": "100"},
        )
        assert resp.status_code == 200
        assert "E-mail inválido" in resp.text


class TestVincularNaConversa:
    @pytest.mark.asyncio
    async def test_salva_e_limpa_referencia(self, ambiente):
        client, maker = ambiente
        await _login(client)
        cid = await _seed_conversa(maker)

        resp = await client.post(f"/painel/pagamentos/vincular/{cid}", data={"ref": "sub_123"})
        assert resp.status_code == 303
        async with maker() as s:
            assert (await s.get(Conversa, cid)).stripe_ref == "sub_123"

        await client.post(f"/painel/pagamentos/vincular/{cid}", data={"ref": ""})
        async with maker() as s:
            assert (await s.get(Conversa, cid)).stripe_ref is None

    @pytest.mark.asyncio
    async def test_referencia_invalida_nao_salva(self, ambiente):
        client, maker = ambiente
        await _login(client)
        cid = await _seed_conversa(maker)
        resp = await client.post(f"/painel/pagamentos/vincular/{cid}", data={"ref": "lixo"})
        assert resp.headers["location"].endswith("?pagamento=invalido")
        async with maker() as s:
            assert (await s.get(Conversa, cid)).stripe_ref is None

    @pytest.mark.asyncio
    async def test_conversa_inexistente_da_404(self, ambiente):
        client, _ = ambiente
        await _login(client)
        resp = await client.post("/painel/pagamentos/vincular/99999", data={"ref": "sub_1"})
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_conversa_mostra_status_do_pagamento(self, ambiente, stripe_ligado):
        client, maker = ambiente
        await _login(client)
        cid = await _seed_conversa(maker)
        async with maker() as s:
            (await s.get(Conversa, cid)).stripe_ref = "sub_123"
            await s.commit()
        status = AsyncMock(
            return_value={"estado": "ativa", "rotulo": "Assinatura ativa", "detalhe": ""}
        )
        with patch("app.services.pagamentos.status_da_referencia", status):
            html = (await client.get(f"/painel/conversas/{cid}/")).text
        assert "Assinatura ativa" in html


class TestPaginasPublicas:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "url,trecho",
        [
            ("/pagamento-sucesso", "Pagamento efetuado"),
            ("/pagamento-cancelado", "Nenhum valor foi cobrado"),
        ],
    )
    async def test_abrem_sem_login(self, ambiente, url, trecho):
        client, _ = ambiente
        resp = await client.get(url)
        assert resp.status_code == 200
        assert trecho in resp.text


class TestPlanoDeLimite:
    """A decisão do reconciliador — lógica pura, sem rede.

    O comportamento de cobrança em si (a assinatura realmente parar na Nª) é
    verificado contra o Stripe de verdade em `scripts/validar_parcelado.py`, com
    test clock. Teste com mock não pega contrato de API quebrado: foi exatamente
    um mock que deixou `subscription_data[cancel_at]` passar por meses.
    """

    def _sub(self, **campos):
        base = {
            "id": "sub_x",
            "status": "active",
            "billing_cycle_anchor": int(datetime(2026, 5, 13, 12, tzinfo=timezone.utc).timestamp()),
            "metadata": {"parcelas_total": "5", "nome_cliente": "Bruna"},
        }
        base.update(campos)
        return base

    def test_corta_um_dia_antes_da_parcela_que_sobra(self):
        plano = pagamentos.plano_de_limite(self._sub(), pagas=2)
        assert plano["acao"] == "cancel_at"
        # Fatura 5 sai em 13/09; a 6ª sairia em 13/10 — cortar em 12/10 mata só ela.
        quando = datetime.fromtimestamp(plano["quando"], timezone.utc)
        assert (quando.day, quando.month, quando.year) == (12, 10, 2026)

    def test_mes_curto_nao_desloca_a_ancora(self):
        """31/01 + 1 mês é 28/02, mas 31/01 + 2 meses volta pro dia 31.

        Iterando mês a mês o dia iria encolhendo e o corte cairia cedo demais —
        cancelando ANTES da última parcela devida, que é perder dinheiro.
        """
        anc = int(datetime(2026, 1, 31, 12, tzinfo=timezone.utc).timestamp())
        plano = pagamentos.plano_de_limite(
            self._sub(billing_cycle_anchor=anc, metadata={"parcelas_total": "3"}), pagas=1
        )
        # Faturas: 31/01, 28/02, 31/03 (a 3ª e última). A 4ª sairia em 30/04
        # (31/04 não existe), então o corte é 29/04 — depois da 3ª, antes da 4ª.
        quando = datetime.fromtimestamp(plano["quando"], timezone.utc)
        assert (quando.day, quando.month) == (29, 4)

    def test_ja_pagou_tudo_vira_nao_renovar(self):
        plano = pagamentos.plano_de_limite(self._sub(), pagas=5)
        assert plano["acao"] == "nao_renovar"
        assert plano["quando"] is None

    def test_excedente_aparece_no_motivo(self):
        """O caso real: plano de 4x que cobrou 5. Reporta; estorno é decisão humana."""
        plano = pagamentos.plano_de_limite(self._sub(metadata={"parcelas_total": "4"}), pagas=5)
        assert "JÁ COBROU 1 A MAIS" in plano["motivo"]

    def test_mensalidade_da_terapia_e_intocavel(self):
        """Sem `parcelas_total` = contínua. Cancelar aqui é o erro caro."""
        assert pagamentos.plano_de_limite(self._sub(metadata={"tipo": "clinica"}), pagas=9) is None

    @pytest.mark.parametrize("ja", [{"cancel_at": 123}, {"cancel_at_period_end": True}])
    def test_nao_sobrescreve_fim_ja_definido(self, ja):
        """Se alguém ajustou à mão no dashboard, o cron não desfaz."""
        assert pagamentos.plano_de_limite(self._sub(**ja), pagas=1) is None


class TestLimitarParcelado:
    def _sub(self, sid, total, status="active"):
        return {
            "id": sid,
            "status": status,
            "billing_cycle_anchor": int(datetime(2026, 5, 13, tzinfo=timezone.utc).timestamp()),
            "metadata": {"parcelas_total": str(total), "nome_cliente": sid},
            "items": {"data": [{"price": {"recurring": {"interval": "month"}, "product": "p1"}}]},
        }

    @pytest.mark.asyncio
    async def test_simular_nao_escreve_nada(self):
        escrever = AsyncMock()
        with patch(
            "app.services.pagamentos.stripe_client.listar_assinaturas",
            AsyncMock(return_value=[self._sub("sub_a", 5)]),
        ), patch(
            "app.services.pagamentos.stripe_client.listar_faturas",
            AsyncMock(return_value=[{"status": "paid"}]),
        ), patch(
            "app.services.pagamentos.stripe_client.atualizar_assinatura", escrever
        ), patch(
            "app.services.pagamentos.stripe_client.listar_produtos", AsyncMock(return_value={})
        ):
            r = await pagamentos.limitar_parcelado(simular=True)
        escrever.assert_not_awaited()
        assert len(r["planejadas"]) == 1
        assert r["simulado"] is True

    @pytest.mark.asyncio
    async def test_aplica_e_ignora_cancelada(self):
        escrever = AsyncMock()
        with patch(
            "app.services.pagamentos.stripe_client.listar_assinaturas",
            AsyncMock(return_value=[self._sub("sub_a", 5), self._sub("sub_morta", 5, "canceled")]),
        ), patch(
            "app.services.pagamentos.stripe_client.listar_faturas",
            AsyncMock(return_value=[{"status": "paid"}]),
        ), patch(
            "app.services.pagamentos.stripe_client.atualizar_assinatura", escrever
        ), patch(
            "app.services.pagamentos.stripe_client.listar_produtos", AsyncMock(return_value={})
        ):
            r = await pagamentos.limitar_parcelado(simular=False)
        assert [c.args[0] for c in escrever.await_args_list] == ["sub_a"]
        assert r["planejadas"][0]["aplicado"] is True

    @pytest.mark.asyncio
    async def test_teto_por_rodada_nao_some_com_o_resto(self):
        escrever = AsyncMock()
        with patch(
            "app.services.pagamentos.stripe_client.listar_assinaturas",
            AsyncMock(return_value=[self._sub(f"sub_{i}", 5) for i in range(25)]),
        ), patch(
            "app.services.pagamentos.stripe_client.listar_faturas",
            AsyncMock(return_value=[{"status": "paid"}]),
        ), patch(
            "app.services.pagamentos.stripe_client.atualizar_assinatura", escrever
        ), patch(
            "app.services.pagamentos.stripe_client.listar_produtos", AsyncMock(return_value={})
        ):
            r = await pagamentos.limitar_parcelado(simular=False, limite=20)
        assert r["truncado"] is True
        assert escrever.await_count == 20

    @pytest.mark.asyncio
    async def test_faturas_ilegiveis_nao_viram_cancelamento(self):
        """Sem saber quantas foram pagas não dá pra decidir — e o default é não mexer."""
        escrever = AsyncMock()
        with patch(
            "app.services.pagamentos.stripe_client.listar_assinaturas",
            AsyncMock(return_value=[self._sub("sub_a", 5)]),
        ), patch(
            "app.services.pagamentos.stripe_client.listar_faturas",
            AsyncMock(side_effect=StripeError("down")),
        ), patch(
            "app.services.pagamentos.stripe_client.atualizar_assinatura", escrever
        ), patch(
            "app.services.pagamentos.stripe_client.listar_produtos", AsyncMock(return_value={})
        ):
            r = await pagamentos.limitar_parcelado(simular=False)
        escrever.assert_not_awaited()
        assert r["planejadas"] == []

    @pytest.mark.asyncio
    async def test_neuro_sem_plano_vira_alerta_e_nao_e_tocado(self):
        """Formato de metadata mudou -> aparece na tela, não some em silêncio."""
        orfa = {
            "id": "sub_orfa",
            "status": "active",
            "metadata": {},
            "items": {"data": [{"price": {"recurring": {"interval": "month"}, "product": "p9"}}]},
        }
        escrever = AsyncMock()
        with patch(
            "app.services.pagamentos.stripe_client.listar_assinaturas",
            AsyncMock(return_value=[orfa]),
        ), patch("app.services.pagamentos.stripe_client.atualizar_assinatura", escrever), patch(
            "app.services.pagamentos.stripe_client.listar_produtos",
            AsyncMock(return_value={"p9": "Neuro Avaliação Neuropsicológica - Alguém"}),
        ):
            r = await pagamentos.limitar_parcelado(simular=False)
        escrever.assert_not_awaited()
        assert r["alertas"][0]["id"] == "sub_orfa"


class TestLegadoNoPainel:
    """As 48 assinaturas criadas fora da Sofia precisam fazer sentido na tela."""

    def test_neuro_antigo_e_reconhecido_pelo_plano_de_parcelas(self):
        """`metadata.tipo` só existe nas 3 criadas pela Sofia; sem este fallback as
        18 de neuro do painel do site apareciam rotuladas como Terapia."""
        assert pagamentos.tipo_da_assinatura({"metadata": {"parcelas_total": "4"}}) == "neuro"

    def test_sem_metadata_nenhum_e_terapia(self):
        assert pagamentos.tipo_da_assinatura({"metadata": {}}) == "clinica"

    @pytest.mark.parametrize(
        "sub,esperado",
        [
            ({"metadata": {"nome_cliente": "Bruna"}}, "Bruna"),
            ({"metadata": {"patient_name": "Luciana"}}, "Luciana"),
            ({"metadata": {}, "customer": {"name": "Alex A C Santos"}}, "Alex A C Santos"),
            ({"metadata": {}, "customer": "cus_sem_expand"}, "(sem nome)"),
            ({"metadata": {}}, "(sem nome)"),
        ],
    )
    def test_cadeia_de_fallback_do_nome(self, sub, esperado):
        assert pagamentos.nome_do_cliente(sub) == esperado
