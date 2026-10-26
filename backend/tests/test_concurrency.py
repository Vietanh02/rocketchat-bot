"""Many users at once: messages run in parallel, each user's stay in order."""

from __future__ import annotations

import asyncio
import stat
import time

import pytest

from app.bot.runner import MAX_LANE_BACKLOG
from app.config import get_settings
from app.rocketchat.ddp import IncomingMessage

_ids = iter(range(1, 1_000_000))


def message(text: str, *, sender: str, room: str | None = None) -> IncomingMessage:
    return IncomingMessage(
        message_id=f"M{next(_ids)}",
        room_id=room or f"dm-{sender}",
        text=text,
        sender_id=f"id-{sender}",
        sender_username=sender,
        room_type="d" if room is None else "c",
    )


class Recorder:
    """Stands in for BotService._handle, tracking overlap."""

    def __init__(self, delay: float = 0.3) -> None:
        self.delay = delay
        self.running = 0
        self.peak = 0
        self.order: list[str] = []
        self.by_sender: dict[str, int] = {}
        self.sender_overlap = False

    async def __call__(self, msg: IncomingMessage) -> None:
        self.running += 1
        self.peak = max(self.peak, self.running)
        sender = msg.sender_username or ""
        self.by_sender[sender] = self.by_sender.get(sender, 0) + 1
        if self.by_sender[sender] > 1:
            self.sender_overlap = True
        try:
            delay = self.delay(msg) if callable(self.delay) else self.delay
            await asyncio.sleep(delay)
            self.order.append(msg.text)
        finally:
            self.running -= 1
            self.by_sender[sender] -= 1


@pytest.fixture
async def dispatching(app_env):
    """Run the bot's dispatcher, as start() would once connected."""
    bot = app_env.bot
    bot._bot_user_id = "the-bot"
    task = asyncio.create_task(bot._dispatch())
    try:
        yield bot
    finally:
        task.cancel()
        for lane in list(bot._lane_tasks):
            lane.cancel()


async def send_all(bot, messages) -> None:
    for msg in messages:
        await bot._enqueue(msg)


async def test_twenty_users_are_served_at_the_same_time(dispatching, monkeypatch):
    recorder = Recorder(delay=0.5)
    monkeypatch.setattr(dispatching, "_handle", recorder)

    started = time.monotonic()
    await send_all(dispatching, [message("hi", sender=f"user{n}") for n in range(20)])
    await dispatching.drain()

    assert recorder.peak == 20
    assert len(recorder.order) == 20
    assert time.monotonic() - started < 2  # 20 × 0.5s one at a time would be 10s


async def test_no_more_than_the_limit_run_at_once(dispatching, monkeypatch):
    assert get_settings().bot_max_concurrency == 20
    recorder = Recorder(delay=0.2)
    monkeypatch.setattr(dispatching, "_handle", recorder)

    await send_all(dispatching, [message("hi", sender=f"user{n}") for n in range(35)])
    await asyncio.sleep(0.05)
    status = dispatching.status.details
    assert status["active_handlers"] == "20" and status["waiting"] == "15"

    await dispatching.drain()
    assert recorder.peak == 20 and len(recorder.order) == 35
    assert dispatching.status.details["waiting"] == "0"


async def test_one_users_messages_are_handled_in_order_one_at_a_time(dispatching, monkeypatch):
    # Later messages are quicker, so any overlap would reorder them.
    recorder = Recorder(delay=lambda msg: 0.3 - 0.05 * int(msg.text))
    monkeypatch.setattr(dispatching, "_handle", recorder)

    await send_all(dispatching, [message(str(n), sender="an") for n in range(5)])
    await dispatching.drain()

    assert recorder.order == ["0", "1", "2", "3", "4"]
    assert not recorder.sender_overlap


async def test_a_slow_user_does_not_hold_up_anyone_else(dispatching, monkeypatch):
    recorder = Recorder(delay=lambda msg: 2.0 if msg.text == "slow" else 0.05)
    monkeypatch.setattr(dispatching, "_handle", recorder)

    await send_all(dispatching, [message("slow", sender="an")])
    await send_all(dispatching, [message(f"quick{n}", sender=f"user{n}") for n in range(5)])
    await asyncio.sleep(0.5)

    assert sorted(recorder.order) == [f"quick{n}" for n in range(5)]  # "slow" still running
    await dispatching.drain()
    assert recorder.order[-1] == "slow"


