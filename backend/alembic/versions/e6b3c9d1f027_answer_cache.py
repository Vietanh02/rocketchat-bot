"""answer cache: saved and pinned answers, cache settings, integration caching

Revision ID: e6b3c9d1f027
Revises: d2f7a4c8e915
Create Date: 2026-11-09 04:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e6b3c9d1f027'
down_revision: Union[str, Sequence[str], None] = 'd2f7a4c8e915'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

KNOWLEDGE_COLUMNS = (
    ("answer_cache", sa.Boolean(), sa.true()),
    ("answer_min_similarity", sa.Float(), "0.95"),
    ("pinned_min_similarity", sa.Float(), "0.9"),
    ("answer_ttl_hours", sa.Integer(), "168"),
)


def _columns(inspector, table: str) -> set[str]:
    return {c["name"] for c in inspector.get_columns(table)}


def upgrade() -> None:
    """Upgrade schema."""
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())

    if "knowledge_config" in tables:
        have = _columns(inspector, "knowledge_config")
        for name, type_, default in KNOWLEDGE_COLUMNS:
            if name not in have:
                op.add_column(
                    "knowledge_config",
                    sa.Column(name, type_, nullable=False, server_default=default),
                )
    if "integration" in tables and "cache_seconds" not in _columns(inspector, "integration"):
        op.add_column(
            "integration",
            sa.Column("cache_seconds", sa.Integer(), nullable=False, server_default="0"),
        )

    if "answer_entry" not in tables:
        op.create_table(
            "answer_entry",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("kind", sa.String(8), nullable=False),
            sa.Column("question", sa.Text(), nullable=False),
            sa.Column("answer", sa.Text(), nullable=False),
            sa.Column("sources_json", sa.Text(), nullable=False),
            sa.Column("scope", sa.String(300), nullable=False),
            sa.Column("kb_version", sa.String(32), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("hits", sa.Integer(), nullable=False),
            sa.Column("last_hit_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("downvotes", sa.Integer(), nullable=False),
            sa.Column("created_by", sa.String(128), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sqlite_autoincrement=True,
        )
        op.create_index("ix_answer_entry_kind", "answer_entry", ["kind"])
    if "answer_key" not in tables:
        op.create_table(
            "answer_key",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("entry_id", sa.Integer(), nullable=False),
            sa.Column("text", sa.Text(), nullable=False),
            sa.Column("text_hash", sa.String(64), nullable=False),
            sa.Column("embedded_model", sa.String(64), nullable=False),
            sa.ForeignKeyConstraint(["entry_id"], ["answer_entry.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sqlite_autoincrement=True,
        )
        op.create_index("ix_answer_key_entry_id", "answer_key", ["entry_id"])
        op.create_index("ix_answer_key_text_hash", "answer_key", ["text_hash"])
        op.create_index("ix_answer_key_embedded_model", "answer_key", ["embedded_model"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("answer_key")
    op.drop_table("answer_entry")
    with op.batch_alter_table("integration") as batch:
        batch.drop_column("cache_seconds")
    with op.batch_alter_table("knowledge_config") as batch:
        for name, _, _ in reversed(KNOWLEDGE_COLUMNS):
            batch.drop_column(name)
