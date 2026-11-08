"""memory notes, user profiles, embedding settings and cache

Revision ID: a3c9d7e1f402
Revises: f17a2c9e5b60
Create Date: 2026-11-08 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a3c9d7e1f402'
down_revision: Union[str, Sequence[str], None] = 'f17a2c9e5b60'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Kept in step with app.models.MEMORY_FTS_DDL; repeated so this revision does
# not change if the models do.
MEMORY_FTS_DDL = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS agent_memory_fts USING fts5("
    "text, content='agent_memory', content_rowid='id', "
    "tokenize='unicode61 remove_diacritics 2')",
    "CREATE TRIGGER IF NOT EXISTS agent_memory_ai AFTER INSERT ON agent_memory BEGIN "
    "INSERT INTO agent_memory_fts(rowid, text) VALUES (new.id, new.text); END",
    "CREATE TRIGGER IF NOT EXISTS agent_memory_ad AFTER DELETE ON agent_memory BEGIN "
    "INSERT INTO agent_memory_fts(agent_memory_fts, rowid, text) "
    "VALUES ('delete', old.id, old.text); END",
    "CREATE TRIGGER IF NOT EXISTS agent_memory_au AFTER UPDATE OF text ON agent_memory BEGIN "
    "INSERT INTO agent_memory_fts(agent_memory_fts, rowid, text) "
    "VALUES ('delete', old.id, old.text); "
    "INSERT INTO agent_memory_fts(rowid, text) VALUES (new.id, new.text); END",
)


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    """Upgrade schema."""
    # The app creates missing tables at startup, so some may already be here.
    existing = set(sa.inspect(op.get_bind()).get_table_names())

    if "embedding_config" not in existing:
        op.create_table(
            "embedding_config",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("provider", sa.String(16), nullable=False, server_default=""),
            sa.Column("base_url", sa.String(512), nullable=False, server_default=""),
            sa.Column("api_key_enc", sa.Text(), nullable=True),
            sa.Column("model", sa.String(256), nullable=False, server_default=""),
            sa.Column("dimensions", sa.Integer(), nullable=True),
            sa.Column("query_prefix", sa.String(64), nullable=False, server_default=""),
            sa.Column("document_prefix", sa.String(64), nullable=False, server_default=""),
            sa.Column("timeout_seconds", sa.Float(), nullable=False, server_default="30"),
            sa.Column("rerank_provider", sa.String(16), nullable=False, server_default=""),
            sa.Column("rerank_base_url", sa.String(512), nullable=False, server_default=""),
            sa.Column("rerank_api_key_enc", sa.Text(), nullable=True),
            sa.Column("rerank_model", sa.String(256), nullable=False, server_default=""),
            *_timestamps(),
            sa.PrimaryKeyConstraint("id"),
        )

    if "memory_config" not in existing:
        op.create_table(
            "memory_config",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("auto_extract", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("prompt_notes", sa.Integer(), nullable=False, server_default="5"),
            sa.Column("min_score", sa.Float(), nullable=False, server_default="0.35"),
            *_timestamps(),
            sa.PrimaryKeyConstraint("id"),
        )

    if "agent_memory" not in existing:
        op.create_table(
            "agent_memory",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("scope", sa.String(160), nullable=False),
            sa.Column("text", sa.Text(), nullable=False),
            sa.Column("source", sa.String(16), nullable=False, server_default="tool"),
            sa.Column("created_by", sa.String(128), nullable=True),
            sa.Column("session_id", sa.Integer(), nullable=True),
            sa.Column("embedded_model", sa.String(512), nullable=False, server_default=""),
            *_timestamps(),
            sa.ForeignKeyConstraint(["session_id"], ["agent_session.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_agent_memory_scope", "agent_memory", ["scope"])
        op.create_index("ix_agent_memory_embedded_model", "agent_memory", ["embedded_model"])
    if op.get_bind().dialect.name == "sqlite":
        for statement in MEMORY_FTS_DDL:
            op.execute(statement)

    if "agent_user_profile" not in existing:
        op.create_table(
            "agent_user_profile",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("rc_user_id", sa.String(64), nullable=False),
            sa.Column("username", sa.String(128), nullable=False, server_default=""),
            sa.Column("display_name", sa.String(128), nullable=False, server_default=""),
            sa.Column("language", sa.String(64), nullable=False, server_default=""),
            sa.Column("timezone", sa.String(64), nullable=False, server_default=""),
            sa.Column("about", sa.Text(), nullable=False, server_default=""),
            *_timestamps(),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_agent_user_profile_rc_user_id", "agent_user_profile", ["rc_user_id"], unique=True
        )
        op.create_index("ix_agent_user_profile_username", "agent_user_profile", ["username"])

    if "embedding_cache" not in existing:
        op.create_table(
            "embedding_cache",
            sa.Column("key", sa.String(64), nullable=False),
            sa.Column("model", sa.String(512), nullable=False),
            sa.Column("vector", sa.LargeBinary(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("key"),
        )
        op.create_index("ix_embedding_cache_model", "embedding_cache", ["model"])
        op.create_index("ix_embedding_cache_created_at", "embedding_cache", ["created_at"])


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP TABLE IF EXISTS agent_memory_fts")
    for table in (
        "embedding_cache",
        "agent_user_profile",
        "agent_memory",
        "memory_config",
        "embedding_config",
    ):
        op.drop_table(table)
