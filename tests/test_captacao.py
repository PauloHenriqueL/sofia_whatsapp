"""Testes da origem do paciente (captação) — Demanda A.

O ponto sensível aqui: o `captacao_id` é escolhido por um LLM. Uma captação
errada contamina o relatório de parceria e a prestação de contas enviada à
prefeitura, e ainda decide se o paciente é cobrado ou não. Então a regra que
estes testes protegem é: **o que não está na lista do Hamilton não passa**.
"""

from unittest.mock import AsyncMock, patch

import pytest

from app.services import captacao, hamilton_client

INSTAGRAM = {"pk_captacao": 7, "nome": "Instagram", "is_active": True, "is_parceria": False}
PREFEITURA = {
    "pk_captacao": 46,
    "nome": "Prefeitura de Materlândia",
    "is_active": True,
    "is_parceria": True,
}
NAO_SEI = {"pk_captacao": 4, "nome": "Não sei", "is_active": True, "is_parceria": False}
LISTA = [INSTAGRAM, PREFEITURA, NAO_SEI]


@pytest.fixture(autouse=True)
def _limpar_cache():
    """O cache é global do processo: sem isso um teste contamina o outro."""
    captacao.limpar()
    yield
    captacao.limpar()


class TestResolver:
    """`resolver` é a allowlist: só devolve captação que existe na lista."""

    def test_encontra_pelo_id(self):
        assert captacao.resolver(7, LISTA) == INSTAGRAM

    def test_aceita_id_como_texto(self):
        """O modelo às vezes manda "7" em vez de 7."""
        assert captacao.resolver("7", LISTA) == INSTAGRAM

    @pytest.mark.parametrize("valor", [999, "abacaxi", None, "", 0, [], {"id": 7}])
    def test_recusa_o_que_nao_esta_na_lista(self, valor):
        """ID inventado, lixo ou vazio viram 'não identificado', não cadastro torto."""
        assert captacao.resolver(valor, LISTA) is None

    def test_lista_vazia_nao_resolve_nada(self):
        """Hamilton fora do ar: melhor sem origem do que com origem chutada."""
        assert captacao.resolver(7, []) is None

    def test_aceita_chave_id_alternativa(self):
        """Se a API mudar `pk_captacao` para `id`, o casamento continua valendo."""
        assert captacao.resolver(3, [{"id": 3, "nome": "Google"}])["nome"] == "Google"


class TestParceria:
    """Parceria decide se o paciente paga. Não pode ser deduzida do nome."""

    def test_marca_parceria_pela_flag(self):
        assert captacao.e_parceria(PREFEITURA) is True

    def test_captacao_comum_nao_e_parceria(self):
        assert captacao.e_parceria(INSTAGRAM) is False

    def test_nenhuma_captacao_nao_e_parceria(self):
        assert captacao.e_parceria(None) is False

    def test_nome_com_prefeitura_sem_a_flag_nao_conta(self):
        """Antes o sistema casava o nome por substring, e isso quebrava em silêncio
        quando alguém renomeava a captação. Agora só a flag vale."""
        falsa = {"pk_captacao": 99, "nome": "Prefeitura de Lugar Nenhum", "is_parceria": False}
        assert captacao.e_parceria(falsa) is False

    def test_lista_de_parcerias(self):
        assert captacao.parcerias(LISTA) == [PREFEITURA]


class TestLinhasParaPrompt:
    def test_marca_as_parcerias(self):
        """O modelo precisa distinguir convênio de prefeitura qualquer."""
        texto = captacao.linhas_para_prompt(LISTA)
        assert "7: Instagram" in texto
        assert "46: Prefeitura de Materlândia [PARCERIA/CONVÊNIO]" in texto
        assert "[PARCERIA/CONVÊNIO]" not in texto.splitlines()[0]  # Instagram não é parceria

    def test_lista_vazia_manda_o_modelo_omitir(self):
        """Sem lista, a instrução tem que ser 'não preencha', nunca 'invente'."""
        assert "omita" in captacao.linhas_para_prompt([]).lower()


class TestListar:
    @pytest.mark.asyncio
    async def test_busca_no_hamilton_e_cacheia(self):
        cliente = AsyncMock()
        cliente.listar_captacoes.return_value = LISTA
        with patch.object(hamilton_client, "get_hamilton_client", return_value=cliente):
            assert await captacao.listar() == LISTA
            await captacao.listar()  # segunda chamada não vai à rede
        cliente.listar_captacoes.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_forcar_ignora_o_cache(self):
        cliente = AsyncMock()
        cliente.listar_captacoes.return_value = LISTA
        with patch.object(hamilton_client, "get_hamilton_client", return_value=cliente):
            await captacao.listar()
            await captacao.listar(forcar=True)
        assert cliente.listar_captacoes.await_count == 2

    @pytest.mark.asyncio
    async def test_hamilton_fora_do_ar_nao_estoura(self):
        """Sem a lista a Sofia ainda cadastra (só sem origem). Degradar > travar."""
        cliente = AsyncMock()
        cliente.listar_captacoes.side_effect = hamilton_client.HamiltonError("caiu")
        with patch.object(hamilton_client, "get_hamilton_client", return_value=cliente):
            assert await captacao.listar() == []

    @pytest.mark.asyncio
    async def test_hamilton_cai_depois_de_carregar_mantem_o_cache(self):
        """Perder a conexão não pode apagar o que já sabíamos."""
        cliente = AsyncMock()
        cliente.listar_captacoes.return_value = LISTA
        with patch.object(hamilton_client, "get_hamilton_client", return_value=cliente):
            await captacao.listar()
            cliente.listar_captacoes.side_effect = hamilton_client.HamiltonError("caiu")
            assert await captacao.listar(forcar=True) == LISTA

    @pytest.mark.asyncio
    async def test_filtra_captacao_inativa(self):
        """Captação desativada não pode ser oferecida ao modelo."""
        cliente = AsyncMock()
        cliente.listar_captacoes.return_value = [
            INSTAGRAM,
            {"pk_captacao": 8, "nome": "Panfleto", "is_active": False},
        ]
        with patch.object(hamilton_client, "get_hamilton_client", return_value=cliente):
            assert await captacao.listar() == [INSTAGRAM]


