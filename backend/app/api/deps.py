"""Shared FastAPI dependencies: auth, DB session, and the runtime handle."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import AuditLog, PortalUser, Role
from app.runtime import Runtime
from app.security import decode_access_token

_bearer = HTTPBearer(auto_error=False)

SessionDep = Annotated[AsyncSession, Depends(get_session)]


def get_runtime(request: Request) -> Runtime:
    runtime: Runtime | None = getattr(request.app.state, "runtime", None)
    if runtime is None:  # pragma: no cover - only if the lifespan did not run
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="application runtime is not ready",
        )
    return runtime


RuntimeDep = Annotated[Runtime, Depends(get_runtime)]


async def get_current_user(
    session: SessionDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)] = None,
) -> PortalUser:
    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    payload = decode_access_token(credentials.credentials)
    if payload is None or not payload.get("sub"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    user = await session.scalar(
        select(PortalUser).where(PortalUser.email == payload["sub"])
    )
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="User is not active"
        )
    return user


CurrentUser = Annotated[PortalUser, Depends(get_current_user)]


async def require_admin(user: CurrentUser) -> PortalUser:
    if user.role != Role.admin.value:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This action requires the admin role",
        )
    return user


AdminUser = Annotated[PortalUser, Depends(require_admin)]


def record_audit(
    session: AsyncSession,
    user: PortalUser | None,
    action: str,
    entity: str,
    entity_id: str | int | None = None,
    detail: str = "",
) -> None:
    session.add(
        AuditLog(
            actor=user.email if user else "system",
            action=action,
            entity=entity,
            entity_id=str(entity_id) if entity_id is not None else None,
            detail=detail[:2000],
        )
    )
