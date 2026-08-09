"""Cliente HTTP do Hamilton (sistema clínico). Autenticação JWT (SimpleJWT).

Endpoints consumidos (criados no Hamilton para a Sofia):
- POST {url}/authentication/token/            -> {access, refresh}
- GET  {url}/api/v1/pacientes/buscar/?telefone=  -> lista de pacientes
- POST {url}/api/v1/pacientes/                -> cria lead (terapeuta nulo, defaults)

Falha de integração vira HamiltonError; o orquestrador degrada para
`cadastro_pendente` (a Thainá cadastra manualmente). Usa httpx async.
"""

import logging
import re
from datetime import datetime
from functools import lru_cache
from typing import Any

import httpx

from app.config import settings
from app.services import config_negocio

logger = logging.getLogger(__name__)


class HamiltonError(Exception):
    """Falha ao falar com a API do Hamilton."""


def normalizar_telefone(numero: str | None) -> str:
    """Mantém só dígitos e remove o DDI 55 quando presente (5531... -> 31...)."""
    digits = re.sub(r"\D", "", numero or "")
    if len(digits) > 11 and digits.startswith("55"):
        digits = digits[2:]
    return digits


def _itens_intake(dados: dict[str, Any]) -> list[str]:
    """Itens que a Sofia coleta e que o Hamilton guarda na observação (texto livre)."""
    itens = []
    if dados.get("motivo_busca"):
        itens.append(f"Motivo: {dados['motivo_busca']}")
    if dados.get("preferencia_terapeuta"):
        itens.append(f"Preferência: {dados['preferencia_terapeuta']}")
    if dados.get("horarios_disponiveis"):
        itens.append(f"Horários: {dados['horarios_disponiveis']}")
    if dados.get("cep"):
        itens.append(f"CEP: {dados['cep']}")
    if dados.get("como_conheceu"):
        # Mantido mesmo com a captação estruturada: guarda o que a pessoa disse
        # com as palavras dela, que é o que permite corrigir uma captação errada.
        itens.append(f"Origem: {dados['como_conheceu']}")
    if dados.get("vinculo_parceria"):
        # Elegibilidade de convênio é auto-declarada; esta linha é a única
        # evidência disso se o parceiro questionar uma consulta na prestação
        # de contas.
        itens.append(f"Parceria: {dados['vinculo_parceria']}")
    if dados.get("observacoes"):
        itens.append(f"Obs: {dados['observacoes']}")
    return itens


def mapear_dados_update(dados: dict[str, Any], existente: dict) -> dict[str, Any]:
    """Payload de atualização (PATCH) quando a MESMA pessoa volta (mesmo nome).

    Atualiza os campos factuais que a Sofia coletou (nascimento, contato de apoio,
    endereço, email) e **anexa** um resumo datado às observações, preservando o que
    já estava lá. NÃO mexe no nome (evita sobrescrever uma correção da coordenação).
    """
    payload: dict[str, Any] = {}
    if dados.get("data_nascimento"):
        payload["dat_nascimento"] = dados["data_nascimento"]
    if dados.get("telefone_apoio"):
        payload["contato_apoio"] = normalizar_telefone(dados["telefone_apoio"])
    if dados.get("endereco"):
        payload["endereco"] = dados["endereco"]
    if dados.get("email"):
        payload["email"] = dados["email"]
    resumo = " | ".join(_itens_intake(dados))
    if resumo:
        anterior = (existente.get("observacao") or "").strip()
        nota = f"[Atualização Sofia {datetime.now().strftime('%d/%m/%Y')}] {resumo}"
        payload["observacao"] = f"{anterior}\n{nota}" if anterior else nota
    return payload