async def test_users_in_the_same_channel_run_in_parallel(dispatching, monkeypatch):
    recorder = Recorder(delay=0.3)
    monkeypatch.setattr(dispatching, "_handle", recorder)

    await send_all(
        dispatching, [message("/x", sender=f"user{n}", room="general") for n in range(4)]
    )
    await dispatching.drain()
    assert recorder.peak == 4


async def test_a_flooding_user_is_cut_off(dispatching, monkeypatch):
    recorder = Recorder(delay=0.2)
    monkeypatch.setattr(dispatching, "_handle", recorder)

    await send_all(dispatching, [message(str(n), sender="spam") for n in range(MAX_LANE_BACKLOG + 10)])
    await send_all(dispatching, [message("hi", sender="calm")])
    await dispatching.drain()

    spam = [text for text in recorder.order if text != "hi"]
    assert recorder.order.count("hi") == 1
    # At most MAX_LANE_BACKLOG wait; one more may already have been taken.
    assert MAX_LANE_BACKLOG <= len(spam) <= MAX_LANE_BACKLOG + 1
    assert spam == [str(n) for n in range(len(spam))]  # the earliest are kept


async def test_an_error_in_one_message_does_not_stop_the_lane(dispatching, monkeypatch):
    handled: list[str] = []

    async def flaky(msg):
        if msg.text == "boom":
            raise RuntimeError("bad message")
        handled.append(msg.text)

    monkeypatch.setattr(dispatching, "_handle", flaky)
    await send_all(dispatching, [message(t, sender="an") for t in ("a", "boom", "b")])
    await dispatching.drain()
    assert handled == ["a", "b"]


async def test_stopping_the_bot_cancels_in_flight_work(dispatching, monkeypatch):
    recorder = Recorder(delay=30)
    monkeypatch.setattr(dispatching, "_handle", recorder)
    await send_all(dispatching, [message("hi", sender=f"user{n}") for n in range(3)])
    await asyncio.sleep(0.05)
    assert recorder.running == 3

    await dispatching.stop()
    assert recorder.running == 0 and dispatching._lanes == {}


# --------------------------------------------------------------------------- #
# End to end, with a real slow script
# --------------------------------------------------------------------------- #


async def test_a_slow_script_does_not_delay_other_users(
    dispatching, admin_client, gateway, tmp_path, monkeypatch
):
    root = tmp_path / "scripts"
    root.mkdir()
    (root / "slow.sh").write_text("sleep 3\necho finally")
    monkeypatch.setattr(get_settings(), "scripts_dir", str(root))
    await admin_client.post(
        "/api/commands",
        json={
            "name": "slow",
            "trigger_type": "keyword",
            "match_mode": "prefix",
            "pattern": "slow",
            "response_type": "script",
            "script_name": "slow.sh",
        },
    )

    await send_all(dispatching, [message("/slow", sender="an")])
    await send_all(dispatching, [message("help", sender=f"user{n}") for n in range(19)])

    deadline = time.monotonic() + 2.5
    while len(gateway.sent) < 19 and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    assert len(gateway.sent) == 19
    assert all("What I can do" in m.text for m in gateway.sent)

    await dispatching.drain()
    assert gateway.last_text == "finally"
    assert gateway.sent[-1].room_id == "dm-an"


async def test_many_real_messages_at_once_are_all_answered_and_logged(dispatching, app_env, gateway):
    from sqlalchemy import func, select

    from app.models import MessageLog

    await send_all(dispatching, [message("help", sender=f"user{n}") for n in range(40)])
    await dispatching.drain()

    assert len(gateway.sent) == 40
    assert {m.room_id for m in gateway.sent} == {f"dm-user{n}" for n in range(40)}
    async with app_env.session_factory() as session:
        statuses = dict(
            (await session.execute(select(MessageLog.status, func.count()).group_by(MessageLog.status))).all()
        )
    assert statuses == {"ok": 40}
