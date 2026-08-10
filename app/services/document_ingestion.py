"""不依赖解析状态表的文档解析与三路索引编排。

本模块只复用已迁入当前项目的 LinkRag 底层实现，不调用
``ParseTaskPipeline``、``SparseIndexingPipeline`` 或 ``Preprocessor``。
"""

from __future__ import annotations

import asyncio
import inspect
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePath, PurePosixPath
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import Document
from app.rag.config import settings
from app.rag.core.dataset_config.execution_context import (
    DatasetExecutionContextLoader,
    DatasetExecutionPurpose,
)
from app.rag.core.encoding.sparse.factory import build_sparse_vector_service
from app.rag.core.llm.provider_lifecycle import aclose_dataset_execution_contexts
from app.rag.core.parse_task_service import ParseTaskService
from app.rag.core.preprocessor.models import ChunkWithTokens, FileIndexMeta, FilePostIndexPlan
from app.rag.core.preprocessor.ragflow_tokenizer import RagFlowTokenizer
from app.rag.core.splitter.factory import (
    build_chunk_embedding_pipeline,
    create_chunking_engine,
    validate_dense_dimension,
)
from app.rag.core.storage.manticore_bm25 import (
    ManticoreBm25IndexingPipeline,
    close_manticore_bm25_store,
)
from app.rag.core.storage.qdrant.point_factory import (
    indexed_point_from_draft,
    sparse_indexed_point_from_draft,
)
from app.rag.core.storage.qdrant.qdrant_store import QdrantIndexStore
from app.rag.core.storage.vector.draft_factory import ChunkDraftFactory
from app.rag.models.chunk_record import ChunkRecordDB
from app.rag.observability.logging import logger
from app.rag.services.storage.factory import StorageFactory


class DocumentIngestionError(RuntimeError):
    """文档入库编排的完整性契约被破坏。"""


class DocumentIngestionLeaseLost(DocumentIngestionError):
    """当前 worker 的租约已失效，不得再提交或清理共享产物。"""


LeaseGuard = Callable[[], bool | Awaitable[bool]]


@dataclass(frozen=True, slots=True)
class DocumentIngestionResult:
    """API 层可直接返回的文档入库结果。"""

    document_id: int
    dataset_id: int
    chunk_count: int
    page_count: int | None
    parse_time_ms: int
    parsed_bucket: str
    parsed_object_key: str


@dataclass(frozen=True, slots=True)
class _DocumentIdentity:
    document_id: int
    dataset_id: int
    user_id: int
    filename: str
    file_type: str


_default_qdrant_store = QdrantIndexStore()


async def close_ingestion_resources() -> None:
    """关闭本服务默认持有的 Qdrant 客户端与 Manticore 连接池。"""

    await _default_qdrant_store.close()
    await close_manticore_bm25_store()


