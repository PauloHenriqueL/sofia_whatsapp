"""Conversas simuladas em vários turnos (pytest-conversational).

Cada cenário fala com a Sofia pelo webhook de verdade, mensagem por mensagem, e
confere o que sairia no WhatsApp. O modelo e o revisor são roteirizados: aqui o
que se testa é o CAMINHO da fala (portão de terapeuta, modo teste, revisão antes
do envio), não a qualidade do modelo. Para conversar com o modelo de verdade,
use a skill `testar-conversa`.

Os cenários vêm de casos reais de produção. Quando aparecer um caso novo, ele
vira cenário aqui.
"""

import hashlib
import hmac
import json
from unittest.mock import patch

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport
from pytest_conversational import Conversation, expect
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app import config
from app.database import Base, get_db
from app.main import app
from app.models import Conversa
from app.services import (
    config_negocio,
    hamilton_client,
    lembretes_terapeutas,
    llm_client,
    revisao,
    serializacao,
)

AMANDA = "553196887526"  # terapeuta, sem o nono dígito, como o WhatsApp entrega
JOANA = "5531911112222"  # interessada em terapia
EQUIPE = "5531900000000"  # celular de teste da equipe


class HamiltonFalso:
    def __call__(self, request: httpx.Request) -> httpx.Response:
        caminho = request.url.path
        if caminho == "/authentication/token/":
            return httpx.Response(200, json={"access": "jwt"})
        if caminho == "/api/v1/terapeutas/por-telefone/":
            if request.url.params.get("telefone") == AMANDA:
                return httpx.Response(
                    200,
                    json={"terapeuta": True, "terapeuta_id": 9, "nome": "Amanda Borges de Souza"},
                )
            return httpx.Response(404, json={"terapeuta": False})
        return httpx.Response(200, json={"ativo": False, "envios": []})


class ModeloRoteirizado:
    """Devolve as falas na ordem do roteiro; a última se repete se acabar."""

    def __init__(self, *falas: str):
        self.falas = list(falas)
        self.chamadas = 0

    async def gerar_resposta(self, historico, tools=None, **kwargs):
        fala = self.falas[min(self.chamadas, len(self.falas) - 1)]
        self.chamadas += 1
        return llm_client.LLMResposta(texto=fala)


class RevisorDeTeste:
    """Faz o papel do revisor com regras simples e visíveis no teste."""

    def __init__(self, fora_do_ar: bool = False):
        self.fora_do_ar = fora_do_ar
        self.pedidos: list[str] = []

    async def gerar_resposta(self, historico, **kwargs):
        if self.fora_do_ar:
            raise llm_client.LLMError("revisor fora")
        pedido = historico[0]["content"]
        self.pedidos.append(pedido)
        fala = pedido.split("FALA A CONFERIR:\n", 1)[1]
        contexto = pedido.split("FALA A CONFERIR:\n", 1)[0]
        if "registrad" in fala.lower() and "AÇÕES DESTE TURNO:\nnenhuma" in contexto:
            reescrita = "Obrigada por me contar. Não consigo registrar isso por aqui."
            resposta = {
                "aprovada": False,
                "problemas": ["ACAO_INEXISTENTE"],
                "reescrita": reescrita,
            }
        elif f"Sofia: {fala}" in contexto:
            reescrita = "Combinado! Qualquer coisa, é só me chamar."
            resposta = {"aprovada": False, "problemas": ["REPETICAO"], "reescrita": reescrita}
        else:
            resposta = {"aprovada": True}
        return llm_client.LLMResposta(texto=json.dumps(resposta, ensure_ascii=False))