class TestNaoSei:
    """ "Não sei" é uma RESPOSTA, não a ausência de uma.

    Antes, origem não identificada saía em branco — e em branco não dá pra
    distinguir "a pessoa não soube dizer" de "o modelo esqueceu de perguntar".
    A prestação de contas da ONG trata os dois igual.
    """

    def test_acha_pelo_nome(self):
        assert captacao.nao_sei(LISTA) == NAO_SEI

    def test_ignora_caixa_e_espaco(self):
        lista = [{"pk_captacao": 9, "nome": "  NÃO SEI  ", "is_active": True}]
        assert captacao.nao_sei(lista)["pk_captacao"] == 9

    def test_lista_sem_a_opcao_devolve_none(self):
        assert captacao.nao_sei([INSTAGRAM]) is None

    def test_resolvida_por_nome_e_nao_por_id_cravado(self):
        """O ID de "Não sei" difere entre produção e a branch de teste; cravar 4
        gravaria a captação errada em um dos dois ambientes, em silêncio."""
        lista = [{"pk_captacao": 99, "nome": "Não sei", "is_active": True}]
        assert captacao.nao_sei(lista)["pk_captacao"] == 99


class TestValidarCaptacaoNoWebhook:
    """O fallback de verdade: o que sai de `_validar_captacao` pro cadastro."""

    @pytest.mark.asyncio
    async def test_id_ausente_vira_nao_sei(self):
        from app.routers import webhook

        with patch.object(captacao, "listar", AsyncMock(return_value=LISTA)):
            r = await webhook._validar_captacao({"nome_completo": "Maria"})
        assert r["captacao_id"] == 4
        assert r["is_parceria"] is False

    @pytest.mark.asyncio
    async def test_id_inventado_vira_nao_sei(self):
        from app.routers import webhook

        with patch.object(captacao, "listar", AsyncMock(return_value=LISTA)):
            r = await webhook._validar_captacao({"captacao_id": 9999})
        assert r["captacao_id"] == 4

    @pytest.mark.asyncio
    async def test_id_valido_passa_intacto(self):
        from app.routers import webhook

        with patch.object(captacao, "listar", AsyncMock(return_value=LISTA)):
            r = await webhook._validar_captacao({"captacao_id": 46})
        assert r["captacao_id"] == 46
        assert r["is_parceria"] is True  # veio da flag do Hamilton, não do modelo

    @pytest.mark.asyncio
    async def test_sem_a_opcao_na_lista_segue_sem_origem(self):
        """Hamilton fora do ar (lista vazia): cadastro sem origem é melhor que
        cadastro travado."""
        from app.routers import webhook

        with patch.object(captacao, "listar", AsyncMock(return_value=[])):
            r = await webhook._validar_captacao({"captacao_id": 7})
        assert "captacao_id" not in r
        assert r["is_parceria"] is False


class TestValorDaOrigem:
    """Rede Cidadã não é gratuita: é R$ 200 contra os R$ 250 de todo mundo.

    `is_parceria` é booleano e não sabe dizer isso — marcá-la zeraria o valor
    de quem deve pagar. O valor vem do cadastro da origem no Hamilton.
    """

    def test_origem_sem_valor_proprio_nao_mexe_no_preco(self):
        assert captacao.valor_da_origem({"pk_captacao": 4, "nome": "Não sei"}) is None
        assert captacao.valor_da_origem({"pk_captacao": 4, "vlr_sessao": None}) is None
        assert captacao.valor_da_origem(None) is None

    def test_origem_com_preco_fechado(self):
        rede = {"pk_captacao": 28, "nome": "Rede Cidadã", "vlr_sessao": "200.00"}
        assert captacao.valor_da_origem(rede) == "200.00"

    def test_valor_quebrado_no_cadastro_nao_derruba_o_cadastro(self):
        assert captacao.valor_da_origem({"pk_captacao": 28, "vlr_sessao": "duzentos"}) is None

    def test_payload_do_hamilton_usa_o_valor_da_origem(self):
        from app.services.hamilton_client import mapear_dados

        from app.services import config_negocio

        padrao = f"{config_negocio.valor('preco_terapia_mensal')}.00"  # 250 em produção
        comum = mapear_dados({"nome_completo": "Fulana", "telefone_contato": "31999990000"})
        assert comum["vlr_sessao"] == padrao
        assert comum["tipo_pagamento"] == "manual"

        # Valor de propósito diferente do padrão da suíte, senão o teste passaria
        # sozinho sem o override fazer nada.
        rede = mapear_dados({
            "nome_completo": "Fulana", "telefone_contato": "31999990000",
            "captacao_id": 28, "vlr_sessao_origem": "199.00",
        })
        assert rede["vlr_sessao"] == "199.00" != padrao
        assert rede["tipo_pagamento"] == "manual"  # paga, só que menos

    def test_parceria_continua_zerando_mesmo_com_valor_na_origem(self):
        from app.services.hamilton_client import mapear_dados

        p = mapear_dados({
            "nome_completo": "Fulana", "telefone_contato": "31999990000",
            "is_parceria": True, "vlr_sessao_origem": "200.00",
        })
        assert p["vlr_sessao"] == "0.00"
        assert p["tipo_pagamento"] == "parceria"
