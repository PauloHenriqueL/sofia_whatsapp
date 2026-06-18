"""Dependências compartilhadas: autenticação do painel e sessão de banco."""

import secrets
from urllib.parse import urlparse

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from app.config import settings
from app.database import get_db  # noqa: F401 (reexport p/ conveniência dos routers)

_security = HTTPBasic()


def verificar_origem(request: Request) -> None:
    """Defesa contra CSRF: se houver header Origin, ele tem que bater com o host.

    Funciona com Basic Auth (sem sessão/cookie): um POST cross-site disparado por
    um site malicioso sempre carrega Origin da origem atacante, que não bate com
    o host do painel e é rejeitado. Requisições sem Origin (navegação direta,
    clientes não-browser) passam — o ataque CSRF via browser sempre tem Origin.
    """
    origin = request.headers.get("origin")
    if not origin:
        return
    origin_host = urlparse(origin).netloc
    host = request.headers.get("host", "")
    if origin_host and origin_host != host:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Origem inválida")


def autenticar(
    credentials: HTTPBasicCredentials = Depends(_security),
) -> str:
    """HTTP Basic Auth do painel. Compara usuário e senha em tempo constante.

    Substituir por algo melhor pós-MVP (ver sofia_briefing.md).
    """
    usuario_ok = secrets.compare_digest(credentials.username, settings.painel_user)
    senha_ok = secrets.compare_digest(credentials.password, settings.painel_password)
    if not (usuario_ok and senha_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Credenciais inválidas",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username
