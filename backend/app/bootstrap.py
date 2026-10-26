"""First-run setup: schema, admin account, and the default command set."""

from __future__ import annotations

import logging
from pathlib import Path

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot.config_store import ensure_bot_config
from app.config import Settings, get_settings
from app.db import Base, engine
from app.models import Command, MatchMode, PortalUser, ResponseType, Role, TriggerType
from app.security import hash_password

log = logging.getLogger(__name__)

# Shipped so a fresh install can already answer `help`. Admins can edit,
# re-trigger or disable any of them from the portal.
DEFAULT_COMMANDS = [
    {
        "name": "help",
        "description": "List everything I can do",
        "trigger_type": TriggerType.keyword.value,
        "pattern": "help",
        "match_mode": MatchMode.exact.value,
        "priority": 10,
        "response_type": ResponseType.builtin.value,
        "builtin_name": "help",
    },
    {
        "name": "status",
        "description": "Show my connection and reminder counts",
        "trigger_type": TriggerType.keyword.value,
        "pattern": "status",
        "match_mode": MatchMode.exact.value,
        "priority": 20,
        "response_type": ResponseType.builtin.value,
        "builtin_name": "status",
    },
    {
        "name": "remind",
        "description": "Set a reminder, e.g. remind daily 09:00 stand-up",
        "trigger_type": TriggerType.keyword.value,
        "pattern": "remind",
        "match_mode": MatchMode.prefix.value,
        "priority": 30,
        "response_type": ResponseType.builtin.value,
        "builtin_name": "remind",
    },
    {
        "name": "tasks",
        "description": "List or change your reminders",
        "trigger_type": TriggerType.keyword.value,
        "pattern": "tasks",
        "match_mode": MatchMode.prefix.value,
        "priority": 40,
        "response_type": ResponseType.builtin.value,
        "builtin_name": "tasks",
    },
]


ALEMBIC_DIR = Path(__file__).resolve().parents[1] / "alembic"


def _alembic_config() -> Config:
    config = Config()
    config.set_main_option("script_location", str(ALEMBIC_DIR))
    config.set_main_option("sqlalchemy.url", get_settings().database_url)
    return config


def _stamp_head(connection) -> None:
    """Record the current revision for a schema we just created ourselves.

    Without this, `alembic upgrade head` on a fresh install would try to
    create tables that already exist.
    """
    context = MigrationContext.configure(connection)
    if context.get_current_revision() is not None:
        return
    script = ScriptDirectory.from_config(_alembic_config())
    head = script.get_current_head()
    if head is not None:
        context.stamp(script, head)
        log.info("Stamped the database at Alembic revision %s", head)


async def create_schema() -> None:
    """Create any missing tables so a fresh checkout runs with no extra step.

    Alembic owns schema *changes* from here on; run `alembic upgrade head`
    after pulling a release that adds a migration.
    """
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.run_sync(_stamp_head)


async def ensure_admin(session: AsyncSession, settings: Settings) -> None:
    existing = await session.scalar(select(func.count()).select_from(PortalUser))
    if existing:
        return
    email = (settings.bootstrap_admin_email or "").strip().lower()
    password = settings.bootstrap_admin_password
    if not (email and password):
        log.warning(
            "No portal users exist and APP_BOOTSTRAP_ADMIN_EMAIL/PASSWORD are "
            "unset - nobody can log in to the portal yet"
        )
        return
    session.add(
        PortalUser(
            email=email,
            password_hash=hash_password(password),
            role=Role.admin.value,
        )
    )
    log.info("Created the first portal admin: %s", email)


async def seed_default_commands(session: AsyncSession) -> int:
    """Insert the default commands that are missing, by name."""
    existing = set(
        (await session.scalars(select(Command.name))).all()
    )
    added = 0
    for spec in DEFAULT_COMMANDS:
        if spec["name"] in existing:
            continue
        session.add(Command(**spec))
        added += 1
    if added:
        log.info("Seeded %d default command(s)", added)
    return added


async def run_bootstrap(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings | None = None,
) -> None:
    settings = settings or get_settings()
    await create_schema()
    async with session_factory() as session:
        await ensure_bot_config(session, settings)
        await ensure_admin(session, settings)
        await seed_default_commands(session)
        await session.commit()
