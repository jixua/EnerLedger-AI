"""基于四表模型的文档召回可见性门禁。"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractAsyncContextManager
from typing import Protocol, TypeAlias, TypeVar

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import Dataset, Document
from app.rag.database import get_db_context
from app.rag.models.chunk_record import ChunkRecordDB

DEFAULT_READINESS_BATCH_SIZE = 500
SessionContext: TypeAlias = AbstractAsyncContextManager[AsyncSession]
SessionContextFactory: TypeAlias = Callable[[], SessionContext]


class RoutedHit(Protocol):
    @property
    def chunk_id(self) -> str: ...

    @property
    def doc_id(self) -> int: ...

    @property
    def dataset_id(self) -> int: ...


RoutedHitT = TypeVar("RoutedHitT", bound=RoutedHit)


class MySqlDocumentReadinessGate:
    """只允许属于当前用户且文档状态为 ``READY`` 的 Chunk 参与召回。"""

    def __init__(
        self,
        *,
        session_context_factory: SessionContextFactory = get_db_context,
        batch_size: int = DEFAULT_READINESS_BATCH_SIZE,
    ) -> None:
        if batch_size <= 0:
            raise ValueError("batch_size must be a positive int")
        self._session_context_factory = session_context_factory
        self._batch_size = batch_size

    async def filter_visible_hits(
        self,
        hits: Sequence[RoutedHitT],
        *,
        user_id: int,
    ) -> list[RoutedHitT]:
        if not hits:
            return []

        chunk_ids = list(dict.fromkeys(hit.chunk_id for hit in hits))
        visible_keys: set[tuple[str, int, int]] = set()
        async with self._session_context_factory() as db:
            for batch in self._iter_batches(chunk_ids):
                rows = (await db.execute(self._build_query(batch, user_id=user_id))).all()
                visible_keys.update(
                    (str(chunk_id), int(document_id), int(dataset_id))
                    for chunk_id, document_id, dataset_id in rows
                )

        return [hit for hit in hits if (hit.chunk_id, hit.doc_id, hit.dataset_id) in visible_keys]

    @staticmethod
    def _build_query(chunk_ids: Sequence[str], *, user_id: int):
        return (
            select(ChunkRecordDB.chunk_id, ChunkRecordDB.doc_id, ChunkRecordDB.set_id)
            .join(
                Document,
                and_(
                    Document.id == ChunkRecordDB.doc_id,
                    Document.dataset_id == ChunkRecordDB.set_id,
                    Document.user_id == ChunkRecordDB.user_id,
                ),
            )
            .join(
                Dataset,
                and_(
                    Dataset.id == ChunkRecordDB.set_id,
                    Dataset.user_id == ChunkRecordDB.user_id,
                ),
            )
            .where(
                ChunkRecordDB.chunk_id.in_(chunk_ids),
                ChunkRecordDB.user_id == user_id,
                Document.status == "READY",
                or_(
                    func.lower(Document.file_type) != "pdf",
                    and_(
                        Document.parse_quality_status == "PASSED",
                        func.upper(
                            func.json_unquote(
                                func.json_extract(Document.parse_quality, "$.status")
                            )
                        )
                        == "PASSED",
                    ),
                ),
                Dataset.status == "ACTIVE",
            )
        )

    def _iter_batches(self, values: Sequence[str]) -> Iterator[Sequence[str]]:
        for offset in range(0, len(values), self._batch_size):
            yield values[offset : offset + self._batch_size]
