"""MCP: kept-open sessions per server; per tool, who gets it, its hints and caching

Revision ID: c5d2a7e9f041
Revises: b9e4f1a6d283
Create Date: 2026-11-12 08:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c5d2a7e9f041'
down_revision: Union[str, Sequence[str], None] = 'b9e4f1a6d283'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SERVER_COLUMNS = (
    ("keep_alive_seconds", sa.Integer(), "300"),
)
TOOL_COLUMNS = (
    ("everyone", sa.Boolean(), sa.false()),
    ("read_only", sa.Boolean(), sa.false()),
    ("destructive", sa.Boolean(), sa.false()),
    ("cache_seconds", sa.Integer(), "0"),
)


def _add(inspector, table: str, columns) -> None:
    if table not in inspector.get_table_names():
        return
    have = {c["name"] for c in inspector.get_columns(table)}
    for name, kind, default in columns:
        if name not in have:
            op.add_column(table, sa.Column(name, kind, nullable=False, server_default=default))


def upgrade() -> None:
    """Upgrade schema."""
    inspector = sa.inspect(op.get_bind())
    _add(inspector, "mcp_server", SERVER_COLUMNS)
    _add(inspector, "mcp_tool", TOOL_COLUMNS)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("mcp_tool") as batch:
        for name, _, _ in TOOL_COLUMNS:
            batch.drop_column(name)
    with op.batch_alter_table("mcp_server") as batch:
        for name, _, _ in SERVER_COLUMNS:
            batch.drop_column(name)
