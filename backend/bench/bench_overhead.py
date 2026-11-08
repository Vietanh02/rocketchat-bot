"""How much time the bot's own Python code adds to an answer, offline.

Runs the real path a chat message takes (BotService._handle → Responder →
SessionQueue → AgentRunner → LLMChat with the OpenAI SDK → LiveReply/deliver →
agent_run) on a real SQLite database in a scratch directory. Only the edges
are fake: the model is an in-process streaming endpoint with a configurable
time to first token and token rate, Rocket.Chat is an in-memory gateway
(optionally with latency), and integration tools sleep instead of calling out.

Each turn's `TurnTimer` splits its total into waiting (model, tools,
Rocket.Chat, database) and the rest, `overhead_ms`: the bot's own work, the
only part a faster language could shrink. CPU time and peak RSS of the
process are measured per scenario.

    .venv/bin/python bench/bench_overhead.py            # full matrix
    .venv/bin/python bench/bench_overhead.py --quick    # a smaller one
    .venv/bin/python bench/report.py bench/results/overhead-*.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import resource
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

_TMP = Path(tempfile.mkdtemp(prefix="rcbot-bench-"))
os.environ.update(
    APP_SECRET_KEY="bench-secret-key-0123456789-abcdefghij",
    APP_DATABASE_URL=f"sqlite+aiosqlite:///{_TMP / 'bench.db'}",
    APP_BOOTSTRAP_ADMIN_EMAIL="admin@example.com",
    APP_BOOTSTRAP_ADMIN_PASSWORD="bench-admin-password",
    APP_ALLOW_PRIVATE_INTEGRATION_HOSTS="true",
    APP_RC_REST_URL="http://rocketchat.invalid:3000",
    APP_RC_WS_URL="ws://rocketchat.invalid:3000/websocket",
    APP_RC_BOT_USER_ID="",
    APP_RC_BOT_AUTH_TOKEN="",
    APP_RC_BOT_USERNAME="",
    APP_RC_BOT_PASSWORD="",
    APP_LOG_LEVEL="ERROR",
)

import httpx  # noqa: E402
import psutil  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.agent.tools import integrations as integration_tools  # noqa: E402
from app.bootstrap import run_bootstrap  # noqa: E402
from app.bot.config_store import ensure_agent_config, ensure_llm_config  # noqa: E402
from app.bot.llm import LLMService  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.integrations.executor import IntegrationResult  # noqa: E402
from app.models import AgentRun, BotConfig, Integration  # noqa: E402
from app.rocketchat.ddp import IncomingMessage  # noqa: E402
from app.rocketchat.gateway import FakeGateway  # noqa: E402
from app.runtime import Runtime  # noqa: E402
from app.security import encrypt_secret  # noqa: E402

# --------------------------------------------------------------------------- #
# Fakes at the edges
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class ModelProfile:
    ttft_ms: float = 300.0
    tokens_per_second: float = 80.0
    answer_tokens: int = 120


class FakeModel:
    """An OpenAI-compatible streaming endpoint with realistic pacing."""

    WORDS = (
        "Đây là **câu trả lời** mẫu với vài từ tiếng Việt, một `đoạn code` và "
        "danh sách:\n- mục một\n- mục hai\n"
    ).split(" ")

    def __init__(self, profile: ModelProfile, tool_calls: int) -> None:
        self.profile = profile
        self.tool_calls = tool_calls
        self.requests = 0

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests += 1
        body = json.loads(request.content)
        wants_tools = (
            self.tool_calls
            and body.get("tools")
            and body["messages"][-1]["role"] != "tool"
        )
        stream = bool(body.get("stream"))
        profile = self.profile

        def chunk(delta: dict) -> bytes:
            event = {
                "id": "c", "object": "chat.completion.chunk", "created": 0, "model": body["model"],
                "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
            }
            return f"data: {json.dumps(event)}\n\n".encode()

        if wants_tools:
            tools = [t["function"]["name"] for t in body["tools"]][: self.tool_calls]
            deltas = [
                {"tool_calls": [{
                    "index": n, "id": f"call_{n}", "type": "function",
                    "function": {"name": name, "arguments": json.dumps({"city": "Hanoi"})},
                }]}
                for n, name in enumerate(tools)
            ]
        else:
            words = [self.WORDS[n % len(self.WORDS)] + " " for n in range(profile.answer_tokens)]
            deltas = [{"content": word} for word in words]

        if not stream:
            await asyncio.sleep(
                (profile.ttft_ms + 1000 * len(deltas) / profile.tokens_per_second) / 1000
            )
            message: dict = {"role": "assistant", "content": None}
            if wants_tools:
                message["tool_calls"] = [d["tool_calls"][0] for d in deltas]
            else:
                message["content"] = "".join(d["content"] for d in deltas)
            return httpx.Response(200, json={
                "id": "c", "object": "chat.completion", "created": 0, "model": body["model"],
                "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
            })

        async def events():
            await asyncio.sleep(profile.ttft_ms / 1000)
            gap = 1 / profile.tokens_per_second
            for delta in deltas:
                yield chunk(delta)
                await asyncio.sleep(gap)
            yield b"data: [DONE]\n\n"

        return httpx.Response(200, content=events(), headers={"content-type": "text/event-stream"})


class SlowGateway(FakeGateway):
    """The in-memory gateway, with a delay standing in for Rocket.Chat's REST API."""

    def __init__(self, latency_ms: float) -> None:
        super().__init__()
        self.latency = latency_ms / 1000

    async def _wait(self) -> None:
        if self.latency:
            await asyncio.sleep(self.latency)

    async def send_message(self, room_id, text, *, thread_id=None):
        await self._wait()
        return await super().send_message(room_id, text, thread_id=thread_id)

    async def update_message(self, room_id, message_id, text):
        await self._wait()
        return await super().update_message(room_id, message_id, text)

    async def react(self, message_id, emoji, *, on=True):
        await self._wait()
        return await super().react(message_id, emoji, on=on)


