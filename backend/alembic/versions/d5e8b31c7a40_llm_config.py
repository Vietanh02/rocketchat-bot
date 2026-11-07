"""llm config table

Revision ID: d5e8b31c7a40
Revises: c41f0a9d2e57
Create Date: 2026-11-06 19:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd5e8b31c7a40'
down_revision: Union[str, Sequence[str], None] = 'c41f0a9d2e57'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # The app creates missing tables at startup, so it may already be here.
    if sa.inspect(op.get_bind()).has_table("llm_config"):
        return
    op.create_table(
        "llm_config",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("base_url", sa.String(length=512), nullable=False, server_default=""),
        sa.Column("api_key_enc", sa.Text(), nullable=True),
        sa.Column("model", sa.String(length=256), nullable=False, server_default=""),
        sa.Column("system_prompt", sa.Text(), nullable=False, server_default=""),
        sa.Column("timeout_seconds", sa.Float(), nullable=False, server_default="60"),
        sa.Column("max_tokens", sa.Integer(), nullable=True),
        sa.Column("temperature", sa.Float(), nullable=True),
        sa.Column("history_messages", sa.Integer(), nullable=False, server_default="10"),
        sa.Column(
            "format_for_rocketchat", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("llm_config")
