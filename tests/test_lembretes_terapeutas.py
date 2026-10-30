"""Lembretes dos terapeutas, de ponta a ponta pelo lado da Sofia.

Entra pelas portas de verdade — a rota do cron (`/tasks/lembretes-terapeutas`)
e o webhook assinado da Meta (`/webhook/whatsapp`) — e sai pelas de verdade: o
`HamiltonClient` faz HTTP (com JWT) contra um Hamilton falso que segue o mesmo
contrato que `principais/tests_e2e_demandas.py` valida no Hamilton, e o envio
para a Meta é capturado no choke point `whatsapp_client._enviar`.
"""

import hashlib
import hmac
import json
from unittest.mock import patch

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app import config
from app.database import Base
from app.main import app
from app.models import Conversa
from app.services import config_negocio, hamilton_client, lembretes_terapeutas, serializacao

TACIA = "5531988553344"
GEMEO = "5531977770000"  # número cadastrado em dois terapeutas

BALOES_HOJE = [
    "Oi, João! Passando para confirmar nossa sessão de hoje às 9h. Tudo certo para você?",
    "Oi, Maria! Passando para confirmar nossa sessão de hoje às 10h. Tudo certo para você?",
    "Outras atividades de hoje:\n\nGrupo de estudos - 14h",
]
AMBIGUO = ["Não consegui identificar seu cadastro. Fale com o Alan ou o Victor."]


class HamiltonFalso:
    """O Hamilton, do jeito que a Sofia o enxerga: JWT + as rotas de lembrete."""

    def __init__(self):
        self.fila = {
            "agenda": {"ativo": True, "template": "agenda_do_dia_terapeuta_v2", "data": "2026-10-29",
                       "envios": [{"terapeuta_id": 7, "telefone": "+" + TACIA,
                                   "variaveis": ["Tácia", "2 sessões (9h e 10h)", "1 atividade (14h)"],
                                   "botoes": ["agenda_hoje:2026-10-29", "agenda_semana:2026-10-29"]}]},
            "semanal": {"ativo": True, "template": "lembrete_semana_terapeuta",
                        "data": "2026-10-29", "envios": []},
        }
        self.confirmados: list[dict] = []
        self.detalhes: list[dict] = []
        self.fora_do_ar = False
        self.detalhe_fora_do_ar = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.fora_do_ar:
            return httpx.Response(502)
        caminho = request.url.path
        if caminho == "/authentication/token/":
            return httpx.Response(200, json={"access": "jwt-da-sofia"})
        if request.headers.get("Authorization") != "Bearer jwt-da-sofia":
            return httpx.Response(401)
        if caminho == "/api/v1/lembretes/agenda-do-dia/":
            return httpx.Response(200, json=self.fila["agenda"])
        if caminho == "/api/v1/lembretes/pendencias-semana/":
            return httpx.Response(200, json=self.fila["semanal"])
        if caminho == "/api/v1/lembretes/enviados/":
            corpo = json.loads(request.content)
            self.confirmados.append(corpo)
            # Como o Hamilton: confirmado sai da fila.
            fila = self.fila["agenda" if corpo["tipo"] == "agenda" else "semanal"]
            fila["envios"] = [e for e in fila["envios"] if e["terapeuta_id"] != corpo["terapeuta_id"]]
            return httpx.Response(200, json={"ok": True})
        if caminho == "/api/v1/terapeutas/por-telefone/":
            if request.url.params.get("telefone") in (TACIA, GEMEO):
                return httpx.Response(200, json={"terapeuta": True, "terapeuta_id": 7, "nome": "Tácia Teste"})
            return httpx.Response(404, json={"terapeuta": False})
        if caminho == "/api/v1/lembretes/detalhe/" and request.method == "POST":
            if self.detalhe_fora_do_ar:
                return httpx.Response(503)
            corpo = json.loads(request.content)
            self.detalhes.append(corpo)
            if corpo["telefone"] == GEMEO:
                return httpx.Response(409, json={"mensagens": AMBIGUO, "ambiguo": True})
            if corpo["telefone"] != TACIA:
                return httpx.Response(404, json={"detail": "Não é terapeuta ativo."})
            if corpo["payload"] == "agenda_hoje:2026-10-29":
                return httpx.Response(200, json={"mensagens": BALOES_HOJE})
            return httpx.Response(400, json={"detail": "Botão desconhecido."})
        return httpx.Response(404)


