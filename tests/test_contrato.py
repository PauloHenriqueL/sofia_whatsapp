"""Testes do contrato terapêutico assinado pelo paciente (Demanda E) — lado da Sofia.

O documento em si é gerado no Hamilton e testado lá. O que está aqui são as
decisões que só a Sofia consegue tomar, porque só ela tem a conversa:

  - **quem não recebe** contrato (parceria, neuro, gratuidade, feature desligada);
  - o que ela **diz** ao modelo sobre o contrato — inclusive o silêncio, que é o
    comportamento certo quando não há contrato: instrução negativa ("não fale de
    contrato") é convite pra ele falar;
  - que uma falha do Hamilton **não derruba a cobrança**: o pagamento é o que
    não pode parar.

O Hamilton é mockado — nenhum teste faz rede (o `conftest.py` bloqueia de todo jeito).
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base
from app.models import Conversa, Escalada
from app.services import config_negocio, contrato, hamilton_client, hoje

AGORA = datetime(2026, 8, 17, 12, 0, 0, tzinfo=timezone.utc)


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
def _contrato_ligado():
    """Nasce desligado (fluxo automático que manda documento jurídico sobe dark)."""
    original = config_negocio.valor("contrato_ativo")
    config_negocio._cache["contrato_ativo"] = True
    yield
    config_negocio._cache["contrato_ativo"] = original


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


def _cliente(resposta=None, erro=False):
    cliente = AsyncMock()
    if erro:
        cliente.gerar_contrato.side_effect = hamilton_client.HamiltonError("500")
        cliente.status_contrato.side_effect = hamilton_client.HamiltonError("500")
    else:
        padrao = (
            resposta
            if resposta is not None
            else {
                "contrato_id": 1,
                "status": "pendente",
                "link": "https://assina.ae/abc123",
            }
        )
        cliente.gerar_contrato.return_value = padrao
        cliente.status_contrato.return_value = padrao
    return cliente


class TestQuemNaoRecebeContrato:
    @pytest.mark.asyncio
    async def test_desligado_no_painel(self, session):
        config_negocio._cache["contrato_ativo"] = False
        conversa = await _conversa(session)
        assert "desligado" in await contrato.motivo_para_pular(session, conversa)

    @pytest.mark.asyncio
    async def test_sem_paciente_no_hamilton(self, session):
        conversa = await _conversa(session, paciente_hamilton_id=None)
        assert "sem paciente" in await contrato.motivo_para_pular(session, conversa)

    @pytest.mark.asyncio
    async def test_parceria_nao_assina_contrato_de_mensalidade(self, session):
        conversa = await _conversa(
            session, dados_coletados={"nome_completo": "Maria", "is_parceria": True}
        )
        assert "parceria" in await contrato.motivo_para_pular(session, conversa)

    @pytest.mark.asyncio
    async def test_conversa_arquivada(self, session):
        conversa = await _conversa(session, arquivada_em=AGORA)
        assert "arquivada" in await contrato.motivo_para_pular(session, conversa)

    @pytest.mark.asyncio
    async def test_neuro_nao_recebe_contrato_de_terapia(self, session):
        conversa = await _conversa(
            session, dados_coletados={"nome_completo": "Maria", "motivo_busca": "quero neuro"}
        )
        assert "neuropsico" in await contrato.motivo_para_pular(session, conversa)

    @pytest.mark.asyncio
    async def test_escalada_de_gratuidade_aberta_barra(self, session):
        """Quem disse que não pode pagar não recebe documento de mensalidade."""
        conversa = await _conversa(session)
        session.add(Escalada(conversa_id=conversa.id, motivo="gratuidade"))
        await session.flush()
        assert "gratuidade" in await contrato.motivo_para_pular(session, conversa)

    @pytest.mark.asyncio
    async def test_gratuidade_ja_resolvida_nao_barra(self, session):
        conversa = await _conversa(session)
        session.add(Escalada(conversa_id=conversa.id, motivo="gratuidade", resolvida_em=AGORA))
        await session.flush()
        assert await contrato.motivo_para_pular(session, conversa) is None

    @pytest.mark.asyncio
    async def test_paciente_comum_passa(self, session):
        conversa = await _conversa(session)
        assert await contrato.motivo_para_pular(session, conversa) is None


class TestGarantir:
    @pytest.mark.asyncio
    async def test_gera_e_encurta_o_link(self, session):
        """O `assina.ae` não pode chegar cru no WhatsApp — é cara de golpe."""
        conversa = await _conversa(session)
        cliente = _cliente()
        with patch.object(hamilton_client, "get_hamilton_client", return_value=cliente):
            dados = await contrato.garantir(session, conversa, 200)

        assert dados["status"] == "pendente"
        assert "assina.ae" not in dados["link"]
        assert dados["link"]  # virou link curto nosso
        assert cliente.gerar_contrato.await_args.kwargs["valor_mensal"] == 200
        # O TEXTO vai no corpo: a fonte é o painel, não uma cópia no Hamilton.
        assert "{{PAC_NOME}}" in cliente.gerar_contrato.await_args.kwargs["texto"]

    @pytest.mark.asyncio
    async def test_guarda_barra_antes_de_chamar_o_hamilton(self, session):
        conversa = await _conversa(
            session, dados_coletados={"nome_completo": "Maria", "is_parceria": True}
        )
        cliente = _cliente()
        with patch.object(hamilton_client, "get_hamilton_client", return_value=cliente):
            assert await contrato.garantir(session, conversa, 200) == {}
        cliente.gerar_contrato.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_hamilton_fora_nao_derruba_a_cobranca(self, session):
        """O pagamento é o que não pode parar; o contrato é o que pode faltar."""
        conversa = await _conversa(session)
        with patch.object(hamilton_client, "get_hamilton_client", return_value=_cliente(erro=True)):
            assert await contrato.garantir(session, conversa, 200) == {}

    @pytest.mark.asyncio
    async def test_mesmo_destino_gera_o_mesmo_link_curto(self, session):
        """A Sofia remonta o link a cada turno; slug novo seria endereço novo."""
        conversa = await _conversa(session)
        with patch.object(hamilton_client, "get_hamilton_client", return_value=_cliente()):
            primeiro = await contrato.garantir(session, conversa, 200)
            segundo = await contrato.garantir(session, conversa, 200)
        assert primeiro["link"] == segundo["link"]

    @pytest.mark.asyncio
    async def test_estado_sem_contrato_e_vazio(self, session):
        conversa = await _conversa(session)
        cliente = _cliente(resposta={"status": "nenhum", "contrato_id": None, "link": ""})
        with patch.object(hamilton_client, "get_hamilton_client", return_value=cliente):
            assert await contrato.estado(session, conversa) == {}


class TestOQueOModeloOuve:
    def test_sem_contrato_o_prompt_fica_calado(self):
        """Instrução negativa é convite: 'não mencione contrato' faz ele mencionar."""
        assert contrato.linhas_para_prompt({}) == []

    def test_pendente_manda_o_link_e_as_regras(self):
        linhas = "\n".join(
            contrato.linhas_para_prompt(
                {"status": "pendente", "link": "https://allos.org.br/p/abc"}
            )
        )
        assert "https://allos.org.br/p/abc" in linhas
        assert "MESMA mensagem" in linhas
        # Não pedir CPF na conversa: quem pergunta é o site da assinatura.
        assert "CPF" in linhas
        # Assinar não trava atendimento.
        assert "NÃO é condição" in linhas

    def test_assinado_manda_nao_repetir(self):
        linhas = "\n".join(contrato.linhas_para_prompt({"status": "assinado"}))
        assert "JÁ ASSINADO" in linhas
        assert "de novo" in linhas

    def test_pendente_sem_link_proibe_inventar(self):
        linhas = "\n".join(contrato.linhas_para_prompt({"status": "pendente", "link": ""}))
        assert "NÃO invente" in linhas

    def test_recusado_manda_nao_falar(self):
        linhas = "\n".join(contrato.linhas_para_prompt({"status": "recusado"}))
        assert "NÃO fale de" in linhas


class TestPromptDaCobranca:
    @pytest.mark.asyncio
    async def test_link_do_contrato_entra_no_prompt(self, session):
        from app.services import cobranca

        conversa = await _conversa(session)
        prompt = cobranca.montar_prompt(
            conversa,
            "https://allos.org.br/p/pag",
            {"status": "pendente", "link": "https://allos.org.br/p/con"},
        )
        assert "https://allos.org.br/p/pag" in prompt  # pagamento
        assert "https://allos.org.br/p/con" in prompt  # contrato

    @pytest.mark.asyncio
    async def test_sem_contrato_o_prompt_nao_muda(self, session):
        from app.services import cobranca

        conversa = await _conversa(session)
        assert "ontrato" not in cobranca.montar_prompt(conversa, "https://x/p/1", {})


class TestPreviaNoPainel:
    """ "Ver como fica": o mesmo renderizador do contrato de verdade, com dados falsos."""

    @pytest.mark.asyncio
    async def test_baixa_o_docx(self, session):
        from httpx import ASGITransport, AsyncClient

        from app.main import app

        cliente = AsyncMock()
        cliente.previa_contrato.return_value = b"PK\x03\x04fake-docx"
        with patch.object(hamilton_client, "get_hamilton_client", return_value=cliente):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://t") as http:
                http.cookies.set("session", _sessao_valida())
                resp = await http.post(
                    "/painel/prompts/prompt_contrato/previa",
                    data={"texto": "CONTRATO {{PAC_NOME}}"},
                    headers={"Origin": "http://t"},
                )

        assert resp.status_code == 200
        assert resp.content.startswith(b"PK")
        assert "contrato-previa.docx" in resp.headers["content-disposition"]

    @pytest.mark.asyncio
    async def test_prompt_comum_nao_tem_previa(self, session):
        from httpx import ASGITransport, AsyncClient

        from app.main import app

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as http:
            http.cookies.set("session", _sessao_valida())
            resp = await http.post(
                "/painel/prompts/prompt_sistema/previa",
                data={"texto": "x"},
                headers={"Origin": "http://t"},
            )
        assert resp.status_code == 404


def _sessao_valida() -> str:
    """Cookie de sessão assinado, como o `/login` emite."""
    import base64
    import json

    from itsdangerous import TimestampSigner

    from app.config import settings

    dados = base64.b64encode(json.dumps({"usuario": settings.painel_user}).encode())
    return TimestampSigner(str(settings.secret_key)).sign(dados).decode()


class TestTelaHoje:
    @pytest.mark.asyncio
    async def test_contrato_pendente_aparece(self, session):
        conversa = await _conversa(session)
        cliente = AsyncMock()
        cliente.contratos_pendentes.return_value = [
            {
                "contrato_id": 1,
                "paciente_id": 500,
                "enviado_em": (AGORA - timedelta(days=3)).isoformat(),
                "link": "https://assina.ae/x",
            }
        ]
        itens = await hoje._contratos_pendentes(session, AGORA, cliente)

        assert len(itens) == 1
        assert itens[0]["tipo"] == "contrato_pendente"
        assert itens[0]["conversa_id"] == conversa.id
        assert "3 dias" in itens[0]["texto"]

    @pytest.mark.asyncio
    async def test_desligado_nem_consulta(self, session):
        config_negocio._cache["contrato_ativo"] = False
        await _conversa(session)
        cliente = AsyncMock()
        assert await hoje._contratos_pendentes(session, AGORA, cliente) == []
        cliente.contratos_pendentes.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_hamilton_fora_some_a_linha_e_nao_quebra(self, session):
        """Contrato pendente não é urgência: virar erro na tela seria pior."""
        await _conversa(session)
        cliente = AsyncMock()
        cliente.contratos_pendentes.side_effect = hamilton_client.HamiltonError("502")
        assert await hoje._contratos_pendentes(session, AGORA, cliente) == []

    @pytest.mark.asyncio
    async def test_contrato_perde_pra_cobranca_no_dedupe(self, session):
        """Uma linha por conversa; quem não pagou é mais urgente que quem não assinou."""
        assert hoje.PRIORIDADE["contrato_pendente"] > hoje.PRIORIDADE["cobranca_sem_resposta"]
        assert hoje.PRIORIDADE["contrato_pendente"] > hoje.PRIORIDADE["cobranca_travada"]

    @pytest.mark.asyncio
    async def test_data_ilegivel_nao_estoura(self, session):
        await _conversa(session)
        cliente = AsyncMock()
        cliente.contratos_pendentes.return_value = [
            {"contrato_id": 1, "paciente_id": 500, "enviado_em": "ontem", "link": ""}
        ]
        itens = await hoje._contratos_pendentes(session, AGORA, cliente)
        assert itens[0]["quando"] is None
