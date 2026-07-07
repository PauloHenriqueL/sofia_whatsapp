"""Testes do serviço de cadastro no Hamilton (fallback de telefone + estados)."""

from unittest.mock import patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base
from app.models import Conversa
from app.services import cadastro, hamilton_client


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


class _FakeHamilton:
    def __init__(self, existentes=None, criado=None, erro=False):
        self._existentes = existentes or []
        self._criado = criado or {}
        self._erro = erro
        self.criou_com = None
        self.atualizou = None  # (pid, payload)

    async def buscar_paciente_por_telefone(self, tel):
        if self._erro:
            raise hamilton_client.HamiltonError("offline")
        return self._existentes

    async def criar_paciente(self, dados):
        if self._erro:
            raise hamilton_client.HamiltonError("offline")
        self.criou_com = dados
        return self._criado

    async def atualizar_paciente(self, pid, payload):
        self.atualizou = (pid, payload)
        return {"pk_paciente": pid}


class TestMapearUpdate:
    def test_anexa_observacao_e_campos_factuais(self):
        dados = {
            "data_nascimento": "2001-08-07",
            "endereco": "Rua Nova, 10",
            "horarios_disponiveis": "manhãs",
            "motivo_busca": "ansiedade",
        }
        payload = hamilton_client.mapear_dados_update(dados, {"observacao": "obs antiga"})
        assert payload["dat_nascimento"] == "2001-08-07"
        assert payload["endereco"] == "Rua Nova, 10"
        assert "obs antiga" in payload["observacao"]  # preserva a anterior
        assert "Horários: manhãs" in payload["observacao"]  # anexa o novo
        assert "nome" not in payload  # não sobrescreve o nome


class TestGarantirTelefone:
    def test_usa_whatsapp_quando_placeholder(self):
        c = Conversa(numero_whatsapp="553183055118")
        dados = cadastro._garantir_telefone(c, {"telefone_contato": "[SEU_NÚMERO]"})
        assert dados["telefone_contato"] == "553183055118"

    def test_usa_whatsapp_quando_vazio(self):
        c = Conversa(numero_whatsapp="553199998888")
        dados = cadastro._garantir_telefone(c, {})
        assert dados["telefone_contato"] == "553199998888"

    def test_mantem_telefone_valido(self):
        c = Conversa(numero_whatsapp="553199998888")
        dados = cadastro._garantir_telefone(c, {"telefone_contato": "31988887777"})
        assert dados["telefone_contato"] == "31988887777"


class TestCadastrarPaciente:
    @pytest.mark.asyncio
    async def test_cadastra_com_sucesso_e_usa_whatsapp(self, session):
        c = Conversa(
            numero_whatsapp="553183055118",
            estado="cadastro_pendente",
            dados_coletados={"nome_completo": "Maria", "telefone_contato": "[SEU_NÚMERO]"},
        )
        session.add(c)
        await session.flush()
        fake = _FakeHamilton(criado={"pk_paciente": 123})
        with patch("app.services.cadastro.hamilton_client.get_hamilton_client", return_value=fake):
            res = await cadastro.cadastrar_paciente(session, c)
        assert res["status"] == "cadastrado"
        assert c.estado == "cadastrado"
        assert c.paciente_hamilton_id == 123
        assert fake.criou_com["telefone_contato"] == "553183055118"

    @pytest.mark.asyncio
    async def test_mesmo_telefone_e_nome_atualiza_nao_cria(self, session):
        # Mesma pessoa voltando (telefone + nome batem) -> atualiza a ficha, não duplica.
        c = Conversa(
            numero_whatsapp="553183055118",
            estado="coletando_dados",
            dados_coletados={
                "nome_completo": "José da Silva",
                "telefone_contato": "553183055118",
                "endereco": "Rua Nova, 10",
                "horarios_disponiveis": "manhãs",
            },
        )
        session.add(c)
        await session.flush()
        # existente com o mesmo nome (com acento/caixa diferentes) nesse telefone
        fake = _FakeHamilton(
            existentes=[{"pk_paciente": 42, "nome": "Jose da silva", "observacao": "antigo"}]
        )
        with patch("app.services.cadastro.hamilton_client.get_hamilton_client", return_value=fake):
            res = await cadastro.cadastrar_paciente(session, c)
        assert res["status"] == "atualizado"
        assert c.paciente_hamilton_id == 42
        assert fake.criou_com is None  # NÃO criou paciente novo
        assert fake.atualizou[0] == 42  # atualizou o 42
        assert "endereco" in fake.atualizou[1]  # com os dados novos

    @pytest.mark.asyncio
    async def test_mesmo_telefone_nome_diferente_cria_novo(self, session):
        # Pai (mesmo telefone) cadastrando o filho (nome diferente) -> paciente NOVO.
        c = Conversa(
            numero_whatsapp="553183055118",
            estado="coletando_dados",
            dados_coletados={"nome_completo": "Pedro da Silva", "telefone_contato": "553183055118"},
        )
        session.add(c)
        await session.flush()
        fake = _FakeHamilton(
            existentes=[{"pk_paciente": 42, "nome": "José da Silva"}],
            criado={"pk_paciente": 99},
        )
        with patch("app.services.cadastro.hamilton_client.get_hamilton_client", return_value=fake):
            res = await cadastro.cadastrar_paciente(session, c)
        assert res["status"] == "cadastrado"
        assert c.paciente_hamilton_id == 99
        assert fake.criou_com is not None  # criou o filho
        assert fake.atualizou is None  # não mexeu no pai

    @pytest.mark.asyncio
    async def test_hamilton_falha_marca_pendente(self, session):
        c = Conversa(
            numero_whatsapp="553183055118",
            estado="coletando_dados",
            dados_coletados={"nome_completo": "Maria"},
        )
        session.add(c)
        await session.flush()
        fake = _FakeHamilton(erro=True)
        with patch("app.services.cadastro.hamilton_client.get_hamilton_client", return_value=fake):
            res = await cadastro.cadastrar_paciente(session, c)
        assert res["status"] == "cadastro_pendente"
        assert c.estado == "cadastro_pendente"
