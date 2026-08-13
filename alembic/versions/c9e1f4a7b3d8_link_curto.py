"""link curto de pagamento (allos.org.br/p/xxxxxxx)

Revision ID: c9e1f4a7b3d8
Revises: a7b8c9d0e1f2
Create Date: 2026-08-13

"""

import sqlalchemy as sa
from alembic import op

revision = "c9e1f4a7b3d8"
down_revision = "a7b8c9d0e1f2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "link_curto",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("slug", sa.String(length=16), nullable=False),
        sa.Column("destino", sa.Text(), nullable=False),
        # SET NULL, não CASCADE: "Reiniciar conversa" não pode matar um link de
        # cobrança que já está no WhatsApp do paciente.
        sa.Column("conversa_id", sa.Integer(), nullable=True),
        sa.Column("cliques", sa.Integer(), server_default="0", nullable=False),
        sa.Column("ultimo_clique_em", sa.DateTime(timezone=True), nullable=True),
        sa.Column("criado_em", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["conversa_id"], ["conversa.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_link_curto_slug"), "link_curto", ["slug"], unique=True)
    op.create_index(op.f("ix_link_curto_conversa_id"), "link_curto", ["conversa_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_link_curto_conversa_id"), table_name="link_curto")
    op.drop_index(op.f("ix_link_curto_slug"), table_name="link_curto")
    op.drop_table("link_curto")
