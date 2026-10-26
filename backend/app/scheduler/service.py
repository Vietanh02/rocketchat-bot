"""Runs the user-defined reminder tasks.

`scheduled_task` rows are the source of truth; APScheduler holds only the live
timers, rebuilt from the table on startup. That keeps one authoritative store
(the portal can edit tasks directly) instead of reconciling two.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.bot.config_store import ConfigStore
from app.integrations.executor import (
    IntegrationError,
    IntegrationSpec,
    bind_params,
    execute_integration,
)
from app.integrations.templating import TemplateRenderError, render
from app.models import (
    ActionType,
    Integration,
    ScheduledTask,
    ScheduleKind,
    TaskRun,
)
from app.rocketchat.gateway import Gateway
from app.scheduler.triggers import ScheduleError, build_trigger, next_fire_time
from app.timeutil import to_utc

log = logging.getLogger(__name__)

JOB_PREFIX = "task:"


def job_id(task_id: int) -> str:
    return f"{JOB_PREFIX}{task_id}"


def _recipient_vars(task: ScheduledTask) -> dict[str, str | None]:
    """Who the reminder is addressed to, for `{{ user.* }}` in templates.

    A portal-created task may name only a DM target, so fall back to that
    rather than rendering an empty mention.
    """
    username = task.owner_username or task.dm_username or ""
    return {
        "id": task.owner_rc_user_id,
        "username": username,
        "mention": f"@{username}" if username else "",
    }


class SchedulerService:
    """Owns the APScheduler instance and keeps it in sync with the DB."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        config_store: ConfigStore,
    ) -> None:
        self._session_factory = session_factory
        self._config_store = config_store
        self._scheduler = AsyncIOScheduler(timezone="UTC")
        self._gateway: Gateway | None = None

    # ----------------------------------------------------------------- #
    # Lifecycle
    # ----------------------------------------------------------------- #

    def set_gateway(self, gateway: Gateway | None) -> None:
        """The gateway is rebuilt whenever bot credentials change."""
        self._gateway = gateway

    @property
    def running(self) -> bool:
        return self._scheduler.running

    @property
    def job_count(self) -> int:
        """How many task timers are currently registered."""
        if not self._scheduler.running:
            return 0
        return sum(1 for job in self._scheduler.get_jobs() if job.id.startswith(JOB_PREFIX))

    async def start(self) -> None:
        if not self._scheduler.running:
            self._scheduler.start()
        await self.reload_all()

    async def shutdown(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)

    # ----------------------------------------------------------------- #
    # Sync
    # ----------------------------------------------------------------- #

    async def reload_all(self) -> int:
        """Rebuild every timer from the enabled rows in the table."""
        for job in self._scheduler.get_jobs():
            if job.id.startswith(JOB_PREFIX):
                job.remove()

        async with self._session_factory() as session:
            tasks = list(
                (
                    await session.scalars(
                        select(ScheduledTask).where(ScheduledTask.enabled.is_(True))
                    )
                ).all()
            )
            scheduled = 0
            for task in tasks:
                if self._register(task):
                    scheduled += 1
            await session.commit()

        log.info("Scheduler holds %d of %d enabled task(s)", scheduled, len(tasks))
        return scheduled

    async def sync_task(self, task_id: int) -> datetime | None:
        """(Re)register one task after a create/update/toggle."""
        async with self._session_factory() as session:
            task = await session.get(ScheduledTask, task_id)
            if task is None or not task.enabled:
                self.remove_task(task_id)
                return None
            self._register(task)
            await session.commit()
            return task.next_run_at

    def remove_task(self, task_id: int) -> None:
        job = self._scheduler.get_job(job_id(task_id))
        if job is not None:
            job.remove()

    def _register(self, task: ScheduledTask) -> bool:
        """Add/replace the timer for `task`; stamps next_run_at on the row."""
        try:
            trigger = build_trigger(task)
        except ScheduleError as exc:
            log.warning("Task %s (%s) has a bad schedule: %s", task.id, task.title, exc)
            task.last_status = "error"
            task.next_run_at = None
            return False

        self._scheduler.add_job(
            self._fire,
            trigger=trigger,
            id=job_id(task.id),
            args=[task.id],
            replace_existing=True,
            misfire_grace_time=300,
            coalesce=True,
            max_instances=1,
            name=task.title,
        )
        job = self._scheduler.get_job(job_id(task.id))
        task.next_run_at = to_utc(
            getattr(job, "next_run_time", None) or next_fire_time(task)
        )
        return True

    # ----------------------------------------------------------------- #
    # Firing
    # ----------------------------------------------------------------- #

    async def run_now(self, task_id: int) -> TaskRun:
        """Fire a task immediately (portal "Run now" button)."""
        return await self._fire(task_id, manual=True)

    async def _fire(self, task_id: int, *, manual: bool = False) -> TaskRun:
        started = datetime.now(timezone.utc)
        status, detail = "ok", ""

        async with self._session_factory() as session:
            task = await session.get(
                ScheduledTask,
                task_id,
                options=[
                    selectinload(ScheduledTask.integration).selectinload(
                        Integration.params
                    )
                ],
            )
            if task is None:
                log.warning("Task %s fired but no longer exists", task_id)
                self.remove_task(task_id)
                return TaskRun(task_id=task_id, status="skipped", detail="task deleted")

            if not manual and not task.enabled:
                status, detail = "skipped", "task disabled"
            else:
                try:
                    text = await self._build_text(task)
                    room_id = await self._resolve_room(task)
                    gateway = self._require_gateway()
                    await gateway.send_message(room_id, text)
                    detail = f"sent to {room_id}"
                except Exception as exc:  # noqa: BLE001 - recorded, never fatal
                    status = "error"
                    detail = str(exc)[:1000]
                    log.warning("Task %s (%s) failed: %s", task.id, task.title, exc)

            duration_ms = int(
                (datetime.now(timezone.utc) - started).total_seconds() * 1000
            )
            run = TaskRun(
                task_id=task.id,
                ran_at=started,
                status=status,
                detail=detail,
                duration_ms=duration_ms,
            )
            session.add(run)
            task.last_run_at = started
            task.last_status = status

            if task.schedule_kind == ScheduleKind.once.value and status != "skipped":
                # A one-off has nothing left to do; retire it so it stops
                # showing up as upcoming.
                task.enabled = False
                task.next_run_at = None
                self.remove_task(task.id)
            else:
                try:
                    task.next_run_at = to_utc(next_fire_time(task))
                except ScheduleError:
                    task.next_run_at = None
            await session.commit()
            return run

    # ----------------------------------------------------------------- #

    def _require_gateway(self) -> Gateway:
        if self._gateway is None:
            raise RuntimeError(
                "Rocket.Chat connection is not configured; set bot credentials "
                "in the portal"
            )
        return self._gateway

    async def _resolve_room(self, task: ScheduledTask) -> str:
        if task.room_id:
            return task.room_id
        target = task.dm_username or task.owner_username
        if not target:
            raise RuntimeError("task has no room_id and no DM target")
        return await self._require_gateway().resolve_dm_room(target)

    async def _build_text(self, task: ScheduledTask) -> str:
        base_context: dict[str, Any] = {
            "task": {
                "id": task.id,
                "title": task.title,
                "message": task.message or "",
            },
            "user": _recipient_vars(task),
            "now": datetime.now(timezone.utc),
        }

        if task.action_type == ActionType.integration.value:
            if task.integration is None or not task.integration.enabled:
                raise RuntimeError("task's integration is missing or disabled")
            spec = IntegrationSpec.from_model(task.integration)
            try:
                args = json.loads(task.integration_args) if task.integration_args else {}
            except ValueError as exc:
                raise RuntimeError(f"integration_args is not valid JSON: {exc}") from exc
            if not isinstance(args, dict):
                raise RuntimeError("integration_args must be a JSON object")
            params = bind_params(
                spec.params, [], named={k: str(v) for k, v in args.items()}
            )
            try:
                result = await execute_integration(
                    spec, params=params, base_context=base_context
                )
            except IntegrationError as exc:
                raise RuntimeError(str(exc)) from exc
            prefix = f"**{task.title}**\n" if task.title else ""
            return prefix + result.text

        try:
            body = render(task.message or task.title, base_context)
        except TemplateRenderError as exc:
            raise RuntimeError(f"message template error: {exc}") from exc
        return body or task.title
