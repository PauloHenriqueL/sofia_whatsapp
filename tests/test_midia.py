"""Recebimento de imagem/documento (P3): guardar, servir e não se machucar.

O nome do arquivo e o MIME vêm do paciente e vão parar em headers HTTP, então há
bateria específica pra header injection, path traversal e XSS na origem do painel.
"""

from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base
from app.models import Conversa, Mensagem, Midia
from app.services import conversation, midia, painel, whatsapp_client


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


async def _mensagem(session, tipo="image"):
    conversa = await conversation.obter_ou_criar_conversa(session, "5531999998888")
    return await conversation.registrar_mensagem_recebida(
        session, conversa, tipo=tipo, texto=midia.ROTULOS[tipo], whatsapp_message_id=f"w-{tipo}"
    )


def _payload(tipo="image", **extra):
    return {"type": tipo, tipo: {"id": "media-1", **extra}}


class TestBaixarEGuardar:
    @pytest.mark.asyncio
    async def test_guarda_bytes_mime_e_tamanho(self, session):
        msg = await _mensagem(session)
        with patch.object(
            midia.whatsapp_client,
            "baixar_midia",
            new_callable=AsyncMock,
            return_value=(b"\x89PNG\r\n", "image/png"),
        ):
            registro = await midia.baixar_e_guardar(session, msg, _payload())
        assert registro.mime == "image/png"
        assert registro.tamanho == 6
        assert registro.conteudo == b"\x89PNG\r\n"

    @pytest.mark.asyncio
    async def test_documento_guarda_o_nome(self, session):
        msg = await _mensagem(session, "document")
        with patch.object(
            midia.whatsapp_client,
            "baixar_midia",
            new_callable=AsyncMock,
            return_value=(b"%PDF", "application/pdf"),
        ):
            registro = await midia.baixar_e_guardar(
                session, msg, _payload("document", filename="laudo.pdf")
            )
        assert registro.nome_arquivo == "laudo.pdf"

    @pytest.mark.asyncio
    async def test_sem_media_id_falha(self, session):
        msg = await _mensagem(session)
        with pytest.raises(midia.MidiaError):
            await midia.baixar_e_guardar(session, msg, {"type": "image", "image": {}})

    @pytest.mark.asyncio
    async def test_download_falho_vira_midia_error(self, session):
        msg = await _mensagem(session)
        with patch.object(
            midia.whatsapp_client,
            "baixar_midia",
            new_callable=AsyncMock,
            side_effect=whatsapp_client.WhatsAppError("url expirada"),
        ):
            with pytest.raises(midia.MidiaError):
                await midia.baixar_e_guardar(session, msg, _payload())

    @pytest.mark.asyncio
    async def test_arquivo_grande_demais_e_recusado(self, session):
        msg = await _mensagem(session)
        gigante = b"x" * (midia.TAMANHO_MAXIMO + 1)
        with patch.object(
            midia.whatsapp_client,
            "baixar_midia",
            new_callable=AsyncMock,
            return_value=(gigante, "image/png"),
        ):
            with pytest.raises(midia.MidiaError):
                await midia.baixar_e_guardar(session, msg, _payload())
        # E nada foi persistido.
        assert (await session.execute(select(Midia))).scalar_one_or_none() is None

    @pytest.mark.asyncio
    async def test_nao_loga_o_nome_do_arquivo(self, session, caplog):
        """LGPD: o log não pode conter o nome do arquivo do paciente."""
        import logging

        msg = await _mensagem(session, "document")
        with caplog.at_level(logging.INFO), patch.object(
            midia.whatsapp_client,
            "baixar_midia",
            new_callable=AsyncMock,
            return_value=(b"%PDF", "application/pdf"),
        ):
            await midia.baixar_e_guardar(
                session, msg, _payload("document", filename="laudo-psiquiatrico-joao.pdf")
            )
        registros = " ".join(r.getMessage() for r in caplog.records)
        assert "laudo-psiquiatrico-joao" not in registros


class TestNomeParaDownload:
    """O nome vem do paciente e entra no header Content-Disposition."""

    def test_nome_normal_passa(self):
        assert midia.nome_para_download(Midia(id=1, nome_arquivo="laudo.pdf")) == "laudo.pdf"

    def test_sem_nome_usa_o_mime(self):
        assert midia.nome_para_download(Midia(id=7, mime="image/png")) == "anexo-7.png"

    def test_aspas_e_quebra_de_linha_nao_injetam_header(self):
        malicioso = 'a".pdf\r\nSet-Cookie: sessao=roubada'
        nome = midia.nome_para_download(Midia(id=1, nome_arquivo=malicioso))
        assert '"' not in nome
        assert "\r" not in nome and "\n" not in nome

    def test_path_traversal_e_neutralizado(self):
        nome = midia.nome_para_download(Midia(id=1, nome_arquivo="../../etc/passwd"))
        assert "/" not in nome
        assert not nome.startswith(".")

    def test_nome_so_de_lixo_cai_no_padrao(self):
        assert midia.nome_para_download(Midia(id=3, nome_arquivo="///", mime="application/pdf"))


class TestMimeSeguro:
    """Servir o anexo do painel: nada pode virar script na origem da Thainá."""

    def test_imagem_passa(self):
        assert midia.mime_seguro(Midia(mime="image/jpeg")) == "image/jpeg"

    def test_pdf_passa(self):
        assert midia.mime_seguro(Midia(mime="application/pdf")) == "application/pdf"

    def test_html_vira_download(self):
        # Servir text/html inline seria XSS na sessão do painel.
        assert midia.mime_seguro(Midia(mime="text/html")) == "application/octet-stream"

    def test_svg_vira_download(self):
        # SVG executa <script>: servido inline da origem do painel, seria XSS na
        # sessão da Thainá. Por isso a allowlist é de formatos raster, não "image/".
        assert midia.mime_seguro(Midia(mime="image/svg+xml")) == "application/octet-stream"
        assert not midia.e_imagem(Midia(mime="image/svg+xml"))

    def test_mime_maiusculo_e_normalizado(self):
        assert midia.mime_seguro(Midia(mime="IMAGE/PNG")) == "image/png"

    def test_mime_malformado_vira_octet_stream(self):
        assert midia.mime_seguro(Midia(mime="text/html\r\nX: y")) == "application/octet-stream"
        assert midia.mime_seguro(Midia(mime="")) == "application/octet-stream"

    def test_e_imagem(self):
        assert midia.e_imagem(Midia(mime="image/png"))
        assert not midia.e_imagem(Midia(mime="application/pdf"))


class TestExclusaoLimpaAMidia:
    @pytest.mark.asyncio
    async def test_reiniciar_conversa_apaga_o_anexo(self, session):
        """LGPD: 'Reiniciar conversa' não pode deixar anexo órfão no banco."""
        msg = await _mensagem(session)
        with patch.object(
            midia.whatsapp_client,
            "baixar_midia",
            new_callable=AsyncMock,
            return_value=(b"PNG", "image/png"),
        ):
            await midia.baixar_e_guardar(session, msg, _payload())
        await session.commit()

        conversa = (await session.execute(select(Conversa))).scalar_one()
        await painel.excluir_conversa(session, conversa)

        assert (await session.execute(select(Midia))).scalar_one_or_none() is None
        assert (await session.execute(select(Mensagem))).scalar_one_or_none() is None
