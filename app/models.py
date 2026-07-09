"""Modelos SQLAlchemy: Conversa, Mensagem, Escalada.

Esquema conforme sofia_briefing.md. JSON portável (JSONB em Postgres,
JSON em SQLite) e timestamps com timezone.
"""

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, LargeBinary, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

# JSONB em Postgres, JSON genérico nos demais (SQLite no dev local).
JSONType = JSON().with_variant(JSONB(), "postgresql")


class Conversa(Base):
    __tablename__ = "conversa"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    numero_whatsapp: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    paciente_hamilton_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    modo: Mapped[str] = mapped_column(String(10), default="bot", index=True)
    estado: Mapped[str] = mapped_column(String(30), default="novo")
    dados_coletados: Mapped[dict] = mapped_column(JSONType, default=dict)
    criada_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    atualizada_em: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    # Quando a Sofia já mandou o follow-up automático de lead parado (Frente 2).
    # NULL = ainda não mandou; garante no máximo um follow-up por conversa.
    seguimento_enviado_em: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Quando a Thainá marcou a cobrança como resolvida (Demanda 4). NULL = ainda
    # pendente; ao marcar, o paciente sai da lista de "pronto pra cobrança".
    cobranca_resolvida_em: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    mensagens: Mapped[list["Mensagem"]] = relationship(
        back_populates="conversa", cascade="all, delete-orphan"
    )
    escaladas: Mapped[list["Escalada"]] = relationship(
        back_populates="conversa", cascade="all, delete-orphan"
    )


class Mensagem(Base):
    __tablename__ = "mensagem"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    conversa_id: Mapped[int] = mapped_column(ForeignKey("conversa.id", ondelete="CASCADE"))
    direcao: Mapped[str] = mapped_column(String(10))  # 'recebida' | 'enviada'
    origem: Mapped[str] = mapped_column(String(30))  # 'paciente' | 'bot' | 'thaina'
    tipo: Mapped[str] = mapped_column(String(20), default="texto")
    texto: Mapped[str | None] = mapped_column(Text, nullable=True)
    whatsapp_message_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # Atributo 'extra' mapeado pra coluna 'metadata' ('metadata' é reservado no ORM).
    extra: Mapped[dict] = mapped_column("metadata", JSONType, default=dict)
    criada_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    conversa: Mapped["Conversa"] = relationship(back_populates="mensagens")
    # Imagem/documento anexo (None pra texto/áudio). `lazy="selectin"` porque o
    # painel sempre lê a lista de mensagens e precisa saber se tem anexo.
    midia: Mapped["Midia | None"] = relationship(
        back_populates="mensagem", cascade="all, delete-orphan", uselist=False, lazy="selectin"
    )

    __table_args__ = (
        Index("idx_mensagem_conversa", "conversa_id", "criada_em"),
        # Idempotência: mesma mensagem da Meta nunca é processada 2x.
        Index(
            "idx_mensagem_whatsapp_id",
            "whatsapp_message_id",
            unique=True,
            postgresql_where=whatsapp_message_id.isnot(None),
            sqlite_where=whatsapp_message_id.isnot(None),
        ),
    )


class Midia(Base):
    """Imagem/documento que o paciente mandou, guardada pra Thainá ver no painel.

    Os bytes ficam aqui (não em disco): o Render recria o filesystem a cada deploy,
    e a URL que a Meta devolve expira em minutos — então baixamos na hora e
    persistimos. Apaga junto com a mensagem (CASCADE), que apaga junto com a
    conversa: o "Reiniciar conversa" continua limpando tudo (LGPD).
    """

    __tablename__ = "midia"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mensagem_id: Mapped[int] = mapped_column(
        ForeignKey("mensagem.id", ondelete="CASCADE"), index=True
    )
    mime: Mapped[str] = mapped_column(String(100))
    nome_arquivo: Mapped[str | None] = mapped_column(String(255), nullable=True)
    tamanho: Mapped[int] = mapped_column(Integer)
    # `deferred`: o painel lista mensagens o tempo todo (poll de 5s) e só precisa
    # dos metadados. Os bytes só são carregados por quem acessa `.conteudo`
    # (a rota de download). Sem isso, cada poll arrastaria todos os blobs.
    conteudo: Mapped[bytes] = mapped_column(LargeBinary, deferred=True)
    criada_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    mensagem: Mapped["Mensagem"] = relationship(back_populates="midia")


class Configuracao(Base):
    """Valores de negócio editáveis pela Thainá no painel (preço, parcelas...).

    Chave/valor simples; o valor é guardado como texto e convertido no uso.
    """

    __tablename__ = "configuracao"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chave: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    # Text (não String(100)): além dos valores curtos, guarda também os textos
    # dos prompts editáveis no painel, que são grandes.
    valor: Mapped[str] = mapped_column(Text)
    atualizada_em: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Escalada(Base):
    __tablename__ = "escalada"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    conversa_id: Mapped[int] = mapped_column(
        ForeignKey("conversa.id", ondelete="CASCADE"), index=True
    )
    motivo: Mapped[str] = mapped_column(String(50))
    contexto: Mapped[str | None] = mapped_column(Text, nullable=True)
    criada_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    resolvida_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    conversa: Mapped["Conversa"] = relationship(back_populates="escaladas")
