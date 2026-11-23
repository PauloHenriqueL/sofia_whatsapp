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
        self.na_fila = True
        self.fila_quebrada = False
        self.reservas: list[dict] = []
        self.candidatos_pedidos: list[dict] = []
        self.reserva_status = 200

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
        if caminho == "/api/v1/encaminhamentos/fila/":
            if self.fila_quebrada:  # 200 fora do contrato (campo com o tipo errado)
                return httpx.Response(200, json={"ativo": True, "itens": {"nao": "e lista"}})
            itens = []
            if self.na_fila:
                itens.append({
                    "paciente": {"id": 579, "nome": "Maria Clara Souza", "telefone": "31911112222",
                                 "dat_nascimento": "1990-01-01", "vlr_sessao": "200.00",
                                 "tipo_pagamento": "manual"},
                    "encaminhamento": {"id": 42, "status": "pendente",
                                       "created_at": "2026-11-13T12:00:00+00:00"},
                    "criterios": {"modalidade": "online", "abordagem_id": 3,
                                  "horarios": [{"dia_semana": 1, "hora_inicio": "19:00"}],
                                  "especialidade": "luto", "texto_original": "Horários: terças à noite"},
                    "ultima_recusa": None,
                })
            return httpx.Response(200, json={"ativo": self.ativo, "itens": itens if self.ativo else []})
        if caminho == "/api/v1/encaminhamentos/candidatos/":
            self.candidatos_pedidos.append(corpo)
            return httpx.Response(200, json={"ativo": True, "candidatos": [{
                "terapeuta": {"id": 7, "nome": "Tácia Teste", "abordagem": "TCC", "modalidade": "Online"},
                "motivos": ["TCC ✓", "Online ✓", "no horário pedido ✓"], "abordagem_ok": True,
                "distancia": 0, "pontos": 5, "horas_livres": 6.0,
                "melhor_slot": {"dia_semana": 1, "hora_inicio": "19:00", "hora_fim": "20:00", "distancia": 0},
                "slots": [{"dia_semana": 1, "hora_inicio": "19:00", "hora_fim": "20:00", "distancia": 0},
                          {"dia_semana": 3, "hora_inicio": "18:30", "hora_fim": "19:30", "distancia": 2910}],
            }]})
        if caminho == "/api/v1/encaminhamentos/42/reservar/":
            self.reservas.append(corpo)
            if self.reserva_status != 200:
                return httpx.Response(self.reserva_status, json={"detail": "x", "codigo": "slot_tomado"})
            self.status = "aguardando_paciente"
            self.na_fila = False
            return httpx.Response(200, json={"encaminhamento": _enc(self.status)})
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
    return _ligar_hamilton(monkeypatch, HamiltonFalso())


def _ligar_hamilton(monkeypatch, falso):
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
        # https: com o .env de produção o cookie de sessão sai `Secure` e não
        # voltaria num cliente http (o login do painel "não pegaria").
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="https://sofia") as c:
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
        assert hamilton.respostas == [{"de": "paciente", "aceitou": True, "motivo": "", "opcao": 1}]
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
        assert hamilton.respostas == [{"de": "paciente", "aceitou": False, "motivo": "", "opcao": 1}]
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


class LLMRoteiro:
    """Modelo falso: devolve as respostas na ordem e guarda o que recebeu."""

    def __init__(self, *respostas):
        self.respostas = list(respostas)
        self.chamadas: list[dict] = []

    async def gerar_resposta(self, historico, tools=None, **kwargs):
        self.chamadas.append({"historico": historico, "tools": tools})
        return self.respostas.pop(0)


def _tool(nome, **argumentos):
    return llm_client.LLMResposta(
        tool_calls=[llm_client.ToolCall(id="t1", name=nome, arguments=argumentos)])


def _nomes_das_tools(chamada):
    return [t["function"]["name"] for t in (chamada["tools"] or [])]


async def _marcar_conversa(sofia, **marcas):
    async with sofia.banco() as s:
        conversa = (await s.execute(select(Conversa))).scalar_one()
        conversa.dados_coletados = {**(conversa.dados_coletados or {}), **marcas}
        await s.commit()


