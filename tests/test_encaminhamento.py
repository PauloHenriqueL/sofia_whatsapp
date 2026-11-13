"""Encaminhamento assistido pelo lado da Sofia, de ponta a ponta.

Entra pelas portas de verdade (webhook assinado da Meta e rota do cron) e sai
pelas de verdade: o `HamiltonClient` faz HTTP contra um Hamilton falso que segue
o contrato de `principais/views_encaminhamento.py`, e o envio para a Meta é
capturado em `whatsapp_client._enviar`.
"""

import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app import config
from app.database import Base
from app.dependencies import get_db
from app.main import app
from app.models import Conversa, Mensagem
from app.services import (
    config_negocio, encaminhamento, hamilton_client, lembretes_terapeutas, llm_client,
    serializacao,
)

TACIA = "5531988553344"      # terapeuta
PACIENTE = "5531911112222"   # Maria, ficha 579 no Hamilton

MENSAGENS = {
    "paciente": {"template": "proposta_horario_paciente",
                 "variaveis": ["Maria", "Tácia Teste", "terça, 17/11, às 19h"],
                 "botoes": ["prop_sim:42", "prop_nao:42"]},
    "terapeuta": {"template": "encaminhamento_terapeuta", "telefone": "+" + TACIA,
                  "variaveis": ["Tácia", "Maria Clara Souza", "01/01/1990", "(31) 91111-2222",
                                "terça, 17/11, às 19h", "200,00", "PIX/Manual"],
                  "botoes": ["enc_aceito:42", "enc_recusado:42"]},
}


def _enc(status):
    return {"id": 42, "status": status, "mensagens": MENSAGENS,
            "paciente": {"id": 579, "nome": "Maria Clara Souza", "telefone": "31911112222"}}


class HamiltonFalso:
    """O Hamilton das rotas de encaminhamento, com a máquina de estados real."""

    def __init__(self):
        self.status = "aguardando_paciente"
        self.respostas: list[dict] = []
        self.motivos: list[str] = []
        self.pedidos: list[dict] = []
        self.ativo = True
        self.expirados: list[dict] = []
        self.slot_tomado = False
        self.tem_terapeuta: set[int] = set()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        caminho = request.url.path
        if caminho == "/authentication/token/":
            return httpx.Response(200, json={"access": "jwt"})
        if caminho == "/api/v1/terapeutas/por-telefone/":
            if request.url.params.get("telefone") == TACIA:
                return httpx.Response(200, json={"terapeuta": True, "terapeuta_id": 7})
            return httpx.Response(404, json={"terapeuta": False})
        if caminho == "/api/v1/captacoes/":
            return httpx.Response(200, json=[])
        if caminho == "/api/v1/abordagens/":
            return httpx.Response(200, json=[{"pk_abordagem": 3, "abordagem": "TCC"},
                                             {"pk_abordagem": 5, "abordagem": "Analítica"}])
        corpo = json.loads(request.content or b"{}")
        if caminho == "/api/v1/encaminhamentos/expirar/":
            return httpx.Response(200, json={"ativo": self.ativo, "expirados": self.expirados})
        if caminho == "/api/v1/encaminhamentos/pedido/":
            self.pedidos.append(corpo)
            if corpo["paciente_id"] in self.tem_terapeuta:
                return httpx.Response(409, json={"detail": "x", "codigo": "tem_terapeuta"})
            return httpx.Response(200, json={"encaminhamento": {"id": 42, "status": "pendente"}})
        if caminho == "/api/v1/encaminhamentos/42/resposta/":
            self.respostas.append(corpo)
            esperado = {"paciente": "aguardando_paciente", "terapeuta": "aguardando_terapeuta"}
            if self.status != esperado[corpo["de"]]:
                return httpx.Response(409, json={"detail": "x", "codigo": "transicao"})
            if corpo["de"] == "terapeuta" and corpo["aceitou"] and self.slot_tomado:
                self.status = "cancelado"
                return httpx.Response(409, json={"detail": "x", "codigo": "slot_tomado",
                                                 "encaminhamento": _enc(self.status)})
            self.status = {
                ("paciente", True): "aguardando_terapeuta",
                ("paciente", False): "recusado_paciente",
                ("terapeuta", True): "aceito",
                ("terapeuta", False): "recusado_terapeuta",
            }[(corpo["de"], corpo["aceitou"])]
            return httpx.Response(200, json={"encaminhamento": _enc(self.status)})
        if caminho == "/api/v1/encaminhamentos/42/motivo/":
            self.motivos.append(corpo["motivo"])
            return httpx.Response(200, json={"encaminhamento": _enc(self.status)})
        return httpx.Response(404)


