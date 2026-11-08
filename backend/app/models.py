"""ORM tables backing the bot and its configuration portal."""

from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    DDL,
    Boolean,
    LargeBinary,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


# --------------------------------------------------------------------------- #
# Enums (stored as plain strings so SQLite migrations stay painless)
# --------------------------------------------------------------------------- #


class Role(str, enum.Enum):
    admin = "admin"
    viewer = "viewer"


class TriggerType(str, enum.Enum):
    # No "slash" type: Rocket.Chat treats "/word" as its own slash command and
    # never sends it on, so such a trigger could not fire.
    keyword = "keyword"  # whole message equals, starts with, or contains the pattern
    regex = "regex"      # full regex match against the message


class MatchMode(str, enum.Enum):
    exact = "exact"
    contains = "contains"
    prefix = "prefix"


class ResponseType(str, enum.Enum):
    static = "static"
    integration = "integration"
    builtin = "builtin"
    script = "script"


class AuthType(str, enum.Enum):
    none = "none"
    bearer = "bearer"
    basic = "basic"
    api_key = "api_key"


class ScheduleKind(str, enum.Enum):
    daily = "daily"
    weekly = "weekly"
    cron = "cron"
    once = "once"


class ActionType(str, enum.Enum):
    message = "message"
    integration = "integration"


class ReplyMode(str, enum.Enum):
    whole = "whole"    # post the finished reply in one message
    stream = "stream"  # post a first chunk, then edit it until the reply is complete


class AccessMode(str, enum.Enum):
    everyone = "everyone"    # anyone in the workspace may use the bot
    allowlist = "allowlist"  # only the listed users
    blocklist = "blocklist"  # everyone except the listed users


class ResponderMode(str, enum.Enum):
    rules = "rules"
    llm = "llm"  # commands first; anything unmatched goes to the language model


# --------------------------------------------------------------------------- #
# Portal access
# --------------------------------------------------------------------------- #


class PortalUser(Base, TimestampMixin):
    __tablename__ = "portal_user"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16), default=Role.admin.value)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# --------------------------------------------------------------------------- #
# Bot configuration (single row, id == 1)
# --------------------------------------------------------------------------- #


class BotConfig(Base, TimestampMixin):
    __tablename__ = "bot_config"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    rc_rest_url: Mapped[str] = mapped_column(String(512), default="")
    rc_ws_url: Mapped[str] = mapped_column(String(512), default="")
    bot_user_id: Mapped[str | None] = mapped_column(String(64))
    bot_username: Mapped[str | None] = mapped_column(String(128))
    # Fernet ciphertext; never returned by the API in plaintext.
    bot_auth_token_enc: Mapped[str | None] = mapped_column(Text)
    bot_password_enc: Mapped[str | None] = mapped_column(Text)

    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    responder_mode: Mapped[str] = mapped_column(
        String(16), default=ResponderMode.rules.value
    )
    greeting: Mapped[str] = mapped_column(
        Text, default="Hi! Send `help` to see what I can do."
    )
    fallback_reply: Mapped[str] = mapped_column(
        Text, default="Sorry, I did not understand that. Try `help`."
    )
    # Empty fallback means "stay quiet when nothing matches".
    reply_in_thread: Mapped[bool] = mapped_column(Boolean, default=False)
    # Timezone applied to reminders created from chat without an explicit tz.
    default_timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    respond_in_channels: Mapped[bool] = mapped_column(Boolean, default=True)
    # How replies appear in chat; see app/bot/streaming.py.
    reply_mode: Mapped[str] = mapped_column(String(16), default=ReplyMode.whole.value)
    stream_chunk_chars: Mapped[int] = mapped_column(Integer, default=24)
    stream_interval_ms: Mapped[int] = mapped_column(Integer, default=250)
    # Who may use the bot; the list itself lives in `bot_access_entry`.
    access_mode: Mapped[str] = mapped_column(
        String(16), default=AccessMode.everyone.value
    )
    # Config generation; bumped on every portal write so caches invalidate.
    config_version: Mapped[int] = mapped_column(Integer, default=1)