TOOL_LATENCY_MS = 40.0


async def fake_execute_integration(spec, *, params, base_context, **_):
    await asyncio.sleep(TOOL_LATENCY_MS / 1000)
    data = {"city": params.get("city", "?"), "temp_c": 31, "conditions": "nắng", "spec": spec.name}
    return IntegrationResult(ok=True, text=json.dumps(data), status_code=200, data=data)


# --------------------------------------------------------------------------- #
# Scenarios
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class Scenario:
    name: str
    concurrency: int = 1  # conversations talking at the same time
    turns: int = 3  # messages per conversation, one after another
    history: int = 0  # earlier messages sent with each question
    tools: int = 0  # tool calls the model makes before answering
    mode: str = "stream"  # stream|whole
    question_chars: int = 40
    rc_latency_ms: float = 0.0


def matrix(quick: bool) -> list[Scenario]:
    full = [
        Scenario("baseline", 1, 5),
        Scenario("history-20", 1, 5, history=20),
        Scenario("history-200", 1, 5, history=200),
        Scenario("tools-3", 1, 5, history=20, tools=3),
        Scenario("long-prompt", 1, 5, history=20, question_chars=4000),
        Scenario("whole-mode", 10, 3, history=20, mode="whole"),
        Scenario("c10", 10, 3, history=20),
        Scenario("c10-tools", 10, 3, history=20, tools=3),
        Scenario("c50", 50, 3, history=20),
        Scenario("c50-h200-tools", 50, 3, history=200, tools=3),
        Scenario("c10-rc-latency", 10, 3, history=20, rc_latency_ms=15),
    ]
    if quick:
        return [s for s in full if s.name in ("baseline", "tools-3", "c10", "c50")]
    return full


def incoming(n: int, room: str, text: str) -> IncomingMessage:
    return IncomingMessage(
        message_id=f"M-{room}-{n}", room_id=room, text=text,
        sender_id=f"U-{room}", sender_username=f"user{room}", room_type="d",
    )


async def configure(runtime: Runtime, scenario: Scenario, tools: int) -> None:
    async with runtime.session_factory() as db:
        bot = await db.get(BotConfig, 1)
        bot.responder_mode = "llm"
        bot.bot_username = "bot"
        bot.reply_mode = scenario.mode
        bot.stream_interval_ms = 250
        bot.config_version += 1
        llm = await ensure_llm_config(db)
        llm.base_url = "http://llm.invalid/v1"
        llm.api_key_enc = encrypt_secret("sk-bench")
        llm.model = "bench-model"
        llm.history_messages = scenario.history
        agent = await ensure_agent_config(db)
        agent.ack_reaction = ":eyes:"
        agent.summarize_after_messages = 0  # keep the history size fixed
        for existing in (await db.scalars(select(Integration))).all():
            await db.delete(existing)
        for n in range(tools):
            db.add(Integration(
                name=f"weather_{n}", description="Current weather in a city",
                base_url="https://weather.invalid", method="GET", path_template="/now",
                query_template="city={{ params.city }}", llm_tool=True,
            ))
        await db.commit()
    runtime.config_store.invalidate()


