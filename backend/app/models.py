"""ORM tables backing the bot and its configuration portal."""

from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
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
    llm = "llm"  # reserved: no implementation ships yet


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
