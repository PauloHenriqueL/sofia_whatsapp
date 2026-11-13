"""Paciente ou terapeuta: a marca na conversa e o que ela muda.

O número da Sofia é um só. Terapeuta que o Hamilton reconhece nem vira conversa,
mas a lista tinha conversas de terapeuta iguais às de paciente (anteriores à
regra, ou de quem o Hamilton não reconhece). O que estes testes seguram:

1. a marca chega pelos dois caminhos (Hamilton e botão) e a escolha feita à mão
   não é desfeita pela conferência;
2. conversa marcada deixa de ser tratada como paciente, sem que nada se apague;
3. ela continua visível no painel, com etiqueta e filtro.
"""

import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app import config
from app.config import settings
from app.database import Base, get_db
from app.main import app
from app.models import Conversa, Escalada, Mensagem
from app.services import (
    cadastro_abandonado,
    cobranca,
    config_negocio,
    contato,
    hamilton_client,
    hoje,
    lembretes_terapeutas,
    llm_client,
    metricas,
    pesquisa,
    seguimento,
    serializacao,
    usuarios,
)

TACIA = "5531988553344"  # terapeuta ativa, com o telefone igual ao do cadastro
RENATO = "5531977771111"  # terapeuta que o Hamilton NÃO reconhece (telefone diferente)
PACIENTE = "5531911112222"


class HamiltonFalso:
    """Só o que estes testes usam: o JWT e a busca de terapeuta por telefone."""

    def __init__(self):
        self.fora_do_ar = False
        self.consultados: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.fora_do_ar:
            return httpx.Response(502)
        caminho = request.url.path
        if caminho == "/authentication/token/":
            return httpx.Response(200, json={"access": "jwt"})
        if caminho == "/api/v1/terapeutas/por-telefone/":
            telefone = request.url.params.get("telefone")
            self.consultados.append(telefone)
            if telefone == TACIA:
                return httpx.Response(200, json={"terapeuta": True, "terapeuta_id": 7})
            return httpx.Response(404, json={"terapeuta": False})
        # Filas do cron (lembretes, encaminhamento): vazias e desligadas.
        return httpx.Response(200, json={"ativo": False, "envios": []})


@pytest.fixture(autouse=True)
def _config_de_teste():
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
        base_url="http://hamilton",
        username="sofia-bot",
        password="x",
        client=httpx.AsyncClient(transport=httpx.MockTransport(falso)),
    )
    monkeypatch.setattr(hamilton_client, "get_hamilton_client", lambda: cliente)
    lembretes_terapeutas._cache.clear()
    lembretes_terapeutas._respondidos.clear()
    return falso


@pytest.fixture
def meta(monkeypatch):
    """Tudo o que sairia para a Cloud API da Meta."""
    enviados: list[dict] = []

    async def _enviar(payload, descricao, sigiloso=False):
        enviados.append(payload)
        return {"messages": [{"id": f"wamid.saida.{len(enviados)}"}]}

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

    async with maker() as s:
        await usuarios.migrar_usuario_do_env(s)

    app.dependency_overrides[get_db] = _db
    with patch("app.routers.webhook.async_session", maker):
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://sofia"
        ) as c:
            c.banco = maker
            yield c
    app.dependency_overrides.pop(get_db, None)
    serializacao.limpar()
    await engine.dispose()


async def _nova(cliente, numero, **campos) -> int:
    async with cliente.banco() as s:
        conversa = Conversa(numero_whatsapp=numero, **campos)
        s.add(conversa)
        await s.commit()
        return conversa.id


async def _ler(cliente, conversa_id) -> Conversa:
    async with cliente.banco() as s:
        return await s.get(Conversa, conversa_id)


async def _mensagens(cliente, conversa_id) -> list[Mensagem]:
    async with cliente.banco() as s:
        q = select(Mensagem).where(Mensagem.conversa_id == conversa_id).order_by(Mensagem.id)
        return list((await s.execute(q)).scalars().all())


