"""Bot settings, connection status and connection testing."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from app.api.deps import AdminUser, CurrentUser, RuntimeDep, SessionDep, record_audit
from app.bot.config_store import bump_config_version, ensure_bot_config
from app.models import BotConfig
from app.rocketchat.rest import RocketChatError, RocketChatRest
from app.scheduler.triggers import ScheduleError, resolve_timezone
from app.schemas import (
    BotConfigOut,
    BotConfigUpdate,
    ConnectionStatusOut,
    ConnectionTestRequest,
    ConnectionTestResult,
)
from app.security import decrypt_secret, encrypt_secret

router = APIRouter(prefix="/api/config", tags=["config"])

# Fields written straight through from the update payload.
PLAIN_FIELDS = (
    "rc_rest_url",
    "rc_ws_url",
    "bot_user_id",
    "bot_username",
    "enabled",
    "responder_mode",
    "greeting",
    "fallback_reply",
    "reply_in_thread",
    "respond_in_channels",
    "default_timezone",
    "reply_mode",
    "stream_chunk_chars",
    "stream_interval_ms",
    "access_mode",
)

# Changing any of these means the live connection must be rebuilt.
RECONNECT_FIELDS = frozenset(
    {
        "rc_rest_url",
        "rc_ws_url",
        "bot_user_id",
        "bot_username",
        "bot_auth_token",
        "bot_password",
        "enabled",
    }
)


def _to_out(config: BotConfig) -> BotConfigOut:
    return BotConfigOut(
        rc_rest_url=config.rc_rest_url,
        rc_ws_url=config.rc_ws_url,
        bot_user_id=config.bot_user_id,
        bot_username=config.bot_username,
        enabled=config.enabled,
        responder_mode=config.responder_mode,
        greeting=config.greeting,
        fallback_reply=config.fallback_reply,
        reply_in_thread=config.reply_in_thread,
        respond_in_channels=config.respond_in_channels,
        default_timezone=config.default_timezone,
        reply_mode=config.reply_mode,
        stream_chunk_chars=config.stream_chunk_chars,
        stream_interval_ms=config.stream_interval_ms,
        access_mode=config.access_mode,
        config_version=config.config_version,
        has_auth_token=bool(config.bot_auth_token_enc),
        has_password=bool(config.bot_password_enc),
    )


@router.get("", response_model=BotConfigOut)
async def read_config(_: CurrentUser, session: SessionDep) -> BotConfigOut:
    return _to_out(await ensure_bot_config(session))


@router.put("", response_model=BotConfigOut)
async def update_config(
    payload: BotConfigUpdate,
    admin: AdminUser,
    session: SessionDep,
    runtime: RuntimeDep,
) -> BotConfigOut:
    config = await ensure_bot_config(session)
    changes = payload.model_dump(exclude_unset=True)

    if "default_timezone" in changes:
        try:
            resolve_timezone(changes["default_timezone"])
        except ScheduleError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
            ) from exc

    for field in PLAIN_FIELDS:
        if field in changes and changes[field] is not None:
            setattr(config, field, changes[field])

    # Secrets: "" clears, a value replaces, omitted leaves alone.
    if "bot_auth_token" in changes:
        config.bot_auth_token_enc = encrypt_secret(changes["bot_auth_token"] or None)
    if "bot_password" in changes:
        config.bot_password_enc = encrypt_secret(changes["bot_password"] or None)

    await bump_config_version(session)
    record_audit(
        session,
        admin,
        "update",
        "bot_config",
        config.id,
        ", ".join(sorted(changes)) or "(no changes)",
    )
    await session.commit()
    await session.refresh(config)

    needs_reconnect = bool(RECONNECT_FIELDS & set(changes))
    await runtime.apply_config_change(reconnect=needs_reconnect)
    return _to_out(config)


async def build_status(runtime: RuntimeDep) -> ConnectionStatusOut:
    """Shared by GET /status and POST /restart."""
    status_ = runtime.bot.status
    snapshot = await runtime.config_store.current()
    return ConnectionStatusOut(
        configured=status_.configured,
        connected=status_.connected,
        bot_user_id=status_.bot_user_id,
        bot_username=status_.bot_username,
        rest_url=status_.rest_url or snapshot.settings.rc_rest_url,
        ws_url=status_.ws_url or snapshot.settings.rc_ws_url,
        last_error=status_.last_error,
        messages_handled=status_.messages_handled,
        scheduler_running=runtime.scheduler.running,
        commands_loaded=len(snapshot.router),
        integrations_loaded=len(snapshot.integrations),
        tasks_scheduled=runtime.scheduler.job_count,
        details=status_.details,
    )


@router.get("/status", response_model=ConnectionStatusOut)
async def connection_status(
    _: CurrentUser, runtime: RuntimeDep
) -> ConnectionStatusOut:
    return await build_status(runtime)


@router.post("/restart", response_model=ConnectionStatusOut)
async def restart_bot(_: AdminUser, runtime: RuntimeDep) -> ConnectionStatusOut:
    await runtime.bot.restart()
    await runtime.scheduler.reload_all()
    return await build_status(runtime)


@router.post("/test", response_model=ConnectionTestResult)
async def test_connection(
    payload: ConnectionTestRequest, _: AdminUser, session: SessionDep
) -> ConnectionTestResult:
    """Verify Rocket.Chat credentials without saving or restarting anything."""
    config = await ensure_bot_config(session)
    rest_url = payload.rc_rest_url or config.rc_rest_url
    if not rest_url:
        return ConnectionTestResult(ok=False, message="No Rocket.Chat URL configured")

    token = payload.bot_auth_token or decrypt_secret(config.bot_auth_token_enc)
    user_id = payload.bot_user_id or config.bot_user_id
    username = payload.bot_username or config.bot_username
    password = payload.bot_password or decrypt_secret(config.bot_password_enc)

    rest = RocketChatRest(rest_url, user_id=user_id, auth_token=token)
    try:
        if not token:
            if not (username and password):
                return ConnectionTestResult(
                    ok=False,
                    message="Provide a personal access token, or a username and password",
                )
            await rest.login(username, password)
        identity = await rest.me()
    except RocketChatError as exc:
        return ConnectionTestResult(ok=False, message=str(exc))
    finally:
        await rest.aclose()

    return ConnectionTestResult(
        ok=True,
        message=f"Connected to {rest_url} as @{identity.get('username')}",
        bot_user_id=identity.get("_id"),
        bot_username=identity.get("username"),
    )
