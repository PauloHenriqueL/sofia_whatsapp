"""Resgate de quem passou os dados e sumiu antes de confirmar.

O que este módulo precisa provar não é o caminho feliz: é **que ele não cadastra
ninguém**. O desenho anterior gravava direto no Hamilton, e foi trocado justamente
porque dá pra saber que a pessoa passou os dados, não que ela quis ser cadastrada.
Se alguém reintroduzir a chamada ao `cadastro`, `test_nao_cadastra_no_hamilton`
quebra.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base
from app.models import Conversa, Escalada, Mensagem
from app.services import cadastro, cadastro_abandonado, hoje, llm_client

AGORA = datetime(2026, 8, 18, 12, 0, 0, tzinfo=timezone.utc)


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


async def _conversa(session, horas_de_silencio=30, **kwargs):
    dados = {"numero_whatsapp": "5531999990000", "estado": "novo", "modo": "bot"}
    dados.update(kwargs)
    conversa = Conversa(**dados)
    session.add(conversa)
    await session.flush()
    session.add(
        Mensagem(
            conversa_id=conversa.id,
            direcao="recebida",
            origem="paciente",
            tipo="texto",
            texto="meu nome é Maria Silva, nasci em 12/03/1992",
            criada_em=AGORA - timedelta(hours=horas_de_silencio),
        )
    )
    await session.flush()
    return conversa


def _llm(texto):
    cliente = AsyncMock()
    cliente.gerar_resposta.return_value = SimpleNamespace(texto=texto, tool_calls=[])
    return cliente


EXTRACAO_OK = (
    '{"nome_completo": "Maria Silva", "data_nascimento": "1992-03-12",'
    ' "horarios_disponiveis": "à noite"}'
)


class TestQuemEntraNaFila:
    @pytest.mark.asyncio
    async def test_quem_falou_ha_pouco_nao_entra(self, session):
        """Conversa viva segue com a Sofia; mexer nela é atropelar."""
        await _conversa(session, horas_de_silencio=3)
        assert await cadastro_abandonado.buscar_abandonadas(session, AGORA) == []

    @pytest.mark.asyncio
    async def test_com_escalada_aberta_nao_entra(self, session):
        conversa = await _conversa(session)
        session.add(Escalada(conversa_id=conversa.id, motivo="gratuidade"))
        await session.flush()
        assert await cadastro_abandonado.buscar_abandonadas(session, AGORA) == []

    @pytest.mark.asyncio
    async def test_escalada_ja_resolvida_nao_barra(self, session):
        conversa = await _conversa(session)
        session.add(Escalada(conversa_id=conversa.id, motivo="gratuidade", resolvida_em=AGORA))
        await session.flush()
        assert len(await cadastro_abandonado.buscar_abandonadas(session, AGORA)) == 1

    @pytest.mark.asyncio
    async def test_modo_humano_nao_entra(self, session):
        await _conversa(session, modo="humano")
        assert await cadastro_abandonado.buscar_abandonadas(session, AGORA) == []

    @pytest.mark.asyncio
    async def test_arquivada_nao_entra(self, session):
        await _conversa(session, arquivada_em=AGORA)
        assert await cadastro_abandonado.buscar_abandonadas(session, AGORA) == []

    @pytest.mark.asyncio
    async def test_ja_cadastrada_nao_entra(self, session):
        await _conversa(session, paciente_hamilton_id=42, estado="cadastrado")
        assert await cadastro_abandonado.buscar_abandonadas(session, AGORA) == []

    @pytest.mark.asyncio
    async def test_ja_tentada_nao_entra_de_novo(self, session):
        """Sem isto o cron gasta uma chamada ao modelo por conversa, pra sempre."""
        await _conversa(
            session,
            dados_coletados={cadastro_abandonado.CHAVE_TENTATIVA: AGORA.isoformat()},
        )
        assert await cadastro_abandonado.buscar_abandonadas(session, AGORA) == []

    @pytest.mark.asyncio
    async def test_abandonada_de_verdade_entra(self, session):
        await _conversa(session)
        assert len(await cadastro_abandonado.buscar_abandonadas(session, AGORA)) == 1


class TestExtracao:
    def test_sem_nome_ou_nascimento_devolve_vazio(self):
        """Meia ficha na fila faz alguém abrir, perder tempo e fechar."""
        assert cadastro_abandonado._normalizar('{"nome_completo": "Maria"}') == {}
        assert cadastro_abandonado._normalizar('{"data_nascimento": "1992-03-12"}') == {}

    def test_campo_inventado_e_descartado(self):
        d = cadastro_abandonado._normalizar(
            '{"nome_completo": "Maria Silva", "data_nascimento": "1992-03-12",'
            ' "cpf": "111", "signo": "peixes"}'
        )
        assert set(d) == {"nome_completo", "data_nascimento"}

    def test_json_em_cerca_de_markdown(self):
        d = cadastro_abandonado._normalizar(f"```json\n{EXTRACAO_OK}\n```")
        assert d["nome_completo"] == "Maria Silva"

    def test_lixo_nao_estoura(self):
        assert cadastro_abandonado._normalizar("não achei nada") == {}
        assert cadastro_abandonado._normalizar(None) == {}


class TestRodada:
    @pytest.mark.asyncio
    async def test_nao_cadastra_no_hamilton(self, session):
        """🔴 A garantia central: a rodada NUNCA chama o cadastro.

        Quem clica é gente, na tela Hoje. Se este teste quebrar, alguém
        reintroduziu a gravação automática no prontuário.
        """
        conversa = await _conversa(session)
        with patch.object(llm_client, "get_llm_client", return_value=_llm(EXTRACAO_OK)):
            with patch.object(cadastro, "cadastrar_paciente", new=AsyncMock()) as criar:
                resumo = await cadastro_abandonado.rodar_resgates(session, AGORA)

        criar.assert_not_awaited()
        assert resumo == {"avaliadas": 1, "prontas": 1}
        await session.refresh(conversa)
        assert conversa.paciente_hamilton_id is None
        assert conversa.estado == "novo"

    @pytest.mark.asyncio
    async def test_ficha_pronta_diz_que_ninguem_confirmou(self, session):
        conversa = await _conversa(session)
        with patch.object(llm_client, "get_llm_client", return_value=_llm(EXTRACAO_OK)):
            await cadastro_abandonado.rodar_resgates(session, AGORA)

        await session.refresh(conversa)
        assert conversa.dados_coletados["nome_completo"] == "Maria Silva"
        assert cadastro_abandonado.NOTA_OBSERVACAO in conversa.dados_coletados["observacoes"]
        assert conversa.dados_coletados[cadastro_abandonado.CHAVE_PRONTO]

    @pytest.mark.asyncio
    async def test_extracao_vazia_nao_vira_linha_na_fila(self, session):
        """Tentativa que não achou nada marca, mas não enche a tela da Thainá."""
        conversa = await _conversa(session)
        with patch.object(llm_client, "get_llm_client", return_value=_llm("{}")):
            resumo = await cadastro_abandonado.rodar_resgates(session, AGORA)

        assert resumo == {"avaliadas": 1, "prontas": 0}
        await session.refresh(conversa)
        assert conversa.dados_coletados[cadastro_abandonado.CHAVE_TENTATIVA]
        assert cadastro_abandonado.CHAVE_PRONTO not in conversa.dados_coletados
        assert await cadastro_abandonado.aguardando_confirmacao(session) == []

    @pytest.mark.asyncio
    async def test_uma_conversa_ruim_nao_derruba_a_rodada(self, session):
        await _conversa(session)
        await _conversa(session, numero_whatsapp="5531999990001")
        cliente = AsyncMock()
        cliente.gerar_resposta.side_effect = [
            RuntimeError("boom"),
            SimpleNamespace(texto=EXTRACAO_OK, tool_calls=[]),
        ]
        with patch.object(llm_client, "get_llm_client", return_value=cliente):
            resumo = await cadastro_abandonado.rodar_resgates(session, AGORA)

        assert resumo["avaliadas"] == 2
        assert resumo["prontas"] == 1


class TestFilaDeConfirmacao:
    @pytest.mark.asyncio
    async def test_aparece_na_tela_hoje(self, session):
        await _conversa(session)
        with patch.object(llm_client, "get_llm_client", return_value=_llm(EXTRACAO_OK)):
            await cadastro_abandonado.rodar_resgates(session, AGORA)

        pendencias = await hoje.listar_pendencias(session)
        linhas = [p for p in pendencias if p["tipo"] == "cadastro_a_confirmar"]
        assert len(linhas) == 1
        assert linhas[0]["nome"] == "Maria Silva"

    @pytest.mark.asyncio
    async def test_sai_da_fila_depois_de_cadastrada(self, session):
        """Não há estado a limpar: ganhar `paciente_hamilton_id` já resolve."""
        conversa = await _conversa(session)
        with patch.object(llm_client, "get_llm_client", return_value=_llm(EXTRACAO_OK)):
            await cadastro_abandonado.rodar_resgates(session, AGORA)
        assert len(await cadastro_abandonado.aguardando_confirmacao(session)) == 1

        conversa.paciente_hamilton_id = 99
        conversa.estado = "cadastrado"
        await session.flush()
        assert await cadastro_abandonado.aguardando_confirmacao(session) == []

    @pytest.mark.asyncio
    async def test_pedido_de_presencial_sobrevive_na_observacao(self, session):
        """O presencial deixou de escalar; some se a observação for sobrescrita."""
        await _conversa(session)
        extracao = (
            '{"nome_completo": "Maria Silva", "data_nascimento": "1992-03-12",'
            ' "observacoes": "Pediu atendimento presencial"}'
        )
        with patch.object(llm_client, "get_llm_client", return_value=_llm(extracao)):
            await cadastro_abandonado.rodar_resgates(session, AGORA)

        [conversa] = await cadastro_abandonado.aguardando_confirmacao(session)
        assert "presencial" in conversa.dados_coletados["observacoes"]
        assert cadastro_abandonado.NOTA_OBSERVACAO in conversa.dados_coletados["observacoes"]