@pytest.fixture(autouse=True)
def _debounce_rapido():
    """Mesmo ajuste dos testes do webhook: janela curta e sem "digitando…"."""
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
    lembretes_terapeutas._respondidos.clear()
    lembretes_terapeutas._toques.clear()
    lembretes_terapeutas._wamids_vistos.clear()
    return falso


@pytest.fixture
def meta(monkeypatch):
    """Tudo o que sairia para a Cloud API da Meta."""
    enviados: list[dict] = []

    async def _enviar(payload, descricao, sigiloso=False):
        enviados.append(payload)
        return {"messages": [{"id": f"wamid.{len(enviados)}"}]}

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
    with patch("app.routers.webhook.async_session", maker):
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://sofia") as c:
            c.banco = maker
            yield c
    serializacao.limpar()
    await engine.dispose()


def _templates(enviados):
    return [p for p in enviados if p.get("type") == "template"]


def _textos(enviados):
    return [p for p in enviados if p.get("type") == "text"]


async def _cron(cliente):
    return await cliente.post("/tasks/lembretes-terapeutas", headers={"X-Tasks-Token": "segredo"})


async def _mensagem_de(cliente, numero, texto, wamid):
    corpo = json.dumps({"entry": [{"changes": [{"value": {"messages": [
        {"from": numero, "id": wamid, "type": "text", "text": {"body": texto}}]}}]}]}).encode()
    assinatura = "sha256=" + hmac.new(b"segredo-do-app", corpo, hashlib.sha256).hexdigest()
    resp = await cliente.post("/webhook/whatsapp", content=corpo,
                              headers={"X-Hub-Signature-256": assinatura, "Content-Type": "application/json"})
    assert resp.status_code == 200
    await serializacao.aguardar_pendentes()


async def _toque_de(cliente, numero, rotulo, payload, wamid):
    """Toque num botão de resposta rápida de template, como a Meta entrega."""
    botao = {"text": rotulo}
    if payload is not None:
        botao["payload"] = payload
    corpo = json.dumps({"entry": [{"changes": [{"value": {"messages": [
        {"from": numero, "id": wamid, "type": "button", "button": botao}]}}]}]}).encode()
    assinatura = "sha256=" + hmac.new(b"segredo-do-app", corpo, hashlib.sha256).hexdigest()
    resp = await cliente.post("/webhook/whatsapp", content=corpo,
                              headers={"X-Hub-Signature-256": assinatura, "Content-Type": "application/json"})
    assert resp.status_code == 200
    await serializacao.aguardar_pendentes()


async def _conversas(cliente):
    async with cliente.banco() as s:
        return (await s.execute(select(func.count()).select_from(Conversa))).scalar_one()


