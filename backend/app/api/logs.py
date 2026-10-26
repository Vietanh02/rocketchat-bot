"""Read-only views over the message, task-run and audit logs."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Query
from sqlalchemy import func, select

from app.api.deps import CurrentUser, RuntimeDep, SessionDep
from app.api.config import build_status
from app.api.tasks import _to_out as task_to_out
from app.models import (
    AuditLog,
    Command,
    Integration,
    MessageLog,
    ScheduledTask,
    TaskRun,
)
from app.schemas import AuditLogOut, DashboardStats, MessageLogOut, TaskRunOut

router = APIRouter(prefix="/api", tags=["logs"])


@router.get("/logs/messages", response_model=list[MessageLogOut])
async def message_logs(
    _: CurrentUser,
    session: SessionDep,
    limit: int = Query(default=100, ge=1, le=500),
    status_filter: str | None = Query(default=None, alias="status"),
    room_id: str | None = Query(default=None),
) -> list[MessageLog]:
    query = select(MessageLog).order_by(MessageLog.received_at.desc()).limit(limit)
    if status_filter:
        query = query.where(MessageLog.status == status_filter)
    if room_id:
        query = query.where(MessageLog.room_id == room_id)
    return list((await session.scalars(query)).all())


@router.get("/logs/task-runs", response_model=list[TaskRunOut])
async def task_run_logs(
    _: CurrentUser,
    session: SessionDep,
    limit: int = Query(default=100, ge=1, le=500),
) -> list[TaskRun]:
    return list(
        (
            await session.scalars(
                select(TaskRun).order_by(TaskRun.ran_at.desc()).limit(limit)
            )
        ).all()
    )


@router.get("/logs/audit", response_model=list[AuditLogOut])
async def audit_logs(
    _: CurrentUser,
    session: SessionDep,
    limit: int = Query(default=100, ge=1, le=500),
) -> list[AuditLog]:
    return list(
        (
            await session.scalars(
                select(AuditLog).order_by(AuditLog.at.desc()).limit(limit)
            )
        ).all()
    )


@router.get("/dashboard", response_model=DashboardStats)
async def dashboard(
    _: CurrentUser, session: SessionDep, runtime: RuntimeDep
) -> DashboardStats:
    since = datetime.now(timezone.utc) - timedelta(days=1)

    async def count(table, *where) -> int:
        return await session.scalar(
            select(func.count()).select_from(table).where(*where)
        ) or 0

    recent = list(
        (
            await session.scalars(
                select(MessageLog).order_by(MessageLog.received_at.desc()).limit(10)
            )
        ).all()
    )
    upcoming = list(
        (
            await session.scalars(
                select(ScheduledTask)
                .where(ScheduledTask.enabled.is_(True))
                .where(ScheduledTask.next_run_at.is_not(None))
                .order_by(ScheduledTask.next_run_at)
                .limit(10)
            )
        ).all()
    )

    return DashboardStats(
        commands_total=await count(Command),
        commands_enabled=await count(Command, Command.enabled.is_(True)),
        integrations_total=await count(Integration),
        tasks_total=await count(ScheduledTask),
        tasks_enabled=await count(ScheduledTask, ScheduledTask.enabled.is_(True)),
        messages_today=await count(MessageLog, MessageLog.received_at >= since),
        errors_today=await count(
            MessageLog, MessageLog.received_at >= since, MessageLog.status == "error"
        ),
        connection=await build_status(runtime),
        recent_messages=[MessageLogOut.model_validate(row) for row in recent],
        upcoming_tasks=[task_to_out(task) for task in upcoming],
    )