async def _mensagem_de(cliente, numero, texto, wamid):
    corpo = json.dumps(
        {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "messages": [
                                    {
                                        "from": numero,
                                        "id": wamid,
                                        "type": "text",
                                        "text": {"body": texto},
                                    }
                                ]
                            }
                        }
                    ]
                }
            ]
        }
    ).encode()
    assinatura = "sha256=" + hmac.new(b"segredo-do-app", corpo, hashlib.sha256).hexdigest()
    resp = await cliente.post(
        "/webhook/whatsapp",
        content=corpo,
        headers={"X-Hub-Signature-256": assinatura, "Content-Type": "application/json"},
    )
    assert resp.status_code == 200
    await serializacao.aguardar_pendentes()


def _textos(enviados):
    return [p["text"]["body"] for p in enviados if p.get("type") == "text"]


class _LLM:
    async def gerar_resposta(self, historico, tools=None, **kwargs):
        return llm_client.LLMResposta(texto="Oi! Sou a Sofia da Allos.")


class TestMarcadaAMaoEscreve:
    """Quem o Hamilton não reconhece e alguém marcou no painel."""

    @pytest.mark.asyncio
    async def test_guarda_a_mensagem_e_responde_o_texto_fixo_sem_llm(self, sofia, hamilton, meta):
        cid = await _nova(sofia, RENATO, eh_terapeuta=True, contato_conferido_por=contato.MANUAL)

        with patch("app.routers.webhook.llm_client.get_llm_client") as llm:
            await _mensagem_de(sofia, RENATO, "oi, quem atende a Marina?", "wamid.r1")
            await _mensagem_de(sofia, RENATO, "alguém?", "wamid.r2")
        llm.assert_not_called()

        # A resposta fixa sai uma vez só, mesmo com duas mensagens seguidas.
        assert _textos(meta) == [lembretes_terapeutas.RESPOSTA_TERAPEUTA]
        # Nada se perde: as duas mensagens dele e a resposta ficam na conversa.
        assert [(m.direcao, m.texto) for m in await _mensagens(sofia, cid)] == [
            ("recebida", "oi, quem atende a Marina?"),
            ("enviada", lembretes_terapeutas.RESPOSTA_TERAPEUTA),
            ("recebida", "alguém?"),
        ]

    @pytest.mark.asyncio
    async def test_com_a_equipe_conversando_a_sofia_fica_calada(self, sofia, hamilton, meta):
        cid = await _nova(
            sofia, RENATO, modo="humano", eh_terapeuta=True, contato_conferido_por=contato.MANUAL
        )

        with patch("app.routers.webhook.llm_client.get_llm_client") as llm:
            await _mensagem_de(sofia, RENATO, "pode ser às 15h", "wamid.r1")
        llm.assert_not_called()

        # "Este número não lê respostas" no meio de uma conversa com a equipe
        # seria mentira; e o aviso de escalada é de paciente.
        assert meta == []
        assert [m.texto for m in await _mensagens(sofia, cid)] == ["pode ser às 15h"]


class TestHamiltonReconhece:
    @pytest.mark.asyncio
    async def test_conversa_antiga_ganha_a_marca_quando_o_terapeuta_escreve(
        self, sofia, hamilton, meta
    ):
        cid = await _nova(sofia, TACIA)  # nasceu antes da regra, como lead

        with patch("app.routers.webhook.llm_client.get_llm_client") as llm:
            await _mensagem_de(sofia, TACIA, "ok, obrigada!", "wamid.t1")
        llm.assert_not_called()

        conversa = await _ler(sofia, cid)
        assert conversa.eh_terapeuta is True
        assert conversa.contato_conferido_por == contato.HAMILTON
        assert _textos(meta) == [lembretes_terapeutas.RESPOSTA_TERAPEUTA]

    @pytest.mark.asyncio
    async def test_marcada_a_mao_como_paciente_vence_o_hamilton(self, sofia, hamilton, meta):
        cid = await _nova(sofia, TACIA, eh_terapeuta=False, contato_conferido_por=contato.MANUAL)

        with patch("app.routers.webhook.llm_client.get_llm_client", return_value=_LLM()):
            await _mensagem_de(sofia, TACIA, "quero marcar terapia pra mim", "wamid.t1")

        # A pessoa disse que aqui ela é paciente: a Sofia atende como paciente.
        assert "Oi! Sou a Sofia da Allos." in _textos(meta)
        assert (await _ler(sofia, cid)).eh_terapeuta is False


