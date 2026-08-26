"""Quem loga no painel e quem recebe alerta de WhatsApp (tabela `usuario`).

Substitui o PAINEL_USER/PAINEL_PASSWORD fixo do .env por login individual.
`migrar_usuario_do_env` cria a Thainá automaticamente a partir do .env na
primeira subida, pra ninguém ficar sem acesso no deploy.
"""

import logging

import bcrypt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Usuario

logger = logging.getLogger(__name__)

# bcrypt direto, não passlib: passlib==1.7.4 (sem manutenção desde 2020) tem
# uma checagem interna ("detect_wrap_bug") que quebra com bcrypt>=4.1
# (ValueError: password cannot be longer than 72 bytes) mesmo pra senha
# curta — bug de compatibilidade entre as duas libs, não da senha em si.


def hash_senha(senha: str) -> str:
    return bcrypt.hashpw(senha.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verificar_senha(senha: str, senha_hash: str) -> bool:
    return bcrypt.checkpw(senha.encode("utf-8"), senha_hash.encode("utf-8"))


async def autenticar(db: AsyncSession, username: str, senha: str) -> Usuario | None:
    """Confere usuário/senha contra a tabela. `None` se inválido ou inativo."""
    usuario = (
        await db.execute(select(Usuario).where(Usuario.username == username.strip()))
    ).scalar_one_or_none()
    if usuario is None or not usuario.ativo:
        return None
    if not verificar_senha(senha, usuario.senha_hash):
        return None
    return usuario


async def listar(db: AsyncSession) -> list[Usuario]:
    return list(
        (await db.execute(select(Usuario).order_by(Usuario.nome))).scalars().all()
    )


async def obter(db: AsyncSession, usuario_id: int) -> Usuario | None:
    return await db.get(Usuario, usuario_id)


async def criar(
    db: AsyncSession,
    *,
    nome: str,
    username: str,
    senha: str,
    telefone_whatsapp: str | None,
    recebe_alertas: bool,
) -> Usuario:
    usuario = Usuario(
        nome=nome.strip(),
        username=username.strip(),
        senha_hash=hash_senha(senha),
        telefone_whatsapp=(telefone_whatsapp or "").strip() or None,
        recebe_alertas=recebe_alertas,
    )
    db.add(usuario)
    await db.flush()
    return usuario


async def atualizar(
    db: AsyncSession,
    usuario: Usuario,
    *,
    nome: str,
    telefone_whatsapp: str | None,
    recebe_alertas: bool,
    ativo: bool,
    nova_senha: str | None = None,
) -> Usuario:
    usuario.nome = nome.strip()
    usuario.telefone_whatsapp = (telefone_whatsapp or "").strip() or None
    usuario.recebe_alertas = recebe_alertas
    usuario.ativo = ativo
    if nova_senha:
        usuario.senha_hash = hash_senha(nova_senha)
    await db.flush()
    return usuario


async def telefones_para_alerta(db: AsyncSession) -> list[str]:
    """Números de quem está ativo e com o alerta ligado agora."""
    usuarios = (
        await db.execute(
            select(Usuario).where(Usuario.ativo.is_(True), Usuario.recebe_alertas.is_(True))
        )
    ).scalars().all()
    return [u.telefone_whatsapp for u in usuarios if u.telefone_whatsapp]


async def migrar_usuario_do_env(db: AsyncSession) -> None:
    """Cria a Thainá a partir do PAINEL_USER/PAINEL_PASSWORD, uma vez só.

    Roda no startup. Se já existir QUALQUER usuário na tabela, não faz nada —
    é só a rede de segurança pra quem já estava rodando com o login antigo do
    .env não ficar trancado fora do painel no dia em que essa tabela nasceu.
    """
    existe = (await db.execute(select(Usuario.id).limit(1))).first()
    if existe:
        return
    if not settings.painel_user or not settings.painel_password:
        return
    await criar(
        db,
        nome="Thainá",
        username=settings.painel_user,
        senha=settings.painel_password,
        telefone_whatsapp=settings.thaina_whatsapp_number or None,
        recebe_alertas=True,
    )
    await db.commit()
    logger.info("Usuário do painel migrado do .env: %s", settings.painel_user)
