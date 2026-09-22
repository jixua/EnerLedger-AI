from datetime import datetime

import pytest
from fastapi import HTTPException

from app.api.users import PasswordReset, UserUpdate, reset_user_password, update_user
from app.domain.auth import hash_admin_password, verify_admin_password
from app.domain.models import UserAccount


def _user(
    *, user_id: int, role: str, status: str = "ACTIVE", auth_version: int = 1
) -> UserAccount:
    now = datetime(2026, 9, 22, 10, 0, 0)
    return UserAccount(
        id=user_id,
        username="root" if user_id == 1 else f"user-{user_id}",
        password_hash=hash_admin_password("old-password", salt=b"0123456789abcdef"),
        role=role,
        status=status,
        auth_version=auth_version,
        created_at=now,
        updated_at=now,
    )


class _ScalarRows:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows


class _UserSession:
    def __init__(self, account: UserAccount, active_admins: list[UserAccount] | None = None):
        self.account = account
        self.active_admins = active_admins or []
        self.commits = 0

    async def scalar(self, _statement):
        return self.account

    async def scalars(self, _statement):
        return _ScalarRows(self.active_admins)

    async def commit(self):
        self.commits += 1

    async def refresh(self, _account):
        return None


@pytest.mark.asyncio
async def test_root_cannot_be_downgraded_or_disabled() -> None:
    root = _user(user_id=1, role="admin")
    db = _UserSession(root, [root])

    with pytest.raises(HTTPException) as exc_info:
        await update_user(
            1,
            UserUpdate(role="user"),
            {"sub": "1", "role": "admin"},
            db,
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "ROOT_PROTECTED"
    assert db.commits == 0


@pytest.mark.asyncio
async def test_last_active_admin_cannot_be_removed() -> None:
    admin = _user(user_id=8, role="admin")
    db = _UserSession(admin, [admin])

    with pytest.raises(HTTPException) as exc_info:
        await update_user(
            8,
            UserUpdate(status="DISABLED"),
            {"sub": "1", "role": "admin"},
            db,
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "LAST_ADMIN"


@pytest.mark.asyncio
async def test_role_change_and_password_reset_revoke_existing_tokens() -> None:
    account = _user(user_id=8, role="user", auth_version=4)
    root = _user(user_id=1, role="admin")
    db = _UserSession(account, [root])

    updated = await update_user(
        8,
        UserUpdate(role="reviewer"),
        {"sub": "1", "role": "admin"},
        db,
    )

    assert updated.role == "reviewer"
    assert account.auth_version == 5

    await reset_user_password(
        8,
        PasswordReset(password="new-password"),
        {"sub": "1", "role": "admin"},
        db,
    )

    assert account.auth_version == 6
    assert verify_admin_password("new-password", account.password_hash)
