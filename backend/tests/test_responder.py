"""The full inbound pipeline: message in, reply out, nothing touching the network."""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import select

from app.bot.types import MessageContext
from app.models import (
    BotConfig,
    Command,
    Integration,
    IntegrationParam,
    MessageLog,
    ScheduledTask,
)
from app.rocketchat.ddp import IncomingMessage


def incoming(text: str, *, room_type: str = "d", **kwargs) -> IncomingMessage:
    base = dict(
        message_id=kwargs.pop("message_id", "M1"),
        room_id=kwargs.pop("room_id", "R1"),
        text=text,
        sender_id=kwargs.pop("sender_id", "U1"),
        sender_username=kwargs.pop("sender_username", "bo"),
        room_type=room_type,
    )
    base.update(kwargs)
    return IncomingMessage(**base)


def context(text: str, **kwargs) -> MessageContext:
    return MessageContext.from_incoming(incoming(text, **kwargs), bot_username="taskbot")


async def add(runtime, *rows):
    async with runtime.session_factory() as session:
        for row in rows:
            session.add(row)
        config = await session.get(BotConfig, 1)
        config.config_version += 1
        await session.commit()
    runtime.config_store.invalidate()


# --------------------------------------------------------------------------- #
# Built-ins shipped by default
# --------------------------------------------------------------------------- #


async def test_help_lists_the_default_commands(app_env, gateway):
    reply = await app_env.bot.responder.respond(context("help"))
    assert reply is not None
    for usage in ("help", "status", "remind …", "tasks …"):
        assert usage in reply.text


async def test_unmatched_dm_gets_the_fallback(app_env, gateway):
    reply = await app_env.bot.responder.respond(context("something random"))
    assert reply is not None and "did not understand" in reply.text


async def test_empty_fallback_means_silence(app_env, gateway):
    async with app_env.session_factory() as session:
        config = await session.get(BotConfig, 1)
        config.fallback_reply = ""
        config.config_version += 1
        await session.commit()
    app_env.config_store.invalidate()

    assert await app_env.bot.responder.respond(context("something random")) is None


# --------------------------------------------------------------------------- #
# Channel etiquette
# --------------------------------------------------------------------------- #


async def test_unaddressed_channel_chatter_is_ignored(app_env, gateway):
    assert await app_env.bot.responder.respond(context("help", room_type="c")) is None


async def test_a_mention_in_a_channel_is_answered(app_env, gateway):
    reply = await app_env.bot.responder.respond(context("@taskbot help", room_type="c"))
    assert reply is not None and "What I can do" in reply.text


async def test_a_slash_command_in_a_channel_is_answered(app_env, gateway):
    reply = await app_env.bot.responder.respond(context("/remind", room_type="c"))
    assert reply is not None


async def test_unmatched_mention_in_a_channel_gets_the_fallback(app_env, gateway):
    reply = await app_env.bot.responder.respond(context("@taskbot wat", room_type="c"))
    assert reply is not None and "did not understand" in reply.text


async def test_disabled_bot_says_nothing(app_env, gateway):
    async with app_env.session_factory() as session:
        config = await session.get(BotConfig, 1)
        config.enabled = False
        config.config_version += 1
        await session.commit()
    app_env.config_store.invalidate()

    assert await app_env.bot.responder.respond(context("help")) is None


# --------------------------------------------------------------------------- #
# Static commands
# --------------------------------------------------------------------------- #


async def test_static_command_renders_its_template(app_env, gateway):
    await add(
        app_env,
        Command(
            name="hi",
            trigger_type="keyword",
            pattern="hi",
            match_mode="exact",
            priority=5,
            response_type="static",
            static_response="Hello {{ user.username }}! You said: {{ message.text }}",
        ),
    )
    reply = await app_env.bot.responder.respond(context("hi"))
    assert reply.text == "Hello bo! You said: hi"


async def test_portal_edits_take_effect_without_a_restart(app_env, gateway):
    await add(
        app_env,
        Command(
            name="ping", trigger_type="keyword", match_mode="prefix", pattern="ping", priority=5,
            response_type="static", static_response="pong v1",
        ),
    )
    assert (await app_env.bot.responder.respond(context("/ping"))).text == "pong v1"

    async with app_env.session_factory() as session:
        command = await session.scalar(select(Command).where(Command.name == "ping"))
        command.static_response = "pong v2"
        config = await session.get(BotConfig, 1)
        config.config_version += 1
        await session.commit()

    # No invalidate() call: the store must notice the version bump itself.
    assert (await app_env.bot.responder.respond(context("/ping"))).text == "pong v2"