class LLMConfig(Base, TimestampMixin):
    """The language model service (single row, id == 1).

    Any OpenAI-compatible chat-completions gateway. Whether the bot uses it is
    `BotConfig.responder_mode == "llm"`; this row only says how to reach it.
    """

    __tablename__ = "llm_config"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    # Base URL of the API, up to and including the version, e.g. https://host/v1.
    base_url: Mapped[str] = mapped_column(String(512), default="")
    # Fernet ciphertext; never returned by the API in plaintext.
    api_key_enc: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str] = mapped_column(String(256), default="")
    # "{bot}" is replaced with the bot's username. Empty uses a built-in prompt.
    system_prompt: Mapped[str] = mapped_column(Text, default="")
    # How long to wait for the gateway: for the whole answer, or when
    # streaming, for the first token and between tokens.
    timeout_seconds: Mapped[float] = mapped_column(Float, default=60.0)
    max_tokens: Mapped[int | None] = mapped_column(Integer)
    temperature: Mapped[float | None] = mapped_column(Float)
    # Earlier messages of the conversation sent with each question.
    history_messages: Mapped[int] = mapped_column(Integer, default=10)
    # Rewrite the model's Markdown into what Rocket.Chat renders.
    format_for_rocketchat: Mapped[bool] = mapped_column(Boolean, default=True)


class BotAccessEntry(Base, TimestampMixin):
    """One Rocket.Chat user on the access list.

    Whether the list allows or blocks them depends on `BotConfig.access_mode`.
    """

    __tablename__ = "bot_access_entry"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Stored lower-case, without the "@".
    username: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    note: Mapped[str] = mapped_column(Text, default="")


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


class Command(Base, TimestampMixin):
    __tablename__ = "command"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    trigger_type: Mapped[str] = mapped_column(
        String(16), default=TriggerType.keyword.value
    )
    pattern: Mapped[str] = mapped_column(String(512))
    match_mode: Mapped[str] = mapped_column(String(16), default=MatchMode.exact.value)
    case_sensitive: Mapped[bool] = mapped_column(Boolean, default=False)
    priority: Mapped[int] = mapped_column(Integer, default=100)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    show_in_help: Mapped[bool] = mapped_column(Boolean, default=True)

    response_type: Mapped[str] = mapped_column(
        String(16), default=ResponseType.static.value
    )
    static_response: Mapped[str] = mapped_column(Text, default="")
    builtin_name: Mapped[str | None] = mapped_column(String(64))
    integration_id: Mapped[int | None] = mapped_column(
        ForeignKey("integration.id", ondelete="SET NULL")
    )

    # Path relative to the scripts folder (APP_SCRIPTS_DIR), e.g. "deploy/status.sh".
    script_name: Mapped[str | None] = mapped_column(String(255))
    script_timeout_seconds: Mapped[float] = mapped_column(Float, default=30.0)

    integration: Mapped[Integration | None] = relationship(back_populates="commands")


# --------------------------------------------------------------------------- #
# API integrations
# --------------------------------------------------------------------------- #


