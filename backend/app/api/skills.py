"""Skills found in the skills folder: what each is for, and whether it is offered."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from app.agent.skills import Skill, SkillCatalog, SkillError, read_file, skills_root
from app.api.deps import AdminUser, CurrentUser, RuntimeDep, SessionDep, record_audit
from app.api.memory import _agent
from app.runtime import Runtime
from app.schemas import SkillDetailOut, SkillOut, SkillUpdate

router = APIRouter(prefix="/api/skills", tags=["skills"])


def _catalog(runtime: Runtime) -> SkillCatalog:
    return _agent(runtime).skills


def _out(skill: Skill) -> dict:
    return dict(
        name=skill.name,
        title=skill.title or skill.name,
        description=skill.description,
        enabled=skill.enabled,
        available=skill.available,
        missing=skill.missing,
        error=skill.error,
        scripts=skill.scripts,
        files=skill.files,
    )


async def _find(runtime: Runtime, name: str) -> Skill:
    catalog = _catalog(runtime)
    catalog.invalidate()
    for skill in await catalog.all():
        if skill.name == name:
            return skill
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such skill")


@router.get("", response_model=list[SkillOut])
async def list_skills(_: CurrentUser, runtime: RuntimeDep) -> list[SkillOut]:
    catalog = _catalog(runtime)
    catalog.invalidate()
    return [SkillOut(**_out(skill)) for skill in await catalog.all()]


@router.get("/folder")
async def skills_folder(_: CurrentUser) -> dict:
    root = skills_root()
    return {"path": str(root), "exists": root.is_dir()}


@router.get("/{name}", response_model=SkillDetailOut)
async def read_skill(name: str, _: CurrentUser, runtime: RuntimeDep) -> SkillDetailOut:
    skill = await _find(runtime, name)
    try:
        text = read_file(skill)
    except (SkillError, OSError) as exc:
        text = f"(cannot read: {exc})"
    return SkillDetailOut(**_out(skill), text=text)


@router.put("/{name}", response_model=SkillOut)
async def set_skill(
    name: str, payload: SkillUpdate, admin: AdminUser, session: SessionDep, runtime: RuntimeDep
) -> SkillOut:
    await _find(runtime, name)
    await _catalog(runtime).set_enabled(name, payload.enabled)
    record_audit(session, admin, "update", "skill", None, f"{name}: {'on' if payload.enabled else 'off'}")
    await session.commit()
    return SkillOut(**_out(await _find(runtime, name)))
