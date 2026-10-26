"""Pydantic models for the portal API.

Secrets are write-only: clients send `auth_secret`, and read back only
`has_secret`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    EmailStr,
    Field,
    field_validator,
)

from app.models import (
    ActionType,
    AuthType,
    MatchMode,
    ResponderMode,
    ResponseType,
    ScheduleKind,
    TriggerType,
)

ORM = ConfigDict(from_attributes=True)


def _attach_utc(value: Any) -> Any:
    """SQLite returns naive datetimes; every stored instant is UTC."""
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


UtcDateTime = Annotated[datetime, BeforeValidator(_attach_utc)]


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)


class TokenResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int
    email: str
    role: str


class UserOut(BaseModel):
    model_config = ORM
    id: int
    email: str
    role: str
    is_active: bool
    last_login_at: UtcDateTime | None = None


class PasswordChange(BaseModel):
    current_password: str = Field(min_length=1)
    new_password: str = Field(min_length=8, max_length=256)


class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=256)
    role: Literal["admin", "viewer"] = "admin"


# --------------------------------------------------------------------------- #
# Bot configuration
# --------------------------------------------------------------------------- #


class BotConfigOut(BaseModel):
    model_config = ORM
    rc_rest_url: str
    rc_ws_url: str
    bot_user_id: str | None = None
    bot_username: str | None = None
    enabled: bool
    responder_mode: str
    greeting: str
    fallback_reply: str
    reply_in_thread: bool
    respond_in_channels: bool
    default_timezone: str
    reply_mode: str = "whole"
    stream_chunk_chars: int = 24
    stream_interval_ms: int = 250
    access_mode: str = "everyone"
    config_version: int
    has_auth_token: bool = False
    has_password: bool = False


class BotConfigUpdate(BaseModel):
    rc_rest_url: str | None = Field(default=None, max_length=512)
    rc_ws_url: str | None = Field(default=None, max_length=512)
    bot_user_id: str | None = Field(default=None, max_length=64)
    bot_username: str | None = Field(default=None, max_length=128)
    # Write-only. "" clears the stored secret; omit to leave it unchanged.
    bot_auth_token: str | None = None
    bot_password: str | None = None
    enabled: bool | None = None
    responder_mode: Literal["rules", "llm"] | None = None
    greeting: str | None = None
    fallback_reply: str | None = None
    reply_in_thread: bool | None = None
    respond_in_channels: bool | None = None
    default_timezone: str | None = Field(default=None, max_length=64)
    reply_mode: Literal["whole", "stream"] | None = None
    # Characters revealed per edit, and the pause between edits.
    stream_chunk_chars: int | None = Field(default=None, ge=1, le=2000)
    stream_interval_ms: int | None = Field(default=None, ge=50, le=5000)
    access_mode: Literal["everyone", "allowlist", "blocklist"] | None = None

    @field_validator("responder_mode")
    @classmethod
    def _rules_only(cls, value: str | None) -> str | None:
        if value == ResponderMode.llm.value:
            raise ValueError(
                "the LLM responder is not implemented yet; use 'rules'"
            )
        return value


class AccessEntryOut(BaseModel):
    model_config = ORM
    id: int
    username: str
    note: str
    created_at: UtcDateTime


class AccessEntryCreate(BaseModel):
    # Several at once is fine: "@an, bo\ncuong" adds three users.
    usernames: str = Field(min_length=1, max_length=20000)
    note: str = Field(default="", max_length=500)


class AccessEntryUpdate(BaseModel):
    note: str = Field(max_length=500)


class AccessCheckResult(BaseModel):
    username: str
    allowed: bool
    reason: str


class ConnectionStatusOut(BaseModel):
    configured: bool
    connected: bool
    bot_user_id: str | None = None
    bot_username: str | None = None
    rest_url: str | None = None
    ws_url: str | None = None
    last_error: str | None = None
    messages_handled: int = 0
    scheduler_running: bool = False
    commands_loaded: int = 0
    integrations_loaded: int = 0
    tasks_scheduled: int = 0
    details: dict[str, str] = Field(default_factory=dict)


class ConnectionTestRequest(BaseModel):
    """Verify credentials without saving them; omit fields to use the stored ones."""

    rc_rest_url: str | None = None
    bot_user_id: str | None = None
    bot_auth_token: str | None = None
    bot_username: str | None = None
    bot_password: str | None = None


class ConnectionTestResult(BaseModel):
    ok: bool
    message: str
    bot_user_id: str | None = None
    bot_username: str | None = None


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


class CommandBase(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    description: str = ""
    trigger_type: Literal["keyword", "regex"] = TriggerType.keyword.value
    pattern: str = Field(min_length=1, max_length=512)
    match_mode: Literal["exact", "contains", "prefix"] = MatchMode.exact.value
    case_sensitive: bool = False
    priority: int = Field(default=100, ge=0, le=10_000)
    enabled: bool = True
    show_in_help: bool = True
    response_type: Literal["static", "integration", "builtin", "script"] = (
        ResponseType.static.value
    )
    static_response: str = ""
    builtin_name: str | None = Field(default=None, max_length=64)
    integration_id: int | None = None
    script_name: str | None = Field(default=None, max_length=255)
    script_timeout_seconds: float = Field(default=30.0, ge=1, le=300)


class CommandCreate(CommandBase):
    pass


class CommandUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    description: str | None = None
    trigger_type: Literal["keyword", "regex"] | None = None
    pattern: str | None = Field(default=None, min_length=1, max_length=512)
    match_mode: Literal["exact", "contains", "prefix"] | None = None
    case_sensitive: bool | None = None
    priority: int | None = Field(default=None, ge=0, le=10_000)
    enabled: bool | None = None
    show_in_help: bool | None = None
    response_type: Literal["static", "integration", "builtin", "script"] | None = None
    static_response: str | None = None
    builtin_name: str | None = Field(default=None, max_length=64)
    integration_id: int | None = None
    script_name: str | None = Field(default=None, max_length=255)
    script_timeout_seconds: float | None = Field(default=None, ge=1, le=300)


class CommandOut(CommandBase):
    model_config = ORM
    id: int
    created_at: UtcDateTime
    updated_at: UtcDateTime


class ScriptOut(BaseModel):
    name: str
    runner: str


class ScriptListOut(BaseModel):
    directory: str
    scripts: list[ScriptOut]


class CommandTestRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    username: str = "portal-tester"
    room_type: Literal["d", "c", "p"] = "d"
    dry_run: bool = True


class CommandTestResult(BaseModel):
    matched: bool
    command_name: str | None = None
    args: str = ""
    reply: str | None = None
    error: str | None = None


# --------------------------------------------------------------------------- #
# Integrations
# --------------------------------------------------------------------------- #


class IntegrationParamIn(BaseModel):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    description: str = ""
    required: bool = False
    default_value: str = ""
    greedy: bool = False


class IntegrationParamOut(IntegrationParamIn):
    model_config = ORM
    id: int
    position: int


class IntegrationBase(BaseModel):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    description: str = ""
    base_url: str = Field(min_length=1, max_length=512)
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"] = "GET"
    path_template: str = ""
    query_template: str = ""
    headers_template: str = ""
    body_template: str = ""
    reply_template: str = ""
    auth_type: Literal["none", "bearer", "basic", "api_key"] = AuthType.none.value
    auth_username: str | None = Field(default=None, max_length=128)
    auth_header_name: str | None = Field(default=None, max_length=64)
    timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    enabled: bool = True


class IntegrationCreate(IntegrationBase):
    auth_secret: str | None = None  # write-only
    params: list[IntegrationParamIn] = Field(default_factory=list)


class IntegrationUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    description: str | None = None
    base_url: str | None = Field(default=None, min_length=1, max_length=512)
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"] | None = None
    path_template: str | None = None
    query_template: str | None = None
    headers_template: str | None = None
    body_template: str | None = None
    reply_template: str | None = None
    auth_type: Literal["none", "bearer", "basic", "api_key"] | None = None
    auth_username: str | None = Field(default=None, max_length=128)
    auth_header_name: str | None = Field(default=None, max_length=64)
    auth_secret: str | None = None  # "" clears it; omit to keep
    timeout_seconds: float | None = Field(default=None, gt=0, le=120)
    enabled: bool | None = None
    params: list[IntegrationParamIn] | None = None


class IntegrationOut(IntegrationBase):
    model_config = ORM
    id: int
    has_secret: bool = False
    params: list[IntegrationParamOut] = Field(default_factory=list)
    created_at: UtcDateTime
    updated_at: UtcDateTime


class IntegrationTestRequest(BaseModel):
    params: dict[str, str] = Field(default_factory=dict)
    username: str = "portal-tester"


class IntegrationTestResult(BaseModel):
    ok: bool
    status_code: int | None = None
    url: str | None = None
    reply: str | None = None
    raw: Any = None
    error: str | None = None
    duration_ms: int | None = None


# --------------------------------------------------------------------------- #
# Scheduled tasks
# --------------------------------------------------------------------------- #


class TaskBase(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    owner_rc_user_id: str | None = Field(default=None, max_length=64)
    owner_username: str | None = Field(default=None, max_length=128)
    room_id: str | None = Field(default=None, max_length=64)
    dm_username: str | None = Field(default=None, max_length=128)
    schedule_kind: Literal["daily", "weekly", "cron", "once"] = ScheduleKind.daily.value
    time_of_day: str | None = Field(default=None, max_length=5)
    days_of_week: str | None = Field(default=None, max_length=32)
    cron_expr: str | None = Field(default=None, max_length=128)
    run_at: UtcDateTime | None = None
    timezone_name: str = Field(default="UTC", max_length=64)
    action_type: Literal["message", "integration"] = ActionType.message.value
    message: str = ""
    integration_id: int | None = None
    integration_args: str = ""
    enabled: bool = True


class TaskCreate(TaskBase):
    pass


class TaskUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    room_id: str | None = Field(default=None, max_length=64)
    dm_username: str | None = Field(default=None, max_length=128)
    schedule_kind: Literal["daily", "weekly", "cron", "once"] | None = None
    time_of_day: str | None = Field(default=None, max_length=5)
    days_of_week: str | None = Field(default=None, max_length=32)
    cron_expr: str | None = Field(default=None, max_length=128)
    run_at: UtcDateTime | None = None
    timezone_name: str | None = Field(default=None, max_length=64)
    action_type: Literal["message", "integration"] | None = None
    message: str | None = None
    integration_id: int | None = None
    integration_args: str | None = None
    enabled: bool | None = None


class TaskOut(TaskBase):
    model_config = ORM
    id: int
    created_via: str
    next_run_at: UtcDateTime | None = None
    last_run_at: UtcDateTime | None = None
    last_status: str | None = None
    schedule_description: str = ""
    # next_run_at rendered in the task's own timezone, for display.
    next_run_local: str = ""
    created_at: UtcDateTime
    updated_at: UtcDateTime


class TaskRunOut(BaseModel):
    model_config = ORM
    id: int
    task_id: int
    ran_at: UtcDateTime
    status: str
    detail: str
    duration_ms: int | None = None


# --------------------------------------------------------------------------- #
# Logs
# --------------------------------------------------------------------------- #


class MessageLogOut(BaseModel):
    model_config = ORM
    id: int
    received_at: UtcDateTime
    room_id: str
    room_type: str | None = None
    sender_username: str | None = None
    text_in: str
    matched_command: str | None = None
    text_out: str
    status: str
    error: str | None = None
    duration_ms: int | None = None


class AuditLogOut(BaseModel):
    model_config = ORM
    id: int
    at: UtcDateTime
    actor: str
    action: str
    entity: str
    entity_id: str | None = None
    detail: str


class DashboardStats(BaseModel):
    commands_total: int
    commands_enabled: int
    integrations_total: int
    tasks_total: int
    tasks_enabled: int
    messages_today: int
    errors_today: int
    connection: ConnectionStatusOut
    recent_messages: list[MessageLogOut] = Field(default_factory=list)
    upcoming_tasks: list[TaskOut] = Field(default_factory=list)
