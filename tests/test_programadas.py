# ruff: noqa: F811 — as fixtures vêm importadas de test_contato e são reusadas pelo nome
"""Mensagem programada: a Sofia manda sozinha numa data marcada (programadas.py).

Pedido da Thainá (23/11/2026): "se o paciente diz que só pode começar no dia
20, quero programar a Sofia pra mandar mensagem pra ele no dia 20". O que estes
testes seguram:

1. a equipe programa e cancela pela conversa; data no passado não entra;
2. no dia, com a janela de 24h aberta, sai o texto; fechada, sai o template de
   início de contato e o texto vira contexto da Sofia quando a pessoa responder;
3. falha de envio tenta de novo e, na terceira, avisa a equipe;
4. a Sofia programa sozinha pela ferramenta `programar_retomada`.
"""

from datetime import date, datetime, time, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.models import Conversa, Mensagem, MensagemProgramada
from app.routers import webhook
from app.services import config_negocio, llm_client, programadas, whatsapp_client
from tests.test_contato import (  # noqa: F401 — fixtures
    _config_de_teste,
    _ler,
    _login,
    _mensagem_de,
    _mensagens,
    _nova,
    hamilton,
    meta,
    sofia,
)

PACIENTE = "5531977778888"


@pytest.fixture(autouse=True)
def _template():
    config_negocio._cache["template_inicio_contato"] = "inicio_contato_allos"
    yield


def _templates(enviados):
    return [p for p in enviados if p.get("type") == "template"]


def _textos_para(enviados, numero):
    return [p["text"]["body"] for p in enviados if p.get("type") == "text" and p["to"] == numero]


async def _programadas(cliente, conversa_id) -> list[MensagemProgramada]:
    async with cliente.banco() as s:
        q = select(MensagemProgramada).where(MensagemProgramada.conversa_id == conversa_id)
        return list((await s.execute(q)).scalars().all())


async def _programar(cliente, conversa_id, texto="Oi, Ana! Como combinamos, vamos começar?",
                     quando=None, por="thaina"):
    async with cliente.banco() as s:
        conversa = await s.get(Conversa, conversa_id)
        p = await programadas.programar(
            s, conversa, texto, quando or programadas.agora_utc() + timedelta(days=2), por)
        await s.commit()
        return p.id


async def _rodar(cliente, daqui=timedelta(days=3)):
    async with cliente.banco() as s:
        return await programadas.rodar(s, programadas.agora_utc() + daqui)


async def _recebida(cliente, conversa_id, quando):
    async with cliente.banco() as s:
        s.add(Mensagem(conversa_id=conversa_id, direcao="recebida", origem="paciente",
                       tipo="texto", texto="oi", criada_em=quando))
        await s.commit()


def test_brasilia_vira_utc():
    assert programadas.de_brasilia(date(2026, 12, 20), time(10, 0)) == datetime(
        2026, 12, 20, 13, 0, tzinfo=timezone.utc)
    assert programadas.de_brasilia(date(2026, 12, 20)).hour == 13  # 10h padrão


