"""knowledge base: documents imported from folders (collection, source path)

Revision ID: b9e4f1a6d283
Revises: a4c7e2f9b318
Create Date: 2026-11-09 08:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b9e4f1a6d283'
down_revision: Union[str, Sequence[str], None] = 'a4c7e2f9b318'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    inspector = sa.inspect(op.get_bind())
    if "kb_document" not in inspector.get_table_names():
        return
    have = {c["name"] for c in inspector.get_columns("kb_document")}
    if "collection" not in have:
        op.add_column("kb_document", sa.Column("collection", sa.String(100), nullable=True))
        op.create_index("ix_kb_document_collection", "kb_document", ["collection"])
    if "source_path" not in have:
        op.add_column("kb_document", sa.Column("source_path", sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_kb_document_collection", table_name="kb_document")
    with op.batch_alter_table("kb_document") as batch:
        batch.drop_column("source_path")
        batch.drop_column("collection")
