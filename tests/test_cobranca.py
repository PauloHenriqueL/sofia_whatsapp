"""Testes da cobrança da mensalidade (Demanda D).

A conversa em si é conduzida por LLM e não dá pra testar. O que dá — e é o que
está aqui — são as **regras em volta**: quem pode ser cobrado, quem nunca pode,
o que acontece quando a mensagem não sai, e a assinatura sem pro-rata.

O caso mais importante do arquivo é `test_nao_cobra_quem_faltou_a_primeira_sessao`:
o gatilho da pesquisa (signal na CRIAÇÃO da consulta) e o da cobrança
(`is_realizado=True`) divergem no mesmo registro, e confundir os dois cobraria
quem não foi atendido.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base
from app.models import Conversa
from app.services import cobranca, config_negocio, hamilton_client, pagamentos, stripe_client

AGORA = datetime(2026, 8, 8, 12, 0, 0, tzinfo=timezone.utc)


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


@pytest.fixture(autouse=True)
def _cobranca_ligada():
    """A cobrança nasce desligada; nos testes ela precisa estar ligada."""
    original = config_negocio.valor("cobranca_ativa")
    config_negocio._cache["cobranca_ativa"] = True
    yield
    config_negocio._cache["cobranca_ativa"] = original


async def _conversa(session, **kwargs):
    dados = {
        "numero_whatsapp": "5531999998888",
        "paciente_hamilton_id": 500,
        "dados_coletados": {"nome_completo": "Maria Silva"},
    }
    dados.update(kwargs)
    conversa = Conversa(**dados)
    session.add(conversa)
    await session.flush()
    return conversa


def _hamilton(realizada=True, pid=500):
    cliente = AsyncMock()
    cliente.status_primeira_consulta.return_value = {
        pid: {
            "pk_paciente": pid,
            "nome": "Maria Silva",
            "created_at": "2026-07-20",
            "primeira_consulta_realizada": realizada,
            "dat_primeira_consulta": "2026-08-01" if realizada else None,
        }
    }
    return cliente


class TestQuemPodeSerCobrado:
    @pytest.mark.asyncio
    async def test_cobra_quem_teve_a_primeira_sessao_realizada(self, session):
        await _conversa(session)
        await session.commit()
        with patch.object(
            hamilton_client, "get_hamilton_client", return_value=_hamilton(realizada=True)
        ), patch.object(cobranca, "iniciar", AsyncMock(return_value=True)) as mock_iniciar:
            resumo = await cobranca.rodar_cobrancas(session, AGORA)
        assert resumo["enviadas"] == 1
        mock_iniciar.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_nao_cobra_quem_faltou_a_primeira_sessao(self, session):
        """O gatilho é `is_realizado`, não "a consulta foi lançada".

        O signal que cria a pesquisa dispara na CRIAÇÃO da consulta e ignora
        `is_realizado` — quem faltou recebe a pesquisa perguntando "como foi sua
        primeira sessão". Cobrar com o mesmo sinal cobraria quem não foi atendido.
        """
        await _conversa(session)
        await session.commit()
        with patch.object(
            hamilton_client, "get_hamilton_client", return_value=_hamilton(realizada=False)
        ), patch.object(cobranca, "iniciar", AsyncMock(return_value=True)) as mock_iniciar:
            resumo = await cobranca.rodar_cobrancas(session, AGORA)
        assert resumo["enviadas"] == 0
        mock_iniciar.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_nunca_cobra_paciente_de_parceria(self, session):
        """Prefeitura/convênio paga por fora; a pessoa não deve nada."""
        await _conversa(session, dados_coletados={"nome_completo": "Maria", "is_parceria": True})
        await session.commit()
        with patch.object(
            hamilton_client, "get_hamilton_client", return_value=_hamilton()
        ), patch.object(cobranca, "iniciar", AsyncMock(return_value=True)) as mock_iniciar:
            resumo = await cobranca.rodar_cobrancas(session, AGORA)
        assert resumo["enviadas"] == 0
        mock_iniciar.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_nao_cobra_duas_vezes(self, session):
        await _conversa(session, cobranca_iniciada_em=AGORA - timedelta(days=3))
        await session.commit()
        with patch.object(
            hamilton_client, "get_hamilton_client", return_value=_hamilton()
        ), patch.object(cobranca, "iniciar", AsyncMock(return_value=True)) as mock_iniciar:
            resumo = await cobranca.rodar_cobrancas(session, AGORA)
        assert resumo["enviadas"] == 0
        mock_iniciar.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_nao_cobra_quem_esta_em_pesquisa(self, session):
        """Quem está em pesquisa é cobrado por `pesquisa.finalizar`, não pelo cron."""
        await _conversa(session, pesquisa_avaliacao_id=10)
        await session.commit()
        with patch.object(
            hamilton_client, "get_hamilton_client", return_value=_hamilton()
        ), patch.object(cobranca, "iniciar", AsyncMock(return_value=True)) as mock_iniciar:
            resumo = await cobranca.rodar_cobrancas(session, AGORA)
        assert resumo["enviadas"] == 0
        mock_iniciar.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_desligada_no_painel_nao_aborda_ninguem(self, session):
        config_negocio._cache["cobranca_ativa"] = False
        await _conversa(session)
        await session.commit()
        with patch.object(cobranca, "iniciar", AsyncMock(return_value=True)) as mock_iniciar:
            resumo = await cobranca.rodar_cobrancas(session, AGORA)
        assert resumo["enviadas"] == 0
        mock_iniciar.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cobra_mesmo_com_a_conversa_escalada(self, session):
        """Decisão do Paulo: sessão realizada ⇒ a conversa TEM que acontecer."""
        await _conversa(session, modo="humano", estado="escalado")
        await session.commit()
        with patch.object(
            hamilton_client, "get_hamilton_client", return_value=_hamilton()
        ), patch.object(cobranca, "iniciar", AsyncMock(return_value=True)):
            resumo = await cobranca.rodar_cobrancas(session, AGORA)
        assert resumo["enviadas"] == 1

    @pytest.mark.asyncio
    async def test_hamilton_fora_do_ar_nao_cobra_ninguem(self, session):
        """Sem confirmação de que a sessão aconteceu, ninguém é cobrado."""
        await _conversa(session)
        await session.commit()
        cliente = AsyncMock()
        cliente.status_primeira_consulta.side_effect = hamilton_client.HamiltonError("fora")
        with patch.object(
            hamilton_client, "get_hamilton_client", return_value=cliente
        ), patch.object(cobranca, "iniciar", AsyncMock(return_value=True)) as mock_iniciar:
            resumo = await cobranca.rodar_cobrancas(session, AGORA)
        assert resumo["enviadas"] == 0
        mock_iniciar.assert_not_awaited()


class TestValorCobrado:
    def test_usa_o_preco_de_tabela(self):
        conversa = Conversa(numero_whatsapp="5531", dados_coletados={})
        config_negocio._cache["preco_terapia_mensal"] = 200
        assert cobranca.valor_mensal(conversa) == 200

    def test_desconto_autorizado_tem_precedencia(self):
        """A Sofia não pode prometer um valor na conversa e cobrar outro."""
        conversa = Conversa(numero_whatsapp="5531", dados_coletados={}, desconto_valor=150)
        config_negocio._cache["preco_terapia_mensal"] = 200
        assert cobranca.valor_mensal(conversa) == 150


class TestEnvioQueFalha:
    @pytest.mark.asyncio
    async def test_fora_da_janela_vai_pra_fila_da_thaina(self, session):
        """Falha de envio não some: marca `sem_janela` e aparece no painel.

        Fora das 24h da Meta a Sofia não consegue mandar texto livre, e não há
        template aprovado. Retentar no próximo tick seria retentar pra sempre.
        """
        conversa = await _conversa(session)
        await session.commit()
        with patch.object(
            cobranca, "_criar_link", AsyncMock(return_value="https://x")
        ), patch.object(
            cobranca, "_turno", AsyncMock(return_value="Oi, sobre a mensalidade...")
        ), patch.object(
            cobranca, "_enviar", AsyncMock(return_value=False)
        ):
            ok = await cobranca.iniciar(session, conversa, AGORA)
        assert ok is False
        assert conversa.cobranca_status == "sem_janela"
        # Encerrada junto: não fica "em curso" esperando uma resposta que não vem.
        assert conversa.cobranca_encerrada_em is not None
        assert cobranca.em_cobranca(conversa) is False

    @pytest.mark.asyncio
    async def test_envio_ok_liga_o_modo_cobranca(self, session):
        conversa = await _conversa(session)
        await session.commit()
        with patch.object(
            cobranca, "_criar_link", AsyncMock(return_value="https://x")
        ), patch.object(
            cobranca, "_turno", AsyncMock(return_value="Oi, sobre a mensalidade...")
        ), patch.object(
            cobranca, "_enviar", AsyncMock(return_value=True)
        ):
            ok = await cobranca.iniciar(session, conversa, AGORA)
        assert ok is True
        assert conversa.cobranca_status == "enviada"
        assert cobranca.em_cobranca(conversa) is True

    @pytest.mark.asyncio
    async def test_sem_link_ainda_cobra_mas_registra_o_erro(self, session):
        """Stripe fora do ar não cancela a cobrança: sobra o Pix."""
        conversa = await _conversa(session)
        await session.commit()
        with patch.object(cobranca, "_criar_link", AsyncMock(return_value=None)), patch.object(
            cobranca, "_turno", AsyncMock(return_value="Oi, a chave Pix é...")
        ), patch.object(cobranca, "_enviar", AsyncMock(return_value=True)):
            await cobranca.iniciar(session, conversa, AGORA)
        assert conversa.cobranca_status == "erro_link"
        assert cobranca.em_cobranca(conversa) is True


class TestLembreteEPrazo:
    @pytest.mark.asyncio
    async def test_manda_um_lembrete_so(self, session):
        config_negocio._cache["cobranca_lembrete_horas"] = 20
        await _conversa(session, cobranca_iniciada_em=AGORA - timedelta(hours=21))
        await session.commit()
        with patch.object(cobranca, "_enviar", AsyncMock(return_value=True)) as mock_enviar:
            resumo = await cobranca.rodar_cobrancas(session, AGORA)
            assert resumo["lembretes"] == 1
            # Segunda rodada: já lembrou, não lembra de novo.
            resumo = await cobranca.rodar_cobrancas(session, AGORA)
        assert resumo["lembretes"] == 0
        assert mock_enviar.await_count == 1

    @pytest.mark.asyncio
    async def test_encerra_por_prazo_sem_mandar_nada(self, session):
        conversa = await _conversa(
            session, cobranca_iniciada_em=AGORA - timedelta(hours=cobranca.HORAS_ENCERRAMENTO + 1)
        )
        await session.commit()
        with patch.object(cobranca, "_enviar", AsyncMock(return_value=True)) as mock_enviar:
            resumo = await cobranca.rodar_cobrancas(session, AGORA)
        assert resumo["encerradas"] == 1
        assert conversa.cobranca_status == "sem_resposta"
        assert cobranca.em_cobranca(conversa) is False
        mock_enviar.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_lembrete_que_nao_sai_vai_pra_fila(self, session):
        config_negocio._cache["cobranca_lembrete_horas"] = 20
        conversa = await _conversa(session, cobranca_iniciada_em=AGORA - timedelta(hours=21))
        await session.commit()
        with patch.object(cobranca, "_enviar", AsyncMock(return_value=False)):
            resumo = await cobranca.rodar_cobrancas(session, AGORA)
        assert resumo["lembretes"] == 0
        assert conversa.cobranca_status == "sem_janela"
        assert cobranca.em_cobranca(conversa) is False


class TestFormaDePagamento:
    @pytest.mark.asyncio
    async def test_registra_a_forma_sem_encerrar_o_modo(self, session):
        """Encerrar aqui jogaria a dúvida seguinte no prompt de acolhimento."""
        conversa = await _conversa(session, cobranca_iniciada_em=AGORA)
        await session.commit()
        tc = SimpleNamespace(id="1", name="registrar_forma_pagamento", arguments={"forma": "pix"})
        resultado = await cobranca._registrar_forma(session, conversa, tc)
        assert resultado["forma"] == "pix"
        assert conversa.cobranca_status == "pix"
        assert cobranca.em_cobranca(conversa) is True

    @pytest.mark.asyncio
    async def test_forma_invalida_e_descartada(self, session):
        conversa = await _conversa(session, cobranca_iniciada_em=AGORA)
        await session.commit()
        tc = SimpleNamespace(
            id="1", name="registrar_forma_pagamento", arguments={"forma": "boleto"}
        )
        resultado = await cobranca._registrar_forma(session, conversa, tc)
        assert resultado["status"] == "invalida"
        assert conversa.cobranca_status is None


class TestPrompt:
    def test_sem_chave_pix_manda_nao_oferecer(self, session=None):
        config_negocio._cache["chave_pix"] = ""
        conversa = Conversa(numero_whatsapp="5531", dados_coletados={"nome_completo": "Maria"})
        prompt = cobranca.montar_prompt(conversa, "https://link")
        assert "NÃO oferecido" in prompt
        config_negocio._cache["chave_pix"] = "50.990.346/0001-52"

    def test_sem_link_proibe_inventar(self):
        conversa = Conversa(numero_whatsapp="5531", dados_coletados={"nome_completo": "Maria"})
        prompt = cobranca.montar_prompt(conversa, None)
        assert "NÃO invente um link" in prompt

    def test_injeta_valor_pix_e_link(self):
        config_negocio._cache["preco_terapia_mensal"] = 200
        config_negocio._cache["chave_pix"] = "50.990.346/0001-52"
        conversa = Conversa(
            numero_whatsapp="5531", dados_coletados={"nome_completo": "Maria Silva"}
        )
        prompt = cobranca.montar_prompt(conversa, "https://buy.stripe.com/abc")
        assert "R$ 200" in prompt
        assert "50.990.346/0001-52" in prompt
        assert "https://buy.stripe.com/abc" in prompt
        assert "Maria" in prompt


class TestAssinaturaSemProRata:
    """A cobrança automática usa a MESMA função do painel (unificado em 08/08)."""

    @pytest.mark.asyncio
    async def test_valor_cheio_hoje_e_todo_mes(self):
        capturado = {}

        async def _fake(payload):
            capturado.update(payload)
            return {"url": "https://checkout", "id": "cs_1"}

        with patch.object(stripe_client, "criar_checkout_session", _fake), patch.object(
            stripe_client, "obter_preco", AsyncMock(side_effect=stripe_client.StripeError("x"))
        ):
            resultado = await pagamentos.criar_assinatura_mensalidade(
                nome="Maria", valor_mensal=200, agora=AGORA
            )
        sub = capturado["subscription_data"]
        # Nenhum dos três: anchor e pro-rata produziriam valor de entrada diferente
        # por paciente; trial_end faria o checkout dizer "avaliação gratuita".
        assert "billing_cycle_anchor" not in sub
        assert "proration_behavior" not in sub
        assert "trial_end" not in sub
        assert len(capturado["line_items"]) == 1
        assert resultado["valor_entrada"] == "R$ 200,00"

    @pytest.mark.asyncio
    async def test_email_e_opcional(self):
        """A Sofia nunca coletou e-mail; o Checkout pede se não vier."""
        capturado = {}

        async def _fake(payload):
            capturado.update(payload)
            return {"url": "https://checkout", "id": "cs_1"}

        with patch.object(stripe_client, "criar_checkout_session", _fake), patch.object(
            stripe_client, "obter_preco", AsyncMock(side_effect=stripe_client.StripeError("x"))
        ):
            await pagamentos.criar_assinatura_mensalidade(nome="Maria", valor_mensal=200)
        assert "customer_email" not in capturado

    @pytest.mark.asyncio
    async def test_email_invalido_ainda_e_rejeitado(self):
        with pytest.raises(pagamentos.ErroValidacao):
            await pagamentos.criar_assinatura_mensalidade(
                nome="Maria", valor_mensal=200, email="não-é-email"
            )


class TestVinculoStripe:
    @pytest.mark.asyncio
    async def test_nao_sobrescreve_referencia_existente(self, session):
        """Um paciente com neuro amarrado não pode perder a referência."""
        conversa = await _conversa(session, stripe_ref="sub_neuro_existente")
        await session.commit()
        with patch.object(stripe_client, "configurado", return_value=True), patch.object(
            pagamentos,
            "criar_assinatura_mensalidade",
            AsyncMock(return_value={"link": "https://x", "ref": "cs_novo"}),
        ):
            link = await cobranca._criar_link(session, conversa)
        assert link == "https://x"
        assert conversa.stripe_ref == "sub_neuro_existente"

    @pytest.mark.asyncio
    async def test_amarra_quando_nao_havia_referencia(self, session):
        conversa = await _conversa(session)
        await session.commit()
        with patch.object(stripe_client, "configurado", return_value=True), patch.object(
            pagamentos,
            "criar_assinatura_mensalidade",
            AsyncMock(return_value={"link": "https://x", "ref": "cs_novo"}),
        ):
            await cobranca._criar_link(session, conversa)
        assert conversa.stripe_ref == "cs_novo"

    @pytest.mark.asyncio
    async def test_stripe_desligado_nao_quebra(self, session):
        conversa = await _conversa(session)
        await session.commit()
        with patch.object(stripe_client, "configurado", return_value=False):
            assert await cobranca._criar_link(session, conversa) is None
