"""One turn of the assistant: from a question to the model's answer.

A turn waits for its conversation to be free (`queue`), loads the
conversation (`session`) while recalling the notes and profile that go with
it (`memory`, `profiles`), asks the model with the conversation's tools
under a deadline, stores the exchange, and returns. Folding old messages
into a summary, and picking facts to remember when that is turned on,
happen afterwards, off the reply path, with the fast model when one is
configured.

Work that takes several steps goes on as a job (`app.agent.jobs`): the
model starts one with `start_job`, or a turn still running after
`job_after_seconds` is handed over to one.

A question to look up in the knowledge base is first matched against the
answers kept for it (`app.cache.answers`): a match is posted at once,
without the model. A turn that answered from the knowledge base alone
leaves an answer to keep, written afterwards by the fast model.

Each turn carries a `TurnTimer`; once the reply is posted, `record` writes
it to `agent_run` for the portal's Logs and for benchmarks.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.intent import COMMAND, LOOKUP, Intent, about_a_document, detect
from app.agent.jobs import JobError, JobService, JobTools
from app.agent.memory import MemoryScopes, MemoryStore, Note, NoteError
from app.agent.profiles import ProfileStore
from app.agent.prompt import COMMAND_NOTE, build_messages, passages_block
from app.agent.queue import MERGED, SessionQueue
from app.agent.session import SessionState, SessionStore, session_key
from app.agent.timing import TurnTimer
from app.agent.tools import ToolProvider, ToolRegistry
from app.agent.skills import SkillCatalog
from app.agent.tools.knowledge import KnowledgeTools
from app.agent.tools.mcp import McpTools
from app.agent.tools.skills import SkillTools
from app.agent.tools.memory import MemoryTools, ProfileTools
from app.bot.llm import LLMError
from app.cache.answers import AnswerCache, CachedAnswer
from app.cache.ttl import TTLCache
from app.bot.rcformat import strip_thinking
from app.bot.types import MessageContext
from app.knowledge.base import BASE_SCOPE, KnowledgeBase, Lookup, Passage, conversation_scope
from app.knowledge.folder import FolderImporter
from app.knowledge.service import KnowledgeService
from app.models import AgentRun, QueueMode

if TYPE_CHECKING:
    from app.bot.config_store import ConfigSnapshot
    from app.bot.llm import LLMChat
    from app.bot.streaming import LiveReply

log = logging.getLogger(__name__)

# The latest messages a summary never swallows, whatever `history_messages` says.
MIN_KEPT_AFTER_SUMMARY = 4
SUMMARY_MAX_TOKENS = 600
# Messages of the conversation shown when an answer is rewritten to keep.
LEARN_HISTORY = 4
SUMMARY_PROMPT = (
    "You maintain the memory of a chat between users and an assistant. "
    "Rewrite the summary below so it also covers the new messages. Keep names, "
    "decisions, facts the users stated, open questions and anything the "
    "assistant promised; drop small talk. Write at most 200 words, in the "
    "language of the conversation. Reply with the summary only."
)

EXTRACT_MAX_TOKENS = 400
# Notes the fast model may add after one exchange.
EXTRACT_MAX_NOTES = 3
EXTRACT_PROMPT = (
    "You decide what an assistant should remember from one exchange, for future "
    "conversations. Keep only lasting facts: preferences, names, roles, decisions, "
    "commitments, and anything the user explicitly asked to remember. Skip the "
    "question itself, small talk, things only true today, and secrets such as "
    "passwords or keys. Skip what the already remembered notes say. Write each note "
    "as one short, self-contained statement in the language of the conversation, "
    "naming who it is about. Reply with JSON only: "
    '{"notes": ["..."]} with at most 3 notes, or {"notes": []}.'
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
    # Set when the kind of message is already known (a document came with it).
    intent: str | None = None
    # Worked out afresh: no kept answer (asked again after a 👎).
    fresh: bool = False


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
    # Given from the kept answers, without the model.
    cached: CachedAnswer | None = None
    # Keeping this turn's answer for next time; gives the entry's id, or None.
    learning: asyncio.Task | None = None


class AgentRunner:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        knowledge: KnowledgeService | None = None,
    ) -> None:
        self._session_factory = session_factory
        self.knowledge = knowledge or KnowledgeService(session_factory)
        self.sessions = SessionStore(session_factory)
        self.memory = MemoryStore(session_factory, self.knowledge)
        self.profiles = ProfileStore(session_factory)
        self.kb = KnowledgeBase(session_factory, self.knowledge)
        # Whole folders (a Confluence export) imported into the knowledge base.
        self.imports = FolderImporter(self.kb)
        self.answers = AnswerCache(session_factory, self.knowledge, self.kb)
        self.skills = SkillCatalog(session_factory)
        # Results of integrations whose answers may be reused (`cache_seconds`).
        self.tool_results: TTLCache = TTLCache(512, 60.0)
        self.queue = SessionQueue()
        # Set by the responder, which can post in the conversation.
        self.jobs: JobService | None = None
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
        tools: list[ToolProvider] | None,
        shape: Shape,
    ) -> AgentResult:
        """The answer to `turn`, or `MERGED` if a later turn answered it too.

        `tools` are offered besides the memory and profile tools, which the
        runner adds for the conversation's own scope.
        """
        key, kind = session_key(turn.message, turn.thread_id)
        collect = snapshot.agent.queue_mode == QueueMode.collect.value

        async def handle(turns: list[AgentTurn]) -> AgentResult:
            return await self._run(key, kind, turns, snapshot, chat, tools, shape)

        turn.timer.mark("queued")
        return await self.queue.submit(key, turn, handle, collect=collect)

    async def reset(self, message: MessageContext, thread_id: str | None) -> bool:
        """Start the conversation afresh, forgetting the documents sent in it too."""
        key = self.key_for(message, thread_id)
        documents = await self.kb.forget_scope(conversation_scope(key))
        return await self.sessions.reset(key) or bool(documents)

    def documents_scope(self, message: MessageContext, thread_id: str | None) -> str:
        """Where documents sent in this conversation are kept."""
        return conversation_scope(self.key_for(message, thread_id))

    # ----------------------------------------------------------------- #

    async def _run(
        self,
        key: str,
        kind: str,
        turns: list[AgentTurn],
        snapshot: ConfigSnapshot,
        chat: LLMChat,
        tools: list[ToolProvider] | None,
        shape: Shape,
    ) -> AgentResult:
        turn = turns[-1]
        timer, live = turn.timer, turn.live
        timer.mark("turn_started")
        # Waiting for earlier turns of the conversation is not this turn's work.
        timer.add_wait("queue", (timer.marks["turn_started"] - timer.marks.get("queued", 0)) / 1000)
        question = "\n".join(t.question for t in turns)
        settings = snapshot.agent

        memory_on = snapshot.memory.enabled
        knowledge_on = snapshot.knowledge.enabled
        scopes = MemoryScopes.for_conversation(turn.message, kind)
        documents = conversation_scope(key)
        # Profiles are part of what is remembered, and private to DMs.
        direct = memory_on and kind == "dm" and bool(turn.message.sender_id)
        if memory_on:
            self.memory.ensure_indexed(snapshot.embedding)
        if knowledge_on:
            self.kb.ensure_indexed(snapshot.embedding)
            self.kb.ensure_pipeline()
            self.answers.ensure_indexed(snapshot.embedding)

        # A question to look up, a request to do something, or small talk?
        wording = turn.message.clean_text or turn.message.text
        if turn.intent:
            intent = Intent(turn.intent, "given")
        else:
            intent = await detect(
                wording,
                mode=snapshot.knowledge.intent_mode,
                chat=chat,
                fast_model=snapshot.agent.fast_model,
            )
        timer.labels["intent"] = intent.kind
        timer.labels["intent_by"] = intent.by
        timer.mark("intent")

        if knowledge_on and intent.kind == LOOKUP and len(turns) == 1 and not turn.fresh:
            kept = await self._kept_answer(wording, documents, snapshot, timer)
            if kept is not None:
                return await self._give_kept(kept, key, kind, question, turn, snapshot)

        # The conversation, its notes, the person's profile and the passages
        # for the question, side by side.
        with timer.waiting("retrieval" if memory_on or knowledge_on else "db"):
            state, notes, profile, (lookup, available) = await asyncio.gather(
                self.sessions.open(
                    key, kind, turn.message, idle_reset_minutes=settings.idle_reset_minutes
                ),
                self._recall(question, scopes, snapshot) if memory_on else _nothing(),
                self.profiles.get(turn.message.sender_id) if direct else _nothing(),
                self._look_up(wording, intent, documents, snapshot, chat)
                if knowledge_on
                else _no_lookup(),
            )
        timer.mark("session_loaded")
        passages = lookup.passages if lookup is not None else []
        if lookup is not None:
            timer.labels["kb"] = lookup.source or "none"
        # Taken before answering: an answer kept later must be from these documents.
        kb_version = (
            await self.kb.base_version()
            if lookup is not None and lookup.source == "base" and passages
            else None
        )
        recent = state.history(LEARN_HISTORY)
        context = ""
        if passages:
            context = passages_block([(p.label, p.shown) for p in passages], lookup.source or "")
            timer.labels["judged"] = "yes" if lookup.judged else "no"
        elif intent.kind == COMMAND:
            context = COMMAND_NOTE
        messages = build_messages(
            chat.system_prompt,
            bot_name=snapshot.settings.bot_username or "the bot",
            timezone_name=(profile and profile.timezone) or snapshot.settings.default_timezone,
            summary=state.summary,
            history=state.history(snapshot.llm.history_messages),
            question=question,
            profile=profile.prompt_text() if profile else "",
            notes=[(note.id, note.text) for note in notes or ()],
            context=context,
        )
        timer.mark("prompt_built")

        providers = list(tools or ())
        if memory_on:
            providers.append(
                MemoryTools(
                    self.memory,
                    scopes,
                    snapshot.embedding,
                    actor=turn.message.sender_username,
                    session_id=state.id,
                    in_channel=kind != "dm",
                )
            )
        if direct:
            providers.append(
                ProfileTools(self.profiles, turn.message.sender_id, turn.message.sender_username)
            )
        if available:
            providers.append(
                KnowledgeTools(
                    self.kb,
                    documents,
                    snapshot.embedding,
                    available=available,
                    min_score=snapshot.knowledge.min_score,
                )
            )
        if snapshot.mcp_tools:
            providers.append(McpTools(snapshot.mcp_tools))
        skills = await self._skills()
        if skills:
            providers.append(SkillTools(skills, turn.message.template_vars()))
        jobs_on = self.jobs is not None and settings.jobs_enabled
        job_tools = None
        if jobs_on:
            job_tools = JobTools(
                self.jobs,  # type: ignore[arg-type]
                message=turn.message,
                question=question,
                thread_id=turn.thread_id,
                session_key=key,
                session_kind=kind,
                snapshot=snapshot,
            )
            providers.append(job_tools)
        # The tools this person may use (`app.agent.policy`).
        registry = ToolRegistry(
            providers, permit=snapshot.policy.for_user(turn.message.sender_username)
        )

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
        deadline = settings.turn_deadline_seconds
        # Past this, the work goes on as a job instead of being cut off.
        hand_off = jobs_on and 0 < settings.job_after_seconds < deadline
        if hand_off:
            deadline = settings.job_after_seconds
        try:
            async with asyncio.timeout(deadline):
                raw = await chat.complete(
                    messages,
                    on_text=None if live is None else on_text,
                    on_event=on_event,
                    tools=registry or None,
                    parallel_tools=settings.parallel_tools,
                    timer=timer,
                )
        except TimeoutError:
            log.info("Turn in %s hit the %gs deadline", key, deadline)
            if hand_off and not (job_tools and job_tools.started):
                handed = await self._hand_off(
                    turn, question, recent, key, kind, snapshot, chat, state.id, len(turns)
                )
                if handed is not None:
                    return handed
            status, raw = "timeout", so_far
            error = f"stopped after {deadline:g}s"
        except LLMError as exc:
            log.info("LLM reply failed: %s", exc)
            return AgentResult("", "error", str(exc), state.id, chat.model, len(turns))
        timer.mark("llm_done")

        answer = strip_thinking(raw).strip()
        learning = None
        if (
            answer
            and status == "ok"
            and kb_version is not None
            and snapshot.knowledge.answer_cache
            and len(turns) == 1
            # The knowledge base alone: no tool, and passages the model judged to answer.
            and not timer.tools
            and (lookup.judged or not snapshot.knowledge.llm_judge)
        ):
            learning = self._background_task(
                self.answers.learn(
                    chat,
                    question=wording,
                    history=recent,
                    passages=passages,
                    kb_version=kb_version,
                    settings=snapshot.knowledge,
                    embedding=snapshot.embedding,
                    model=snapshot.agent.fast_model or None,
                    created_by=turn.message.sender_username,
                ),
                f"keep-answer-{key}",
            )
        if answer:
            with timer.waiting("db"):
                await self.sessions.append(
                    state, question, answer, sender_username=turn.message.sender_username
                )
            timer.mark("persisted")
            self._maybe_compact(state, snapshot, chat)
            if memory_on and snapshot.memory.auto_extract and status == "ok":
                self._background_task(
                    self._extract(question, answer, notes or [], scopes, turn, state, snapshot, chat),
                    f"remember-{key}",
                )
        # The transcript keeps the model's words; the reply also lists the sources.
        shown = with_sources(answer, passages) if answer else answer
        return AgentResult(
            shown, status, error, state.id, chat.model, len(turns), learning=learning
        )

    async def _hand_off(
        self,
        turn: AgentTurn,
        question: str,
        recent: list,
        key: str,
        kind: str,
        snapshot: ConfigSnapshot,
        chat: LLMChat,
        session_id: int | None,
        turns: int,
    ) -> AgentResult | None:
        """Go on with a turn that ran long as a job; None if it cannot."""
        assert self.jobs is not None
        plan = await self.jobs.plan(chat, question, recent, snapshot)
        if plan is None:
            return None
        try:
            job = await self.jobs.create(
                plan,
                message=turn.message,
                question=question,
                thread_id=turn.thread_id,
                session_key=key,
                session_kind=kind,
                snapshot=snapshot,
                started_by="deadline",
            )
        except JobError as exc:
            log.info("Could not hand the turn in %s over to a job: %s", key, exc)
            return None
        self.jobs.start(job.id)
        turn.timer.labels["job"] = str(job.id)
        text = (
            f":hourglass_flowing_sand: This takes more than one reply, so I am working on it as "
            f"job #{job.id} in {len(plan.steps)} steps. Progress and the answer will appear here."
        )
        return AgentResult(text, "ok", None, session_id, chat.model, turns)

    async def _skills(self) -> list:
        try:
            return await self.skills.available()
        except Exception:  # noqa: BLE001 - answer without skills rather than not at all
            log.warning("Could not read the skills folder", exc_info=True)
            return []

    async def _kept_answer(
        self, wording: str, documents: str, snapshot: ConfigSnapshot, timer: TurnTimer
    ) -> CachedAnswer | None:
        """The answer kept for this question, unless the conversation has documents of its own."""
        with timer.waiting("retrieval"):
            if await self.kb.scopes_with_documents([documents]):
                return None
            found = await self.answers.find(
                wording, embedding=snapshot.embedding, settings=snapshot.knowledge
            )
        timer.labels["kept"] = found.kind if found else "miss"
        return found

    async def _give_kept(
        self,
        kept: CachedAnswer,
        key: str,
        kind: str,
        question: str,
        turn: AgentTurn,
        snapshot: ConfigSnapshot,
    ) -> AgentResult:
        """Answer with a kept answer, and add the exchange to the conversation."""
        timer = turn.timer
        timer.labels["kb"] = "kept"
        with timer.waiting("db"):
            state = await self.sessions.open(
                key, kind, turn.message, idle_reset_minutes=snapshot.agent.idle_reset_minutes
            )
            await self.sessions.append(
                state, question, kept.answer, sender_username=turn.message.sender_username
            )
        timer.mark("persisted")
        log.info(
            "Answered in %s from kept answer %d (%s%s)",
            key,
            kept.entry_id,
            kept.matched_by,
            f" {kept.score:.3f}" if kept.score is not None else "",
        )
        return AgentResult(kept.render(), "ok", None, state.id, "kept answer", 1, cached=kept)

    async def _look_up(
        self,
        question: str,
        intent: Intent,
        documents: str,
        snapshot: ConfigSnapshot,
        chat: LLMChat | None = None,
    ) -> tuple[Lookup | None, set[str]]:
        """Passages for a question to look up, and which documents the conversation can search."""
        knowledge = snapshot.knowledge
        judge_with = None
        if knowledge.llm_judge and chat is not None:
            judge_with = (chat, knowledge.index_model or snapshot.agent.fast_model or None)
        try:
            if intent.kind != LOOKUP:
                return None, await self.kb.scopes_with_documents([documents, BASE_SCOPE])
            lookup = await self.kb.lookup(
                question,
                documents,
                embedding=snapshot.embedding,
                limit=snapshot.knowledge.top_k,
                min_score=snapshot.knowledge.min_score,
                # Sent with a document, or talking about one.
                about_document=intent.by == "given" or about_a_document(question),
                judge_with=judge_with,
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - answer without passages rather than not at all
            log.warning("Could not look up %r in the documents", question[:80], exc_info=True)
            return None, set()
        return lookup, lookup.available

    async def _recall(
        self, question: str, scopes: MemoryScopes, snapshot: ConfigSnapshot
    ) -> list[Note]:
        try:
            return await self.memory.recall(
                question,
                scopes.read,
                embedding=snapshot.embedding,
                limit=snapshot.memory.prompt_notes,
                min_score=snapshot.memory.min_score,
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - answer without notes rather than not at all
            log.warning("Could not recall notes for %s", scopes.write, exc_info=True)
            return []

    def _background_task(self, coroutine, name: str) -> asyncio.Task:
        task = asyncio.create_task(coroutine, name=name)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return task

    # ----------------------------------------------------------------- #
    # Remembering, after the reply
    # ----------------------------------------------------------------- #

    async def _extract(
        self,
        question: str,
        answer: str,
        known: list[Note],
        scopes: MemoryScopes,
        turn: AgentTurn,
        state: SessionState,
        snapshot: ConfigSnapshot,
        chat: LLMChat,
    ) -> None:
        """Ask the fast model which facts of this exchange to keep, and keep them."""
        try:
            remembered = "\n".join(f"- {note.text}" for note in known) or "(none)"
            raw = await chat.complete(
                [
                    {"role": "system", "content": EXTRACT_PROMPT},
                    {
                        "role": "user",
                        "content": f"Already remembered:\n{remembered}\n\n"
                        f"User: {question}\n\nAssistant: {answer}",
                    },
                ],
                model=snapshot.agent.fast_model or None,
                max_tokens=EXTRACT_MAX_TOKENS,
                quick=True,
            )
            for note in parse_notes(strip_thinking(raw))[:EXTRACT_MAX_NOTES]:
                try:
                    saved, created = await self.memory.add(
                        scopes.write,
                        note,
                        embedding=snapshot.embedding,
                        source="auto",
                        created_by=turn.message.sender_username,
                        session_id=state.id,
                    )
                except NoteError as exc:
                    log.info("Did not keep an extracted note: %s", exc)
                    continue
                if created:
                    log.info("Remembered note %d in %s (extracted)", saved.id, scopes.write)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - nothing is remembered this time
            log.warning("Could not extract notes for %s", scopes.write, exc_info=True)

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
                quick=True,
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
        if self.jobs is not None:
            await self.jobs.close()
        await self.answers.close()
        await self.imports.close()
        await self.memory.close()
        await self.kb.close()
        await self.knowledge.aclose()


async def _nothing() -> None:
    return None


async def _no_lookup() -> tuple[None, set[str]]:
    return None, set()


_CITATION = re.compile(r"\[(\d{1,2})\]")


def with_sources(answer: str, passages: list[Passage]) -> str:
    """`answer` with the passages it cites ([1], [2], ...) listed after it."""
    if not passages:
        return answer
    cited = sorted(
        {int(n) for n in _CITATION.findall(answer) if 1 <= int(n) <= len(passages)}
    )
    if not cited:
        return answer
    sources = " · ".join(f"[{n}] {passages[n - 1].label}" for n in cited)
    return f"{answer}\n\n_Sources: {sources}_"


def parse_notes(raw: str) -> list[str]:
    """The notes in the fast model's JSON answer; [] if there are none or it is not JSON."""
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return []
    try:
        data = json.loads(raw[start : end + 1])
    except ValueError:
        return []
    notes = data.get("notes") if isinstance(data, dict) else None
    if not isinstance(notes, list):
        return []
    return [note.strip() for note in notes if isinstance(note, str) and note.strip()]


def _ms(value: float | None) -> int | None:
    return None if value is None else int(round(value))


__all__ = ["MERGED", "AgentResult", "AgentRunner", "AgentTurn"]
