"""silent access denial

Revision ID: a7d47d1e1a38
Revises: e80673efd2e1
Create Date: 2026-10-20 15:05:21.714261

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a7d47d1e1a38'
down_revision: Union[str, Sequence[str], None] = 'e80673efd2e1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Refused users are now always ignored, so the reply text has no use.
    with op.batch_alter_table("bot_config", schema=None) as batch_op:
        batch_op.drop_column("access_denied_reply")


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("bot_config", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "access_denied_reply",
                sa.Text(),
                nullable=False,
                server_default="Sorry, you are not allowed to use this bot.",
            )
        )
