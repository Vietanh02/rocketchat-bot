"""Which tools the model may use for whom: the safe and trusted profiles."""

from __future__ import annotations

from fastapi import APIRouter

from app.agent.policy import SAFE, TRUSTED, ToolPolicySettings
from app.agent.tools.mcp import mcp_tool_name
from app.agent.tools.skills import READ, RUN
from app.api.deps import AdminUser, CurrentUser, RuntimeDep, SessionDep, record_audit
from app.bot.config_store import bump_config_version, ensure_tool_policy
from app.models import ToolPolicy
from app.schemas import PolicyToolOut, ToolPolicyOut, ToolPolicyUpdate

router = APIRouter(prefix="/api/policy", tags=["policy"])

FIELDS = ("trusted_usernames", "safe_allow", "safe_deny", "trusted_allow", "trusted_deny")
BUILT_IN = (
    ("kb_search", "knowledge base"),
    ("memory_save", "memory"),
    ("memory_search", "memory"),
    ("memory_forget", "memory"),
    ("profile_update", "memory"),
    ("start_job", "jobs"),
    (READ, "skills"),
    (RUN, "skills"),
)


def _out(config: ToolPolicy) -> ToolPolicyOut:
    return ToolPolicyOut(**{field: getattr(config, field) or "" for field in FIELDS})


@router.get("", response_model=ToolPolicyOut)
async def read_policy(_: CurrentUser, session: SessionDep) -> ToolPolicyOut:
    config = await ensure_tool_policy(session)
    await session.commit()
    return _out(config)


@router.put("", response_model=ToolPolicyOut)
async def update_policy(
    payload: ToolPolicyUpdate, admin: AdminUser, session: SessionDep, runtime: RuntimeDep
) -> ToolPolicyOut:
    config = await ensure_tool_policy(session)
    changes = {k: v for k, v in payload.model_dump(exclude_unset=True).items() if v is not None}
    for field, value in changes.items():
        setattr(config, field, value.strip())
    await bump_config_version(session)
    record_audit(session, admin, "update", "tool_policy", config.id, ", ".join(sorted(changes)) or "(no changes)")
    await session.commit()
    await session.refresh(config)
    await runtime.apply_config_change()
    return _out(config)


@router.get("/tools", response_model=list[PolicyToolOut])
async def tools_by_profile(_: CurrentUser, runtime: RuntimeDep) -> list[PolicyToolOut]:
    """Every tool the model could be offered, and which profiles get it."""
    snapshot = await runtime.config_store.current()
    policy: ToolPolicySettings = snapshot.policy
    names = [(name, kind) for name, kind in BUILT_IN]
    names += [(spec.name, "integration") for spec in snapshot.integrations.values() if spec.llm_tool]
    names += [(mcp_tool_name(t.server.name, t.name), f"MCP: {t.server.name}") for t in snapshot.mcp_tools]
    return [
        PolicyToolOut(
            name=name, kind=kind, safe=policy.permits(SAFE, name), trusted=policy.permits(TRUSTED, name)
        )
        for name, kind in names
    ]
