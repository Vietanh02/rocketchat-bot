"""agent config, sessions, transcripts and runs

Revision ID: f17a2c9e5b60
Revises: d5e8b31c7a40
Create Date: 2026-11-07 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f17a2c9e5b60'
down_revision: Union[str, Sequence[str], None] = 'd5e8b31c7a40'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    """Upgrade schema."""
    # The app creates missing tables at startup, so some may already be here.
    existing = set(sa.inspect(op.get_bind()).get_table_names())

    if "agent_config" not in existing:
        op.create_table(
            "agent_config",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("ack_reaction", sa.String(64), nullable=False, server_default=":eyes:"),
            sa.Column("fast_model", sa.String(256), nullable=False, server_default=""),
            sa.Column("turn_deadline_seconds", sa.Float(), nullable=False, server_default="60"),
            sa.Column("queue_mode", sa.String(16), nullable=False, server_default="followup"),
            sa.Column("idle_reset_minutes", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("summarize_after_messages", sa.Integer(), nullable=False, server_default="40"),
            sa.Column("parallel_tools", sa.Boolean(), nullable=False, server_default=sa.true()),
            *_timestamps(),
            sa.PrimaryKeyConstraint("id"),
        )

    if "agent_session" not in existing:
        op.create_table(
            "agent_session",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("key", sa.String(255), nullable=False),
            sa.Column("kind", sa.String(16), nullable=False),
            sa.Column("room_id", sa.String(64), nullable=False),
            sa.Column("thread_id", sa.String(64), nullable=True),
            sa.Column("owner_rc_user_id", sa.String(64), nullable=True),
            sa.Column("owner_username", sa.String(128), nullable=True),
            sa.Column("summary", sa.Text(), nullable=False, server_default=""),
            sa.Column("generation", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("last_active_at", sa.DateTime(timezone=True), nullable=False),
            *_timestamps(),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_agent_session_key", "agent_session", ["key"], unique=True)
        op.create_index("ix_agent_session_room_id", "agent_session", ["room_id"])
        op.create_index("ix_agent_session_last_active_at", "agent_session", ["last_active_at"])

    if "agent_message" not in existing:
        op.create_table(
            "agent_message",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("session_id", sa.Integer(), nullable=False),
            sa.Column("generation", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("role", sa.String(16), nullable=False),
            sa.Column("content", sa.Text(), nullable=False, server_default=""),
            sa.Column("sender_username", sa.String(128), nullable=True),
            sa.Column("compacted", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["session_id"], ["agent_session.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_agent_message_session_id", "agent_message", ["session_id"])
        op.create_index("ix_agent_message_created_at", "agent_message", ["created_at"])

    if "agent_run" not in existing:
        op.create_table(
            "agent_run",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("session_id", sa.Integer(), nullable=True),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("trigger", sa.String(16), nullable=False, server_default="chat"),
            sa.Column("status", sa.String(16), nullable=False, server_default="ok"),
            sa.Column("model", sa.String(256), nullable=False, server_default=""),
            sa.Column("rounds", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("tools_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("timings_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("ack_ms", sa.Integer(), nullable=True),
            sa.Column("ttft_ms", sa.Integer(), nullable=True),
            sa.Column("total_ms", sa.Integer(), nullable=True),
            sa.Column("overhead_ms", sa.Float(), nullable=True),
            sa.Column("error", sa.Text(), nullable=True),
            sa.ForeignKeyConstraint(["session_id"], ["agent_session.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_agent_run_session_id", "agent_run", ["session_id"])
        op.create_index("ix_agent_run_started_at", "agent_run", ["started_at"])


def downgrade() -> None:
    """Downgrade schema."""
    for table in ("agent_run", "agent_message", "agent_session", "agent_config"):
        op.drop_table(table)