@pytest.fixture(autouse=True)
def _debounce_rapido():
    orig = dict(config_negocio._cache)
    config_negocio._cache["debounce_segundos"] = 0.05
    config_negocio._cache["simular_digitacao"] = False
    yield
    config_negocio._cache.clear()
    config_negocio._cache.update(orig)


@pytest.fixture
def hamilton(monkeypatch):
    falso = HamiltonFalso()
    cliente = hamilton_client.HamiltonClient(
        base_url="http://hamilton", username="sofia-bot", password="x",
        client=httpx.AsyncClient(transport=httpx.MockTransport(falso)),
    )
    monkeypatch.setattr(hamilton_client, "get_hamilton_client", lambda: cliente)
    lembretes_terapeutas._cache.clear()
    encaminhamento._aguardando_motivo.clear()
    encaminhamento._wamids_vistos.clear()
    return falso


@pytest.fixture
def meta(monkeypatch):
    enviados: list[dict] = []

    async def _enviar(payload, descricao, sigiloso=False):
        enviados.append(payload)
        return {"messages": [{"id": f"wamid.out{len(enviados)}"}]}

    monkeypatch.setattr("app.services.whatsapp_client._enviar", _enviar)
    monkeypatch.setattr(config.settings, "whatsapp_dry_run", False)
    monkeypatch.setattr(config.settings, "tasks_token", "segredo")
    monkeypatch.setattr(config.settings, "whatsapp_app_secret", "segredo-do-app")
    return enviados


@pytest_asyncio.fixture
async def sofia():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _db():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_db] = _db
    with patch("app.routers.webhook.async_session", maker), \
            patch("app.services.encaminhamento.async_session", maker):
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://sofia") as c:
            c.banco = maker
            async with maker() as s:
                s.add(Conversa(numero_whatsapp=PACIENTE, paciente_hamilton_id=579,
                               dados_coletados={"nome_completo": "Maria Clara Souza"}))
                await s.commit()
            yield c
    app.dependency_overrides.pop(get_db, None)
    serializacao.limpar()
    await engine.dispose()


async def _entregar(cliente, mensagem):
    corpo = json.dumps({"entry": [{"changes": [{"value": {"messages": [mensagem]}}]}]}).encode()
    assinatura = "sha256=" + hmac.new(b"segredo-do-app", corpo, hashlib.sha256).hexdigest()
    resp = await cliente.post("/webhook/whatsapp", content=corpo, headers={
        "X-Hub-Signature-256": assinatura, "Content-Type": "application/json"})
    assert resp.status_code == 200
    await serializacao.aguardar_pendentes()


async def _toque(cliente, numero, rotulo, payload, wamid):
    await _entregar(cliente, {"from": numero, "id": wamid, "type": "button",
                              "button": {"text": rotulo, "payload": payload}})


async def _texto(cliente, numero, texto, wamid):
    await _entregar(cliente, {"from": numero, "id": wamid, "type": "text", "text": {"body": texto}})


def _templates(enviados):
    return [p for p in enviados if p.get("type") == "template"]


def _textos_para(enviados, numero):
    return [p["text"]["body"] for p in enviados if p.get("type") == "text" and p["to"] == numero]


