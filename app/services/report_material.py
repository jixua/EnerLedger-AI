"""对话直传材料的暂存区：上传时落盘，创建报告时被消费。

单独一段暂存而不是把全文塞进创建请求，是因为报告材料动辄十几万字符：每轮对话重传
一遍既撑大请求体，也会把会话记录撑爆。上传返回一个 ``material_id``，之后只传这个 id。

正文按对象存储存放（与 ``Document`` 同样的「元数据进库、字节进 MinIO」约定），表里
只留指针。消费后删对象并置 ``consumed_at``；没被消费的行由 ``purge_expired_materials``
按时间清理。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import tempfile
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import ReportMaterial
from app.domain.time import utc_now
from app.rag.config import settings
from app.rag.services.storage.base import BaseObjectStorage

_MAX_MATERIAL_BYTES = 16 * 1024 * 1024


def material_object_key(*, user_id: int, material_id: str) -> str:
    return f"report-materials/{int(user_id)}/{material_id}.txt"


def encode_material_text(text: str) -> bytes:
    return text.encode("utf-8")


async def store_material(
    storage: BaseObjectStorage,
    *,
    user_id: int,
    filename: str,
    text: str,
    page_count: int | None = None,
) -> ReportMaterial:
    material_id = str(uuid.uuid4())
    data = encode_material_text(text)
    bucket = settings.MINIO_PRIVATE_BUCKET
    object_key = material_object_key(user_id=user_id, material_id=material_id)
    await asyncio.to_thread(
        storage.upload_bytes, bucket, object_key, data, "text/plain; charset=utf-8"
    )
    return ReportMaterial(
        id=material_id,
        user_id=int(user_id),
        filename=filename,
        char_count=len(text),
        page_count=page_count,
        bucket=bucket,
        object_key=object_key,
        content_hash=hashlib.sha256(data).hexdigest(),
    )


async def read_material_text(
    storage: BaseObjectStorage,
    *,
    material: ReportMaterial,
    max_bytes: int = _MAX_MATERIAL_BYTES,
) -> bytes:
    temp_path: Path | None = None
    try:
        descriptor, temp_name = tempfile.mkstemp(prefix="report-material-", suffix=".txt")
        temp_path = Path(temp_name)
        os.close(descriptor)
        await asyncio.to_thread(
            storage.download_to_path, material.bucket, material.object_key, temp_path
        )
        size = temp_path.stat().st_size
        if size > max_bytes:
            raise ValueError("上传的材料超过读取上限")
        data = temp_path.read_bytes()
        if hashlib.sha256(data).hexdigest() != str(material.content_hash):
            raise ValueError("上传的材料内容与登记不一致，请重新上传")
        return data
    finally:
        if temp_path is not None:
            with contextlib.suppress(OSError):
                temp_path.unlink()


async def purge_material(
    storage: BaseObjectStorage, *, material: ReportMaterial
) -> None:
    """尽力删对象；对象已不在也不该让调用方失败。"""
    with contextlib.suppress(Exception):
        await asyncio.to_thread(
            storage.remove_object, material.bucket, material.object_key
        )


async def purge_expired_materials(
    db: AsyncSession,
    storage: BaseObjectStorage,
    *,
    older_than: datetime,
    limit: int = 200,
) -> int:
    """清理没被消费、又已经过期的暂存材料。返回清理条数。"""
    rows = (
        await db.scalars(
            select(ReportMaterial)
            .where(ReportMaterial.created_at < older_than)
            .order_by(ReportMaterial.created_at)
            .limit(limit)
        )
    ).all()
    for material in rows:
        await purge_material(storage, material=material)
    if rows:
        await db.execute(
            delete(ReportMaterial).where(
                ReportMaterial.id.in_([material.id for material in rows])
            )
        )
        await db.commit()
    return len(rows)


def material_expired_before(*, days: int = 7) -> datetime:
    """暂存材料的保留期：上传后一直没用上的，超过这个时间就清掉。"""
    return utc_now() - timedelta(days=days)
