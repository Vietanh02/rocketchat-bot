"""knowledge base: settings, documents and passages

Revision ID: b8e2f4a6c913
Revises: a3c9d7e1f402
Create Date: 2026-11-08 16:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b8e2f4a6c913'
down_revision: Union[str, Sequence[str], None] = 'a3c9d7e1f402'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Kept in step with app.models.KB_FTS_DDL; repeated so this revision does not
# change if the models do.
KB_FTS_DDL = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS kb_chunk_fts USING fts5("
    "text, heading, content='kb_chunk', content_rowid='id', "
    "tokenize='unicode61 remove_diacritics 2')",
    "CREATE TRIGGER IF NOT EXISTS kb_chunk_ai AFTER INSERT ON kb_chunk BEGIN "
    "INSERT INTO kb_chunk_fts(rowid, text, heading) VALUES (new.id, new.text, new.heading); END",
    "CREATE TRIGGER IF NOT EXISTS kb_chunk_ad AFTER DELETE ON kb_chunk BEGIN "
    "INSERT INTO kb_chunk_fts(kb_chunk_fts, rowid, text, heading) "
    "VALUES ('delete', old.id, old.text, old.heading); END",
    "CREATE TRIGGER IF NOT EXISTS kb_chunk_au AFTER UPDATE OF text, heading ON kb_chunk BEGIN "
    "INSERT INTO kb_chunk_fts(kb_chunk_fts, rowid, text, heading) "
    "VALUES ('delete', old.id, old.text, old.heading); "
    "INSERT INTO kb_chunk_fts(rowid, text, heading) VALUES (new.id, new.text, new.heading); END",
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

    if "knowledge_config" not in existing:
        op.create_table(
            "knowledge_config",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("chat_uploads", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("top_k", sa.Integer(), nullable=False, server_default="5"),
            sa.Column("min_score", sa.Float(), nullable=False, server_default="0.3"),
            sa.Column("intent_mode", sa.String(16), nullable=False, server_default="rules"),
            *_timestamps(),
            sa.PrimaryKeyConstraint("id"),
        )

    if "kb_document" not in existing:
        op.create_table(
            "kb_document",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("scope", sa.String(300), nullable=False),
            sa.Column("title", sa.String(300), nullable=False),
            sa.Column("filename", sa.String(300), nullable=False),
            sa.Column("content_type", sa.String(128), nullable=False, server_default=""),
            sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("content_hash", sa.String(64), nullable=False),
            sa.Column("status", sa.String(16), nullable=False, server_default="ready"),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("pages", sa.Integer(), nullable=True),
            sa.Column("chunks", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("chars", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("truncated", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("source", sa.String(16), nullable=False, server_default="portal"),
            sa.Column("uploaded_by", sa.String(255), nullable=True),
            *_timestamps(),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_kb_document_scope", "kb_document", ["scope"])
        op.create_index("ix_kb_document_content_hash", "kb_document", ["content_hash"])

    if "kb_chunk" not in existing:
        op.create_table(
            "kb_chunk",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("document_id", sa.Integer(), nullable=False),
            sa.Column("scope", sa.String(300), nullable=False),
            sa.Column("ord", sa.Integer(), nullable=False),
            sa.Column("text", sa.Text(), nullable=False),
            sa.Column("heading", sa.String(300), nullable=False, server_default=""),
            sa.Column("page", sa.Integer(), nullable=True),
            sa.Column("embedded_model", sa.String(64), nullable=False, server_default=""),
            sa.ForeignKeyConstraint(["document_id"], ["kb_document.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_kb_chunk_document_id", "kb_chunk", ["document_id"])
        op.create_index("ix_kb_chunk_scope", "kb_chunk", ["scope"])
        op.create_index("ix_kb_chunk_embedded_model", "kb_chunk", ["embedded_model"])
    if op.get_bind().dialect.name == "sqlite":
        for statement in KB_FTS_DDL:
            op.execute(statement)


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP TABLE IF EXISTS kb_chunk_fts")
    for table in ("kb_chunk", "kb_document", "knowledge_config"):
        op.drop_table(table)
