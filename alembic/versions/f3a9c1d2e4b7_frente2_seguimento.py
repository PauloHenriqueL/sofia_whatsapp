"""Frente 2: coluna seguimento_enviado_em em conversa

Revision ID: f3a9c1d2e4b7
Revises: 058b58ebedb9
Create Date: 2026-06-24 10:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f3a9c1d2e4b7"
down_revision: Union[str, None] = "058b58ebedb9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("conversa", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("seguimento_enviado_em", sa.DateTime(timezone=True), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("conversa", schema=None) as batch_op:
        batch_op.drop_column("seguimento_enviado_em")
