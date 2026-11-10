"""The language model service: its gateway, key, model and limits.

Turning it on sets `bot_config.responder_mode` to "llm": commands still
answer first, and messages that match none of them go to the model.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException, status

from app.api.deps import AdminUser, CurrentUser, RuntimeDep, SessionDep, record_audit
from app.bot.config_store import (
    LLMSettings,
    bump_config_version,
    ensure_bot_config,
    ensure_llm_config,
)
from app.bot.llm import DEFAULT_SYSTEM_PROMPT, DEFAULT_SYSTEM_PROMPT_RAW, LLMChat, LLMError
from app.bot.rcformat import strip_thinking, to_rocketchat
from app.integrations.executor import IntegrationError, assert_url_allowed
from app.models import BotConfig, LLMConfig, ResponderMode
from app.schemas import LLMConfigOut, LLMConfigUpdate, LLMTestRequest, LLMTestResult
from app.security import encrypt_secret

router = APIRouter(prefix="/api/llm", tags=["llm"])

PLAIN_FIELDS = (
    "base_url",
    "model",
    "system_prompt",
    "timeout_seconds",
    "history_messages",
    "format_for_rocketchat",
    "quick_extra_body",
)
# Null is meaningful for these: it leaves the choice to the gateway.
NULLABLE_FIELDS = ("max_tokens", "temperature")


def _to_out(config: LLMConfig, bot: BotConfig) -> LLMConfigOut:
    settings = LLMSettings.from_model(config)
    return LLMConfigOut(
        enabled=bot.responder_mode == ResponderMode.llm.value,
        base_url=config.base_url,
        model=config.model,
        system_prompt=config.system_prompt,
        timeout_seconds=config.timeout_seconds,
        max_tokens=config.max_tokens,
        temperature=config.temperature,
        history_messages=config.history_messages,
        format_for_rocketchat=config.format_for_rocketchat,
        quick_extra_body=config.quick_extra_body or "",
        has_api_key=bool(config.api_key_enc),
        configured=settings.configured,
        default_system_prompt=(
            DEFAULT_SYSTEM_PROMPT if config.format_for_rocketchat else DEFAULT_SYSTEM_PROMPT_RAW
        ),
    )


async def _check_gateway(url: str) -> None:
    try:
        await assert_url_allowed(url)
    except IntegrationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=f"Gateway URL: {exc}"
        ) from exc


@router.get("", response_model=LLMConfigOut)
async def read_llm_config(_: CurrentUser, session: SessionDep) -> LLMConfigOut:
    config = await ensure_llm_config(session)
    bot = await ensure_bot_config(session)
    await session.commit()
    return _to_out(config, bot)


@router.put("", response_model=LLMConfigOut)
async def update_llm_config(
    payload: LLMConfigUpdate,
    admin: AdminUser,
    session: SessionDep,
    runtime: RuntimeDep,
) -> LLMConfigOut:
    config = await ensure_llm_config(session)
    bot = await ensure_bot_config(session)
    changes = payload.model_dump(exclude_unset=True)

    if changes.get("base_url") and changes["base_url"] != config.base_url:
        await _check_gateway(changes["base_url"])

    for field in PLAIN_FIELDS:
        if field in changes and changes[field] is not None:
            setattr(config, field, changes[field])
    for field in NULLABLE_FIELDS:
        if field in changes:
            setattr(config, field, changes[field])
    if "api_key" in changes:
        config.api_key_enc = encrypt_secret((changes["api_key"] or "").strip() or None)

    if changes.get("enabled") is not None:
        bot.responder_mode = (
            ResponderMode.llm.value if changes["enabled"] else ResponderMode.rules.value
        )
    if bot.responder_mode == ResponderMode.llm.value and not LLMSettings.from_model(config).configured:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Set the gateway URL, API key and model before turning the model on.",
        )

    await bump_config_version(session)
    record_audit(
        session,
        admin,
        "update",
        "llm_config",
        config.id,
        # Field names only; the key itself never reaches the audit log.
        ", ".join(sorted(changes)) or "(no changes)",
    )
    await session.commit()
    await session.refresh(config)
    await session.refresh(bot)

    await runtime.apply_config_change()
    return _to_out(config, bot)


@router.post("/test", response_model=LLMTestResult)
async def test_llm(payload: LLMTestRequest, _: AdminUser, session: SessionDep) -> LLMTestResult:
    """Ask the model one question with these settings, without saving them."""
    stored = LLMSettings.from_model(await ensure_llm_config(session))
    await session.commit()
    fields = payload.model_dump(exclude_unset=True, exclude={"prompt"})
    if not (fields.get("api_key") or "").strip():
        fields.pop("api_key", None)
    settings = LLMSettings(
        base_url=fields.get("base_url", stored.base_url),
        api_key=fields.get("api_key", stored.api_key),
        model=fields.get("model") or stored.model,
        system_prompt=fields.get("system_prompt", stored.system_prompt) or "",
        timeout_seconds=fields.get("timeout_seconds") or stored.timeout_seconds,
        max_tokens=fields.get("max_tokens", stored.max_tokens),
        temperature=fields.get("temperature", stored.temperature),
        format_for_rocketchat=fields.get("format_for_rocketchat", stored.format_for_rocketchat),
    )
    missing = [
        name
        for name, value in (
            ("gateway URL", settings.base_url),
            ("API key", settings.api_key),
            ("model", settings.model),
        )
        if not value
    ]
    if missing:
        return LLMTestResult(ok=False, message="Missing: " + ", ".join(missing))
    try:
        await assert_url_allowed(settings.base_url)
    except IntegrationError as exc:
        return LLMTestResult(ok=False, message=f"Gateway URL: {exc}")

    chat = LLMChat.from_settings(settings)
    assert chat is not None
    started = time.monotonic()
    try:
        raw = await chat.complete(chat.build_messages(payload.prompt, [], bot_name="the bot"))
    except LLMError as exc:
        text = str(exc)
        # The admin testing the gateway needs the cause, debug mode or not.
        if exc.detail and exc.detail not in text:
            text = f"{text}: {exc.detail}"
        return LLMTestResult(
            ok=False,
            message=text[:1].upper() + text[1:],
            duration_ms=int((time.monotonic() - started) * 1000),
        )
    finally:
        await chat.aclose()
    duration_ms = int((time.monotonic() - started) * 1000)
    reply = to_rocketchat(raw) if settings.format_for_rocketchat else strip_thinking(raw)
    if not reply.strip():
        return LLMTestResult(
            ok=False, message="The model returned an empty answer", duration_ms=duration_ms
        )
    return LLMTestResult(
        ok=True,
        message=f"{settings.model} answered in {duration_ms / 1000:.1f}s",
        reply=reply.strip(),
        duration_ms=duration_ms,
    )