class TestPacienteRespondeAProposta:
    @pytest.mark.asyncio
    async def test_pode_ser_avisa_o_terapeuta_sem_llm(self, sofia, hamilton, meta):
        with patch("app.routers.webhook.llm_client.get_llm_client") as llm:
            await _toque(sofia, PACIENTE, "Pode ser", "prop_sim:42", "wamid.p1")
        llm.assert_not_called()
        assert hamilton.respostas == [{"de": "paciente", "aceitou": True, "motivo": ""}]
        [aviso] = _templates(meta)
        assert aviso["to"] == "+" + TACIA
        assert aviso["template"]["name"] == "encaminhamento_terapeuta"
        corpo, aceito, recusado = aviso["template"]["components"]
        assert [p["text"] for p in corpo["parameters"]] == MENSAGENS["terapeuta"]["variaveis"]
        assert (aceito["parameters"][0]["payload"], recusado["parameters"][0]["payload"]) == \
            ("enc_aceito:42", "enc_recusado:42")
        assert _textos_para(meta, PACIENTE) == [encaminhamento.TEXTO_PACIENTE_SIM]

    @pytest.mark.asyncio
    async def test_fura_o_modo_humano(self, sofia, hamilton, meta):
        async with sofia.banco() as s:
            conversa = (await s.execute(select(Conversa))).scalar_one()
            conversa.modo = "humano"
            await s.commit()
        await _toque(sofia, PACIENTE, "Pode ser", "prop_sim:42", "wamid.p1")
        assert len(_templates(meta)) == 1  # o terapeuta foi avisado mesmo assim

    @pytest.mark.asyncio
    async def test_prefiro_outro_nao_envolve_o_terapeuta(self, sofia, hamilton, meta):
        await _toque(sofia, PACIENTE, "Prefiro outro horário", "prop_nao:42", "wamid.p1")
        assert hamilton.respostas == [{"de": "paciente", "aceitou": False, "motivo": ""}]
        assert _templates(meta) == []
        assert _textos_para(meta, PACIENTE) == [encaminhamento.TEXTO_PACIENTE_NAO]

    @pytest.mark.asyncio
    async def test_proposta_vencida(self, sofia, hamilton, meta):
        hamilton.status = "sem_resposta"
        await _toque(sofia, PACIENTE, "Pode ser", "prop_sim:42", "wamid.p1")
        assert _templates(meta) == []
        assert _textos_para(meta, PACIENTE) == [encaminhamento.TEXTO_PROPOSTA_VENCIDA]

    @pytest.mark.asyncio
    async def test_historico_guarda_o_rotulo_do_botao(self, sofia, hamilton, meta):
        await _toque(sofia, PACIENTE, "Pode ser", "prop_sim:42", "wamid.p1")
        async with sofia.banco() as s:
            textos = [m.texto for m in (await s.execute(select(Mensagem))).scalars()]
        assert "Pode ser" in textos


