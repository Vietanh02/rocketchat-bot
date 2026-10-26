"""Scheduled task CRUD, "Run now", and run history."""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import select

from app.api.deps import AdminUser, CurrentUser, RuntimeDep, SessionDep, record_audit
from app.integrations.templating import validate
from app.models import ActionType, Integration, ScheduledTask, ScheduleKind, TaskRun
from app.scheduler.triggers import (
    ScheduleError,
    describe,
    next_fire_time,
    normalize_days,
    normalize_time_of_day,
    resolve_timezone,
)
from app.schemas import TaskCreate, TaskOut, TaskRunOut, TaskUpdate
from app.timeutil import format_local, to_utc

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


def _bad(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=detail
    )


def _to_out(task: ScheduledTask) -> TaskOut:
    out = TaskOut.model_validate(task)
    try:
        out.schedule_description = describe(task)
    except ScheduleError as exc:
        out.schedule_description = f"invalid: {exc}"
    out.next_run_local = format_local(task.next_run_at, task.timezone_name)
    return out


async def _validate(session: SessionDep, task: ScheduledTask) -> None:
    """Normalise and check a task before it is saved."""
    try:
        resolve_timezone(task.timezone_name)
    except ScheduleError as exc:
        raise _bad(str(exc)) from exc

    if task.schedule_kind in (ScheduleKind.daily.value, ScheduleKind.weekly.value):
        try:
            task.time_of_day = normalize_time_of_day(task.time_of_day)
        except ScheduleError as exc:
            raise _bad(str(exc)) from exc
    if task.schedule_kind == ScheduleKind.weekly.value:
        try:
            task.days_of_week = normalize_days(task.days_of_week)
        except ScheduleError as exc:
            raise _bad(str(exc)) from exc

    if not (task.room_id or task.dm_username or task.owner_username):
        raise _bad("Set a room_id, a dm_username, or an owner_username to deliver to")

    if task.action_type == ActionType.integration.value:
        if task.integration_id is None:
            raise _bad("An integration task needs an integration_id")
        if await session.get(Integration, task.integration_id) is None:
            raise _bad(f"No integration with id {task.integration_id}")
        if task.integration_args:
            try:
                parsed = json.loads(task.integration_args)
            except ValueError as exc:
                raise _bad(f"integration_args is not valid JSON: {exc}") from exc
            if not isinstance(parsed, dict):
                raise _bad("integration_args must be a JSON object")
    else:
        if not (task.message or "").strip():
            raise _bad("A message task needs a message")
        error = validate(task.message)
        if error:
            raise _bad(f"message template is invalid: {error}")

    task.run_at = to_utc(task.run_at)

    # Builds the trigger, which is the real test of the schedule fields.
    try:
        task.next_run_at = to_utc(next_fire_time(task))
    except ScheduleError as exc:
        raise _bad(str(exc)) from exc


@router.get("", response_model=list[TaskOut])
async def list_tasks(
    _: CurrentUser,
    session: SessionDep,
    owner: str | None = Query(default=None, description="Filter by Rocket.Chat username"),
    enabled: bool | None = Query(default=None),
) -> list[TaskOut]:
    query = select(ScheduledTask).order_by(ScheduledTask.id)
    if owner:
        query = query.where(ScheduledTask.owner_username == owner)
    if enabled is not None:
        query = query.where(ScheduledTask.enabled.is_(enabled))
    return [_to_out(task) for task in (await session.scalars(query)).all()]


@router.post("", response_model=TaskOut, status_code=status.HTTP_201_CREATED)
async def create_task(
    payload: TaskCreate,
    admin: AdminUser,
    session: SessionDep,
    runtime: RuntimeDep,
) -> TaskOut:
    task = ScheduledTask(**payload.model_dump(), created_via="portal")
    await _validate(session, task)
    session.add(task)
    await session.flush()
    record_audit(session, admin, "create", "scheduled_task", task.id, task.title)
    await session.commit()
    await runtime.scheduler.sync_task(task.id)
    return _to_out(await session.get(ScheduledTask, task.id))  # type: ignore[arg-type]


@router.put("/{task_id}", response_model=TaskOut)
async def update_task(
    task_id: int,
    payload: TaskUpdate,
    admin: AdminUser,
    session: SessionDep,
    runtime: RuntimeDep,
) -> TaskOut:
    task = await session.get(ScheduledTask, task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such task")

    changes = payload.model_dump(exclude_unset=True)
    for field, new_value in changes.items():
        setattr(task, field, new_value)
    await _validate(session, task)

    record_audit(
        session, admin, "update", "scheduled_task", task.id, ", ".join(sorted(changes))
    )
    await session.commit()
    await runtime.scheduler.sync_task(task_id)
    await session.refresh(task)
    return _to_out(task)


@router.delete("/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_task(
    task_id: int, admin: AdminUser, session: SessionDep, runtime: RuntimeDep
) -> None:
    task = await session.get(ScheduledTask, task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such task")
    title = task.title
    await session.delete(task)
    record_audit(session, admin, "delete", "scheduled_task", task_id, title)
    await session.commit()
    runtime.scheduler.remove_task(task_id)


@router.post("/{task_id}/run", response_model=TaskRunOut)
async def run_task_now(
    task_id: int, admin: AdminUser, session: SessionDep, runtime: RuntimeDep
) -> TaskRun:
    if await session.get(ScheduledTask, task_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such task")
    record_audit(session, admin, "run", "scheduled_task", task_id)
    await session.commit()
    return await runtime.scheduler.run_now(task_id)


@router.get("/{task_id}/runs", response_model=list[TaskRunOut])
async def list_task_runs(
    task_id: int,
    _: CurrentUser,
    session: SessionDep,
    limit: int = Query(default=50, ge=1, le=500),
) -> list[TaskRun]:
    return list(
        (
            await session.scalars(
                select(TaskRun)
                .where(TaskRun.task_id == task_id)
                .order_by(TaskRun.ran_at.desc())
                .limit(limit)
            )
        ).all()
    )
