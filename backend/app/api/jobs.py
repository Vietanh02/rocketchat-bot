"""Work the assistant does in several steps (`app.agent.jobs`): see and stop it."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, status

from app.agent.jobs import JobService
from app.api.deps import AdminUser, CurrentUser, RuntimeDep, SessionDep, record_audit
from app.api.memory import _agent
from app.models import AgentJob
from app.runtime import Runtime
from app.schemas import JobOut, JobPhaseOut

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


def _jobs(runtime: Runtime) -> JobService:
    jobs = _agent(runtime).jobs
    if jobs is None:  # pragma: no cover - the responder always sets it
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="No jobs")
    return jobs


def _out(job: AgentJob) -> JobOut:
    return JobOut(
        id=job.id,
        status=job.status,  # type: ignore[arg-type]
        title=job.title,
        question=job.question,
        room_id=job.room_id,
        thread_id=job.thread_id,
        owner_username=job.owner_username,
        started_by=job.started_by,
        current_phase=job.current_phase,
        result=job.result,
        error=job.error,
        created_at=job.created_at,
        started_at=job.started_at,
        ended_at=job.ended_at,
        phases=[
            JobPhaseOut(
                ord=p.ord,
                goal=p.goal,
                status=p.status,
                output=p.output,
                error=p.error,
                attempts=p.attempts,
                started_at=p.started_at,
                ended_at=p.ended_at,
            )
            for p in job.phases
        ],
    )


@router.get("", response_model=list[JobOut])
async def list_jobs(
    _: CurrentUser, runtime: RuntimeDep, limit: int = Query(default=100, ge=1, le=500)
) -> list[JobOut]:
    return [_out(job) for job in await _jobs(runtime).list(limit=limit)]


@router.get("/{job_id}", response_model=JobOut)
async def read_job(job_id: int, _: CurrentUser, runtime: RuntimeDep) -> JobOut:
    job = await _jobs(runtime).get(job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such job")
    return _out(job)


@router.post("/{job_id}/cancel", response_model=JobOut)
async def cancel_job(
    job_id: int, admin: AdminUser, session: SessionDep, runtime: RuntimeDep
) -> JobOut:
    jobs = _jobs(runtime)
    if await jobs.get(job_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such job")
    if not await jobs.cancel(job_id):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="The job is not under way")
    record_audit(session, admin, "update", "agent_job", job_id, "cancelled")
    await session.commit()
    job = await jobs.get(job_id)
    assert job is not None
    return _out(job)