class TestTerapeutaResponde:
    @pytest.mark.asyncio
    async def test_aceito_confirma_aos_dois(self, sofia, hamilton, meta):
        hamilton.status = "aguardando_terapeuta"
        await _toque(sofia, TACIA, "Aceito", "enc_aceito:42", "wamid.t1")
        assert hamilton.respostas == [{"de": "terapeuta", "aceitou": True, "motivo": ""}]
        assert hamilton.status == "aceito"
        [para_ela] = _textos_para(meta, TACIA)
        assert "Maria Clara Souza já está vinculado(a) a você" in para_ela
        [para_paciente] = _textos_para(meta, PACIENTE)
        assert para_paciente == encaminhamento.TEXTO_PACIENTE_CONFIRMADO.format(
            terapeuta="Tácia Teste", quando="terça, 17/11, às 19h")

    @pytest.mark.asyncio
    async def test_toque_repetido_nao_responde_duas_vezes(self, sofia, hamilton, meta):
        hamilton.status = "aguardando_terapeuta"
        await _toque(sofia, TACIA, "Aceito", "enc_aceito:42", "wamid.t1")
        await _toque(sofia, TACIA, "Aceito", "enc_aceito:42", "wamid.t1")  # reentrega da Meta
        assert len(hamilton.respostas) == 1

    @pytest.mark.asyncio
    async def test_nao_aceito_e_o_motivo_digitado_depois(self, sofia, hamilton, meta):
        hamilton.status = "aguardando_terapeuta"
        await _toque(sofia, TACIA, "Não aceito", "enc_recusado:42", "wamid.t1")
        assert hamilton.status == "recusado_terapeuta"
        await _texto(sofia, TACIA, "Estou sem horário à noite", "wamid.t2")
        assert hamilton.motivos == ["Estou sem horário à noite"]
        assert _textos_para(meta, TACIA) == [
            encaminhamento.TEXTO_TERAPEUTA_RECUSOU, encaminhamento.TEXTO_MOTIVO_OBRIGADO]
        # Depois do motivo, volta a ser a resposta fixa de sempre.
        await _texto(sofia, TACIA, "obrigada", "wamid.t3")
        assert _textos_para(meta, TACIA)[-1] == lembretes_terapeutas.RESPOSTA_TERAPEUTA

    @pytest.mark.asyncio
    async def test_horario_tomado_no_meio_do_caminho(self, sofia, hamilton, meta):
        hamilton.status = "aguardando_terapeuta"
        hamilton.slot_tomado = True
        await _toque(sofia, TACIA, "Aceito", "enc_aceito:42", "wamid.t1")
        assert _textos_para(meta, TACIA) == [encaminhamento.TEXTO_SLOT_TOMADO]
        assert _textos_para(meta, PACIENTE) == []

    @pytest.mark.asyncio
    async def test_encaminhamento_ja_fechado(self, sofia, hamilton, meta):
        hamilton.status = "aceito"
        await _toque(sofia, TACIA, "Não aceito", "enc_recusado:42", "wamid.t1")
        assert _textos_para(meta, TACIA) == [encaminhamento.TEXTO_ENCAMINHAMENTO_FECHADO]


class LLMDeCriterios:
    def __init__(self, resposta):
        self.resposta = resposta
        self.chamadas = 0

    async def gerar_resposta(self, historico, **kwargs):
        self.chamadas += 1
        return llm_client.LLMResposta(texto=self.resposta)


async def _cron(cliente):
    resp = await cliente.post("/tasks/lembretes-terapeutas", headers={"X-Tasks-Token": "segredo"})
    assert resp.status_code == 200
    return resp.json()["encaminhamento"]


