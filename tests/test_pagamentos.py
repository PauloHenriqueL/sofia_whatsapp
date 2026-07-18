"""Testes dos pagamentos (Stripe): links, assinatura dia 10, vínculo e status."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.database import Base, get_db
from app.main import app
from app.models import Conversa
from app.services import pagamentos
from app.services.pagamentos import ErroValidacao
from app.services.stripe_client import StripeError, _achatar

AGORA = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def stripe_ligado():
    """Liga o Stripe nos testes (a chave dummy nunca é usada: o client é mockado)."""
    original = settings.stripe_secret_key
    settings.stripe_secret_key = "sk_test_dummy"
    yield
    settings.stripe_secret_key = original


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
        assert r["ref"] == "https://buy.stripe.com/x"  # a URL é a referência do vínculo
        assert preco.await_args.args[0]["unit_amount"] == 120000  # centavos
        assert "recurring" not in preco.await_args.args[0]
        assert r["resumo"]["valor_total"] == "R$ 1.200,00"

    @pytest.mark.asyncio
    async def test_parcelado_vira_assinatura_com_cancel_at(self):
        """3x = assinatura mensal da parcela que se cancela após ~90 dias (bug do
        original corrigido: sem cancel_at ela cobraria pra sempre)."""
        preco = AsyncMock(return_value={"id": "price_1"})
        sessao = AsyncMock(return_value={"id": "cs_test_1", "url": "https://checkout.stripe.com/1"})
        with patch("app.services.pagamentos.stripe_client.criar_preco", preco), patch(
            "app.services.pagamentos.stripe_client.criar_checkout_session", sessao
        ):
            r = await pagamentos.criar_link_neuro("Maria", "m@x.com", 1200, parcelas=3, agora=AGORA)

        assert r["ref"] == "cs_test_1"
        assert preco.await_args.args[0]["unit_amount"] == 40000  # 1200/3 em centavos
        assert preco.await_args.args[0]["recurring"]["interval"] == "month"
        dados = sessao.await_args.args[0]
        assert dados["subscription_data"]["cancel_at"] == int(
            (AGORA + timedelta(days=90)).timestamp()
        )
        assert dados["subscription_data"]["metadata"]["parcelas_total"] == "3"

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


class TestAssinaturaTerapia:
    @pytest.mark.asyncio
    async def test_ancora_no_proximo_dia_10(self):
        """Dia 18 -> âncora no dia 10 do mês seguinte, com pro-rata dos dias até lá."""
        sessao = AsyncMock(return_value={"id": "cs_t_1", "url": "https://checkout.stripe.com/2"})
        with patch("app.services.pagamentos.stripe_client.criar_checkout_session", sessao):
            r = await pagamentos.criar_assinatura_terapia("Ana", "a@x.com", 200, agora=AGORA)

        dados = sessao.await_args.args[0]
        ancora = datetime.fromtimestamp(
            dados["subscription_data"]["billing_cycle_anchor"], tz=timezone.utc
        )
        assert (ancora.year, ancora.month, ancora.day) == (2026, 8, 10)
        assert dados["subscription_data"]["proration_behavior"] == "create_prorations"
        assert r["proximo_dia10"] == "10/08/2026"
        assert r["dias_ate_dia10"] == 22
        assert r["pro_rata"] == pagamentos.fmt_centavos(round(200 * 100 / 30 * 22))

    @pytest.mark.asyncio
    async def test_antes_do_dia_10_ancora_no_mesmo_mes(self):
        sessao = AsyncMock(return_value={"id": "cs_t_2", "url": "u"})
        cedo = datetime(2026, 7, 3, tzinfo=timezone.utc)
        with patch("app.services.pagamentos.stripe_client.criar_checkout_session", sessao):
            r = await pagamentos.criar_assinatura_terapia("Ana", "a@x.com", 200, agora=cedo)
        assert r["proximo_dia10"] == "10/07/2026"

    @pytest.mark.asyncio
    async def test_valor_fora_da_faixa(self):
        with pytest.raises(ErroValidacao):
            await pagamentos.criar_assinatura_terapia("Ana", "a@x.com", 10)

    @pytest.mark.asyncio
    async def test_reusa_preco_do_catalogo_quando_o_valor_bate(self):
        """STRIPE_PRECO_MENSAL_ID + mensalidade igual -> line item com o price
        do catálogo (relatórios unificados com o site da Allos)."""
        original = settings.stripe_preco_mensal_id
        settings.stripe_preco_mensal_id = "price_catalogo"
        preco = AsyncMock(return_value={"id": "price_catalogo", "unit_amount": 20000})
        sessao = AsyncMock(return_value={"id": "cs_t_3", "url": "u"})
        try:
            with patch("app.services.pagamentos.stripe_client.obter_preco", preco), patch(
                "app.services.pagamentos.stripe_client.criar_checkout_session", sessao
            ):
                await pagamentos.criar_assinatura_terapia("Ana", "a@x.com", 200, agora=AGORA)
        finally:
            settings.stripe_preco_mensal_id = original
        item = sessao.await_args.args[0]["line_items"][0]
        assert item == {"price": "price_catalogo", "quantity": 1}

    @pytest.mark.asyncio
    async def test_valor_diferente_do_catalogo_cai_pro_preco_inline(self):
        original = settings.stripe_preco_mensal_id
        settings.stripe_preco_mensal_id = "price_catalogo"
        preco = AsyncMock(return_value={"id": "price_catalogo", "unit_amount": 20000})
        sessao = AsyncMock(return_value={"id": "cs_t_4", "url": "u"})
        try:
            with patch("app.services.pagamentos.stripe_client.obter_preco", preco), patch(
                "app.services.pagamentos.stripe_client.criar_checkout_session", sessao
            ):
                await pagamentos.criar_assinatura_terapia("Ana", "a@x.com", 150, agora=AGORA)
        finally:
            settings.stripe_preco_mensal_id = original
        item = sessao.await_args.args[0]["line_items"][0]
        assert item["price_data"]["unit_amount"] == 15000  # bolsa/ajuste: preço inline

    @pytest.mark.asyncio
    async def test_catalogo_inacessivel_nao_impede_a_assinatura(self):
        original = settings.stripe_preco_mensal_id
        settings.stripe_preco_mensal_id = "price_catalogo"
        preco = AsyncMock(side_effect=StripeError("down"))
        sessao = AsyncMock(return_value={"id": "cs_t_5", "url": "u"})
        try:
            with patch("app.services.pagamentos.stripe_client.obter_preco", preco), patch(
                "app.services.pagamentos.stripe_client.criar_checkout_session", sessao
            ):
                r = await pagamentos.criar_assinatura_terapia("Ana", "a@x.com", 200, agora=AGORA)
        finally:
            settings.stripe_preco_mensal_id = original
        assert r["ref"] == "cs_t_5"
        assert "price_data" in sessao.await_args.args[0]["line_items"][0]


class TestUrlDeCancelamento:
    @pytest.mark.asyncio
    async def test_cancel_url_customizada_vai_pro_checkout(self):
        """STRIPE_CANCEL_URL (ex.: página do Hamilton) substitui a página da Sofia."""
        original = settings.stripe_cancel_url
        settings.stripe_cancel_url = "https://hamilton-v2.onrender.com/api/v1/stripe/cancelado/"
        preco = AsyncMock(return_value={"id": "price_1"})
        sessao = AsyncMock(return_value={"id": "cs_x", "url": "u"})
        try:
            with patch("app.services.pagamentos.stripe_client.criar_preco", preco), patch(
                "app.services.pagamentos.stripe_client.criar_checkout_session", sessao
            ):
                await pagamentos.criar_link_neuro("Maria", "m@x.com", 300, parcelas=2)
        finally:
            settings.stripe_cancel_url = original
        assert (
            sessao.await_args.args[0]["cancel_url"]
            == "https://hamilton-v2.onrender.com/api/v1/stripe/cancelado/"
        )

    @pytest.mark.asyncio
    async def test_sem_cancel_url_usa_a_pagina_da_sofia(self):
        sessao = AsyncMock(return_value={"id": "cs_y", "url": "u"})
        with patch("app.services.pagamentos.stripe_client.criar_checkout_session", sessao):
            await pagamentos.criar_assinatura_terapia("Ana", "a@x.com", 200, agora=AGORA)
        assert sessao.await_args.args[0]["cancel_url"] == (
            f"{settings.base_url}/pagamento-cancelado"
        )


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
    async def test_sem_chave_nao_faz_nada(self):
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
    async def test_sem_chave_mostra_aviso(self, ambiente):
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
                "ref": "https://buy.stripe.com/x",
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
        assert "https://buy.stripe.com/x" in resp.text
        async with maker() as s:
            assert (await s.get(Conversa, cid)).stripe_ref == "https://buy.stripe.com/x"

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