class Integration(Base, TimestampMixin):
    __tablename__ = "integration"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    base_url: Mapped[str] = mapped_column(String(512))
    method: Mapped[str] = mapped_column(String(8), default="GET")
    path_template: Mapped[str] = mapped_column(String(512), default="")
    query_template: Mapped[str] = mapped_column(Text, default="")   # JSON object
    headers_template: Mapped[str] = mapped_column(Text, default="") # JSON object
    body_template: Mapped[str] = mapped_column(Text, default="")    # raw/JSON body
    reply_template: Mapped[str] = mapped_column(Text, default="")   # Jinja over response

    auth_type: Mapped[str] = mapped_column(String(16), default=AuthType.none.value)
    auth_username: Mapped[str | None] = mapped_column(String(128))
    auth_header_name: Mapped[str | None] = mapped_column(String(64))
    auth_secret_enc: Mapped[str | None] = mapped_column(Text)

    timeout_seconds: Mapped[float] = mapped_column(Float, default=10.0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # Offered to the language model as a tool it may call on its own.
    llm_tool: Mapped[bool] = mapped_column(Boolean, default=False)

    commands: Mapped[list[Command]] = relationship(back_populates="integration")
    params: Mapped[list[IntegrationParam]] = relationship(
        back_populates="integration",
        cascade="all, delete-orphan",
        order_by="IntegrationParam.position",
    )


class IntegrationParam(Base):
    __tablename__ = "integration_param"
    __table_args__ = (UniqueConstraint("integration_id", "name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    integration_id: Mapped[int] = mapped_column(
        ForeignKey("integration.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(Text, default="")
    position: Mapped[int] = mapped_column(Integer, default=0)
    required: Mapped[bool] = mapped_column(Boolean, default=False)
    default_value: Mapped[str] = mapped_column(Text, default="")
    # When true this param swallows the rest of the message text.
    greedy: Mapped[bool] = mapped_column(Boolean, default=False)

    integration: Mapped[Integration] = relationship(back_populates="params")


# --------------------------------------------------------------------------- #
# Scheduled tasks / reminders
# --------------------------------------------------------------------------- #


class ScheduledTask(Base, TimestampMixin):
    __tablename__ = "scheduled_task"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(255))
    owner_rc_user_id: Mapped[str | None] = mapped_column(String(64), index=True)
    owner_username: Mapped[str | None] = mapped_column(String(128))
    # Where to deliver. Either a known room id, or a username for a DM.
    room_id: Mapped[str | None] = mapped_column(String(64))
    dm_username: Mapped[str | None] = mapped_column(String(128))

    schedule_kind: Mapped[str] = mapped_column(
        String(16), default=ScheduleKind.daily.value
    )
    time_of_day: Mapped[str | None] = mapped_column(String(5))  # "HH:MM"
    days_of_week: Mapped[str | None] = mapped_column(String(32))  # "mon,tue,..."
    cron_expr: Mapped[str | None] = mapped_column(String(128))
    run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    timezone_name: Mapped[str] = mapped_column(String(64), default="UTC")

    action_type: Mapped[str] = mapped_column(String(16), default=ActionType.message.value)
    message: Mapped[str] = mapped_column(Text, default="")
    integration_id: Mapped[int | None] = mapped_column(
        ForeignKey("integration.id", ondelete="SET NULL")
    )
    integration_args: Mapped[str] = mapped_column(Text, default="")  # JSON object

    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_via: Mapped[str] = mapped_column(String(16), default="portal")  # portal|chat
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_status: Mapped[str | None] = mapped_column(String(16))

    integration: Mapped[Integration | None] = relationship()


class TaskRun(Base):
    __tablename__ = "task_run"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(
        ForeignKey("scheduled_task.id", ondelete="CASCADE"), index=True
    )
    ran_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    status: Mapped[str] = mapped_column(String(16))  # ok|error|skipped
    detail: Mapped[str] = mapped_column(Text, default="")
    duration_ms: Mapped[int | None] = mapped_column(Integer)


# --------------------------------------------------------------------------- #
# Observability
# --------------------------------------------------------------------------- #


class MessageLog(Base):
    __tablename__ = "message_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    room_id: Mapped[str] = mapped_column(String(64), index=True)
    room_type: Mapped[str | None] = mapped_column(String(8))
    sender_id: Mapped[str | None] = mapped_column(String(64))
    sender_username: Mapped[str | None] = mapped_column(String(128))
    text_in: Mapped[str] = mapped_column(Text, default="")
    matched_command: Mapped[str | None] = mapped_column(String(64))
    text_out: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="ok")  # ok|error|ignored|denied
    error: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int | None] = mapped_column(Integer)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    actor: Mapped[str] = mapped_column(String(255), default="system")
    action: Mapped[str] = mapped_column(String(64))
    entity: Mapped[str] = mapped_column(String(64))
    entity_id: Mapped[str | None] = mapped_column(String(64))
    detail: Mapped[str] = mapped_column(Text, default="")


# --------------------------------------------------------------------------- #
# Agent: sessions, transcripts and runs
# --------------------------------------------------------------------------- #


class QueueMode(str, enum.Enum):
    # Messages that arrive while a session is busy get their own turn afterwards.
    followup = "followup"
    # Messages that pile up while a session is busy are merged into one turn.
    collect = "collect"


class AgentConfig(Base, TimestampMixin):
    """How the assistant runs a conversation (single row, id == 1).

    Kept apart from `llm_config`, which says how to reach the model, so that
    adding these settings needs no change to an existing table.
    """

    __tablename__ = "agent_config"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    # Reaction put on a message as soon as the model starts on it; "" for none.
    ack_reaction: Mapped[str] = mapped_column(String(64), default=":eyes:")
    # A cheaper model on the same gateway for side jobs such as summaries.
    # Empty uses the main model.
    fast_model: Mapped[str] = mapped_column(String(256), default="")
    # A turn never runs longer than this; what was said by then is posted.
    turn_deadline_seconds: Mapped[float] = mapped_column(Float, default=60.0)
    queue_mode: Mapped[str] = mapped_column(String(16), default=QueueMode.followup.value)
    # Start a fresh conversation after this long without messages; 0 never.
    idle_reset_minutes: Mapped[int] = mapped_column(Integer, default=0)
    # Older messages are folded into a summary once a conversation has more
    # than this many; 0 keeps them all (only `history_messages` are sent).
    summarize_after_messages: Mapped[int] = mapped_column(Integer, default=40)
    # Run the tools of one model round at the same time.
    parallel_tools: Mapped[bool] = mapped_column(Boolean, default=True)


class AgentSession(Base, TimestampMixin):
    """One conversation: a DM, or a channel or thread the bot talks in."""

    __tablename__ = "agent_session"

    id: Mapped[int] = mapped_column(primary_key=True)
    # e.g. "dm:<roomId>", "room:<roomId>" or "room:<roomId>:t:<tmid>".
    key: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    kind: Mapped[str] = mapped_column(String(16))  # dm|room|thread
    room_id: Mapped[str] = mapped_column(String(64), index=True)
    thread_id: Mapped[str | None] = mapped_column(String(64))
    owner_rc_user_id: Mapped[str | None] = mapped_column(String(64))
    owner_username: Mapped[str | None] = mapped_column(String(128))
    # What the model was told about messages folded away by compaction.
    summary: Mapped[str] = mapped_column(Text, default="")
    # Bumped by `new` and idle resets; messages of older generations are kept
    # for the portal but never sent to the model again.
    generation: Mapped[int] = mapped_column(Integer, default=1)
    last_active_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )


