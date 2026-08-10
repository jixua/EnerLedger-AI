"""数据集管理 API：模型绑定直接写入 ``dataset``。"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.auth import get_user_id
from app.domain.models import Dataset, Document
from app.domain.schemas import DatasetCreate, DatasetRead, DatasetUpdate
from app.rag.core.storage.manticore_bm25 import ManticoreBm25IndexingPipeline
from app.rag.database import get_db
from app.rag.models.db_models import LLMModelConfigDB
from app.services.document_queue import (
    DOCUMENT_STATUS_FAILED,
    DOCUMENT_STATUS_READY,
    reset_document_for_queue,
)

router = APIRouter(prefix="/api/v1/datasets", tags=["数据集"])


async def _drop_dataset_bm25_table(*, user_id: int, dataset_id: int) -> None:
    """Remove the per-dataset Manticore table after all documents are gone."""

    pipeline = ManticoreBm25IndexingPipeline(update_chunk_status=False)
    await pipeline.delete_by_dataset(user_id=user_id, dataset_id=dataset_id)


async def _lock_and_validate_model_bindings(
    db: AsyncSession,
    *,
    user_id: int,
    bindings: list[tuple[int, str, str]],
) -> None:
    """按 ID 稳定加锁并校验绑定，和配置停用/删除事务串行。"""

    if not bindings:
        return
    config_ids = sorted({config_id for config_id, _capability, _field in bindings})
    configs = (
        await db.scalars(
            select(LLMModelConfigDB)
            .where(LLMModelConfigDB.id.in_(config_ids))
            .order_by(LLMModelConfigDB.id)
            .with_for_update()
        )
    ).all()
    by_id = {config.id: config for config in configs}

    for config_id, expected_capability, field_name in bindings:
        config = by_id.get(config_id)
        if config is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "LLM_CONFIG_NOT_FOUND", "field": field_name},
            )
        if not config.is_active:
            raise HTTPException(
                status_code=409,
                detail={"code": "LLM_CONFIG_INACTIVE", "field": field_name},
            )
        if config.scope != "USER" or config.owner_user_id != user_id:
            raise HTTPException(
                status_code=403,
                detail={"code": "LLM_CONFIG_FORBIDDEN", "field": field_name},
            )
        if config.capability.upper() != expected_capability:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "LLM_CONFIG_CAPABILITY_MISMATCH",
                    "field": field_name,
                    "expected": expected_capability,
                    "actual": config.capability.upper(),
                },
            )


def _dataset_response(dataset: Dataset) -> DatasetRead:
    return DatasetRead(
        id=dataset.id,
        name=dataset.name,
        description=dataset.description,
        status=dataset.status,
        dense_embedding_config_id=dataset.dense_embedding_config_id,
        sparse_embedding_config_id=dataset.sparse_embedding_config_id,
        chat_config_id=dataset.chat_config_id,
        vision_config_id=dataset.vision_config_id,
        created_at=dataset.created_at,
        updated_at=dataset.updated_at,
    )


async def _owned_dataset(
    db: AsyncSession,
    *,
    dataset_id: int,
    user_id: int,
    for_update: bool = False,
) -> Dataset:
    statement = select(Dataset).where(
        Dataset.id == dataset_id,
        Dataset.user_id == user_id,
        Dataset.status == "ACTIVE",
    )
    if for_update:
        statement = statement.with_for_update()
    dataset = await db.scalar(statement)
    if dataset is None:
        raise HTTPException(status_code=404, detail="数据集不存在或无权访问")
    return dataset


@router.post("", response_model=DatasetRead, status_code=status.HTTP_201_CREATED)
async def create_dataset(
    payload: DatasetCreate,
    user_id: Annotated[int, Depends(get_user_id)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> DatasetRead:
    bindings = [
        (
            payload.dense_embedding_config_id,
            "EMBEDDING",
            "dense_embedding_config_id",
        ),
        (
            payload.sparse_embedding_config_id,
            "SPARSE_EMBEDDING",
            "sparse_embedding_config_id",
        ),
    ]
    if payload.chat_config_id is not None:
        bindings.append((payload.chat_config_id, "CHAT", "chat_config_id"))
    if payload.vision_config_id is not None:
        bindings.append((payload.vision_config_id, "VISION", "vision_config_id"))
    await _lock_and_validate_model_bindings(db, user_id=user_id, bindings=bindings)

    dataset = Dataset(
        user_id=user_id,
        name=payload.name,
        description=payload.description,
        status="ACTIVE",
        dense_embedding_config_id=payload.dense_embedding_config_id,
        sparse_embedding_config_id=payload.sparse_embedding_config_id,
        chat_config_id=payload.chat_config_id,
        vision_config_id=payload.vision_config_id,
    )
    try:
        db.add(dataset)
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail="当前用户下已存在同名数据集") from exc
    await db.refresh(dataset)
    return _dataset_response(dataset)


@router.get("", response_model=list[DatasetRead])
async def list_datasets(
    user_id: Annotated[int, Depends(get_user_id)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[DatasetRead]:
    datasets = (
        await db.scalars(
            select(Dataset)
            .where(Dataset.user_id == user_id, Dataset.status == "ACTIVE")
            .order_by(Dataset.updated_at.desc(), Dataset.id.desc())
        )
    ).all()
    return [_dataset_response(dataset) for dataset in datasets]


@router.get("/{dataset_id}", response_model=DatasetRead)
async def get_dataset(
    dataset_id: int,
    user_id: Annotated[int, Depends(get_user_id)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> DatasetRead:
    dataset = await _owned_dataset(db, dataset_id=dataset_id, user_id=user_id)
    return _dataset_response(dataset)


@router.patch("/{dataset_id}", response_model=DatasetRead)
async def update_dataset(
    dataset_id: int,
    payload: DatasetUpdate,
    user_id: Annotated[int, Depends(get_user_id)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> DatasetRead:
    """更新当前用户的数据集元数据和模型绑定。"""

    updates = payload.model_dump(exclude_unset=True)

    bindings = {
        "dense_embedding_config_id": "EMBEDDING",
        "sparse_embedding_config_id": "SPARSE_EMBEDDING",
        "chat_config_id": "CHAT",
        "vision_config_id": "VISION",
    }
    requested_bindings = [
        (updates[field_name], expected_capability, field_name)
        for field_name, expected_capability in bindings.items()
        if field_name in updates and updates[field_name] is not None
    ]
    await _lock_and_validate_model_bindings(
        db,
        user_id=user_id,
        bindings=requested_bindings,
    )
    dataset = await _owned_dataset(
        db,
        dataset_id=dataset_id,
        user_id=user_id,
        for_update=True,
    )

    parse_binding_changed = any(
        field_name in updates and updates[field_name] != getattr(dataset, field_name)
        for field_name in (
            "dense_embedding_config_id",
            "sparse_embedding_config_id",
            "vision_config_id",
        )
    )
    if parse_binding_changed:
        documents = (
            await db.scalars(
                select(Document)
                .where(
                    Document.dataset_id == dataset.id,
                    Document.user_id == user_id,
                )
                .with_for_update()
            )
        ).all()
        active = [
            document.id
            for document in documents
            if document.status not in {DOCUMENT_STATUS_READY, DOCUMENT_STATUS_FAILED}
        ]
        if active:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "DATASET_DOCUMENTS_ACTIVE",
                    "message": "仍有文档正在排队或处理，请等待完成后再更换解析模型绑定",
                    "document_ids": active,
                },
            )
        for document in documents:
            reset_document_for_queue(document, reparse=True)

    for field_name, value in updates.items():
        setattr(dataset, field_name, value)

    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail="当前用户下已存在同名数据集") from exc
    except Exception:
        await db.rollback()
        raise

    await db.refresh(dataset)
    return _dataset_response(dataset)


@router.delete("/{dataset_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_dataset(
    dataset_id: int,
    user_id: Annotated[int, Depends(get_user_id)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    """删除空数据集；存在文档时要求调用方先显式处理文档。"""

    dataset = await _owned_dataset(
        db,
        dataset_id=dataset_id,
        user_id=user_id,
        for_update=True,
    )
    document_id = await db.scalar(
        select(Document.id).where(Document.dataset_id == dataset.id).limit(1)
    )
    if document_id is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "DATASET_NOT_EMPTY", "message": "数据集存在文档，不能删除"},
        )

    try:
        await _drop_dataset_bm25_table(user_id=user_id, dataset_id=dataset.id)
    except Exception as exc:
        await db.rollback()
        raise HTTPException(
            status_code=502,
            detail={
                "code": "DATASET_INDEX_CLEANUP_FAILED",
                "message": "数据集检索索引清理失败，请稍后重试",
            },
        ) from exc

    await db.delete(dataset)
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        raise