async def seed_history(runtime: Runtime, room: str, messages: int) -> None:
    agent = runtime.bot.responder.agent
    from app.bot.types import MessageContext

    ctx = MessageContext.from_incoming(incoming(0, room, "seed"), bot_username="bot")
    state = await agent.sessions.open(f"dm:{room}", "dm", ctx)
    for n in range(messages // 2):
        await agent.sessions.append(
            state, f"Câu hỏi trước đó số {n} về một chủ đề nào đó?",
            f"Câu trả lời số {n}, dài vừa phải, có **định dạng** và `code`. " * 3,
        )


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    import math

    return round(ordered[max(0, min(len(ordered) - 1, math.ceil(p / 100 * len(ordered)) - 1))], 3)


def summary(values: list[float]) -> dict:
    return {
        "p50": percentile(values, 50), "p95": percentile(values, 95),
        "p99": percentile(values, 99), "max": round(max(values), 3) if values else None,
        "mean": round(sum(values) / len(values), 3) if values else None,
    }


async def run_scenario(runtime: Runtime, scenario: Scenario, profile: ModelProfile, index: int) -> dict:
    model = FakeModel(profile, scenario.tools)
    runtime.bot.responder.llm = LLMService(
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(model))
    )
    gateway = SlowGateway(scenario.rc_latency_ms)
    runtime.bot._gateway = gateway
    await configure(runtime, scenario, max(scenario.tools, 0))
    # Reloading the configuration happens only after a portal change; keep it
    # out of the measured turns.
    await runtime.config_store.current()
    rooms = [f"S{index}R{n}" for n in range(scenario.concurrency)]
    for room in rooms:
        if scenario.history:
            await seed_history(runtime, room, scenario.history)

    async with runtime.session_factory() as db:
        first_run = (await db.scalar(select(AgentRun.id).order_by(AgentRun.id.desc()).limit(1))) or 0

    question = ("Thời tiết Hà Nội hôm nay thế nào và tôi nên mặc gì? " * 200)[: scenario.question_chars]
    slots = asyncio.Semaphore(get_settings().bot_max_concurrency)
    process = psutil.Process()
    peak_rss = process.memory_info().rss
    sampling = True

    async def sample() -> None:
        nonlocal peak_rss
        while sampling:
            peak_rss = max(peak_rss, process.memory_info().rss)
            await asyncio.sleep(0.05)

    async def conversation(room: str) -> list[float]:
        walls = []
        for n in range(scenario.turns):
            async with slots:
                begin = time.monotonic()
                await runtime.bot._handle(incoming(n + 1, room, question))
                walls.append((time.monotonic() - begin) * 1000)
        return walls

    sampler = asyncio.create_task(sample())
    cpu_before = resource.getrusage(resource.RUSAGE_SELF)
    wall_begin = time.monotonic()
    walls = [w for ws in await asyncio.gather(*(conversation(r) for r in rooms)) for w in ws]
    wall = time.monotonic() - wall_begin
    cpu_after = resource.getrusage(resource.RUSAGE_SELF)
    # Background wrap-up (reaction removal, agent_run rows).
    for _ in range(5):
        pending = list(runtime.bot.responder._background)
        if not pending:
            break
        await asyncio.gather(*pending, return_exceptions=True)
    sampling = False
    await sampler

    async with runtime.session_factory() as db:
        rows = (await db.scalars(select(AgentRun).where(AgentRun.id > first_run))).all()
    cpu = (cpu_after.ru_utime - cpu_before.ru_utime) + (cpu_after.ru_stime - cpu_before.ru_stime)
    timings = [json.loads(r.timings_json) for r in rows]
    waits = {k: summary([t["waits"].get(k, 0.0) for t in timings]) for k in ("llm", "tools", "rc", "db", "queue")}
    turns = len(rows)
    result = {
        "scenario": asdict(scenario),
        "turns": turns,
        "statuses": {s: sum(r.status == s for r in rows) for s in {r.status for r in rows}},
        "wall_seconds": round(wall, 3),
        "turns_per_second": round(turns / wall, 2) if wall else None,
        "cpu_seconds": round(cpu, 3),
        # Share of one core used while the scenario ran.
        "cpu_percent_of_core": round(100 * cpu / wall, 1) if wall else None,
        "cpu_ms_per_turn": round(1000 * cpu / turns, 2) if turns else None,
        "peak_rss_mb": round(peak_rss / 2**20, 1),
        "model_requests": model.requests,
        "rc_calls": len(gateway.sent) + len(gateway.updates),
        "overhead_ms": summary([r.overhead_ms for r in rows if r.overhead_ms is not None]),
        "total_ms": summary([float(r.total_ms) for r in rows if r.total_ms is not None]),
        "ttft_ms": summary([float(r.ttft_ms) for r in rows if r.ttft_ms is not None]),
        "ack_ms": summary([float(r.ack_ms) for r in rows if r.ack_ms is not None]),
        "handle_wall_ms": summary(walls),
        "waits_ms": waits,
    }
    await runtime.bot.responder.llm.aclose()
    return result