class TestConferencia:
    @pytest.mark.asyncio
    async def test_marca_quem_o_hamilton_reconhece_e_registra_quem_nao(self, sofia, hamilton):
        tacia = await _nova(sofia, TACIA)
        paciente = await _nova(sofia, PACIENTE)

        async with sofia.banco() as s:
            resumo = await contato.conferir(s)

        assert resumo == {"conferidas": 2, "terapeutas": 1}
        assert (await _ler(sofia, tacia)).eh_terapeuta is True
        conferido = await _ler(sofia, paciente)
        assert (conferido.eh_terapeuta, conferido.contato_conferido_por) == (False, "hamilton")

        # Cada conversa é conferida uma vez: a segunda rodada não chama ninguém.
        hamilton.consultados.clear()
        async with sofia.banco() as s:
            assert await contato.conferir(s) == {"conferidas": 0, "terapeutas": 0}
        assert hamilton.consultados == []

    @pytest.mark.asyncio
    async def test_nao_mexe_na_data_de_atividade(self, sofia, hamilton):
        # A lista ordena por `atualizada_em`: se conferir carimbasse "agora", o
        # cron jogaria conversa de meses atrás para o topo, quinze por rodada.
        antiga = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
        ids = [await _nova(sofia, n, atualizada_em=antiga) for n in (TACIA, PACIENTE)]

        async with sofia.banco() as s:
            await contato.conferir(s)

        for cid in ids:
            quando = (await _ler(sofia, cid)).atualizada_em
            assert quando.replace(tzinfo=timezone.utc) == antiga

    @pytest.mark.asyncio
    async def test_nao_desfaz_a_escolha_feita_a_mao(self, sofia, hamilton):
        # O Hamilton não conhece o Renato; alguém o marcou no painel.
        renato = await _nova(sofia, RENATO, eh_terapeuta=True, contato_conferido_por=contato.MANUAL)
        # E reconhece a Tácia, que alguém marcou como paciente.
        tacia = await _nova(sofia, TACIA, eh_terapeuta=False, contato_conferido_por=contato.MANUAL)

        async with sofia.banco() as s:
            assert await contato.conferir(s) == {"conferidas": 0, "terapeutas": 0}

        assert (await _ler(sofia, renato)).eh_terapeuta is True
        assert (await _ler(sofia, tacia)).eh_terapeuta is False

    @pytest.mark.asyncio
    async def test_hamilton_fora_nao_marca_ninguem_como_paciente(self, sofia, hamilton):
        cid = await _nova(sofia, TACIA)
        hamilton.fora_do_ar = True

        async with sofia.banco() as s:
            assert await contato.conferir(s) == {"conferidas": 0, "terapeutas": 0}

        # "Não sei" não vira "é paciente": a conversa volta na próxima rodada.
        assert (await _ler(sofia, cid)).contato_conferido_por is None

    @pytest.mark.asyncio
    async def test_respeita_o_limite_da_rodada(self, sofia, hamilton):
        for i in range(5):
            await _nova(sofia, f"553190000000{i}")

        async with sofia.banco() as s:
            assert (await contato.conferir(s, limite=3))["conferidas"] == 3

    @pytest.mark.asyncio
    async def test_roda_pendurada_no_cron_dos_lembretes(self, sofia, hamilton, meta):
        cid = await _nova(sofia, TACIA)

        resp = await sofia.post("/tasks/lembretes-terapeutas", headers={"X-Tasks-Token": "segredo"})

        assert resp.status_code == 200
        assert resp.json()["contatos"] == {"conferidas": 1, "terapeutas": 1}
        assert (await _ler(sofia, cid)).eh_terapeuta is True


