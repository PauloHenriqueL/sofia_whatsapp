"""Aviso único pós-escalada e pesquisa de satisfação em andamento

- `aviso_escalada_em`: marca que a Sofia já respondeu, uma vez, a quem escreveu
  depois da conversa ter sido escalada. Sem isso ela ficava totalmente muda em
  modo humano e a pessoa escrevia no vazio até alguém abrir o painel.
- `pesquisa_avaliacao_id` / `pesquisa_iniciada_em`: pesquisa de satisfação em
  curso nesta conversa (a `Avaliacao` correspondente vive no Hamilton) e quando
  ela começou, que é a base do lembrete e do encerramento por prazo.

Revision ID: b1c2d3e4f5a6
Revises: c3d4e5f6a7b8
Create Date: 2026-08-06 00:00:00.000000

Nota: esta migration nasceu apontando para `a0b1c2d3e4f5`, mas a `main` andou
antes do merge e criou duas outras a partir da mesma revisão (arquivamento e
stripe_ref). Duas cabeças fariam `alembic upgrade head` falhar no deploy, então
ela foi reencadeada depois da última — as colunas são independentes, a ordem
entre elas não importa.

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b1c2d3e4f5a6"
down_revision: Union[str, None] = "c3d4e5f6a7b8"
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