async def test_priority_decides_between_overlapping_commands(app_env, gateway):
    await add(
        app_env,
        Command(name="low", trigger_type="keyword", pattern="deploy", match_mode="contains",
                priority=90, response_type="static", static_response="LOW"),
        Command(name="high", trigger_type="keyword", pattern="deploy", match_mode="contains",
                priority=5, response_type="static", static_response="HIGH"),
    )
    assert (await app_env.bot.responder.respond(context("deploy now"))).text == "HIGH"


# --------------------------------------------------------------------------- #
# Integration commands
# --------------------------------------------------------------------------- #


async def test_integration_command_calls_the_api(app_env, gateway, monkeypatch):
    integration = Integration(
        name="weather",
        base_url="https://api.example.com",
        method="GET",
        path_template="/w/{{ params.city }}",
        reply_template="{{ params.city }}: {{ response.temp }}C",
    )
    integration.params = [IntegrationParam(name="city", position=0, required=True)]
    await add(
        app_env,
        integration,
        Command(name="weather", trigger_type="keyword", match_mode="prefix", pattern="weather", priority=5,
                response_type="integration", integration_id=1),
    )

    captured: dict = {}

    async def fake_execute(spec, *, params, base_context=None, strict=False, client=None):
        from app.integrations.executor import IntegrationResult

        captured["params"] = params
        return IntegrationResult(ok=True, text=f"{params['city']}: 26C", status_code=200)

    monkeypatch.setattr("app.bot.responder.execute_integration", fake_execute)

    reply = await app_env.bot.responder.respond(context("/weather Hanoi"))
    assert captured["params"] == {"city": "Hanoi"}
    assert reply.text == "Hanoi: 26C"


async def test_missing_argument_returns_usage(app_env, gateway):
    integration = Integration(
        name="weather", base_url="https://api.example.com", reply_template="x"
    )
    integration.params = [IntegrationParam(name="city", position=0, required=True)]
    await add(
        app_env,
        integration,
        Command(name="weather", trigger_type="keyword", match_mode="prefix", pattern="weather", priority=5,
                response_type="integration", integration_id=1),
    )
    reply = await app_env.bot.responder.respond(context("/weather"))
    assert "Usage" in reply.text and "<city>" in reply.text


async def test_command_pointing_at_a_disabled_integration_explains_itself(app_env, gateway):
    await add(
        app_env,
        Integration(name="dead", base_url="https://api.example.com", enabled=False),
        Command(name="dead", trigger_type="keyword", match_mode="prefix", pattern="dead", priority=5,
                response_type="integration", integration_id=1),
    )
    reply = await app_env.bot.responder.respond(context("/dead"))
    assert "missing or disabled" in reply.text
    assert reply.error is not None


# --------------------------------------------------------------------------- #
# Reminders via chat
# --------------------------------------------------------------------------- #


async def test_remind_creates_a_task_and_schedules_it(app_env, gateway):
    reply = await app_env.bot.responder.respond(context("/remind daily 09:00 stand-up"))
    assert "Reminder **#1** saved" in reply.text

    async with app_env.session_factory() as session:
        task = await session.get(ScheduledTask, 1)
    assert task.owner_rc_user_id == "U1"
    assert task.schedule_kind == "daily" and task.time_of_day == "09:00"
    assert task.dm_username == "bo"  # DMs go back to the requester
    assert task.next_run_at is not None
    assert app_env.scheduler.job_count == 1


async def test_remind_in_a_channel_delivers_to_that_channel(app_env, gateway):
    await app_env.bot.responder.respond(
        context("@taskbot /remind daily 09:00 stand-up", room_type="c", room_id="C9")
    )
    async with app_env.session_factory() as session:
        task = await session.get(ScheduledTask, 1)
    assert task.room_id == "C9" and task.dm_username is None


async def test_remind_can_target_a_named_channel(app_env, gateway):
    gateway.channels["general"] = "GENERAL"
    await app_env.bot.responder.respond(context("/remind daily 09:00 sync #general"))
    async with app_env.session_factory() as session:
        task = await session.get(ScheduledTask, 1)
    assert task.room_id == "GENERAL"


