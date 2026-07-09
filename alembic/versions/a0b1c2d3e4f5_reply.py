"""mensagem.responde_a_id: reply a uma mensagem específica (P4)

Auto-referência em mensagem. SET NULL: se a citada sumir, a resposta continua
existindo (sem a citação).

Revision ID: a0b1c2d3e4f5
Revises: f9a0b1c2d3e4
"""

import sqlalchemy as sa
from alembic import op

revision = "a0b1c2d3e4f5"
down_revision = "f9a0b1c2d3e4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # batch_alter: o SQLite (dev) não faz ALTER TABLE com FK; o Postgres ignora.
    with op.batch_alter_table("mensagem") as batch:
        batch.add_column(sa.Column("responde_a_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_mensagem_responde_a", "mensagem", ["responde_a_id"], ["id"], ondelete="SET NULL"
        )


def downgrade() -> None:
    with op.batch_alter_table("mensagem") as batch:
        batch.drop_constraint("fk_mensagem_responde_a", type_="foreignkey")
        batch.drop_column("responde_a_id")
