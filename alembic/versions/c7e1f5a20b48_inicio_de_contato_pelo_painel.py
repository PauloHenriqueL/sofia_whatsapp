"""início de contato pelo painel (Demanda G)

Revision ID: c7e1f5a20b48
Revises: a4c2e7b91f30
Create Date: 2026-11-16
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "c7e1f5a20b48"
down_revision: Union[str, None] = "a4c2e7b91f30"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("conversa") as batch:
        batch.add_column(sa.Column("contato_iniciado_em", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("contato_iniciado_por", sa.String(50), nullable=True))
        batch.add_column(sa.Column("contato_respondido_em", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("contato_consentimento_por", sa.String(50), nullable=True))
        batch.add_column(sa.Column("contato_nota", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("conversa") as batch:
        batch.drop_column("contato_nota")
        batch.drop_column("contato_consentimento_por")
        batch.drop_column("contato_respondido_em")
        batch.drop_column("contato_iniciado_por")
        batch.drop_column("contato_iniciado_em")
