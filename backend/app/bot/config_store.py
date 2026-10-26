"""Cached, detached view of the bot's configuration.

The API, the bot runner and the scheduler all need the current commands,
integrations and bot settings. Reading ORM objects on every inbound message
would be wasteful, so we cache an immutable snapshot and rebuild it whenever
`bot_config.config_version` changes - which every portal write bumps.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.bot.access import AccessPolicy
from app.bot.router import Router
from app.config import Settings, get_settings
from app.integrations.executor import IntegrationSpec
from app.models import BotAccessEntry, BotConfig, Command, Integration, ResponderMode
from app.security import decrypt_secret, encrypt_secret

log = logging.getLogger(__name__)

BOT_CONFIG_ID = 1


@dataclass(slots=True, frozen=True)
class BotSettingsSnapshot:
    rc_rest_url: str
    rc_ws_url: str
    bot_user_id: str | None
    bot_username: str | None
    auth_token: str | None
    password: str | None
    enabled: bool
    responder_mode: str
    greeting: str
    fallback_reply: str
    reply_in_thread: bool
    respond_in_channels: bool
    default_timezone: str
    config_version: int
    reply_mode: str = "whole"
    stream_chunk_chars: int = 24
    stream_interval_ms: int = 250

    @property
    def has_credentials(self) -> bool:
        return bool(self.auth_token or (self.bot_username and self.password))

    @classmethod
    def from_model(cls, model: BotConfig) -> BotSettingsSnapshot:
        return cls(
            rc_rest_url=model.rc_rest_url or "",
            rc_ws_url=model.rc_ws_url or "",
            bot_user_id=model.bot_user_id,
            bot_username=model.bot_username,
            auth_token=decrypt_secret(model.bot_auth_token_enc),
            password=decrypt_secret(model.bot_password_enc),
            enabled=model.enabled,
            responder_mode=model.responder_mode,
            greeting=model.greeting or "",
            fallback_reply=model.fallback_reply or "",
            reply_in_thread=model.reply_in_thread,
            respond_in_channels=model.respond_in_channels,
            default_timezone=model.default_timezone or "UTC",
            config_version=model.config_version,
            reply_mode=model.reply_mode or "whole",
            stream_chunk_chars=model.stream_chunk_chars,
            stream_interval_ms=model.stream_interval_ms,
        )


@dataclass(slots=True, frozen=True)
class ConfigSnapshot:
    settings: BotSettingsSnapshot
    router: Router
    integrations: dict[int, IntegrationSpec]
    access: AccessPolicy = AccessPolicy()

    def integration_by_name(self, name: str) -> IntegrationSpec | None:
        folded = name.casefold()
        for spec in self.integrations.values():
            if spec.name.casefold() == folded:
                return spec
        return None


async def ensure_bot_config(
    session: AsyncSession, settings: Settings | None = None
) -> BotConfig:
    """Fetch the singleton config row, seeding it from the environment once."""
    settings = settings or get_settings()
    config = await session.get(BotConfig, BOT_CONFIG_ID)
    if config is not None:
        return config

    config = BotConfig(
        id=BOT_CONFIG_ID,
        rc_rest_url=settings.rc_rest_url,
        rc_ws_url=settings.rc_ws_url,
        bot_user_id=settings.rc_bot_user_id,
        bot_username=settings.rc_bot_username,
        bot_auth_token_enc=encrypt_secret(settings.rc_bot_auth_token),
        bot_password_enc=encrypt_secret(settings.rc_bot_password),
        enabled=settings.bot_enabled,
        responder_mode=ResponderMode.rules.value,
        config_version=1,
    )
    session.add(config)
    await session.flush()
    log.info("Seeded bot_config from environment")
    return config


async def bump_config_version(session: AsyncSession) -> int:
    """Signal every cache that configuration changed."""
    config = await ensure_bot_config(session)
    config.config_version += 1
    await session.flush()
    return config.config_version


class ConfigStore:
    """Loads and caches a ConfigSnapshot, refreshing on version changes."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._snapshot: ConfigSnapshot | None = None
        self._lock = asyncio.Lock()

    async def current(self) -> ConfigSnapshot:
        """Return the cached snapshot, reloading if the DB version moved on."""
        cached = self._snapshot
        if cached is not None:
            async with self._session_factory() as session:
                version = await session.scalar(
                    select(BotConfig.config_version).where(BotConfig.id == BOT_CONFIG_ID)
                )
            if version == cached.settings.config_version:
                return cached
        return await self.reload()

    async def reload(self) -> ConfigSnapshot:
        async with self._lock:
            async with self._session_factory() as session:
                config = await ensure_bot_config(session)
                await session.commit()
                await session.refresh(config)

                commands = list(
                    (await session.scalars(select(Command).order_by(Command.priority, Command.id))).all()
                )
                integration_models = list(
                    (
                        await session.scalars(
                            select(Integration)
                            .where(Integration.enabled.is_(True))
                            .options(selectinload(Integration.params))
                        )
                    ).all()
                )
                access_usernames = frozenset(
                    (await session.scalars(select(BotAccessEntry.username))).all()
                )

            settings_snapshot = BotSettingsSnapshot.from_model(config)
            snapshot = ConfigSnapshot(
                settings=settings_snapshot,
                router=Router.from_models(
                    commands, config_version=settings_snapshot.config_version
                ),
                integrations={
                    m.id: IntegrationSpec.from_model(m) for m in integration_models
                },
                access=AccessPolicy(mode=config.access_mode, usernames=access_usernames),
            )
            self._snapshot = snapshot
            log.debug(
                "Config snapshot v%s: %d commands, %d integrations",
                settings_snapshot.config_version,
                len(snapshot.router),
                len(snapshot.integrations),
            )
            return snapshot

    def invalidate(self) -> None:
        self._snapshot = None
