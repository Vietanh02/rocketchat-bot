"""llm_config.quick_extra_body: extra request fields for quick side jobs

Revision ID: d2f7a4c8e915
Revises: c5d1e9f3a720
Create Date: 2026-11-09 03:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd2f7a4c8e915'
down_revision: Union[str, Sequence[str], None] = 'c5d1e9f3a720'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    inspector = sa.inspect(op.get_bind())
    if "llm_config" not in inspector.get_table_names():
        return
    columns = {c["name"] for c in inspector.get_columns("llm_config")}
    if "quick_extra_body" not in columns:
        op.add_column(
            "llm_config",
            sa.Column("quick_extra_body", sa.Text(), nullable=False, server_default=""),
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("llm_config") as batch:
        batch.drop_column("quick_extra_body")
