"""Tabela midia: imagem/documento que o paciente manda (P3)

Os bytes ficam no banco porque a URL da Meta expira em minutos e o filesystem do
Render é recriado a cada deploy. CASCADE na mensagem, que já cascateia da conversa.

Revision ID: f9a0b1c2d3e4
Revises: e8f9a0b1c2d3
"""

import sqlalchemy as sa
from alembic import op

revision = "f9a0b1c2d3e4"
down_revision = "e8f9a0b1c2d3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "midia",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("mensagem_id", sa.Integer(), nullable=False),
        sa.Column("mime", sa.String(length=100), nullable=False),
        sa.Column("nome_arquivo", sa.String(length=255), nullable=True),
        sa.Column("tamanho", sa.Integer(), nullable=False),
        sa.Column("conteudo", sa.LargeBinary(), nullable=False),
        sa.Column(
            "criada_em", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["mensagem_id"], ["mensagem.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_midia_mensagem_id", "midia", ["mensagem_id"])


def downgrade() -> None:
    op.drop_index("ix_midia_mensagem_id", table_name="midia")
    op.drop_table("midia")
