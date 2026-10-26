"""script commands

Revision ID: e80673efd2e1
Revises: 3b3831be5032
Create Date: 2026-10-20 11:36:41.538924

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e80673efd2e1'
down_revision: Union[str, Sequence[str], None] = '3b3831be5032'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("command", schema=None) as batch_op:
        batch_op.add_column(sa.Column("script_name", sa.String(length=255), nullable=True))
        batch_op.add_column(
            sa.Column("script_timeout_seconds", sa.Float(), nullable=False, server_default="30")
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("command", schema=None) as batch_op:
        batch_op.drop_column("script_timeout_seconds")
        batch_op.drop_column("script_name")
