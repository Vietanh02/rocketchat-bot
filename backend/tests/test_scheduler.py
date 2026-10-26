"""Scheduler firing: delivery, integration actions, failures, and retirement."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models import (
    ActionType,
    Integration,
    IntegrationParam,
    ScheduledTask,
    TaskRun,
)


async def make_task(runtime, **overrides) -> int:
    defaults = dict(
        title="Stand-up",
        dm_username="bo",
        owner_rc_user_id="U1",
        owner_username="bo",
        schedule_kind="daily",
        time_of_day="09:00",
        timezone_name="UTC",
        message="Stand-up time, {{ user.mention }}",
        enabled=True,
    )
    defaults.update(overrides)
    async with runtime.session_factory() as session:
        task = ScheduledTask(**defaults)
        session.add(task)
        await session.commit()
        task_id = task.id
    await runtime.scheduler.sync_task(task_id)
    return task_id


async def test_firing_renders_the_message_and_sends_a_dm(app_env, gateway):
    task_id = await make_task(app_env)
    run = await app_env.scheduler.run_now(task_id)

    assert run.status == "ok"
    assert gateway.sent[0].text == "Stand-up time, @bo"
    assert gateway.sent[0].room_id == "dm-bo"


async def test_firing_a_room_task_posts_to_that_room(app_env, gateway):
    task_id = await make_task(app_env, room_id="C1", dm_username=None)
    await app_env.scheduler.run_now(task_id)
    assert gateway.sent[0].room_id == "C1"


async def test_a_run_is_recorded_and_stamped_on_the_task(app_env, gateway):
    task_id = await make_task(app_env)
    await app_env.scheduler.run_now(task_id)

    async with app_env.session_factory() as session:
        runs = list((await session.scalars(select(TaskRun))).all())
        task = await session.get(ScheduledTask, task_id)

    assert len(runs) == 1 and runs[0].status == "ok"
    assert runs[0].duration_ms is not None
    assert task.last_status == "ok" and task.last_run_at is not None


async def test_a_one_off_retires_itself_after_firing(app_env, gateway):
    task_id = await make_task(
        app_env,
        schedule_kind="once",
        run_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    assert app_env.scheduler.job_count == 1

    await app_env.scheduler.run_now(task_id)

    async with app_env.session_factory() as session:
        task = await session.get(ScheduledTask, task_id)
    assert task.enabled is False and task.next_run_at is None
    assert app_env.scheduler.job_count == 0


async def test_a_recurring_task_keeps_its_next_run(app_env, gateway):
    task_id = await make_task(app_env)
    await app_env.scheduler.run_now(task_id)

    async with app_env.session_factory() as session:
        task = await session.get(ScheduledTask, task_id)
    assert task.enabled is True and task.next_run_at is not None


async def test_an_integration_task_posts_the_api_result(app_env, gateway, monkeypatch):
    async with app_env.session_factory() as session:
        integration = Integration(
            name="queue", base_url="https://api.example.com", reply_template="{{ response.n }}"
        )
        integration.params = [IntegrationParam(name="env", position=0, required=True)]
        session.add(integration)
        await session.commit()
        integration_id = integration.id

    async def fake_execute(spec, *, params, base_context=None, strict=False, client=None):
        from app.integrations.executor import IntegrationResult

        return IntegrationResult(ok=True, text=f"depth={params['env']}", status_code=200)

    monkeypatch.setattr("app.scheduler.service.execute_integration", fake_execute)

    task_id = await make_task(
        app_env,
        title="Queue depth",
        action_type=ActionType.integration.value,
        integration_id=integration_id,
        integration_args=json.dumps({"env": "prod"}),
        message="",
    )
    run = await app_env.scheduler.run_now(task_id)

    assert run.status == "ok"
    assert gateway.last_text == "**Queue depth**\ndepth=prod"


async def test_a_send_failure_is_recorded_not_raised(app_env, gateway):
    from app.rocketchat.rest import RocketChatError

    task_id = await make_task(app_env)
    gateway.fail_next = RocketChatError("rocket.chat is down")

    run = await app_env.scheduler.run_now(task_id)
    assert run.status == "error" and "down" in run.detail


async def test_firing_without_a_connection_is_recorded_as_an_error(app_env):
    app_env.scheduler.set_gateway(None)
    task_id = await make_task(app_env)

    run = await app_env.scheduler.run_now(task_id)
    assert run.status == "error" and "not configured" in run.detail


async def test_a_deleted_task_unschedules_itself_when_it_fires(app_env, gateway):
    task_id = await make_task(app_env)
    async with app_env.session_factory() as session:
        await session.delete(await session.get(ScheduledTask, task_id))
        await session.commit()

    run = await app_env.scheduler._fire(task_id)
    assert run.status == "skipped"


async def test_a_disabled_task_is_skipped_when_fired_by_the_timer(app_env, gateway):
    task_id = await make_task(app_env, enabled=False)
    run = await app_env.scheduler._fire(task_id)
    assert run.status == "skipped" and gateway.sent == []


async def test_run_now_overrides_disabled(app_env, gateway):
    """The portal's "Run now" button should work on a paused task."""
    task_id = await make_task(app_env, enabled=False)
    run = await app_env.scheduler.run_now(task_id)
    assert run.status == "ok" and len(gateway.sent) == 1


async def test_a_bad_schedule_is_not_registered(app_env, gateway):
    async with app_env.session_factory() as session:
        task = ScheduledTask(
            title="broken", dm_username="bo", schedule_kind="cron", cron_expr="nope",
            timezone_name="UTC", message="x", enabled=True,
        )
        session.add(task)
        await session.commit()
        task_id = task.id

    assert await app_env.scheduler.sync_task(task_id) is None
    assert app_env.scheduler.job_count == 0


async def test_reload_all_rebuilds_from_the_table(app_env, gateway):
    await make_task(app_env, title="one")
    await make_task(app_env, title="two")
    await make_task(app_env, title="paused", enabled=False)

    assert await app_env.scheduler.reload_all() == 2
    assert app_env.scheduler.job_count == 2


async def test_integration_args_must_be_a_json_object(app_env, gateway):
    async with app_env.session_factory() as session:
        integration = Integration(name="x", base_url="https://api.example.com")
        session.add(integration)
        await session.commit()
        integration_id = integration.id

    task_id = await make_task(
        app_env,
        action_type=ActionType.integration.value,
        integration_id=integration_id,
        integration_args="[1,2]",
        message="",
    )
    run = await app_env.scheduler.run_now(task_id)
    assert run.status == "error" and "JSON object" in run.detail
