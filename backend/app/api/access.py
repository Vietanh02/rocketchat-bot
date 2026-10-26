"""The bot's access list: which Rocket.Chat users it allows or blocks.

Whether the list is an allowlist or a blocklist is `access_mode` on the bot
settings (PUT /api/config); this router manages the usernames on it.
"""

from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import select

from app.api.deps import AdminUser, CurrentUser, RuntimeDep, SessionDep, record_audit
from app.bot.access import normalize_username
from app.bot.config_store import bump_config_version
from app.models import AccessMode, BotAccessEntry
from app.schemas import (
    AccessCheckResult,
    AccessEntryCreate,
    AccessEntryOut,
    AccessEntryUpdate,
)

router = APIRouter(prefix="/api/access", tags=["access"])

_SEPARATORS = re.compile(r"[\s,;]+")


def _parse_usernames(raw: str) -> list[str]:
    """Split a pasted list into normalised, de-duplicated usernames."""
    seen: dict[str, None] = {}
    bad: list[str] = []
    for token in _SEPARATORS.split(raw):
        if not token:
            continue
        try:
            seen[normalize_username(token)] = None
        except ValueError:
            bad.append(token)
    if bad:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Not valid Rocket.Chat usernames: " + ", ".join(bad[:20]),
        )
    if not seen:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Give at least one username",
        )
    return list(seen)


@router.get("", response_model=list[AccessEntryOut])
async def list_entries(_: CurrentUser, session: SessionDep) -> list[BotAccessEntry]:
    return list(
        (await session.scalars(select(BotAccessEntry).order_by(BotAccessEntry.username))).all()
    )


@router.post("", response_model=list[AccessEntryOut], status_code=status.HTTP_201_CREATED)
async def add_entries(
    payload: AccessEntryCreate,
    admin: AdminUser,
    session: SessionDep,
    runtime: RuntimeDep,
) -> list[BotAccessEntry]:
    """Add one or more users. Users already on the list are left as they are."""
    usernames = _parse_usernames(payload.usernames)
    existing = set(
        (
            await session.scalars(
                select(BotAccessEntry.username).where(BotAccessEntry.username.in_(usernames))
            )
        ).all()
    )
    added = [
        BotAccessEntry(username=name, note=payload.note.strip())
        for name in usernames
        if name not in existing
    ]
    if not added:
        return []

    session.add_all(added)
    await session.flush()
    await bump_config_version(session)
    record_audit(
        session,
        admin,
        "create",
        "access_entry",
        None,
        ", ".join(entry.username for entry in added),
    )
    await session.commit()
    await runtime.apply_config_change()
    return added


@router.put("/{entry_id}", response_model=AccessEntryOut)
async def update_entry(
    entry_id: int,
    payload: AccessEntryUpdate,
    admin: AdminUser,
    session: SessionDep,
) -> BotAccessEntry:
    """Only the note can change; the policy does not depend on it."""
    entry = await session.get(BotAccessEntry, entry_id)
    if entry is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such entry")
    entry.note = payload.note.strip()
    record_audit(session, admin, "update", "access_entry", entry.id, entry.username)
    await session.commit()
    await session.refresh(entry)
    return entry


@router.delete("/{entry_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_entry(
    entry_id: int, admin: AdminUser, session: SessionDep, runtime: RuntimeDep
) -> None:
    entry = await session.get(BotAccessEntry, entry_id)
    if entry is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such entry")
    username = entry.username
    await session.delete(entry)
    await bump_config_version(session)
    record_audit(session, admin, "delete", "access_entry", entry_id, username)
    await session.commit()
    await runtime.apply_config_change()


@router.get("/check", response_model=AccessCheckResult)
async def check_user(
    _: CurrentUser,
    runtime: RuntimeDep,
    username: str = Query(min_length=1, max_length=130),
) -> AccessCheckResult:
    """Would the bot answer this user right now?"""
    try:
        name = normalize_username(username)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc

    policy = (await runtime.config_store.current()).access
    listed = name in policy.usernames
    if policy.mode == AccessMode.everyone.value:
        reason = "the bot is open to everyone"
    elif policy.mode == AccessMode.allowlist.value:
        reason = "on the allowlist" if listed else "not on the allowlist"
    else:
        reason = "on the blocklist" if listed else "not on the blocklist"
    return AccessCheckResult(
        username=name, allowed=policy.allows(name), reason=reason
    )