class TestSaiDosAutomaticos:
    @pytest.mark.asyncio
    async def test_marcar_encerra_a_cobranca_em_curso(self, sofia):
        agora = datetime.now(timezone.utc)
        cid = await _nova(
            sofia,
            RENATO,
            paciente_hamilton_id=9,
            cobranca_iniciada_em=agora,
            cobranca_status="enviada",
        )

        async with sofia.banco() as s:
            conversa = await s.get(Conversa, cid)
            await contato.definir(s, conversa, True, contato.MANUAL)

        conversa = await _ler(sofia, cid)
        assert cobranca.em_cobranca(conversa) is False
        assert conversa.cobranca_status == "assumida"

    @pytest.mark.asyncio
    async def test_marcar_encerra_a_pesquisa_sem_emendar_cobranca(self, sofia):
        cid = await _nova(
            sofia,
            RENATO,
            paciente_hamilton_id=9,
            pesquisa_avaliacao_id=77,
            pesquisa_iniciada_em=datetime.now(timezone.utc),
        )
        vistos = {}

        async def _finalizar(db, conversa, avaliacao, recusou=False):
            # Na hora em que a pesquisa fecha (e tenta emendar a cobrança), a
            # marca já tem de estar posta: é ela que barra `cobranca._elegivel`.
            vistos["marca"] = conversa.eh_terapeuta
            await pesquisa.limpar(db, conversa)

        with (
            patch.object(
                pesquisa, "buscar_avaliacao", AsyncMock(return_value={"pk_avaliacao": 77})
            ),
            patch.object(pesquisa, "finalizar", _finalizar),
        ):
            async with sofia.banco() as s:
                await contato.definir(s, await s.get(Conversa, cid), True, contato.MANUAL)

        assert vistos == {"marca": True}
        assert pesquisa.em_pesquisa(await _ler(sofia, cid)) is False

    @pytest.mark.asyncio
    async def test_nao_e_cobrada_nem_recebe_pesquisa_de_entrada(self, sofia):
        config_negocio._cache["cobranca_ativa"] = True
        config_negocio._cache["pesquisa_entrada_ativa"] = True
        cid = await _nova(
            sofia, RENATO, paciente_hamilton_id=9, estado="cadastrado", eh_terapeuta=True
        )

        async with sofia.banco() as s:
            conversa = await s.get(Conversa, cid)
            assert await cobranca._elegivel(s, conversa) is False
            motivo = await pesquisa.motivo_para_pular_entrada(s, conversa)
        assert motivo == "a conversa é de terapeuta, não de paciente"

    @pytest.mark.asyncio
    async def test_nao_recebe_follow_up_de_lead_parado(self, sofia):
        agora = datetime.now(timezone.utc)
        horas = int(config_negocio.valor("followup_horas")) + 1
        ids = {}
        for nome, numero, marca in (("lead", PACIENTE, False), ("terapeuta", RENATO, True)):
            ids[nome] = await _nova(sofia, numero, eh_terapeuta=marca)
            async with sofia.banco() as s:
                s.add(
                    Mensagem(
                        conversa_id=ids[nome],
                        direcao="recebida",
                        origem="paciente",
                        tipo="texto",
                        texto="oi",
                        criada_em=agora - timedelta(hours=horas),
                    )
                )
                await s.commit()

        async with sofia.banco() as s:
            parados = [c.id for c in await seguimento.buscar_leads_parados(s, agora)]

        assert parados == [ids["lead"]]

    @pytest.mark.asyncio
    async def test_nao_entra_na_fila_de_cadastro_a_confirmar(self, sofia):
        pronto = {cadastro_abandonado.CHAVE_PRONTO: {"nome_completo": "Fulano"}}
        lead = await _nova(sofia, PACIENTE, dados_coletados=pronto)
        await _nova(sofia, RENATO, dados_coletados=pronto, eh_terapeuta=True)

        async with sofia.banco() as s:
            fila = [c.id for c in await cadastro_abandonado.aguardando_confirmacao(s)]

        assert fila == [lead]


class TestSaiDosNumerosEDaFila:
    @pytest.mark.asyncio
    async def test_metricas_contam_so_pacientes(self, sofia):
        await _nova(sofia, PACIENTE, paciente_hamilton_id=1)
        await _nova(sofia, RENATO, eh_terapeuta=True)
        hamilton_fora = AsyncMock()
        hamilton_fora.status_primeira_consulta.side_effect = hamilton_client.HamiltonError("fora")

        async with sofia.banco() as s:
            dados = await metricas.calcular_metricas(s, hamilton=hamilton_fora)

        # Com o terapeuta na conta seriam 2 conversas e 50% de conversão.
        assert (dados["total"], dados["taxa_conversao"]) == (1, 100)

    @pytest.mark.asyncio
    async def test_escalada_de_terapeuta_nao_entra_na_fila_hoje(self, sofia):
        ids = {}
        for nome, numero, marca in (("paciente", PACIENTE, False), ("terapeuta", RENATO, True)):
            ids[nome] = await _nova(sofia, numero, modo="humano", eh_terapeuta=marca)
            async with sofia.banco() as s:
                s.add(Escalada(conversa_id=ids[nome], motivo="pedido_humano"))
                await s.commit()

        async with sofia.banco() as s:
            fila = [i["conversa_id"] for i in await hoje.listar_pendencias(s)]

        assert fila == [ids["paciente"]]


