"""Testes da pesquisa de satisfação (Demanda C).

A pesquisa é conduzida e extraída **por LLM, sem tool de registro** — decisão
consciente, registrada em `demandas.md`. Isso significa que nada garante que o
modelo faça todas as perguntas nem que leia cada resposta corretamente.

O que dá pra proteger com teste, e é o que está aqui, são as **defesas em volta**
do modelo: nota fora da escala não entra no banco, campo inventado é descartado,
o marcador interno não chega ao paciente, o texto da pesquisa de encerramento
muda conforme o tipo de saída, e ninguém é abordado duas vezes.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base
from app.models import Conversa
from app.services import hamilton_client, pesquisa, saida

AGORA = datetime(2026, 8, 6, 12, 0, 0, tzinfo=timezone.utc)

PRIMEIRA_SESSAO = {
    "pk_avaliacao": 10,
    "fk_paciente": 500,
    "paciente_nome": "Maria Silva",
    "paciente_telefone": "31999998888",
    "terapeuta_nome": "Ana",
    "momento": pesquisa.MOMENTO_PRIMEIRA_SESSAO,
    "tipo_saida": None,
    "cancelador": None,
    "sofia_enviada_em": None,
    "sofia_lembrete_em": None,
}


def _encerramento(tipo_saida="desistencia", cancelador="paciente", pk=11):
    return {
        **PRIMEIRA_SESSAO,
        "pk_avaliacao": pk,
        "momento": pesquisa.MOMENTO_ENCERRAMENTO,
        "tipo_saida": tipo_saida,
        "cancelador": cancelador,
    }


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


async def _conversa(session, **kwargs):
    dados = {"numero_whatsapp": "5531999998888", "paciente_hamilton_id": 500}
    dados.update(kwargs)
    conversa = Conversa(**dados)
    session.add(conversa)
    await session.flush()
    return conversa


class TestExtracao:
    """A extração é a última barreira antes de dado ruim virar KPI de Qualidade."""

    def _extrair(self, bruto, avaliacao=None):
        return pesquisa._normalizar_extracao(bruto, avaliacao or PRIMEIRA_SESSAO)

    def test_json_puro(self):
        assert self._extrair('{"individual": 8, "nota_terapeuta": 10}') == {
            "individual": 8,
            "nota_terapeuta": 10,
        }

    def test_json_em_bloco_de_codigo(self):
        """O modelo costuma embrulhar em ```json apesar da instrução."""
        assert self._extrair('```json\n{"individual": 7}\n```') == {"individual": 7}

    def test_json_com_texto_em_volta(self):
        assert self._extrair('Claro! Aqui está:\n{"social": 5}\nEspero ter ajudado.') == {
            "social": 5
        }

    @pytest.mark.parametrize("nota", [11, -1, 100])
    def test_nota_fora_da_escala_e_descartada(self, nota):
        """0-10 é a escala. Fora disso é dado errado, não dado parcial."""
        assert self._extrair(f'{{"individual": {nota}, "social": 7}}') == {"social": 7}

    def test_nota_em_palavras_nao_vira_numero(self):
        """ "muito bom" não é 8. Chutar aqui falsearia a média do time de Qualidade."""
        assert self._extrair('{"individual": "muito bom", "social": 7}') == {"social": 7}

    def test_nota_como_string_numerica_e_aceita(self):
        assert self._extrair('{"individual": "8"}') == {"individual": 8}

    def test_campo_inventado_e_ignorado(self):
        """Allowlist: campo fora do model não pode virar 400 no Hamilton."""
        assert self._extrair('{"individual": 8, "campo_inexistente": "x"}') == {"individual": 8}

    def test_null_e_omitido(self):
        """Campo ausente significa 'não perguntado', que é melhor que zero."""
        assert self._extrair('{"individual": null, "social": 6}') == {"social": 6}

    def test_texto_vazio_e_omitido(self):
        assert self._extrair('{"feedback_livre": "   ", "social": 6}') == {"social": 6}

    def test_data_iso_e_aceita(self):
        assert self._extrair('{"dat_ultima_sessao": "2026-08-03"}') == {
            "dat_ultima_sessao": "2026-08-03"
        }

    def test_data_ilegivel_e_omitida(self):
        """ "semana passada" não dá pra converter sem inventar."""
        assert self._extrair('{"dat_ultima_sessao": "semana passada"}') == {}

    def test_booleano_precisa_ser_booleano(self):
        """ "sim" é texto; só `true`/`false` viram booleano."""
        resultado = self._extrair('{"consentimento_paciente": true, "indicaria_allos_bool": "sim"}')
        assert resultado == {"consentimento_paciente": True}

    def test_motivo_interrupcao_so_no_encerramento(self):
        """Não faz sentido registrar 'por que saiu' de quem acabou de começar."""
        bruto = '{"motivo_interrupcao": "mudei de cidade"}'
        assert self._extrair(bruto) == {}
        assert self._extrair(bruto, _encerramento()) == {"motivo_interrupcao": "mudei de cidade"}

    @pytest.mark.parametrize("bruto", ["", None, "não consegui extrair", "{quebrado", "[1,2]"])
    def test_resposta_impossivel_de_ler_estoura(self, bruto):
        """Melhor falhar e logar do que gravar lixo silenciosamente."""
        with pytest.raises(pesquisa.PesquisaError):
            self._extrair(bruto)


class TestMarcadores:
    """Os marcadores são sinal interno e nunca podem chegar ao paciente."""

    def test_reconhece_conclusao(self):
        assert pesquisa._marcador_de_fim("Obrigada! [[PESQUISA_CONCLUIDA]]") == "concluiu"

    def test_reconhece_recusa(self):
        assert pesquisa._marcador_de_fim("Tudo bem. [[PESQUISA_RECUSADA]]") == "recusou"

    def test_texto_normal_nao_encerra(self):
        assert pesquisa._marcador_de_fim("E de 0 a 10, como foi?") is None

    def test_remove_o_marcador_da_fala(self):
        assert pesquisa._sem_marcador("Obrigada! [[PESQUISA_CONCLUIDA]]") == "Obrigada!"

    def test_sanitizacao_pega_marcador_no_meio_da_frase(self):
        """Rede de segurança: se o modelo puser o marcador fora do fim, o choke
        point de saída (o mesmo do P0) remove antes de ir pro WhatsApp."""
        assert "[[" not in saida.limpar("A nota foi 8 [[PESQUISA_CONCLUIDA]] e obrigada.")

    def test_sanitizacao_nao_corta_fala_legitima_com_colchetes(self):
        texto = "Ele disse [isso aqui] e eu concordei."
        assert saida.limpar(texto) == texto


class TestContextoEncerramento:
    """O texto muda conforme o tipo de saída: perguntar errado aqui machuca."""

    def test_reencaminhamento_nao_e_saida(self):
        """A pessoa continua na Allos, só troca de terapeuta."""
        ctx = pesquisa._contexto_encerramento(_encerramento("Solicitação de reencaminhamento"))
        assert "REENCAMINHAMENTO" in ctx
        assert "NÃO está saindo" in ctx
        assert "terapeuta anterior" in ctx

    def test_alta_nao_pergunta_por_que_interrompeu(self):
        """Alta é conclusão, não abandono."""
        ctx = pesquisa._contexto_encerramento(_encerramento("alta"))
        assert "ALTA" in ctx
        assert "NÃO pergunte por que ela decidiu" in ctx

    def test_paciente_que_sumiu_e_abordado_sem_cobranca(self):
        ctx = pesquisa._contexto_encerramento(_encerramento("não responde"))
        assert "sem cobrança" in ctx

    def test_desistencia_pergunta_o_motivo(self):
        ctx = pesquisa._contexto_encerramento(_encerramento("desistencia"))
        assert "motivo" in ctx.lower()

    def test_desligado_pelo_terapeuta_nunca_culpa_o_paciente(self):
        """Perguntar 'por que VOCÊ decidiu interromper' a quem foi desligado é
        factualmente errado e soa como cobrança."""
        ctx = pesquisa._contexto_encerramento(_encerramento("desistencia", cancelador="terapeuta"))
        assert "quem encerrou foi o terapeuta" in ctx.lower()


class TestMontarPrompt:
    def test_usa_o_roteiro_da_primeira_sessao(self):
        prompt = pesquisa.montar_prompt(PRIMEIRA_SESSAO)
        assert "Maria" in prompt  # primeiro nome, não o nome completo
        assert "Ana" in prompt
        assert "primeira sessão" in prompt.lower()

    def test_usa_o_roteiro_de_encerramento(self):
        prompt = pesquisa.montar_prompt(_encerramento("alta"))
        assert "ALTA" in prompt

    def test_aguenta_avaliacao_sem_nome(self):
        """Dado faltando no Hamilton não pode derrubar a pesquisa."""
        prompt = pesquisa.montar_prompt({"momento": pesquisa.MOMENTO_PRIMEIRA_SESSAO})
        assert "desconhecido" in prompt


class TestEmPesquisa:
    @pytest.mark.asyncio
    async def test_conversa_sem_pesquisa(self, session):
        assert pesquisa.em_pesquisa(await _conversa(session)) is False

    @pytest.mark.asyncio
    async def test_conversa_com_pesquisa(self, session):
        conversa = await _conversa(session, pesquisa_avaliacao_id=10)
        assert pesquisa.em_pesquisa(conversa) is True


class TestConversaDoPaciente:
    """Achar com quem falar. Errar aqui manda pesquisa pro paciente errado."""

    @pytest.mark.asyncio
    async def test_acha_pelo_vinculo_de_cadastro(self, session):
        conversa = await _conversa(session)
        achada = await pesquisa._conversa_do_paciente(session, PRIMEIRA_SESSAO)
        assert achada.id == conversa.id

    @pytest.mark.asyncio
    async def test_acha_por_telefone_ignorando_o_ddi(self, session):
        """Quem falou com a Sofia mas foi cadastrado à mão não tem o vínculo.
        O Hamilton guarda 31..., o WhatsApp manda 5531...."""
        conversa = await _conversa(session, paciente_hamilton_id=None)
        achada = await pesquisa._conversa_do_paciente(session, PRIMEIRA_SESSAO)
        assert achada.id == conversa.id

    @pytest.mark.asyncio
    async def test_paciente_desconhecido_nao_e_abordado(self, session):
        """Sem conversa aberta e fora da janela de 24h da Meta, não há como falar."""
        await _conversa(session, numero_whatsapp="5531900000000", paciente_hamilton_id=None)
        avaliacao = {**PRIMEIRA_SESSAO, "fk_paciente": 999, "paciente_telefone": "31911112222"}
        assert await pesquisa._conversa_do_paciente(session, avaliacao) is None

    @pytest.mark.asyncio
    async def test_sem_telefone_nao_tenta_adivinhar(self, session):
        await _conversa(session, paciente_hamilton_id=None)
        avaliacao = {**PRIMEIRA_SESSAO, "fk_paciente": None, "paciente_telefone": ""}
        assert await pesquisa._conversa_do_paciente(session, avaliacao) is None


class TestIniciar:
    @pytest.mark.asyncio
    async def test_envia_convite_e_marca_dos_dois_lados(self, session):
        conversa = await _conversa(session)
        cliente = AsyncMock()
        with patch.object(
            pesquisa, "_turno", AsyncMock(return_value="Oi, posso te fazer umas perguntas?")
        ), patch.object(pesquisa, "_enviar", AsyncMock(return_value=True)), patch.object(
            hamilton_client, "get_hamilton_client", return_value=cliente
        ):
            assert await pesquisa.iniciar(session, conversa, PRIMEIRA_SESSAO, AGORA) is True

        assert conversa.pesquisa_avaliacao_id == 10
        assert conversa.pesquisa_iniciada_em == AGORA
        # `status='pendente'` é "sem resposta", não "sem envio": sem marcar o
        # envio no Hamilton, a pessoa seria abordada de novo a cada rodada.
        cliente.atualizar_avaliacao.assert_awaited_once()
        assert "sofia_enviada_em" in cliente.atualizar_avaliacao.await_args.args[1]

    @pytest.mark.asyncio
    async def test_falha_de_envio_nao_marca_a_conversa(self, session):
        """Se o convite não saiu, a avaliação continua pendente pra próxima rodada."""
        conversa = await _conversa(session)
        with patch.object(pesquisa, "_turno", AsyncMock(return_value="oi")), patch.object(
            pesquisa, "_enviar", AsyncMock(return_value=False)
        ):
            assert await pesquisa.iniciar(session, conversa, PRIMEIRA_SESSAO, AGORA) is False
        assert conversa.pesquisa_avaliacao_id is None

    @pytest.mark.asyncio
    async def test_hamilton_fora_do_ar_nao_desfaz_o_envio(self, session):
        """A mensagem já foi pro paciente; perder a marcação é ruim, mas fingir
        que não enviamos seria pior (mandaria de novo)."""
        conversa = await _conversa(session)
        cliente = AsyncMock()
        cliente.atualizar_avaliacao.side_effect = hamilton_client.HamiltonError("caiu")
        with patch.object(pesquisa, "_turno", AsyncMock(return_value="oi")), patch.object(
            pesquisa, "_enviar", AsyncMock(return_value=True)
        ), patch.object(hamilton_client, "get_hamilton_client", return_value=cliente):
            assert await pesquisa.iniciar(session, conversa, PRIMEIRA_SESSAO, AGORA) is True
        assert conversa.pesquisa_avaliacao_id == 10


class TestFinalizar:
    @pytest.mark.asyncio
    async def test_recusa_vira_nao_respondeu_sem_extrair(self, session):
        """Quem recusou não tem resposta pra extrair — nem se gasta LLM nisso."""
        conversa = await _conversa(session, pesquisa_avaliacao_id=10)
        cliente = AsyncMock()
        with patch.object(hamilton_client, "get_hamilton_client", return_value=cliente):
            await pesquisa.finalizar(session, conversa, PRIMEIRA_SESSAO, recusou=True)
        assert cliente.atualizar_avaliacao.await_args.args[1]["status"] == "nao_respondeu"
        assert conversa.pesquisa_avaliacao_id is None  # sai do modo pesquisa

    @pytest.mark.asyncio
    async def test_conclusao_extrai_e_grava(self, session):
        conversa = await _conversa(session, pesquisa_avaliacao_id=10)
        cliente = AsyncMock()
        with patch.object(
            hamilton_client, "get_hamilton_client", return_value=cliente
        ), patch.object(pesquisa, "extrair_respostas", AsyncMock(return_value={"individual": 8})):
            await pesquisa.finalizar(session, conversa, PRIMEIRA_SESSAO)
        payload = cliente.atualizar_avaliacao.await_args.args[1]
        assert payload["status"] == "avaliado"
        assert payload["individual"] == 8

    @pytest.mark.asyncio
    async def test_extracao_falha_mas_o_status_e_gravado(self, session):
        """A pessoa respondeu de verdade; perder o status faria a pesquisa ser
        reenviada pra quem já respondeu. A conversa fica no painel de qualquer forma."""
        conversa = await _conversa(session, pesquisa_avaliacao_id=10)
        cliente = AsyncMock()
        with patch.object(
            hamilton_client, "get_hamilton_client", return_value=cliente
        ), patch.object(
            pesquisa,
            "extrair_respostas",
            AsyncMock(side_effect=pesquisa.PesquisaError("json ruim")),
        ):
            await pesquisa.finalizar(session, conversa, PRIMEIRA_SESSAO)
        assert cliente.atualizar_avaliacao.await_args.args[1]["status"] == "avaliado"
        assert conversa.pesquisa_avaliacao_id is None

    @pytest.mark.asyncio
    async def test_hamilton_fora_do_ar_ainda_libera_a_conversa(self, session):
        """Senão o paciente ficaria preso em modo pesquisa pra sempre."""
        conversa = await _conversa(session, pesquisa_avaliacao_id=10)
        cliente = AsyncMock()
        cliente.atualizar_avaliacao.side_effect = hamilton_client.HamiltonError("caiu")
        with patch.object(hamilton_client, "get_hamilton_client", return_value=cliente):
            await pesquisa.finalizar(session, conversa, PRIMEIRA_SESSAO, recusou=True)
        assert conversa.pesquisa_avaliacao_id is None


class TestRodarPesquisas:
    """A rodada do cron: aborda, lembra e encerra."""

    @pytest.mark.asyncio
    async def test_aborda_quem_tem_avaliacao_pendente(self, session):
        conversa = await _conversa(session)
        await session.commit()
        cliente = AsyncMock()
        cliente.avaliacoes_pendentes.return_value = [PRIMEIRA_SESSAO]
        with patch.object(
            hamilton_client, "get_hamilton_client", return_value=cliente
        ), patch.object(pesquisa, "iniciar", AsyncMock(return_value=True)) as mock_iniciar:
            resumo = await pesquisa.rodar_pesquisas(session, AGORA)
        assert resumo["enviadas"] == 1
        assert mock_iniciar.await_args.args[1].id == conversa.id

    @pytest.mark.asyncio
    async def test_nao_aborda_quem_esta_com_a_thaina(self, session):
        """Modo humano: a Sofia não fala por cima de quem assumiu a conversa."""
        await _conversa(session, modo="humano")
        await session.commit()
        cliente = AsyncMock()
        cliente.avaliacoes_pendentes.return_value = [PRIMEIRA_SESSAO]
        with patch.object(
            hamilton_client, "get_hamilton_client", return_value=cliente
        ), patch.object(pesquisa, "iniciar", AsyncMock(return_value=True)) as mock_iniciar:
            resumo = await pesquisa.rodar_pesquisas(session, AGORA)
        assert resumo["enviadas"] == 0
        mock_iniciar.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_nao_aborda_quem_ja_esta_em_pesquisa(self, session):
        await _conversa(session, pesquisa_avaliacao_id=10, pesquisa_iniciada_em=AGORA)
        await session.commit()
        cliente = AsyncMock()
        cliente.avaliacoes_pendentes.return_value = [PRIMEIRA_SESSAO]
        with patch.object(
            hamilton_client, "get_hamilton_client", return_value=cliente
        ), patch.object(pesquisa, "iniciar", AsyncMock(return_value=True)) as mock_iniciar:
            await pesquisa.rodar_pesquisas(session, AGORA)
        mock_iniciar.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_hamilton_fora_do_ar_nao_derruba_a_rodada(self, session):
        cliente = AsyncMock()
        cliente.avaliacoes_pendentes.side_effect = hamilton_client.HamiltonError("caiu")
        with patch.object(hamilton_client, "get_hamilton_client", return_value=cliente):
            assert (await pesquisa.rodar_pesquisas(session, AGORA))["enviadas"] == 0

    @pytest.mark.asyncio
    async def test_lembra_depois_de_20h_de_silencio(self, session):
        conversa = await _conversa(
            session,
            pesquisa_avaliacao_id=10,
            pesquisa_iniciada_em=AGORA - timedelta(hours=pesquisa.HORAS_LEMBRETE),
        )
        await session.commit()
        cliente = AsyncMock()
        cliente.avaliacoes_pendentes.return_value = [PRIMEIRA_SESSAO]
        with patch.object(
            hamilton_client, "get_hamilton_client", return_value=cliente
        ), patch.object(pesquisa, "_enviar", AsyncMock(return_value=True)) as mock_enviar:
            resumo = await pesquisa.rodar_pesquisas(session, AGORA)
        assert resumo["lembretes"] == 1
        assert mock_enviar.await_args.args[2] == pesquisa.LEMBRETE_TEXTO
        # Lembrar não encerra: a pessoa ainda pode responder.
        assert conversa.pesquisa_avaliacao_id == 10

    @pytest.mark.asyncio
    async def test_nao_lembra_duas_vezes(self, session):
        """Um lembrete é um empurrãozinho; dois viram pressão."""
        await _conversa(
            session,
            pesquisa_avaliacao_id=10,
            pesquisa_iniciada_em=AGORA - timedelta(hours=pesquisa.HORAS_LEMBRETE),
        )
        await session.commit()
        cliente = AsyncMock()
        cliente.avaliacoes_pendentes.return_value = [
            {**PRIMEIRA_SESSAO, "sofia_lembrete_em": "2026-08-06T00:00:00+00:00"}
        ]
        with patch.object(
            hamilton_client, "get_hamilton_client", return_value=cliente
        ), patch.object(pesquisa, "_enviar", AsyncMock(return_value=True)) as mock_enviar:
            resumo = await pesquisa.rodar_pesquisas(session, AGORA)
        assert resumo["lembretes"] == 0
        mock_enviar.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_nao_lembra_antes_da_hora(self, session):
        await _conversa(
            session,
            pesquisa_avaliacao_id=10,
            pesquisa_iniciada_em=AGORA - timedelta(hours=pesquisa.HORAS_LEMBRETE - 1),
        )
        await session.commit()
        cliente = AsyncMock()
        cliente.avaliacoes_pendentes.return_value = [PRIMEIRA_SESSAO]
        with patch.object(
            hamilton_client, "get_hamilton_client", return_value=cliente
        ), patch.object(pesquisa, "_enviar", AsyncMock(return_value=True)) as mock_enviar:
            assert (await pesquisa.rodar_pesquisas(session, AGORA))["lembretes"] == 0
        mock_enviar.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_encerra_depois_de_44h(self, session):
        """Sem mandar mensagem nenhuma: passada a janela da Meta, é só marcação."""
        conversa = await _conversa(
            session,
            pesquisa_avaliacao_id=10,
            pesquisa_iniciada_em=AGORA - timedelta(hours=pesquisa.HORAS_ENCERRAMENTO),
        )
        await session.commit()
        cliente = AsyncMock()
        cliente.avaliacoes_pendentes.return_value = [PRIMEIRA_SESSAO]
        with patch.object(
            hamilton_client, "get_hamilton_client", return_value=cliente
        ), patch.object(pesquisa, "_enviar", AsyncMock(return_value=True)) as mock_enviar:
            resumo = await pesquisa.rodar_pesquisas(session, AGORA)
        assert resumo["encerradas"] == 1
        assert conversa.pesquisa_avaliacao_id is None
        mock_enviar.assert_not_awaited()