class TestCron:
    @pytest.mark.asyncio
    async def test_envia_a_agenda_confirma_e_nao_repete(self, sofia, hamilton, meta):
        resp = await _cron(sofia)
        assert resp.status_code == 200
        assert resp.json() == {"agenda": {"enviados": 1, "falhas": 0},
                               "semanal": {"enviados": 0, "falhas": 0}}

        [msg] = _templates(meta)
        assert msg["to"] == "+" + TACIA
        assert msg["template"]["name"] == "agenda_do_dia_terapeuta_v2"
        assert msg["template"]["language"] == {"code": "pt_BR"}
        corpo, botao0, botao1 = msg["template"]["components"]
        assert [p["text"] for p in corpo["parameters"]] == [
            "Tácia", "2 sessões (9h e 10h)", "1 atividade (14h)"]
        # O código de cada botão vai no envio e volta no toque (button.payload).
        assert botao0 == {"type": "button", "sub_type": "quick_reply", "index": "0",
                          "parameters": [{"type": "payload", "payload": "agenda_hoje:2026-10-29"}]}
        assert botao1["index"] == "1"
        assert botao1["parameters"][0]["payload"] == "agenda_semana:2026-10-29"
        assert hamilton.confirmados == [{"tipo": "agenda", "terapeuta_id": 7, "data": "2026-10-29"}]

        # Próxima rodada do cron (30 min depois): nada de novo.
        assert (await _cron(sofia)).json()["agenda"] == {"enviados": 0, "falhas": 0}
        assert len(_templates(meta)) == 1

    @pytest.mark.asyncio
    async def test_envio_sem_botoes_continua_valendo(self, sofia, hamilton, meta):
        del hamilton.fila["agenda"]["envios"][0]["botoes"]
        await _cron(sofia)
        [msg] = _templates(meta)
        assert [c["type"] for c in msg["template"]["components"]] == ["body"]

    @pytest.mark.asyncio
    async def test_dry_run_nao_confirma(self, sofia, hamilton, meta, monkeypatch):
        monkeypatch.setattr(config.settings, "whatsapp_dry_run", True)
        await _cron(sofia)
        # Confirmar sem ter enviado engoliria o lembrete real do dia.
        assert hamilton.confirmados == []

    @pytest.mark.asyncio
    async def test_hamilton_fora_nao_envia_nada(self, sofia, hamilton, meta):
        hamilton.fora_do_ar = True
        resp = await _cron(sofia)
        assert resp.status_code == 200
        assert resp.json()["agenda"] == {"erro": "hamilton_indisponivel"}
        assert meta == []

    @pytest.mark.asyncio
    async def test_sem_token_do_cron_e_proibido(self, sofia, hamilton, meta):
        resp = await sofia.post("/tasks/lembretes-terapeutas")
        assert resp.status_code == 403
        assert meta == []


class TestTerapeutaRespondendo:
    @pytest.mark.asyncio
    async def test_resposta_fixa_sem_llm_sem_conversa(self, sofia, hamilton, meta):
        with patch("app.routers.webhook.llm_client.get_llm_client") as llm:
            await _mensagem_de(sofia, TACIA, "ok, obrigada!", "wamid.t1")
            await _mensagem_de(sofia, TACIA, "e amanhã?", "wamid.t2")
        llm.assert_not_called()

        # Uma resposta fixa só (não repete a cada mensagem), e nenhuma conversa
        # nasce — então ela não aparece como lead no painel da Thainá.
        [resposta] = _textos(meta)
        assert resposta["to"] == TACIA
        assert resposta["text"]["body"] == lembretes_terapeutas.RESPOSTA_TERAPEUTA
        assert "Alan ou o Victor" in resposta["text"]["body"]
        assert await _conversas(sofia) == 0

    @pytest.mark.asyncio
    async def test_paciente_segue_o_fluxo_normal(self, sofia, hamilton, meta):
        class LLM:
            async def gerar_resposta(self, historico, tools=None, **kwargs):
                from app.services import llm_client
                return llm_client.LLMResposta(texto="Oi! Sou a Sofia da Allos.")

        with patch("app.routers.webhook.llm_client.get_llm_client", return_value=LLM()):
            await _mensagem_de(sofia, "5531911112222", "oi, quero terapia", "wamid.p1")

        assert await _conversas(sofia) == 1
        assert "Oi! Sou a Sofia da Allos." in [t["text"]["body"] for t in _textos(meta)]


