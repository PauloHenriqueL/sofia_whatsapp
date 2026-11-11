"""Rede de proteção: a Sofia diz que vai acionar a Thainá e não chama a tool.

Em 11/11/2026, em 3 de 7 conversas de teste, a Sofia escreveu algo como "vou
confirmar com a Thainá, ela te responde em pouco tempo" e encerrou o turno sem
chamar `escalar_para_thaina`: conversa em modo bot, nenhuma escalada, nenhum
alerta. A pessoa fica esperando e ninguém sabe.

Estes testes cobrem o detector (o que dispara e, mais importante, o que NÃO
dispara) e os três modos em que a rede foi ligada. Cobrem também o bug vizinho
achado no mesmo dia: no modo pesquisa a tool de escalada era recusada em silêncio.
"""

from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base
from app.models import Conversa, Escalada
from app.routers import webhook
from app.services import cobranca, escalation, llm_client, pesquisa

# As três falas reais de 11/11, mais variações do mesmo erro.
PROMETEM = [
    "Vou confirmar com a Thainá, nossa coordenadora clínica, se existe alguma parceria "
    "para a rede municipal do Recife. Ela te responde em pouco tempo.",
    "No cartão, a cobrança é automática todo mês. Sobre o dia e como parar, vou pedir "
    "para a Thainá te orientar certinho.",
    "Vou chamar a Thainá pra continuar daqui contigo.",
    "Já avisei a Thainá.",
    "Tô passando seu caso pra Thainá agora.",
    "Isso eu vou ver com a Thainá e te falo.",
    "Vou marcar uma conversa com a Amanda, nossa coordenadora de neuropsicologia.",
]

# Falas legítimas que citam a Thainá e NÃO são promessa de escalada. A primeira é
# a mais comum da Sofia (o aviso depois de todo cadastro): se ela disparasse, todo
# paciente cadastrado viraria escalada.
NAO_PROMETEM = [
    "Pronto, deixei tudo registrado. A Thainá, coordenadora clínica da Allos, vai ver "
    "qual terapeuta da equipe tem horário que combina com o seu e te chama aqui no "
    "WhatsApp em até 36 horas.",
    "Posso anotar sua preferência pra Thainá considerar no encaixe. Você quer seguir?",
    "Já vou anotar o que você me falou pra ela levar em conta.",
    "Quer que eu marque uma conversa com a Amanda?",
    "Quem faz esse encaixe é a Thainá. Ela te chama aqui antes da primeira sessão.",
    "Sobre isso eu não consigo te dizer daqui, quem vê isso é a Thainá.",
    "Vou te explicar como funciona. A Thainá entra em contato depois.",
    "Se preferir, eu posso chamar a Thainá.",
    "Vou deixar anotado pra Thainá levar em conta.",
    "",
    None,
]


@pytest.mark.parametrize("fala", PROMETEM)
def test_detecta_promessa_em_primeira_pessoa(fala):
    assert escalation.promessa_de_acionar(fala) is not None


@pytest.mark.parametrize("fala", NAO_PROMETEM)
def test_nao_confunde_com_fala_legitima(fala):
    assert escalation.promessa_de_acionar(fala) is None


def test_devolve_a_frase_que_prometeu_e_quem():
    frase, quem = escalation.promessa_de_acionar(
        "A mensalidade é R$ 200. Vou confirmar com a Thainá se dá pra mudar o dia. "
        "Ela te responde em pouco tempo."
    )
    assert frase == "Vou confirmar com a Thainá se dá pra mudar o dia."
    assert quem == "thaina"


# --------------------------------------------------------------------------- #
# Os três modos
# --------------------------------------------------------------------------- #


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


async def _conversa(db, **kw) -> Conversa:
    c = Conversa(numero_whatsapp="5531900000999", modo="bot", estado="novo", **kw)
    db.add(c)
    await db.commit()
    return c


async def _escaladas(db, conversa) -> list[Escalada]:
    return list(
        (await db.execute(select(Escalada).where(Escalada.conversa_id == conversa.id))).scalars()
    )


class _LLMFixo:
    """Devolve as respostas na ordem, uma por chamada."""

    def __init__(self, *respostas):
        self._respostas = list(respostas)

    async def gerar_resposta(self, historico, tools=None, **kwargs):
        return self._respostas.pop(0)


def _so_texto(texto):
    return llm_client.LLMResposta(texto=texto)


def _tool(nome, **argumentos):
    return llm_client.LLMResposta(
        tool_calls=[llm_client.ToolCall(id="call_1", name=nome, arguments=argumentos)]
    )


