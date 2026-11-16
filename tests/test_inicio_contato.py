# ruff: noqa: F811 — as fixtures vêm importadas de test_contato e são reusadas pelo nome
"""A equipe inicia a conversa pelo painel (Demanda G — docs/demandas/08).

O que estes testes seguram:

1. o número digitado vira `55DD...` e só celular brasileiro passa;
2. número repetido (inclusive na outra forma do nono dígito) não duplica;
3. iniciar manda o template com o primeiro nome, grava, e a equipe assume;
4. a 1ª resposta abre escalada + alerta, sem triagem e sem o aviso de escalada;
5. a resposta que chega sem o nono dígito cai na MESMA conversa.
"""

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.models import Conversa, Escalada
from app.services import config_negocio, hoje, inicio_contato
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

NOVO = "5531966665555"
NOVO_SEM_9 = "553166665555"


@pytest.fixture(autouse=True)
def _template():
    config_negocio._cache["template_inicio_contato"] = "inicio_contato_allos"
    yield


def _templates(enviados):
    return [p for p in enviados if p.get("type") == "template"]


class TestNumero:
    @pytest.mark.parametrize(
        "digitado,esperado",
        [
            ("(31) 96666-5555", "5531966665555"),
            ("31966665555", "5531966665555"),
            ("+55 31 96666-5555", "5531966665555"),
            ("3166665555", "553166665555"),  # sem o nono dígito: ainda é celular
        ],
    )
    def test_normaliza(self, digitado, esperado):
        assert inicio_contato.normalizar_numero_br(digitado) == esperado

    @pytest.mark.parametrize("digitado", ["", "966665555", "31 3333-4444", "(31) 86666-5555", "+1 415 555 1234"])
    def test_recusa_o_que_nao_e_celular_br(self, digitado):
        with pytest.raises(inicio_contato.NumeroInvalido):
            inicio_contato.normalizar_numero_br(digitado)

    def test_variantes_do_nono_digito(self):
        assert inicio_contato.variantes(NOVO) == [NOVO, NOVO_SEM_9]
        assert inicio_contato.variantes(NOVO_SEM_9) == [NOVO_SEM_9, NOVO]


class TestNovoContato:
    @pytest.mark.asyncio
    async def test_cria_manda_o_convite_e_assume(self, sofia, hamilton, meta):
        await _login(sofia)
        resp = await sofia.post(
            "/painel/contatos/novo",
            data={"nome": "Maria Souza", "numero": "(31) 96666-5555", "tipo": "paciente",
                  "nota": "retorno da vaga de quinta", "consentimento": "1"},
        )
        assert resp.status_code == 303 and "contato=criado" in resp.headers["location"]

        [template] = _templates(meta)
        assert template["to"] == NOVO
        assert template["template"]["name"] == "inicio_contato_allos"
        assert template["template"]["components"][0]["parameters"] == [{"type": "text", "text": "Maria"}]

        async with sofia.banco() as s:
            conversa = (await s.execute(select(Conversa))).scalar_one()
        assert conversa.modo == "humano"
        assert conversa.contato_iniciado_por == "thaina"
        assert conversa.contato_consentimento_por == "thaina"
        assert conversa.contato_nota == "retorno da vaga de quinta"
        assert not conversa.eh_terapeuta
        [msg] = await _mensagens(sofia, conversa.id)
        assert msg.tipo == "template" and msg.origem == "thaina"

    @pytest.mark.asyncio
    async def test_terapeuta_vai_para_a_aba_dele(self, sofia, hamilton, meta):
        await _login(sofia)
        await sofia.post(
            "/painel/contatos/novo",
            data={"nome": "Renato", "numero": "31966665555", "tipo": "terapeuta", "consentimento": "1"},
        )
        assert "Renato" in (await sofia.get("/painel/terapeutas")).text
        assert "Renato" not in (await sofia.get("/painel/conversas")).text

    @pytest.mark.asyncio
    async def test_sem_consentimento_nao_cria_nem_envia(self, sofia, hamilton, meta):
        await _login(sofia)
        resp = await sofia.post(
            "/painel/contatos/novo", data={"nome": "Maria", "numero": "31966665555"}
        )
        assert resp.status_code == 400
        assert "passou o número" in resp.text
        assert meta == []
        async with sofia.banco() as s:
            assert (await s.execute(select(Conversa))).first() is None

    @pytest.mark.asyncio
    async def test_numero_invalido_volta_com_o_que_foi_digitado(self, sofia, hamilton, meta):
        await _login(sofia)
        resp = await sofia.post(
            "/painel/contatos/novo",
            data={"nome": "Maria Souza", "numero": "3133334444", "consentimento": "1"},
        )
        assert resp.status_code == 400
        assert "fixo" in resp.text and 'value="Maria Souza"' in resp.text

    @pytest.mark.asyncio
    async def test_numero_que_ja_existe_abre_a_conversa_dele(self, sofia, hamilton, meta):
        # Gravado sem o nono dígito; a equipe digita com ele.
        cid = await _nova(sofia, NOVO_SEM_9)
        await _login(sofia)
        resp = await sofia.post(
            "/painel/contatos/novo",
            data={"nome": "Maria", "numero": "31966665555", "consentimento": "1"},
        )
        assert resp.headers["location"] == f"/painel/conversas/{cid}/?contato=existia"
        assert meta == []

    @pytest.mark.asyncio
    async def test_wa_id_da_meta_substitui_o_numero_digitado(self, sofia, hamilton, monkeypatch):
        async def _enviar(payload, descricao, sigiloso=False):
            return {"contacts": [{"input": NOVO, "wa_id": NOVO_SEM_9}],
                    "messages": [{"id": "wamid.x"}]}

        monkeypatch.setattr("app.services.whatsapp_client._enviar", _enviar)
        await _login(sofia)
        await sofia.post(
            "/painel/contatos/novo",
            data={"nome": "Maria", "numero": "31966665555", "consentimento": "1"},
        )
        async with sofia.banco() as s:
            conversa = (await s.execute(select(Conversa))).scalar_one()
        assert conversa.numero_whatsapp == NOVO_SEM_9