@pytest.fixture(autouse=True)
def _ambiente(monkeypatch):
    orig = dict(config_negocio._cache)
    config_negocio._cache.update(
        debounce_segundos=0.05,
        simular_digitacao=False,
        revisar_respostas=True,
        numeros_modo_teste=EQUIPE,
    )
    cliente = hamilton_client.HamiltonClient(
        base_url="http://hamilton",
        username="sofia-bot",
        password="x",
        client=httpx.AsyncClient(transport=httpx.MockTransport(HamiltonFalso())),
    )
    monkeypatch.setattr(hamilton_client, "get_hamilton_client", lambda: cliente)
    monkeypatch.setattr(config.settings, "whatsapp_dry_run", False)
    monkeypatch.setattr(config.settings, "whatsapp_app_secret", "segredo-do-app")
    lembretes_terapeutas._cache.clear()
    lembretes_terapeutas._respondidos.clear()
    revisao.zerar()
    yield
    config_negocio._cache.clear()
    config_negocio._cache.update(orig)


@pytest_asyncio.fixture
async def sofia(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    enviados: list[dict] = []

    async def _enviar(payload, descricao, sigiloso=False):
        enviados.append(payload)
        return {"messages": [{"id": f"wamid.saida.{len(enviados)}"}]}

    async def _db():
        async with maker() as s:
            yield s

    monkeypatch.setattr("app.services.whatsapp_client._enviar", _enviar)
    app.dependency_overrides[get_db] = _db
    with patch("app.routers.webhook.async_session", maker):
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://sofia"
        ) as c:
            c.banco = maker
            c.enviados = enviados
            yield c
    app.dependency_overrides.pop(get_db, None)
    serializacao.limpar()
    await engine.dispose()


def no_whatsapp(cliente, numero: str):
    """Adaptador do pytest-conversational: a fala da pessoa entra pelo webhook e a
    resposta é o que a Sofia mandaria para a Meta naquele turno (bolhas juntas)."""

    async def bot(texto: str, convo: Conversation) -> str:
        antes = len(cliente.enviados)
        convo.state["n"] = convo.state.get("n", 0) + 1
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
                                            "id": f"wamid.{numero}.{convo.state['n']}",
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
        novos = cliente.enviados[antes:]
        return "\n\n".join(p["text"]["body"] for p in novos if p.get("type") == "text")

    return bot


async def _conversa(cliente, numero, **campos):
    async with cliente.banco() as s:
        s.add(Conversa(numero_whatsapp=numero, **campos))
        await s.commit()


class TestTerapeutaNaoViraLead:
    """Conversa 106 (23/11/2026): a Amanda respondeu ao resumo da semana e, com o
    modo teste ligado para todos, a Sofia a atendeu como lead por 22 falas."""

    @pytest.mark.asyncio
    async def test_resumo_da_semana_respondido_pela_terapeuta(self, sofia):
        await _conversa(
            sofia,
            AMANDA,
            eh_terapeuta=True,
            contato_conferido_por="hamilton",
            dados_coletados={"nome_terapeuta": "Amanda Borges de Souza"},
        )
        modelo = ModeloRoteirizado("Você quer que eu te ajude a corrigir o resumo?")
        convo = Conversation(bot=no_whatsapp(sofia, AMANDA))

        with patch("app.routers.webhook.llm_client.get_llm_client", return_value=modelo):
            await convo.say_async("Não tem paciente sem registro de pagamento")
            await convo.say_async("De onde vem essa informação?")

        assert modelo.chamadas == 0, convo.transcript()
        expect.contains(convo.turns[0].bot, "só envia lembretes")
        # Resposta fixa no máximo uma vez por hora: a segunda fica sem resposta.
        assert convo.turns[1].bot == "", convo.transcript()
        for turno in convo.turns:
            expect.not_contains(turno.bot, "registrad")

    @pytest.mark.asyncio
    async def test_celular_da_equipe_no_modo_teste_e_atendido_como_paciente(self, sofia):
        modelo = ModeloRoteirizado("Oi! Sou a Sofia, da Allos. Como posso te ajudar?")
        convo = Conversation(bot=no_whatsapp(sofia, EQUIPE))

        with patch(
            "app.routers.webhook.llm_client.get_llm_client", return_value=modelo
        ), patch.object(revisao, "get_revisor", return_value=RevisorDeTeste()):
            await convo.say_async("Busco terapia")

        expect.contains(convo.last.bot, "Sou a Sofia")


