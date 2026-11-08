"""Which conversation a message belongs to, and that conversation's transcript.

A DM is one conversation, and so is a channel; a thread is its own. The
transcript is kept in the database, so a restart forgets nothing, and the
latest messages of busy conversations are also kept in memory so a turn does
not have to read them back.

`new` (and an idle reset, if configured) starts a new *generation*: the old
messages stay in the database for the portal, but are never sent to the
model again.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot.types import MessageContext
from app.models import AgentMessage, AgentSession

log = logging.getLogger(__name__)

# Conversations whose recent messages are kept in memory.
HOT_SESSIONS = 500
# Messages kept in memory per conversation; more are read from the database.
HOT_MESSAGES = 100

Message = dict[str, str]


def session_key(message: MessageContext, thread_id: str | None) -> tuple[str, str]:
    """(key, kind) of the conversation `message` belongs to, given its reply thread."""
    if thread_id:
        return f"room:{message.room_id}:t:{thread_id}", "thread"
    if message.is_direct:
        return f"dm:{message.room_id}", "dm"
    return f"room:{message.room_id}", "room"


@dataclass(slots=True)
class SessionState:
    """A conversation as a turn sees it."""

    id: int
    key: str
    generation: int
    summary: str
    # Latest messages of the current generation not yet folded into the
    # summary, oldest first; at most HOT_MESSAGES.
    messages: list[Message] = field(default_factory=list)
    # How many such messages the database holds (may exceed `messages`).
    uncompacted: int = 0

    def history(self, limit: int) -> list[Message]:
        if limit <= 0:
            return []
        return [dict(m) for m in self.messages[-limit:]]


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class SessionStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._hot: OrderedDict[str, SessionState] = OrderedDict()
        self._last_active: dict[str, datetime] = {}

    def _remember(self, state: SessionState) -> None:
        self._hot[state.key] = state
        self._hot.move_to_end(state.key)
        while len(self._hot) > HOT_SESSIONS:
            old, _ = self._hot.popitem(last=False)
            self._last_active.pop(old, None)

    def forget(self, key: str) -> None:
        self._hot.pop(key, None)

    async def open(
        self,
        key: str,
        kind: str,
        message: MessageContext,
        *,
        idle_reset_minutes: int = 0,
    ) -> SessionState:
        """The conversation for `key`, created on first use."""
        now = datetime.now(timezone.utc)
        state = self._hot.get(key)
        if state is not None:
            last = self._last_active.get(key)
            if not (idle_reset_minutes and last and now - last > timedelta(minutes=idle_reset_minutes)):
                self._hot.move_to_end(key)
                return state
            # Idle too long: let the database path below start a new generation.

        async with self._session_factory() as db:
            row = await db.scalar(select(AgentSession).where(AgentSession.key == key))
            if row is None:
                row = AgentSession(
                    key=key,
                    kind=kind,
                    room_id=message.room_id,
                    thread_id=key.split(":t:", 1)[1] if ":t:" in key else None,
                    owner_rc_user_id=message.sender_id if kind == "dm" else None,
                    owner_username=message.sender_username if kind == "dm" else None,
                    last_active_at=now,
                )
                db.add(row)
                await db.commit()
                state = SessionState(row.id, key, row.generation, "")
                self._remember(state)
                self._last_active[key] = now
                return state

            if idle_reset_minutes and now - _utc(row.last_active_at) > timedelta(
                minutes=idle_reset_minutes
            ):
                row.generation += 1
                row.summary = ""
                await db.commit()
                log.info("Conversation %s was idle; starting afresh", key)

            state = await self._load(db, row)
        self._remember(state)
        self._last_active[key] = _utc(row.last_active_at)
        return state

    async def _load(self, db: AsyncSession, row: AgentSession) -> SessionState:
        live = (
            AgentMessage.session_id == row.id,
            AgentMessage.generation == row.generation,
            AgentMessage.compacted.is_(False),
        )
        rows = (
            await db.scalars(
                select(AgentMessage).where(*live).order_by(AgentMessage.id.desc()).limit(HOT_MESSAGES)
            )
        ).all()
        count = await db.scalar(select(func.count()).select_from(AgentMessage).where(*live))
        return SessionState(
            id=row.id,
            key=row.key,
            generation=row.generation,
            summary=row.summary or "",
            messages=[{"role": m.role, "content": m.content} for m in reversed(rows)],
            uncompacted=count or 0,
        )

    async def append(
        self,
        state: SessionState,
        question: str,
        answer: str,
        *,
        sender_username: str | None = None,
    ) -> None:
        """Add one exchange to the transcript."""
        now = datetime.now(timezone.utc)
        async with self._session_factory() as db:
            db.add_all(
                [
                    AgentMessage(
                        session_id=state.id,
                        generation=state.generation,
                        role="user",
                        content=question,
                        sender_username=sender_username,
                        created_at=now,
                    ),
                    AgentMessage(
                        session_id=state.id,
                        generation=state.generation,
                        role="assistant",
                        content=answer,
                        created_at=now,
                    ),
                ]
            )
            await db.execute(
                update(AgentSession).where(AgentSession.id == state.id).values(last_active_at=now)
            )
            await db.commit()
        state.messages.extend(
            [{"role": "user", "content": question}, {"role": "assistant", "content": answer}]
        )
        del state.messages[:-HOT_MESSAGES]
        state.uncompacted += 2
        self._last_active[state.key] = now

    async def reset(self, key: str) -> bool:
        """Start the conversation afresh; False if there was none."""
        self.forget(key)
        async with self._session_factory() as db:
            row = await db.scalar(select(AgentSession).where(AgentSession.key == key))
            if row is None:
                return False
            row.generation += 1
            row.summary = ""
            await db.commit()
        return True

    # ----------------------------------------------------------------- #
    # Compaction
    # ----------------------------------------------------------------- #

    async def oldest_uncompacted(self, state: SessionState, keep: int) -> list[tuple[int, Message]]:
        """All but the latest `keep` messages not yet folded into the summary."""
        async with self._session_factory() as db:
            rows = (
                await db.scalars(
                    select(AgentMessage)
                    .where(
                        AgentMessage.session_id == state.id,
                        AgentMessage.generation == state.generation,
                        AgentMessage.compacted.is_(False),
                    )
                    .order_by(AgentMessage.id)
                )
            ).all()
        rows = rows[: max(0, len(rows) - keep)]
        return [(m.id, {"role": m.role, "content": m.content}) for m in rows]

    async def compact(self, state: SessionState, message_ids: list[int], summary: str) -> None:
        """Replace `message_ids` with `summary` in what the model is sent."""
        async with self._session_factory() as db:
            row = await db.get(AgentSession, state.id)
            if row is None or row.generation != state.generation:
                return  # reset meanwhile; the summary belongs to a past generation
            row.summary = summary
            await db.execute(
                update(AgentMessage)
                .where(AgentMessage.id.in_(message_ids))
                .values(compacted=True)
            )
            await db.commit()
        state.summary = summary
        state.uncompacted = max(0, state.uncompacted - len(message_ids))
        # What is left uncompacted is the latest messages; keep only those.
        del state.messages[: max(0, len(state.messages) - state.uncompacted)]