class AgentMessage(Base):
    """One message of a session's transcript."""

    __tablename__ = "agent_message"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("agent_session.id", ondelete="CASCADE"), index=True
    )
    generation: Mapped[int] = mapped_column(Integer, default=1)
    role: Mapped[str] = mapped_column(String(16))  # user|assistant
    content: Mapped[str] = mapped_column(Text, default="")
    sender_username: Mapped[str | None] = mapped_column(String(128))
    # Folded into the session summary; no longer sent as a message.
    compacted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )


class AgentRun(Base):
    """One turn of the model: how long each step took, for Logs and benchmarks."""

    __tablename__ = "agent_run"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int | None] = mapped_column(
        ForeignKey("agent_session.id", ondelete="SET NULL"), index=True
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    trigger: Mapped[str] = mapped_column(String(16), default="chat")
    status: Mapped[str] = mapped_column(String(16), default="ok")  # ok|error|timeout|merged
    model: Mapped[str] = mapped_column(String(256), default="")
    rounds: Mapped[int] = mapped_column(Integer, default=0)
    # [{"name": ..., "ms": ..., "ok": ...}, ...]
    tools_json: Mapped[str] = mapped_column(Text, default="[]")
    # Every timing mark of the turn, in milliseconds; see app.agent.timing.
    timings_json: Mapped[str] = mapped_column(Text, default="{}")
    ack_ms: Mapped[int | None] = mapped_column(Integer)
    ttft_ms: Mapped[int | None] = mapped_column(Integer)
    total_ms: Mapped[int | None] = mapped_column(Integer)
    overhead_ms: Mapped[float | None] = mapped_column(Float)
    error: Mapped[str | None] = mapped_column(Text)


# --------------------------------------------------------------------------- #
# Vector search and the assistant's memory
# --------------------------------------------------------------------------- #


class EmbeddingProvider(str, enum.Enum):
    # Nothing set up: searches match words only.
    none = ""
    # POST {base_url}/embeddings, as OpenAI and most gateways serve it.
    openai = "openai"
    # Hugging Face text-embeddings-inference: POST {base_url}/embed.
    tei = "tei"


class RerankProvider(str, enum.Enum):
    none = ""
    # text-embeddings-inference: POST {base_url}/rerank.
    tei = "tei"
    # POST {base_url}/rerank with {model, query, documents}, as Cohere, Jina
    # and LiteLLM serve it.
    cohere = "cohere"


class EmbeddingConfig(Base, TimestampMixin):
    """How text is turned into vectors, and optionally reranked (single row).

    Shared by memory now and the knowledge base later. Changing the model
    makes every stored text be embedded again.
    """

    __tablename__ = "embedding_config"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    provider: Mapped[str] = mapped_column(String(16), default=EmbeddingProvider.none.value)
    base_url: Mapped[str] = mapped_column(String(512), default="")
    api_key_enc: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str] = mapped_column(String(256), default="")
    # Asked of models that can shorten their vectors; None takes the model's own.
    dimensions: Mapped[int | None] = mapped_column(Integer)
    # Put before questions and before stored texts, for models trained that
    # way (e5: "query: " and "passage: ").
    query_prefix: Mapped[str] = mapped_column(String(64), default="")
    document_prefix: Mapped[str] = mapped_column(String(64), default="")
    timeout_seconds: Mapped[float] = mapped_column(Float, default=30.0)
    rerank_provider: Mapped[str] = mapped_column(String(16), default=RerankProvider.none.value)
    rerank_base_url: Mapped[str] = mapped_column(String(512), default="")
    rerank_api_key_enc: Mapped[str | None] = mapped_column(Text)
    rerank_model: Mapped[str] = mapped_column(String(256), default="")