async def micro() -> dict:
    """Per-call cost of the CPU-bound helpers on the reply path, in microseconds."""
    import timeit

    from app.agent.prompt import build_messages
    from app.bot.rcformat import to_rocketchat
    from app.bot.splitting import split_message

    answer = (
        "## Kết quả\n\n**Hà Nội** hôm nay *nắng*, 31°C.\n\n| Giờ | Nhiệt độ |\n|---|---|\n"
        + "".join(f"| {h}:00 | {25 + h % 7}°C |\n" for h in range(12))
        + "\n```python\nprint('xin chào')\n```\n\n- mục một\n- mục hai\n"
    ) * 4
    history = [{"role": "user" if n % 2 == 0 else "assistant", "content": "nội dung " * 40} for n in range(200)]
    long_text = ("đoạn văn " * 120 + "\n\n") * 40
    request = {"model": "m", "messages": history, "stream": True}
    cases = {
        "rcformat.to_rocketchat (2.6k chars)": lambda: to_rocketchat(answer),
        "rcformat partial, per streamed edit": lambda: to_rocketchat(answer[:1200], partial=True),
        "split_message (40k chars)": lambda: split_message(long_text),
        "build_messages (200 history)": lambda: build_messages(
            "You are {bot}. Today is {today}.", bot_name="bot", timezone_name="Asia/Ho_Chi_Minh",
            summary="", history=history, question="q",
        ),
        "json.dumps request (200 history)": lambda: json.dumps(request),
    }
    out = {}
    for name, fn in cases.items():
        number = 200
        best = min(timeit.repeat(fn, number=number, repeat=5)) / number
        out[name] = round(best * 1e6, 1)
    return out


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--quick", action="store_true")
    parser.add_argument("--ttft-ms", type=float, default=300)
    parser.add_argument("--tps", type=float, default=80, help="model tokens per second")
    parser.add_argument("--answer-tokens", type=int, default=120)
    parser.add_argument("--only", nargs="*", help="scenario names")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    try:
        import uvloop  # noqa: F401

        loop_name = "uvloop"
    except ImportError:
        loop_name = "asyncio"

    await run_bootstrap(SessionLocal, get_settings())
    runtime = Runtime.build(SessionLocal)
    profile = ModelProfile(args.ttft_ms, args.tps, args.answer_tokens)
    scenarios = [s for s in matrix(args.quick) if not args.only or s.name in args.only]
    results = []
    try:
        integration_tools.execute_integration = fake_execute_integration
        for index, scenario in enumerate(scenarios):
            print(f"… {scenario.name}", file=sys.stderr, flush=True)
            result = await run_scenario(runtime, scenario, profile, index)
            results.append(result)
            o, t = result["overhead_ms"], result["total_ms"]
            print(
                f"  {scenario.name:16} turns={result['turns']:4} overhead p50={o['p50']}ms "
                f"p95={o['p95']}ms total p50={t['p50']}ms cpu={result['cpu_percent_of_core']}% "
                f"rss={result['peak_rss_mb']}MB",
                file=sys.stderr, flush=True,
            )
    finally:
        await runtime.stop()

    report = {
        "kind": "overhead",
        "date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "python": sys.version.split()[0],
        "event_loop": loop_name,
        "cpus": os.cpu_count(),
        "memory_mb": round(psutil.virtual_memory().total / 2**20),
        "model_profile": asdict(profile),
        "tool_latency_ms": TOOL_LATENCY_MS,
        "bot_max_concurrency": get_settings().bot_max_concurrency,
        "scenarios": results,
        "micro_us": await micro(),
    }
    out = args.out or BACKEND / "bench" / "results" / f"overhead-{datetime.now():%Y%m%d-%H%M}.json"
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(out)


if __name__ == "__main__":
    try:
        import uvloop

        uvloop.install()
    except ImportError:
        pass
    asyncio.run(main())