class TestTela:
    @pytest.mark.asyncio
    async def test_programar_e_cancelar(self, sofia, hamilton, meta):
        cid = await _nova(sofia, PACIENTE, dados_coletados={"nome_completo": "Ana Lima"})
        await _login(sofia)
        dia = (date.today() + timedelta(days=5)).isoformat()
        resp = await sofia.post(f"/painel/conversas/{cid}/programar",
                                data={"dia": dia, "hora": "09:30", "texto": "Oi, Ana!"})
        assert resp.status_code == 303 and "programada=ok" in resp.headers["location"]
        [p] = await _programadas(sofia, cid)
        assert programadas.em_brasilia(p.enviar_em).strftime("%H:%M") == "09:30"
        assert p.criada_por == "thaina"

        html = (await sofia.get(f"/painel/conversas/{cid}/?programada=ok")).text
        assert "Mensagem programada. A Sofia manda" in html
        assert "Oi, Ana!" in html and "Programada" in html

        resp = await sofia.post(f"/painel/conversas/{cid}/programadas/{p.id}/cancelar")
        assert "programada=cancelada" in resp.headers["location"]
        [p] = await _programadas(sofia, cid)
        assert p.cancelada_em is not None
        assert (await _rodar(sofia, timedelta(days=10))) == {"enviadas": 0, "nao_enviadas": 0}
        assert meta == []

    @pytest.mark.asyncio
    async def test_data_no_passado_nao_entra(self, sofia, hamilton, meta):
        cid = await _nova(sofia, PACIENTE)
        await _login(sofia)
        ontem = (date.today() - timedelta(days=1)).isoformat()
        resp = await sofia.post(f"/painel/conversas/{cid}/programar",
                                data={"dia": ontem, "hora": "10:00", "texto": "Oi"})
        assert "programada=passado" in resp.headers["location"]
        resp = await sofia.post(f"/painel/conversas/{cid}/programar",
                                data={"dia": "amanhã", "hora": "10:00", "texto": "Oi"})
        assert "programada=data" in resp.headers["location"]
        assert await _programadas(sofia, cid) == []

    @pytest.mark.asyncio
    async def test_cancelar_de_outra_conversa_nao_vale(self, sofia, hamilton, meta):
        cid = await _nova(sofia, PACIENTE)
        outra = await _nova(sofia, "5531911110000")
        pid = await _programar(sofia, cid)
        await _login(sofia)
        await sofia.post(f"/painel/conversas/{outra}/programadas/{pid}/cancelar")
        [p] = await _programadas(sofia, cid)
        assert p.cancelada_em is None


