"""reply streaming settings

Revision ID: 2bd5681624e8
Revises: b23b2df175b4
Create Date: 2026-10-20 10:31:15.173562

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2bd5681624e8'
down_revision: Union[str, Sequence[str], None] = 'b23b2df175b4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("bot_config", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("reply_mode", sa.String(length=16), nullable=False, server_default="whole")
        )
        batch_op.add_column(
            sa.Column("stream_chunk_chars", sa.Integer(), nullable=False, server_default="24")
        )
        batch_op.add_column(
            sa.Column("stream_interval_ms", sa.Integer(), nullable=False, server_default="250")
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("bot_config", schema=None) as batch_op:
        batch_op.drop_column("stream_interval_ms")
        batch_op.drop_column("stream_chunk_chars")
        batch_op.drop_column("reply_mode")