def mapear_dados(dados: dict[str, Any]) -> dict[str, Any]:
    """Converte `dados_coletados` da Sofia no payload de intake do Hamilton.

    A origem do paciente vai em `fk_captacao` (ID real do Hamilton) e a
    mensalidade acordada em `vlr_sessao` — antes os dois eram decididos lá
    dentro, com a captação fixa "WhatsApp (Sofia)" (que é o canal, não a origem)
    e um valor default desatualizado.

    Paciente de parceria vai com valor zero: quem custeia é o parceiro. O
    Hamilton também zera por conta própria ao ver uma captação de parceria — a
    checagem dupla é de propósito, porque o dado que decide isso veio de uma
    conversa.

    O que o Hamilton não tem como campo nativo (CEP, o texto literal da origem,
    o vínculo declarado com a parceria) continua indo pela `observacao`.
    """
    observacao = _itens_intake(dados)
    parceria = bool(dados.get("is_parceria"))
    # Mensalidade só faz sentido pra terapia paga (neuro é orçamento à parte, e
    # parceria não tem mensalidade nenhuma).
    if not parceria and "neuro" not in (dados.get("motivo_busca") or "").lower():
        preco = config_negocio.valor("preco_terapia_mensal")
        observacao.append(f"Mensalidade: R$ {preco:,}".replace(",", "."))

    payload: dict[str, Any] = {
        "nome": dados.get("nome_completo"),
        "telefone": normalizar_telefone(dados.get("telefone_contato")),
        "vlr_sessao": "0.00" if parceria else f"{config_negocio.valor('preco_terapia_mensal')}.00",
        # `parceria` tira o paciente do dropdown de cobrança manual do Hamilton.
        # Sem isso ele entra como 'manual' e reaparece na fila de quem deve
        # mensalidade — justamente quem não deve nada.
        "tipo_pagamento": "parceria" if parceria else "manual",
    }
    captacao_id = dados.get("captacao_id")
    if captacao_id:
        payload["fk_captacao"] = captacao_id
    if dados.get("data_nascimento"):
        payload["dat_nascimento"] = dados["data_nascimento"]
    if dados.get("telefone_apoio"):
        payload["contato_apoio"] = normalizar_telefone(dados["telefone_apoio"])
    if dados.get("endereco"):
        payload["endereco"] = dados["endereco"]
    if dados.get("email"):
        payload["email"] = dados["email"]
    if observacao:
        payload["observacao"] = " | ".join(observacao)
    return payload


