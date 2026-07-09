"""Recebimento de imagem e documento (P3).

A Sofia não "lê" o anexo: ela guarda e passa pra Thainá, que abre no painel.

Por que os bytes vão pro banco: a URL que a Meta devolve **expira em minutos**,
então não dá pra guardar só o link; e o filesystem do Render é recriado a cada
deploy. O Postgres (Neon) é o único lugar durável que já temos, e a mídia apaga
junto com a conversa (CASCADE), o que mantém o "Reiniciar conversa" limpando tudo.

LGPD: o conteúdo do anexo **nunca** é logado — só tipo, tamanho e ids.
"""

import logging
import re

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Mensagem, Midia
from app.services import whatsapp_client

logger = logging.getLogger(__name__)

# Teto por arquivo. A Meta já limita o envio (imagem 5MB, documento 100MB), mas
# um documento de 100MB no Postgres é abuso: recusamos e pedimos outro caminho.
# Se isto ficar apertado, é sinal de que chegou a hora do bucket externo.
TAMANHO_MAXIMO = 8 * 1024 * 1024  # 8 MB

TIPOS_SUPORTADOS = ("image", "document")

# O que a Thainá vê no chat no lugar do anexo (o texto vai pro histórico do LLM,
# então descreve o anexo sem expor conteúdo).
ROTULOS = {
    "image": "[imagem recebida]",
    "document": "[documento recebido]",
}


# Nome de arquivo vem do paciente: só letras, dígitos e pontuação inofensiva.
# Barra e ponto-ponto (traversal), aspas e \n (header injection) viram '_'.
_NOME_SEGURO = re.compile(r"[^A-Za-z0-9._ -]")

# MIME também vem de fora. Servir `text/html` do domínio do painel seria XSS na
# sessão da Thainá; servir tipo malformado quebra o header. Só deixamos passar um
# MIME bem-formado, e o que não for exibível vai como octet-stream (download).
_MIME_VALIDO = re.compile(r"^[\w.+-]+/[\w.+-]+$")

# Allowlist, não prefixo: `image/svg+xml` casaria com "image/" e SVG executa
# <script> — seria XSS na origem do painel. Só formatos raster + PDF.
_MIME_INLINE_SEGURO = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/jpg",
        "image/gif",
        "image/webp",
        "application/pdf",
    }
)


class MidiaError(Exception):
    """Não foi possível obter ou guardar o anexo."""


def mime_seguro(midia: Midia) -> str:
    """MIME que podemos devolver no `Content-Type` sem risco.

    Só imagem e PDF são exibidos inline; qualquer outra coisa vira download
    genérico, pra o navegador nunca interpretar o anexo como HTML/script na
    origem do painel.
    """
    mime = (midia.mime or "").strip().lower()
    if not _MIME_VALIDO.match(mime):
        return "application/octet-stream"
    if mime in _MIME_INLINE_SEGURO:
        return mime
    return "application/octet-stream"


def _extrair(mensagem: dict, tipo: str) -> tuple[str | None, str | None]:
    """Devolve (media_id, nome_do_arquivo) do payload da Meta."""
    bloco = mensagem.get(tipo) or {}
    return bloco.get("id"), bloco.get("filename")


async def baixar_e_guardar(db: AsyncSession, mensagem_db: Mensagem, mensagem: dict) -> Midia:
    """Baixa o anexo da Cloud API e persiste ligado à mensagem já registrada.

    Raises:
        MidiaError: se não houver media_id, se o download falhar ou se o arquivo
            passar de `TAMANHO_MAXIMO`.
    """
    tipo = mensagem.get("type", "")
    media_id, nome = _extrair(mensagem, tipo)
    if not media_id:
        raise MidiaError(f"mensagem de {tipo} sem media_id")

    try:
        conteudo, mime = await whatsapp_client.baixar_midia(media_id)
    except whatsapp_client.WhatsAppError as exc:
        raise MidiaError(f"falha ao baixar mídia {media_id}: {exc}") from exc

    if len(conteudo) > TAMANHO_MAXIMO:
        raise MidiaError(f"mídia {media_id} tem {len(conteudo)} bytes (máx {TAMANHO_MAXIMO})")

    midia = Midia(
        mensagem_id=mensagem_db.id,
        mime=mime or "application/octet-stream",
        nome_arquivo=nome,
        tamanho=len(conteudo),
        conteudo=conteudo,
    )
    db.add(midia)
    await db.flush()
    # Sem nome de arquivo nem conteúdo no log (LGPD): só o que ajuda a diagnosticar.
    logger.info(
        "Mídia guardada (mensagem=%s, tipo=%s, mime=%s, bytes=%d)",
        mensagem_db.id,
        tipo,
        midia.mime,
        midia.tamanho,
    )
    return midia


def e_imagem(midia: Midia) -> bool:
    """Imagem ganha miniatura no painel; o resto vira ícone + botão de baixar.

    Usa o MIME já saneado: um `image/svg+xml` não é exibido (vira download), pra
    não abrirmos uma rota de conteúdo ativo servido da origem do painel.
    """
    return mime_seguro(midia).startswith("image/")


def nome_para_download(midia: Midia) -> str:
    """Nome do arquivo no download. A Meta só manda `filename` pra documento.

    O nome vem do paciente e vai pro header `Content-Disposition`: aspas, quebras
    de linha ou barras ali seriam header injection / path traversal. Ficamos só
    com um conjunto seguro de caracteres.
    """
    bruto = midia.nome_arquivo or ""
    limpo = _NOME_SEGURO.sub("_", bruto).strip("._ ")[:120]
    if limpo:
        return limpo
    extensao = (midia.mime or "").rsplit("/", 1)[-1] or "bin"
    extensao = _NOME_SEGURO.sub("", extensao)[:10] or "bin"
    return f"anexo-{midia.id}.{extensao}"