async def _conversa(sofia):
    """A conversa da paciente (o aviso ao terapeuta cria a dele também)."""
    async with sofia.banco() as s:
        return (await s.execute(
            select(Conversa).where(Conversa.numero_whatsapp == PACIENTE))).scalar_one()


class TestRespostaEscritaAProposta:
    """Teste em produção de 17/11/2026: o paciente recusou ESCREVENDO, a Sofia
    só escalou e ele sumiu da fila "Pacientes esperando terapeuta"."""

    @pytest.mark.asyncio
    async def test_proposta_enviada_marca_a_conversa(self, sofia, hamilton, meta):
        async with sofia.banco() as s:
            assert await encaminhamento.propor_ao_paciente(s, _enc("aguardando_paciente"))
        conversa = await _conversa(sofia)
        assert encaminhamento.proposta_aberta(conversa) == 42

    @pytest.mark.asyncio
    async def test_recusa_escrita_registra_no_hamilton_sem_escalar(self, sofia, hamilton, meta):
        await _marcar_conversa(sofia, proposta_aberta=42)
        llm = LLMRoteiro(
            _tool("responder_proposta", aceitou=False),
            llm_client.LLMResposta(texto="Tudo bem! O que não funcionou nessa proposta?"),
        )
        with patch("app.routers.webhook.llm_client.get_llm_client", return_value=llm):
            await _texto(sofia, PACIENTE, "não, esse horário não dá pra mim", "wamid.p1")

        assert "responder_proposta" in _nomes_das_tools(llm.chamadas[0])
        assert hamilton.respostas == [{"de": "paciente", "aceitou": False, "motivo": ""}]
        assert hamilton.status == "recusado_paciente"
        conversa = await _conversa(sofia)
        assert conversa.modo == "bot"
        assert encaminhamento.proposta_aberta(conversa) is None
        assert encaminhamento.recusa_sem_motivo(conversa) == 42
        assert _templates(meta) == []  # o terapeuta não é envolvido
        assert _textos_para(meta, PACIENTE) == ["Tudo bem! O que não funcionou nessa proposta?"]

    @pytest.mark.asyncio
    async def test_motivo_escrito_depois_vai_para_a_fila(self, sofia, hamilton, meta):
        hamilton.status = "recusado_paciente"
        await _marcar_conversa(sofia, recusa_sem_motivo=42)
        llm = LLMRoteiro(
            _tool("registrar_motivo_recusa", motivo="quarta à noite eu trabalho"),
            llm_client.LLMResposta(texto="Obrigada! A nova proposta chega por aqui."),
        )
        with patch("app.routers.webhook.llm_client.get_llm_client", return_value=llm):
            await _texto(sofia, PACIENTE, "quarta à noite eu trabalho", "wamid.p1")

        assert "registrar_motivo_recusa" in _nomes_das_tools(llm.chamadas[0])
        assert hamilton.motivos == ["quarta à noite eu trabalho"]
        assert encaminhamento.recusa_sem_motivo(await _conversa(sofia)) is None

    @pytest.mark.asyncio
    async def test_aceite_escrito_avisa_o_terapeuta(self, sofia, hamilton, meta):
        await _marcar_conversa(sofia, proposta_aberta=42)
        llm = LLMRoteiro(
            _tool("responder_proposta", aceitou=True),
            llm_client.LLMResposta(texto="Combinado! Assim que a confirmação chegar, te aviso."),
        )
        with patch("app.routers.webhook.llm_client.get_llm_client", return_value=llm):
            await _texto(sofia, PACIENTE, "pode ser, fechado", "wamid.p1")

        assert hamilton.respostas == [{"de": "paciente", "aceitou": True, "motivo": ""}]
        [aviso] = _templates(meta)
        assert aviso["to"] == "+" + TACIA

    @pytest.mark.asyncio
    async def test_sem_proposta_aberta_a_ferramenta_nao_aparece(self, sofia, hamilton, meta):
        llm = LLMRoteiro(llm_client.LLMResposta(texto="Oi! Sou a Sofia."))
        with patch("app.routers.webhook.llm_client.get_llm_client", return_value=llm):
            await _texto(sofia, PACIENTE, "não", "wamid.p1")
        nomes = _nomes_das_tools(llm.chamadas[0])
        assert "responder_proposta" not in nomes
        assert "registrar_motivo_recusa" not in nomes

    @pytest.mark.asyncio
    async def test_botao_prefiro_outro_abre_espaco_para_o_motivo(self, sofia, hamilton, meta):
        await _marcar_conversa(sofia, proposta_aberta=42)
        await _toque(sofia, PACIENTE, "Prefiro outro horário", "prop_nao:42", "wamid.p1")
        conversa = await _conversa(sofia)
        assert encaminhamento.proposta_aberta(conversa) is None
        assert encaminhamento.recusa_sem_motivo(conversa) == 42


