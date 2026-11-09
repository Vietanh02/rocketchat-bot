"""agent jobs: work done in several steps, and their settings

Revision ID: f3a8d5b2c614
Revises: e6b3c9d1f027
Create Date: 2026-11-09 05:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f3a8d5b2c614'
down_revision: Union[str, Sequence[str], None] = 'e6b3c9d1f027'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

AGENT_COLUMNS = (
    ("jobs_enabled", sa.Boolean(), sa.true()),
    ("job_after_seconds", sa.Float(), "45"),
    ("job_max_phases", sa.Integer(), "6"),
    ("jobs_per_user", sa.Integer(), "2"),
)


def upgrade() -> None:
    """Upgrade schema."""
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if "agent_config" in tables:
        have = {c["name"] for c in inspector.get_columns("agent_config")}
        for name, type_, default in AGENT_COLUMNS:
            if name not in have:
                op.add_column("agent_config", sa.Column(name, type_, nullable=False, server_default=default))

    if "agent_job" not in tables:
        op.create_table(
            "agent_job",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(16), nullable=False),
            sa.Column("title", sa.String(300), nullable=False),
            sa.Column("question", sa.Text(), nullable=False),
            sa.Column("session_key", sa.String(255), nullable=False),
            sa.Column("session_kind", sa.String(16), nullable=False),
            sa.Column("room_id", sa.String(64), nullable=False),
            sa.Column("thread_id", sa.String(64), nullable=True),
            sa.Column("owner_rc_user_id", sa.String(64), nullable=True),
            sa.Column("owner_username", sa.String(128), nullable=True),
            sa.Column("message_json", sa.Text(), nullable=False),
            sa.Column("started_by", sa.String(16), nullable=False),
            sa.Column("current_phase", sa.Integer(), nullable=False),
            sa.Column("result", sa.Text(), nullable=False),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("progress_msg_id", sa.String(64), nullable=True),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sqlite_autoincrement=True,
        )
        op.create_index("ix_agent_job_status", "agent_job", ["status"])
        op.create_index("ix_agent_job_session_key", "agent_job", ["session_key"])
        op.create_index("ix_agent_job_owner_rc_user_id", "agent_job", ["owner_rc_user_id"])
    if "agent_job_phase" not in tables:
        op.create_table(
            "agent_job_phase",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("job_id", sa.Integer(), nullable=False),
            sa.Column("ord", sa.Integer(), nullable=False),
            sa.Column("goal", sa.Text(), nullable=False),
            sa.Column("status", sa.String(16), nullable=False),
            sa.Column("output", sa.Text(), nullable=False),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("attempts", sa.Integer(), nullable=False),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(["job_id"], ["agent_job.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_agent_job_phase_job_id", "agent_job_phase", ["job_id"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("agent_job_phase")
    op.drop_table("agent_job")
    with op.batch_alter_table("agent_config") as batch:
        for name, _, _ in reversed(AGENT_COLUMNS):
            batch.drop_column(name)
