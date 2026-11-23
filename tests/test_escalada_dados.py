"""Dados antes de escalar: a Sofia pede uma vez, e só depois passa pra Thainá.

Em 23/11/2026 a pessoa escreveu "quero falar com uma pessoa, não com um robô" e
a Sofia escalou na hora, sem pedir nem o nome. O prompt já mandava pedir; o
modelo leu o pedido de humano como recusa. A regra passou para o código
(`escalation.faltam_dados`), e estes testes seguram as quatro garantias:

1. primeira tentativa sem os dados não escala: pede;
2. a tentativa seguinte escala, com ou sem os dados (uma vez só);
3. crise, menor de 11, recusa explícita e paciente já cadastrado não esperam;
4. a rede da promessa ("vou passar pra Thainá" sem tool) respeita a mesma regra.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base
from app.models import Conversa, Escalada
from app.routers import webhook
from app.services import escalation, llm_client, painel

DADOS = {
    "nome_completo": "Maria da Silva",
    "data_nascimento": "1990-03-12",
    "horarios_disponiveis": "terça à noite",
}


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
    c = Conversa(numero_whatsapp="5531900000777", modo="bot", estado="novo", **kw)
    db.add(c)
    await db.commit()
    return c


async def _escaladas(db, conversa) -> list[Escalada]:
    return list(
        (await db.execute(select(Escalada).where(Escalada.conversa_id == conversa.id))).scalars()
    )


class _LLM:
    """Devolve as respostas na ordem e guarda o que recebeu."""

    def __init__(self, *respostas):
        self._respostas = list(respostas)
        self.historicos: list[list[dict]] = []

    async def gerar_resposta(self, historico, tools=None, **kwargs):
        self.historicos.append(historico)
        return self._respostas.pop(0)


def _texto(t):
    return llm_client.LLMResposta(texto=t)


def _escalar(**argumentos):
    return llm_client.LLMResposta(
        tool_calls=[
            llm_client.ToolCall(id="call_1", name="escalar_para_thaina", arguments=argumentos)
        ]
    )


PEDIDO = (
    "Consigo, sim. Pra Thainá chegar sabendo, me manda seu nome completo, sua data "
    "de nascimento e seus horários livres? Assim que você mandar, eu passo a conversa pra ela."
)


async def _turno(db, conversa, fake):
    with patch("app.routers.webhook.llm_client.get_llm_client", return_value=fake), patch(
        "app.routers.webhook.captacao.listar", new_callable=AsyncMock, return_value=[]
    ), patch(
        "app.services.escalation.alertar_thaina", new_callable=AsyncMock, return_value=True
    ) as alerta:
        texto = await webhook.processar_turno_bot(db, conversa)
    return texto, alerta


class TestPedeAntes:
    @pytest.mark.asyncio
    async def test_pedido_de_humano_sem_dados_nao_escala_e_pede(self, db):
        """O caso de 23/11."""
        conversa = await _conversa(db)
        fake = _LLM(_escalar(motivo="pedido_humano"), _texto(PEDIDO))

        texto, alerta = await _turno(db, conversa, fake)

        assert texto == PEDIDO
        assert conversa.modo == "bot"
        assert await _escaladas(db, conversa) == []
        alerta.assert_not_awaited()
        assert escalation.escalada_pendente(conversa)["motivo"] == "pedido_humano"
        # O modelo recebeu o que falta e a instrução de pedir.
        resultado_tool = fake.historicos[1][-1]["content"]
        assert "faltam_dados" in resultado_tool
        assert "nome completo" in resultado_tool

    @pytest.mark.asyncio
    async def test_pede_so_o_que_falta(self, db):
        conversa = await _conversa(db, dados_coletados={"nome_completo": "Maria da Silva"})
        fake = _LLM(_escalar(motivo="preco"), _texto(PEDIDO))

        await _turno(db, conversa, fake)

        resultado_tool = fake.historicos[1][-1]["content"]
        assert "nome completo" not in resultado_tool
        assert "data de nascimento" in resultado_tool

    @pytest.mark.asyncio
    async def test_dados_que_vem_na_tool_vao_pra_ficha(self, db):
        conversa = await _conversa(db)
        fake = _LLM(_escalar(motivo="pedido_humano", **DADOS), _texto("Passei pra Thainá."))

        await _turno(db, conversa, fake)

        assert conversa.modo == "humano"
        for campo, valor in DADOS.items():
            assert conversa.dados_coletados[campo] == valor
        assert escalation.CHAVE_PENDENTE not in conversa.dados_coletados

    @pytest.mark.asyncio
    async def test_sem_fala_do_modelo_sai_o_pedido_fixo(self, db):
        conversa = await _conversa(db)
        fake = _LLM(_escalar(motivo="pedido_humano"), _texto(None))

        texto, _ = await _turno(db, conversa, fake)

        assert texto.startswith("Pra Thainá, nossa coordenadora clínica")
        assert "seu nome completo, sua data de nascimento e os horários" in texto


class TestUmaVezSo:
    @pytest.mark.asyncio
    async def test_turno_seguinte_escala_pelo_modelo(self, db):
        conversa = await _conversa(db)
        await _turno(db, conversa, _LLM(_escalar(motivo="pedido_humano"), _texto(PEDIDO)))

        # A pessoa recusa: o modelo chama de novo, sem dado nenhum.
        fake = _LLM(
            _escalar(motivo="pedido_humano"),
            _texto("Tudo bem. Vou passar a conversa pra Thainá agora."),
        )
        texto, alerta = await _turno(db, conversa, fake)

        assert conversa.modo == "humano"
        (escalada,) = await _escaladas(db, conversa)
        assert escalada.motivo == "pedido_humano"
        alerta.assert_awaited_once()
        assert texto == "Tudo bem. Vou passar a conversa pra Thainá agora."
        # O modelo foi lembrado de que já pediu.
        assert any(
            m["role"] == "system" and "Não peça os dados de novo" in m["content"]
            for m in fake.historicos[0]
        )

    @pytest.mark.asyncio
    async def test_turno_seguinte_escala_mesmo_se_o_modelo_esquecer(self, db):
        """O modelo não chama a tool nem promete nada: o código escala."""
        conversa = await _conversa(db)
        await _turno(
            db, conversa, _LLM(_escalar(motivo="gratuidade", contexto="sem renda"), _texto(PEDIDO))
        )

        texto, alerta = await _turno(db, conversa, _LLM(_texto("Obrigada, Maria!")))

        assert conversa.modo == "humano"
        (escalada,) = await _escaladas(db, conversa)
        assert escalada.motivo == "gratuidade"
        assert "sem renda" in escalada.contexto
        alerta.assert_awaited_once()
        assert texto == f"Obrigada, Maria!\n\n{escalation.AVISO_PASSEI}"
        assert escalation.escalada_pendente(conversa) is None

    @pytest.mark.asyncio
    async def test_pendente_velha_nao_vale(self, db):
        velha = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
        conversa = await _conversa(
            db,
            dados_coletados={
                escalation.CHAVE_PENDENTE: {"motivo": "preco", "contexto": None, "pedido_em": velha}
            },
        )
        assert escalation.escalada_pendente(conversa) is None

        _, alerta = await _turno(db, conversa, _LLM(_texto("Oi de novo!")))

        assert conversa.modo == "bot"
        alerta.assert_not_awaited()


class TestNaoEspera:
    @pytest.mark.parametrize("motivo", ["crise", "menor_11"])
    @pytest.mark.asyncio
    async def test_motivos_que_escalam_na_hora(self, db, motivo):
        conversa = await _conversa(db)
        fake = _LLM(_escalar(motivo=motivo), _texto("Vou chamar a Thainá agora."))

        await _turno(db, conversa, fake)

        assert conversa.modo == "humano"
        (escalada,) = await _escaladas(db, conversa)
        assert escalada.motivo == motivo

    @pytest.mark.asyncio
    async def test_recusa_explicita_escala_na_hora(self, db):
        conversa = await _conversa(db)
        fake = _LLM(
            _escalar(motivo="pedido_humano", recusou_passar_dados=True),
            _texto("Tudo bem, vou passar pra Thainá."),
        )

        await _turno(db, conversa, fake)

        assert conversa.modo == "humano"

    @pytest.mark.asyncio
    async def test_paciente_ja_cadastrado_escala_na_hora(self, db):
        conversa = await _conversa(db, paciente_hamilton_id=123)
        fake = _LLM(_escalar(motivo="preco"), _texto("Vou passar pra Thainá."))

        await _turno(db, conversa, fake)

        assert conversa.modo == "humano"


class TestRedeDaPromessa:
    @pytest.mark.asyncio
    async def test_promessa_sem_dados_vira_pedido(self, db):
        """A fala do print: prometeu a Thainá, sem tool e sem dado nenhum."""
        conversa = await _conversa(db)
        fala = (
            "Entendo. Vou passar a conversa pra Thainá, nossa coordenadora clínica, "
            "e ela te responde por aqui em pouco tempo."
        )

        texto, alerta = await _turno(db, conversa, _LLM(_texto(fala)))

        assert conversa.modo == "bot"
        alerta.assert_not_awaited()
        assert "Vou passar a conversa" not in texto  # a promessa saiu
        assert texto.startswith("Entendo.\n\nPra Thainá")
        assert escalation.escalada_pendente(conversa)["motivo"] == "outro"

        # Na volta, escala.
        await _turno(db, conversa, _LLM(_texto("Obrigada!")))
        assert conversa.modo == "humano"

    @pytest.mark.asyncio
    async def test_pedido_do_modelo_nao_e_tomado_como_promessa(self, db):
        """Depois de `faltam_dados`, a fala "vou passar pra Thainá assim que você
        mandar" é o pedido, não uma promessa a cumprir agora."""
        conversa = await _conversa(db)
        fake = _LLM(
            _escalar(motivo="pedido_humano"),
            _texto("Me manda seu nome? Vou passar pra Thainá assim que você mandar."),
        )

        _, alerta = await _turno(db, conversa, fake)

        assert conversa.modo == "bot"
        alerta.assert_not_awaited()


