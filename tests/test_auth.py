from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from app.domain.auth import (
    ADMIN_USER_ID,
    authenticate_user,
    create_access_token,
    decode_access_token,
    get_actor_user_id,
    get_review_actor_user_id,
    get_review_owner_user_id,
    get_shared_owner_user_id,
    get_user_id,
    hash_admin_password,
    verify_admin_password,
)
from app.domain.models import UserAccount
from app.rag.database import get_db


class _AccountSession:
    def __init__(self, account: UserAccount):
        self.account = account
        self.commits = 0

    async def scalar(self, _statement):
        return self.account

    async def get(self, _model, key):
        return self.account if self.account.id == key else None

    async def commit(self):
        self.commits += 1


def _account(*, role: str, status: str = "ACTIVE", user_id: int = 7) -> UserAccount:
    return UserAccount(
        id=user_id,
        username=f"account-{user_id}",
        password_hash=hash_admin_password("test-password", salt=b"0123456789abcdef"),
        role=role,
        status=status,
        auth_version=3,
    )


def _principal(*, role: str, user_id: int = 7) -> dict[str, object]:
    return {"sub": str(user_id), "role": role, "username": f"account-{user_id}", "ver": 3}


def test_scrypt_password_hash_never_contains_plaintext() -> None:
    encoded = hash_admin_password("test-password", salt=b"0123456789abcdef")

    assert encoded.startswith("scrypt:")
    assert "test-password" not in encoded
    assert verify_admin_password("test-password", encoded) is True
    assert verify_admin_password("wrong-password", encoded) is False


def test_access_token_contains_versioned_admin_identity() -> None:
    now = datetime.now(UTC)
    token, expires_at = create_access_token(now=now)

    payload = decode_access_token(token)

    assert payload["sub"] == str(ADMIN_USER_ID)
    assert payload["role"] == "admin"
    assert payload["ver"] == 1
    assert expires_at > now


def test_expired_access_token_is_rejected() -> None:
    token, _ = create_access_token(now=datetime.now(UTC) - timedelta(days=2))

    with pytest.raises(HTTPException) as exc_info:
        decode_access_token(token)

    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_database_account_authentication_honors_status_and_password() -> None:
    active = _account(role="user")
    db = _AccountSession(active)

    identity = await authenticate_user(db, active.username, "test-password")

    assert identity == {
        "user_id": active.id,
        "username": active.username,
        "role": "user",
        "auth_version": 3,
    }
    assert db.commits == 1
    assert await authenticate_user(db, active.username, "wrong-password") is None

    active.status = "DISABLED"
    assert await authenticate_user(db, active.username, "test-password") is None


def test_role_boundaries_separate_business_and_review_access() -> None:
    reviewer = _principal(role="reviewer")
    normal = _principal(role="user")

    assert get_review_owner_user_id(reviewer) == ADMIN_USER_ID
    assert get_review_actor_user_id(reviewer) == 7
    for dependency in (get_user_id, get_shared_owner_user_id, get_actor_user_id):
        with pytest.raises(HTTPException) as forbidden:
            dependency(reviewer)
        assert forbidden.value.status_code == 403

    assert get_user_id(normal) == ADMIN_USER_ID
    assert get_shared_owner_user_id(normal) == ADMIN_USER_ID
    assert get_actor_user_id(normal) == 7
    for dependency in (get_review_owner_user_id, get_review_actor_user_id):
        with pytest.raises(HTTPException) as forbidden:
            dependency(normal)
        assert forbidden.value.status_code == 403


@pytest.mark.asyncio
async def test_reviewer_login_cannot_call_business_api() -> None:
    from app.main import app

    account = _account(role="reviewer", user_id=9)
    db = _AccountSession(account)

    async def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            login_response = await client.post(
                "/api/v1/auth/login",
                json={"username": account.username, "password": "test-password"},
            )
            assert login_response.status_code == 200
            token = login_response.json()["access_token"]
            headers = {"Authorization": f"Bearer {token}"}
            me_response = await client.get("/api/v1/auth/me", headers=headers)
            forbidden_response = await client.get("/api/v1/system/status", headers=headers)
    finally:
        app.dependency_overrides.clear()

    assert me_response.json()["role"] == "reviewer"
    assert forbidden_response.status_code == 403
    assert forbidden_response.json()["detail"]["code"] == "PERMISSION_DENIED"


@pytest.mark.asyncio
async def test_normal_user_cannot_call_review_api() -> None:
    from app.main import app

    account = _account(role="user", user_id=10)
    db = _AccountSession(account)

    async def override_db():
        yield db

    token, _ = create_access_token(
        identity={
            "user_id": account.id,
            "username": account.username,
            "role": "user",
            "auth_version": account.auth_version,
        }
    )
    app.dependency_overrides[get_db] = override_db
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(
                "/api/v1/crawler/submissions",
                headers={"Authorization": f"Bearer {token}"},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "PERMISSION_DENIED"


@pytest.mark.asyncio
async def test_auth_version_change_invalidates_existing_token() -> None:
    from app.main import app

    account = _account(role="user", user_id=11)
    db = _AccountSession(account)
    token, _ = create_access_token(
        identity={
            "user_id": account.id,
            "username": account.username,
            "role": "user",
            "auth_version": account.auth_version - 1,
        }
    )

    async def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(
                "/api/v1/auth/me",
                headers={"Authorization": f"Bearer {token}"},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "ACCOUNT_CHANGED"
