"""Aviso único pós-escalada e pesquisa de satisfação em andamento

- `aviso_escalada_em`: marca que a Sofia já respondeu, uma vez, a quem escreveu
  depois da conversa ter sido escalada. Sem isso ela ficava totalmente muda em
  modo humano e a pessoa escrevia no vazio até alguém abrir o painel.
- `pesquisa_avaliacao_id` / `pesquisa_iniciada_em`: pesquisa de satisfação em
  curso nesta conversa (a `Avaliacao` correspondente vive no Hamilton) e quando
  ela começou, que é a base do lembrete e do encerramento por prazo.

Revision ID: b1c2d3e4f5a6
Revises: a0b1c2d3e4f5
Create Date: 2026-08-06 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b1c2d3e4f5a6"
down_revision: Union[str, None] = "a0b1c2d3e4f5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("conversa", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("aviso_escalada_em", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(sa.Column("pesquisa_avaliacao_id", sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column("pesquisa_iniciada_em", sa.DateTime(timezone=True), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("conversa", schema=None) as batch_op:
        batch_op.drop_column("pesquisa_iniciada_em")
        batch_op.drop_column("pesquisa_avaliacao_id")
        batch_op.drop_column("aviso_escalada_em")
