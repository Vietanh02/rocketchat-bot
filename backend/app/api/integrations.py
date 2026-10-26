"""Integration CRUD plus a live "Test" call."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.api.deps import AdminUser, CurrentUser, RuntimeDep, SessionDep, record_audit
from app.bot.config_store import bump_config_version
from app.integrations.executor import (
    IntegrationError,
    IntegrationSpec,
    bind_params,
    execute_integration,
)
from app.integrations.templating import validate
from app.models import Command, Integration, IntegrationParam
from app.schemas import (
    IntegrationCreate,
    IntegrationOut,
    IntegrationParamIn,
    IntegrationTestRequest,
    IntegrationTestResult,
    IntegrationUpdate,
)
from app.security import encrypt_secret

router = APIRouter(prefix="/api/integrations", tags=["integrations"])

TEMPLATE_FIELDS = (
    "path_template",
    "query_template",
    "headers_template",
    "body_template",
    "reply_template",
)
SCALAR_FIELDS = (
    "name",
    "description",
    "base_url",
    "method",
    "auth_type",
    "auth_username",
    "auth_header_name",
    "timeout_seconds",
    "enabled",
    *TEMPLATE_FIELDS,
)


def _to_out(integration: Integration) -> IntegrationOut:
    out = IntegrationOut.model_validate(integration)
    out.has_secret = bool(integration.auth_secret_enc)
    return out


def _check_templates(data: dict) -> None:
    for field in TEMPLATE_FIELDS:
        error = validate(data.get(field) or "")
        if error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"{field} is invalid: {error}",
            )


def _check_url(base_url: str | None) -> None:
    if base_url and not base_url.lower().startswith(("http://", "https://")):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="base_url must start with http:// or https://",
        )


async def _replace_params(
    session: SessionDep, integration: Integration, params: list[IntegrationParamIn]
) -> None:
    names = [p.name for p in params]
    if len(names) != len(set(names)):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Parameter names must be unique",
        )
    greedy_positions = [i for i, p in enumerate(params) if p.greedy]
    if greedy_positions and greedy_positions != [len(params) - 1]:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Only the last parameter may be greedy",
        )
    # The collection may be unloaded (a just-flushed new row), and touching an
    # unloaded collection outside the async greenlet raises. Load it first.
    await session.refresh(integration, ["params"])

    # Flush the removals before inserting: otherwise autoflush emits the new
    # INSERTs first and a reused parameter name trips the unique constraint.
    if integration.params:
        integration.params.clear()
        await session.flush()

    integration.params = [
        IntegrationParam(
            name=p.name,
            description=p.description,
            position=index,
            required=p.required,
            default_value=p.default_value,
            greedy=p.greedy,
        )
        for index, p in enumerate(params)
    ]


async def _load(session: SessionDep, integration_id: int) -> Integration:
    integration = await session.get(
        Integration, integration_id, options=[selectinload(Integration.params)]
    )
    if integration is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No such integration"
        )
    return integration


@router.get("", response_model=list[IntegrationOut])
async def list_integrations(
    _: CurrentUser, session: SessionDep
) -> list[IntegrationOut]:
    rows = list(
        (
            await session.scalars(
                select(Integration)
                .options(selectinload(Integration.params))
                .order_by(Integration.name)
            )
        ).all()
    )
    return [_to_out(row) for row in rows]


@router.post("", response_model=IntegrationOut, status_code=status.HTTP_201_CREATED)
async def create_integration(
    payload: IntegrationCreate,
    admin: AdminUser,
    session: SessionDep,
    runtime: RuntimeDep,
) -> IntegrationOut:
    data = payload.model_dump()
    _check_templates(data)
    _check_url(payload.base_url)
    if await session.scalar(
        select(Integration).where(Integration.name == payload.name)
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"An integration named {payload.name!r} already exists",
        )

    integration = Integration(**{k: data[k] for k in SCALAR_FIELDS})
    integration.auth_secret_enc = encrypt_secret(payload.auth_secret or None)
    session.add(integration)
    await session.flush()
    await _replace_params(session, integration, payload.params)
    await session.flush()
    await bump_config_version(session)
    record_audit(
        session, admin, "create", "integration", integration.id, integration.name
    )
    await session.commit()
    await runtime.apply_config_change()
    return _to_out(await _load(session, integration.id))


@router.put("/{integration_id}", response_model=IntegrationOut)
async def update_integration(
    integration_id: int,
    payload: IntegrationUpdate,
    admin: AdminUser,
    session: SessionDep,
    runtime: RuntimeDep,
) -> IntegrationOut:
    integration = await _load(session, integration_id)
    changes = payload.model_dump(exclude_unset=True)
    _check_templates({**{f: getattr(integration, f) for f in TEMPLATE_FIELDS}, **changes})
    _check_url(changes.get("base_url"))

    if "name" in changes and changes["name"] != integration.name:
        clash = await session.scalar(
            select(Integration).where(
                Integration.name == changes["name"], Integration.id != integration_id
            )
        )
        if clash:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"An integration named {changes['name']!r} already exists",
            )

    for field in SCALAR_FIELDS:
        if field in changes and changes[field] is not None:
            setattr(integration, field, changes[field])

    if "auth_secret" in changes:
        integration.auth_secret_enc = encrypt_secret(changes["auth_secret"] or None)
    if payload.params is not None:
        await _replace_params(session, integration, payload.params)

    await bump_config_version(session)
    record_audit(
        session, admin, "update", "integration", integration.id, ", ".join(sorted(changes))
    )
    await session.commit()
    await runtime.apply_config_change()
    return _to_out(await _load(session, integration_id))


@router.delete("/{integration_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_integration(
    integration_id: int, admin: AdminUser, session: SessionDep, runtime: RuntimeDep
) -> None:
    integration = await _load(session, integration_id)
    linked = list(
        (
            await session.scalars(
                select(Command.name).where(Command.integration_id == integration_id)
            )
        ).all()
    )
    if linked:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Still used by command(s): " + ", ".join(linked),
        )
    name = integration.name
    await session.delete(integration)
    await bump_config_version(session)
    record_audit(session, admin, "delete", "integration", integration_id, name)
    await session.commit()
    await runtime.apply_config_change()


@router.post("/{integration_id}/test", response_model=IntegrationTestResult)
async def test_integration(
    integration_id: int,
    payload: IntegrationTestRequest,
    _: AdminUser,
    session: SessionDep,
) -> IntegrationTestResult:
    """Call the API for real, with strict templates so typos surface."""
    integration = await _load(session, integration_id)
    spec = IntegrationSpec.from_model(integration)

    base_context = {
        "user": {"id": "portal-test", "username": payload.username, "mention": f"@{payload.username}"},
        "room": {"id": "portal-test", "type": "d"},
        "message": {"id": "portal-test", "text": ""},
        "args": " ".join(payload.params.values()),
        "argv": list(payload.params.values()),
        "groups": {},
        "command": {"name": spec.name, "pattern": spec.name},
    }

    try:
        params = bind_params(spec.params, [], named=payload.params)
        result = await execute_integration(
            spec, params=params, base_context=base_context, strict=True
        )
    except IntegrationError as exc:
        return IntegrationTestResult(ok=False, error=str(exc))

    return IntegrationTestResult(
        ok=result.ok,
        status_code=result.status_code,
        url=result.url,
        reply=result.text,
        raw=result.data,
        error=result.error,
        duration_ms=result.duration_ms,
    )
