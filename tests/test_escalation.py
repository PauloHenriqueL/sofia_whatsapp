"""Testes da escalada: motivos novos + rótulo legível no alerta da Thainá."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base
from app.models import Conversa, Escalada
from app.services import escalation
from app.services import painel as painel_service
from app.services import tools, usuarios, whatsapp_client


@pytest_asyncio.fixture
async def db_com_usuario_de_alerta():
    """Banco em memória com um usuário `recebe_alertas=True`.

    `escalation._enviar_alerta` busca destinatários no banco em vez de um
    número fixo — os testes de alerta precisam de alguém cadastrado pra
    `ok is True` fazer sentido.
    """
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as s:
        await usuarios.criar(
            s,
            nome="Thainá",
            username="thaina",
            senha="x",
            telefone_whatsapp="5531999990000",
            recebe_alertas=True,
        )
        await s.commit()
    async with maker() as s:
        yield s
    await engine.dispose()


def test_todo_motivo_da_escalada_tem_rotulo():
    for motivo in tools.MOTIVOS_ESCALADA:
        assert motivo in tools.MOTIVO_LABELS, f"motivo sem rótulo: {motivo}"


def test_motivos_novos_disponiveis_pro_modelo():
    # Motivos que o LLM pode escolher (inclui os roteamentos da v2).
    for motivo in ("neuro_reuniao", "preco", "menor_11", "crise"):
        assert motivo in tools.MOTIVOS_ESCALADA


def test_presencial_nao_e_mais_escalada():
    """Pedir presencial não tira a pessoa do fluxo.

    A Sofia cadastra normalmente e registra o pedido em `observacoes`, que é o
    que a coordenação lê no match. Escalar punha a conversa em modo humano por
    um pedido que não exige decisão na hora. O RÓTULO continua existindo: há
    escaladas antigas com esse motivo gravadas no banco, e sem ele a tela da
    Thainá mostraria o código cru.
    """
    assert "presencial" not in tools.MOTIVOS_ESCALADA
    assert "presencial" in tools.MOTIVO_LABELS


@pytest.mark.asyncio
async def test_alerta_envia_rotulo_legivel_e_nao_o_codigo(db_com_usuario_de_alerta):
    conversa = Conversa(
        numero_whatsapp="5531999990000",
        dados_coletados={"nome_completo": "Ana"},
    )
    with patch(
        "app.services.escalation.whatsapp_client.enviar_template",
        new_callable=AsyncMock,
    ) as mock_template:
        ok = await escalation.alertar_thaina(db_com_usuario_de_alerta, conversa, "neuro_reuniao")

    assert ok is True
    parametros = mock_template.await_args.kwargs["parametros"]
    assert parametros == ["Ana", tools.MOTIVO_LABELS["neuro_reuniao"]]


class TestResolverEscaladas:
    """`Escalada.resolvida_em` existia mas nunca era preenchido em produção.

    Como `pesquisa._abrir_entradas` exclui conversa com escalada aberta, quem foi
    escalado uma vez — áudio, anexo, preço, gratuidade, pedido de humano — ficava
    fora da pesquisa de linha de base PARA SEMPRE, sem sintoma nenhum.
    """

    @pytest_asyncio.fixture
    async def session(self):
        engine = create_async_engine("sqlite+aiosqlite://")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with maker() as s:
            yield s
        await engine.dispose()

    @pytest.mark.asyncio
    async def test_devolver_ao_bot_fecha_as_escaladas(self, session):
        conversa = Conversa(numero_whatsapp="5531999998888")
        session.add(conversa)
        await session.flush()
        await escalation.registrar_escalada(session, conversa, "preco")
        await escalation.registrar_escalada(session, conversa, "pedido_humano")
        await session.commit()

        await painel_service.devolver_ao_bot(session, conversa)

        abertas = (
            (
                await session.execute(
                    select(Escalada).where(
                        Escalada.conversa_id == conversa.id, Escalada.resolvida_em.is_(None)
                    )
                )
            )
            .scalars()
            .all()
        )
        assert abertas == []
        assert conversa.modo == "bot"

    @pytest.mark.asyncio
    async def test_arquivar_fecha_escalada_e_zera_o_aviso(self, session):
        """Arquivar devolvia ao bot sem zerar `aviso_escalada_em`.

        Numa escalada posterior, `_avisar_escalada_uma_vez` caía no early-return e
        o paciente ficava em silêncio TOTAL: nem a Sofia, nem o aviso.
        """
        conversa = Conversa(numero_whatsapp="5531999998888")
        session.add(conversa)
        await session.flush()
        await escalation.registrar_escalada(session, conversa, "anexo_recebido")
        conversa.aviso_escalada_em = datetime.now(timezone.utc)
        await session.commit()

        await painel_service.arquivar(session, conversa)

        assert conversa.aviso_escalada_em is None
        assert conversa.modo == "bot"
        aberta = (
            await session.execute(
                select(Escalada).where(
                    Escalada.conversa_id == conversa.id, Escalada.resolvida_em.is_(None)
                )
            )
        ).scalar_one_or_none()
        assert aberta is None


class TestAlertarCadastro:
    """A Thainá precisa saber que entrou paciente, sem abrir o painel."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "status,esperado",
        [
            ("cadastrado", "paciente novo cadastrado no Hamilton (ficha 42)"),
            ("atualizado", "paciente já conhecido voltou; ficha 42 atualizada"),
            ("cadastro_pendente", "CADASTRO FALHOU — precisa cadastrar à mão no Hamilton"),
        ],
    )
    async def test_manda_o_rotulo_certo_por_status(self, status, esperado, db_com_usuario_de_alerta):
        conversa = Conversa(
            numero_whatsapp="5531999998888", dados_coletados={"nome_completo": "Maria"}
        )
        with patch(
            "app.services.escalation.whatsapp_client.enviar_template", new_callable=AsyncMock
        ) as mock_tpl:
            ok = await escalation.alertar_cadastro(
                db_com_usuario_de_alerta, conversa, {"status": status, "paciente_id": 42}
            )
        assert ok is True
        assert mock_tpl.await_args.kwargs["parametros"] == ["Maria", esperado]

    @pytest.mark.asyncio
    async def test_status_desconhecido_nao_manda_nada(self, db_com_usuario_de_alerta):
        conversa = Conversa(numero_whatsapp="5531999998888")
        with patch(
            "app.services.escalation.whatsapp_client.enviar_template", new_callable=AsyncMock
        ) as mock_tpl:
            assert (
                await escalation.alertar_cadastro(
                    db_com_usuario_de_alerta, conversa, {"status": "sei_la"}
                )
                is False
            )
        mock_tpl.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_sem_nome_usa_o_numero(self, db_com_usuario_de_alerta):
        conversa = Conversa(numero_whatsapp="5531999998888", dados_coletados={})
        with patch(
            "app.services.escalation.whatsapp_client.enviar_template", new_callable=AsyncMock
        ) as mock_tpl:
            await escalation.alertar_cadastro(
                db_com_usuario_de_alerta, conversa, {"status": "cadastrado", "paciente_id": 1}
            )
        assert mock_tpl.await_args.kwargs["parametros"][0] == "5531999998888"

    @pytest.mark.asyncio
    async def test_falha_do_template_nao_derruba_o_cadastro(self, db_com_usuario_de_alerta):
        """O cadastro já aconteceu; o alerta é conveniência."""
        conversa = Conversa(numero_whatsapp="5531999998888")
        with patch(
            "app.services.escalation.whatsapp_client.enviar_template",
            new_callable=AsyncMock,
            side_effect=whatsapp_client.WhatsAppError("fora do ar"),
        ):
            assert (
                await escalation.alertar_cadastro(
                    db_com_usuario_de_alerta, conversa, {"status": "cadastrado"}
                )
                is False
            )

    @pytest.mark.asyncio
    async def test_nao_loga_o_nome_do_paciente(self, caplog, db_com_usuario_de_alerta):
        import logging

        conversa = Conversa(
            numero_whatsapp="5531999998888", dados_coletados={"nome_completo": "Joana Prado"}
        )
        with caplog.at_level(logging.ERROR), patch(
            "app.services.escalation.whatsapp_client.enviar_template",
            new_callable=AsyncMock,
            side_effect=whatsapp_client.WhatsAppError("x"),
        ):
            await escalation.alertar_cadastro(
                db_com_usuario_de_alerta, conversa, {"status": "cadastrado"}
            )
        assert "Joana" not in " ".join(r.getMessage() for r in caplog.records)
