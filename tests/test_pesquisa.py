"""Testes da pesquisa de satisfação (Demanda C).

A pesquisa é **conduzida** por LLM, mas as notas e os sim/não passaram a ser
gravados por ferramenta (`registrar_resposta_pesquisa`), uma a uma, na hora. A
extração no fim continua existindo para os textos — e como rede para algum
numérico que a ferramenta não tenha registrado.

O que dá pra proteger com teste, e é o que está aqui, são as **defesas em volta**
do modelo: nota fora da escala não entra no banco, `true` não vira a nota 1,
campo inventado é descartado, a extração não sobrescreve o que a ferramenta
gravou, o marcador interno não chega ao paciente, cada `momento` cai no roteiro
certo, e ninguém é abordado duas vezes.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base
from app.models import Conversa, Escalada
from app.services import (
    acompanhamento,
    config_negocio,
    config_prompt,
    hamilton_client,
    pesquisa,
    saida,
)

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
        assert self._extrair('{"individual": 8, "qualidade_geral": 10}') == {
            "individual": 8,
            "qualidade_geral": 10,
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

    def test_campos_do_desenho_antigo_sumiram(self):
        """`dat_ultima_sessao` e afins foram cortados: são deriváveis no Hamilton."""
        bruto = (
            '{"dat_ultima_sessao": "2026-08-03", "atendimento_rapido_bool": true,'
            ' "indicaria_allos_bool": true, "nota_terapeuta": 9, "social": 6}'
        )
        assert self._extrair(bruto) == {"social": 6}

    def test_booleano_precisa_ser_booleano(self):
        """ "sim" é texto; só `true`/`false` viram booleano."""
        resultado = self._extrair('{"consentimento_paciente": true, "continuar_allos": "sim"}')
        assert resultado == {"consentimento_paciente": True}

    def test_motivo_encerramento_so_onde_ha_motivo(self):
        """Não faz sentido registrar 'por que saiu' de quem acabou de começar."""
        bruto = '{"motivo_encerramento": "mudei de cidade"}'
        assert self._extrair(bruto) == {}
        assert self._extrair(bruto, _encerramento()) == {"motivo_encerramento": "mudei de cidade"}

    def test_motivo_encerramento_vale_tambem_na_troca_de_terapeuta(self):
        """É um campo só pros dois casos: o `momento` diz qual foi."""
        reenc = {**PRIMEIRA_SESSAO, "momento": pesquisa.MOMENTO_ACOMPANHAMENTO}
        assert self._extrair('{"motivo_encerramento": "não engatei"}', reenc) == {
            "motivo_encerramento": "não engatei"
        }

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
    async def test_aborda_mesmo_com_a_conversa_escalada(self, session):
        """Modo humano NÃO bloqueia mais a pesquisa (decisão do Paulo, Demanda D).

        Antes a Sofia pulava quem estava com a Thainá. Passou a abordar: se a
        primeira sessão aconteceu, a avaliação e a cobrança TÊM que acontecer, e o
        caso de borda se resolve pela pessoa reagir e a Sofia escalar de novo.
        O portão de `webhook.ingerir_mensagem` abre exceção pro modo pesquisa, então
        a resposta dela não cai no vazio.
        """
        conversa = await _conversa(session, modo="humano")
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
        cliente.obter_avaliacao.return_value = PRIMEIRA_SESSAO
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
        ja_lembrada = {**PRIMEIRA_SESSAO, "sofia_lembrete_em": "2026-08-06T00:00:00+00:00"}
        cliente.avaliacoes_pendentes.return_value = [ja_lembrada]
        cliente.obter_avaliacao.return_value = ja_lembrada
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
        cliente.obter_avaliacao.return_value = PRIMEIRA_SESSAO
        with patch.object(
            hamilton_client, "get_hamilton_client", return_value=cliente
        ), patch.object(pesquisa, "_enviar", AsyncMock(return_value=True)) as mock_enviar:
            resumo = await pesquisa.rodar_pesquisas(session, AGORA)
        assert resumo["encerradas"] == 1
        assert conversa.pesquisa_avaliacao_id is None
        mock_enviar.assert_not_awaited()


class TestFalhaDoHamiltonNaoEncerraPesquisa:
    """Regressão de produção: um 502 do proxy apagou uma pesquisa em andamento.

    `_buscar_avaliacao` devolvia `None` tanto para "o Hamilton respondeu e ela
    não existe" quanto para "não consegui perguntar", e `responder` tratava os
    dois como sumiço — zerava `pesquisa_avaliacao_id` e não falava nada. Foi o
    que aconteceu com a avaliação 392 em 09/08/2026: a pessoa respondeu que
    aceitava a pesquisa e ficou sem resposta, sem erro visível em lugar nenhum.

    A distinção é o comportamento sob teste, não um detalhe de implementação.
    """

    @pytest.mark.asyncio
    async def test_hamilton_fora_do_ar_mantem_a_pesquisa_em_curso(self, session):
        conversa = await _conversa(session, pesquisa_avaliacao_id=10, pesquisa_iniciada_em=AGORA)
        await session.commit()
        cliente = AsyncMock()
        cliente.obter_avaliacao.side_effect = hamilton_client.HamiltonError("502 do proxy")
        with patch.object(
            hamilton_client, "get_hamilton_client", return_value=cliente
        ), patch.object(pesquisa, "_enviar", AsyncMock(return_value=True)) as mock_enviar:
            await pesquisa.responder(session, conversa, conversa.numero_whatsapp)
        # Continua em curso: a pessoa repete, ou o lembrete de 20h pega.
        assert conversa.pesquisa_avaliacao_id == 10
        mock_enviar.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_avaliacao_de_fato_apagada_encerra_a_pesquisa(self, session):
        """O outro lado da moeda: respondeu 404, aí sim não há o que conduzir."""
        conversa = await _conversa(session, pesquisa_avaliacao_id=10, pesquisa_iniciada_em=AGORA)
        await session.commit()
        cliente = AsyncMock()
        cliente.obter_avaliacao.return_value = None
        with patch.object(hamilton_client, "get_hamilton_client", return_value=cliente):
            await pesquisa.responder(session, conversa, conversa.numero_whatsapp)
        assert conversa.pesquisa_avaliacao_id is None

    @pytest.mark.asyncio
    async def test_busca_pelo_id_em_vez_de_varrer_a_fila(self, session):
        """A varredura baixava o acumulado histórico inteiro (~68 KB) por 1 registro.

        Foi esse payload que derrubou o proxy com 502. Buscar pelo pk que já
        sabemos é o que remove a causa, não só o sintoma.
        """
        conversa = await _conversa(session, pesquisa_avaliacao_id=10, pesquisa_iniciada_em=AGORA)
        await session.commit()
        cliente = AsyncMock()
        cliente.obter_avaliacao.return_value = None
        with patch.object(hamilton_client, "get_hamilton_client", return_value=cliente):
            await pesquisa.responder(session, conversa, conversa.numero_whatsapp)
        cliente.obter_avaliacao.assert_awaited_once_with(10)
        cliente.avaliacoes_pendentes.assert_not_awaited()


class TestValidacaoDaTool:
    """O modelo escolhe `campo` e `valor`. Nada do que ele manda entra sem conferência."""

    def test_nota_valida(self):
        assert pesquisa._validar_resposta("individual", 7) == ("individual", 7)

    @pytest.mark.parametrize("nota", [0, 10])
    def test_extremos_da_escala_valem(self, nota):
        assert pesquisa._validar_resposta("geral", nota) == ("geral", nota)

    @pytest.mark.parametrize("nota", [11, -1, 100])
    def test_nota_fora_da_escala_e_recusada(self, nota):
        assert pesquisa._validar_resposta("individual", nota) is None

    def test_string_numerica_vira_nota(self):
        assert pesquisa._validar_resposta("social", "8") == ("social", 8)

    def test_palavra_nao_vira_nota(self):
        assert pesquisa._validar_resposta("social", "muito bom") is None

    def test_booleano_nunca_vira_nota(self):
        """`True` é instância de `int` em Python: sem a checagem, viraria a nota 1."""
        assert pesquisa._validar_resposta("individual", True) is None
        assert pesquisa._validar_resposta("individual", False) is None

    def test_campo_booleano_so_aceita_booleano(self):
        assert pesquisa._validar_resposta("continuar_terapeuta", True) == (
            "continuar_terapeuta",
            True,
        )
        assert pesquisa._validar_resposta("continuar_terapeuta", False) == (
            "continuar_terapeuta",
            False,
        )
        assert pesquisa._validar_resposta("continuar_terapeuta", "sim") is None
        assert pesquisa._validar_resposta("continuar_terapeuta", 1) is None

    def test_campo_fora_da_allowlist_e_recusado(self):
        """Inclui os campos do desenho antigo, que o modelo pode ter aprendido."""
        for campo in ("nota_terapeuta", "observacao", "fk_paciente", "dat_ultima_sessao"):
            assert pesquisa._validar_resposta(campo, 8) is None


def _tool_call(campo, valor):
    return SimpleNamespace(
        id="c1",
        name="registrar_resposta_pesquisa",
        arguments={"campo": campo, "valor": valor},
    )


class TestRegistroPelaTool:
    """A tool grava na hora — é isso que faz resposta parcial sobreviver."""

    @pytest.mark.asyncio
    async def test_grava_no_hamilton_imediatamente(self, session):
        conversa = await _conversa(session, pesquisa_avaliacao_id=10)
        await session.commit()
        cliente = AsyncMock()
        with patch.object(hamilton_client, "get_hamilton_client", return_value=cliente):
            res = await pesquisa._registrar_resposta(session, conversa, _tool_call("individual", 6))
        assert res["status"] == "registrado"
        cliente.atualizar_avaliacao.assert_awaited_once_with(10, {"individual": 6})
        assert pesquisa._ja_gravados(conversa) == {"individual"}

    @pytest.mark.asyncio
    async def test_valor_invalido_nao_chega_ao_hamilton(self, session):
        conversa = await _conversa(session, pesquisa_avaliacao_id=10)
        await session.commit()
        cliente = AsyncMock()
        with patch.object(hamilton_client, "get_hamilton_client", return_value=cliente):
            res = await pesquisa._registrar_resposta(
                session, conversa, _tool_call("individual", 42)
            )
        assert res["status"] == "recusado"
        cliente.atualizar_avaliacao.assert_not_awaited()
        assert pesquisa._ja_gravados(conversa) == set()

    @pytest.mark.asyncio
    async def test_falha_do_hamilton_deixa_o_campo_pra_extracao(self, session):
        """Não marcar como gravado é o que dá a segunda chance no fim."""
        conversa = await _conversa(session, pesquisa_avaliacao_id=10)
        await session.commit()
        cliente = AsyncMock()
        cliente.atualizar_avaliacao.side_effect = hamilton_client.HamiltonError("500")
        with patch.object(hamilton_client, "get_hamilton_client", return_value=cliente):
            res = await pesquisa._registrar_resposta(session, conversa, _tool_call("geral", 5))
        assert res["status"] == "erro"
        assert pesquisa._ja_gravados(conversa) == set()

    @pytest.mark.asyncio
    async def test_sem_pesquisa_em_curso_nao_grava(self, session):
        conversa = await _conversa(session)
        await session.commit()
        cliente = AsyncMock()
        with patch.object(hamilton_client, "get_hamilton_client", return_value=cliente):
            res = await pesquisa._registrar_resposta(session, conversa, _tool_call("geral", 5))
        assert res["status"] == "recusado"
        cliente.atualizar_avaliacao.assert_not_awaited()


class TestPrecedenciaDaTool:
    """A tool leu a resposta no turno; a extração relê tudo de fora e pode errar."""

    @pytest.mark.asyncio
    async def test_extracao_nao_sobrescreve_o_que_a_tool_gravou(self, session):
        conversa = await _conversa(
            session,
            pesquisa_avaliacao_id=10,
            dados_coletados={pesquisa._CHAVE_GRAVADOS: {"individual": 8, "social": 7}},
        )
        await session.commit()
        cliente = AsyncMock()
        extraido = {"individual": 2, "social": 3, "geral": 7, "feedback_livre": "gostei"}
        with patch.object(
            hamilton_client, "get_hamilton_client", return_value=cliente
        ), patch.object(pesquisa, "extrair_respostas", AsyncMock(return_value=extraido)):
            await pesquisa.finalizar(session, conversa, PRIMEIRA_SESSAO)
        _, payload = cliente.atualizar_avaliacao.await_args.args
        assert "individual" not in payload and "social" not in payload
        assert payload["geral"] == 7
        assert payload["feedback_livre"] == "gostei"

    @pytest.mark.asyncio
    async def test_resposta_parcial_e_avaliado_nao_nao_respondeu(self, session):
        """Quem respondeu 2 de 8 e sumiu não pode sumir do radar."""
        conversa = await _conversa(
            session,
            pesquisa_avaliacao_id=10,
            dados_coletados={pesquisa._CHAVE_GRAVADOS: {"individual": 8}},
        )
        await session.commit()
        cliente = AsyncMock()
        with patch.object(
            hamilton_client, "get_hamilton_client", return_value=cliente
        ), patch.object(pesquisa, "extrair_respostas", AsyncMock(return_value={})):
            await pesquisa.finalizar(session, conversa, PRIMEIRA_SESSAO, recusou=True)
        _, payload = cliente.atualizar_avaliacao.await_args.args
        assert payload["status"] == "avaliado"

    @pytest.mark.asyncio
    async def test_recusa_sem_nenhuma_resposta_e_nao_respondeu(self, session):
        conversa = await _conversa(session, pesquisa_avaliacao_id=10)
        await session.commit()
        cliente = AsyncMock()
        with patch.object(hamilton_client, "get_hamilton_client", return_value=cliente):
            await pesquisa.finalizar(session, conversa, PRIMEIRA_SESSAO, recusou=True)
        _, payload = cliente.atualizar_avaliacao.await_args.args
        assert payload["status"] == "nao_respondeu"

    @pytest.mark.asyncio
    async def test_sair_da_pesquisa_zera_o_rastro(self, session):
        """Senão a pesquisa seguinte acharia que já gravaram o que ninguém gravou."""
        conversa = await _conversa(
            session,
            pesquisa_avaliacao_id=10,
            dados_coletados={"nome_completo": "Maria", pesquisa._CHAVE_GRAVADOS: {"geral": 7}},
        )
        await session.commit()
        await pesquisa._limpar(session, conversa)
        assert pesquisa._ja_gravados(conversa) == set()
        assert conversa.dados_coletados["nome_completo"] == "Maria"


class TestFormatoAntigoDoRastro:
    """Uma pesquisa em curso no momento do deploy não pode explodir."""

    def test_lista_antiga_ainda_e_lida_como_conjunto_de_campos(self):
        conversa = Conversa(
            numero_whatsapp="5531999998888",
            dados_coletados={pesquisa._CHAVE_GRAVADOS: ["individual", "geral"]},
        )
        assert pesquisa._ja_gravados(conversa) == {"individual", "geral"}
        # Sem valor, a precedência continua valendo; só o alerta fica cego.
        assert pesquisa._valores_gravados(conversa) == {"individual": None, "geral": None}


class TestRoteiroPorMomento:
    """O `momento` é o seletor: cair no roteiro errado é fazer as perguntas erradas."""

    @pytest.mark.parametrize(
        "momento,chave",
        [
            (pesquisa.MOMENTO_LINHA_DE_BASE, "prompt_pesquisa_entrada"),
            (pesquisa.MOMENTO_PRIMEIRA_SESSAO, "prompt_pesquisa_primeira_sessao"),
            (pesquisa.MOMENTO_ACOMPANHAMENTO, "prompt_pesquisa_reencaminhamento"),
        ],
    )
    def test_cada_momento_usa_o_seu_roteiro(self, momento, chave):
        prompt = pesquisa.montar_prompt({**PRIMEIRA_SESSAO, "momento": momento})
        assert config_prompt.padrao(chave)[:80] in prompt

    def test_encerramento_usa_o_roteiro_de_encerramento(self):
        prompt = pesquisa.montar_prompt(_encerramento())
        assert config_prompt.padrao("prompt_pesquisa_encerramento")[:80] in prompt

    def test_momento_desconhecido_cai_no_mais_curto(self):
        """Pior caso: perguntar de menos. Nunca fazer as perguntas de saída."""
        prompt = pesquisa.montar_prompt({**PRIMEIRA_SESSAO, "momento": "coisa nova"})
        assert config_prompt.padrao("prompt_pesquisa_primeira_sessao")[:80] in prompt

    def test_entrada_nao_pede_nota_de_terapeuta(self):
        """Na linha de base ainda não houve sessão — não há terapeuta a avaliar."""
        roteiro = config_prompt.padrao("prompt_pesquisa_entrada")
        assert "qualidade_geral" not in roteiro
        assert "nota_sofia" in roteiro

    def test_reencaminhamento_nao_pede_nps(self):
        """NPS de quem não está saindo é ruído."""
        assert "nota_indicacao" not in config_prompt.padrao("prompt_pesquisa_reencaminhamento")

    def test_primeira_sessao_nao_pede_mais_o_ors(self):
        """O ORS agora é colhido antes da 1a sessao; repetir aqui não mede nada."""
        roteiro = config_prompt.padrao("prompt_pesquisa_primeira_sessao")
        assert "`individual`" not in roteiro
        assert "`continuar_terapeuta`" in roteiro


class TestQuemResponde:
    """Se quem responde não é a pessoa atendida, o ORS não se aplica."""

    def _prompt(self, quem):
        conversa = Conversa(numero_whatsapp="5531999998888", dados_coletados={"quem_fala": quem})
        return pesquisa.montar_prompt(PRIMEIRA_SESSAO, conversa)

    def test_acompanhante_manda_pular_o_bloco(self):
        assert "PULE inteiro o bloco" in self._prompt("acompanhante")

    def test_paciente_segue_o_roteiro_inteiro(self):
        prompt = self._prompt("paciente")
        assert "própria pessoa atendida" in prompt
        assert "PULE inteiro o bloco" not in prompt

    def test_sem_informacao_manda_confirmar(self):
        assert "confirme isso em UMA" in pesquisa.montar_prompt(PRIMEIRA_SESSAO, None)

    def test_valor_invalido_e_tratado_como_desconhecido(self):
        assert "confirme isso em UMA" in self._prompt("sei la")


ENTRADA_CRIADA = {
    "pk_avaliacao": 77,
    "fk_paciente": 500,
    "momento": pesquisa.MOMENTO_LINHA_DE_BASE,
    "status": "pendente",
    "sofia_enviada_em": None,
}


def _cliente_de_entrada(**overrides):
    """Hamilton mockado pro caminho da linha de base."""
    cliente = AsyncMock()
    cliente.avaliacoes_pendentes.return_value = []
    cliente.criar_avaliacao_entrada.return_value = dict(ENTRADA_CRIADA)
    cliente.status_primeira_consulta.return_value = {}
    for chave, valor in overrides.items():
        setattr(cliente, chave, valor)
    return cliente


class TestPesquisaDeEntrada:
    """A linha de base é a única que a Sofia cria. As guardas são o que a protegem.

    O caminho principal é a EMENDA (`iniciar_entrada`, chamada pelo webhook logo
    depois do cadastro). O cron é a rede, e as duas passam pelas mesmas guardas
    de propósito — duas listas divergiriam na primeira mudança.
    """

    async def _cadastrada(self, session, horas_atras=4, dados=None, **kwargs):
        campos = {
            "paciente_hamilton_id": 500,
            "estado": "cadastrado",
            "modo": "bot",
            "cadastrado_em": AGORA - timedelta(hours=horas_atras),
            "dados_coletados": {"cadastro_novo": True, **(dados or {})},
        }
        campos.update(kwargs)
        return await _conversa(session, **campos)

    async def _rodar(self, session, cliente=None):
        cliente = cliente or _cliente_de_entrada()
        with patch.object(
            hamilton_client, "get_hamilton_client", return_value=cliente
        ), patch.object(pesquisa, "_turno", AsyncMock(return_value="oi")), patch.object(
            pesquisa, "_enviar", AsyncMock(return_value=True)
        ):
            resumo = await pesquisa.rodar_pesquisas(session, AGORA)
        return resumo, cliente

    # --- a emenda no cadastro (caminho principal) -------------------------- #

    @pytest.mark.asyncio
    async def test_emenda_no_cadastro_aborda_na_hora(self, session):
        """Sem esperar cron, tick duplo nem a trava do Hamilton."""
        conversa = await self._cadastrada(session, horas_atras=0)
        await session.commit()
        cliente = _cliente_de_entrada()
        with patch.object(
            hamilton_client, "get_hamilton_client", return_value=cliente
        ), patch.object(pesquisa, "_turno", AsyncMock(return_value="oi")), patch.object(
            pesquisa, "_enviar", AsyncMock(return_value=True)
        ):
            assert await pesquisa.iniciar_entrada(session, conversa, AGORA) is True
        cliente.criar_avaliacao_entrada.assert_awaited_once_with(500)
        assert conversa.pesquisa_avaliacao_id == 77

    @pytest.mark.asyncio
    async def test_nao_aborda_de_novo_quem_ja_recebeu_o_convite(self, session):
        """O POST é idempotente: `sofia_enviada_em` é o que diz que já abordamos."""
        conversa = await self._cadastrada(session, horas_atras=0)
        await session.commit()
        cliente = _cliente_de_entrada()
        cliente.criar_avaliacao_entrada.return_value = {
            **ENTRADA_CRIADA,
            "sofia_enviada_em": "2026-08-06T10:00:00Z",
        }
        with patch.object(hamilton_client, "get_hamilton_client", return_value=cliente):
            assert await pesquisa.iniciar_entrada(session, conversa, AGORA) is False
        assert conversa.pesquisa_avaliacao_id is None

    @pytest.mark.asyncio
    async def test_neuro_nao_tem_ors_por_escalada(self, session):
        """Neuro vai pra reunião com a Amanda; não há 'antes e depois' a medir."""
        conversa = await self._cadastrada(session)
        session.add(Escalada(conversa_id=conversa.id, motivo="neuro_reuniao", resolvida_em=AGORA))
        await session.commit()
        assert await pesquisa.motivo_para_pular_entrada(session, conversa) is not None

    @pytest.mark.asyncio
    async def test_neuro_nao_tem_ors_pelo_motivo_da_busca(self, session):
        conversa = await self._cadastrada(
            session, dados={"motivo_busca": "quero uma avaliação neuropsicológica pro meu filho"}
        )
        await session.commit()
        assert await pesquisa.motivo_para_pular_entrada(session, conversa) is not None

    @pytest.mark.asyncio
    async def test_terapia_passa(self, session):
        conversa = await self._cadastrada(session, dados={"motivo_busca": "ansiedade no trabalho"})
        await session.commit()
        assert await pesquisa.motivo_para_pular_entrada(session, conversa) is None

    @pytest.mark.asyncio
    async def test_acompanhante_nao_recebe(self, session):
        """Sem as 4 notas não sobra pesquisa — e ele não pode responder por ela."""
        conversa = await self._cadastrada(session, dados={"quem_fala": "acompanhante"})
        await session.commit()
        assert await pesquisa.motivo_para_pular_entrada(session, conversa) is not None

    @pytest.mark.asyncio
    async def test_reencontro_nao_recebe(self, session):
        """A ficha já existia no Hamilton: essa pessoa não está começando agora."""
        conversa = await self._cadastrada(session, dados={"cadastro_novo": False})
        await session.commit()
        assert await pesquisa.motivo_para_pular_entrada(session, conversa) is not None

    @pytest.mark.asyncio
    async def test_conversa_sem_a_marca_de_cadastro_novo_nao_recebe(self, session):
        """Cadastrada antes desta chave existir: não vira disparo retroativo."""
        conversa = await self._cadastrada(session)
        conversa.dados_coletados = {}
        await session.commit()
        assert await pesquisa.motivo_para_pular_entrada(session, conversa) is not None

    @pytest.mark.asyncio
    async def test_desligada_no_painel_nao_aborda(self, session):
        conversa = await self._cadastrada(session)
        await session.commit()
        with patch.object(config_negocio, "valor", return_value=False):
            assert await pesquisa.motivo_para_pular_entrada(session, conversa) is not None

    # --- a rede do cron ---------------------------------------------------- #

    @pytest.mark.asyncio
    async def test_rede_aborda_depois_das_3h(self, session):
        await self._cadastrada(session)
        await session.commit()
        resumo, cliente = await self._rodar(session)
        assert resumo["entradas_criadas"] == 1
        cliente.criar_avaliacao_entrada.assert_awaited_once_with(500)

    @pytest.mark.asyncio
    async def test_rede_espera_as_3h(self, session):
        """A rede não atropela a emenda que acabou de rodar."""
        await self._cadastrada(session, horas_atras=1)
        await session.commit()
        resumo, cliente = await self._rodar(session)
        assert resumo["entradas_criadas"] == 0
        cliente.criar_avaliacao_entrada.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_desiste_depois_de_5_dias(self, session):
        """Baseline velho demais deixa de ser baseline."""
        await self._cadastrada(session, horas_atras=24 * (pesquisa.DIAS_LIMITE_ENTRADA + 1))
        await session.commit()
        resumo, _ = await self._rodar(session)
        assert resumo["entradas_criadas"] == 0

    @pytest.mark.asyncio
    async def test_conversa_antiga_sem_carimbo_nunca_entra(self, session):
        """É o que impede a estreia disto de virar disparo em massa pra base."""
        await self._cadastrada(session, cadastrado_em=None)
        await session.commit()
        resumo, _ = await self._rodar(session)
        assert resumo["entradas_criadas"] == 0

    @pytest.mark.asyncio
    async def test_modo_humano_espera(self, session):
        await self._cadastrada(session, modo="humano")
        await session.commit()
        resumo, _ = await self._rodar(session)
        assert resumo["entradas_criadas"] == 0

    @pytest.mark.asyncio
    async def test_cadastro_pendente_nao_tem_paciente_de_verdade(self, session):
        await self._cadastrada(session, estado="cadastro_pendente")
        await session.commit()
        resumo, _ = await self._rodar(session)
        assert resumo["entradas_criadas"] == 0

    @pytest.mark.asyncio
    async def test_escalada_aberta_segura_a_pesquisa(self, session):
        conversa = await self._cadastrada(session)
        session.add(Escalada(conversa_id=conversa.id, motivo="preco"))
        await session.commit()
        resumo, _ = await self._rodar(session)
        assert resumo["entradas_criadas"] == 0

    @pytest.mark.asyncio
    async def test_escalada_resolvida_libera(self, session):
        conversa = await self._cadastrada(session)
        session.add(Escalada(conversa_id=conversa.id, motivo="preco", resolvida_em=AGORA))
        await session.commit()
        resumo, _ = await self._rodar(session)
        assert resumo["entradas_criadas"] == 1

    @pytest.mark.asyncio
    async def test_pesquisa_em_curso_nao_leva_outra(self, session):
        await self._cadastrada(session, pesquisa_avaliacao_id=10)
        await session.commit()
        resumo, _ = await self._rodar(session)
        assert resumo["entradas_criadas"] == 0

    @pytest.mark.asyncio
    async def test_quem_ja_teve_a_primeira_consulta_nao_recebe(self, session):
        """Perguntar 'como você está antes de começar?' a quem já foi atendido."""
        await self._cadastrada(session)
        await session.commit()
        cliente = _cliente_de_entrada()
        cliente.status_primeira_consulta.return_value = {500: {"primeira_consulta_realizada": True}}
        resumo, cliente = await self._rodar(session, cliente)
        assert resumo["entradas_criadas"] == 0
        cliente.criar_avaliacao_entrada.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_hamilton_fora_no_check_da_consulta_nao_trava_a_rede(self, session):
        await self._cadastrada(session)
        await session.commit()
        cliente = _cliente_de_entrada()
        cliente.status_primeira_consulta.side_effect = hamilton_client.HamiltonError("502")
        resumo, _ = await self._rodar(session, cliente)
        assert resumo["entradas_criadas"] == 1

    @pytest.mark.asyncio
    async def test_falha_do_hamilton_nao_derruba_a_rodada(self, session):
        await self._cadastrada(session)
        await session.commit()
        cliente = _cliente_de_entrada()
        cliente.criar_avaliacao_entrada.side_effect = hamilton_client.HamiltonError("502")
        resumo, _ = await self._rodar(session, cliente)
        assert resumo["entradas_criadas"] == 0


class TestLinhaDeBaseObsoleta:
    """A corrida que deixou a avaliação 393 pendente pra sempre no teste de 09/08."""

    def _entrada(self, pk=77, paciente=500):
        return {
            "pk_avaliacao": pk,
            "fk_paciente": paciente,
            "momento": pesquisa.MOMENTO_LINHA_DE_BASE,
            "status": "pendente",
        }

    @pytest.mark.asyncio
    async def test_descarta_entrada_de_quem_ja_tem_pesquisa_de_1a_sessao(self, session):
        conversa = await _conversa(session)
        await session.commit()
        cliente = AsyncMock()
        cliente.avaliacoes_pendentes.return_value = [self._entrada(), PRIMEIRA_SESSAO]
        with patch.object(
            hamilton_client, "get_hamilton_client", return_value=cliente
        ), patch.object(pesquisa, "iniciar", AsyncMock(return_value=True)) as mock_iniciar:
            resumo = await pesquisa.rodar_pesquisas(session, AGORA)
        # A de 1ª sessão foi abordada; a de entrada foi marcada e saiu da fila.
        assert resumo["enviadas"] == 1
        assert mock_iniciar.await_args.args[2]["pk_avaliacao"] == PRIMEIRA_SESSAO["pk_avaliacao"]
        cliente.atualizar_avaliacao.assert_awaited_once_with(77, {"status": "nao_respondeu"})
        assert conversa.id  # a conversa segue existindo, só a avaliação foi descartada

    @pytest.mark.asyncio
    async def test_entrada_sozinha_na_fila_continua_valendo(self, session):
        await _conversa(session)
        await session.commit()
        cliente = AsyncMock()
        cliente.avaliacoes_pendentes.return_value = [self._entrada()]
        with patch.object(
            hamilton_client, "get_hamilton_client", return_value=cliente
        ), patch.object(pesquisa, "iniciar", AsyncMock(return_value=True)) as mock_iniciar:
            await pesquisa.rodar_pesquisas(session, AGORA)
        mock_iniciar.assert_awaited_once()
        cliente.atualizar_avaliacao.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_entrada_de_outro_paciente_nao_e_afetada(self, session):
        await _conversa(session)
        await session.commit()
        cliente = AsyncMock()
        cliente.avaliacoes_pendentes.return_value = [
            self._entrada(pk=88, paciente=999),
            PRIMEIRA_SESSAO,
        ]
        with patch.object(
            hamilton_client, "get_hamilton_client", return_value=cliente
        ), patch.object(pesquisa, "iniciar", AsyncMock(return_value=True)):
            await pesquisa.rodar_pesquisas(session, AGORA)
        cliente.atualizar_avaliacao.assert_not_awaited()


class TestMotivosDeAlerta:
    """Sem time de qualidade, é o alerta que transforma a pesquisa de custo em produto."""

    def test_pesquisa_boa_nao_alerta(self):
        payload = {"qualidade_geral": 9, "nota_indicacao": 10, "individual": 3, "geral": 2}
        assert pesquisa.motivos_de_alerta(payload) == []

    def test_ors_baixo_nunca_alerta(self):
        """Decisão explícita: a Sofia não se intromete com nota de bem-estar.

        Crise se detecta pela descrição clara do paciente, com o modelo de crise
        que já existe — nunca por nota de escala.
        """
        payload = {"individual": 0, "interpessoal": 0, "social": 0, "geral": 0}
        assert pesquisa.motivos_de_alerta(payload) == []

    @pytest.mark.parametrize(
        "campo,rotulo",
        [
            ("qualidade_geral", "nota do terapeuta"),
            ("nota_sofia", "nota do acolhimento"),
            ("nota_indicacao", "nota de indicação"),
        ],
    )
    def test_nota_abaixo_do_limiar_alerta(self, campo, rotulo):
        motivos = pesquisa.motivos_de_alerta({campo: 5})
        assert motivos == [f"{rotulo} 5"]

    @pytest.mark.parametrize("campo", ["qualidade_geral", "nota_sofia", "nota_indicacao"])
    def test_nota_no_limiar_nao_alerta(self, campo):
        """O limiar é 'menor que': 6 é aceitável, 5 não."""
        assert pesquisa.motivos_de_alerta({campo: 6}) == []

    def test_encaixe_ruim_alerta_sempre(self):
        """Pegar match ruim na sessão 1 vale mais que qualquer nota."""
        assert pesquisa.motivos_de_alerta({"continuar_terapeuta": False}) == [
            "não sentiu encaixe com o terapeuta"
        ]

    def test_encaixe_bom_nao_alerta(self):
        assert pesquisa.motivos_de_alerta({"continuar_terapeuta": True}) == []

    def test_encaixe_nao_perguntado_nao_alerta(self):
        """NULL é 'não perguntado' — foi pra isso que o campo virou nullable."""
        assert pesquisa.motivos_de_alerta({"continuar_terapeuta": None}) == []

    def test_quer_continuar_alerta_porque_exige_acao(self):
        """Boa notícia, mas alguém tem que fazer o novo match."""
        assert pesquisa.motivos_de_alerta({"continuar_allos": True}) == [
            "QUER CONTINUAR na Allos com outro terapeuta"
        ]

    def test_nao_quer_continuar_nao_alerta(self):
        assert pesquisa.motivos_de_alerta({"continuar_allos": False}) == []

    def test_reclamacao_alerta(self):
        assert pesquisa.motivos_de_alerta({}, reclamacao=True) == [
            "RELATOU EXPERIÊNCIA RUIM / reclamação"
        ]

    def test_varios_gatilhos_viram_um_alerta_so(self):
        """Uma pesquisa ruim dispara vários; três templates seguidos seriam spam."""
        motivos = pesquisa.motivos_de_alerta(
            {"qualidade_geral": 3, "nota_indicacao": 2, "continuar_terapeuta": False},
            reclamacao=True,
        )
        assert len(motivos) == 4

    def test_limiar_zero_desliga(self):
        """Escape hatch do painel se o volume incomodar."""
        with patch.object(
            config_negocio, "valor", side_effect=lambda c: 0 if c.startswith("alerta_") else 6
        ):
            assert pesquisa.motivos_de_alerta({"qualidade_geral": 1}) == []

    def test_limiar_editado_no_painel_vale(self):
        with patch.object(
            config_negocio, "valor", side_effect=lambda c: 9 if c.startswith("alerta_") else 6
        ):
            assert pesquisa.motivos_de_alerta({"qualidade_geral": 8}) == ["nota do terapeuta 8"]

    def test_booleano_nao_e_lido_como_nota(self):
        """`True` é instância de `int`; sem a guarda, viraria 'nota 1' e alertaria."""
        assert pesquisa.motivos_de_alerta({"qualidade_geral": True}) == []


class TestAlertaNoFimDaPesquisa:
    @pytest.mark.asyncio
    async def test_alerta_marca_a_conversa_e_manda_template(self, session):
        conversa = await _conversa(
            session,
            pesquisa_avaliacao_id=10,
            dados_coletados={pesquisa._CHAVE_GRAVADOS: {"qualidade_geral": 2}},
        )
        await session.commit()
        cliente = AsyncMock()
        with patch.object(
            hamilton_client, "get_hamilton_client", return_value=cliente
        ), patch.object(
            pesquisa, "extrair_respostas", AsyncMock(return_value={"qualidade_geral": 2})
        ), patch.object(
            pesquisa.escalation, "alertar_pesquisa", AsyncMock(return_value=True)
        ) as alerta:
            await pesquisa.finalizar(session, conversa, PRIMEIRA_SESSAO)
        alerta.assert_awaited_once()
        assert conversa.alerta_pesquisa_em is not None
        assert "nota do terapeuta 2" in conversa.alerta_pesquisa_motivos

    @pytest.mark.asyncio
    async def test_pesquisa_boa_nao_entra_na_fila(self, session):
        conversa = await _conversa(session, pesquisa_avaliacao_id=10)
        await session.commit()
        cliente = AsyncMock()
        with patch.object(
            hamilton_client, "get_hamilton_client", return_value=cliente
        ), patch.object(
            pesquisa, "extrair_respostas", AsyncMock(return_value={"qualidade_geral": 10})
        ), patch.object(
            pesquisa.escalation, "alertar_pesquisa", AsyncMock(return_value=True)
        ) as alerta:
            await pesquisa.finalizar(session, conversa, PRIMEIRA_SESSAO)
        alerta.assert_not_awaited()
        assert conversa.alerta_pesquisa_em is None

    @pytest.mark.asyncio
    async def test_reclamacao_nao_vai_pro_hamilton(self, session):
        """`alerta_reclamacao` não é campo da Avaliacao: viraria 400 (ou lixo)."""
        conversa = await _conversa(session, pesquisa_avaliacao_id=10)
        await session.commit()
        cliente = AsyncMock()
        extraido = {"motivo_encerramento": "ninguém me respondeu", "alerta_reclamacao": True}
        with patch.object(
            hamilton_client, "get_hamilton_client", return_value=cliente
        ), patch.object(
            pesquisa, "extrair_respostas", AsyncMock(return_value=dict(extraido))
        ), patch.object(
            pesquisa.escalation, "alertar_pesquisa", AsyncMock(return_value=True)
        ) as alerta:
            await pesquisa.finalizar(session, conversa, _encerramento())
        _, payload = cliente.atualizar_avaliacao.await_args.args
        assert "alerta_reclamacao" not in payload
        assert payload["motivo_encerramento"] == "ninguém me respondeu"
        alerta.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_hamilton_fora_do_ar_ainda_alerta(self, session):
        """A Thainá precisa saber da nota 2 mesmo se o PATCH falhou."""
        conversa = await _conversa(session, pesquisa_avaliacao_id=10)
        await session.commit()
        cliente = AsyncMock()
        cliente.atualizar_avaliacao.side_effect = hamilton_client.HamiltonError("502")
        with patch.object(
            hamilton_client, "get_hamilton_client", return_value=cliente
        ), patch.object(
            pesquisa, "extrair_respostas", AsyncMock(return_value={"qualidade_geral": 1})
        ), patch.object(
            pesquisa.escalation, "alertar_pesquisa", AsyncMock(return_value=True)
        ) as alerta:
            await pesquisa.finalizar(session, conversa, PRIMEIRA_SESSAO)
        alerta.assert_awaited_once()
        assert conversa.alerta_pesquisa_em is not None

    @pytest.mark.asyncio
    async def test_alerta_nao_muda_o_modo_da_conversa(self, session):
        """A pessoa terminou de responder; não está esperando ninguém."""
        conversa = await _conversa(session, pesquisa_avaliacao_id=10, modo="bot")
        await session.commit()
        cliente = AsyncMock()
        with patch.object(
            hamilton_client, "get_hamilton_client", return_value=cliente
        ), patch.object(
            pesquisa, "extrair_respostas", AsyncMock(return_value={"qualidade_geral": 1})
        ), patch.object(
            pesquisa.escalation, "alertar_pesquisa", AsyncMock(return_value=True)
        ):
            await pesquisa.finalizar(session, conversa, PRIMEIRA_SESSAO)
        assert conversa.modo == "bot"

    @pytest.mark.asyncio
    async def test_alerta_novo_reabre_a_fila(self, session):
        """A pessoa recebe até 4 pesquisas; o alerta desta não pode nascer tratado."""
        conversa = await _conversa(
            session, pesquisa_avaliacao_id=10, alerta_resolvido_em=AGORA - timedelta(days=30)
        )
        await session.commit()
        cliente = AsyncMock()
        with patch.object(
            hamilton_client, "get_hamilton_client", return_value=cliente
        ), patch.object(
            pesquisa, "extrair_respostas", AsyncMock(return_value={"qualidade_geral": 1})
        ), patch.object(
            pesquisa.escalation, "alertar_pesquisa", AsyncMock(return_value=True)
        ):
            await pesquisa.finalizar(session, conversa, PRIMEIRA_SESSAO)
        assert conversa.alerta_resolvido_em is None


class TestFilaDeAlertasNoPainel:
    """O template do WhatsApp some na rolagem; a fila é o que garante que alguém veja."""

    @pytest.mark.asyncio
    async def test_lista_so_o_que_nao_foi_tratado(self, session):
        await _conversa(
            session,
            numero_whatsapp="5531900000001",
            alerta_pesquisa_em=AGORA,
            alerta_pesquisa_motivos="nota do terapeuta 2",
            dados_coletados={"nome_completo": "Maria"},
        )
        await _conversa(
            session,
            numero_whatsapp="5531900000002",
            alerta_pesquisa_em=AGORA,
            alerta_resolvido_em=AGORA,
        )
        await _conversa(session, numero_whatsapp="5531900000003")
        await session.commit()
        fila = await acompanhamento.listar_alertas_pesquisa(session)
        assert [f["nome"] for f in fila] == ["Maria"]
        assert fila[0]["motivos"] == "nota do terapeuta 2"

    @pytest.mark.asyncio
    async def test_marcar_tratado_tira_da_fila_sem_apagar(self, session):
        conversa = await _conversa(
            session, alerta_pesquisa_em=AGORA, alerta_pesquisa_motivos="reclamação"
        )
        await session.commit()
        await acompanhamento.marcar_alerta_resolvido(session, conversa)
        assert await acompanhamento.listar_alertas_pesquisa(session) == []
        assert conversa.alerta_pesquisa_em is not None
        assert conversa.alerta_pesquisa_motivos == "reclamação"

    @pytest.mark.asyncio
    async def test_reabrir_devolve_pra_fila(self, session):
        conversa = await _conversa(session, alerta_pesquisa_em=AGORA, alerta_resolvido_em=AGORA)
        await session.commit()
        await acompanhamento.reabrir_alerta(session, conversa)
        assert len(await acompanhamento.listar_alertas_pesquisa(session)) == 1

    @pytest.mark.asyncio
    async def test_sem_nome_cai_no_numero(self, session):
        await _conversa(
            session, alerta_pesquisa_em=AGORA, alerta_pesquisa_motivos="x", dados_coletados={}
        )
        await session.commit()
        fila = await acompanhamento.listar_alertas_pesquisa(session)
        assert fila[0]["nome"] == "5531999998888"
