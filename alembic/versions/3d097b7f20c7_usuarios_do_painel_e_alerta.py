"""usuarios do painel e alerta

Revision ID: 3d097b7f20c7
Revises: c9e1f4a7b3d8
Create Date: 2026-08-26 11:55:59.418199

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3d097b7f20c7'
down_revision: Union[str, None] = 'c9e1f4a7b3d8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'usuario',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('nome', sa.String(length=100), nullable=False),
        sa.Column('username', sa.String(length=50), nullable=False),
        sa.Column('senha_hash', sa.String(length=255), nullable=False),
        sa.Column('telefone_whatsapp', sa.String(length=20), nullable=True),
        sa.Column('recebe_alertas', sa.Boolean(), nullable=False),
        sa.Column('ativo', sa.Boolean(), nullable=False),
        sa.Column(
            'criado_em', sa.DateTime(timezone=True),
            server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False,
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('usuario', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_usuario_username'), ['username'], unique=True)


def downgrade() -> None:
    with op.batch_alter_table('usuario', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_usuario_username'))
    op.drop_table('usuario')
