"""单表 LLM 可执行配置 API。"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.auth import get_user_id
from app.domain.models import Dataset, Document
from app.domain.schemas import LLMConfigCreate, LLMConfigRead, LLMConfigUpdate
from app.rag.core.llm.encryption import decrypt_api_key, encrypt_api_key, mask_api_key
from app.rag.core.llm.factory import ModelFactory
from app.rag.database import get_db
from app.rag.models.db_models import LLMModelConfigDB

router = APIRouter(prefix="/api/v1/llm", tags=["模型配置"])

# ``LLMModelConfigDB`` 保留 provider_id 作为 LinkRag runtime snapshot 兼容字段，
# 当前单表控制面不再维护 provider catalog，因此统一写入占位值 1。
_COMPAT_PROVIDER_ID = 1

_CAPABILITY_VALUES = {
    "CHAT": "text",
    "EMBEDDING": "embedding",
    "SPARSE_EMBEDDING": "sparse_embedding",
    "RERANK": "rerank",
    "VISION": "vision",
}


def _validate_protocol_capability(protocol: str, capability: str) -> None:
    """使用已迁入的 ModelFactory 校验 adapter 及其能力，不发起网络请求。"""

    try:
        info = ModelFactory().get_provider_info(protocol)
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "UNSUPPORTED_PROTOCOL", "protocol": protocol},
        ) from exc

    required = _CAPABILITY_VALUES[capability]
    if required not in info["capabilities"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "UNSUPPORTED_PROTOCOL_CAPABILITY",
                "protocol": protocol,
                "capability": capability,
            },
        )


def _safe_mask_ciphertext(ciphertext: str) -> str:
    """只返回明文掩码；历史坏密文也绝不回显密文。"""

    try:
        return mask_api_key(decrypt_api_key(ciphertext))
    except Exception:  # noqa: BLE001 - 列表不应以密文作为错误信息泄露
        return "****"


def _config_response(config: LLMModelConfigDB) -> LLMConfigRead:
    return LLMConfigRead(
        id=config.id,
        scope=config.scope,
        owner_user_id=config.owner_user_id,
        provider_id=config.provider_id,
        provider_type=config.provider_type,
        model_name=config.model_name,
        display_name=config.display_name,
        capability=config.capability,
        protocol=config.protocol,
        api_base_url=config.api_base_url,
        api_key_masked=_safe_mask_ciphertext(config.api_key),
        is_active=config.is_active,
        snapshot_version=config.snapshot_version,
        created_at=config.created_at,
        updated_at=config.updated_at,
    )


async def _owned_config(
    db: AsyncSession,
    *,
    config_id: int,
    user_id: int,
    for_update: bool = False,
) -> LLMModelConfigDB:
    statement = select(LLMModelConfigDB).where(
        LLMModelConfigDB.id == config_id,
        LLMModelConfigDB.scope == "USER",
        LLMModelConfigDB.owner_user_id == user_id,
    )
    if for_update:
        statement = statement.with_for_update()
    config = await db.scalar(statement)
    if config is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="模型配置不存在或无权访问",
        )
    return config


@router.post("/configs", response_model=LLMConfigRead, status_code=status.HTTP_201_CREATED)
async def create_config(
    payload: LLMConfigCreate,
    user_id: Annotated[int, Depends(get_user_id)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> LLMConfigRead:
    """创建当前用户的单表 LLM 可执行快照。"""

    _validate_protocol_capability(payload.protocol, payload.capability)

    duplicate = await db.scalar(
        select(LLMModelConfigDB.id).where(
            LLMModelConfigDB.scope == "USER",
            LLMModelConfigDB.owner_user_id == user_id,
            LLMModelConfigDB.provider_type == payload.provider_type,
            LLMModelConfigDB.model_name == payload.model_name,
            LLMModelConfigDB.capability == payload.capability,
        )
    )
    if duplicate is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="当前用户的模型能力配置已存在",
        )

    config = LLMModelConfigDB(
        scope="USER",
        owner_user_id=user_id,
        provider_id=_COMPAT_PROVIDER_ID,
        provider_type=payload.provider_type,
        model_name=payload.model_name,
        display_name=payload.display_name,
        capability=payload.capability,
        protocol=payload.protocol,
        api_base_url=str(payload.api_base_url),
        api_key=encrypt_api_key(payload.api_key.get_secret_value().strip()),
        is_active=payload.is_active,
        snapshot_version=1,
    )
    try:
        db.add(config)
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="当前用户的模型能力配置已存在",
        ) from exc
    except Exception:
        await db.rollback()
        raise

    await db.refresh(config)
    return _config_response(config)


@router.get("/configs", response_model=list[LLMConfigRead])
async def list_configs(
    user_id: Annotated[int, Depends(get_user_id)],
    db: Annotated[AsyncSession, Depends(get_db)],
    capability: Annotated[str | None, Query(max_length=32)] = None,
    include_inactive: bool = False,
) -> list[LLMConfigRead]:
    """列出当前用户的 USER 配置，响应从不包含 API Key 密文。"""

    stmt = select(LLMModelConfigDB).where(
        LLMModelConfigDB.scope == "USER",
        LLMModelConfigDB.owner_user_id == user_id,
    )
    if capability:
        stmt = stmt.where(LLMModelConfigDB.capability == capability.strip().upper())
    if not include_inactive:
        stmt = stmt.where(LLMModelConfigDB.is_active.is_(True))

    configs = (
        await db.scalars(
            stmt.order_by(
                LLMModelConfigDB.capability,
                LLMModelConfigDB.provider_type,
                LLMModelConfigDB.model_name,
                LLMModelConfigDB.id,
            )
        )
    ).all()
    return [_config_response(config) for config in configs]


@router.patch("/configs/{config_id}", response_model=LLMConfigRead)
async def update_config(
    config_id: int,
    payload: LLMConfigUpdate,
    user_id: Annotated[int, Depends(get_user_id)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> LLMConfigRead:
    """更新当前用户的模型配置，并生成新的运行时快照版本。"""

    config = await _owned_config(
        db,
        config_id=config_id,
        user_id=user_id,
        for_update=True,
    )
    updates = payload.model_dump(exclude_unset=True)

    binding_filter = or_(
        Dataset.dense_embedding_config_id == config.id,
        Dataset.sparse_embedding_config_id == config.id,
        Dataset.chat_config_id == config.id,
    )
    deactivating = updates.get("is_active") is False and config.is_active
    if deactivating:
        bound_dataset_id = await db.scalar(select(Dataset.id).where(binding_filter).limit(1))
        if bound_dataset_id is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "LLM_CONFIG_IN_USE",
                    "message": "模型配置已被数据集绑定，请先完成重新绑定",
                },
            )

    model_changed = "model_name" in updates and updates["model_name"] != config.model_name
    if model_changed and config.capability.upper() in {"EMBEDDING", "SPARSE_EMBEDDING"}:
        indexed_document_id = await db.scalar(
            select(Document.id)
            .join(Dataset, Dataset.id == Document.dataset_id)
            .where(binding_filter)
            .limit(1)
        )
        if indexed_document_id is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "LLM_MODEL_REINDEX_REQUIRED",
                    "message": "该向量模型已有存量文档；请新建配置并在数据集设置中重新绑定",
                },
            )

    if "api_key" in updates:
        secret = updates.pop("api_key")
        config.api_key = encrypt_api_key(secret.get_secret_value().strip())
    if "api_base_url" in updates:
        updates["api_base_url"] = str(updates["api_base_url"])
    for field_name, value in updates.items():
        setattr(config, field_name, value)
    config.snapshot_version += 1

    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="当前用户的模型能力配置已存在",
        ) from exc
    except Exception:
        await db.rollback()
        raise

    await db.refresh(config)
    return _config_response(config)


@router.delete("/configs/{config_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_config(
    config_id: int,
    user_id: Annotated[int, Depends(get_user_id)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    """删除未被任何数据集绑定的当前用户模型配置。"""

    config = await _owned_config(
        db,
        config_id=config_id,
        user_id=user_id,
        for_update=True,
    )
    dataset_id = await db.scalar(
        select(Dataset.id)
        .where(
            or_(
                Dataset.dense_embedding_config_id == config.id,
                Dataset.sparse_embedding_config_id == config.id,
                Dataset.chat_config_id == config.id,
            )
        )
        .limit(1)
    )
    if dataset_id is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "LLM_CONFIG_IN_USE", "message": "模型配置已被数据集绑定"},
        )

    await db.delete(config)
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        raise
