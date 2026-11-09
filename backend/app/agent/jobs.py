"""Work that takes several steps: a job, run one phase at a time.

A job starts in one of two ways:

- the model calls the `start_job` tool with a title and its steps, for
  work it sees will take more than one reply ("compare these three
  policies and write a summary");
- a turn is still running after `job_after_seconds`: the fast model splits
  the request into steps, and the reply says the work goes on as a job.

Each phase is a turn of its own, with the request, the plan, the results of
the phases before it and the same tools as a chat turn (knowledge base,
integrations), under the turn deadline. Its output is stored as soon as it
is done, so a restart goes on from the phase that was under way. The last
phase writes the answer, which is posted in the conversation and added to
its transcript.

While it runs, a checklist message in the conversation is edited as each
phase starts and ends. People see and stop their jobs with `job list`,
`job status <id>` and `job cancel <id>`; admins on the portal's Jobs page.
Jobs run apart from the conversation: the person can keep chatting.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.agent.prompt import system_text
from app.agent.tools import ToolRegistry, tool_result
from app.agent.tools.integrations import IntegrationTools
from app.agent.tools.knowledge import KnowledgeTools
from app.agent.tools.mcp import McpTools
from app.agent.tools.skills import SkillTools
from app.bot.llm import LLMError
from app.bot.rcformat import strip_thinking, to_rocketchat
from app.bot.streaming import deliver
from app.bot.types import MessageContext
from app.knowledge.base import BASE_SCOPE, conversation_scope
from app.models import AgentJob, AgentJobPhase, JobStatus
from app.rocketchat.rest import RocketChatError

if TYPE_CHECKING:
    from app.agent.runner import AgentRunner
    from app.bot.config_store import ConfigSnapshot
    from app.bot.llm import LLMChat
    from app.rocketchat.gateway import Gateway

log = logging.getLogger(__name__)

# Jobs running at the same time, for everyone; the rest wait their turn.
RUNNING_AT_ONCE = 2
# Tries per phase before the job fails.
PHASE_ATTEMPTS = 2
# Of each earlier phase's output, this much is shown to the next ones.
EARLIER_CHARS = 3000
EARLIER_TOTAL_CHARS = 12000
PLAN_TIMEOUT_SECONDS = 30.0
PLAN_MAX_TOKENS = 800
MAX_TITLE_CHARS = 120
MAX_STEP_CHARS = 400
UNDER_WAY = (JobStatus.planned.value, JobStatus.running.value)

PLAN_PROMPT = """You plan work an assistant will do in steps, each a separate turn of under a minute.
The assistant can search the knowledge base and call the integrations it has.
Split the user's request into 2 to {most} steps. Each step says what to find out or
produce; the last step writes the final answer for the user. Write in the user's
language. Reply with JSON only: {{"title": "...", "steps": ["...", "..."]}}"""

PHASE_PROMPT = """You are carrying out one step of a job done in several steps.
Do only the current step, using your tools where they help, and reply with its
result: the facts found, or the text produced, with sources where you have them.
Results of earlier steps are given; do not repeat their work."""

LAST_PHASE_NOTE = (
    "This is the last step: write the final answer to the request for the user, "
    "from the results above. Write it as you would answer in chat."
)

ICONS = {
    "waiting": ":white_small_square:",
    "running": ":hourglass_flowing_sand:",
    "done": ":white_check_mark:",
    "failed": ":x:",
}


class JobError(ValueError):
    """A job was refused; the message says why, for the person asking."""


@dataclass(slots=True)
class JobPlan:
    title: str
    steps: list[str]


def clean_plan(title: object, steps: object, most: int) -> JobPlan | None:
    """A usable plan from what the model gave, or None."""
    if not isinstance(steps, list):
        return None
    cleaned = [" ".join(str(s).split())[:MAX_STEP_CHARS] for s in steps if str(s).strip()]
    cleaned = cleaned[: max(1, most)]
    if not cleaned:
        return None
    name = " ".join(str(title or "").split())[:MAX_TITLE_CHARS] or cleaned[0][:MAX_TITLE_CHARS]
    return JobPlan(name, cleaned)


def _json_object(raw: str) -> dict | None:
    raw = strip_thinking(raw)
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(raw[start : end + 1])
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def message_to_json(message: MessageContext) -> str:
    return json.dumps(
        {
            "message_id": message.message_id,
            "room_id": message.room_id,
            "text": message.text,
            "sender_id": message.sender_id,
            "sender_username": message.sender_username,
            "room_type": message.room_type,
            "thread_id": message.thread_id,
            "clean_text": message.clean_text,
            "mentioned_bot": message.mentioned_bot,
        }
    )


def message_from_json(raw: str) -> MessageContext:
    data = json.loads(raw or "{}")
    return MessageContext(
        message_id=data.get("message_id", ""),
        room_id=data.get("room_id", ""),
        text=data.get("text", ""),
        sender_id=data.get("sender_id"),
        sender_username=data.get("sender_username"),
        room_type=data.get("room_type"),
        thread_id=data.get("thread_id"),
        clean_text=data.get("clean_text", ""),
        mentioned_bot=bool(data.get("mentioned_bot")),
    )


def _elapsed(job: AgentJob) -> str:
    if job.started_at is None:
        return ""
    end = job.ended_at or datetime.now(timezone.utc)
    start = job.started_at if job.started_at.tzinfo else job.started_at.replace(tzinfo=timezone.utc)
    end = end if end.tzinfo else end.replace(tzinfo=timezone.utc)
    seconds = max(0, int((end - start).total_seconds()))
    return f"{seconds // 60}m {seconds % 60:02d}s" if seconds >= 60 else f"{seconds}s"


def progress_text(job: AgentJob, waiting_for_slot: bool = False) -> str:
    """The checklist shown in the conversation while the job runs."""
    head = f":gear: *Job #{job.id}: {job.title}*"
    lines = [head]
    for phase in job.phases:
        lines.append(f"{ICONS.get(phase.status, ICONS['waiting'])} {phase.ord}. {phase.goal}")
    status = job.status
    if status == JobStatus.done.value:
        lines.append(f"_Done in {_elapsed(job)}; the answer is below._")
    elif status == JobStatus.failed.value:
        lines.append(f"_Stopped: {job.error or 'a step failed'}._")
    elif status == JobStatus.cancelled.value:
        lines.append("_Cancelled._")
    elif waiting_for_slot:
        lines.append(f"_Waiting for other jobs to finish · `job cancel {job.id}` to stop it._")
    else:
        lines.append(f"_`job status {job.id}` for details · `job cancel {job.id}` to stop it._")
    return "\n".join(lines)


def _earlier_results(phases: Sequence[AgentJobPhase]) -> str:
    parts: list[str] = []
    budget = EARLIER_TOTAL_CHARS
    for phase in phases:
        output = phase.output.strip() or "(nothing)"
        if len(output) > EARLIER_CHARS:
            output = output[:EARLIER_CHARS].rstrip() + " …"
        part = f"### Step {phase.ord}: {phase.goal}\n{output}"
        if len(part) > budget:
            part = part[:budget]
        parts.append(part)
        budget -= len(part)
        if budget <= 0:
            break
    return "\n\n".join(parts)


# (settings, the model to use or None when none is set up)
ModelAccess = Callable[[], Awaitable["tuple[ConfigSnapshot, LLMChat | None]"]]
GatewayProvider = Callable[[], "Gateway | None"]


class JobService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        agent: AgentRunner,
        model_access: ModelAccess,
        gateway_provider: GatewayProvider,
    ) -> None:
        self._session_factory = session_factory
        self.agent = agent
        self._model_access = model_access
        self._gateway = gateway_provider
        self._tasks: dict[int, asyncio.Task] = {}
        self._cancelling: set[int] = set()
        self._slots = asyncio.Semaphore(RUNNING_AT_ONCE)

    # ----------------------------------------------------------------- #
    # Starting
    # ----------------------------------------------------------------- #

    async def plan(
        self,
        chat: LLMChat,
        question: str,
        history: Sequence[dict],
        snapshot: ConfigSnapshot,
    ) -> JobPlan | None:
        """The fast model's steps for `question`; None if it gave none usable."""
        said = "\n".join(
            f"{m.get('role')}: {str(m.get('content') or '')[:500]}" for m in history[-4:]
        )
        try:
            async with asyncio.timeout(PLAN_TIMEOUT_SECONDS):
                raw = await chat.complete(
                    [
                        {
                            "role": "system",
                            "content": PLAN_PROMPT.format(most=snapshot.agent.job_max_phases),
                        },
                        {
                            "role": "user",
                            "content": f"Conversation:\n{said or '(none)'}\n\nRequest: {question}",
                        },
                    ],
                    model=snapshot.agent.fast_model or None,
                    max_tokens=PLAN_MAX_TOKENS,
                    quick=True,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the turn ends as it would have
            log.info("Could not plan a job for %r: %s", question[:80], exc)
            return None
        data = _json_object(raw)
        if data is None:
            return None
        return clean_plan(data.get("title"), data.get("steps"), snapshot.agent.job_max_phases)

    async def create(
        self,
        plan: JobPlan,
        *,
        message: MessageContext,
        question: str,
        thread_id: str | None,
        session_key: str,
        session_kind: str,
        snapshot: ConfigSnapshot,
        started_by: str = "tool",
    ) -> AgentJob:
        """Store a new job; `start` runs it. JobError if the person has too many."""
        settings = snapshot.agent
        if not settings.jobs_enabled:
            raise JobError("jobs are turned off")
        steps = plan.steps[: max(1, settings.job_max_phases)]
        async with self._session_factory() as db:
            if message.sender_id:
                under_way = await db.scalar(
                    select(func.count())
                    .select_from(AgentJob)
                    .where(
                        AgentJob.owner_rc_user_id == message.sender_id,
                        AgentJob.status.in_(UNDER_WAY),
                    )
                )
                if (under_way or 0) >= settings.jobs_per_user:
                    raise JobError(
                        f"you already have {under_way} job(s) under way; wait for them, "
                        "or stop one with `job cancel <id>`"
                    )
            job = AgentJob(
                status=JobStatus.planned.value,
                title=plan.title,
                question=question,
                session_key=session_key,
                session_kind=session_kind,
                room_id=message.room_id,
                thread_id=thread_id,
                owner_rc_user_id=message.sender_id,
                owner_username=message.sender_username,
                message_json=message_to_json(message),
                started_by=started_by,
                current_phase=0,
                result="",
            )
            job.phases = [
                AgentJobPhase(ord=n, goal=goal, status="waiting", output="", attempts=0)
                for n, goal in enumerate(steps, start=1)
            ]
            db.add(job)
            await db.commit()
            await db.refresh(job, ["phases"])
        log.info("Job %d for @%s: %s (%d steps)", job.id, message.sender_username, plan.title, len(steps))
        return job

    def start(self, job_id: int) -> None:
        """Run the job in the background, unless it already runs."""
        task = self._tasks.get(job_id)
        if task is not None and not task.done():
            return
        task = asyncio.create_task(self._run(job_id), name=f"job-{job_id}")
        self._tasks[job_id] = task
        task.add_done_callback(lambda _t, job_id=job_id: self._tasks.pop(job_id, None))

    async def resume(self) -> int:
        """Start again the jobs a restart interrupted; how many."""
        async with self._session_factory() as db:
            ids = list(
                (
                    await db.scalars(
                        select(AgentJob.id).where(AgentJob.status.in_(UNDER_WAY)).order_by(AgentJob.id)
                    )
                ).all()
            )
        for job_id in ids:
            self.start(job_id)
        if ids:
            log.info("Resumed %d job(s): %s", len(ids), ", ".join(map(str, ids)))
        return len(ids)

    # ----------------------------------------------------------------- #
    # Looking and stopping
    # ----------------------------------------------------------------- #

    async def get(self, job_id: int) -> AgentJob | None:
        async with self._session_factory() as db:
            return await db.get(AgentJob, job_id, options=[selectinload(AgentJob.phases)])

    async def list(
        self, *, owner: str | None = None, limit: int = 100, under_way: bool = False
    ) -> list[AgentJob]:
        async with self._session_factory() as db:
            statement = select(AgentJob).options(selectinload(AgentJob.phases))
            if owner is not None:
                statement = statement.where(AgentJob.owner_rc_user_id == owner)
            if under_way:
                statement = statement.where(AgentJob.status.in_(UNDER_WAY))
            statement = statement.order_by(AgentJob.id.desc()).limit(limit)
            return list((await db.scalars(statement)).all())

    def running(self, job_id: int) -> bool:
        task = self._tasks.get(job_id)
        return task is not None and not task.done()

    async def cancel(self, job_id: int) -> bool:
        """Stop a job that is under way; False if it was not."""
        async with self._session_factory() as db:
            job = await db.get(AgentJob, job_id)
            if job is None or job.status not in UNDER_WAY:
                return False
            job.status = JobStatus.cancelled.value
            job.ended_at = datetime.now(timezone.utc)
            await db.execute(
                update(AgentJobPhase)
                .where(AgentJobPhase.job_id == job_id, AgentJobPhase.status == "running")
                .values(status="waiting")
            )
            await db.commit()
        task = self._tasks.get(job_id)
        if task is not None and not task.done():
            self._cancelling.add(job_id)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        else:
            await self._show_progress(job_id)
        log.info("Job %d cancelled", job_id)
        return True

    async def close(self) -> None:
        """Stop running jobs for now (the bot is stopping); they resume on the next start."""
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    # ----------------------------------------------------------------- #
    # Running
    # ----------------------------------------------------------------- #

    async def _run(self, job_id: int) -> None:
        try:
            job = await self.get(job_id)
            if job is None or job.status not in UNDER_WAY:
                return
            if job.progress_msg_id is None:
                await self._show_progress(job_id, waiting_for_slot=self._slots.locked())
            async with self._slots:
                await self._run_phases(job_id)
        except asyncio.CancelledError:
            if job_id in self._cancelling:
                self._cancelling.discard(job_id)
                await self._show_progress(job_id)
            raise
        except Exception as exc:  # noqa: BLE001 - the job fails, the bot goes on
            log.exception("Job %d failed", job_id)
            await self._fail(job_id, str(exc)[:300] or exc.__class__.__name__)

    async def _run_phases(self, job_id: int) -> None:
        async with self._session_factory() as db:
            job = await db.get(AgentJob, job_id)
            if job is None or job.status not in UNDER_WAY:
                return
            job.status = JobStatus.running.value
            job.started_at = job.started_at or datetime.now(timezone.utc)
            await db.commit()
        while True:
            job = await self.get(job_id)
            if job is None or job.status != JobStatus.running.value:
                return
            pending = [p for p in job.phases if p.status != "done"]
            if not pending:
                await self._finish(job)
                return
            phase = pending[0]
            done = [p for p in job.phases if p.status == "done"]
            await self._set_phase(job_id, phase.id, status="running", started=True)
            await self._show_progress(job_id)
            output, error = await self._phase(job, phase, done, last=len(pending) == 1)
            if output is None:
                await self._set_phase(job_id, phase.id, status="failed", error=error)
                await self._fail(job_id, f"step {phase.ord} failed: {error}")
                return
            await self._set_phase(job_id, phase.id, status="done", output=output)

    async def _phase(
        self,
        job: AgentJob,
        phase: AgentJobPhase,
        done: Sequence[AgentJobPhase],
        *,
        last: bool,
    ) -> tuple[str | None, str | None]:
        """The phase's output, or (None, why it failed)."""
        snapshot, chat = await self._model_access()
        if chat is None:
            return None, "no language model is set up"
        message = message_from_json(job.message_json)
        plan = "\n".join(f"{p.ord}. {p.goal}" for p in job.phases)
        earlier = _earlier_results(done)
        step = f"Now do step {phase.ord}: {phase.goal}"
        if last:
            step += f"\n\n{LAST_PHASE_NOTE}"
        messages = [
            {
                "role": "system",
                "content": system_text(
                    chat.system_prompt,
                    bot_name=snapshot.settings.bot_username or "the bot",
                    timezone_name=snapshot.settings.default_timezone,
                )
                + f"\n\n{PHASE_PROMPT}",
            },
            {
                "role": "user",
                "content": f"Job: {job.title}\nRequest: {job.question}\n\nPlan:\n{plan}\n\n"
                f"Results so far:\n{earlier or '(none yet)'}\n\n{step}",
            },
        ]
        registry = await self._tools(job, message, snapshot)
        deadline = snapshot.agent.turn_deadline_seconds
        error = "no answer"
        for attempt in range(1, PHASE_ATTEMPTS + 1):
            await self._set_phase(job.id, phase.id, attempts=attempt)
            written = ""

            def keep(text: str) -> None:
                nonlocal written
                written = text

            try:
                async with asyncio.timeout(deadline):
                    raw = await chat.complete(
                        messages,
                        on_text=keep,
                        tools=registry or None,
                        parallel_tools=snapshot.agent.parallel_tools,
                    )
            except TimeoutError:
                partial = strip_thinking(written).strip()
                if partial:
                    log.info("Job %d step %d hit the %gs deadline; keeping what came", job.id, phase.ord, deadline)
                    return f"{partial}\n\n(cut off after {deadline:g}s)", None
                error = f"no answer within {deadline:g}s"
                continue
            except LLMError as exc:
                error = str(exc)
                continue
            output = strip_thinking(raw).strip()
            if output:
                return output, None
            error = "the model gave an empty answer"
        log.info("Job %d step %d failed after %d tries: %s", job.id, phase.ord, PHASE_ATTEMPTS, error)
        return None, error

    async def _tools(
        self, job: AgentJob, message: MessageContext, snapshot: ConfigSnapshot
    ) -> ToolRegistry:
        providers: list[Any] = [
            IntegrationTools(
                (spec for spec in snapshot.integrations.values() if spec.llm_tool),
                message.template_vars(),
                cache=self.agent.tool_results,
            )
        ]
        if snapshot.knowledge.enabled:
            documents = conversation_scope(job.session_key)
            available = await self.agent.kb.scopes_with_documents([documents, BASE_SCOPE])
            if available:
                providers.append(
                    KnowledgeTools(
                        self.agent.kb,
                        documents,
                        snapshot.embedding,
                        available=available,
                        min_score=snapshot.knowledge.min_score,
                    )
                )
        if snapshot.mcp_tools:
            providers.append(McpTools(snapshot.mcp_tools))
        skills = await self.agent._skills()
        if skills:
            providers.append(SkillTools(skills, message.template_vars()))
        # The tools of the person who started the job.
        return ToolRegistry(providers, permit=snapshot.policy.for_user(job.owner_username))

    async def _finish(self, job: AgentJob) -> None:
        snapshot, _ = await self._model_access()
        answer = job.phases[-1].output if job.phases else ""
        async with self._session_factory() as db:
            await db.execute(
                update(AgentJob)
                .where(AgentJob.id == job.id)
                .values(
                    status=JobStatus.done.value,
                    result=answer,
                    current_phase=len(job.phases),
                    ended_at=datetime.now(timezone.utc),
                )
            )
            await db.commit()
        shaped = to_rocketchat(answer) if snapshot.llm.format_for_rocketchat else strip_thinking(answer)
        await self._post(job, f"{shaped}\n\n_:white_check_mark: Job #{job.id}: {job.title}_")
        await self._show_progress(job.id)
        # The answer joins the conversation, so follow-up questions can use it.
        try:
            message = message_from_json(job.message_json)
            state = await self.agent.sessions.open(job.session_key, job.session_kind, message)
            asked = job.question if job.started_by == "deadline" else f"[Result of job #{job.id}: {job.title}]"
            await self.agent.sessions.append(
                state, asked, answer, sender_username=job.owner_username
            )
        except Exception:  # noqa: BLE001 - the answer was posted; only the transcript misses it
            log.warning("Could not add job %d's answer to its conversation", job.id, exc_info=True)
        log.info("Job %d done in %s", job.id, _elapsed(await self.get(job.id) or job))

    async def _fail(self, job_id: int, error: str) -> None:
        async with self._session_factory() as db:
            job = await db.get(AgentJob, job_id)
            if job is None or job.status not in UNDER_WAY:
                return
            job.status = JobStatus.failed.value
            job.error = error[:500]
            job.ended_at = datetime.now(timezone.utc)
            await db.commit()
        await self._show_progress(job_id)
        job = await self.get(job_id)
        if job is not None:
            await self._post(job, f":warning: Job #{job.id} stopped: {error}")

    async def _set_phase(
        self,
        job_id: int,
        phase_id: int,
        *,
        status: str | None = None,
        output: str | None = None,
        error: str | None = None,
        attempts: int | None = None,
        started: bool = False,
    ) -> None:
        values: dict[str, Any] = {}
        now = datetime.now(timezone.utc)
        if status is not None:
            values["status"] = status
            if status in ("done", "failed"):
                values["ended_at"] = now
        if started:
            values["started_at"] = now
        if output is not None:
            values["output"] = output
        if error is not None:
            values["error"] = error[:500]
        if attempts is not None:
            values["attempts"] = attempts
        async with self._session_factory() as db:
            await db.execute(update(AgentJobPhase).where(AgentJobPhase.id == phase_id).values(**values))
            if status == "running":
                ord_ = await db.scalar(select(AgentJobPhase.ord).where(AgentJobPhase.id == phase_id))
                await db.execute(update(AgentJob).where(AgentJob.id == job_id).values(current_phase=ord_))
            await db.commit()

    # ----------------------------------------------------------------- #
    # Showing it in the conversation
    # ----------------------------------------------------------------- #

    async def _post(self, job: AgentJob, text: str) -> None:
        gateway = self._gateway()
        if gateway is None:
            log.warning("Job %d: not connected, could not post to the conversation", job.id)
            return
        try:
            await deliver(gateway, job.room_id, text, thread_id=job.thread_id)
        except RocketChatError as exc:
            log.warning("Job %d: could not post to the conversation: %s", job.id, exc)

    async def _show_progress(self, job_id: int, *, waiting_for_slot: bool = False) -> None:
        """Post the checklist, or edit it to the job's current state."""
        job = await self.get(job_id)
        gateway = self._gateway()
        if job is None or gateway is None:
            return
        text = progress_text(job, waiting_for_slot)
        try:
            if job.progress_msg_id:
                await gateway.update_message(job.room_id, job.progress_msg_id, text)
                return
            sent = await gateway.send_message(job.room_id, text, thread_id=job.thread_id)
        except RocketChatError as exc:
            log.info("Job %d: could not show its progress: %s", job_id, exc)
            return
        if sent.message_id:
            async with self._session_factory() as db:
                await db.execute(
                    update(AgentJob).where(AgentJob.id == job_id).values(progress_msg_id=sent.message_id)
                )
                await db.commit()


# --------------------------------------------------------------------------- #
# The tool the model starts jobs with
# --------------------------------------------------------------------------- #


START_JOB = "start_job"


class JobTools:
    """`start_job`, for work that takes more than one reply."""

    def __init__(
        self,
        service: JobService,
        *,
        message: MessageContext,
        question: str,
        thread_id: str | None,
        session_key: str,
        session_kind: str,
        snapshot: ConfigSnapshot,
    ) -> None:
        self._service = service
        self._message = message
        self._question = question
        self._thread_id = thread_id
        self._session_key = session_key
        self._session_kind = session_kind
        self._snapshot = snapshot
        self.started: list[int] = []

    def tools(self) -> dict[str, dict[str, Any]]:
        most = self._snapshot.agent.job_max_phases
        return {
            START_JOB: {
                "description": (
                    "Start work that needs several steps and more than a minute, such as "
                    "researching several sources and writing a report, or comparing documents. "
                    "Each step runs as its own turn with your tools; progress and the final "
                    "answer appear in the conversation. Do not use it for questions you can "
                    "answer now."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "A short name for the job."},
                        "steps": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": f"2 to {most} steps in order; the last writes the final answer.",
                        },
                    },
                    "required": ["title", "steps"],
                },
            }
        }

    async def call(self, name: str, arguments: dict[str, Any]) -> str:
        if self.started:
            return tool_result({"error": f"job #{self.started[0]} is already started for this message"})
        plan = clean_plan(arguments.get("title"), arguments.get("steps"), self._snapshot.agent.job_max_phases)
        if plan is None:
            return tool_result({"error": "give a title and at least one step"})
        try:
            job = await self._service.create(
                plan,
                message=self._message,
                question=self._question,
                thread_id=self._thread_id,
                session_key=self._session_key,
                session_kind=self._session_kind,
                snapshot=self._snapshot,
                started_by="tool",
            )
        except JobError as exc:
            return tool_result({"error": str(exc)})
        self.started.append(job.id)
        self._service.start(job.id)
        return tool_result(
            {
                "job_id": job.id,
                "steps": len(plan.steps),
                "note": (
                    "Started. Tell the user in one or two sentences that you are working on it "
                    f"as job #{job.id} in {len(plan.steps)} steps, and that progress and the "
                    "answer will appear here. Do not do the work now."
                ),
            }
        )


_JOB_ID = re.compile(r"^#?(\d+)$")


def parse_job_id(text: str) -> int | None:
    match = _JOB_ID.match(text.strip())
    return int(match.group(1)) if match else None


__all__ = [
    "JobError",
    "JobPlan",
    "JobService",
    "JobTools",
    "clean_plan",
    "parse_job_id",
    "progress_text",
]