class TestTerapeutaResponde:
    @pytest.mark.asyncio
    async def test_aceito_confirma_aos_dois(self, sofia, hamilton, meta):
        hamilton.status = "aguardando_terapeuta"
        await _toque(sofia, TACIA, "Aceito", "enc_aceito:42", "wamid.t1")
        assert hamilton.respostas == [{"de": "terapeuta", "aceitou": True, "motivo": ""}]
        assert hamilton.status == "aceito"
        [para_ela] = _textos_para(meta, TACIA)
        assert "O atendimento de Maria Clara Souza já está registrado com você" in para_ela
        assert "em até 24 horas" in para_ela
        [para_paciente] = _textos_para(meta, PACIENTE)
        assert para_paciente == (
            "Tudo certo! Tácia Teste confirmou o seu atendimento, e a primeira sessão é "
            "terça, 17/11, às 19h. Antes disso, Tácia vai te chamar aqui no WhatsApp em até "
            "24 horas para se apresentar e confirmar a sessão."
        )

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

    @pytest.mark.asyncio
    async def test_ficha_de_paciente_de_fora_do_brasil(self, sofia, hamilton, meta):
        """O Hamilton guarda "+351..."; o WhatsApp quer DDI sem "+". Antes virava
        "55+351..." descartado e a proposta não saía."""
        enc = _enc("aguardando_paciente")
        enc["paciente"] = {"id": 998, "nome": "Ana", "telefone": "+351912345678"}
        async with sofia.banco() as s:
            assert await encaminhamento.propor_ao_paciente(s, enc)
        assert _templates(meta)[0]["to"] == "351912345678"


async def _logar(cliente, maker):
    from app.services import usuarios
    async with maker() as s:
        await usuarios.migrar_usuario_do_env(s)
    resp = await cliente.post("/login", data={"usuario": config.settings.painel_user,
                                              "senha": config.settings.painel_password})
    assert resp.status_code == 303


