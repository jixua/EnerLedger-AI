"""当前无前端阶段的临时身份边界。"""

from typing import Annotated

from fastapi import Header, HTTPException, status


def get_user_id(
    x_user_id: Annotated[str, Header(alias="X-User-Id")],
) -> int:
    """从 ``X-User-Id`` 读取正整数用户 ID。

    这只是单体后端初期的信任边界，不等价于生产环境的身份认证。
    后续接入 JWT / SSO 时，其他路由可保持依赖这个函数而无需改业务签名。
    """

    try:
        user_id = int(x_user_id)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="X-User-Id 必须是正整数",
        ) from exc
    if user_id <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="X-User-Id 必须是正整数",
        )
    return user_id
