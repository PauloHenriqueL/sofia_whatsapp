"""Dependências compartilhadas: autenticação do painel e sessão de banco."""

import secrets

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from app.config import settings
from app.database import get_db  # noqa: F401 (reexport p/ conveniência dos routers)

_security = HTTPBasic()


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