class TestIniciarNaConversa:
    @pytest.mark.asyncio
    async def test_janela_fechada_mostra_o_botao(self, sofia, hamilton, meta):
        cid = await _nova(sofia, NOVO, dados_coletados={"nome_completo": "Maria"})
        await _login(sofia)
        html = (await sofia.get(f"/painel/conversas/{cid}/")).text
        assert "iniciar-contato" in html and "ainda não escreveu" in html
        assert 'id="compositor-form"' not in html

    @pytest.mark.asyncio
    async def test_janela_aberta_mostra_o_compositor_com_o_prazo(self, sofia, hamilton, meta):
        await _mensagem_de(sofia, NOVO, "oi", "wamid.in.1")
        async with sofia.banco() as s:
            conversa = (await s.execute(select(Conversa))).scalar_one()
            conversa.modo = "humano"
            await s.commit()
        await _login(sofia)
        html = (await sofia.get(f"/painel/conversas/{conversa.id}/")).text
        assert 'id="compositor-form"' in html and "Janela aberta até" in html

    @pytest.mark.asyncio
    async def test_sem_template_configurado_nao_envia(self, sofia, hamilton, meta):
        config_negocio._cache["template_inicio_contato"] = ""
        cid = await _nova(sofia, NOVO)
        await _login(sofia)
        resp = await sofia.post(f"/painel/conversas/{cid}/iniciar-contato")
        assert resp.headers["location"].endswith("contato=sem_template")
        assert meta == []

    @pytest.mark.asyncio
    async def test_falha_da_meta_nao_grava_nada(self, sofia, hamilton, monkeypatch):
        from app.services import whatsapp_client

        async def _falha(payload, descricao, sigiloso=False):
            raise whatsapp_client.WhatsAppError("template não aprovado")

        monkeypatch.setattr("app.services.whatsapp_client._enviar", _falha)
        cid = await _nova(sofia, NOVO)
        await _login(sofia)
        resp = await sofia.post(f"/painel/conversas/{cid}/iniciar-contato")
        assert resp.headers["location"].endswith("contato=erro")
        assert await _mensagens(sofia, cid) == []
        assert (await _ler(sofia, cid)).contato_iniciado_em is None


class TestResposta:
    async def _iniciado(self, sofia, numero=NOVO, **campos):
        cid = await _nova(sofia, numero, dados_coletados={"nome_completo": "Maria"}, **campos)
        await _login(sofia)
        await sofia.post(f"/painel/conversas/{cid}/iniciar-contato")
        return cid

    @pytest.mark.asyncio
    async def test_primeira_resposta_escala_e_alerta_uma_vez(self, sofia, hamilton, meta):
        cid = await self._iniciado(sofia)
        meta.clear()
        with patch("app.services.escalation.alertar_thaina", new=AsyncMock(return_value=True)) as alerta, \
                patch("app.routers.webhook.llm_client.get_llm_client") as llm:
            await _mensagem_de(sofia, NOVO, "oi, pode falar", "wamid.r1")
            await _mensagem_de(sofia, NOVO, "estou aqui", "wamid.r2")
        llm.assert_not_called()
        assert alerta.await_count == 1
        # Nem triagem nem "a equipe já vai te responder": foi a equipe que chamou.
        assert meta == [] or all(p.get("type") != "text" for p in meta)

        async with sofia.banco() as s:
            [esc] = (await s.execute(select(Escalada))).scalars().all()
        assert esc.motivo == "respondeu_contato" and "thaina" in (esc.contexto or "")
        conversa = await _ler(sofia, cid)
        assert conversa.contato_respondido_em is not None and conversa.modo == "humano"

    @pytest.mark.asyncio
    async def test_resposta_sem_o_nono_digito_cai_na_mesma_conversa(self, sofia, hamilton, meta):
        cid = await self._iniciado(sofia)
        with patch("app.services.escalation.alertar_thaina", new=AsyncMock(return_value=True)):
            await _mensagem_de(sofia, NOVO_SEM_9, "oi", "wamid.r9")
        async with sofia.banco() as s:
            conversas = (await s.execute(select(Conversa))).scalars().all()
        assert [c.id for c in conversas] == [cid]
        assert conversas[0].numero_whatsapp == NOVO_SEM_9
        assert [m.texto for m in await _mensagens(sofia, cid)][-1] == "oi"

    @pytest.mark.asyncio
    async def test_terapeuta_que_responde_entra_na_fila_hoje(self, sofia, hamilton, meta):
        cid = await self._iniciado(sofia, eh_terapeuta=True)
        with patch("app.services.escalation.alertar_thaina", new=AsyncMock(return_value=True)):
            await _mensagem_de(sofia, NOVO, "oi", "wamid.t1")
        async with sofia.banco() as s:
            itens = await hoje.listar_pendencias(s)
        assert [i["conversa_id"] for i in itens] == [cid]
