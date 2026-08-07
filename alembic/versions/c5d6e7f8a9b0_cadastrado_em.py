"""Marca quando o cadastro no Hamilton deu certo (âncora da pesquisa de entrada)

A pesquisa de linha de base é o único ponto de medida ANTES do tratamento — sem
ela não existe par pré/pós, e o ORS sozinho não significa nada. Ela dispara a
partir de 3h depois do cadastro e desiste depois de 5 dias, então precisa de um
carimbo de quando o cadastro aconteceu. Nenhuma coluna existente serve:
`atualizada_em` muda a cada mensagem e `criada_em` é o início da conversa.

**Sem backfill, de propósito.** Nascendo NULL, nenhuma das conversas já
cadastradas entra na fila da pesquisa de entrada quando isto subir — só cadastros
feitos depois do deploy. É o que impede a estreia da feature de virar um disparo
em massa pra base inteira.

Revision ID: c5d6e7f8a9b0
Revises: b1c2d3e4f5a6
Create Date: 2026-08-07 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c5d6e7f8a9b0"
down_revision: Union[str, None] = "b1c2d3e4f5a6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("conversa", schema=None) as batch_op:
        batch_op.add_column(sa.Column("cadastrado_em", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("conversa", schema=None) as batch_op:
        batch_op.drop_column("cadastrado_em")