class TestDisparo:
    @pytest.mark.asyncio
    async def test_antes_da_hora_nao_sai(self, sofia, hamilton, meta):
        cid = await _nova(sofia, PACIENTE)
        await _programar(sofia, cid)
        assert (await _rodar(sofia, timedelta(0))) == {"enviadas": 0, "nao_enviadas": 0}

    @pytest.mark.asyncio
    async def test_janela_aberta_sai_o_texto(self, sofia, hamilton, meta):
        cid = await _nova(sofia, PACIENTE)
        await _programar(sofia, cid)
        # A pessoa escreveu há pouco (em relação ao momento do disparo).
        await _recebida(sofia, cid, programadas.agora_utc() + timedelta(days=2, hours=12))
        assert (await _rodar(sofia)) == {"enviadas": 1, "nao_enviadas": 0}
        assert _textos_para(meta, PACIENTE) == ["Oi, Ana! Como combinamos, vamos começar?"]
        [p] = await _programadas(sofia, cid)
        assert p.enviada_como == "texto"
        assert (await _mensagens(sofia, cid))[-1].origem == "bot"
        # Não sai de novo.
        assert (await _rodar(sofia)) == {"enviadas": 0, "nao_enviadas": 0}

    @pytest.mark.asyncio
    async def test_janela_fechada_sai_o_convite_e_a_sofia_retoma(self, sofia, hamilton, meta):
        cid = await _nova(sofia, PACIENTE, dados_coletados={"nome_completo": "Ana Lima"})
        await _programar(sofia, cid, texto="Oi, Ana! Já é dia 20, vamos deixar tudo pronto?")
        await _rodar(sofia)
        [convite] = _templates(meta)
        assert convite["template"]["name"] == "inicio_contato_allos"
        assert convite["template"]["components"][0]["parameters"][0]["text"] == "Ana"
        conversa = await _ler(sofia, cid)
        assert conversa.modo == "bot"  # não assume: quem responde é a Sofia

        class _LLM:
            historicos = []

            async def gerar_resposta(self, historico, tools=None, **kwargs):
                self.historicos.append(historico)
                return llm_client.LLMResposta(texto="Oi, Ana! Vamos deixar tudo pronto?")

        llm = _LLM()
        with patch("app.routers.webhook.llm_client.get_llm_client", return_value=llm), patch(
            "app.routers.webhook.captacao.listar", new_callable=AsyncMock, return_value=[]
        ):
            await _mensagem_de(sofia, PACIENTE, "oi, quem é?", "wamid.r1")
            notas = [m["content"] for m in llm.historicos[0] if m["role"] == "system"]
            assert any("Já é dia 20, vamos deixar tudo pronto?" in n for n in notas)
            # Vale um turno só.
            await _mensagem_de(sofia, PACIENTE, "ah sim", "wamid.r2")
            notas = [m["content"] for m in llm.historicos[1] if m["role"] == "system"]
            assert not any("mensagem programada" in n for n in notas)

    @pytest.mark.asyncio
    async def test_falha_tenta_de_novo_e_avisa_na_terceira(self, sofia, hamilton, meta):
        cid = await _nova(sofia, PACIENTE, dados_coletados={"nome_completo": "Ana Lima"})
        await _programar(sofia, cid)
        with patch("app.services.programadas.whatsapp_client.enviar_template",
                   new_callable=AsyncMock, side_effect=whatsapp_client.WhatsAppError("recusado")), \
                patch("app.services.programadas.escalation.alertar_equipe",
                      new_callable=AsyncMock) as alerta:
            for _ in range(4):
                await _rodar(sofia)
        [p] = await _programadas(sofia, cid)
        assert p.tentativas == programadas.TENTATIVAS_MAXIMAS
        assert p.enviada_em is None and "recusado" in p.erro
        alerta.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_da_sofia_nao_sai_para_quem_ja_cadastrou(self, sofia, hamilton, meta):
        cid = await _nova(sofia, PACIENTE)
        await _programar(sofia, cid, por=programadas.SOFIA)
        async with sofia.banco() as s:
            c = await s.get(Conversa, cid)
            c.paciente_hamilton_id = 900
            await s.commit()
        await _rodar(sofia)
        assert meta == []
        [p] = await _programadas(sofia, cid)
        assert p.cancelada_em is not None

    @pytest.mark.asyncio
    async def test_cron_dispara(self, sofia, hamilton, meta):
        cid = await _nova(sofia, PACIENTE)
        await _programar(sofia, cid, quando=programadas.agora_utc() + timedelta(seconds=1))
        async with sofia.banco() as s:
            await s.execute(
                MensagemProgramada.__table__.update().values(
                    enviar_em=programadas.agora_utc() - timedelta(minutes=1)))
            await s.commit()
        with patch("app.routers.tasks.lembretes_terapeutas.rodar",
                   new_callable=AsyncMock, return_value={}), \
                patch("app.routers.tasks.encaminhamento.rodar",
                      new_callable=AsyncMock, return_value={}), \
                patch("app.routers.tasks.contato.conferir", new_callable=AsyncMock, return_value={}):
            resp = await sofia.post("/tasks/lembretes-terapeutas",
                                    headers={"X-Tasks-Token": "segredo"})
        assert resp.json()["programadas"] == {"enviadas": 1, "nao_enviadas": 0}


class TestFerramentaDaSofia:
    @pytest.mark.asyncio
    async def test_programar_retomada(self, sofia, hamilton, meta):
        cid = await _nova(sofia, PACIENTE)
        dia = programadas.em_brasilia(programadas.agora_utc()).date() + timedelta(days=10)
        async with sofia.banco() as s:
            conversa = await s.get(Conversa, cid)
            r = await webhook._executar_tool(s, conversa, llm_client.ToolCall(
                id="t1", name="programar_retomada",
                arguments={"data": dia.isoformat(), "mensagem": "Oi, Ana! Chegou o dia 20."}))
            await s.commit()
        assert r["status"] == "programada"
        [p] = await _programadas(sofia, cid)
        assert p.criada_por == "sofia"
        assert programadas.em_brasilia(p.enviar_em).date() == dia
        assert programadas.em_brasilia(p.enviar_em).hour == 10

    @pytest.mark.asyncio
    @pytest.mark.parametrize("data", ["", "20/12", "2020-01-01"])
    async def test_data_ruim_nao_programa(self, sofia, hamilton, meta, data):
        cid = await _nova(sofia, PACIENTE)
        async with sofia.banco() as s:
            conversa = await s.get(Conversa, cid)
            r = await programadas.programar_pela_sofia(s, conversa, {"data": data, "mensagem": "Oi"})
        assert r["status"] == "data_invalida"
        assert await _programadas(sofia, cid) == []
