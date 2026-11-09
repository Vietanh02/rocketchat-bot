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
    # A JSON object added to the requests of quick side jobs (sorting a
    # message, picking passages, indexing, summaries), e.g. to turn a
    # reasoning model's thinking off: {"chat_template_kwargs": {"enable_thinking": false}}.
    quick_extra_body: Mapped[str] = mapped_column(Text, default="")


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
    # A successful answer is reused this many seconds for the same request,
    # by commands and the model alike; 0 never. Only for read-only calls.
    cache_seconds: Mapped[int] = mapped_column(Integer, default=0)

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
    # Work that takes several steps runs as a job: each step a turn of its
    # own, with progress shown in the thread (`AgentJob`).
    jobs_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # A turn still running after this long goes on as a job; 0 never.
    job_after_seconds: Mapped[float] = mapped_column(Float, default=45.0)
    job_max_phases: Mapped[int] = mapped_column(Integer, default=6)
    # Jobs one person may have under way at once.
    jobs_per_user: Mapped[int] = mapped_column(Integer, default=2)


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


# --------------------------------------------------------------------------- #
# Knowledge base: documents uploaded on the portal or sent in a conversation
# --------------------------------------------------------------------------- #


class IntentMode(str, enum.Enum):
    # Patterns of the wording alone; instant.
    rules = "rules"
    # The patterns, and the fast model for messages they cannot tell.
    model = "model"


class KnowledgeConfig(Base, TimestampMixin):
    """How the assistant looks things up in documents (single row)."""

    __tablename__ = "knowledge_config"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    # Look up questions in the documents, and offer the kb_search tool.
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # Read documents people send in a conversation, for that conversation.
    chat_uploads: Mapped[bool] = mapped_column(Boolean, default=True)
    # Passages put in front of a question that asks to look something up.
    top_k: Mapped[int] = mapped_column(Integer, default=5)
    # A passage found only by meaning must be at least this similar (cosine).
    min_score: Mapped[float] = mapped_column(Float, default=0.3)
    # How questions are told apart from requests to do something.
    intent_mode: Mapped[str] = mapped_column(String(16), default=IntentMode.rules.value)
    # The language model groups documents into sections and writes their
    # titles, keywords and likely questions for the search index.
    llm_index: Mapped[bool] = mapped_column(Boolean, default=True)
    # The language model picks, among what search found, the passages that
    # answer the question.
    llm_judge: Mapped[bool] = mapped_column(Boolean, default=True)
    # The model for both; empty uses the fast model, else the main one.
    index_model: Mapped[str] = mapped_column(String(256), default="")
    # Answers to knowledge-base questions are saved and given again to the
    # same question (`AnswerEntry`). Pinned answers are given either way.
    answer_cache: Mapped[bool] = mapped_column(Boolean, default=True)
    # How similar (cosine) a question must be to a saved one to get its
    # answer; pinned answers have their own, usually lower, bar.
    answer_min_similarity: Mapped[float] = mapped_column(Float, default=0.95)
    pinned_min_similarity: Mapped[float] = mapped_column(Float, default=0.9)
    # How long a saved answer is given before it is worked out again.
    answer_ttl_hours: Mapped[int] = mapped_column(Integer, default=168)


class KbDocument(Base, TimestampMixin):
    """One uploaded document; its text and passages are `KbSource` and `KbPassage`.

    `scope` is "base" for the knowledge base set up on the portal, or
    "conv:<session key>" for a document someone sent in a conversation,
    which only that conversation reads.
    """

    __tablename__ = "kb_document"

    id: Mapped[int] = mapped_column(primary_key=True)
    scope: Mapped[str] = mapped_column(String(300), index=True)
    title: Mapped[str] = mapped_column(String(300))
    filename: Mapped[str] = mapped_column(String(300))
    content_type: Mapped[str] = mapped_column(String(128), default="")
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    # sha256 of the file, so the same file is not added twice to a scope.
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(16), default="ready")  # ready|error
    error: Mapped[str | None] = mapped_column(Text)
    pages: Mapped[int | None] = mapped_column(Integer)
    # Passages.
    chunks: Mapped[int] = mapped_column(Integer, default=0)
    chars: Mapped[int] = mapped_column(Integer, default=0)
    truncated: Mapped[bool] = mapped_column(Boolean, default=False)
    source: Mapped[str] = mapped_column(String(16), default="portal")  # portal|chat
    uploaded_by: Mapped[str | None] = mapped_column(String(255))