class TestRevisaoAntesDeEnviar:
    @pytest.mark.asyncio
    async def test_nao_afirma_registro_nem_erra_o_nome(self, sofia):
        await _conversa(sofia, JOANA, dados_coletados={"nome_completo": "Joana Lima"})
        modelo = ModeloRoteirizado("Obrigada por confirmar, Maria! Ficou registrado assim.")
        revisor = RevisorDeTeste()
        convo = Conversation(bot=no_whatsapp(sofia, JOANA))

        with patch(
            "app.routers.webhook.llm_client.get_llm_client", return_value=modelo
        ), patch.object(revisao, "get_revisor", return_value=revisor):
            await convo.say_async("A consulta da minha filha Maria já foi paga, tá?")

        expect.not_contains(convo.last.bot, "Maria")
        expect.not_contains(convo.last.bot, "registrado assim")
        expect.contains(convo.last.bot, "Não consigo registrar")
        # O nome já chega limpo ao revisor: a correção por código vem antes.
        assert "Obrigada por confirmar! Ficou registrado assim." in revisor.pedidos[0]

    @pytest.mark.asyncio
    async def test_nao_repete_a_mesma_fala(self, sofia):
        fala = "Perfeito, Joana. Se quiser seguir, me conta qual horário fica melhor pra você."
        await _conversa(sofia, JOANA, dados_coletados={"nome_completo": "Joana Lima"})
        modelo = ModeloRoteirizado(fala, fala)
        convo = Conversation(bot=no_whatsapp(sofia, JOANA))

        with patch(
            "app.routers.webhook.llm_client.get_llm_client", return_value=modelo
        ), patch.object(revisao, "get_revisor", return_value=RevisorDeTeste()):
            await convo.say_async("Quero começar terapia")
            await convo.say_async("Ok, vou ver aqui")

        assert convo.turns[0].bot == fala
        assert convo.turns[1].bot != fala, convo.transcript()
        expect.contains(convo.turns[1].bot, "é só me chamar")

    @pytest.mark.asyncio
    async def test_revisor_fora_do_ar_nao_cala_a_sofia(self, sofia):
        modelo = ModeloRoteirizado("Oi! Que bom que você escreveu. Me conta o que te trouxe aqui?")
        convo = Conversation(bot=no_whatsapp(sofia, JOANA))

        with patch(
            "app.routers.webhook.llm_client.get_llm_client", return_value=modelo
        ), patch.object(revisao, "get_revisor", return_value=RevisorDeTeste(fora_do_ar=True)):
            await convo.say_async("Oi")

        expect.contains(convo.last.bot, "Me conta o que te trouxe aqui?")

    @pytest.mark.asyncio
    async def test_escalada_feita_pelo_codigo_chega_ao_revisor_como_acao(self, sofia):
        """Sem isto o revisor tomaria "passei pra Thainá" como ação inexistente."""
        await _conversa(
            sofia, JOANA, paciente_hamilton_id=5, dados_coletados={"nome_completo": "Joana Lima"}
        )
        # Promete a Thainá sem chamar a tool: quem escala é a rede do código.
        modelo = ModeloRoteirizado("Vou chamar a Thainá pra continuar daqui contigo.")
        revisor = RevisorDeTeste()
        convo = Conversation(bot=no_whatsapp(sofia, JOANA))

        with patch(
            "app.routers.webhook.llm_client.get_llm_client", return_value=modelo
        ), patch.object(revisao, "get_revisor", return_value=revisor):
            await convo.say_async("Quero falar com alguém da equipe sobre o pagamento")

        assert "- escalar_para_thaina: escalado" in revisor.pedidos[0], revisor.pedidos[0]
        expect.contains(convo.last.bot, "Thainá")
