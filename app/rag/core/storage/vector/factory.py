"""向量门面装配。

召回只需要 query 编码与 Qdrant 查询，因此默认构造轻量门面，不加载旧的状态机、
补偿或 Wiki 仓储。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.rag.core.storage.qdrant import QdrantIndexStore

from .facade import VectorStorageFacade

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.rag.core.splitter.embedding_pipeline import ChunkEmbeddingPipeline
    from app.rag.core.storage.chunks import ChunkRepository


class _UnconfiguredEmbeddingPipeline:
    batch_size = 1
    embedding_model = None
    embedder = None

    async def aembed_query(self, _query):
        raise RuntimeError("dataset dense embedding resolved model is required")

    async def aembed_chunks(self, _chunks):
        raise RuntimeError("dataset dense embedding resolved model is required")


def create_vector_storage_facade(
    *,
    embedding_pipeline: ChunkEmbeddingPipeline,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    repository: ChunkRepository | None = None,
    qdrant_store: QdrantIndexStore | None = None,
    qdrant_client: Any | None = None,
    wiki_repository: Any | None = None,
) -> VectorStorageFacade:
    """兼容完整门面装配；旧写入状态机只在显式调用本函数时延迟导入。"""
    from app.rag.core.storage.chunks import ChunkRepository
    from app.rag.database import get_async_session_factory

    from .compensation_pipeline import VectorStorageCompensationPipeline
    from .draft_factory import ChunkDraftFactory
    from .management_pipeline import VectorStorageManagementPipeline
    from .pipeline import VectorStoragePipeline

    resolved_session_factory = session_factory or get_async_session_factory()
    resolved_repository = repository or ChunkRepository()
    resolved_qdrant_store = qdrant_store or QdrantIndexStore(client=qdrant_client)
    storage_service = VectorStoragePipeline(
        session_factory=resolved_session_factory,
        draft_factory=ChunkDraftFactory(),
        repository=resolved_repository,
        qdrant_store=resolved_qdrant_store,
        embedding_pipeline=embedding_pipeline,
        sparse_vector_service=None,
    )
    management_service = VectorStorageManagementPipeline(
        session_factory=resolved_session_factory,
        repository=resolved_repository,
        qdrant_store=resolved_qdrant_store,
        embedding_pipeline=embedding_pipeline,
        sparse_vector_service=None,
        wiki_repository=wiki_repository,
    )
    compensation_service = VectorStorageCompensationPipeline(
        session_factory=resolved_session_factory,
        repository=resolved_repository,
        qdrant_store=resolved_qdrant_store,
        embedding_pipeline=embedding_pipeline,
        sparse_vector_service=None,
    )
    return VectorStorageFacade(
        storage_service=storage_service,
        management_service=management_service,
        compensation_service=compensation_service,
        qdrant_store=resolved_qdrant_store,
        embedding_pipeline=embedding_pipeline,
    )


def compose_vector_storage_facade(
    *,
    embedding_pipeline: ChunkEmbeddingPipeline | None = None,
    qdrant_store: QdrantIndexStore | None = None,
    qdrant_client: Any | None = None,
    **_unused,
) -> VectorStorageFacade:
    """构造三路召回使用的轻量门面。"""
    return VectorStorageFacade(
        qdrant_store=qdrant_store or QdrantIndexStore(client=qdrant_client),
        embedding_pipeline=embedding_pipeline or _UnconfiguredEmbeddingPipeline(),
    )
