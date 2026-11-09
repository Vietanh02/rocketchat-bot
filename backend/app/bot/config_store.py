"""Cached, detached view of the bot's configuration.

The API, the bot runner and the scheduler all need the current commands,
integrations and bot settings. Reading ORM objects on every inbound message
would be wasteful, so we cache an immutable snapshot and rebuild it whenever
`bot_config.config_version` changes - which every portal write bumps.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.bot.access import AccessPolicy
from app.bot.router import Router
from app.config import Settings, get_settings
from app.integrations.executor import IntegrationSpec
from app.agent.policy import ToolPolicySettings
from app.integrations.mcp import McpServerSpec, McpToolSpec
from app.models import (
    AgentConfig,
    BotAccessEntry,
    McpServer,
    McpTool,
    ToolPolicy,
    EmbeddingConfig,
    KnowledgeConfig,
    MemoryConfig,
    BotConfig,
    Command,
    Integration,
    LLMConfig,
    ResponderMode,
)
from app.security import decrypt_secret, encrypt_secret

log = logging.getLogger(__name__)

BOT_CONFIG_ID = 1
LLM_CONFIG_ID = 1
AGENT_CONFIG_ID = 1
EMBEDDING_CONFIG_ID = 1
MEMORY_CONFIG_ID = 1
KNOWLEDGE_CONFIG_ID = 1
# How long a snapshot is used before its version is compared with the database.
VERSION_CHECK_SECONDS = 1.0


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
class LLMSettings:
    """How to reach the language model; see `app.models.LLMConfig`."""

    base_url: str = ""
    api_key: str | None = None
    model: str = ""
    system_prompt: str = ""
    timeout_seconds: float = 60.0
    max_tokens: int | None = None
    temperature: float | None = None
    history_messages: int = 10
    format_for_rocketchat: bool = True
    # JSON text; see `app.models.LLMConfig.quick_extra_body`.
    quick_extra_body: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)

    @classmethod
    def from_model(cls, model: LLMConfig) -> LLMSettings:
        return cls(
            base_url=(model.base_url or "").strip(),
            api_key=decrypt_secret(model.api_key_enc),
            model=(model.model or "").strip(),
            system_prompt=model.system_prompt or "",
            timeout_seconds=model.timeout_seconds,
            max_tokens=model.max_tokens,
            temperature=model.temperature,
            history_messages=model.history_messages,
            format_for_rocketchat=model.format_for_rocketchat,
            quick_extra_body=(model.quick_extra_body or "").strip(),
        )


@dataclass(slots=True, frozen=True)
class AgentSettings:
    """How conversations are run; see `app.models.AgentConfig`."""

    ack_reaction: str = ":eyes:"
    fast_model: str = ""
    turn_deadline_seconds: float = 60.0
    queue_mode: str = "followup"
    idle_reset_minutes: int = 0
    summarize_after_messages: int = 40
    parallel_tools: bool = True
    jobs_enabled: bool = True
    job_after_seconds: float = 45.0
    job_max_phases: int = 6
    jobs_per_user: int = 2

    @classmethod
    def from_model(cls, model: AgentConfig) -> AgentSettings:
        return cls(
            ack_reaction=(model.ack_reaction or "").strip(),
            fast_model=(model.fast_model or "").strip(),
            turn_deadline_seconds=model.turn_deadline_seconds,
            queue_mode=model.queue_mode,
            idle_reset_minutes=model.idle_reset_minutes,
            summarize_after_messages=model.summarize_after_messages,
            parallel_tools=model.parallel_tools,
            jobs_enabled=model.jobs_enabled,
            job_after_seconds=model.job_after_seconds,
            job_max_phases=model.job_max_phases,
            jobs_per_user=model.jobs_per_user,
        )


@dataclass(slots=True, frozen=True)
class EmbeddingSettings:
    """How to embed and rerank text; see `app.models.EmbeddingConfig`."""

    provider: str = ""
    base_url: str = ""
    api_key: str | None = None
    model: str = ""
    dimensions: int | None = None
    query_prefix: str = ""
    document_prefix: str = ""
    timeout_seconds: float = 30.0
    rerank_provider: str = ""
    rerank_base_url: str = ""
    rerank_api_key: str | None = None
    rerank_model: str = ""

    @property
    def configured(self) -> bool:
        # A local text-embeddings-inference server needs no key and no model.
        if self.provider == "tei":
            return bool(self.base_url)
        return bool(self.provider and self.base_url and self.model)

    @property
    def rerank_configured(self) -> bool:
        if self.rerank_provider == "tei":
            return bool(self.rerank_base_url)
        return bool(self.rerank_provider and self.rerank_base_url and self.rerank_model)

    @property
    def model_id(self) -> str:
        """Names the vectors this produces: equal ids, comparable vectors."""
        if not self.configured:
            return ""
        return "|".join(
            [
                self.provider,
                self.base_url.rstrip("/"),
                self.model,
                str(self.dimensions or ""),
                self.query_prefix,
                self.document_prefix,
            ]
        )

    @classmethod
    def from_model(cls, model: EmbeddingConfig) -> EmbeddingSettings:
        return cls(
            provider=(model.provider or "").strip(),
            base_url=(model.base_url or "").strip(),
            api_key=decrypt_secret(model.api_key_enc),
            model=(model.model or "").strip(),
            dimensions=model.dimensions,
            query_prefix=model.query_prefix or "",
            document_prefix=model.document_prefix or "",
            timeout_seconds=model.timeout_seconds,
            rerank_provider=(model.rerank_provider or "").strip(),
            rerank_base_url=(model.rerank_base_url or "").strip(),
            rerank_api_key=decrypt_secret(model.rerank_api_key_enc),
            rerank_model=(model.rerank_model or "").strip(),
        )


@dataclass(slots=True, frozen=True)
class MemorySettings:
    """What the assistant remembers; see `app.models.MemoryConfig`."""

    enabled: bool = True
    auto_extract: bool = False
    prompt_notes: int = 5
    min_score: float = 0.35

    @classmethod
    def from_model(cls, model: MemoryConfig) -> MemorySettings:
        return cls(
            enabled=model.enabled,
            auto_extract=model.auto_extract,
            prompt_notes=model.prompt_notes,
            min_score=model.min_score,
        )


@dataclass(slots=True, frozen=True)
class KnowledgeSettings:
    """How documents are looked up; see `app.models.KnowledgeConfig`."""

    enabled: bool = True
    chat_uploads: bool = True
    top_k: int = 5
    min_score: float = 0.3
    intent_mode: str = "rules"
    llm_index: bool = True
    llm_judge: bool = True
    index_model: str = ""
    answer_cache: bool = True
    answer_min_similarity: float = 0.95
    pinned_min_similarity: float = 0.9
    answer_ttl_hours: int = 168

    @classmethod
    def from_model(cls, model: KnowledgeConfig) -> KnowledgeSettings:
        return cls(
            enabled=model.enabled,
            chat_uploads=model.chat_uploads,
            top_k=model.top_k,
            min_score=model.min_score,
            intent_mode=model.intent_mode,
            llm_index=model.llm_index,
            llm_judge=model.llm_judge,
            index_model=(model.index_model or "").strip(),
            answer_cache=model.answer_cache,
            answer_min_similarity=model.answer_min_similarity,
            pinned_min_similarity=model.pinned_min_similarity,
            answer_ttl_hours=model.answer_ttl_hours,
        )


@dataclass(slots=True, frozen=True)
class ConfigSnapshot:
    settings: BotSettingsSnapshot
    router: Router
    integrations: dict[int, IntegrationSpec]
    access: AccessPolicy = AccessPolicy()
    llm: LLMSettings = LLMSettings()
    agent: AgentSettings = AgentSettings()
    embedding: EmbeddingSettings = EmbeddingSettings()
    memory: MemorySettings = MemorySettings()
    knowledge: KnowledgeSettings = KnowledgeSettings()
    # Who may use which tools.
    policy: ToolPolicySettings = ToolPolicySettings()
    # Enabled tools of enabled MCP servers.
    mcp_tools: tuple[McpToolSpec, ...] = ()

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


async def ensure_llm_config(
    session: AsyncSession, settings: Settings | None = None
) -> LLMConfig:
    """Fetch the singleton LLM row, seeding it from APP_LLM_* once."""
    settings = settings or get_settings()
    config = await session.get(LLMConfig, LLM_CONFIG_ID)
    if config is not None:
        return config

    config = LLMConfig(
        id=LLM_CONFIG_ID,
        base_url=settings.llm_base_url,
        api_key_enc=encrypt_secret(settings.llm_api_key or None),
        model=settings.llm_model,
        system_prompt=settings.llm_system_prompt,
        timeout_seconds=settings.llm_timeout_seconds,
        max_tokens=settings.llm_max_tokens,
        temperature=settings.llm_temperature,
        history_messages=settings.llm_history_messages,
        format_for_rocketchat=True,
    )
    session.add(config)
    await session.flush()
    if settings.llm_configured:
        log.info("Seeded llm_config from environment")
    return config


async def ensure_agent_config(session: AsyncSession) -> AgentConfig:
    """Fetch the singleton agent row, creating it with the defaults once."""
    config = await session.get(AgentConfig, AGENT_CONFIG_ID)
    if config is None:
        config = AgentConfig(id=AGENT_CONFIG_ID)
        session.add(config)
        await session.flush()
    return config


async def ensure_embedding_config(session: AsyncSession) -> EmbeddingConfig:
    """Fetch the singleton embedding row, creating it (nothing set up) once."""
    config = await session.get(EmbeddingConfig, EMBEDDING_CONFIG_ID)
    if config is None:
        config = EmbeddingConfig(id=EMBEDDING_CONFIG_ID)
        session.add(config)
        await session.flush()
    return config


async def ensure_memory_config(session: AsyncSession) -> MemoryConfig:
    """Fetch the singleton memory row, creating it with the defaults once."""
    config = await session.get(MemoryConfig, MEMORY_CONFIG_ID)
    if config is None:
        config = MemoryConfig(id=MEMORY_CONFIG_ID)
        session.add(config)
        await session.flush()
    return config


async def ensure_knowledge_config(session: AsyncSession) -> KnowledgeConfig:
    """Fetch the singleton knowledge row, creating it with the defaults once."""
    config = await session.get(KnowledgeConfig, KNOWLEDGE_CONFIG_ID)
    if config is None:
        config = KnowledgeConfig(id=KNOWLEDGE_CONFIG_ID)
        session.add(config)
        await session.flush()
    return config


async def ensure_tool_policy(session: AsyncSession) -> ToolPolicy:
    """Fetch the singleton tool policy row, creating it with the defaults once."""
    config = await session.get(ToolPolicy, 1)
    if config is None:
        config = ToolPolicy(id=1)
        session.add(config)
        await session.flush()
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
        # time.monotonic() until which the cached version is trusted unread.
        self._checked_until = 0.0

    async def current(self) -> ConfigSnapshot:
        """Return the cached snapshot, reloading if the DB version moved on.

        Portal writes call `invalidate`, so the version is read back at most
        every VERSION_CHECK_SECONDS, as a safety net, not on every message.
        """
        cached = self._snapshot
        if cached is not None:
            now = time.monotonic()
            if now < self._checked_until:
                return cached
            async with self._session_factory() as session:
                version = await session.scalar(
                    select(BotConfig.config_version).where(BotConfig.id == BOT_CONFIG_ID)
                )
            if version == cached.settings.config_version:
                self._checked_until = now + VERSION_CHECK_SECONDS
                return cached
        return await self.reload()

    async def reload(self) -> ConfigSnapshot:
        async with self._lock:
            async with self._session_factory() as session:
                config = await ensure_bot_config(session)
                llm_config = await ensure_llm_config(session)
                agent_config = await ensure_agent_config(session)
                embedding_config = await ensure_embedding_config(session)
                memory_config = await ensure_memory_config(session)
                knowledge_config = await ensure_knowledge_config(session)
                policy_config = await ensure_tool_policy(session)
                await session.commit()
                await session.refresh(config)
                await session.refresh(llm_config)
                await session.refresh(agent_config)
                await session.refresh(embedding_config)
                await session.refresh(memory_config)
                await session.refresh(knowledge_config)
                await session.refresh(policy_config)
                mcp_rows = (
                    await session.execute(
                        select(McpTool, McpServer)
                        .join(McpServer, McpServer.id == McpTool.server_id)
                        .where(McpTool.enabled.is_(True), McpServer.enabled.is_(True))
                        .order_by(McpServer.name, McpTool.name)
                    )
                ).all()
                servers = {server.id: McpServerSpec.from_model(server) for _, server in mcp_rows}
                mcp_tools = tuple(
                    McpToolSpec(servers[server.id], tool.name, tool.description, tool.input_schema_json)
                    for tool, server in mcp_rows
                )

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
                llm=LLMSettings.from_model(llm_config),
                agent=AgentSettings.from_model(agent_config),
                embedding=EmbeddingSettings.from_model(embedding_config),
                memory=MemorySettings.from_model(memory_config),
                knowledge=KnowledgeSettings.from_model(knowledge_config),
                policy=ToolPolicySettings.from_model(policy_config),
                mcp_tools=mcp_tools,
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
        self._checked_until = 0.0