class TestTelaDaThaina:
    @pytest.mark.asyncio
    async def test_hoje_mostra_a_fila_de_encaminhar(self, sofia, hamilton, meta):
        await _logar(sofia, sofia.banco)
        html = (await sofia.get("/painel/")).text
        assert "Encaminhar" in html
        assert "Maria Clara Souza" in html
        assert "/painel/encaminhamentos/42" in html
        assert "TCC" in html and "ter 19h" in html
        assert "enc-chip-leitura" in html  # especialidade aparece, mas marcada como "não filtra"

    @pytest.mark.asyncio
    async def test_trava_desligada_esconde_o_card(self, sofia, hamilton, meta):
        hamilton.ativo = False
        await _logar(sofia, sofia.banco)
        assert "Escolher terapeuta" not in (await sofia.get("/painel/")).text

    @pytest.mark.asyncio
    async def test_pagina_de_escolha(self, sofia, hamilton, meta):
        await _logar(sofia, sofia.banco)
        resp = await sofia.get("/painel/encaminhamentos/42")
        assert resp.status_code == 200
        html = resp.text
        assert "Encaminhar Maria Clara Souza" in html
        assert "não filtra" in html  # especialidade "luto" lida, sem filtrar
        assert 'data-terapeuta="7"' in html and 'data-hora="19:00"' in html
        assert "enc-slot--exato" in html
        assert hamilton.candidatos_pedidos == [{"paciente_id": 579}]

    @pytest.mark.asyncio
    async def test_busca_com_criterios_ajustados_grava_no_pedido(self, sofia, hamilton, meta):
        await _logar(sofia, sofia.banco)
        resp = await sofia.post("/painel/encaminhamentos/42/candidatos", data={
            "modalidade": "presencial", "abordagem_id": "", "dia": ["3"], "hora": ["18:30"]})
        assert resp.status_code == 200
        assert "Tácia Teste" in resp.text
        esperado = {"modalidade": "presencial",
                    "horarios": [{"dia_semana": 3, "hora_inicio": "18:30"}],
                    "especialidade": "luto", "texto_original": "Horários: terças à noite"}
        assert hamilton.candidatos_pedidos[-1] == {"paciente_id": 579, "criterios": esperado}
        assert hamilton.pedidos[-1] == {"paciente_id": 579, "criterios": esperado}

    @pytest.mark.asyncio
    async def test_reservar_manda_a_proposta_ao_paciente(self, sofia, hamilton, meta):
        await _logar(sofia, sofia.banco)
        resp = await sofia.post("/painel/encaminhamentos/42/reservar", data={
            "terapeuta_id": "7", "dia_semana": "1", "hora_inicio": "19:00",
            "dat_primeira_sessao": "2026-11-17"})
        assert resp.status_code == 303
        assert resp.headers["location"].startswith("/painel/?proposta=Maria")
        assert hamilton.reservas == [{"terapeuta_id": 7, "dia_semana": 1, "hora_inicio": "19:00",
                                      "dat_primeira_sessao": "2026-11-17"}]
        [proposta] = _templates(meta)
        assert proposta["to"] == PACIENTE
        assert proposta["template"]["name"] == "proposta_horario_paciente"
        # Escolhido: some da fila, e a página dele manda de volta para Hoje.
        resp = await sofia.get("/painel/encaminhamentos/42")
        assert resp.headers["location"] == "/painel/?aviso=fora_da_fila"

    @pytest.mark.asyncio
    async def test_horario_tomado_volta_para_a_escolha_com_aviso(self, sofia, hamilton, meta):
        hamilton.reserva_status = 409
        await _logar(sofia, sofia.banco)
        resp = await sofia.post("/painel/encaminhamentos/42/reservar", data={
            "terapeuta_id": "7", "dia_semana": "1", "hora_inicio": "19:00"})
        assert resp.headers["location"] == "/painel/encaminhamentos/42?erro=slot_tomado"
        assert _templates(meta) == []
        html = (await sofia.get(resp.headers["location"])).text
        assert "acabou de ser ocupado" in html

    @pytest.mark.asyncio
    async def test_sem_login_nao_entra(self, sofia, hamilton, meta):
        resp = await sofia.get("/painel/encaminhamentos/42")
        assert resp.status_code == 303 and resp.headers["location"] == "/login"

    @pytest.mark.asyncio
    async def test_hamilton_estranho_nao_derruba_a_home(self, sofia, hamilton, meta):
        """A fila é o pedaço menos importante da home: resposta fora do contrato
        vira aviso, não 500 na tela que a Thainá usa o dia inteiro."""
        hamilton.fila_quebrada = True
        await _logar(sofia, sofia.banco)
        resp = await sofia.get("/painel/")
        assert resp.status_code == 200
        assert "Não consegui buscar os encaminhamentos no Hamilton agora." in resp.text


# --- Duas propostas (23/11/2026) ---------------------------------------------

ALTERNATIVA = {"template": "proposta_horario_paciente", "opcao": 2,
               "variaveis": ["Maria", "Ulisses Teste", "quarta, 18/11, às 9h"],
               "botoes": ["prop_sim2:42", "prop_nao2:42"]}


