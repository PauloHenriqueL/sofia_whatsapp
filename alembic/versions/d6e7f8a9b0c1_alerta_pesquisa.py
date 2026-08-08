"""Fila de alertas de pesquisa no painel

Sem time de qualidade além da Sofia, coletar as respostas e não fazer nada com
elas seria o pior dos mundos: um `qualidade_geral = 2` ou um feedback relatando
algo grave entrariam no banco e ninguém saberia. **É o alerta que transforma a
pesquisa de custo em produto.**

O template do WhatsApp sozinho não basta — ele some na rolagem. Estas três
colunas são a fila no painel:

- `alerta_pesquisa_em`: quando o alerta disparou (NULL = nenhum);
- `alerta_pesquisa_motivos`: o que disparou, em texto curto. Snapshot: a
  `Avaliacao` pode ser editada no Hamilton depois, e a fila não pode depender de
  uma chamada de API por linha;
- `alerta_resolvido_em`: quando a Thainá tratou. Soft-delete, igual à cobrança.

Revision ID: d6e7f8a9b0c1
Revises: c5d6e7f8a9b0
Create Date: 2026-08-07 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d6e7f8a9b0c1"
down_revision: Union[str, None] = "c5d6e7f8a9b0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("conversa", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("alerta_pesquisa_em", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(sa.Column("alerta_pesquisa_motivos", sa.Text(), nullable=True))
        batch_op.add_column(
            sa.Column("alerta_resolvido_em", sa.DateTime(timezone=True), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("conversa", schema=None) as batch_op:
        batch_op.drop_column("alerta_resolvido_em")
        batch_op.drop_column("alerta_pesquisa_motivos")
        batch_op.drop_column("alerta_pesquisa_em")
