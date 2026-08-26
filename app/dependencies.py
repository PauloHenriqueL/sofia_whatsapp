"""Dependências compartilhadas: autenticação (sessão), CSRF e sessão de banco."""

from urllib.parse import urlparse

from fastapi import HTTPException, Request, status

from app.database import get_db  # noqa: F401 (reexport p/ conveniência dos routers)


def verificar_origem(request: Request) -> None:
    """Defesa contra CSRF: se houver header Origin, ele tem que bater com o host.

    Um POST cross-site disparado por outro site sempre carrega o Origin da origem
    atacante, que não bate com o host do painel e é rejeitado. Requisições sem
    Origin (navegação direta, clientes não-browser) passam.
    """
    origin = request.headers.get("origin")
    if not origin:
        return
    origin_host = urlparse(origin).netloc
    host = request.headers.get("host", "")
    if origin_host and origin_host != host:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Origem inválida")


def requer_login_pagina(request: Request) -> str:
    """Páginas HTML: redireciona pro /login se não estiver autenticado."""
    usuario = request.session.get("usuario")
    if not usuario:
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/login"})
    return usuario


def requer_login_api(request: Request) -> str:
    """API JSON: responde 401 se não estiver autenticado."""
    usuario = request.session.get("usuario")
    if not usuario:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Não autenticado")
    return usuario
