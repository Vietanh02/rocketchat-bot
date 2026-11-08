"""One turn of the assistant: from a question to the model's answer.

A turn waits for its conversation to be free (`queue`), loads the
conversation (`session`), asks the model with the conversation's tools
under a deadline, stores the exchange, and returns. Folding old messages
into a summary happens afterwards, off the reply path, with the fast model
when one is configured.

Each turn carries a `TurnTimer`; once the reply is posted, `record` writes
it to `agent_run` for the portal's Logs and for benchmarks.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.prompt import build_messages
from app.agent.queue import MERGED, SessionQueue
from app.agent.session import SessionState, SessionStore, session_key
from app.agent.timing import TurnTimer
from app.agent.tools import ToolRegistry
from app.bot.llm import LLMError
from app.bot.rcformat import strip_thinking
from app.bot.types import MessageContext
from app.models import AgentRun, QueueMode

if TYPE_CHECKING:
    from app.bot.config_store import ConfigSnapshot
    from app.bot.llm import LLMChat
    from app.bot.streaming import LiveReply

log = logging.getLogger(__name__)

# The latest messages a summary never swallows, whatever `history_messages` says.
MIN_KEPT_AFTER_SUMMARY = 4
SUMMARY_MAX_TOKENS = 600
SUMMARY_PROMPT = (
    "You maintain the memory of a chat between users and an assistant. "
    "Rewrite the summary below so it also covers the new messages. Keep names, "
    "decisions, facts the users stated, open questions and anything the "
    "assistant promised; drop small talk. Write at most 200 words, in the "
    "language of the conversation. Reply with the summary only."
)

Shape = Callable[..., str]


@dataclass(slots=True)
class AgentTurn:
    message: MessageContext
    # What the model is asked; in a channel prefixed with who is asking.
    question: str
    # The thread the reply goes to, which also picks the conversation.
    thread_id: str | None
    timer: TurnTimer
    live: LiveReply | None = None


@dataclass(slots=True)
class AgentResult:
    # The model's own words (standard Markdown, reasoning removed).
    text: str
    status: str = "ok"  # ok|timeout|error
    error: str | None = None
    session_id: int | None = None
    model: str = ""
    # How many messages this turn answered (more than one when collected).
    turns: int = 1


class AgentRunner:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self.sessions = SessionStore(session_factory)
        self.queue = SessionQueue()
        self._background: set[asyncio.Task] = set()
        self._compacting: set[str] = set()

    def key_for(self, message: MessageContext, thread_id: str | None) -> str:
        return session_key(message, thread_id)[0]

    async def answer(
        self,
        turn: AgentTurn,
        *,
        snapshot: ConfigSnapshot,
        chat: LLMChat,
        tools: ToolRegistry | None,
        shape: Shape,
    ) -> AgentResult:
        """The answer to `turn`, or `MERGED` if a later turn answered it too."""
        key, kind = session_key(turn.message, turn.thread_id)
        collect = snapshot.agent.queue_mode == QueueMode.collect.value

        async def handle(turns: list[AgentTurn]) -> AgentResult:
            return await self._run(key, kind, turns, snapshot, chat, tools, shape)

        turn.timer.mark("queued")
        return await self.queue.submit(key, turn, handle, collect=collect)

    async def reset(self, message: MessageContext, thread_id: str | None) -> bool:
        return await self.sessions.reset(self.key_for(message, thread_id))

    # ----------------------------------------------------------------- #

    async def _run(
        self,
        key: str,
        kind: str,
        turns: list[AgentTurn],
        snapshot: ConfigSnapshot,
        chat: LLMChat,
        tools: ToolRegistry | None,
        shape: Shape,
    ) -> AgentResult:
        turn = turns[-1]
        timer, live = turn.timer, turn.live
        timer.mark("turn_started")
        # Waiting for earlier turns of the conversation is not this turn's work.
        timer.add_wait("queue", (timer.marks["turn_started"] - timer.marks.get("queued", 0)) / 1000)
        question = "\n".join(t.question for t in turns)
        settings = snapshot.agent

        with timer.waiting("db"):
            state = await self.sessions.open(
                key, kind, turn.message, idle_reset_minutes=settings.idle_reset_minutes
            )
        timer.mark("session_loaded")
        messages = build_messages(
            chat.system_prompt,
            bot_name=snapshot.settings.bot_username or "the bot",
            timezone_name=snapshot.settings.default_timezone,
            summary=state.summary,
            history=state.history(snapshot.llm.history_messages),
            question=question,
        )
        timer.mark("prompt_built")

        so_far = ""
        status_line = ""

        def render() -> str:
            text = shape(so_far, partial=True) if so_far else ""
            return f"{text}\n\n{status_line}".strip() if status_line else text

        def show() -> None:
            # Formatted only when the live message is about to be edited.
            if live is not None:
                live.push(render)

        def on_text(text: str) -> None:
            nonlocal so_far, status_line
            so_far, status_line = text, ""
            show()

        def on_event(kind: str, name: str, *rest: object) -> None:
            nonlocal status_line
            if kind == "tool_start":
                status_line = f"_:wrench: `{name}`…_"
                show()

        status, error = "ok", None
        try:
            async with asyncio.timeout(settings.turn_deadline_seconds):
                raw = await chat.complete(
                    messages,
                    on_text=None if live is None else on_text,
                    on_event=on_event,
                    tools=tools or None,
                    parallel_tools=settings.parallel_tools,
                    timer=timer,
                )
        except TimeoutError:
            status, raw = "timeout", so_far
            error = f"stopped after {settings.turn_deadline_seconds:g}s"
            log.info("Turn in %s hit the %gs deadline", key, settings.turn_deadline_seconds)
        except LLMError as exc:
            log.info("LLM reply failed: %s", exc)
            return AgentResult("", "error", str(exc), state.id, chat.model, len(turns))
        timer.mark("llm_done")

        answer = strip_thinking(raw).strip()
        if answer:
            with timer.waiting("db"):
                await self.sessions.append(
                    state, question, answer, sender_username=turn.message.sender_username
                )
            timer.mark("persisted")
            self._maybe_compact(state, snapshot, chat)
        return AgentResult(answer, status, error, state.id, chat.model, len(turns))

    # ----------------------------------------------------------------- #
    # Compaction, after the reply
    # ----------------------------------------------------------------- #

    def _maybe_compact(self, state: SessionState, snapshot: ConfigSnapshot, chat: LLMChat) -> None:
        limit = snapshot.agent.summarize_after_messages
        if limit <= 0 or state.uncompacted <= limit or state.key in self._compacting:
            return
        keep = max(snapshot.llm.history_messages, MIN_KEPT_AFTER_SUMMARY)
        self._compacting.add(state.key)
        task = asyncio.create_task(
            self._compact(state, chat, snapshot.agent.fast_model, keep),
            name=f"compact-{state.key}",
        )
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        task.add_done_callback(lambda _t, key=state.key: self._compacting.discard(key))

    async def _compact(self, state: SessionState, chat: LLMChat, fast_model: str, keep: int) -> None:
        try:
            folded = await self.sessions.oldest_uncompacted(state, keep)
            if not folded:
                return
            transcript = "\n".join(f"{m['role']}: {m['content']}" for _, m in folded)
            summary = await chat.complete(
                [
                    {"role": "system", "content": SUMMARY_PROMPT},
                    {
                        "role": "user",
                        "content": f"Summary so far:\n{state.summary or '(none)'}\n\n"
                        f"New messages:\n{transcript}",
                    },
                ],
                model=fast_model or None,
                max_tokens=SUMMARY_MAX_TOKENS,
            )
            summary = strip_thinking(summary).strip()
            if summary:
                await self.sessions.compact(state, [message_id for message_id, _ in folded], summary)
                log.info("Folded %d messages of %s into its summary", len(folded), state.key)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the conversation just stays longer
            log.warning("Could not summarize %s", state.key, exc_info=True)

    # ----------------------------------------------------------------- #

    async def record(
        self,
        timer: TurnTimer,
        result: AgentResult | None,
        *,
        trigger: str = "chat",
        status: str | None = None,
        error: str | None = None,
    ) -> None:
        """Write the turn's timings to `agent_run`."""
        if status is None:
            if result is not None and result.status != "ok":
                status = result.status
            else:
                status = "error" if error or result is None else "ok"
        timer.finish()
        marks = timer.marks
        first_seen = marks.get("first_visible", marks.get("delivered"))
        try:
            async with self._session_factory() as db:
                db.add(
                    AgentRun(
                        session_id=result.session_id if result else None,
                        trigger=trigger,
                        status=status,
                        model=result.model if result else "",
                        rounds=timer.rounds,
                        tools_json=json.dumps(timer.as_dict()["tools"]),
                        timings_json=json.dumps(timer.as_dict()),
                        ack_ms=_ms(marks.get("ack")),
                        ttft_ms=_ms(first_seen),
                        total_ms=_ms(timer.total_ms),
                        overhead_ms=round(timer.overhead_ms, 3),
                        error=error or (result.error if result else None),
                    )
                )
                await db.commit()
        except Exception:  # noqa: BLE001 - timings must never break a reply
            log.warning("Could not record the turn's timings", exc_info=True)

    async def close(self) -> None:
        """Cancel running turns and background work (the bot is stopping)."""
        await self.queue.close()
        tasks = list(self._background)
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._compacting.clear()


def _ms(value: float | None) -> int | None:
    return None if value is None else int(round(value))


__all__ = ["MERGED", "AgentResult", "AgentRunner", "AgentTurn"]
