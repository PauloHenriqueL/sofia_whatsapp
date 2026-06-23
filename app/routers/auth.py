"""Login/logout do painel (sessão por cookie assinado)."""

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app.dependencies import credenciais_validas, verificar_origem

logger = logging.getLogger(__name__)
router = APIRouter(tags=["auth"])

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


@router.get("/login")
async def pagina_login(request: Request):
    if request.session.get("usuario"):
        return RedirectResponse("/painel/", status_code=303)
    return templates.TemplateResponse("login.html", {"request": request})


@router.post("/login", dependencies=[Depends(verificar_origem)])
async def fazer_login(request: Request, usuario: str = Form(...), senha: str = Form(...)):
    if credenciais_validas(usuario.strip(), senha):
        request.session["usuario"] = usuario.strip()
        logger.info("Login no painel: %s", usuario.strip())
        return RedirectResponse("/painel/", status_code=303)
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "erro": "Usuário ou senha inválidos."},
        status_code=401,
    )


@router.post("/logout", dependencies=[Depends(verificar_origem)])
async def fazer_logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
