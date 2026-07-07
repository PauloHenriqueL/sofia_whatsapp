"""configuracao.valor -> Text (pra caber os prompts editáveis no painel)

Revision ID: e8f9a0b1c2d3
Revises: d7e8f9a0b1c2
Create Date: 2026-07-07 12:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e8f9a0b1c2d3"
down_revision: Union[str, None] = "d7e8f9a0b1c2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("configuracao", schema=None) as batch_op:
        batch_op.alter_column(
            "valor",
            existing_type=sa.String(length=100),
            type_=sa.Text(),
            existing_nullable=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("configuracao", schema=None) as batch_op:
        batch_op.alter_column(
            "valor",
            existing_type=sa.Text(),
            type_=sa.String(length=100),
            existing_nullable=False,
        )