async def _login(cliente):
    resp = await cliente.post(
        "/login", data={"usuario": settings.painel_user, "senha": settings.painel_password}
    )
    assert resp.status_code == 303


class TestPainel:
    @pytest.mark.asyncio
    async def test_lista_mostra_a_etiqueta_so_em_quem_e_terapeuta(self, sofia):
        await _nova(sofia, PACIENTE, dados_coletados={"nome_completo": "Paula Paciente"})
        await _nova(
            sofia, RENATO, dados_coletados={"nome_completo": "Renato Terapeuta"}, eh_terapeuta=True
        )
        await _login(sofia)

        html = (await sofia.get("/painel/conversas")).text

        assert html.count('class="tag-terapeuta"') == 1
        linha_dele = html.split("Renato Terapeuta")[1].split("</tr>")[0]
        assert "tag-terapeuta" in linha_dele
        # "Sofia atendendo" seria mentira na linha de quem ela não atende.
        assert "Sofia não atende" in linha_dele

    @pytest.mark.asyncio
    async def test_filtros_separam_pacientes_de_terapeutas(self, sofia):
        await _nova(sofia, PACIENTE, dados_coletados={"nome_completo": "Paula Paciente"})
        await _nova(
            sofia, RENATO, dados_coletados={"nome_completo": "Renato Terapeuta"}, eh_terapeuta=True
        )
        await _login(sofia)

        so_pacientes = (await sofia.get("/painel/conversas?filtro=pacientes")).text
        so_terapeutas = (await sofia.get("/painel/conversas?filtro=terapeutas")).text
        todas = (await sofia.get("/painel/conversas")).text

        assert "Paula Paciente" in so_pacientes and "Renato Terapeuta" not in so_pacientes
        assert "Renato Terapeuta" in so_terapeutas and "Paula Paciente" not in so_terapeutas
        # O padrão não esconde ninguém.
        assert "Paula Paciente" in todas and "Renato Terapeuta" in todas

    @pytest.mark.asyncio
    async def test_botao_marca_a_mao_e_desmarca(self, sofia):
        cid = await _nova(sofia, RENATO)
        await _login(sofia)
        pagina = f"/painel/conversas/{cid}/"

        assert "Marcar como terapeuta" in (await sofia.get(pagina)).text

        resp = await sofia.post(f"/painel/conversas/{cid}/contato", data={"tipo": "terapeuta"})
        assert resp.status_code == 303
        conversa = await _ler(sofia, cid)
        assert (conversa.eh_terapeuta, conversa.contato_conferido_por) == (True, "manual")
        html = (await sofia.get(pagina)).text
        assert "Esta conversa é de terapeuta" in html and "Marcar como paciente" in html

        await sofia.post(f"/painel/conversas/{cid}/contato", data={"tipo": "paciente"})
        conversa = await _ler(sofia, cid)
        assert (conversa.eh_terapeuta, conversa.contato_conferido_por) == (False, "manual")

    @pytest.mark.asyncio
    async def test_tipo_desconhecido_e_recusado(self, sofia):
        cid = await _nova(sofia, RENATO)
        await _login(sofia)

        resp = await sofia.post(f"/painel/conversas/{cid}/contato", data={"tipo": "coordenador"})

        assert resp.status_code == 400
        assert (await _ler(sofia, cid)).contato_conferido_por is None

    @pytest.mark.asyncio
    async def test_marcar_exige_login(self, sofia):
        cid = await _nova(sofia, RENATO)

        resp = await sofia.post(f"/painel/conversas/{cid}/contato", data={"tipo": "terapeuta"})

        assert resp.status_code == 303 and resp.headers["location"].startswith("/login")
        assert (await _ler(sofia, cid)).eh_terapeuta is False