class TestAcolhimento:
    @pytest.mark.asyncio
    async def test_promessa_sem_tool_vira_escalada_com_alerta(self, db):
        conversa = await _conversa(db)
        fake = _LLMFixo(_so_texto(PROMETEM[0]))
        with patch("app.routers.webhook.llm_client.get_llm_client", return_value=fake), patch(
            "app.routers.webhook.captacao.listar", new_callable=AsyncMock, return_value=[]
        ), patch(
            "app.services.escalation.alertar_thaina", new_callable=AsyncMock, return_value=True
        ) as alerta:
            texto = await webhook.processar_turno_bot(db, conversa)

        assert texto == PROMETEM[0]  # a fala sai igual: a pessoa não percebe a rede
        assert conversa.modo == "humano"
        assert conversa.estado == "escalado"
        (escalada,) = await _escaladas(db, conversa)
        assert escalada.motivo == "outro"
        assert "Vou confirmar com a Thainá" in escalada.contexto
        alerta.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_fala_comum_nao_escala(self, db):
        conversa = await _conversa(db)
        fake = _LLMFixo(_so_texto(NAO_PROMETEM[0]))
        with patch("app.routers.webhook.llm_client.get_llm_client", return_value=fake), patch(
            "app.routers.webhook.captacao.listar", new_callable=AsyncMock, return_value=[]
        ), patch("app.services.escalation.alertar_thaina", new_callable=AsyncMock) as alerta:
            await webhook.processar_turno_bot(db, conversa)

        assert conversa.modo == "bot"
        assert await _escaladas(db, conversa) == []
        alerta.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_quando_o_modelo_escala_certo_nao_duplica(self, db):
        conversa = await _conversa(db)
        fake = _LLMFixo(
            _tool("escalar_para_thaina", motivo="pedido_humano"),
            _so_texto("Vou chamar a Thainá pra continuar daqui contigo."),
        )
        with patch("app.routers.webhook.llm_client.get_llm_client", return_value=fake), patch(
            "app.routers.webhook.captacao.listar", new_callable=AsyncMock, return_value=[]
        ), patch(
            "app.services.escalation.alertar_thaina", new_callable=AsyncMock, return_value=True
        ) as alerta:
            await webhook.processar_turno_bot(db, conversa)

        (escalada,) = await _escaladas(db, conversa)
        assert escalada.motivo == "pedido_humano"
        alerta.assert_awaited_once()


class TestCobranca:
    @pytest.mark.asyncio
    async def test_promessa_sem_tool_escala_e_encerra_a_cobranca(self, db):
        conversa = await _conversa(db, cobranca_iniciada_em=cobranca._agora())
        fake = _LLMFixo(_so_texto(PROMETEM[1]))
        with patch("app.services.cobranca.llm_client.get_llm_client", return_value=fake), patch(
            "app.services.escalation.alertar_thaina", new_callable=AsyncMock, return_value=True
        ) as alerta:
            texto = await cobranca._turno(db, conversa, link=None, historico=[])

        assert texto == PROMETEM[1]
        (escalada,) = await _escaladas(db, conversa)
        assert escalada.motivo == "outro"
        assert conversa.cobranca_status == "escalada"
        assert not cobranca.em_cobranca(conversa)
        alerta.assert_awaited_once()


class TestPesquisa:
    AVALIACAO = {"pk_avaliacao": 1, "momento": "entrada", "paciente_nome": "Ana"}

    @pytest.mark.asyncio
    async def test_tool_de_escalada_agora_escala_de_verdade(self, db):
        """Antes a tool caía em `_registrar_resposta` e era recusada em silêncio."""
        conversa = await _conversa(db, pesquisa_avaliacao_id=1)
        fake = _LLMFixo(
            _tool("escalar_para_thaina", motivo="crise", contexto="disse que quer morrer"),
            _so_texto("Vou avisar a Thainá agora. O CVV atende no 188, 24 horas."),
        )
        with patch("app.services.pesquisa.llm_client.get_llm_client", return_value=fake), patch(
            "app.services.pesquisa.montar_prompt", return_value="prompt"
        ), patch(
            "app.services.escalation.alertar_thaina", new_callable=AsyncMock, return_value=True
        ) as alerta:
            await pesquisa._turno(conversa, self.AVALIACAO, historico=[], db=db)

        (escalada,) = await _escaladas(db, conversa)
        assert escalada.motivo == "crise"
        assert conversa.modo == "humano"
        assert conversa._escalou_na_pesquisa is True
        alerta.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_promessa_sem_tool_tambem_escala(self, db):
        conversa = await _conversa(db, pesquisa_avaliacao_id=1)
        fake = _LLMFixo(_so_texto("Vou avisar a Thainá agora."))
        with patch("app.services.pesquisa.llm_client.get_llm_client", return_value=fake), patch(
            "app.services.pesquisa.montar_prompt", return_value="prompt"
        ), patch(
            "app.services.escalation.alertar_thaina", new_callable=AsyncMock, return_value=True
        ):
            await pesquisa._turno(conversa, self.AVALIACAO, historico=[], db=db)

        (escalada,) = await _escaladas(db, conversa)
        assert escalada.motivo == "outro"
        assert conversa._escalou_na_pesquisa is True
