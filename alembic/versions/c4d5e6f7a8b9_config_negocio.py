"""Tabela configuracao (valores de negócio editáveis no painel)

Revision ID: c4d5e6f7a8b9
Revises: f3a9c1d2e4b7
Create Date: 2026-06-24 11:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4d5e6f7a8b9"
down_revision: Union[str, None] = "f3a9c1d2e4b7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "configuracao",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("chave", sa.String(length=50), nullable=False),
        sa.Column("valor", sa.String(length=100), nullable=False),
        sa.Column(
            "atualizada_em",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("configuracao", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_configuracao_chave"), ["chave"], unique=True
        )


def downgrade() -> None:
    with op.batch_alter_table("configuracao", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_configuracao_chave"))
    op.drop_table("configuracao")
