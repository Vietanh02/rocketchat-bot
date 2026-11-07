"""integration llm tool flag

Revision ID: c41f0a9d2e57
Revises: 7aedcf6e5707
Create Date: 2026-10-27 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c41f0a9d2e57'
down_revision: Union[str, Sequence[str], None] = '7aedcf6e5707'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("integration", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("llm_tool", sa.Boolean(), nullable=False, server_default=sa.false())
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("integration", schema=None) as batch_op:
        batch_op.drop_column("llm_tool")
