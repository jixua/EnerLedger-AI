"""Database-backed JWT identities and role boundaries."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal, TypedDict, cast

import jwt
from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import InvalidTokenError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import UserAccount
from app.domain.time import utc_now
from app.rag.config import settings
from app.rag.database import get_async_session_factory, get_db

ADMIN_USER_ID = 1
REVIEWER_USER_ID = 2
_SCHEME = "scrypt"
_bearer = HTTPBearer(auto_error=False)


class AuthIdentity(TypedDict):
    user_id: int
    username: str
    role: Literal["admin", "user", "reviewer"]
    auth_version: int


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


async def ensure_bootstrap_accounts() -> None:
    """Create the fixed root account and migrate the optional legacy reviewer once."""

    session_factory = get_async_session_factory()
    async with session_factory() as db:
        root = await db.get(UserAccount, ADMIN_USER_ID)
        if root is None:
            db.add(
                UserAccount(
                    id=ADMIN_USER_ID,
                    username=settings.ADMIN_USERNAME.strip(),
                    password_hash=settings.ADMIN_PASSWORD_HASH,
                    role="admin",
                    status="ACTIVE",
                    auth_version=1,
                )
            )
        if settings.REVIEWER_PASSWORD_HASH.strip():
            reviewer = await db.get(UserAccount, REVIEWER_USER_ID)
            if reviewer is None:
                db.add(
                    UserAccount(
                        id=REVIEWER_USER_ID,
                        username=settings.REVIEWER_USERNAME.strip(),
                        password_hash=settings.REVIEWER_PASSWORD_HASH,
                        role="reviewer",
                        status="ACTIVE",
                        auth_version=1,
                        created_by_user_id=ADMIN_USER_ID,
                    )
                )
        try:
            await db.commit()
        except IntegrityError:
            # Multiple API workers may bootstrap concurrently. A competing successful
            # insert is harmless; any other conflict is surfaced by the root check.
            await db.rollback()
        persisted_root = await db.get(UserAccount, ADMIN_USER_ID)
        if persisted_root is None or persisted_root.role != "admin":
            raise RuntimeError("root 管理员账号初始化失败")


async def authenticate_user(
    db: AsyncSession, username: str, password: str
) -> AuthIdentity | None:
    """Authenticate an active database account and update its last-login timestamp."""

    normalized = username.strip()
    if not normalized:
        return None
    account = await db.scalar(select(UserAccount).where(UserAccount.username == normalized))
    # Keep unknown/disabled usernames on the same expensive password path so the
    # login response does not become a practical account-enumeration oracle.
    encoded_hash = account.password_hash if account is not None else settings.ADMIN_PASSWORD_HASH
    password_matches = verify_admin_password(password, encoded_hash)
    if (
        account is None
        or account.status != "ACTIVE"
        or account.role not in {"admin", "user", "reviewer"}
        or not password_matches
    ):
        return None
    account.last_login_at = utc_now()
    await db.commit()
    return {
        "user_id": account.id,
        "username": account.username,
        "role": cast(Literal["admin", "user", "reviewer"], account.role),
        "auth_version": account.auth_version,
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
        "auth_version": 1,
    }
    token = jwt.encode(
        {
            "sub": str(authenticated["user_id"]),
            "username": authenticated["username"],
            "role": authenticated["role"],
            "ver": authenticated["auth_version"],
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
            options={"require": ["sub", "role", "ver", "exp", "iat", "iss", "aud"]},
        )
    except InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "INVALID_ACCESS_TOKEN", "message": "登录状态无效或已过期"},
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    if payload.get("role") not in {"admin", "user", "reviewer"}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "ROLE_INVALID", "message": "账号角色无效"},
        )
    try:
        if int(str(payload.get("sub"))) <= 0 or int(payload.get("ver", 0)) <= 0:
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "INVALID_ACCESS_TOKEN", "message": "登录状态无效或已过期"},
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    return payload


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, object]:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "AUTH_REQUIRED", "message": "请先登录"},
            headers={"WWW-Authenticate": "Bearer"},
        )
    payload = decode_access_token(credentials.credentials)
    account = await db.get(UserAccount, int(str(payload["sub"])))
    if (
        account is None
        or account.status != "ACTIVE"
        or account.role != payload.get("role")
        or account.auth_version != int(payload["ver"])
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "ACCOUNT_CHANGED", "message": "账号状态已变更，请重新登录"},
            headers={"WWW-Authenticate": "Bearer"},
        )
    return {
        **payload,
        "username": account.username,
        "role": account.role,
        "auth_version": account.auth_version,
    }


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
    principal: Annotated[dict[str, object], Depends(get_current_user)],
) -> int:
    """Map administrators and normal users to the shared business workspace."""

    _require_role(principal, {"admin", "user"})
    return ADMIN_USER_ID


def get_shared_owner_user_id(
    principal: Annotated[dict[str, object], Depends(get_current_user)],
) -> int:
    """Map chat/business access to the shared root-owned data boundary."""

    _require_role(principal, {"admin", "user"})
    return ADMIN_USER_ID


def get_actor_user_id(
    principal: Annotated[dict[str, object], Depends(get_current_user)],
) -> int:
    """Return the authenticated actor for immutable audit attribution."""

    _require_role(principal, {"admin", "user"})
    return int(principal["sub"])


def get_workspace_owner_user_id(
    principal: Annotated[dict[str, object], Depends(get_current_user)],
) -> int:
    """Allow all roles to read the dataset names needed by their permitted UI."""

    _require_role(principal, {"admin", "user", "reviewer"})
    return ADMIN_USER_ID


def get_review_owner_user_id(
    principal: Annotated[dict[str, object], Depends(get_current_user)],
) -> int:
    """Map administrators and reviewers to the shared review queue."""

    _require_role(principal, {"admin", "reviewer"})
    return ADMIN_USER_ID


def get_review_actor_user_id(
    principal: Annotated[dict[str, object], Depends(get_current_user)],
) -> int:
    """Return the administrator/reviewer who made an immutable review decision."""

    _require_role(principal, {"admin", "reviewer"})
    return int(principal["sub"])


def require_crawler_api_key(
    api_key: Annotated[str | None, Header(alias="X-Crawler-Api-Key")] = None,
    submission_api_key: Annotated[
        str | None,
        Header(alias="X-Document-Submission-Key"),
    ] = None,
) -> None:
    """Authenticate the external document upload boundary.

    ``X-Crawler-Api-Key`` remains supported for existing integrations. New
    clients should use the format-neutral ``X-Document-Submission-Key``.
    """

    configured = settings.CRAWLER_UPLOAD_API_KEY.strip()
    if not configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "CRAWLER_UPLOAD_DISABLED",
                "message": "外部文档提交入口尚未配置",
            },
        )
    supplied = submission_api_key or api_key
    if supplied is None or not hmac.compare_digest(supplied, configured):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "INVALID_CRAWLER_API_KEY", "message": "外部文档提交凭证无效"},
        )
