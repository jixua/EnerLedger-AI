from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException

from app.domain.auth import (
    ADMIN_USER_ID,
    create_access_token,
    decode_access_token,
    hash_admin_password,
    verify_admin_password,
)


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
