"""knowledge base: whole texts and exact passages, model indexing settings

Replaces kb_chunk (passages rewritten from the text) with kb_source (each
document's whole text) and kb_passage (exact slices of it, with what the
language model wrote about their section). Documents of the old table have
no whole text to be cut from again, so their passages are dropped; they
need uploading once more.

Revision ID: c5d1e9f3a720
Revises: b8e2f4a6c913
Create Date: 2026-11-08 19:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c5d1e9f3a720'
down_revision: Union[str, Sequence[str], None] = 'b8e2f4a6c913'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Kept in step with app.models.KB_FTS_DDL; repeated so this revision does not
# change if the models do.
_COLUMNS = "text, section, keywords, questions, summary"
KB_FTS_DDL = (
    f"CREATE VIRTUAL TABLE IF NOT EXISTS kb_passage_fts USING fts5({_COLUMNS}, "
    "content='kb_passage', content_rowid='id', tokenize='unicode61 remove_diacritics 2')",
    "CREATE TRIGGER IF NOT EXISTS kb_passage_ai AFTER INSERT ON kb_passage BEGIN "
    f"INSERT INTO kb_passage_fts(rowid, {_COLUMNS}) "
    "VALUES (new.id, new.text, new.section, new.keywords, new.questions, new.summary); END",
    "CREATE TRIGGER IF NOT EXISTS kb_passage_ad AFTER DELETE ON kb_passage BEGIN "
    f"INSERT INTO kb_passage_fts(kb_passage_fts, rowid, {_COLUMNS}) "
    "VALUES ('delete', old.id, old.text, old.section, old.keywords, old.questions, old.summary); END",
    f"CREATE TRIGGER IF NOT EXISTS kb_passage_au AFTER UPDATE OF {_COLUMNS} ON kb_passage BEGIN "
    f"INSERT INTO kb_passage_fts(kb_passage_fts, rowid, {_COLUMNS}) "
    "VALUES ('delete', old.id, old.text, old.section, old.keywords, old.questions, old.summary); "
    f"INSERT INTO kb_passage_fts(rowid, {_COLUMNS}) "
    "VALUES (new.id, new.text, new.section, new.keywords, new.questions, new.summary); END",
)


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = set(inspector.get_table_names())

    columns = {c["name"] for c in inspector.get_columns("knowledge_config")} if "knowledge_config" in existing else set()
    if "knowledge_config" in existing:
        if "llm_index" not in columns:
            op.add_column("knowledge_config", sa.Column("llm_index", sa.Boolean(), nullable=False, server_default=sa.true()))
        if "llm_judge" not in columns:
            op.add_column("knowledge_config", sa.Column("llm_judge", sa.Boolean(), nullable=False, server_default=sa.true()))
        if "index_model" not in columns:
            op.add_column("knowledge_config", sa.Column("index_model", sa.String(256), nullable=False, server_default=""))

    if "kb_source" not in existing:
        op.create_table(
            "kb_source",
            sa.Column("document_id", sa.Integer(), nullable=False),
            sa.Column("text", sa.Text(), nullable=False),
            sa.Column("blocks_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("file_path", sa.String(600), nullable=True),
            sa.Column("stage", sa.String(16), nullable=False, server_default="queued"),
            sa.Column("stage_error", sa.Text(), nullable=True),
            sa.Column("indexed_by", sa.String(256), nullable=False, server_default=""),
            sa.Column("fallback_windows", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["document_id"], ["kb_document.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("document_id"),
        )
        op.create_index("ix_kb_source_stage", "kb_source", ["stage"])

    if "kb_passage" not in existing:
        op.create_table(
            "kb_passage",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("document_id", sa.Integer(), nullable=False),
            sa.Column("scope", sa.String(300), nullable=False),
            sa.Column("ord", sa.Integer(), nullable=False),
            sa.Column("section_ord", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("start", sa.Integer(), nullable=False),
            sa.Column("end", sa.Integer(), nullable=False),
            sa.Column("page", sa.Integer(), nullable=True),
            sa.Column("text", sa.Text(), nullable=False),
            sa.Column("context", sa.Text(), nullable=False, server_default=""),
            sa.Column("section", sa.String(300), nullable=False, server_default=""),
            sa.Column("summary", sa.Text(), nullable=False, server_default=""),
            sa.Column("keywords", sa.Text(), nullable=False, server_default=""),
            sa.Column("questions", sa.Text(), nullable=False, server_default=""),
            sa.Column("embedded_model", sa.String(64), nullable=False, server_default=""),
            sa.ForeignKeyConstraint(["document_id"], ["kb_document.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_kb_passage_document_id", "kb_passage", ["document_id"])
        op.create_index("ix_kb_passage_scope", "kb_passage", ["scope"])
        op.create_index("ix_kb_passage_embedded_model", "kb_passage", ["embedded_model"])
    if bind.dialect.name == "sqlite":
        for statement in KB_FTS_DDL:
            op.execute(statement)

    if "kb_chunk" in existing:
        # Old documents have no whole text to cut exact passages from.
        op.execute("DROP TABLE IF EXISTS kb_chunk_fts")
        op.drop_table("kb_chunk")
        op.execute("DELETE FROM kb_document WHERE id NOT IN (SELECT document_id FROM kb_source)")


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP TABLE IF EXISTS kb_passage_fts")
    op.drop_table("kb_passage")
    op.drop_table("kb_source")
    with op.batch_alter_table("knowledge_config") as batch:
        batch.drop_column("index_model")
        batch.drop_column("llm_judge")
        batch.drop_column("llm_index")
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