async def test_remind_reports_an_unknown_channel(app_env, gateway):
    reply = await app_env.bot.responder.respond(context("/remind daily 09:00 sync #nope"))
    assert "cannot post to #nope" in reply.text


async def test_bad_remind_syntax_is_explained(app_env, gateway):
    reply = await app_env.bot.responder.respond(context("/remind whenever"))
    assert "could not find a time" in reply.text


async def test_tasks_lists_only_your_own_reminders(app_env, gateway):
    await app_env.bot.responder.respond(context("/remind daily 09:00 mine"))
    await app_env.bot.responder.respond(
        context("/remind daily 10:00 theirs", sender_id="U2", sender_username="other")
    )

    mine = await app_env.bot.responder.respond(context("tasks"))
    assert "mine" in mine.text and "theirs" not in mine.text


async def test_you_cannot_delete_someone_elses_reminder(app_env, gateway):
    await app_env.bot.responder.respond(context("/remind daily 09:00 theirs"))
    reply = await app_env.bot.responder.respond(
        context("tasks delete 1", sender_id="U2", sender_username="other")
    )
    assert "no reminder **#1**" in reply.text

    async with app_env.session_factory() as session:
        assert await session.get(ScheduledTask, 1) is not None


async def test_pause_resume_and_delete_a_reminder(app_env, gateway):
    await app_env.bot.responder.respond(context("/remind daily 09:00 mine"))
    assert app_env.scheduler.job_count == 1

    assert "paused" in (await app_env.bot.responder.respond(context("tasks off 1"))).text
    assert app_env.scheduler.job_count == 0

    assert "resumed" in (await app_env.bot.responder.respond(context("tasks on 1"))).text
    assert app_env.scheduler.job_count == 1

    assert "Deleted" in (await app_env.bot.responder.respond(context("tasks delete 1"))).text
    assert app_env.scheduler.job_count == 0
    async with app_env.session_factory() as session:
        assert await session.get(ScheduledTask, 1) is None


# --------------------------------------------------------------------------- #
# Runner-level behaviour
# --------------------------------------------------------------------------- #


async def test_the_bot_never_answers_itself(app_env, gateway):
    app_env.bot._bot_user_id = "BOT"
    await app_env.bot._enqueue(incoming("help", sender_id="BOT"))
    assert app_env.bot._queue.empty()


@pytest.mark.parametrize(
    "message",
    [
        incoming("help", is_bot=True),
        incoming("help", is_system=True),
        incoming("help", is_edit=True),
        incoming("   "),
    ],
)
async def test_non_actionable_messages_are_dropped(app_env, gateway, message):
    await app_env.bot._enqueue(message)
    assert app_env.bot._queue.empty()


async def test_a_duplicate_message_id_is_only_handled_once(app_env, gateway):
    await app_env.bot._enqueue(incoming("help", message_id="SAME"))
    await app_env.bot._enqueue(incoming("help", message_id="SAME"))
    assert app_env.bot._queue.qsize() == 1


async def test_handling_a_message_sends_a_reply_and_writes_a_log(app_env, gateway):
    await app_env.bot._handle(incoming("help"))

    assert len(gateway.sent) == 1
    assert "What I can do" in gateway.last_text

    async with app_env.session_factory() as session:
        logs = list((await session.scalars(select(MessageLog))).all())
    assert len(logs) == 1
    assert logs[0].matched_command == "help" and logs[0].status == "ok"


async def test_a_send_failure_is_logged_as_an_error(app_env, gateway):
    from app.rocketchat.rest import RocketChatError

    gateway.fail_next = RocketChatError("rocket.chat said no")
    await app_env.bot._handle(incoming("help"))

    async with app_env.session_factory() as session:
        log = (await session.scalars(select(MessageLog))).one()
    assert log.status == "error" and "rocket.chat said no" in log.error


async def test_reply_in_thread_setting_is_honoured(app_env, gateway):
    async with app_env.session_factory() as session:
        config = await session.get(BotConfig, 1)
        config.reply_in_thread = True
        config.config_version += 1
        await session.commit()
    app_env.config_store.invalidate()

    await app_env.bot._handle(incoming("help", message_id="M42"))
    assert gateway.sent[0].thread_id == "M42"
