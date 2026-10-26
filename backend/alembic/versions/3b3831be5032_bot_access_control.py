"""bot access control

Revision ID: 3b3831be5032
Revises: 2bd5681624e8
Create Date: 2026-10-20 10:45:42.342368

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3b3831be5032'
down_revision: Union[str, Sequence[str], None] = '2bd5681624e8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "bot_access_entry",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("username", sa.String(length=128), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("bot_access_entry", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_bot_access_entry_username"), ["username"], unique=True
        )

    with op.batch_alter_table("bot_config", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("access_mode", sa.String(length=16), nullable=False, server_default="everyone")
        )
        batch_op.add_column(
            sa.Column(
                "access_denied_reply",
                sa.Text(),
                nullable=False,
                server_default="Sorry, you are not allowed to use this bot.",
            )
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("bot_config", schema=None) as batch_op:
        batch_op.drop_column("access_denied_reply")
        batch_op.drop_column("access_mode")

    with op.batch_alter_table("bot_access_entry", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_bot_access_entry_username"))
    op.drop_table("bot_access_entry")
