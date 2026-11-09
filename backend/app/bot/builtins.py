"""Built-in commands - the behaviour that ships with the bot.

Portal-defined commands point at these by `builtin_name`, so an admin can
rename or re-trigger them (e.g. make `remind` fire on `remember`) without code
changes, and can disable any of them.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot.config_store import ConfigSnapshot
from app.bot.remind_parser import RemindParseError, parse_reminder
from app.bot.router import MatchResult
from app.bot.types import MessageContext
from app.models import ResponderMode, ScheduledTask
from app.rocketchat.gateway import Gateway
from app.scheduler.triggers import ScheduleError, describe
from app.timeutil import format_local, to_utc

if TYPE_CHECKING:  # avoid a circular import at runtime
    from app.agent.runner import AgentRunner
    from app.scheduler.service import SchedulerService

log = logging.getLogger(__name__)

MAX_TASKS_PER_USER = 50


@dataclass(slots=True)
class BuiltinContext:
    message: MessageContext
    match: MatchResult
    snapshot: ConfigSnapshot
    session_factory: async_sessionmaker[AsyncSession]
    gateway: Gateway | None = None
    scheduler: SchedulerService | None = None
    agent: AgentRunner | None = None


BuiltinHandler = Callable[[BuiltinContext], Awaitable[str]]
_REGISTRY: dict[str, BuiltinHandler] = {}


def builtin(name: str) -> Callable[[BuiltinHandler], BuiltinHandler]:
    def decorator(func: BuiltinHandler) -> BuiltinHandler:
        _REGISTRY[name] = func
        return func

    return decorator


def get_builtin(name: str | None) -> BuiltinHandler | None:
    return _REGISTRY.get(name or "")


def builtin_names() -> list[str]:
    return sorted(_REGISTRY)


# --------------------------------------------------------------------------- #
# help / status
# --------------------------------------------------------------------------- #


@builtin("help")
async def help_command(ctx: BuiltinContext) -> str:
    entries = ctx.snapshot.router.help_entries()
    if not entries:
        return "No commands are configured yet. An admin can add them in the portal."

    width = max(len(usage) for usage, _ in entries)
    lines = [
        f"`{usage.ljust(width)}`  {description}" if description else f"`{usage}`"
        for usage, description in entries
    ]
    greeting = ctx.snapshot.settings.greeting.strip()
    header = f"{greeting}\n\n" if greeting else ""
    return f"{header}**What I can do**\n" + "\n".join(lines)


@builtin("status")
async def status_command(ctx: BuiltinContext) -> str:
    settings = ctx.snapshot.settings
    async with ctx.session_factory() as session:
        total = await session.scalar(select(func.count()).select_from(ScheduledTask)) or 0
        mine = 0
        if ctx.message.sender_id:
            mine = (
                await session.scalar(
                    select(func.count())
                    .select_from(ScheduledTask)
                    .where(ScheduledTask.owner_rc_user_id == ctx.message.sender_id)
                    .where(ScheduledTask.enabled.is_(True))
                )
                or 0
            )

    scheduler_state = (
        "running" if ctx.scheduler and ctx.scheduler.running else "stopped"
    )
    responder = f"`{settings.responder_mode}`"
    if settings.responder_mode == ResponderMode.llm.value:
        responder += f" ({ctx.snapshot.llm.model or 'no model configured'})"
    return "\n".join(
        [
            "**Bot status**",
            f"- responder: {responder}",
            f"- commands loaded: {len(ctx.snapshot.router)}",
            f"- integrations: {len(ctx.snapshot.integrations)}",
            f"- scheduler: {scheduler_state}",
            f"- reminders: {mine} of yours, {total} total",
            f"- server time: {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC",
        ]
    )


# --------------------------------------------------------------------------- #
# new
# --------------------------------------------------------------------------- #


@builtin("new")
async def new_command(ctx: BuiltinContext) -> str:
    """Forget the conversation so far (this DM, channel or thread)."""
    if ctx.agent is None:
        return "There is no conversation to start afresh."
    await ctx.agent.reset(ctx.message, ctx.message.thread_id)
    return "Started a new conversation. I have forgotten what we said before."


# --------------------------------------------------------------------------- #
# remind
# --------------------------------------------------------------------------- #


@builtin("remind")
async def remind_command(ctx: BuiltinContext) -> str:
    if not ctx.message.sender_id:
        return "I cannot tell who you are, so I will not create a reminder."

    args = ctx.match.args_text or ctx.match.named.get("rest", "")
    try:
        parsed = parse_reminder(
            args, default_timezone=ctx.snapshot.settings.default_timezone
        )
    except RemindParseError as exc:
        return f":warning: {exc}"

    room_id: str | None = None
    if parsed.channel_name:
        if ctx.gateway is None:
            return ":warning: I am not connected to Rocket.Chat right now."
        try:
            room_id = await ctx.gateway.resolve_channel_room(parsed.channel_name)
        except Exception as exc:  # noqa: BLE001 - surfaced to the user
            return f":warning: I cannot post to #{parsed.channel_name}: {exc}"
    elif not ctx.message.is_direct:
        # Asked in a channel without naming one: remind here.
        room_id = ctx.message.room_id

    async with ctx.session_factory() as session:
        existing = (
            await session.scalar(
                select(func.count())
                .select_from(ScheduledTask)
                .where(ScheduledTask.owner_rc_user_id == ctx.message.sender_id)
            )
            or 0
        )
        if existing >= MAX_TASKS_PER_USER:
            return (
                f":warning: You already have {existing} reminders "
                f"(limit {MAX_TASKS_PER_USER}). Delete one with `tasks delete <id>`."
            )

        task = ScheduledTask(
            title=parsed.title,
            owner_rc_user_id=ctx.message.sender_id,
            owner_username=ctx.message.sender_username,
            room_id=room_id,
            dm_username=None if room_id else ctx.message.sender_username,
            schedule_kind=parsed.schedule_kind,
            time_of_day=parsed.time_of_day,
            days_of_week=parsed.days_of_week,
            cron_expr=parsed.cron_expr,
            run_at=to_utc(parsed.run_at),
            timezone_name=parsed.timezone_name,
            message=parsed.message,
            enabled=True,
            created_via="chat",
        )
        session.add(task)
        try:
            await session.flush()
            schedule_text = describe(task)
        except ScheduleError as exc:
            await session.rollback()
            return f":warning: {exc}"
        await session.commit()
        task_id = task.id

    next_run = None
    if ctx.scheduler is not None:
        next_run = await ctx.scheduler.sync_task(task_id)

    where = f"#{parsed.channel_name}" if parsed.channel_name else (
        "this channel" if room_id else "a DM"
    )
    lines = [
        f":white_check_mark: Reminder **#{task_id}** saved - {schedule_text}, in {where}.",
        f"> {parsed.message}",
    ]
    if next_run:
        lines.append(f"Next run: {format_local(next_run, parsed.timezone_name)}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# tasks
# --------------------------------------------------------------------------- #

TASKS_USAGE = (
    "Usage: `tasks` | `tasks delete <id>` | `tasks off <id>` | `tasks on <id>`"
)


@builtin("tasks")
async def tasks_command(ctx: BuiltinContext) -> str:
    if not ctx.message.sender_id:
        return "I cannot tell who you are, so I cannot list your reminders."

    argv = ctx.match.args
    action = argv[0].casefold() if argv else "list"

    if action in {"list", "ls"}:
        return await _list_tasks(ctx)
    if action in {"delete", "del", "rm", "cancel"}:
        return await _mutate_task(ctx, argv[1:], mode="delete")
    if action in {"off", "disable", "pause"}:
        return await _mutate_task(ctx, argv[1:], mode="off")
    if action in {"on", "enable", "resume"}:
        return await _mutate_task(ctx, argv[1:], mode="on")
    return f":warning: Unknown action `{action}`.\n{TASKS_USAGE}"


async def _list_tasks(ctx: BuiltinContext) -> str:
    async with ctx.session_factory() as session:
        tasks = list(
            (
                await session.scalars(
                    select(ScheduledTask)
                    .where(ScheduledTask.owner_rc_user_id == ctx.message.sender_id)
                    .order_by(ScheduledTask.id)
                )
            ).all()
        )
    if not tasks:
        return "You have no reminders yet. Try `remind daily 09:00 stand-up`."

    lines = ["**Your reminders**"]
    for task in tasks:
        flag = "" if task.enabled else " _(paused)_"
        try:
            schedule_text = describe(task)
        except ScheduleError as exc:
            schedule_text = f"invalid schedule ({exc})"
        next_run = (
            f" - next {format_local(task.next_run_at, task.timezone_name)}"
            if task.next_run_at
            else ""
        )
        lines.append(f"**#{task.id}** {task.title}{flag}\n  {schedule_text}{next_run}")
    lines.append(f"\n{TASKS_USAGE}")
    return "\n".join(lines)


async def _mutate_task(ctx: BuiltinContext, rest: list[str], *, mode: str) -> str:
    if not rest:
        return f":warning: Which reminder? {TASKS_USAGE}"
    try:
        task_id = int(rest[0].lstrip("#"))
    except ValueError:
        return f":warning: `{rest[0]}` is not a reminder id. {TASKS_USAGE}"

    async with ctx.session_factory() as session:
        task = await session.get(ScheduledTask, task_id)
        # Users may only touch their own reminders.
        if task is None or task.owner_rc_user_id != ctx.message.sender_id:
            return f":warning: You have no reminder **#{task_id}**."
        title = task.title

        if mode == "delete":
            await session.delete(task)
            await session.commit()
            if ctx.scheduler is not None:
                ctx.scheduler.remove_task(task_id)
            return f":wastebasket: Deleted reminder **#{task_id}** ({title})."

        task.enabled = mode == "on"
        await session.commit()

    if ctx.scheduler is not None:
        if mode == "on":
            await ctx.scheduler.sync_task(task_id)
        else:
            ctx.scheduler.remove_task(task_id)

    state = "resumed" if mode == "on" else "paused"
    return f":white_check_mark: Reminder **#{task_id}** ({title}) {state}."


# --------------------------------------------------------------------------- #
# job
# --------------------------------------------------------------------------- #

JOB_USAGE = "Usage: `job list`, `job status <id>`, `job cancel <id>`."
JOB_ICONS = {
    "planned": ":white_small_square:",
    "running": ":hourglass_flowing_sand:",
    "done": ":white_check_mark:",
    "failed": ":x:",
    "cancelled": ":no_entry_sign:",
}


@builtin("job")
async def job_command(ctx: BuiltinContext) -> str:
    """The person's jobs: work the assistant does in several steps."""
    jobs = ctx.agent.jobs if ctx.agent is not None else None
    if jobs is None:
        return "Jobs are not available."
    if not ctx.message.sender_id:
        return "I cannot tell who you are, so I cannot list your jobs."
    from app.agent.jobs import ICONS as PHASE_ICONS, parse_job_id

    argv = ctx.match.args
    action = argv[0].casefold() if argv else "list"
    if action in {"list", "ls"}:
        mine = await jobs.list(owner=ctx.message.sender_id, limit=10)
        if not mine:
            return "You have no jobs. I start one when something takes several steps."
        lines = ["**Your jobs**"]
        for job in mine:
            done = sum(1 for p in job.phases if p.status == "done")
            lines.append(
                f"{JOB_ICONS.get(job.status, '')} **#{job.id}** {job.title} "
                f"- {job.status}, {done}/{len(job.phases)} steps"
            )
        lines.append(f"\n{JOB_USAGE}")
        return "\n".join(lines)
    if action not in {"status", "show", "cancel", "stop"}:
        return f":warning: Unknown action `{action}`.\n{JOB_USAGE}"
    job_id = parse_job_id(argv[1]) if len(argv) > 1 else None
    if job_id is None:
        return f":warning: Which job? {JOB_USAGE}"
    job = await jobs.get(job_id)
    # People may only see and stop their own jobs.
    if job is None or job.owner_rc_user_id != ctx.message.sender_id:
        return f":warning: You have no job **#{job_id}**."
    if action in {"cancel", "stop"}:
        if not await jobs.cancel(job_id):
            return f"Job **#{job_id}** is already {job.status}."
        return f":no_entry_sign: Stopped job **#{job_id}** ({job.title})."
    lines = [f"{JOB_ICONS.get(job.status, '')} **Job #{job.id}: {job.title}** - {job.status}"]
    for phase in job.phases:
        lines.append(f"{PHASE_ICONS.get(phase.status, '')} {phase.ord}. {phase.goal}")
        if phase.status == "done" and phase.output:
            summary = " ".join(phase.output.split())
            lines.append(f"    _{summary[:200]}{'…' if len(summary) > 200 else ''}_")
        if phase.error:
            lines.append(f"    :warning: {phase.error}")
    if job.error:
        lines.append(f":warning: {job.error}")
    return "\n".join(lines)
