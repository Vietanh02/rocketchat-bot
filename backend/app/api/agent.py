"""The assistant's settings (`agent_config`) and how fast its turns were (`agent_run`)."""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Query
from sqlalchemy import select

from app.api.deps import AdminUser, CurrentUser, RuntimeDep, SessionDep, record_audit
from app.bot.config_store import bump_config_version, ensure_agent_config
from app.models import AgentConfig, AgentRun
from app.schemas import (
    AgentConfigOut,
    AgentConfigUpdate,
    AgentRunOut,
    AgentStatsOut,
    Percentiles,
)

router = APIRouter(prefix="/api/agent", tags=["agent"])

# Runs looked at for the percentiles, newest first.
STATS_WINDOW = 1000


def _to_out(config: AgentConfig) -> AgentConfigOut:
    return AgentConfigOut(
        ack_reaction=config.ack_reaction,
        fast_model=config.fast_model,
        turn_deadline_seconds=config.turn_deadline_seconds,
        queue_mode=config.queue_mode,
        idle_reset_minutes=config.idle_reset_minutes,
        summarize_after_messages=config.summarize_after_messages,
        parallel_tools=config.parallel_tools,
    )


@router.get("", response_model=AgentConfigOut)
async def read_agent_config(_: CurrentUser, session: SessionDep) -> AgentConfigOut:
    config = await ensure_agent_config(session)
    await session.commit()
    return _to_out(config)


@router.put("", response_model=AgentConfigOut)
async def update_agent_config(
    payload: AgentConfigUpdate,
    admin: AdminUser,
    session: SessionDep,
    runtime: RuntimeDep,
) -> AgentConfigOut:
    config = await ensure_agent_config(session)
    changes = {k: v for k, v in payload.model_dump(exclude_unset=True).items() if v is not None}
    for field, value in changes.items():
        setattr(config, field, value)
    await bump_config_version(session)
    record_audit(
        session, admin, "update", "agent_config", config.id, ", ".join(sorted(changes)) or "(no changes)"
    )
    await session.commit()
    await session.refresh(config)
    await runtime.apply_config_change()
    return _to_out(config)


def percentiles(values: list[float]) -> Percentiles:
    """Nearest-rank p50/p95 and the maximum; empty when there are no values."""
    if not values:
        return Percentiles()
    ordered = sorted(values)

    def rank(p: float) -> float:
        index = max(0, min(len(ordered) - 1, math.ceil(p / 100 * len(ordered)) - 1))
        return round(ordered[index], 2)

    return Percentiles(p50=rank(50), p95=rank(95), max=round(ordered[-1], 2))


def _run_out(run: AgentRun) -> AgentRunOut:
    try:
        tools = json.loads(run.tools_json or "[]")
    except ValueError:
        tools = []
    try:
        labels = json.loads(run.timings_json or "{}").get("labels") or {}
    except (ValueError, AttributeError):
        labels = {}
    return AgentRunOut(
        id=run.id,
        started_at=run.started_at,
        trigger=run.trigger,
        status=run.status,
        model=run.model,
        rounds=run.rounds,
        tools=tools,
        ack_ms=run.ack_ms,
        ttft_ms=run.ttft_ms,
        total_ms=run.total_ms,
        overhead_ms=run.overhead_ms,
        error=run.error,
        intent=labels.get("intent"),
        kb=labels.get("kb"),
    )


@router.get("/stats", response_model=AgentStatsOut)
async def agent_stats(
    _: CurrentUser,
    session: SessionDep,
    hours: int = Query(default=24, ge=1, le=24 * 90),
    recent: int = Query(default=20, ge=0, le=200),
) -> AgentStatsOut:
    """Percentiles of the latest turns' timings (merged turns are left out)."""
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    runs = (
        await session.scalars(
            select(AgentRun)
            .where(AgentRun.started_at >= since, AgentRun.status != "merged")
            .order_by(AgentRun.id.desc())
            .limit(STATS_WINDOW)
        )
    ).all()

    def column(name: str) -> list[float]:
        return [float(v) for run in runs if (v := getattr(run, name)) is not None]

    return AgentStatsOut(
        runs=len(runs),
        since=runs[-1].started_at if runs else None,
        errors=sum(run.status == "error" for run in runs),
        timeouts=sum(run.status == "timeout" for run in runs),
        ack_ms=percentiles(column("ack_ms")),
        ttft_ms=percentiles(column("ttft_ms")),
        total_ms=percentiles(column("total_ms")),
        overhead_ms=percentiles(column("overhead_ms")),
        recent=[_run_out(run) for run in runs[:recent]],
    )
