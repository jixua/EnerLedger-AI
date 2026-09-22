"""Administrator-only interactive account management."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, Self

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.auth import ADMIN_USER_ID, get_current_admin, hash_admin_password
from app.domain.models import UserAccount
from app.domain.schemas import UtcTimestampModel
from app.rag.database import get_db

router = APIRouter(prefix="/api/v1/users", tags=["用户管理"])

AccountRole = Literal["admin", "user", "reviewer"]
AccountStatus = Literal["ACTIVE", "DISABLED"]


class UserCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=64)
    password: SecretStr = Field(min_length=8, max_length=256)
    role: AccountRole = "user"

    @field_validator("username")
    @classmethod
    def normalize_username(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("用户名不能为空")
        if any(character.isspace() for character in normalized):
            raise ValueError("用户名不能包含空白字符")
        return normalized


class UserUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: AccountRole | None = None
    status: AccountStatus | None = None

    @model_validator(mode="after")
    def validate_patch(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("至少需要更新一个字段")
        if any(getattr(self, name) is None for name in self.model_fields_set):
            raise ValueError("角色和状态不能为 null")
        return self


class PasswordReset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    password: SecretStr = Field(min_length=8, max_length=256)


class UserRead(UtcTimestampModel):
    id: int
    username: str
    role: AccountRole
    status: AccountStatus
    last_login_at: datetime | None
    created_at: datetime
    updated_at: datetime


def _payload(account: UserAccount) -> UserRead:
    return UserRead.model_validate(account, from_attributes=True)


async def _locked_account(db: AsyncSession, user_id: int) -> UserAccount:
    account = await db.scalar(
        select(UserAccount).where(UserAccount.id == user_id).with_for_update()
    )
    if account is None:
        raise HTTPException(status_code=404, detail="账号不存在")
    return account


async def _protect_last_admin(
    db: AsyncSession, account: UserAccount, *, next_role: str, next_status: str
) -> None:
    losing_admin = (
        account.role == "admin"
        and account.status == "ACTIVE"
        and (next_role != "admin" or next_status != "ACTIVE")
    )
    if not losing_admin:
        return
    active_admins = (
        await db.scalars(
            select(UserAccount)
            .where(UserAccount.role == "admin", UserAccount.status == "ACTIVE")
            .with_for_update()
        )
    ).all()
    if len(active_admins) <= 1:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "LAST_ADMIN", "message": "系统必须至少保留一个启用的管理员"},
        )


@router.get("", response_model=list[UserRead])
async def list_users(
    _: Annotated[dict[str, object], Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[UserRead]:
    accounts = (
        await db.scalars(select(UserAccount).order_by(UserAccount.created_at, UserAccount.id))
    ).all()
    return [_payload(account) for account in accounts]


@router.post("", response_model=UserRead, status_code=status.HTTP_201_CREATED)
async def create_user(
    payload: UserCreate,
    principal: Annotated[dict[str, object], Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> UserRead:
    account = UserAccount(
        username=payload.username,
        password_hash=hash_admin_password(payload.password.get_secret_value()),
        role=payload.role,
        status="ACTIVE",
        auth_version=1,
        created_by_user_id=int(principal["sub"]),
    )
    db.add(account)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail="用户名已存在") from exc
    await db.refresh(account)
    return _payload(account)


@router.patch("/{user_id}", response_model=UserRead)
async def update_user(
    user_id: int,
    payload: UserUpdate,
    _: Annotated[dict[str, object], Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> UserRead:
    account = await _locked_account(db, user_id)
    updates = payload.model_dump(exclude_unset=True)
    next_role = str(updates.get("role", account.role))
    next_status = str(updates.get("status", account.status))
    if account.id == ADMIN_USER_ID and (
        next_role != "admin" or next_status != "ACTIVE"
    ):
        raise HTTPException(
            status_code=409,
            detail={"code": "ROOT_PROTECTED", "message": "root 账号不能降级或禁用"},
        )
    await _protect_last_admin(db, account, next_role=next_role, next_status=next_status)
    changed = next_role != account.role or next_status != account.status
    account.role = next_role
    account.status = next_status
    if changed:
        account.auth_version += 1
    await db.commit()
    await db.refresh(account)
    return _payload(account)


@router.post("/{user_id}/reset-password", response_model=UserRead)
async def reset_user_password(
    user_id: int,
    payload: PasswordReset,
    _: Annotated[dict[str, object], Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> UserRead:
    account = await _locked_account(db, user_id)
    account.password_hash = hash_admin_password(payload.password.get_secret_value())
    account.auth_version += 1
    await db.commit()
    await db.refresh(account)
    return _payload(account)