class TestTerapeutaTocandoNoBotao:
    @pytest.mark.asyncio
    async def test_balloes_na_ordem_sem_llm_sem_conversa(self, sofia, hamilton, meta):
        with patch("app.routers.webhook.llm_client.get_llm_client") as llm:
            await _toque_de(sofia, TACIA, "Agenda de hoje", "agenda_hoje:2026-10-29", "wamid.b1")
        llm.assert_not_called()
        assert hamilton.detalhes == [{"telefone": TACIA, "payload": "agenda_hoje:2026-10-29"}]
        assert [t["text"]["body"] for t in _textos(meta)] == BALOES_HOJE
        assert all(t["to"] == TACIA for t in _textos(meta))
        # Marcado como lido, e nada vira conversa no painel.
        assert any(p.get("status") == "read" and p.get("message_id") == "wamid.b1" for p in meta)
        assert await _conversas(sofia) == 0

    @pytest.mark.asyncio
    async def test_toque_repetido_nao_manda_de_novo(self, sofia, hamilton, meta):
        await _toque_de(sofia, TACIA, "Agenda de hoje", "agenda_hoje:2026-10-29", "wamid.b1")
        await _toque_de(sofia, TACIA, "Agenda de hoje", "agenda_hoje:2026-10-29", "wamid.b1")  # reentrega
        await _toque_de(sofia, TACIA, "Agenda de hoje", "agenda_hoje:2026-10-29", "wamid.b2")  # dedo nervoso
        assert len(hamilton.detalhes) == 1
        assert len(_textos(meta)) == len(BALOES_HOJE)

    @pytest.mark.asyncio
    async def test_toque_depois_de_30s_responde_de_novo(self, sofia, hamilton, meta, monkeypatch):
        relogio = [1000.0]
        monkeypatch.setattr(lembretes_terapeutas.time, "monotonic", lambda: relogio[0])
        await _toque_de(sofia, TACIA, "Agenda de hoje", "agenda_hoje:2026-10-29", "wamid.b1")
        relogio[0] += 31
        await _toque_de(sofia, TACIA, "Agenda de hoje", "agenda_hoje:2026-10-29", "wamid.b2")
        assert len(hamilton.detalhes) == 2
        assert len(_textos(meta)) == 2 * len(BALOES_HOJE)

    @pytest.mark.asyncio
    async def test_numero_de_dois_terapeutas_recebe_so_o_aviso(self, sofia, hamilton, meta):
        await _toque_de(sofia, GEMEO, "Agenda de hoje", "agenda_hoje:2026-10-29", "wamid.b1")
        assert [t["text"]["body"] for t in _textos(meta)] == AMBIGUO

    @pytest.mark.asyncio
    async def test_botao_desconhecido_cai_na_resposta_fixa(self, sofia, hamilton, meta):
        await _toque_de(sofia, TACIA, "Outro botão", "inventado:2026-10-29", "wamid.b1")
        assert [t["text"]["body"] for t in _textos(meta)] == [lembretes_terapeutas.RESPOSTA_TERAPEUTA]

    @pytest.mark.asyncio
    async def test_hamilton_fora_avisa_e_libera_novo_toque(self, sofia, hamilton, meta):
        hamilton.detalhe_fora_do_ar = True
        await _toque_de(sofia, TACIA, "Agenda de hoje", "agenda_hoje:2026-10-29", "wamid.b1")
        assert [t["text"]["body"] for t in _textos(meta)] == [lembretes_terapeutas.ERRO_DETALHE]
        hamilton.detalhe_fora_do_ar = False
        await _toque_de(sofia, TACIA, "Agenda de hoje", "agenda_hoje:2026-10-29", "wamid.b2")
        assert [t["text"]["body"] for t in _textos(meta)][1:] == BALOES_HOJE

    @pytest.mark.asyncio
    async def test_sem_payload_usa_o_rotulo(self, sofia, hamilton, meta):
        # Template sem código no envio: a Meta devolve o rótulo no payload; se
        # vier vazio, a Sofia manda o texto, e o Hamilton recusa (400).
        await _toque_de(sofia, TACIA, "Agenda de hoje", None, "wamid.b1")
        assert hamilton.detalhes == [{"telefone": TACIA, "payload": "Agenda de hoje"}]
        assert [t["text"]["body"] for t in _textos(meta)] == [lembretes_terapeutas.RESPOSTA_TERAPEUTA]

    @pytest.mark.asyncio
    async def test_texto_digitado_continua_na_resposta_fixa(self, sofia, hamilton, meta):
        await _mensagem_de(sofia, TACIA, "obrigada!", "wamid.t1")
        assert hamilton.detalhes == []
        assert [t["text"]["body"] for t in _textos(meta)] == [lembretes_terapeutas.RESPOSTA_TERAPEUTA]

    @pytest.mark.asyncio
    async def test_dry_run_nao_registra_o_texto_dos_balloes(self, sofia, hamilton, monkeypatch, caplog):
        monkeypatch.setattr(config.settings, "whatsapp_dry_run", True)
        monkeypatch.setattr(config.settings, "whatsapp_app_secret", "segredo-do-app")
        with caplog.at_level("WARNING"):
            await _toque_de(sofia, TACIA, "Agenda de hoje", "agenda_hoje:2026-10-29", "wamid.b1")
        assert "João" not in caplog.text and "Maria" not in caplog.text
        assert "caracteres omitidos" in caplog.text