class SimpleDocumentIngestionService:
    """OpenDataLoader/现有 Parser -> 切块 -> 三路索引的轻量编排。"""

    def __init__(
        self,
        *,
        storage: Any | None = None,
        qdrant_store: Any | None = None,
        bm25_pipeline: Any | None = None,
        execution_context_loader_factory: Callable[[AsyncSession], Any] | None = None,
        parse_service: Any = ParseTaskService,
        dense_pipeline_builder: Callable[[Any], Any] = build_chunk_embedding_pipeline,
        chunking_engine_factory: Callable[..., Any] = create_chunking_engine,
        sparse_service_builder: Callable[[Any], Any] = build_sparse_vector_service,
        draft_factory: ChunkDraftFactory | None = None,
        tokenizer_factory: Callable[[], Any] = RagFlowTokenizer,
    ) -> None:
        self._storage = storage or StorageFactory.get_storage()
        self._qdrant_store = qdrant_store or _default_qdrant_store
        self._bm25_pipeline = bm25_pipeline or ManticoreBm25IndexingPipeline(
            update_chunk_status=False
        )
        self._execution_context_loader_factory = (
            execution_context_loader_factory or DatasetExecutionContextLoader
        )
        self._parse_service = parse_service
        self._dense_pipeline_builder = dense_pipeline_builder
        self._chunking_engine_factory = chunking_engine_factory
        self._sparse_service_builder = sparse_service_builder
        self._draft_factory = draft_factory or ChunkDraftFactory()
        self._tokenizer_factory = tokenizer_factory

    async def ingest(
        self,
        document: Document,
        source_path: Path,
        db: AsyncSession,
        *,
        replace_existing: bool = False,
        lease_token: str | None = None,
        lease_guard: LeaseGuard | None = None,
        manage_failure: bool = True,
    ) -> DocumentIngestionResult:
        """解析已上传的文档，用同一批 chunk ID 完成三路索引并落库。

        worker 必须先将 ``PROCESSING`` Document 与原文对象持久化。本方法在
        全部索引成功后替换 ``document_chunk`` 真值集并将 Document 改为
        ``READY``。队列模式通过 ``lease_token`` + ``lease_guard`` 做阶段性 fencing，
        并让 worker 统一决定退避重试或最终 ``FAILED``；同步/测试调用仍可用默认的
        ``manage_failure=True`` 维持原行为。
        """

        identity = self._document_identity(document)
        if document.status != "PROCESSING":
            raise ValueError("文档入库服务只接受已取得租约的 PROCESSING 文档")
        current_chunk_ids: list[str] = []
        qdrant_write_started = False
        bm25_write_started = False
        parsed_bucket: str | None = None
        parsed_prefix: str | None = None
        previous_parsed_location = (
            (str(document.parsed_bucket), str(document.parsed_object_key))
            if document.parsed_bucket and document.parsed_object_key
            else None
        )
        execution_context: Any | None = None
        try:
            await self._assert_lease(lease_guard)
            self._validate_source_path(source_path)

            previous_chunk_ids = await self._previous_chunk_ids(db, identity)
            if previous_chunk_ids and not replace_existing:
                raise DocumentIngestionError(
                    "不支持覆盖已有 chunk；只有重新解析任务可以显式覆盖"
                )

            await self._assert_lease(lease_guard)
            execution_context = await self._execution_context_loader_factory(db).load(
                identity.user_id,
                identity.dataset_id,
                DatasetExecutionPurpose.PARSE,
            )
            parsed_bucket, parsed_object_key, image_prefix = self._parsed_locations(
                document,
                identity,
                attempt_token=lease_token or document.lease_token,
            )
            parsed_prefix = str(PurePosixPath(parsed_object_key).parent)
            parse_output = await self._parse(
                identity=identity,
                source_path=source_path,
                parsed_bucket=parsed_bucket,
                image_prefix=image_prefix,
                execution_context=execution_context,
            )
            await self._assert_lease(lease_guard)
            markdown = self._required_markdown(parse_output)
            await self._upload_markdown(parsed_bucket, parsed_object_key, markdown)

            dense_pipeline = self._dense_pipeline_builder(execution_context.dense_embedding)
            chunking_engine = self._chunking_engine_factory(
                config=execution_context.config.chunking,
                embedder=dense_pipeline.embedder,
            )
            chunks = await chunking_engine.aprocess_parse_result(parse_output["parse_result"])
            if not chunks:
                raise DocumentIngestionError("解析结果未产生可索引的 chunk")

            drafts = self._draft_factory.build_drafts(
                user_id=identity.user_id,
                set_id=identity.dataset_id,
                doc_id=identity.document_id,
                chunks=chunks,
            )
            self._require_chunk_indexes(drafts)
            current_chunk_ids = [draft.chunk_id for draft in drafts]

            await self._assert_lease(lease_guard)
            embedded_chunks = await dense_pipeline.aembed_chunks(chunks)
            if len(embedded_chunks) != len(drafts):
                raise DocumentIngestionError(
                    "Dense embedding 输出数量与 chunk 数量不一致: "
                    f"{len(embedded_chunks)} != {len(drafts)}"
                )
            validate_dense_dimension(
                embedded_chunks,
                user_id=identity.user_id,
                model_name=execution_context.dense_embedding.model_name,
            )

            sparse_service = self._sparse_service_builder(execution_context.sparse_embedding)
            sparse_vectors = await sparse_service.vectorize_texts(
                [draft.content for draft in drafts]
            )
            if len(sparse_vectors) != len(drafts):
                raise DocumentIngestionError(
                    "Sparse embedding 输出数量与 chunk 数量不一致: "
                    f"{len(sparse_vectors)} != {len(drafts)}"
                )

            bm25_plan = self._build_bm25_plan(identity, drafts)

            await self._assert_lease(lease_guard)
            qdrant_write_started = True
            await self._write_dense_index(drafts, embedded_chunks)
            await self._assert_lease(lease_guard)
            await self._write_sparse_index(drafts, sparse_vectors, sparse_service.vector_name)
            await self._assert_lease(lease_guard)
            bm25_write_started = True
            await self._write_bm25_index(bm25_plan)
            await self._assert_lease(lease_guard)

            if replace_existing and previous_chunk_ids:
                stale_chunk_ids = sorted(set(previous_chunk_ids) - set(current_chunk_ids))
                if stale_chunk_ids:
                    await self._qdrant_store.delete_points(chunk_ids=stale_chunk_ids)

            await self._replace_chunk_records(
                db,
                identity,
                drafts,
                chunks,
                document_version=int(document.version or 1),
            )
            page_count = self._page_count(parse_output.get("metadata"))
            parse_time_ms = int(parse_output.get("time_cost_ms") or 0)
            await self.mark_ready(
                document,
                db,
                parsed_bucket=parsed_bucket,
                parsed_object_key=parsed_object_key,
                page_count=page_count,
                chunk_count=len(drafts),
                parse_time_ms=parse_time_ms,
                lease_token=lease_token,
            )
            await self._cleanup_superseded_parsed_output(
                identity,
                previous_parsed_location=previous_parsed_location,
                current_parsed_location=(parsed_bucket, parsed_object_key),
            )
            return DocumentIngestionResult(
                document_id=identity.document_id,
                dataset_id=identity.dataset_id,
                chunk_count=len(drafts),
                page_count=page_count,
                parse_time_ms=parse_time_ms,
                parsed_bucket=parsed_bucket,
                parsed_object_key=parsed_object_key,
            )
        except BaseException as exc:
            lease_lost = isinstance(exc, DocumentIngestionLeaseLost)
            if not lease_lost and lease_guard is not None:
                try:
                    lease_lost = not await self._lease_is_owned(lease_guard)
                except BaseException:
                    # 无法确认租约时按失租处理；清理共享索引比留下可重试的幂等写更危险。
                    lease_lost = True
            secondary_errors: list[BaseException] = []
            try:
                await db.rollback()
            except BaseException as rollback_exc:
                secondary_errors.append(rollback_exc)

            if lease_lost:
                # 每个 lease 使用独立的版本化产物目录，失租 worker 可以只清理
                # 自己的尝试，不会误删新 worker 或上一个 READY 版本。索引仍按
                # chunk ID 共享资源处理，失租时不做危险的全量删除。
                if parsed_bucket and parsed_prefix not in {None, "", "."}:
                    try:
                        await asyncio.to_thread(
                            self._storage.remove_prefix,
                            parsed_bucket,
                            parsed_prefix,
                        )
                    except BaseException as cleanup_exc:
                        secondary_errors.append(cleanup_exc)
                if secondary_errors:
                    raise BaseExceptionGroup(
                        "文档解析租约失效，且回滚或当次产物清理失败",
                        [exc, *secondary_errors],
                    ) from exc
                if isinstance(exc, DocumentIngestionLeaseLost):
                    raise
                raise DocumentIngestionLeaseLost("文档解析租约已失效") from exc

            if qdrant_write_started and current_chunk_ids:
                try:
                    await self._qdrant_store.delete_points(chunk_ids=current_chunk_ids)
                except BaseException as cleanup_exc:
                    secondary_errors.append(cleanup_exc)

            if bm25_write_started:
                try:
                    await self._bm25_pipeline.delete_document_index(
                        user_id=identity.user_id,
                        dataset_id=identity.dataset_id,
                        doc_id=identity.document_id,
                    )
                except BaseException as cleanup_exc:
                    secondary_errors.append(cleanup_exc)

            if parsed_bucket and parsed_prefix not in {None, "", "."}:
                try:
                    await asyncio.to_thread(
                        self._storage.remove_prefix,
                        parsed_bucket,
                        parsed_prefix,
                    )
                except BaseException as cleanup_exc:
                    secondary_errors.append(cleanup_exc)

            if manage_failure:
                try:
                    await self.mark_failed(document, db, exc)
                except BaseException as status_exc:
                    secondary_errors.append(status_exc)
            if secondary_errors:
                raise BaseExceptionGroup(
                    "文档入库失败，且收敛步骤失败",
                    [exc, *secondary_errors],
                ) from exc
            raise
        finally:
            await aclose_dataset_execution_contexts([execution_context])

    async def mark_ready(
        self,
        document: Document,
        db: AsyncSession,
        *,
        parsed_bucket: str,
        parsed_object_key: str,
        page_count: int | None,
        chunk_count: int,
        parse_time_ms: int,
        lease_token: str | None = None,
    ) -> None:
        """将文档的解析产物和终态与 chunk 真值集一次提交。"""

        parser_backend = (
            "opendataloader" if document.file_type.lower() == "pdf" else document.parser_backend
        )
        values = {
            "parsed_bucket": parsed_bucket,
            "parsed_object_key": parsed_object_key,
            "parser_backend": parser_backend,
            "status": "READY",
            "error_code": None,
            "error_message": None,
            "page_count": page_count,
            "chunk_count": chunk_count,
            "parse_time_ms": parse_time_ms,
            "available_at": None,
            "lease_token": None,
            "lease_owner": None,
            "lease_expires_at": None,
            "finished_at": datetime.now(UTC).replace(tzinfo=None),
            "reparse_requested": False,
        }
        if lease_token is not None:
            now = datetime.now(UTC).replace(tzinfo=None)
            result = await db.execute(
                update(Document)
                .where(
                    Document.id == document.id,
                    Document.status == "PROCESSING",
                    Document.lease_token == lease_token,
                    Document.lease_expires_at > now,
                )
                .values(**values, updated_at=now)
                .execution_options(synchronize_session=False)
            )
            if int(result.rowcount or 0) != 1:
                await db.rollback()
                raise DocumentIngestionLeaseLost("READY 终态写入时租约已失效")
        else:
            for field_name, value in values.items():
                setattr(document, field_name, value)
            db.add(document)
        await db.commit()
        # expire_on_commit=False 时让直接调用者看到与数据库一致的终态。
        for field_name, value in values.items():
            setattr(document, field_name, value)

    async def mark_failed(
        self,
        document: Document,
        db: AsyncSession,
        error: BaseException,
    ) -> None:
        """记录简化文档终态；调用方仍会收到原异常。"""

        message = f"{type(error).__name__}: {error}"[:1000]
        document.status = "FAILED"
        document.error_code = type(error).__name__.upper()[:64]
        document.error_message = message
        # 重新解析失败时保留上一个 READY 版本的指针与统计；新产物只会在
        # ``mark_ready`` 的同一次 DB commit 中取代这些字段。首次解析时它们
        # 本来就是 None/0，因此也不会伪造可用产物。
        document.available_at = None
        document.lease_token = None
        document.lease_owner = None
        document.lease_expires_at = None
        document.finished_at = datetime.now(UTC).replace(tzinfo=None)
        db.add(document)
        await db.commit()

    async def purge_document(
        self,
        document: Document,
        db: AsyncSession,
        *,
        include_raw: bool = True,
    ) -> None:
        """幂等清理文档的三路索引、解析产物、原文件和数据库记录。"""

        identity = self._document_identity(document)
        chunk_ids = await self._previous_chunk_ids(db, identity)
        if chunk_ids:
            await self._qdrant_store.delete_points(chunk_ids=chunk_ids)
        await self._bm25_pipeline.delete_document_index(
            user_id=identity.user_id,
            dataset_id=identity.dataset_id,
            doc_id=identity.document_id,
        )

        # 删除文档时清理文档级根目录，一次覆盖当前指针、历史版本和
        # 曾经失败但未能即时清理的尝试目录。
        parsed_bucket = document.parsed_bucket or settings.MINIO_PRIVATE_BUCKET
        await asyncio.to_thread(
            self._storage.remove_prefix,
            parsed_bucket,
            self._parsed_document_root(identity),
        )
        if include_raw and document.raw_bucket and document.raw_object_key:
            await asyncio.to_thread(
                self._storage.remove_prefix,
                document.raw_bucket,
                document.raw_object_key,
            )

        await db.execute(
            delete(ChunkRecordDB).where(
                ChunkRecordDB.doc_id == identity.document_id,
                ChunkRecordDB.set_id == identity.dataset_id,
                ChunkRecordDB.user_id == identity.user_id,
            )
        )
        await db.delete(document)
        await db.commit()

    @classmethod
    async def _assert_lease(cls, lease_guard: LeaseGuard | None) -> None:
        if lease_guard is not None and not await cls._lease_is_owned(lease_guard):
            raise DocumentIngestionLeaseLost("文档解析租约已失效")

    @staticmethod
    async def _lease_is_owned(lease_guard: LeaseGuard) -> bool:
        result = lease_guard()
        if inspect.isawaitable(result):
            result = await result
        return bool(result)

    async def _parse(
        self,
        *,
        identity: _DocumentIdentity,
        source_path: Path,
        parsed_bucket: str,
        image_prefix: str,
        execution_context: Any,
    ) -> dict[str, Any]:
        parser_kwargs: dict[str, Any] = {}
        if identity.file_type == "pdf":
            parser_kwargs = {
                "backend": "opendataloader",
                "storage": self._storage,
                "image_bucket": parsed_bucket,
                "image_prefix": image_prefix,
                # 入库返回 READY 时图片必须已经真实持久化。
                "image_upload_async": False,
            }
        output = await self._parse_service.aprocess(
            source_path,
            identity.file_type,
            source_file=identity.filename,
            user_id=identity.user_id,
            dataset_id=identity.dataset_id,
            task_id=f"document-{identity.document_id}",
            enhancement_config=execution_context.config.enhancement,
            execution_context=execution_context,
            **parser_kwargs,
        )
        metadata = output.get("metadata") or {}
        if identity.file_type == "pdf" and metadata.get("pdf_parser_backend") != "opendataloader":
            raise DocumentIngestionError("PDF 未由 OpenDataLoader 完成解析")
        return output

    async def _upload_markdown(self, bucket: str, object_key: str, markdown: str) -> None:
        await asyncio.to_thread(
            self._storage.upload_bytes,
            bucket=bucket,
            object_key=object_key,
            content=markdown.encode("utf-8"),
            content_type="text/markdown; charset=utf-8",
        )

    async def _write_dense_index(
        self,
        drafts: Sequence[Any],
        embedded_chunks: Sequence[Any],
    ) -> None:
        points = [
            indexed_point_from_draft(draft, embedded)
            for draft, embedded in zip(drafts, embedded_chunks, strict=True)
        ]
        if not points:
            return
        await self._qdrant_store.ensure_collection(vector_size=len(points[0].vector))
        await self._qdrant_store.ensure_points(points=points)
        await self._qdrant_store.upsert_points(points=points)

    async def _write_sparse_index(
        self,
        drafts: Sequence[Any],
        sparse_vectors: Sequence[Any],
        vector_name: str,
    ) -> None:
        points = [
            sparse_indexed_point_from_draft(
                draft,
                sparse_vector,
                vector_name=vector_name,
            )
            for draft, sparse_vector in zip(drafts, sparse_vectors, strict=True)
        ]
        if not points:
            return
        await self._qdrant_store.ensure_sparse_vector_schema(vector_name=vector_name)
        await self._qdrant_store.upsert_sparse_vectors(points=points)

    async def _write_bm25_index(self, plan: FilePostIndexPlan) -> None:
        meta = plan.file_meta
        await self._bm25_pipeline.delete_document_index(
            user_id=meta.user_id,
            dataset_id=meta.dataset_id,
            doc_id=meta.doc_id,
        )
        result = await self._bm25_pipeline.write_es_index(plan, db=None)
        if not result.is_success:
            reason = result.failure_reason or (
                f"Manticore BM25 入库未全部成功: {result.indexed_items}/{result.total_items}"
            )
            raise DocumentIngestionError(reason)

    def _build_bm25_plan(
        self,
        identity: _DocumentIdentity,
        drafts: Sequence[Any],
    ) -> FilePostIndexPlan:
        tokenizer = self._tokenizer_factory()
        tokenized_chunks: list[ChunkWithTokens] = []
        for draft in drafts:
            tokenized = tokenizer.tokenize(draft.content)
            tokenized_chunks.append(
                ChunkWithTokens(
                    chunk_id=draft.chunk_id,
                    chunk_index=int(draft.chunk_index),
                    coarse_tokens=tokenized.coarse_tokens,
                    fine_tokens=tokenized.fine_tokens,
                    chunk_type=draft.chunk_type,
                )
            )
        return FilePostIndexPlan(
            file_meta=FileIndexMeta(
                user_id=identity.user_id,
                dataset_id=identity.dataset_id,
                doc_id=identity.document_id,
            ),
            chunks_with_tokens=tokenized_chunks,
        )

    async def _previous_chunk_ids(
        self,
        db: AsyncSession,
        identity: _DocumentIdentity,
    ) -> list[str]:
        result = await db.execute(
            select(ChunkRecordDB.chunk_id).where(
                ChunkRecordDB.doc_id == identity.document_id,
                ChunkRecordDB.set_id == identity.dataset_id,
                ChunkRecordDB.user_id == identity.user_id,
            )
        )
        return [str(chunk_id) for chunk_id in result.scalars().all()]

    async def _replace_chunk_records(
        self,
        db: AsyncSession,
        identity: _DocumentIdentity,
        drafts: Sequence[Any],
        chunks: Sequence[Any],
        *,
        document_version: int,
    ) -> None:
        await db.execute(
            delete(ChunkRecordDB).where(
                ChunkRecordDB.doc_id == identity.document_id,
                ChunkRecordDB.set_id == identity.dataset_id,
                ChunkRecordDB.user_id == identity.user_id,
            )
        )
        if len(drafts) != len(chunks):
            raise DocumentIngestionError("chunk 草稿与原始 chunk 数量不一致")
        records: list[ChunkRecordDB] = []
        for draft, chunk in zip(drafts, chunks, strict=True):
            start_page, end_page = self._chunk_page_range(getattr(chunk, "metadata", None))
            records.append(
                ChunkRecordDB(
                    chunk_id=draft.chunk_id,
                    doc_id=draft.doc_id,
                    document_version=document_version,
                    set_id=draft.set_id,
                    user_id=draft.user_id,
                    content=draft.content,
                    content_hash=draft.content_hash,
                    chunk_type=draft.chunk_type,
                    start_line=draft.start_line,
                    end_line=draft.end_line,
                    start_page=start_page,
                    end_page=end_page,
                    structure_metadata=self._chunk_structure_metadata(
                        getattr(chunk, "metadata", None)
                    ),
                    chunk_index=int(draft.chunk_index),
                )
            )
        db.add_all(records)
        await db.flush()

    @staticmethod
    def _chunk_page_range(metadata: Any) -> tuple[int | None, int | None]:
        """只持久化解析/切块元数据明确给出的页码，不根据行号猜测。"""

        if not isinstance(metadata, dict):
            return None, None

        def as_page(value: Any) -> int | None:
            if isinstance(value, bool) or value is None:
                return None
            try:
                return int(value)
            except (TypeError, ValueError):
                return None

        start_page = as_page(metadata.get("start_page"))
        end_page = as_page(metadata.get("end_page"))

        page_range = metadata.get("page_range")
        if isinstance(page_range, dict):
            start_page = start_page if start_page is not None else as_page(
                page_range.get("start_page", page_range.get("start"))
            )
            end_page = end_page if end_page is not None else as_page(
                page_range.get("end_page", page_range.get("end"))
            )
        elif isinstance(page_range, (list, tuple)) and page_range:
            start_page = start_page if start_page is not None else as_page(page_range[0])
            end_page = end_page if end_page is not None else as_page(page_range[-1])

        page_numbers = metadata.get("page_numbers")
        if isinstance(page_numbers, (list, tuple, set)):
            explicit_pages = [
                page for value in page_numbers if (page := as_page(value)) is not None
            ]
            if explicit_pages:
                start_page = start_page if start_page is not None else min(explicit_pages)
                end_page = end_page if end_page is not None else max(explicit_pages)

        single_page = as_page(metadata.get("page_number", metadata.get("page")))
        if single_page is not None:
            start_page = start_page if start_page is not None else single_page
            end_page = end_page if end_page is not None else single_page
        return start_page, end_page

    @staticmethod
    def _chunk_structure_metadata(metadata: Any) -> dict[str, Any] | None:
        """只持久化详情页需要的 LinkRag 结构化分片白名单字段。"""

        if not isinstance(metadata, dict):
            return None

        def clean_text(value: Any, *, max_length: int = 256) -> str | None:
            if not isinstance(value, str):
                return None
            normalized = value.strip()
            return normalized[:max_length] if normalized else None

        def clean_text_list(value: Any, *, max_items: int = 24) -> list[str] | None:
            if not isinstance(value, (list, tuple)):
                return None
            cleaned = [
                text
                for item in value[:max_items]
                if (text := clean_text(item)) is not None
            ]
            return cleaned or None

        result: dict[str, Any] = {}
        for key in ("split_strategy", "chunk_role", "oversized_reason", "truncated_reason"):
            if (value := clean_text(metadata.get(key), max_length=128)) is not None:
                result[key] = value
        for key in ("heading_trail", "element_types", "protected_element_types"):
            if (value := clean_text_list(metadata.get(key))) is not None:
                result[key] = value

        heading_trails = metadata.get("heading_trails")
        if isinstance(heading_trails, (list, tuple)):
            cleaned_trails = [
                trail
                for raw_trail in heading_trails[:24]
                if (trail := clean_text_list(raw_trail)) is not None
            ]
            if cleaned_trails:
                result["heading_trails"] = cleaned_trails

        source_chunk_index = metadata.get("source_chunk_index")
        if isinstance(source_chunk_index, int) and not isinstance(source_chunk_index, bool):
            result["source_chunk_index"] = source_chunk_index
        for key in ("oversized", "truncated", "line_span_approx"):
            if isinstance(metadata.get(key), bool):
                result[key] = metadata[key]
        return result or None

    @staticmethod
    def _document_identity(document: Document) -> _DocumentIdentity:
        if document.id is None:
            raise ValueError("Document 必须先持久化并获得 id")
        filename = PurePath(document.filename or "").name
        file_type = (document.file_type or "").strip().lower().lstrip(".")
        if not filename:
            raise ValueError("Document.filename 不能为空")
        if not file_type:
            raise ValueError("Document.file_type 不能为空")
        return _DocumentIdentity(
            document_id=int(document.id),
            dataset_id=int(document.dataset_id),
            user_id=int(document.user_id),
            filename=filename,
            file_type=file_type,
        )

    @staticmethod
    def _validate_source_path(source_path: Path) -> None:
        if not source_path.exists() or not source_path.is_file():
            raise FileNotFoundError(f"本地源文件不存在: {source_path}")
        if source_path.stat().st_size <= 0:
            raise ValueError("本地源文件不能为空")

    @staticmethod
    def _parsed_document_root(identity: _DocumentIdentity) -> str:
        """返回该文档唯一的解析产物根目录。"""

        return f"parsed/{identity.user_id}/{identity.dataset_id}/{identity.document_id}"

    @staticmethod
    def _safe_attempt_segment(attempt_token: str | None, attempt_count: int | None) -> str:
        """把 lease token 收敛成可作为对象键目录的单段字符串。"""

        fallback = f"attempt-{max(1, int(attempt_count or 1))}"
        normalized = re.sub(r"[^A-Za-z0-9_-]+", "-", (attempt_token or fallback).strip())
        normalized = normalized.strip("-_")[:64]
        return normalized or fallback

    @classmethod
    def _parsed_locations(
        cls,
        document: Document,
        identity: _DocumentIdentity,
        *,
        attempt_token: str | None = None,
    ) -> tuple[str, str, str]:
        """为当前文档版本和 worker 尝试创建独立产物目录。

        ``Document.parsed_object_key`` 只是最后一个 READY 版本的指针，不能作为
        重新解析的写入目标。否则新任务会在终态提交前覆盖旧正文，
        导致失败无法回滚，也可能让失租 worker 覆盖新 worker 的输出。
        """

        bucket = document.parsed_bucket or settings.MINIO_PRIVATE_BUCKET
        document_root = cls._parsed_document_root(identity)
        version = max(1, int(document.version or 1))
        attempt_segment = cls._safe_attempt_segment(attempt_token, document.attempt_count)
        version_prefix = f"{document_root}/versions/v{version}/{attempt_segment}"
        stem = PurePath(identity.filename).stem or f"document-{identity.document_id}"
        object_key = f"{version_prefix}/{stem}.md"
        image_prefix = f"{version_prefix}/images"
        return bucket, object_key, image_prefix

    async def _cleanup_superseded_parsed_output(
        self,
        identity: _DocumentIdentity,
        *,
        previous_parsed_location: tuple[str, str] | None,
        current_parsed_location: tuple[str, str],
    ) -> None:
        """在 READY 指针提交后尽力清理上一版解析产物。

        新版本目录与旧版本目录相互隔离，因此常规情况可直接删除旧前缀。
        对早期直接把 Markdown 写在文档根目录的数据，不能删整个旧父目录
        （它同时是新版本的祖先目录）；这时只删旧 Markdown 和旧图片子目录。

        清理发生在终态 DB commit 之后，任何对象存储故障都不得把已成功的
        READY 任务逆转为 FAILED；文档最终删除仍会按文档根目录全量收敛。
        """

        if previous_parsed_location is None:
            return
        old_bucket, old_key = previous_parsed_location
        new_bucket, new_key = current_parsed_location
        if (old_bucket, old_key) == (new_bucket, new_key):
            return

        document_root = PurePosixPath(self._parsed_document_root(identity))
        old_path = PurePosixPath(old_key)
        try:
            old_path.relative_to(document_root)
        except ValueError:
            logger.bind(
                event="document_superseded_parse_cleanup_skipped",
                document_id=identity.document_id,
                reason="outside_document_root",
            ).warning("跳过非文档根目录内的旧解析产物清理")
            return

        old_parent = old_path.parent
        new_parent = PurePosixPath(new_key).parent
        try:
            new_parent.relative_to(old_parent)
            old_parent_contains_current = True
        except ValueError:
            old_parent_contains_current = False

        try:
            if not old_parent_contains_current:
                await asyncio.to_thread(
                    self._storage.remove_prefix,
                    old_bucket,
                    old_parent.as_posix(),
                )
                return

            # 兼容旧版本：旧 Markdown 直接位于 document root，PDF 图片通常在
            # ``image/images``；某些早期输出也可能保留在 ``images``。
            await asyncio.to_thread(self._storage.remove_object, old_bucket, old_key)
            for asset_dir in ("image", "images"):
                await asyncio.to_thread(
                    self._storage.remove_prefix,
                    old_bucket,
                    (old_parent / asset_dir).as_posix(),
                )
        except Exception as exc:
            logger.bind(
                event="document_superseded_parse_cleanup_failed",
                document_id=identity.document_id,
                error_type=type(exc).__name__,
            ).warning("旧版解析产物清理失败，将在删除文档时按根目录收敛")

    @staticmethod
    def _required_markdown(parse_output: dict[str, Any]) -> str:
        markdown = parse_output.get("markdown")
        if not isinstance(markdown, str) or not markdown.strip():
            raise DocumentIngestionError("解析器未返回有效 Markdown")
        if parse_output.get("parse_result") is None:
            raise DocumentIngestionError("解析器未返回 ParseResult")
        return markdown

    @staticmethod
    def _require_chunk_indexes(drafts: Sequence[Any]) -> None:
        missing = [draft.chunk_id for draft in drafts if draft.chunk_index is None]
        if missing:
            raise DocumentIngestionError(f"chunk 缺少 chunk_index: {', '.join(missing[:5])}")

    @staticmethod
    def _page_count(metadata: Any) -> int | None:
        if not isinstance(metadata, dict):
            return None
        value = metadata.get("pages_or_length")
        if value is None:
            return None
        try:
            page_count = int(value)
        except (TypeError, ValueError):
            return None
        return page_count if page_count >= 0 else None
