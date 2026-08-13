"""Encurtador dos links de pagamento: `allos.org.br/p/k7m2xq`.

O problema que resolve não é comprimento — depois da troca de Checkout Session
por Payment Link o link do Stripe já é curto. É **confiança**: uma cobrança
chegando por WhatsApp com um domínio que o paciente não conhece
(`buy.stripe.com`) tem exatamente o formato de golpe que as pessoas foram
treinadas a desconfiar. O ganho está no momento em que ela decide clicar.

⚠️ **Depois do clique o Stripe aparece na barra de endereço de qualquer jeito** —
é um redirect, não um proxy (fazer proxy da página de checkout do Stripe não
funcionaria: domínio, cookies e antifraude são dele). Isso é limitação conhecida
e aceita.

O dado mora aqui e não no banco do site porque "quem é o paciente X e quanto ele
paga" é dado financeiro; o site é só proxy burro.

**Degrada sozinho**: sem `LINK_CURTO_BASE` configurada, o link sai apontando pra
própria Sofia (`/l/<slug>`). Assim o deploy daqui não depende do deploy do site —
e se o site cair, o que já foi mandado pelo domínio antigo continua de pé.
"""

import logging
import secrets
import string

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import func

from app.config import settings
from app.models import LinkCurto

logger = logging.getLogger(__name__)

# Sem 0/O/1/l/I: o link é lido em voz alta e digitado à mão com mais frequência
# do que se imagina (paciente que abre no computador, recepção que dita).
ALFABETO = "".join(c for c in string.ascii_lowercase + string.digits if c not in "0o1li")
TAMANHO = 7
TENTATIVAS = 5


def _novo_slug() -> str:
    return "".join(secrets.choice(ALFABETO) for _ in range(TAMANHO))


def base_publica() -> str:
    """Prefixo do link curto, sem barra no fim."""
    return (settings.link_curto_base or f"{settings.base_url}/l").rstrip("/")


def url_de(slug: str) -> str:
    return f"{base_publica()}/{slug}"


async def encurtar(db: AsyncSession, destino: str, conversa_id: int | None = None) -> str:
    """URL curta pro `destino`. **Idempotente por destino.**

    Reusar em vez de criar outro importa: a Sofia remonta o link a cada turno da
    cobrança, e um slug novo por turno faria ela mandar endereços diferentes pro
    mesmo pagamento — o paciente não saberia qual vale, e o contador de cliques
    viraria pó.

    Nunca levanta: se o banco falhar, devolve o link do Stripe puro. Um link feio
    que funciona é melhor que uma cobrança que não sai.
    """
    destino = (destino or "").strip()
    if not destino:
        return destino
    try:
        existente = await db.scalar(select(LinkCurto).where(LinkCurto.destino == destino))
        if existente is not None:
            return url_de(existente.slug)

        for _ in range(TENTATIVAS):
            link = LinkCurto(slug=_novo_slug(), destino=destino, conversa_id=conversa_id)
            db.add(link)
            try:
                await db.flush()
            except IntegrityError:
                # Colisão de slug (ou corrida com outro turno no mesmo destino).
                await db.rollback()
                achado = await db.scalar(select(LinkCurto).where(LinkCurto.destino == destino))
                if achado is not None:
                    return url_de(achado.slug)
                continue
            return url_de(link.slug)
        logger.error("Não consegui gerar slug único depois de %d tentativas", TENTATIVAS)
    except Exception:  # noqa: BLE001 - link curto é conveniência, não pode derrubar cobrança
        logger.exception("Falha ao encurtar link; devolvendo a URL original")
    return destino


async def resolver(db: AsyncSession, slug: str) -> str | None:
    """Destino do slug, contando o clique. `None` = slug desconhecido."""
    link = await db.scalar(select(LinkCurto).where(LinkCurto.slug == (slug or "").strip().lower()))
    if link is None:
        return None
    # UPDATE direto em vez de ler-somar-gravar: dois cliques simultâneos no mesmo
    # link (paciente clicando duas vezes) perderiam uma contagem.
    await db.execute(
        update(LinkCurto)
        .where(LinkCurto.id == link.id)
        .values(cliques=LinkCurto.cliques + 1, ultimo_clique_em=func.now())
    )
    await db.commit()
    return link.destino
