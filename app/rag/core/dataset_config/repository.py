"""从 ``dataset`` 表读取模型绑定的最小 Repository。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import Dataset


@dataclass(frozen=True)
class DatasetParseConfigSnapshot:
    user_id: int
    dataset_id: int
    sparse_embedding_config_id: int | None
    dense_embedding_config_id: int | None
    enhancement_chat_config_id: int | None
    enhancement_vision_config_id: int | None
    rerank_config_id: int | None
    chunking_config: dict[str, Any]
    enhancement_config: dict[str, Any]
    pdf_config: dict[str, Any]
    recall_config: dict[str, Any]
    is_active: bool


class DatasetParseConfigRepository:
    """保持 LinkRag 配置快照接口，事实源改为当前项目的 ``dataset`` 表。"""

    @staticmethod
    def _project(row: Dataset) -> DatasetParseConfigSnapshot:
        return DatasetParseConfigSnapshot(
            user_id=row.user_id,
            dataset_id=row.id,
            sparse_embedding_config_id=row.sparse_embedding_config_id,
            dense_embedding_config_id=row.dense_embedding_config_id,
            enhancement_chat_config_id=None,
            # 当前最小 schema 将 PDF OCR/视觉兜底模型直接绑定
            # 在 dataset 行，并复用 LinkRag 执行面的 vision 绑定槽位。
            enhancement_vision_config_id=row.vision_config_id,
            rerank_config_id=None,
            chunking_config={},
            enhancement_config={},
            pdf_config={"pdf_parser_backend": "opendataloader"},
            recall_config={},
            is_active=row.status == "ACTIVE",
        )

    async def get(
        self, user_id: int, dataset_id: int, db: AsyncSession
    ) -> DatasetParseConfigSnapshot | None:
        row = (
            await db.execute(
                select(Dataset).where(
                    Dataset.id == int(dataset_id),
                    Dataset.user_id == int(user_id),
                    Dataset.status == "ACTIVE",
                )
            )
        ).scalar_one_or_none()
        return self._project(row) if row is not None else None
