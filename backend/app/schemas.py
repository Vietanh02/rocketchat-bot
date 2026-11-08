"""Pydantic models for the portal API.

Secrets are write-only: clients send `auth_secret`, and read back only
`has_secret`.
"""

from __future__ import annotations

import re
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
    # Whether the language model is configured, so "llm" can be selected,
    # and the model it uses.
    llm_available: bool = False
    llm_model: str | None = None


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


def _gateway_url(value: str | None) -> str | None:
    """An OpenAI-compatible base URL, without the trailing slash."""
    if value is None:
        return None
    value = value.strip().rstrip("/")
    if value and not re.match(r"^https?://[^\s/]+", value):
        raise ValueError("must be an http:// or https:// URL")
    return value


class LLMConfigOut(BaseModel):
    # Answer messages that match no command with the model ("llm" responder).
    enabled: bool
    base_url: str
    model: str
    system_prompt: str
    timeout_seconds: float
    max_tokens: int | None = None
    temperature: float | None = None
    history_messages: int
    format_for_rocketchat: bool
    has_api_key: bool
    # Gateway, key and model are all set.
    configured: bool
    # What an empty system prompt stands for.
    default_system_prompt: str


class LLMConfigUpdate(BaseModel):
    enabled: bool | None = None
    base_url: str | None = Field(default=None, max_length=512)
    # Write-only. "" clears the stored key; omit to leave it unchanged.
    api_key: str | None = Field(default=None, max_length=4096)
    model: str | None = Field(default=None, max_length=256)
    system_prompt: str | None = Field(default=None, max_length=20000)
    timeout_seconds: float | None = Field(default=None, ge=1, le=600)
    # null clears these, leaving the gateway's default.
    max_tokens: int | None = Field(default=None, ge=1, le=200000)
    temperature: float | None = Field(default=None, ge=0, le=2)
    history_messages: int | None = Field(default=None, ge=0, le=100)
    format_for_rocketchat: bool | None = None

    _base_url = field_validator("base_url")(classmethod(lambda cls, v: _gateway_url(v)))

    @field_validator("model")
    @classmethod
    def _strip(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None


class LLMTestRequest(BaseModel):
    """Try the gateway; anything omitted comes from what is saved."""

    base_url: str | None = Field(default=None, max_length=512)
    api_key: str | None = Field(default=None, max_length=4096)
    model: str | None = Field(default=None, max_length=256)
    system_prompt: str | None = Field(default=None, max_length=20000)
    timeout_seconds: float | None = Field(default=None, ge=1, le=600)
    max_tokens: int | None = Field(default=None, ge=1, le=200000)
    temperature: float | None = Field(default=None, ge=0, le=2)
    format_for_rocketchat: bool | None = None
    prompt: str = Field(default="Reply with one short sentence to say you are working.", min_length=1, max_length=4000)

    _base_url = field_validator("base_url")(classmethod(lambda cls, v: _gateway_url(v)))


class LLMTestResult(BaseModel):
    ok: bool
    message: str
    # The answer as it would be posted in Rocket.Chat.
    reply: str | None = None
    duration_ms: int | None = None


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
    llm_tool: bool = False


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
    llm_tool: bool | None = None
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


# --------------------------------------------------------------------------- #
# Assistant (agent) settings and timings
# --------------------------------------------------------------------------- #


class AgentConfigOut(BaseModel):
    ack_reaction: str
    fast_model: str
    turn_deadline_seconds: float
    queue_mode: Literal["followup", "collect"]
    idle_reset_minutes: int
    summarize_after_messages: int
    parallel_tools: bool


class AgentConfigUpdate(BaseModel):
    # An emoji shortcode such as ":eyes:"; "" for none.
    ack_reaction: str | None = Field(default=None, max_length=64, pattern=r"^(:[\w+-]+:)?$")
    fast_model: str | None = Field(default=None, max_length=256)
    turn_deadline_seconds: float | None = Field(default=None, ge=5, le=600)
    queue_mode: Literal["followup", "collect"] | None = None
    idle_reset_minutes: int | None = Field(default=None, ge=0, le=60 * 24 * 30)
    summarize_after_messages: int | None = Field(default=None, ge=0, le=1000)
    parallel_tools: bool | None = None

    @field_validator("ack_reaction", "fast_model")
    @classmethod
    def _strip(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None


class Percentiles(BaseModel):
    p50: float | None = None
    p95: float | None = None
    max: float | None = None


class AgentRunOut(BaseModel):
    id: int
    started_at: datetime
    trigger: str
    status: str
    model: str
    rounds: int
    tools: list[dict[str, Any]]
    ack_ms: int | None
    ttft_ms: int | None
    total_ms: int | None
    overhead_ms: float | None
    error: str | None


class AgentStatsOut(BaseModel):
    runs: int
    since: datetime | None
    errors: int
    timeouts: int
    ack_ms: Percentiles
    ttft_ms: Percentiles
    total_ms: Percentiles
    overhead_ms: Percentiles
    recent: list[AgentRunOut]


# --------------------------------------------------------------------------- #
# Vector search, memory and profiles
# --------------------------------------------------------------------------- #


class EmbeddingConfigOut(BaseModel):
    provider: Literal["", "openai", "tei"]
    base_url: str
    model: str
    dimensions: int | None
    query_prefix: str
    document_prefix: str
    timeout_seconds: float
    has_api_key: bool
    rerank_provider: Literal["", "tei", "cohere"]
    rerank_base_url: str
    rerank_model: str
    has_rerank_api_key: bool
    configured: bool
    rerank_configured: bool
    # Whether APP_QDRANT_URL is set; vectors are only searched when it is.
    vector_db_configured: bool


class EmbeddingConfigUpdate(BaseModel):
    provider: Literal["", "openai", "tei"] | None = None
    base_url: str | None = Field(default=None, max_length=512)
    # Write-only. "" clears the stored key; omit to leave it unchanged.
    api_key: str | None = Field(default=None, max_length=4096)
    model: str | None = Field(default=None, max_length=256)
    # null takes the model's own size.
    dimensions: int | None = Field(default=None, ge=8, le=8192)
    query_prefix: str | None = Field(default=None, max_length=64)
    document_prefix: str | None = Field(default=None, max_length=64)
    timeout_seconds: float | None = Field(default=None, ge=1, le=300)
    rerank_provider: Literal["", "tei", "cohere"] | None = None
    rerank_base_url: str | None = Field(default=None, max_length=512)
    rerank_api_key: str | None = Field(default=None, max_length=4096)
    rerank_model: str | None = Field(default=None, max_length=256)

    _urls = field_validator("base_url", "rerank_base_url")(
        classmethod(lambda cls, v: _gateway_url(v))
    )

    @field_validator("model", "rerank_model")
    @classmethod
    def _strip(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None


class EmbeddingTestResult(BaseModel):
    ok: bool
    message: str
    dimensions: int | None = None
    embed_ms: int | None = None
    # Present when a reranker is set up.
    rerank_ok: bool | None = None
    rerank_message: str | None = None
    rerank_ms: int | None = None


class MemoryConfigOut(BaseModel):
    enabled: bool
    auto_extract: bool
    prompt_notes: int
    min_score: float


class MemoryConfigUpdate(BaseModel):
    enabled: bool | None = None
    auto_extract: bool | None = None
    prompt_notes: int | None = Field(default=None, ge=0, le=30)
    min_score: float | None = Field(default=None, ge=0, le=1)


class VectorDbStatus(BaseModel):
    configured: bool
    reachable: bool = False
    version: str | None = None
    error: str | None = None
    # Vectors in the memory collection; None if it does not exist yet.
    memory_vectors: int | None = None


class MemoryStatusOut(BaseModel):
    vector_db: VectorDbStatus
    embedding_configured: bool
    notes: int
    # Notes with no vector of the current model yet.
    pending: int
    # Searches use words only when this is false.
    semantic: bool


class MemoryNoteOut(BaseModel):
    id: int
    scope: str
    text: str
    source: str
    created_by: str | None
    created_at: datetime | None
    score: float | None = None


class MemoryNoteList(BaseModel):
    items: list[MemoryNoteOut]
    total: int


class MemoryScopeOut(BaseModel):
    scope: str
    count: int
    # A person's username or "everyone", when known.
    label: str


class MemoryNoteCreate(BaseModel):
    # "global", "user:<rc user id>" or "room:<room id>".
    scope: str = Field(min_length=1, max_length=160, pattern=r"^(global|user:[\w-]+|room:[\w-]+)$")
    text: str = Field(min_length=1, max_length=1000)


class MemoryNoteUpdate(BaseModel):
    text: str = Field(min_length=1, max_length=1000)


class ProfileOut(BaseModel):
    rc_user_id: str
    username: str
    display_name: str
    language: str
    timezone: str
    about: str
    notes: int = 0
    updated_at: datetime | None = None


class ProfileUpdate(BaseModel):
    username: str | None = Field(default=None, max_length=128)
    display_name: str | None = Field(default=None, max_length=128)
    language: str | None = Field(default=None, max_length=64)
    timezone: str | None = Field(default=None, max_length=64)
    about: str | None = Field(default=None, max_length=1000)
