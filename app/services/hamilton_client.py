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


def mapear_dados(dados: dict[str, Any]) -> dict[str, Any]:
    """Converte `dados_coletados` da Sofia no payload de intake do Hamilton.

    Campos que o Hamilton ainda não tem como nativos (CEP, captação/origem e o
    valor da mensalidade) vão pela `observacao` (texto livre que a Thainá lê e
    usa pra completar o cadastro no Hamilton). O Hamilton seta um valor padrão
    de mensalidade que não bate com o nosso, então anotamos o valor configurado.
    """
    motivo = (dados.get("motivo_busca") or "").lower()
    eh_neuro = "neuro" in motivo

    observacao = []
    if dados.get("motivo_busca"):
        observacao.append(f"Motivo: {dados['motivo_busca']}")
    if dados.get("preferencia_terapeuta"):
        observacao.append(f"Preferência: {dados['preferencia_terapeuta']}")
    if dados.get("horarios_disponiveis"):
        observacao.append(f"Horários: {dados['horarios_disponiveis']}")
    if dados.get("cep"):
        observacao.append(f"CEP: {dados['cep']}")
    if dados.get("como_conheceu"):
        observacao.append(f"Origem: {dados['como_conheceu']}")
    # Mensalidade só faz sentido pra terapia (neuro é pagamento único/orçamento).
    if not eh_neuro:
        preco = config_negocio.valor("preco_terapia_mensal")
        observacao.append(f"Mensalidade: R$ {preco:,}".replace(",", "."))

    payload: dict[str, Any] = {
        "nome": dados.get("nome_completo"),
        "telefone": normalizar_telefone(dados.get("telefone_contato")),
    }
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
