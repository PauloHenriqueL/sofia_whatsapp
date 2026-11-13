"""conversa de terapeuta

Revision ID: a4c2e7b91f30
Revises: 3d097b7f20c7
Create Date: 2026-11-13 18:40:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a4c2e7b91f30'
down_revision: Union[str, None] = '3d097b7f20c7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # `server_default`: a tabela já tem linhas, e a coluna é NOT NULL. Toda
    # conversa existente nasce "não é terapeuta" e "ninguém conferiu" — é a
    # conferência do cron que marca as antigas, aos poucos.
    with op.batch_alter_table('conversa', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('eh_terapeuta', sa.Boolean(), server_default=sa.false(), nullable=False)
        )
        batch_op.add_column(
            sa.Column('contato_conferido_por', sa.String(length=10), nullable=True)
        )
        batch_op.create_index(
            batch_op.f('ix_conversa_eh_terapeuta'), ['eh_terapeuta'], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table('conversa', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_conversa_eh_terapeuta'))
        batch_op.drop_column('contato_conferido_por')
        batch_op.drop_column('eh_terapeuta')
