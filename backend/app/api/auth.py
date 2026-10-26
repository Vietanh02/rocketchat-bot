"""Portal authentication."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import func, select

from app.api.deps import AdminUser, CurrentUser, SessionDep, record_audit
from app.models import PortalUser, Role
from app.schemas import (
    LoginRequest,
    PasswordChange,
    TokenResponse,
    UserCreate,
    UserOut,
)
from app.security import create_access_token, hash_password, verify_password

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login", response_model=TokenResponse)
async def login(payload: LoginRequest, session: SessionDep) -> TokenResponse:
    user = await session.scalar(
        select(PortalUser).where(PortalUser.email == payload.email.lower())
    )
    # Same response for unknown email and wrong password, so the endpoint does
    # not confirm which accounts exist.
    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
        )
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="This account is disabled"
        )

    user.last_login_at = datetime.now(timezone.utc)
    token, expires_in = create_access_token(user.email, user.role)
    return TokenResponse(
        access_token=token,
        expires_in=expires_in,
        email=user.email,
        role=user.role,
    )


@router.get("/me", response_model=UserOut)
async def me(user: CurrentUser) -> PortalUser:
    return user


@router.post("/password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(
    payload: PasswordChange, user: CurrentUser, session: SessionDep
) -> None:
    if not verify_password(payload.current_password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Current password is incorrect",
        )
    user.password_hash = hash_password(payload.new_password)
    record_audit(session, user, "password_change", "portal_user", user.id)


@router.get("/users", response_model=list[UserOut])
async def list_users(_: AdminUser, session: SessionDep) -> list[PortalUser]:
    return list((await session.scalars(select(PortalUser).order_by(PortalUser.id))).all())


@router.post("/users", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def create_user(
    payload: UserCreate, admin: AdminUser, session: SessionDep
) -> PortalUser:
    email = payload.email.lower()
    existing = await session.scalar(
        select(func.count()).select_from(PortalUser).where(PortalUser.email == email)
    )
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A user with the email {email} already exists",
        )
    user = PortalUser(
        email=email,
        password_hash=hash_password(payload.password),
        role=payload.role or Role.admin.value,
    )
    session.add(user)
    await session.flush()
    record_audit(session, admin, "create", "portal_user", user.id, email)
    return user


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(user_id: int, admin: AdminUser, session: SessionDep) -> None:
    user = await session.get(PortalUser, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such user")
    if user.id == admin.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You cannot delete your own account",
        )
    remaining_admins = await session.scalar(
        select(func.count())
        .select_from(PortalUser)
        .where(PortalUser.role == Role.admin.value, PortalUser.id != user_id)
    )
    if not remaining_admins:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Refusing to remove the last admin",
        )
    await session.delete(user)
    record_audit(session, admin, "delete", "portal_user", user_id, user.email)
