"""mensagem programada (a Sofia manda numa data marcada)

Revision ID: b5d3f8e2a6c1
Revises: c7e1f5a20b48
Create Date: 2026-11-23
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "b5d3f8e2a6c1"
down_revision: Union[str, None] = "c7e1f5a20b48"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "mensagem_programada",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "conversa_id",
            sa.Integer(),
            sa.ForeignKey("conversa.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("texto", sa.Text(), nullable=False),
        sa.Column("enviar_em", sa.DateTime(timezone=True), nullable=False),
        sa.Column("criada_por", sa.String(50), nullable=False),
        sa.Column(
            "criada_em", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("enviada_em", sa.DateTime(timezone=True), nullable=True),
        sa.Column("enviada_como", sa.String(10), nullable=True),
        sa.Column("cancelada_em", sa.DateTime(timezone=True), nullable=True),
        sa.Column("erro", sa.String(200), nullable=True),
        sa.Column("tentativas", sa.Integer(), server_default="0", nullable=False),
        sa.Column("retomada_usada_em", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_mensagem_programada_conversa_id", "mensagem_programada", ["conversa_id"])
    op.create_index("ix_mensagem_programada_enviar_em", "mensagem_programada", ["enviar_em"])


def downgrade() -> None:
    op.drop_index("ix_mensagem_programada_enviar_em", table_name="mensagem_programada")
    op.drop_index("ix_mensagem_programada_conversa_id", table_name="mensagem_programada")
    op.drop_table("mensagem_programada")
