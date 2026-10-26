"""keyword triggers only

Revision ID: 7aedcf6e5707
Revises: a7d47d1e1a38
Create Date: 2026-10-20 16:17:43.909563

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7aedcf6e5707'
down_revision: Union[str, Sequence[str], None] = 'a7d47d1e1a38'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Rocket.Chat swallows "/word" messages as its own slash commands, so slash
    # triggers never fired. They become keywords that take arguments; a typed
    # leading slash is still tolerated by keyword matching.
    op.execute(
        """
        UPDATE command
        SET trigger_type = 'keyword',
            match_mode = 'prefix',
            description = REPLACE(description, '/' || LTRIM(pattern, '/'), LTRIM(pattern, '/')),
            pattern = LTRIM(pattern, '/')
        WHERE trigger_type = 'slash'
        """
    )
    # Scripts take arguments, which only "starts with" leaves room for.
    op.execute(
        """
        UPDATE command SET match_mode = 'prefix'
        WHERE response_type = 'script' AND trigger_type = 'keyword'
        """
    )


def downgrade() -> None:
    """Downgrade schema."""
    # Data-only change; which commands were slash triggers is not recorded.