@pytest.mark.asyncio
async def test_assumir_pelo_painel_limpa_a_pendente(db):
    conversa = await _conversa(db)
    escalation.marcar_pendente(conversa, "preco", None)

    await painel.assumir(db, conversa)

    assert escalation.escalada_pendente(conversa) is None


def test_guardar_ignora_campo_fora_da_lista_e_vazio():
    conversa = Conversa(numero_whatsapp="1", dados_coletados={"nome_completo": "Ana Souza"})
    escalation.guardar_dados_da_escalada(
        conversa, {"nome_completo": "  ", "motivo": "preco", "data_nascimento": "2000-01-02"}
    )
    assert conversa.dados_coletados == {"nome_completo": "Ana Souza", "data_nascimento": "2000-01-02"}


@pytest.mark.asyncio
async def test_sinal_de_crise_nao_segura_nem_a_rede(db):
    """Quem disse "não quero mais viver" não pode receber pedido de data de
    nascimento, mesmo que o modelo erre o motivo ou só prometa a Thainá."""
    from app.models import Mensagem

    conversa = await _conversa(db)
    db.add(
        Mensagem(
            conversa_id=conversa.id,
            direcao="recebida",
            origem="paciente",
            tipo="texto",
            texto="não quero mais viver",
        )
    )
    await db.commit()

    texto, alerta = await _turno(
        db, conversa, _LLM(_texto("Tô aqui com você. Já estou avisando a Thainá."))
    )

    assert texto == "Tô aqui com você. Já estou avisando a Thainá."
    assert conversa.modo == "humano"
    alerta.assert_awaited_once()