class HamiltonDuasOpcoes(HamiltonFalso):
    """O Hamilton com duas propostas abertas, seguindo `responder_paciente`."""

    def __init__(self):
        super().__init__()
        self.abertas = [1, 2]

    def enc(self):
        e = _enc(self.status)
        aguardando = self.status == "aguardando_paciente"
        e["mensagens"] = {
            **MENSAGENS,
            "paciente": {**MENSAGENS["paciente"], "opcao": 1},
            "paciente_alternativa": ALTERNATIVA if aguardando and len(self.abertas) == 2 else None,
        }
        e["opcoes_abertas"] = self.abertas if aguardando else []
        return e

    def __call__(self, request):
        caminho = request.url.path
        corpo = json.loads(request.content or b"{}")
        if caminho == "/api/v1/encaminhamentos/42/reservar/":
            self.reservas.append(corpo)
            self.status, self.na_fila = "aguardando_paciente", False
            return httpx.Response(200, json={"encaminhamento": self.enc()})
        if caminho == "/api/v1/encaminhamentos/42/resposta/" and corpo.get("de") == "paciente":
            self.respostas.append(corpo)
            opcao = corpo.get("opcao")
            if self.status != "aguardando_paciente" or (opcao is not None and opcao not in self.abertas):
                return httpx.Response(409, json={"detail": "x", "codigo": "transicao"})
            if corpo["aceitou"]:
                if opcao is None and len(self.abertas) > 1:
                    return httpx.Response(409, json={"detail": "x", "codigo": "opcao"})
                self.status = "aguardando_terapeuta"
            elif opcao is not None and len(self.abertas) > 1:
                self.abertas = [o for o in self.abertas if o != opcao]
            else:
                self.status = "recusado_paciente"
            return httpx.Response(200, json={"encaminhamento": self.enc()})
        return super().__call__(request)


@pytest.fixture
def hamilton2(monkeypatch):
    return _ligar_hamilton(monkeypatch, HamiltonDuasOpcoes())


async def _propor_duas(sofia, hamilton2):
    async with sofia.banco() as s:
        assert await encaminhamento.propor_ao_paciente(s, hamilton2.enc())