class KbSource(Base):
    """A document's whole text, which its passages are cut from, and how far along it is.

    `stage`: "queued" (waiting for the language model to index it),
    "indexing", "indexed", "basic" (cut by its headings only, because model
    indexing is off or not available) or "failed" (the model could not; the
    heading cut stays in use).
    """

    __tablename__ = "kb_source"

    document_id: Mapped[int] = mapped_column(
        ForeignKey("kb_document.id", ondelete="CASCADE"), primary_key=True
    )
    text: Mapped[str] = mapped_column(Text)
    # [[start, end, kind, page, level, header], ...]; see app.knowledge.chunking.Block.
    blocks_json: Mapped[str] = mapped_column(Text, default="[]")
    # The uploaded file, kept so the document can be read again.
    file_path: Mapped[str | None] = mapped_column(String(600))
    stage: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    stage_error: Mapped[str | None] = mapped_column(Text)
    # The model that indexed it, and windows where it fell back to headings.
    indexed_by: Mapped[str] = mapped_column(String(256), default="")
    fallback_windows: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class KbPassage(Base):
    """One passage: `text` is exactly `kb_source.text[start:end]`.

    Everything else describes it for search: its section's title, summary,
    keywords and likely questions (written by the language model, or the
    heading path when it did not index the document), and `context`, the
    header of a table it continues.
    """

    __tablename__ = "kb_passage"

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(
        ForeignKey("kb_document.id", ondelete="CASCADE"), index=True
    )
    # Copied from the document, so searches filter without a join.
    scope: Mapped[str] = mapped_column(String(300), index=True)
    ord: Mapped[int] = mapped_column(Integer)
    section_ord: Mapped[int] = mapped_column(Integer, default=0)
    start: Mapped[int] = mapped_column(Integer)
    end: Mapped[int] = mapped_column(Integer)
    page: Mapped[int | None] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    context: Mapped[str] = mapped_column(Text, default="")
    section: Mapped[str] = mapped_column(String(300), default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    # Space separated, for word search.
    keywords: Mapped[str] = mapped_column(Text, default="")
    # One per line.
    questions: Mapped[str] = mapped_column(Text, default="")
    # The embedding model its vectors were made with; "" when it has none yet.
    embedded_model: Mapped[str] = mapped_column(String(64), default="", index=True)


# Word search over passages and what describes them; kept in step by triggers.
_KB_COLUMNS = "text, section, keywords, questions, summary"
KB_FTS_DDL = (
    f"CREATE VIRTUAL TABLE IF NOT EXISTS kb_passage_fts USING fts5({_KB_COLUMNS}, "
    "content='kb_passage', content_rowid='id', tokenize='unicode61 remove_diacritics 2')",
    "CREATE TRIGGER IF NOT EXISTS kb_passage_ai AFTER INSERT ON kb_passage BEGIN "
    f"INSERT INTO kb_passage_fts(rowid, {_KB_COLUMNS}) "
    "VALUES (new.id, new.text, new.section, new.keywords, new.questions, new.summary); END",
    "CREATE TRIGGER IF NOT EXISTS kb_passage_ad AFTER DELETE ON kb_passage BEGIN "
    f"INSERT INTO kb_passage_fts(kb_passage_fts, rowid, {_KB_COLUMNS}) "
    "VALUES ('delete', old.id, old.text, old.section, old.keywords, old.questions, old.summary); END",
    f"CREATE TRIGGER IF NOT EXISTS kb_passage_au AFTER UPDATE OF {_KB_COLUMNS} ON kb_passage BEGIN "
    f"INSERT INTO kb_passage_fts(kb_passage_fts, rowid, {_KB_COLUMNS}) "
    "VALUES ('delete', old.id, old.text, old.section, old.keywords, old.questions, old.summary); "
    f"INSERT INTO kb_passage_fts(rowid, {_KB_COLUMNS}) "
    "VALUES (new.id, new.text, new.section, new.keywords, new.questions, new.summary); END",
)
for _statement in KB_FTS_DDL:
    event.listen(KbPassage.__table__, "after_create", DDL(_statement).execute_if(dialect="sqlite"))
event.listen(
    KbPassage.__table__,
    "before_drop",
    DDL("DROP TABLE IF EXISTS kb_passage_fts").execute_if(dialect="sqlite"),
)


class AnswerKind(str, enum.Enum):
    # Saved after the assistant answered from the knowledge base.
    auto = "auto"
    # Written (or kept) by an admin; given until changed or removed.
    pinned = "pinned"


class AnswerEntry(Base, TimestampMixin):
    """An answer given again, without the model, to the same question.

    Saved answers ("auto") come from turns that only used the knowledge
    base. The fast model rewrites the question to stand on its own and
    answers it from the passages alone, so the saved answer holds nothing
    of the person who asked. One is dropped when the knowledge base changes
    (`kb_version`), when it expires, or when someone reacts 👎 to it.
    """

    __tablename__ = "answer_entry"
    # Ids are never reused: a 👎 on an old reply must not reach a new entry.
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(8), default=AnswerKind.auto.value, index=True)
    question: Mapped[str] = mapped_column(Text)
    answer: Mapped[str] = mapped_column(Text)
    # JSON list of the cited sources' labels, in citation order.
    sources_json: Mapped[str] = mapped_column(Text, default="[]")
    # The documents it was answered from.
    scope: Mapped[str] = mapped_column(String(300), default="base")
    # `KnowledgeBase.base_version()` when it was saved.
    kb_version: Mapped[str] = mapped_column(String(32), default="")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    hits: Mapped[int] = mapped_column(Integer, default=0)
    last_hit_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 👎 reactions; a saved answer goes at the first, a pinned one stays.
    downvotes: Mapped[int] = mapped_column(Integer, default=0)
    created_by: Mapped[str | None] = mapped_column(String(128))

    keys: Mapped[list[AnswerKey]] = relationship(
        back_populates="entry", cascade="all, delete-orphan", order_by="AnswerKey.id"
    )


