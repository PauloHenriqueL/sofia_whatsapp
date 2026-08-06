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
LISTA = [INSTAGRAM, PREFEITURA]


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
