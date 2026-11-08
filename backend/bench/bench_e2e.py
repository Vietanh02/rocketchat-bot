"""What a user of the running stack waits for, measured from their side.

Logs in to Rocket.Chat as a test user, opens a DM with the bot, sends
questions one after another and watches the room over DDP (the same client
the bot uses). For each question it records, from the moment it was posted:

- ack_ms: the bot's reaction appears on the question;
- first_ms: the bot's first message appears (with streaming, the first words);
- done_ms: the bot's message is complete (no streaming cursor left).

The bot answers with whatever model the portal has configured, so this needs
the model switched on and a working key. Nothing on the bot is changed.

    .venv/bin/python bench/bench_e2e.py --user bench --password '...' \\
        --bot bot -n 10 --rc http://localhost:3000
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.rocketchat.ddp import DDPClient, IncomingMessage  # noqa: E402
from app.rocketchat.rest import RocketChatRest  # noqa: E402

CURSOR = "▍"
QUESTIONS = [
    "Xin chào! Bạn là ai?",
    "Giải thích ngắn gọn HTTP/2 khác HTTP/1.1 thế nào?",
    "Viết một hàm Python đảo ngược chuỗi.",
    "Liệt kê 3 mẹo viết commit message tốt.",
    "Hôm nay là thứ mấy?",
]


@dataclass
class Probe:
    question_id: str
    sent_at: float
    ack: float | None = None
    first: float | None = None
    done: float | None = None
    replies: set[str] = field(default_factory=set)
    finished: asyncio.Event = field(default_factory=asyncio.Event)


def summary(values: list[float]) -> dict:
    import math

    if not values:
        return {"p50": None, "p95": None, "max": None}
    ordered = sorted(values)

    def rank(p):
        return round(ordered[max(0, min(len(ordered) - 1, math.ceil(p / 100 * len(ordered)) - 1))], 1)

    return {"p50": rank(50), "p95": rank(95), "max": round(ordered[-1], 1)}


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rc", default="http://localhost:3000")
    parser.add_argument("--user", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--bot", default="bot", help="the bot's username")
    parser.add_argument("-n", "--messages", type=int, default=10)
    parser.add_argument("--timeout", type=float, default=90)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

    rest = RocketChatRest(args.rc)
    user_id, token = await rest.login(args.user, args.password)
    room_id = await rest.create_dm(args.bot)
    probes: dict[str, Probe] = {}
    current: list[Probe] = []

    async def on_message(message: IncomingMessage) -> None:
        now = time.monotonic()
        if message.room_id != room_id or not current:
            return
        probe = current[-1]
        raw = message.raw
        if message.message_id == probe.question_id:
            if raw.get("reactions") and probe.ack is None:
                probe.ack = now
            return
        if message.sender_id == user_id:
            return
        probe.replies.add(message.message_id)
        if probe.first is None:
            probe.first = now
        if not message.text.rstrip().endswith(CURSOR):
            probe.done = now
            probe.finished.set()

    ws_url = args.rc.replace("http", "ws", 1).rstrip("/") + "/websocket"
    ddp = DDPClient(ws_url, auth_token=token, on_message=on_message)
    runner = asyncio.create_task(ddp.run_forever())
    await asyncio.wait_for(ddp.connected.wait(), 20)
    await asyncio.sleep(1)

    missing = 0
    for n in range(args.messages):
        text = QUESTIONS[n % len(QUESTIONS)]
        sent_at = time.monotonic()
        posted = await rest.post_message(room_id, text)
        probe = Probe(posted["message"]["_id"], sent_at)
        probes[probe.question_id] = probe
        current.append(probe)
        try:
            await asyncio.wait_for(probe.finished.wait(), args.timeout)
        except TimeoutError:
            missing += 1
        # Let a split answer's later messages and the reaction removal pass.
        await asyncio.sleep(1.5)
        print(
            f"  {n + 1:3}: ack={_ms(probe.ack, sent_at)} first={_ms(probe.first, sent_at)} "
            f"done={_ms(probe.done, sent_at)}",
            file=sys.stderr, flush=True,
        )

    await ddp.stop()
    runner.cancel()
    await rest.aclose()

    def column(name: str) -> list[float]:
        return [(getattr(p, name) - p.sent_at) * 1000 for p in probes.values() if getattr(p, name) is not None]

    report = {
        "kind": "e2e",
        "date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "messages": args.messages,
        "concurrency": 1,
        "ack_ms": summary(column("ack")),
        "first_ms": summary(column("first")),
        "done_ms": summary(column("done")),
        "missing": missing,
    }
    out = args.out or BACKEND / "bench" / "results" / f"e2e-{datetime.now():%Y%m%d-%H%M}.json"
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(out)


def _ms(at: float | None, start: float) -> str:
    return "—" if at is None else f"{(at - start) * 1000:.0f}ms"


if __name__ == "__main__":
    asyncio.run(main())
