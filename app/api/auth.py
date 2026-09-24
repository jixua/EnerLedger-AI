"""Interactive account login API. Registration is intentionally not exposed."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.auth import authenticate_user, create_access_token, get_current_user
from app.domain.schemas import AdminLogin, AuthToken, CurrentAdmin
from app.rag.database import get_db

router = APIRouter(prefix="/api/v1/auth", tags=["身份认证"])


@router.post("/login", response_model=AuthToken)
async def login(
    payload: AdminLogin,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AuthToken:
    identity = await authenticate_user(
        db, payload.username, payload.password.get_secret_value()
    )
    if identity is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "INVALID_CREDENTIALS", "message": "用户名或密码错误"},
            headers={"WWW-Authenticate": "Bearer"},
        )
    token, expires_at = create_access_token(identity=identity)
    return AuthToken(access_token=token, expires_at=expires_at)


@router.get("/me", response_model=CurrentAdmin)
async def current_admin(
    principal: Annotated[dict[str, object], Depends(get_current_user)],
) -> CurrentAdmin:
    return CurrentAdmin(
        user_id=int(principal["sub"]),
        username=str(principal["username"]),
        role=str(principal["role"]),
    )
