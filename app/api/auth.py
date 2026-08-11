"""Administrator login API. Registration is intentionally not exposed."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from app.domain.auth import authenticate_admin, create_access_token, get_current_admin
from app.domain.schemas import AdminLogin, AuthToken, CurrentAdmin
from app.rag.config import settings

router = APIRouter(prefix="/api/v1/auth", tags=["身份认证"])


@router.post("/login", response_model=AuthToken)
async def login(payload: AdminLogin) -> AuthToken:
    if not authenticate_admin(payload.username, payload.password.get_secret_value()):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "INVALID_CREDENTIALS", "message": "用户名或密码错误"},
            headers={"WWW-Authenticate": "Bearer"},
        )
    token, expires_at = create_access_token()
    return AuthToken(access_token=token, expires_at=expires_at)


@router.get("/me", response_model=CurrentAdmin)
async def current_admin(
    _: Annotated[dict[str, object], Depends(get_current_admin)],
) -> CurrentAdmin:
    return CurrentAdmin(user_id=1, username=settings.ADMIN_USERNAME, role="admin")
