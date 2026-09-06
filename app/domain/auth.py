"""Configuration-backed JWT identities and role boundaries."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal, TypedDict

import jwt
from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import InvalidTokenError

from app.rag.config import settings

ADMIN_USER_ID = 1
REVIEWER_USER_ID = 2
_SCHEME = "scrypt"
_bearer = HTTPBearer(auto_error=False)


class AuthIdentity(TypedDict):
    user_id: int
    username: str
    role: Literal["admin", "reviewer"]


def hash_admin_password(password: str, *, salt: bytes | None = None) -> str:
    """Return a portable scrypt password hash for configuration/bootstrap tooling."""

    if not password:
        raise ValueError("管理员密码不能为空")
    actual_salt = salt or secrets.token_bytes(16)
    derived = hashlib.scrypt(
        password.encode("utf-8"),
        salt=actual_salt,
        n=2**14,
        r=8,
        p=1,
        dklen=32,
    )
    return ":".join(
        (
            _SCHEME,
            "16384",
            "8",
            "1",
            base64.urlsafe_b64encode(actual_salt).decode("ascii").rstrip("="),
            base64.urlsafe_b64encode(derived).decode("ascii").rstrip("="),
        )
    )


def verify_admin_password(password: str, encoded: str) -> bool:
    try:
        scheme, n, r, p, salt_text, hash_text = encoded.split(":", 5)
        if scheme != _SCHEME:
            return False
        salt = base64.urlsafe_b64decode(salt_text + "=" * (-len(salt_text) % 4))
        expected = base64.urlsafe_b64decode(hash_text + "=" * (-len(hash_text) % 4))
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
        )
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(actual, expected)


def authenticate_admin(username: str, password: str) -> bool:
    username_matches = hmac.compare_digest(username, settings.ADMIN_USERNAME)
    password_matches = verify_admin_password(password, settings.ADMIN_PASSWORD_HASH)
    return username_matches and password_matches


def authenticate_user(username: str, password: str) -> AuthIdentity | None:
    """Authenticate one of the explicitly configured interactive accounts."""

    if authenticate_admin(username, password):
        return {"user_id": ADMIN_USER_ID, "username": settings.ADMIN_USERNAME, "role": "admin"}

    reviewer_hash = settings.REVIEWER_PASSWORD_HASH.strip()
    if not reviewer_hash:
        return None
    username_matches = hmac.compare_digest(username, settings.REVIEWER_USERNAME)
    password_matches = verify_admin_password(password, reviewer_hash)
    if not username_matches or not password_matches:
        return None
    return {
        "user_id": REVIEWER_USER_ID,
        "username": settings.REVIEWER_USERNAME,
        "role": "reviewer",
    }


def _jwt_secret() -> bytes:
    configured = settings.JWT_SECRET.strip()
    if configured:
        return configured.encode("utf-8")
    # Keep JWT and API-key encryption keys cryptographically separated even when the
    # deployment chooses the single-secret bootstrap path.
    return hashlib.sha256(
        b"energy-carbon-rag:jwt:" + bytes.fromhex(settings.API_KEY_ENCRYPTION_SECRET)
    ).digest()


def create_access_token(
    *,
    identity: AuthIdentity | None = None,
    now: datetime | None = None,
) -> tuple[str, datetime]:
    issued_at = now or datetime.now(UTC)
    expires_at = issued_at + timedelta(minutes=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES)
    authenticated = identity or {
        "user_id": ADMIN_USER_ID,
        "username": settings.ADMIN_USERNAME,
        "role": "admin",
    }
    token = jwt.encode(
        {
            "sub": str(authenticated["user_id"]),
            "username": authenticated["username"],
            "role": authenticated["role"],
            "iat": issued_at,
            "exp": expires_at,
            "iss": settings.JWT_ISSUER,
            "aud": settings.JWT_AUDIENCE,
        },
        _jwt_secret(),
        algorithm="HS256",
    )
    return token, expires_at


def decode_access_token(token: str) -> dict[str, object]:
    try:
        payload = jwt.decode(
            token,
            _jwt_secret(),
            algorithms=["HS256"],
            issuer=settings.JWT_ISSUER,
            audience=settings.JWT_AUDIENCE,
            options={"require": ["sub", "exp", "iat", "iss", "aud"]},
        )
    except InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "INVALID_ACCESS_TOKEN", "message": "登录状态无效或已过期"},
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    expected_subject = {
        "admin": str(ADMIN_USER_ID),
        "reviewer": str(REVIEWER_USER_ID),
    }.get(str(payload.get("role")))
    if expected_subject is None or payload.get("sub") != expected_subject:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "ROLE_INVALID", "message": "账号角色无效"},
        )
    return payload


def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> dict[str, object]:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "AUTH_REQUIRED", "message": "请先登录"},
            headers={"WWW-Authenticate": "Bearer"},
        )
    return decode_access_token(credentials.credentials)


def _require_role(principal: dict[str, object], allowed_roles: set[str]) -> None:
    if principal.get("role") not in allowed_roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "PERMISSION_DENIED", "message": "当前账号无权执行此操作"},
        )


def get_current_admin(
    principal: Annotated[dict[str, object], Depends(get_current_user)],
) -> dict[str, object]:
    """Retain the admin-only dependency used by management endpoints."""

    _require_role(principal, {"admin"})
    return principal


def get_user_id(
    admin: Annotated[dict[str, object], Depends(get_current_admin)],
) -> int:
    """Map the authenticated administrator to the existing data ownership boundary."""

    _require_role(admin, {"admin"})
    return int(admin["sub"])


def get_shared_owner_user_id(
    principal: Annotated[dict[str, object], Depends(get_current_user)],
) -> int:
    """Map admin and reviewer reads/chat to the administrator-owned data boundary."""

    _require_role(principal, {"admin", "reviewer"})
    return ADMIN_USER_ID


def get_actor_user_id(
    principal: Annotated[dict[str, object], Depends(get_current_user)],
) -> int:
    """Return the authenticated actor for immutable audit attribution."""

    _require_role(principal, {"admin", "reviewer"})
    return int(principal["sub"])


def require_crawler_api_key(
    api_key: Annotated[str | None, Header(alias="X-Crawler-Api-Key")] = None,
) -> None:
    """Authenticate the machine-to-machine crawler upload boundary."""

    configured = settings.CRAWLER_UPLOAD_API_KEY.strip()
    if not configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "CRAWLER_UPLOAD_DISABLED",
                "message": "第三方爬虫上传入口尚未配置",
            },
        )
    if api_key is None or not hmac.compare_digest(api_key, configured):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "INVALID_CRAWLER_API_KEY", "message": "爬虫上传凭证无效"},
        )