class TestRodadaDoCron:
    async def _cadastrar(self, sofia, quando=None):
        async with sofia.banco() as s:
            conversa = (await s.execute(select(Conversa))).scalar_one()
            conversa.cadastrado_em = quando or datetime.now(timezone.utc)
            conversa.dados_coletados = {"nome_completo": "Maria Clara Souza",
                                        "horarios_disponiveis": "terças à noite, online"}
            await s.commit()

    @pytest.mark.asyncio
    async def test_registra_o_pedido_uma_vez(self, sofia, hamilton, meta):
        await self._cadastrar(sofia)
        llm = LLMDeCriterios(json.dumps({
            "modalidade": "online", "abordagem_id": 3,
            "horarios": [{"dia_semana": 1, "hora_inicio": "19:00"}], "especialidade": None}))
        with patch.object(llm_client, "get_llm_client", return_value=llm):
            assert (await _cron(sofia))["registrados"] == 1
            assert (await _cron(sofia))["registrados"] == 0  # já registrado: não repete
        [pedido] = hamilton.pedidos
        assert pedido["paciente_id"] == 579
        assert pedido["criterios"]["modalidade"] == "online"
        assert pedido["criterios"]["abordagem_id"] == 3
        assert pedido["criterios"]["horarios"] == [{"dia_semana": 1, "hora_inicio": "19:00"}]
        assert "terças à noite" in pedido["criterios"]["texto_original"]
        assert llm.chamadas == 1

    @pytest.mark.asyncio
    async def test_trava_desligada_nao_faz_nada(self, sofia, hamilton, meta):
        await self._cadastrar(sofia)
        hamilton.ativo = False
        with patch.object(llm_client, "get_llm_client") as llm:
            assert await _cron(sofia) == {"ativo": False}
        llm.assert_not_called()
        assert hamilton.pedidos == []

    @pytest.mark.asyncio
    async def test_cadastro_antigo_nao_vira_pedido(self, sofia, hamilton, meta):
        await self._cadastrar(sofia, datetime.now(timezone.utc) - timedelta(days=30))
        with patch.object(llm_client, "get_llm_client") as llm:
            await _cron(sofia)
        llm.assert_not_called()
        assert hamilton.pedidos == []

    @pytest.mark.asyncio
    async def test_paciente_com_terapeuta_fica_marcado_e_nao_volta(self, sofia, hamilton, meta):
        await self._cadastrar(sofia)
        hamilton.tem_terapeuta.add(579)
        with patch.object(llm_client, "get_llm_client", return_value=LLMDeCriterios("{}")):
            r = await _cron(sofia)
            assert (r["registrados"], r["ignorados"]) == (0, 1)
            await _cron(sofia)
        assert len(hamilton.pedidos) == 1

    @pytest.mark.asyncio
    async def test_extracao_quebrada_ainda_registra_com_a_fala(self, sofia, hamilton, meta):
        await self._cadastrar(sofia)
        with patch.object(llm_client, "get_llm_client", return_value=LLMDeCriterios("não sei")):
            await _cron(sofia)
        [pedido] = hamilton.pedidos
        assert set(pedido["criterios"]) == {"texto_original"}


class TestValidacaoDosCriterios:
    def test_descarta_o_que_o_hamilton_nao_aceitaria(self):
        bruto = json.dumps({
            "modalidade": "tanto faz",
            "abordagem_id": 99,  # não está na lista
            "horarios": [{"dia_semana": 1, "hora_inicio": "19:00"},
                         {"dia_semana": 9, "hora_inicio": "10:00"},
                         {"dia_semana": 2, "hora_inicio": "19:15"},
                         "terça"],
            "especialidade": "  luto  "})
        assert encaminhamento._validar_criterios(bruto, {3: "TCC"}) == {
            "horarios": [{"dia_semana": 1, "hora_inicio": "19:00"}],
            "especialidade": "luto",
        }

    def test_json_em_bloco_de_codigo(self):
        bruto = '```json\n{"modalidade": "presencial"}\n```'
        assert encaminhamento._validar_criterios(bruto, {}) == {"modalidade": "presencial"}

    def test_lixo_vira_vazio(self):
        assert encaminhamento._validar_criterios("nada", {}) == {}
        assert encaminhamento._validar_criterios(None, {}) == {}


class TestProposta:
    @pytest.mark.asyncio
    async def test_proposta_vai_para_o_numero_da_conversa_e_fica_no_historico(self, sofia, hamilton, meta):
        async with sofia.banco() as s:
            assert await encaminhamento.propor_ao_paciente(s, _enc("aguardando_paciente"))
        [proposta] = _templates(meta)
        assert proposta["to"] == PACIENTE
        assert proposta["template"]["name"] == "proposta_horario_paciente"
        async with sofia.banco() as s:
            [msg] = (await s.execute(select(Mensagem))).scalars().all()
        assert msg.tipo == "template"
        assert "Primeira sessão: terça, 17/11, às 19h" in msg.texto

    @pytest.mark.asyncio
    async def test_sem_conversa_usa_o_telefone_da_ficha(self, sofia, hamilton, meta):
        enc = _enc("aguardando_paciente")
        enc["paciente"] = {"id": 999, "nome": "Outra", "telefone": "31977776666"}
        async with sofia.banco() as s:
            assert await encaminhamento.propor_ao_paciente(s, enc)
        assert _templates(meta)[0]["to"] == "5531977776666"