class HamiltonClient:
    """Wrapper fino sobre a API do Hamilton, com token JWT cacheado."""

    def __init__(
        self,
        base_url: str | None = None,
        username: str | None = None,
        password: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base = (base_url or settings.hamilton_api_url).rstrip("/")
        self._user = username or settings.hamilton_username
        self._pwd = password or settings.hamilton_password
        self._client = client  # injeção em teste; se None, cria por request
        self._token: str | None = None

    async def _autenticar(self, client: httpx.AsyncClient) -> str:
        resp = await client.post(
            f"{self._base}/authentication/token/",
            json={"username": self._user, "password": self._pwd},
        )
        if resp.status_code != 200:
            raise HamiltonError(f"Auth Hamilton falhou ({resp.status_code})")
        token = resp.json().get("access")
        if not token:
            raise HamiltonError("Auth Hamilton não retornou token de acesso")
        return token

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        owns = self._client is None
        client = self._client or httpx.AsyncClient(timeout=10.0)
        try:
            if not self._token:
                self._token = await self._autenticar(client)
            headers = {"Authorization": f"Bearer {self._token}"}
            resp = await client.request(method, f"{self._base}{path}", headers=headers, **kwargs)
            if resp.status_code == 401:
                # Token expirado: re-autentica uma vez e repete.
                self._token = await self._autenticar(client)
                headers = {"Authorization": f"Bearer {self._token}"}
                resp = await client.request(
                    method, f"{self._base}{path}", headers=headers, **kwargs
                )
            return resp
        except httpx.HTTPError as exc:
            raise HamiltonError(f"Falha de rede com Hamilton: {exc}") from exc
        finally:
            if owns:
                await client.aclose()

    async def buscar_paciente_por_telefone(self, telefone: str | None) -> list[dict]:
        """Retorna a lista de pacientes com aquele telefone (vazia se nenhum)."""
        tel = normalizar_telefone(telefone)
        if not tel:
            return []
        resp = await self._request("GET", f"/api/v1/pacientes/buscar/?telefone={tel}")
        if resp.status_code != 200:
            raise HamiltonError(f"Busca de paciente falhou ({resp.status_code})")
        data = resp.json()
        if isinstance(data, dict) and "results" in data:  # caso paginado
            return data["results"]
        return data if isinstance(data, list) else []

    async def listar_captacoes(self) -> list[dict]:
        """Captações ativas do Hamilton (origem do paciente: Instagram, indicação...).

        É a lista que a Sofia oferece ao modelo pra ele casar o que a pessoa
        contou com uma origem real — e é contra ela que o ID escolhido é
        validado antes de virar cadastro.
        """
        resp = await self._request("GET", "/api/v1/captacoes/")
        if resp.status_code != 200:
            raise HamiltonError(f"Listagem de captações falhou ({resp.status_code})")
        data = resp.json()
        if isinstance(data, dict) and "results" in data:  # caso paginado
            data = data["results"]
        if not isinstance(data, list):
            return []
        return [c for c in data if c.get("is_active", True)]

    async def avaliacoes_pendentes(self, incluir_enviadas: bool = False) -> list[dict]:
        """Pesquisas de satisfação a conduzir (fila criada pelos gatilhos do Hamilton).

        Sem `incluir_enviadas`, só as que ainda não foram abordadas. Com, também
        as já enviadas — é assim que se acha quem precisa de lembrete ou de
        encerramento por prazo.
        """
        rota = "/api/v1/avaliacoes/pendentes/"
        if incluir_enviadas:
            rota += "?enviadas=1"
        resp = await self._request("GET", rota)
        if resp.status_code != 200:
            raise HamiltonError(f"Busca de avaliações pendentes falhou ({resp.status_code})")
        data = resp.json()
        if isinstance(data, dict) and "results" in data:  # caso paginado
            data = data["results"]
        return data if isinstance(data, list) else []

    async def criar_avaliacao_entrada(self, paciente_id: int) -> dict | None:
        """Cria a avaliação de LINHA DE BASE do paciente (a única que a Sofia cria).

        As outras três nascem de gatilho interno do Hamilton. Esta não pode:
        um signal no `Paciente` dispararia pra todo mundo cadastrado lá, inclusive
        quem nunca falou com a Sofia. **Quem sabe que houve encaminhamento pela
        Sofia é a Sofia.**

        O endpoint é idempotente do lado do Hamilton: repetir devolve a que já
        existe (200) em vez de criar uma segunda (201). Por isso aqui não há
        controle de "já criei" — chamar de novo é barato e seguro.
        """
        if not paciente_id:
            return None
        resp = await self._request("POST", "/api/v1/avaliacoes/", json={"fk_paciente": paciente_id})
        if resp.status_code not in (200, 201):
            raise HamiltonError(
                f"Criação da avaliação de entrada falhou ({resp.status_code}): {resp.text[:200]}"
            )
        return resp.json()

    async def obter_avaliacao(self, pk: int) -> dict | None:
        """Uma avaliação pelo id. Mesmo shape dos itens de `avaliacoes_pendentes`.

        `None` quer dizer **o Hamilton respondeu e ela não existe** (404). Falha
        de rede ou erro do servidor levanta `HamiltonError` — quem chama TEM que
        distinguir os dois casos.

        Existe porque a alternativa era baixar a fila inteira (`?enviadas=1`, o
        acumulado histórico de anos) pra achar um pk já conhecido: payload que
        estourava o timeout, e o timeout era lido como "a avaliação sumiu".
        """
        if not pk:
            return None
        resp = await self._request("GET", f"/api/v1/avaliacoes/{pk}/")
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise HamiltonError(f"Leitura da avaliação {pk} falhou ({resp.status_code})")
        data = resp.json()
        return data if isinstance(data, dict) else None

    async def atualizar_avaliacao(self, pk: int, payload: dict) -> dict:
        """PATCH parcial de uma avaliação (respostas, marcação de envio, status)."""
        if not pk or not payload:
            return {}
        resp = await self._request("PATCH", f"/api/v1/avaliacoes/{pk}/", json=payload)
        if resp.status_code not in (200, 201):
            raise HamiltonError(
                f"Atualização de avaliação falhou ({resp.status_code}): {resp.text[:200]}"
            )
        return resp.json()

    async def status_primeira_consulta(self, ids: list[int]) -> dict[int, dict]:
        """Status da 1ª consulta de cada paciente (por pk_paciente).

        Retorna um dict {pk_paciente: {nome, created_at, primeira_consulta_realizada,
        dat_primeira_consulta}}. Usado no acompanhamento (Demandas 3 e 4).
        """
        ids = [i for i in ids if i]
        if not ids:
            return {}
        param = ",".join(str(i) for i in ids)
        resp = await self._request(
            "GET", f"/api/v1/pacientes/status-primeira-consulta/?ids={param}"
        )
        if resp.status_code != 200:
            raise HamiltonError(f"Status da 1ª consulta falhou ({resp.status_code})")
        data = resp.json()
        return {item["pk_paciente"]: item for item in data if item.get("pk_paciente")}

    async def atualizar_paciente(self, pid: int, payload: dict) -> dict:
        """PATCH parcial da ficha de um paciente que já existe (reencontro).

        `payload` já vem pronto (ver `mapear_dados_update`). Payload vazio = no-op.
        """
        if not pid or not payload:
            return {}
        resp = await self._request("PATCH", f"/api/v1/pacientes/{pid}/atualizar/", json=payload)
        if resp.status_code not in (200, 201):
            raise HamiltonError(
                f"Atualização de paciente falhou ({resp.status_code}): {resp.text[:200]}"
            )
        return resp.json()

    async def criar_paciente(self, dados: dict) -> dict:
        """Cria o paciente no Hamilton e devolve o registro criado."""
        payload = mapear_dados(dados)
        if not payload.get("nome") or not payload.get("telefone"):
            raise HamiltonError("Dados insuficientes para cadastro (nome/telefone)")
        resp = await self._request("POST", "/api/v1/pacientes/", json=payload)
        if resp.status_code not in (200, 201):
            raise HamiltonError(
                f"Cadastro de paciente falhou ({resp.status_code}): {resp.text[:200]}"
            )
        return resp.json()


@lru_cache(maxsize=1)
def get_hamilton_client() -> HamiltonClient:
    """Retorna o cliente Hamilton padrão (singleton). Ponto único de mocking."""
    return HamiltonClient()
