"""tool policy, MCP servers and their tools, skill states

Revision ID: a4c7e2f9b318
Revises: f3a8d5b2c614
Create Date: 2026-11-09 06:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a4c7e2f9b318'
down_revision: Union[str, Sequence[str], None] = 'f3a8d5b2c614'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _stamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    """Upgrade schema."""
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "tool_policy" not in tables:
        op.create_table(
            "tool_policy",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("trusted_usernames", sa.Text(), nullable=False),
            sa.Column("safe_allow", sa.Text(), nullable=False),
            sa.Column("safe_deny", sa.Text(), nullable=False),
            sa.Column("trusted_allow", sa.Text(), nullable=False),
            sa.Column("trusted_deny", sa.Text(), nullable=False),
            *_stamps(),
            sa.PrimaryKeyConstraint("id"),
        )
    if "mcp_server" not in tables:
        op.create_table(
            "mcp_server",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("name", sa.String(64), nullable=False),
            sa.Column("description", sa.Text(), nullable=False),
            sa.Column("transport", sa.String(8), nullable=False),
            sa.Column("url", sa.String(512), nullable=False),
            sa.Column("headers_enc", sa.Text(), nullable=True),
            sa.Column("command", sa.String(512), nullable=False),
            sa.Column("args_json", sa.Text(), nullable=False),
            sa.Column("env_enc", sa.Text(), nullable=True),
            sa.Column("timeout_seconds", sa.Float(), nullable=False),
            sa.Column("enabled", sa.Boolean(), nullable=False),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("tools_refreshed_at", sa.DateTime(timezone=True), nullable=True),
            *_stamps(),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_mcp_server_name", "mcp_server", ["name"], unique=True)
    if "mcp_tool" not in tables:
        op.create_table(
            "mcp_tool",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("server_id", sa.Integer(), nullable=False),
            sa.Column("name", sa.String(128), nullable=False),
            sa.Column("description", sa.Text(), nullable=False),
            sa.Column("input_schema_json", sa.Text(), nullable=False),
            sa.Column("enabled", sa.Boolean(), nullable=False),
            sa.ForeignKeyConstraint(["server_id"], ["mcp_server.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_mcp_tool_server_id", "mcp_tool", ["server_id"])
    if "skill_state" not in tables:
        op.create_table(
            "skill_state",
            sa.Column("name", sa.String(128), nullable=False),
            sa.Column("enabled", sa.Boolean(), nullable=False),
            *_stamps(),
            sa.PrimaryKeyConstraint("name"),
        )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("skill_state")
    op.drop_table("mcp_tool")
    op.drop_table("mcp_server")
    op.drop_table("tool_policy")
