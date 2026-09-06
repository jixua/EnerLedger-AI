from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from app.domain.auth import (
    ADMIN_USER_ID,
    REVIEWER_USER_ID,
    authenticate_user,
    create_access_token,
    decode_access_token,
    get_actor_user_id,
    get_shared_owner_user_id,
    get_user_id,
    hash_admin_password,
    verify_admin_password,
)
from app.rag.config import settings


def test_scrypt_password_hash_never_contains_plaintext() -> None:
    encoded = hash_admin_password("test-password", salt=b"0123456789abcdef")

    assert encoded.startswith("scrypt:")
    assert "test-password" not in encoded
    assert verify_admin_password("test-password", encoded) is True
    assert verify_admin_password("wrong-password", encoded) is False


def test_access_token_contains_fixed_admin_identity() -> None:
    now = datetime.now(UTC)
    token, expires_at = create_access_token(now=now)

    payload = decode_access_token(token)

    assert payload["sub"] == str(ADMIN_USER_ID)
    assert payload["role"] == "admin"
    assert expires_at > now


def test_expired_access_token_is_rejected() -> None:
    token, _ = create_access_token(now=datetime.now(UTC) - timedelta(days=2))

    with pytest.raises(HTTPException) as exc_info:
        decode_access_token(token)

    assert exc_info.value.status_code == 401


def test_reviewer_account_is_disabled_without_a_password_hash(monkeypatch) -> None:
    monkeypatch.setattr(settings, "REVIEWER_USERNAME", "auditor")
    monkeypatch.setattr(settings, "REVIEWER_PASSWORD_HASH", "")

    assert authenticate_user("auditor", "secret") is None


def test_reviewer_token_keeps_actor_and_shared_owner_id_separate(monkeypatch) -> None:
    password_hash = hash_admin_password("review-secret", salt=b"reviewer-salt-01")
    monkeypatch.setattr(settings, "REVIEWER_USERNAME", "auditor")
    monkeypatch.setattr(settings, "REVIEWER_PASSWORD_HASH", password_hash)
    identity = authenticate_user("auditor", "review-secret")

    assert identity == {"user_id": REVIEWER_USER_ID, "username": "auditor", "role": "reviewer"}
    token, _ = create_access_token(identity=identity)
    principal = decode_access_token(token)

    assert get_shared_owner_user_id(principal) == ADMIN_USER_ID
    assert get_actor_user_id(principal) == REVIEWER_USER_ID
    with pytest.raises(HTTPException) as forbidden:
        get_user_id(principal)
    assert forbidden.value.status_code == 403


@pytest.mark.asyncio
async def test_reviewer_login_cannot_call_an_admin_only_api(monkeypatch) -> None:
    from app.main import app

    monkeypatch.setattr(settings, "REVIEWER_USERNAME", "auditor")
    monkeypatch.setattr(
        settings,
        "REVIEWER_PASSWORD_HASH",
        hash_admin_password("review-secret", salt=b"reviewer-salt-01"),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        login_response = await client.post(
            "/api/v1/auth/login",
            json={"username": "auditor", "password": "review-secret"},
        )
        assert login_response.status_code == 200
        token = login_response.json()["access_token"]
        me_response = await client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        forbidden_response = await client.get(
            "/api/v1/system/status",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert me_response.json()["role"] == "reviewer"
    assert forbidden_response.status_code == 403
    assert forbidden_response.json()["detail"]["code"] == "PERMISSION_DENIED"
