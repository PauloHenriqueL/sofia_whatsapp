"""Modelos SQLAlchemy: Conversa, Mensagem, Escalada.

Esquema conforme sofia_briefing.md. JSON portável (JSONB em Postgres,
JSON em SQLite) e timestamps com timezone.
"""

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, Text, func
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
