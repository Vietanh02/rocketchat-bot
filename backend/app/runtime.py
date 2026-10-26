"""The long-lived objects shared by the API, the bot and the scheduler."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot.config_store import ConfigStore
from app.bot.runner import BotService
from app.scheduler.service import SchedulerService

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Runtime:
    session_factory: async_sessionmaker[AsyncSession]
    config_store: ConfigStore
    scheduler: SchedulerService
    bot: BotService

    @classmethod
    def build(cls, session_factory: async_sessionmaker[AsyncSession]) -> Runtime:
        config_store = ConfigStore(session_factory)
        scheduler = SchedulerService(session_factory, config_store)
        bot = BotService(session_factory, config_store, scheduler)
        return cls(session_factory, config_store, scheduler, bot)

    async def start(self) -> None:
        await self.scheduler.start()
        await self.bot.start()

    async def stop(self) -> None:
        await self.bot.stop()
        await self.scheduler.shutdown()

    async def apply_config_change(self, *, reconnect: bool = False) -> None:
        """Called after a portal write so running components pick it up."""
        self.config_store.invalidate()
        await self.config_store.current()
        if reconnect:
            await self.bot.restart()