class TestDuasPropostas:
    @pytest.mark.asyncio
    async def test_saem_dois_templates_cada_um_com_os_seus_botoes(self, sofia, hamilton2, meta):
        await _propor_duas(sofia, hamilton2)
        um, dois = _templates(meta)
        botoes = [[c["parameters"][0]["payload"] for c in t["template"]["components"][1:]]
                  for t in (um, dois)]
        assert botoes == [["prop_sim:42", "prop_nao:42"], ["prop_sim2:42", "prop_nao2:42"]]
        conversa = await _conversa(sofia)
        assert encaminhamento.proposta_aberta(conversa) == 42
        assert encaminhamento.opcoes_abertas(conversa) == {
            "1": "Tácia Teste, terça, 17/11, às 19h", "2": "Ulisses Teste, quarta, 18/11, às 9h"}
        async with sofia.banco() as s:
            textos = [m.texto for m in (await s.execute(select(Mensagem))).scalars()]
        assert textos[0].startswith("[Opção 1 de 2]") and textos[1].startswith("[Opção 2 de 2]")
        # A pessoa nunca escreveu: fora da janela, só os templates.
        assert _textos_para(meta, PACIENTE) == []

    @pytest.mark.asyncio
    async def test_dentro_da_janela_vai_uma_linha_explicando(self, sofia, hamilton2, meta):
        async with sofia.banco() as s:
            conversa = (await s.execute(select(Conversa))).scalar_one()
            s.add(Mensagem(conversa_id=conversa.id, direcao="recebida", origem="paciente",
                           tipo="texto", texto="oi"))
            await s.commit()
        await _propor_duas(sofia, hamilton2)
        assert _textos_para(meta, PACIENTE) == [encaminhamento.TEXTO_DUAS_OPCOES]

    @pytest.mark.asyncio
    async def test_escolher_a_segunda_e_depois_tocar_na_primeira(self, sofia, hamilton2, meta):
        await _propor_duas(sofia, hamilton2)
        await _toque(sofia, PACIENTE, "Pode ser", "prop_sim2:42", "wamid.p1")
        assert hamilton2.respostas[-1] == {"de": "paciente", "aceitou": True, "motivo": "", "opcao": 2}
        assert _templates(meta)[-1]["template"]["name"] == "encaminhamento_terapeuta"
        assert encaminhamento.proposta_aberta(await _conversa(sofia)) is None

        await _toque(sofia, PACIENTE, "Pode ser", "prop_sim:42", "wamid.p2")
        assert _textos_para(meta, PACIENTE)[-1] == encaminhamento.TEXTO_JA_ESCOLHEU

    @pytest.mark.asyncio
    async def test_prefiro_outro_numa_deixa_a_outra_valendo(self, sofia, hamilton2, meta):
        await _propor_duas(sofia, hamilton2)
        await _toque(sofia, PACIENTE, "Prefiro outro horário", "prop_nao:42", "wamid.p1")
        assert _textos_para(meta, PACIENTE)[-1] == encaminhamento.TEXTO_OPCAO_DESCARTADA
        conversa = await _conversa(sofia)
        assert encaminhamento.proposta_aberta(conversa) == 42
        assert list(encaminhamento.opcoes_abertas(conversa)) == ["2"]
        assert encaminhamento.recusa_sem_motivo(conversa) is None

        await _toque(sofia, PACIENTE, "Pode ser", "prop_sim:42", "wamid.p2")
        assert _textos_para(meta, PACIENTE)[-1] == encaminhamento.TEXTO_OPCAO_FECHADA

        await _toque(sofia, PACIENTE, "Pode ser", "prop_sim2:42", "wamid.p3")
        assert hamilton2.status == "aguardando_terapeuta"
        assert _textos_para(meta, PACIENTE)[-1] == encaminhamento.TEXTO_PACIENTE_SIM

    @pytest.mark.asyncio
    async def test_recusar_as_duas_volta_para_a_equipe(self, sofia, hamilton2, meta):
        await _propor_duas(sofia, hamilton2)
        await _toque(sofia, PACIENTE, "Prefiro outro horário", "prop_nao2:42", "wamid.p1")
        await _toque(sofia, PACIENTE, "Prefiro outro horário", "prop_nao:42", "wamid.p2")
        assert hamilton2.status == "recusado_paciente"
        assert _textos_para(meta, PACIENTE)[-1] == encaminhamento.TEXTO_PACIENTE_NAO
        conversa = await _conversa(sofia)
        assert encaminhamento.proposta_aberta(conversa) is None
        assert encaminhamento.recusa_sem_motivo(conversa) == 42

    @pytest.mark.asyncio
    async def test_aceite_escrito_sem_dizer_qual_pergunta(self, sofia, hamilton2, meta):
        await _propor_duas(sofia, hamilton2)
        llm = LLMRoteiro(
            _tool("responder_proposta", aceitou=True),
            llm_client.LLMResposta(texto="Qual das duas você prefere?"),
            _tool("responder_proposta", aceitou=True, opcao=2),
            llm_client.LLMResposta(texto="Combinado!"),
        )
        with patch("app.routers.webhook.llm_client.get_llm_client", return_value=llm):
            await _texto(sofia, PACIENTE, "pode ser", "wamid.p1")
            assert [r for r in hamilton2.respostas] == []  # nem foi ao Hamilton
            nota = " ".join(m["content"] for m in llm.chamadas[0]["historico"] if m["role"] == "system")
            assert "opção 2: Ulisses Teste, quarta, 18/11, às 9h" in nota
            await _texto(sofia, PACIENTE, "a da quarta", "wamid.p2")
        assert hamilton2.respostas == [{"de": "paciente", "aceitou": True, "motivo": "", "opcao": 2}]
        assert hamilton2.status == "aguardando_terapeuta"

    @pytest.mark.asyncio
    async def test_tela_manda_a_segunda_opcao(self, sofia, hamilton2, meta):
        await _logar(sofia, sofia.banco)
        resp = await sofia.post("/painel/encaminhamentos/42/reservar", data={
            "terapeuta_id": "7", "dia_semana": "1", "hora_inicio": "19:00",
            "dat_primeira_sessao": "2026-11-17", "alt_terapeuta_id": "8", "alt_dia_semana": "2",
            "alt_hora_inicio": "09:00", "alt_dat_primeira_sessao": "2026-11-18"})
        assert resp.status_code == 303
        assert hamilton2.reservas == [{
            "terapeuta_id": 7, "dia_semana": 1, "hora_inicio": "19:00",
            "dat_primeira_sessao": "2026-11-17",
            "alternativa": {"terapeuta_id": 8, "dia_semana": 2, "hora_inicio": "09:00",
                            "dat_primeira_sessao": "2026-11-18"}}]
        assert len(_templates(meta)) == 2

    @pytest.mark.asyncio
    async def test_pagina_tem_o_botao_da_segunda_opcao(self, sofia, hamilton, meta):
        await _logar(sofia, sofia.banco)
        html = (await sofia.get("/painel/encaminhamentos/42")).text
        assert 'id="enc-alt-adicionar"' in html and 'name="alt_terapeuta_id"' in html