class AnswerKey(Base):
    """One way of asking an `AnswerEntry`'s question; each gets a vector."""

    __tablename__ = "answer_key"
    # Ids are vector ids too; never reused.
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(primary_key=True)
    entry_id: Mapped[int] = mapped_column(
        ForeignKey("answer_entry.id", ondelete="CASCADE"), index=True
    )
    text: Mapped[str] = mapped_column(Text)
    # sha256 of the question with case and spacing folded, for exact matches.
    text_hash: Mapped[str] = mapped_column(String(64), index=True)
    embedded_model: Mapped[str] = mapped_column(String(64), default="", index=True)

    entry: Mapped[AnswerEntry] = relationship(back_populates="keys")


class JobStatus(str, enum.Enum):
    planned = "planned"
    running = "running"
    done = "done"
    failed = "failed"
    cancelled = "cancelled"


class AgentJob(Base, TimestampMixin):
    """Work done in several steps (phases), each a turn of its own.

    Started when the model calls `start_job`, or when a turn runs past
    `AgentConfig.job_after_seconds`. Each phase's output is kept as it is
    done, so a restart goes on from the phase that was under way.
    """

    __tablename__ = "agent_job"
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(primary_key=True)
    status: Mapped[str] = mapped_column(String(16), default=JobStatus.planned.value, index=True)
    title: Mapped[str] = mapped_column(String(300))
    # What was asked, as the model was asked it.
    question: Mapped[str] = mapped_column(Text)
    # The conversation it belongs to (`app.agent.session`).
    session_key: Mapped[str] = mapped_column(String(255), index=True)
    session_kind: Mapped[str] = mapped_column(String(16))
    room_id: Mapped[str] = mapped_column(String(64))
    thread_id: Mapped[str | None] = mapped_column(String(64))
    owner_rc_user_id: Mapped[str | None] = mapped_column(String(64), index=True)
    owner_username: Mapped[str | None] = mapped_column(String(128))
    # The message that started it, to answer as it would have been answered.
    message_json: Mapped[str] = mapped_column(Text, default="{}")
    # "tool" (the model asked) or "deadline" (the turn ran long).
    started_by: Mapped[str] = mapped_column(String(16), default="tool")
    current_phase: Mapped[int] = mapped_column(Integer, default=0)
    # The final answer, as posted.
    result: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str | None] = mapped_column(Text)
    # The checklist message edited as phases finish.
    progress_msg_id: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    phases: Mapped[list[AgentJobPhase]] = relationship(
        back_populates="job", cascade="all, delete-orphan", order_by="AgentJobPhase.ord"
    )


class AgentJobPhase(Base):
    __tablename__ = "agent_job_phase"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("agent_job.id", ondelete="CASCADE"), index=True)
    ord: Mapped[int] = mapped_column(Integer)
    goal: Mapped[str] = mapped_column(Text)
    # waiting|running|done|failed
    status: Mapped[str] = mapped_column(String(16), default="waiting")
    output: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    job: Mapped[AgentJob] = relationship(back_populates="phases")