class MemoryConfig(Base, TimestampMixin):
    """What the assistant remembers between conversations (single row)."""

    __tablename__ = "memory_config"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    # Offer the memory tools and show remembered notes to the model.
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # After each answer, the fast model picks facts worth keeping.
    auto_extract: Mapped[bool] = mapped_column(Boolean, default=False)
    # Notes put in front of each question, the most relevant first; 0 none.
    prompt_notes: Mapped[int] = mapped_column(Integer, default=5)
    # A note found only by meaning must be at least this similar (cosine).
    min_score: Mapped[float] = mapped_column(Float, default=0.35)


class AgentMemory(Base, TimestampMixin):
    """One note the assistant keeps across conversations.

    `scope` says who can see it: "user:<rc user id>" (that person's DMs),
    "room:<room id>" (that channel and its threads) or "global" (everyone;
    written on the portal only). It always comes from where the conversation
    happens, never from what the model asks for.
    """

    __tablename__ = "agent_memory"

    id: Mapped[int] = mapped_column(primary_key=True)
    scope: Mapped[str] = mapped_column(String(160), index=True)
    text: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(16), default="tool")  # tool|auto|portal
    # Who it was learnt from, for the portal.
    created_by: Mapped[str | None] = mapped_column(String(128))
    session_id: Mapped[int | None] = mapped_column(
        ForeignKey("agent_session.id", ondelete="SET NULL")
    )
    # The embedding model its vector was made with; "" when it has none yet.
    embedded_model: Mapped[str] = mapped_column(String(512), default="", index=True)


# Word search over notes: an FTS5 index kept in step with agent_memory by
# triggers, created and dropped with the table. Accents are ignored, so
# "nha" finds "nhà".
_MEMORY_FTS = [
    "CREATE VIRTUAL TABLE IF NOT EXISTS agent_memory_fts USING fts5("
    "text, content='agent_memory', content_rowid='id', "
    "tokenize='unicode61 remove_diacritics 2')",
    "CREATE TRIGGER IF NOT EXISTS agent_memory_ai AFTER INSERT ON agent_memory BEGIN "
    "INSERT INTO agent_memory_fts(rowid, text) VALUES (new.id, new.text); END",
    "CREATE TRIGGER IF NOT EXISTS agent_memory_ad AFTER DELETE ON agent_memory BEGIN "
    "INSERT INTO agent_memory_fts(agent_memory_fts, rowid, text) "
    "VALUES ('delete', old.id, old.text); END",
    "CREATE TRIGGER IF NOT EXISTS agent_memory_au AFTER UPDATE OF text ON agent_memory BEGIN "
    "INSERT INTO agent_memory_fts(agent_memory_fts, rowid, text) "
    "VALUES ('delete', old.id, old.text); "
    "INSERT INTO agent_memory_fts(rowid, text) VALUES (new.id, new.text); END",
]
MEMORY_FTS_DDL = tuple(_MEMORY_FTS)
for _statement in MEMORY_FTS_DDL:
    event.listen(
        AgentMemory.__table__, "after_create", DDL(_statement).execute_if(dialect="sqlite")
    )
event.listen(
    AgentMemory.__table__,
    "before_drop",
    DDL("DROP TABLE IF EXISTS agent_memory_fts").execute_if(dialect="sqlite"),
)


class AgentUserProfile(Base, TimestampMixin):
    """What the assistant knows about one Rocket.Chat user, shown in their DMs."""

    __tablename__ = "agent_user_profile"

    id: Mapped[int] = mapped_column(primary_key=True)
    rc_user_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    username: Mapped[str] = mapped_column(String(128), default="", index=True)
    # How they like to be called.
    display_name: Mapped[str] = mapped_column(String(128), default="")
    # The language to answer in, e.g. "Vietnamese".
    language: Mapped[str] = mapped_column(String(64), default="")
    # IANA name, e.g. "Asia/Ho_Chi_Minh"; "" uses the bot's default.
    timezone: Mapped[str] = mapped_column(String(64), default="")
    # Free text: role, team, how they like answers.
    about: Mapped[str] = mapped_column(Text, default="")


class EmbeddingCache(Base):
    """Vectors already computed, so the same text is never embedded twice."""

    __tablename__ = "embedding_cache"

    # sha256 of the model id and the exact text sent.
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    model: Mapped[str] = mapped_column(String(512), index=True)
    # float32, little-endian.
    vector: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
