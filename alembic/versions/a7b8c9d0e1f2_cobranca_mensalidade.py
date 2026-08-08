"""cobranca da mensalidade encadeada na primeira sessao (Demanda D)

Revision ID: a7b8c9d0e1f2
Revises: fd757db2682f
Create Date: 2026-08-08 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a7b8c9d0e1f2'
down_revision: Union[str, None] = 'fd757db2682f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Sem backfill de propósito: as quatro colunas nascem NULL, então nenhum
    # paciente já cadastrado entra na fila da cobrança quando isto subir. Mesmo
    # raciocínio do `cadastrado_em` (migration c5d6e7f8a9b0) — uma coluna nova
    # preenchida retroativamente viraria uma enxurrada de mensagens no primeiro
    # tick do cron.
    with op.batch_alter_table('conversa', schema=None) as batch_op:
        batch_op.add_column(sa.Column('cobranca_iniciada_em', sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column('cobranca_encerrada_em', sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column('cobranca_lembrete_em', sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column('cobranca_status', sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('conversa', schema=None) as batch_op:
        batch_op.drop_column('cobranca_status')
        batch_op.drop_column('cobranca_lembrete_em')
        batch_op.drop_column('cobranca_encerrada_em')
        batch_op.drop_column('cobranca_iniciada_em')
