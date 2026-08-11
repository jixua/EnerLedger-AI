"""不依赖解析状态表的文档解析与三路索引编排。

本模块只复用已迁入当前项目的 LinkRag 底层实现，不调用
``ParseTaskPipeline``、``SparseIndexingPipeline`` 或 ``Preprocessor``。
"""

from __future__ import annotations

import asyncio
import inspect
import math
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
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
from app.rag.core.markdown_parser.models import (
    META_VISUAL_DESCRIPTION,
    ElementType,
    MarkdownElement,
)
from app.rag.core.markdown_parser.parser import MarkdownParser
from app.rag.core.parse_task_service import ParseTaskService
from app.rag.core.parser.pdf.content_validation import (
    PdfContentValidationPolicy,
    PdfContentValidator,
)
from app.rag.core.parser.pdf.image_asset_policy import (
    ImageAssetCategory,
    PdfImageAssetPolicy,
    StructuredVisualDescription,
)
from app.rag.core.parser.pdf.page_fallback import (
    AnalyzeImagePageProviderAdapter,
    PageFallbackMethod,
    PdfPageFallbackProcessor,
    QualityGatedOcrPageProvider,
    RapidOcrPageProviderAdapter,
)
from app.rag.core.parser.pdf.quality import (
    PdfQualityAnalyzer,
    PdfQualityReport,
    PdfQualityStatus,
)
from app.rag.core.parser.pdf.source_structure import PdfSourceStructureInspector
from app.rag.core.parser.pdf.table_structure import PdfTableStructureExtractor
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


class DocumentQualityGateError(DocumentIngestionError):
    """文档解析质量、兜底或专项验证未达到可检索条件。"""

    def __init__(
        self,
        *,
        quality_status: str,
        quality_report: dict[str, Any],
        error_code: str,
        message: str,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.quality_status = quality_status
        self.quality_report = quality_report
        self.error_code = error_code
        self.retryable = retryable


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
        pdf_quality_analyzer: Any | None = None,
        pdf_fallback_processor_factory: Callable[[Any | None], Any] | None = None,
        pdf_source_structure_inspector: Any | None = None,
        pdf_content_validator: Any | None = None,
        pdf_table_structure_extractor: Any | None = None,
        pdf_image_asset_policy: Any | None = None,
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
        self._pdf_quality_analyzer = pdf_quality_analyzer or PdfQualityAnalyzer(
            min_effective_text_chars=settings.PDF_QUALITY_MIN_EFFECTIVE_TEXT_CHARS,
            image_only_max_text_chars=settings.PDF_QUALITY_IMAGE_ONLY_MAX_TEXT_CHARS,
            image_only_min_coverage_ratio=(
                settings.PDF_QUALITY_IMAGE_ONLY_MIN_COVERAGE_RATIO
            ),
            min_ocr_confidence=settings.PDF_QUALITY_MIN_OCR_CONFIDENCE,
            min_text_retention_ratio=settings.PDF_QUALITY_MIN_TEXT_RETENTION_RATIO,
        )
        self._rapidocr_provider = RapidOcrPageProviderAdapter()
        self._pdf_fallback_processor_factory = (
            pdf_fallback_processor_factory or self._build_pdf_fallback_processor
        )
        self._pdf_source_structure_inspector = (
            pdf_source_structure_inspector or PdfSourceStructureInspector()
        )
        self._pdf_content_validator = pdf_content_validator or PdfContentValidator(
            policy=PdfContentValidationPolicy(
                high_image_coverage_ratio=settings.PDF_CONTENT_HIGH_IMAGE_COVERAGE_RATIO
            )
        )
        self._pdf_table_structure_extractor = (
            pdf_table_structure_extractor or PdfTableStructureExtractor()
        )
        self._pdf_image_asset_policy = pdf_image_asset_policy or PdfImageAssetPolicy()

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
            if identity.file_type == "pdf":
                parse_quality_status, parse_quality = await self._process_pdf_quality(
                    identity=identity,
                    source_path=source_path,
                    parse_output=parse_output,
                    markdown=markdown,
                    execution_context=execution_context,
                )
                markdown = self._required_markdown(parse_output)
            elif identity.file_type in {"doc", "docx"}:
                parse_quality_status, parse_quality = self._process_word_quality(
                    identity=identity,
                    parse_output=parse_output,
                    markdown=markdown,
                )
            else:
                parse_quality_status = "NOT_APPLICABLE"
                parse_quality = {
                    "schema_version": 2,
                    "status": "NOT_APPLICABLE",
                    "warnings": [],
                }
            await self._assert_lease(lease_guard)
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
                parse_quality_status=parse_quality_status,
                parse_quality=parse_quality,
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
        parse_quality_status: str,
        parse_quality: dict[str, Any],
        lease_token: str | None = None,
    ) -> None:
        """将文档的解析产物和终态与 chunk 真值集一次提交。"""

        if document.file_type.lower() in {"pdf", "doc", "docx"}:
            report_status = str(parse_quality.get("status") or "")
            if (
                parse_quality_status != PdfQualityStatus.PASSED.value
                or report_status != PdfQualityStatus.PASSED.value
            ):
                raise DocumentIngestionError(
                    "文档质量门禁未通过，禁止写入 READY 终态"
                )
        parser_backend = (
            "opendataloader"
            if document.file_type.lower() == "pdf"
            else str(parse_quality.get("parser_backend") or document.parser_backend or "") or None
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
            "parse_quality_status": parse_quality_status,
            "parse_quality": parse_quality,
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
        document.error_code = str(
            getattr(error, "error_code", type(error).__name__)
        ).upper()[:64]
        document.error_message = message
        quality_status = getattr(error, "quality_status", None)
        quality_report = getattr(error, "quality_report", None)
        if quality_status is not None:
            document.parse_quality_status = str(quality_status)
        if isinstance(quality_report, dict):
            document.parse_quality = quality_report
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

    def _build_pdf_fallback_processor(
        self,
        resolved_vision: Any | None,
    ) -> PdfPageFallbackProcessor:
        vision_provider = None
        if resolved_vision is not None:
            vision_provider = AnalyzeImagePageProviderAdapter(
                resolved_vision.provider,
                model_name=resolved_vision.model_name,
            )
        ocr_provider = QualityGatedOcrPageProvider(
            self._rapidocr_provider,
            vision_provider,
            min_effective_text_chars=settings.PDF_QUALITY_MIN_EFFECTIVE_TEXT_CHARS,
            min_confidence=settings.PDF_QUALITY_MIN_OCR_CONFIDENCE,
        )
        return PdfPageFallbackProcessor(
            ocr_provider=ocr_provider,
            vision_provider=vision_provider,
            dpi=settings.PDF_FALLBACK_RENDER_DPI,
            min_chart_image_coverage_ratio=(
                settings.PDF_FALLBACK_MIN_CHART_IMAGE_COVERAGE_RATIO
            ),
            max_concurrency=settings.PDF_FALLBACK_MAX_CONCURRENCY,
            max_rendered_page_pixels=settings.PDF_FALLBACK_MAX_RENDERED_PAGE_PIXELS,
            max_rendered_page_bytes=settings.PDF_FALLBACK_MAX_RENDERED_PAGE_BYTES,
            max_structured_report_bytes=(
                settings.PDF_FALLBACK_MAX_STRUCTURED_REPORT_BYTES
            ),
        )

    def _process_word_quality(
        self,
        *,
        identity: _DocumentIdentity,
        parse_output: dict[str, Any],
        markdown: str,
    ) -> tuple[str, dict[str, Any]]:
        """校验 Word 文本、表格和图片覆盖，并把结构绑定到分片输入。"""

        metadata = parse_output.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
        parse_result = parse_output.get("parse_result")
        if parse_result is None:
            raise DocumentIngestionError("Word 解析器未返回 ParseResult")

        table_report = self._pdf_table_structure_extractor.extract(
            markdown,
            merge_continuations=False,
        )
        self._apply_word_structured_tables(parse_result, table_report)
        suppressed_images = self._apply_word_image_retrieval_policy(parse_result)

        def integer(name: str) -> int:
            try:
                return max(0, int(metadata.get(name) or 0))
            except (TypeError, ValueError):
                return 0

        source_text_chars = integer("source_text_chars")
        mammoth_text_chars = integer("mammoth_text_chars")
        text_retention_ratio = (
            min(1.0, mammoth_text_chars / source_text_chars)
            if source_text_chars > 0
            else 1.0
        )
        source_table_count = integer("source_table_count")
        source_top_level_tables = integer("source_top_level_table_count")
        mammoth_table_count = integer("mammoth_html_table_count")
        parsed_table_count = sum(
            element.type is ElementType.TABLE for element in parse_result.elements
        )
        source_image_references = integer("source_image_reference_count")
        image_occurrences = integer("image_occurrence_count")
        rendered_images = integer("image_count")

        warnings = [
            str(item)
            for item in metadata.get("warnings", [])
            if str(item).strip()
        ]
        blocking: list[str] = []
        blocking.extend(
            warning for warning in warnings if warning.startswith("WORD_IMAGE_TRANSCODE_FAILED:")
        )
        if text_retention_ratio < 0.97:
            blocking.append(
                "WORD_TEXT_RETENTION_LOW:"
                f"expected>={0.97},actual={text_retention_ratio:.4f}"
            )
        if source_table_count != mammoth_table_count:
            blocking.append(
                "WORD_TABLE_COUNT_MISMATCH:"
                f"source={source_table_count},mammoth={mammoth_table_count}"
            )
        if source_top_level_tables != len(table_report.tables):
            blocking.append(
                "WORD_STRUCTURED_TABLE_COUNT_MISMATCH:"
                f"source={source_top_level_tables},structured={len(table_report.tables)}"
            )
        if source_top_level_tables != parsed_table_count:
            blocking.append(
                "WORD_PARSE_RESULT_TABLE_COUNT_MISMATCH:"
                f"source={source_top_level_tables},parsed={parsed_table_count}"
            )
        if source_image_references != image_occurrences or image_occurrences != rendered_images:
            blocking.append(
                "WORD_IMAGE_COUNT_MISMATCH:"
                f"source={source_image_references},hook={image_occurrences},rendered={rendered_images}"
            )
        if source_image_references and not bool(metadata.get("image_assets_persisted")):
            blocking.append("WORD_IMAGE_ASSETS_NOT_PERSISTED")
        critical_warnings = [
            str(item)
            for item in metadata.get("critical_warnings", [])
            if str(item).strip()
        ]
        if critical_warnings:
            blocking.extend(f"WORD_UNSUPPORTED_OBJECT:{item}" for item in critical_warnings)

        status = "PASSED" if not blocking else "CONTENT_VALIDATION_FAILED"
        report = {
            "schema_version": 2,
            "status": status,
            "parser_backend": str(metadata.get("parser_backend") or "mammoth"),
            "unit_type": "block",
            "unit_count": integer("unit_count"),
            "text_coverage_ratio": round(text_retention_ratio, 6),
            "source_text_chars": source_text_chars,
            "output_text_chars": mammoth_text_chars,
            "source_table_count": source_table_count,
            "source_top_level_table_count": source_top_level_tables,
            "source_nested_table_count": integer("source_nested_table_count"),
            "structured_table_count": len(table_report.tables),
            "source_image_reference_count": source_image_references,
            "image_occurrence_count": image_occurrences,
            "image_asset_count": integer("image_asset_count"),
            "image_upload_count": integer("image_upload_count"),
            "unsupported_vision_image_count": integer(
                "unsupported_vision_image_count"
            ),
            "suppressed_unexplained_image_count": suppressed_images,
            "mammoth_message_count": integer("mammoth_message_count"),
            "warnings": list(dict.fromkeys([*warnings, *blocking])),
            "blocking_issues": blocking,
            "table_structure": table_report.to_dict(),
            "image_assets": metadata.get("word_image_assets", []),
        }
        if blocking:
            raise DocumentQualityGateError(
                quality_status=status,
                quality_report=report,
                error_code="WORD_CONTENT_VALIDATION_FAILED",
                message="Word 文本、表格或图片完整性验证未通过",
                retryable=False,
            )
        metadata["word_quality_status"] = status
        return status, report

    @staticmethod
    def _apply_word_structured_tables(parse_result: Any, table_report: Any) -> None:
        tables = iter(table_report.tables)
        for element in parse_result.elements:
            if element.type is not ElementType.TABLE:
                continue
            table = next(tables, None)
            if table is None:
                break
            element.metadata["table_structure"] = table.to_dict()

    @staticmethod
    def _apply_word_image_retrieval_policy(parse_result: Any) -> int:
        """没有 alt/视觉说明的独立图片保留预览，但不进入召回索引。"""

        suppressed = 0
        for element in parse_result.elements:
            if element.type is not ElementType.IMAGE:
                continue
            description = str(element.metadata.get(META_VISUAL_DESCRIPTION) or "").strip()
            alt = str(element.metadata.get("alt") or "").strip()
            if description or alt:
                element.metadata["retrieval_eligible"] = True
                continue
            element.metadata["suppress_retrieval"] = True
            element.metadata["retrieval_eligible"] = False
            element.metadata["retrieval_noise_reason"] = "WORD_IMAGE_DESCRIPTION_MISSING"
            suppressed += 1
        return suppressed

    async def _process_pdf_quality(
        self,
        *,
        identity: _DocumentIdentity,
        source_path: Path,
        parse_output: dict[str, Any],
        markdown: str,
        execution_context: Any,
    ) -> tuple[str, dict[str, Any]]:
        """ODL 主解析后执行页级补齐，并只在全部门禁通过后替换 ParseResult。"""

        initial_quality: PdfQualityReport = await asyncio.to_thread(
            self._pdf_quality_analyzer.analyze,
            source_path,
            markdown,
        )
        if initial_quality.status in {
            PdfQualityStatus.PAGE_COUNT_MISMATCH,
            PdfQualityStatus.PAGE_PROVENANCE_INVALID,
        }:
            report = self._compose_pdf_quality_report(
                status=initial_quality.status.value,
                initial_quality=initial_quality,
                final_quality=initial_quality,
            )
            self._raise_pdf_quality_gate(initial_quality.status.value, report)

        fallback_processor = self._pdf_fallback_processor_factory(
            getattr(execution_context, "enhancement_vision", None)
        )
        fallback_report = await fallback_processor.process(
            source_path,
            markdown,
            initial_quality,
        )
        merged_markdown = fallback_processor.merge_markdown(markdown, fallback_report)
        final_quality: PdfQualityReport = await asyncio.to_thread(
            self._pdf_quality_analyzer.analyze,
            source_path,
            merged_markdown,
            ocr_results=fallback_report.ocr_results,
        )

        source_inspection = await asyncio.to_thread(
            self._pdf_source_structure_inspector.inspect,
            source_path,
            markdown,
            initial_quality,
        )
        table_structure_report = self._pdf_table_structure_extractor.extract(
            merged_markdown
        )
        preliminary_image_report = self._pdf_image_asset_policy.extract_and_classify(
            merged_markdown
        )
        categories_by_page: dict[int, list[ImageAssetCategory]] = {}
        for decision in preliminary_image_report.assets:
            if decision.page_number is not None:
                categories_by_page.setdefault(decision.page_number, []).append(
                    decision.category
                )
        structured_by_page: dict[int, StructuredVisualDescription] = {}
        visually_assessed_pages: set[int] = set()
        visual_no_content_pages: set[int] = set()
        for result in fallback_report.results:
            if (
                not result.structured_data
                or bool(getattr(result, "truncated", False))
                or bool(getattr(result, "error_code", None))
            ):
                continue
            has_visual_content = self._visual_assessment_state(result.structured_data)
            if has_visual_content is None:
                continue
            if has_visual_content is False:
                visually_assessed_pages.add(result.page_number)
                visual_no_content_pages.add(result.page_number)
                continue
            try:
                description = StructuredVisualDescription.from_mapping(result.structured_data)
            except (TypeError, ValueError):
                continue
            # 扫描页使用同一个视觉模型执行 OCR；OCR 提示同时要求返回图表/流程关系，
            # 因此有真实视觉字段时也可作为结构化图片说明，避免同页重复调用模型。
            if self._structured_visual_is_complete(
                description,
                expected_page=result.page_number,
                categories=categories_by_page.get(result.page_number, ()),
            ):
                # One full-page description is stored once by source page.  It is
                # deliberately not copied onto every image reference on that page.
                structured_by_page.setdefault(result.page_number, description)
                visually_assessed_pages.add(result.page_number)
        visual_descriptions = self._explicit_asset_visual_descriptions(
            fallback_report,
            preliminary_image_report,
        )
        image_policy_report = self._pdf_image_asset_policy.extract_and_classify(
            merged_markdown,
            visual_descriptions=visual_descriptions,
        )
        markdown_pages = self._split_pdf_markdown_pages(merged_markdown)
        validation_report = self._pdf_content_validator.validate(
            source_inspection.pages,
            markdown_pages,
        )
        vision_incomplete_pages = self._pdf_vision_incomplete_pages(
            fallback_report,
            structured_by_page=structured_by_page,
            visually_assessed_pages=visually_assessed_pages,
            image_policy_report=image_policy_report,
        )

        if final_quality.status is not PdfQualityStatus.PASSED:
            final_status = final_quality.status.value
        elif vision_incomplete_pages:
            final_status = "FALLBACK_INCOMPLETE"
        elif not validation_report.structural_passed:
            final_status = "CONTENT_VALIDATION_FAILED"
        else:
            final_status = PdfQualityStatus.PASSED.value

        report = self._compose_pdf_quality_report(
            status=final_status,
            initial_quality=initial_quality,
            final_quality=final_quality,
            fallback_report=fallback_report,
            source_inspection=source_inspection,
            validation_report=validation_report,
            vision_incomplete_pages=vision_incomplete_pages,
            visually_assessed_pages=visually_assessed_pages,
            visual_no_content_pages=visual_no_content_pages,
            table_structure_report=table_structure_report,
            image_policy_report=image_policy_report,
        )
        if final_status != PdfQualityStatus.PASSED.value:
            self._raise_pdf_quality_gate(
                final_status,
                report,
                retryable=bool(getattr(fallback_report, "has_retryable_failure", False)),
            )

        cleaned_markdown, visible_page_number_report = self._remove_pdf_visible_page_numbers(
            merged_markdown
        )
        report["visible_page_numbers"] = visible_page_number_report
        rebuilt_parse_result = MarkdownParser().parse(
            self._pdf_markdown_for_indexing(cleaned_markdown),
            source_file=identity.filename,
        )
        marker_count = self._apply_pdf_page_numbers_from_markdown(
            rebuilt_parse_result,
            cleaned_markdown,
        )
        self._apply_pdf_page_visual_descriptions(
            rebuilt_parse_result,
            structured_by_page,
        )
        self._apply_pdf_structured_assets(
            rebuilt_parse_result,
            table_structure_report=table_structure_report,
            image_policy_report=image_policy_report,
        )
        retrieval_noise = self._apply_pdf_retrieval_noise_policy(rebuilt_parse_result)
        report["retrieval_noise"] = retrieval_noise
        if retrieval_noise["suppressed_element_count"]:
            report["warnings"] = list(
                dict.fromkeys(
                    [
                        *report.get("warnings", []),
                        "RETRIEVAL_NOISE_SUPPRESSED:"
                        f"count={retrieval_noise['suppressed_element_count']}",
                    ]
                )
            )
        if marker_count != final_quality.pdf_page_count:
            report["status"] = PdfQualityStatus.PAGE_COUNT_MISMATCH.value
            report["warnings"] = list(
                dict.fromkeys(
                    [
                        *report.get("warnings", []),
                        "PARSE_RESULT_PAGE_MARKER_COUNT_MISMATCH:"
                        f"expected={final_quality.pdf_page_count},actual={marker_count}",
                    ]
                )
            )
            self._raise_pdf_quality_gate(PdfQualityStatus.PAGE_COUNT_MISMATCH.value, report)

        parse_output["markdown"] = cleaned_markdown
        parse_output["parse_result"] = rebuilt_parse_result
        metadata = parse_output.setdefault("metadata", {})
        metadata["pages_or_length"] = final_quality.pdf_page_count
        metadata["pdf_page_markers"] = marker_count
        metadata["pdf_quality_status"] = final_status
        metadata["pdf_ocr_page_count"] = final_quality.ocr_page_count
        return final_status, report

    @staticmethod
    def _visual_assessment_state(payload: Mapping[str, object]) -> bool | None:
        """Return a consistent explicit page-level visual judgment.

        A Boolean without a matching type is not enough: contradictory model JSON
        (for example ``false`` + ``chart``) must remain incomplete rather than close
        a blocking visual task.
        """

        has_visual_content = payload.get("has_visual_content")
        visual_content_type = str(payload.get("visual_content_type") or "").strip().casefold()
        if type(has_visual_content) is not bool:
            return None
        if has_visual_content is False:
            return False if visual_content_type == "none" else None
        if visual_content_type not in {
            "chart",
            "flowchart",
            "system_boundary",
            "other",
        }:
            return None
        return True

    @staticmethod
    def _pdf_vision_incomplete_pages(
        fallback_report: Any,
        *,
        structured_by_page: Mapping[int, StructuredVisualDescription],
        visually_assessed_pages: Sequence[int] = (),
        image_policy_report: Any,
    ) -> list[int]:
        """返回视觉调用无结构结果或仍有待办视觉资产的原 PDF 页码。"""

        completed_pages = set(structured_by_page) | set(visually_assessed_pages)
        return sorted(
            {
                result.page_number
                for result in fallback_report.results
                # A local OCR result completes the text task but never claims that a
                # visual task is complete. Explicit visual assets are checked below.
                # Vision calls and model-backed OCR fallback still require a
                # consistent page-level visual assessment.
                if (
                    result.page_number not in completed_pages
                    and (
                        result.method is PageFallbackMethod.VISION
                        or "OCR_SOURCE:VISION_FALLBACK" in result.warnings
                    )
                )
            }
            | {
                decision.page_number
                for decision in image_policy_report.assets
                if (
                    decision.visual_task is not None
                    and decision.page_number is not None
                    and decision.page_number not in completed_pages
                )
            }
        )

    @staticmethod
    def _structured_visual_is_complete(
        description: StructuredVisualDescription,
        *,
        expected_page: int,
        categories: Sequence[ImageAssetCategory] = (),
    ) -> bool:
        """Conservatively validate a page-level chart/flow description.

        A generic summary alone does not prove that values, nodes or arrows were
        extracted.  Page provenance is fixed by the rendered target and must match.
        """

        base_complete = bool(
            description.source_page == expected_page
            and description.summary.strip()
            and (
                description.relationships
                or description.key_values
                or len(description.entities) >= 2
            )
        )
        if not base_complete:
            return False
        if categories:
            return not PdfImageAssetPolicy().validate_page_visual_description(
                description,
                page_number=expected_page,
                categories=categories,
            )
        return True

    def _explicit_asset_visual_descriptions(
        self,
        fallback_report: Any,
        preliminary_image_report: Any,
    ) -> dict[str, StructuredVisualDescription]:
        """Read only explicitly mapped per-asset descriptions from provider JSON.

        A full-page ``summary/entities/...`` payload is intentionally excluded.  A
        nested ``visual_assets`` item must identify one preliminary ``asset_key`` or
        an otherwise unique source/page/line occurrence before it may be attached to
        an image.  This keeps page-level analysis from being cloned onto logos,
        signatures and unrelated images on the same page.
        """

        decisions = tuple(preliminary_image_report.assets)
        by_key = {decision.asset_key: decision for decision in decisions}
        descriptions: dict[str, StructuredVisualDescription] = {}
        for result in fallback_report.results:
            payload = result.structured_data
            if not isinstance(payload, Mapping):
                continue
            raw_assets = payload.get("visual_assets")
            if not isinstance(raw_assets, Sequence) or isinstance(
                raw_assets,
                (str, bytes),
            ):
                continue
            for raw_asset in raw_assets:
                if not isinstance(raw_asset, Mapping):
                    continue
                asset_key = str(raw_asset.get("asset_key") or "").strip()
                if asset_key not in by_key:
                    source_ref = str(raw_asset.get("source_ref") or "").strip()
                    normalized_ref = self._pdf_image_asset_policy.normalize_source_ref(
                        source_ref
                    )
                    raw_line_number = raw_asset.get("line_number")
                    raw_occurrence_index = raw_asset.get("occurrence_index")
                    try:
                        line_number = (
                            int(raw_line_number)
                            if raw_line_number is not None
                            and not isinstance(raw_line_number, bool)
                            else None
                        )
                        occurrence_index = (
                            int(raw_occurrence_index)
                            if raw_occurrence_index is not None
                            and not isinstance(raw_occurrence_index, bool)
                            else None
                        )
                    except (TypeError, ValueError):
                        continue
                    matches = [
                        decision
                        for decision in decisions
                        if decision.page_number == result.page_number
                        and self._pdf_image_asset_policy.normalize_source_ref(
                            decision.source_ref
                        )
                        == normalized_ref
                        and (
                            line_number is None
                            or decision.line_number == line_number
                        )
                        and (
                            occurrence_index is None
                            or decision.occurrence_index == occurrence_index
                        )
                    ]
                    if len(matches) != 1:
                        continue
                    asset_key = matches[0].asset_key
                decision = by_key[asset_key]
                if decision.page_number != result.page_number:
                    continue
                raw_description = raw_asset.get("description")
                description_payload = (
                    raw_description if isinstance(raw_description, Mapping) else raw_asset
                )
                normalized_payload = dict(description_payload)
                normalized_payload["source_page"] = result.page_number
                try:
                    descriptions[asset_key] = StructuredVisualDescription.from_mapping(
                        normalized_payload
                    )
                except (TypeError, ValueError):
                    continue
        return descriptions

    @staticmethod
    def _apply_pdf_page_visual_descriptions(
        parse_result: Any,
        descriptions: Mapping[int, StructuredVisualDescription],
    ) -> None:
        """Materialize each full-page visual result exactly once for retrieval."""

        if not descriptions:
            return
        elements: list[MarkdownElement] = parse_result.elements
        for page_number, description in sorted(descriptions.items()):
            page_elements = [
                (index, element)
                for index, element in enumerate(elements)
                if element.metadata.get("page_number") == page_number
            ]
            if not page_elements:
                continue
            existing = next(
                (
                    element
                    for _, element in page_elements
                    if element.metadata.get("page_visual_description") is not None
                ),
                None,
            )
            retrieval_text = "页面视觉结构：\n" + description.to_retrieval_text()
            if existing is not None:
                existing.content = retrieval_text
                existing.metadata["page_visual_description"] = description.to_dict()
                continue

            marker_index = next(
                (
                    index
                    for index, element in page_elements
                    if "PAGE_FALLBACK:VISION" in element.content
                ),
                None,
            )
            target_index: int | None = None
            if marker_index is not None:
                marker = elements[marker_index]
                marker.metadata["suppress_retrieval"] = True
                marker.metadata["retrieval_noise_reason"] = "PAGE_FALLBACK_MARKER"
                for index in range(marker_index + 1, len(elements)):
                    candidate = elements[index]
                    if candidate.metadata.get("page_number") != page_number:
                        break
                    if candidate.type not in {ElementType.IMAGE, ElementType.TABLE}:
                        target_index = index
                        break

            if target_index is not None:
                target = elements[target_index]
                target.content = retrieval_text
                target.metadata.update(
                    {
                        "page_visual_description": description.to_dict(),
                        "page_visual_description_indexed": True,
                    }
                )
                continue

            insert_after, anchor = page_elements[-1]
            elements.insert(
                insert_after + 1,
                MarkdownElement(
                    type=ElementType.PARAGRAPH,
                    content=retrieval_text,
                    start_line=anchor.end_line,
                    end_line=anchor.end_line,
                    metadata={
                        "page_number": page_number,
                        "page_visual_description": description.to_dict(),
                        "page_visual_description_indexed": True,
                        "retrieval_only": True,
                    },
                ),
            )

    @staticmethod
    def _compose_pdf_quality_report(
        *,
        status: str,
        initial_quality: PdfQualityReport,
        final_quality: PdfQualityReport,
        fallback_report: Any | None = None,
        source_inspection: Any | None = None,
        validation_report: Any | None = None,
        vision_incomplete_pages: Sequence[int] = (),
        visually_assessed_pages: Sequence[int] = (),
        visual_no_content_pages: Sequence[int] = (),
        table_structure_report: Any | None = None,
        image_policy_report: Any | None = None,
    ) -> dict[str, Any]:
        warnings: list[str] = [*initial_quality.warnings, *final_quality.warnings]
        if fallback_report is not None:
            warnings.extend(fallback_report.warnings)
            for result in fallback_report.results:
                warnings.extend(
                    f"PAGE_{result.page_number}_{warning}" for warning in result.warnings
                )
        if source_inspection is not None:
            warnings.extend(source_inspection.warnings)
        if validation_report is not None:
            warnings.extend(
                f"PAGE_{issue.page_number}_{issue.code}" for issue in validation_report.warnings
            )
            warnings.extend(
                f"PAGE_{issue.page_number}_{issue.code}"
                for issue in validation_report.blocking_issues
            )
        return {
            "schema_version": 2,
            "status": status,
            "parser_backend": "opendataloader",
            "pdf_page_count": final_quality.pdf_page_count,
            "markdown_page_count": final_quality.markdown_page_count,
            "text_coverage_ratio": final_quality.text_coverage_ratio,
            "ocr_required_pages": list(initial_quality.ocr_required_pages),
            "ocr_page_count": final_quality.ocr_page_count,
            "low_confidence_pages": list(final_quality.low_confidence_pages),
            "vision_incomplete_pages": list(vision_incomplete_pages),
            "visually_assessed_pages": sorted(set(visually_assessed_pages)),
            "visual_no_content_pages": sorted(set(visual_no_content_pages)),
            "warnings": list(dict.fromkeys(warnings)),
            "page_quality": final_quality.to_dict(),
            "fallback": fallback_report.to_dict() if fallback_report is not None else None,
            "source_structure": (
                source_inspection.to_dict() if source_inspection is not None else None
            ),
            "content_validation": (
                validation_report.to_dict() if validation_report is not None else None
            ),
            "table_structure": (
                table_structure_report.to_dict()
                if table_structure_report is not None
                else None
            ),
            "image_assets": (
                image_policy_report.to_dict() if image_policy_report is not None else None
            ),
        }

    @staticmethod
    def _raise_pdf_quality_gate(
        status: str,
        report: dict[str, Any],
        *,
        retryable: bool = False,
    ) -> None:
        code_by_status = {
            PdfQualityStatus.PAGE_COUNT_MISMATCH.value: "PDF_PAGE_COUNT_MISMATCH",
            PdfQualityStatus.PAGE_PROVENANCE_INVALID.value: "PDF_PAGE_PROVENANCE_INVALID",
            PdfQualityStatus.OCR_REQUIRED.value: "PDF_OCR_REQUIRED",
            PdfQualityStatus.LOW_CONFIDENCE.value: "PDF_OCR_LOW_CONFIDENCE",
            "FALLBACK_INCOMPLETE": "PDF_FALLBACK_INCOMPLETE",
            "CONTENT_VALIDATION_FAILED": "PDF_CONTENT_VALIDATION_FAILED",
        }
        message_by_status = {
            PdfQualityStatus.PAGE_COUNT_MISMATCH.value: "原 PDF 页数与 ODL 页标记不一致",
            PdfQualityStatus.PAGE_PROVENANCE_INVALID.value: "ODL 输出存在无法归属原页的正文",
            PdfQualityStatus.OCR_REQUIRED.value: "扫描页 OCR 尚未完成或正文仍不足",
            PdfQualityStatus.LOW_CONFIDENCE.value: "扫描页 OCR 置信度未达到门槛",
            "FALLBACK_INCOMPLETE": "图表页视觉补齐未完成",
            "CONTENT_VALIDATION_FAILED": "表格、图片或公式专项验证未通过",
        }
        raise DocumentQualityGateError(
            quality_status=status,
            quality_report=report,
            error_code=code_by_status.get(status, "PDF_QUALITY_GATE_FAILED"),
            message=message_by_status.get(status, "PDF 页级质量门禁未通过"),
            retryable=retryable,
        )

    @staticmethod
    def _split_pdf_markdown_pages(markdown: str) -> dict[int, str]:
        marker = re.compile(
            r"^[\t ]*<!--[\t ]*ODL_PAGE:(\d+)[\t ]*-->[\t ]*\r?$",
            flags=re.MULTILINE,
        )
        matches = list(marker.finditer(markdown or ""))
        pages: dict[int, str] = {}
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown)
            pages[int(match.group(1))] = markdown[match.end() : end]
        return pages

    @staticmethod
    def _pdf_markdown_for_indexing(markdown: str) -> str:
        """Replace ODL page markers with blank lines before Markdown parsing.

        The newline is preserved, so element line numbers still map to the original
        marker positions.  This prevents a marker from joining the previous footer
        and next header into one cross-page paragraph while keeping provenance exact.
        """

        marker = re.compile(
            r"^[\t ]*<!--[\t ]*ODL_PAGE:\d+[\t ]*-->[\t ]*\r?$",
            flags=re.MULTILINE,
        )
        return marker.sub("", markdown or "")

    @staticmethod
    def _remove_pdf_visible_page_numbers(markdown: str) -> tuple[str, dict[str, Any]]:
        """从 PDF Markdown 页边缘移除可见页码，保留 ODL 页标记和行号。

        显式格式（如「第 8 页 共 30 页」）在页首/页尾直接移除。纯数字或
        「- 12 -」只在等于 PDF 页号，或多页同一侧出现稳定编号偏移时移除。
        被移除行替换为空行，不改变后续元素与 ODL_PAGE 的行号对齐。
        """

        source = markdown or ""
        lines = source.splitlines(keepends=True)
        marker = re.compile(r"^\s*<!--\s*ODL_PAGE:(\d+)\s*-->\s*$")
        explicit = re.compile(
            r"^(?:第\s*(?P<zh>\d{1,4})\s*页(?:\s*共\s*\d{1,4}\s*页)?|"
            r"(?:page\s*)?(?P<en>\d{1,4})\s*(?:/|of)\s*\d{1,4})$",
            flags=re.IGNORECASE,
        )
        bare = re.compile(r"^(?:[-—–]\s*)?(?P<number>\d{1,4})(?:\s*[-—–])?$")
        page_markers = [
            (index, int(match.group(1)))
            for index, line in enumerate(lines)
            if (match := marker.fullmatch(line.strip())) is not None
        ]

        candidates: list[dict[str, Any]] = []
        for marker_position, (marker_index, page_number) in enumerate(page_markers):
            page_end = (
                page_markers[marker_position + 1][0]
                if marker_position + 1 < len(page_markers)
                else len(lines)
            )
            content_indexes: list[int] = []
            fenced = False
            for line_index in range(marker_index + 1, page_end):
                stripped = lines[line_index].strip()
                if stripped.startswith("```") or stripped.startswith("~~~"):
                    fenced = not fenced
                    continue
                if stripped and not fenced:
                    content_indexes.append(line_index)
            head = set(content_indexes[:2])
            tail = set(content_indexes[-2:])
            for line_index in head | tail:
                text = lines[line_index].strip()
                explicit_match = explicit.fullmatch(text)
                bare_match = bare.fullmatch(text)
                if explicit_match is None and bare_match is None:
                    continue
                number_text = (
                    explicit_match.group("zh") or explicit_match.group("en")
                    if explicit_match is not None
                    else bare_match.group("number")
                )
                edge = "tail" if line_index in tail else "head"
                candidates.append(
                    {
                        "line_index": line_index,
                        "page_number": page_number,
                        "printed_number": int(number_text),
                        "edge": edge,
                        "explicit": explicit_match is not None,
                    }
                )

        offset_pages: dict[tuple[str, int], set[int]] = {}
        for candidate in candidates:
            if candidate["explicit"]:
                continue
            key = (
                str(candidate["edge"]),
                int(candidate["printed_number"]) - int(candidate["page_number"]),
            )
            offset_pages.setdefault(key, set()).add(int(candidate["page_number"]))

        removed: list[dict[str, Any]] = []
        for candidate in candidates:
            offset_key = (
                str(candidate["edge"]),
                int(candidate["printed_number"]) - int(candidate["page_number"]),
            )
            should_remove = bool(candidate["explicit"]) or (
                int(candidate["printed_number"]) == int(candidate["page_number"])
                or len(offset_pages.get(offset_key, set())) >= 2
            )
            if not should_remove:
                continue
            line_index = int(candidate["line_index"])
            original = lines[line_index]
            if original.endswith("\r\n"):
                newline = "\r\n"
            elif original.endswith("\n"):
                newline = "\n"
            else:
                newline = ""
            lines[line_index] = newline
            removed.append(
                {
                    "page_number": int(candidate["page_number"]),
                    "line_number": line_index,
                    "text": original.strip(),
                }
            )

        return "".join(lines), {
            "schema_version": 1,
            "removed_count": len(removed),
            "removed_lines": removed,
        }

    @staticmethod
    def _apply_pdf_page_numbers_from_markdown(parse_result: Any, markdown: str) -> int:
        """用真实 ODL 页标记行给元素赋页码，绝不从图片文件名推断。"""

        marker = re.compile(r"^\s*<!--\s*ODL_PAGE:(\d+)\s*-->\s*$")
        page_markers = [
            (line_number, int(match.group(1)))
            for line_number, line in enumerate((markdown or "").splitlines())
            if (match := marker.fullmatch(line)) is not None
        ]
        marker_index = 0
        current_page: int | None = None
        for element in parse_result.elements:
            while (
                marker_index < len(page_markers)
                and page_markers[marker_index][0] <= int(element.start_line)
            ):
                current_page = page_markers[marker_index][1]
                marker_index += 1
            if current_page is not None:
                element.metadata["page_number"] = current_page
        return len(page_markers)

    def _apply_pdf_structured_assets(
        self,
        parse_result: Any,
        *,
        table_structure_report: Any,
        image_policy_report: Any,
    ) -> None:
        """把表格/图片策略写入切块输入，预览资产仍留在持久化 Markdown。"""

        tables_by_part = {
            part_id: table
            for table in table_structure_report.tables
            for part_id in table.part_table_ids
        }
        table_parts_by_line: dict[tuple[int, int], tuple[Any, str]] = {}
        for table in table_structure_report.tables:
            for part_index, line_range in enumerate(table.source_line_ranges):
                if part_index < len(table.part_table_ids):
                    table_parts_by_line[tuple(line_range)] = (
                        table,
                        table.part_table_ids[part_index],
                    )
        table_index = 0
        for element in parse_result.elements:
            if element.type is not ElementType.TABLE:
                continue
            table_index += 1
            matched = table_parts_by_line.get((element.start_line, element.end_line))
            if matched is None:
                raw_table_id = f"table-{table_index:04d}"
                table = tables_by_part.get(raw_table_id)
            else:
                table, raw_table_id = matched
            if table is None:
                continue
            if raw_table_id != table.part_table_ids[0]:
                element.metadata["suppress_retrieval"] = True
                element.metadata["continuation_merged_into"] = table.table_id
                continue
            element.metadata.update(
                {
                    "table_structure": table.to_dict(),
                    "page_numbers": list(table.source_pages),
                    "start_page": table.source_page_start,
                    "end_page": table.source_page_end,
                }
            )

        decisions_by_owner: dict[int, list[Any]] = {}
        owners_by_id: dict[int, Any] = {}
        for decision in image_policy_report.assets:
            owner = next(
                (
                    element
                    for element in parse_result.elements
                    if element.start_line <= decision.line_number <= element.end_line
                ),
                None,
            )
            if owner is None:
                continue
            owner_key = id(owner)
            owners_by_id[owner_key] = owner
            decisions_by_owner.setdefault(owner_key, []).append(decision)

        for owner_key, owner_decisions in decisions_by_owner.items():
            owner = owners_by_id[owner_key]
            owner_decisions.sort(key=lambda item: item.occurrence_index)
            payloads = [decision.to_dict() for decision in owner_decisions]
            owner.metadata["image_assets"] = payloads
            owner.metadata["retrieval_eligible"] = any(
                decision.retrieval_asset for decision in owner_decisions
            )
            if len(owner_decisions) == 1:
                owner.metadata["image_asset"] = payloads[0]

            if owner.type is ElementType.IMAGE:
                decision = owner_decisions[0]
                if decision.retrieval_asset and decision.retrieval_text:
                    owner.metadata[META_VISUAL_DESCRIPTION] = decision.retrieval_text
                else:
                    owner.metadata["suppress_retrieval"] = True
                continue

            owner.content = self._filter_inline_image_references(
                owner.content,
                owner_start_line=owner.start_line,
                decisions=owner_decisions,
            )
            retrieval_descriptions = list(
                dict.fromkeys(
                    decision.retrieval_text
                    for decision in owner_decisions
                    if decision.retrieval_asset and decision.retrieval_text
                )
            )
            if retrieval_descriptions:
                description_text = "\n".join(
                    f"图片说明：{description}" for description in retrieval_descriptions
                )
                owner.content = "\n\n".join(
                    part for part in (owner.content.strip(), description_text) if part
                )
            if not owner.content.strip():
                owner.metadata["suppress_retrieval"] = True

    @staticmethod
    def _apply_pdf_retrieval_noise_policy(parse_result: Any) -> dict[str, Any]:
        """Suppress repeated page-edge boilerplate from retrieval, not preview.

        The original Markdown remains untouched.  Only short paragraph/heading/list
        elements repeated at the first/last two positions on most pages are marked for
        the chunker to skip.  Explicit page numbers and public/signature boilerplate
        are also suppressed.  Tables, formulas, code and image assets are excluded
        from this text-noise heuristic.
        """

        eligible_types = {ElementType.HEADING, ElementType.PARAGRAPH, ElementType.LIST}
        elements_by_page: dict[int, list[Any]] = {}
        for element in parse_result.elements:
            page_number = element.metadata.get("page_number")
            if isinstance(page_number, int) and page_number > 0:
                elements_by_page.setdefault(page_number, []).append(element)

        page_count = len(elements_by_page)
        edge_occurrences: dict[str, set[int]] = {}
        normalized_by_element: dict[int, str] = {}
        for page_number, elements in elements_by_page.items():
            candidates = [element for element in elements if element.type in eligible_types]
            edge_elements = [*candidates[:2], *candidates[-2:]]
            for element in edge_elements:
                normalized = re.sub(r"\s+", "", element.content).strip("#*_—- ").casefold()
                if not normalized or len(normalized) > 120:
                    continue
                normalized_by_element[id(element)] = normalized
                edge_occurrences.setdefault(normalized, set()).add(page_number)

        repeated_threshold = max(2, math.ceil(page_count * 0.6)) if page_count else 2
        repeated_noise = {
            text
            for text, pages in edge_occurrences.items()
            if len(pages) >= repeated_threshold
        }
        explicit_noise = re.compile(
            r"^(?:第?\d+页(?:共\d+页)?|page\d+(?:of\d+)?|"
            r"公开属性[:：]?.{0,40}|(?:签名|签字|盖章|公章)[:：]?.{0,40})$",
            flags=re.IGNORECASE,
        )

        suppressed: list[dict[str, object]] = []
        for page_number, elements in elements_by_page.items():
            for element in elements:
                if element.type not in eligible_types:
                    continue
                normalized = normalized_by_element.get(id(element)) or re.sub(
                    r"\s+",
                    "",
                    element.content,
                ).strip("#*_—- ").casefold()
                reason = None
                if normalized in repeated_noise:
                    reason = "REPEATED_PAGE_EDGE_TEXT"
                elif len(normalized) <= 120 and explicit_noise.fullmatch(normalized):
                    reason = "EXPLICIT_BOILERPLATE"
                if reason is None:
                    continue
                element.metadata["suppress_retrieval"] = True
                element.metadata["retrieval_noise_reason"] = reason
                suppressed.append(
                    {
                        "page_number": page_number,
                        "start_line": int(element.start_line),
                        "reason": reason,
                        "text_preview": element.content.strip()[:80],
                    }
                )
        return {
            "schema_version": 1,
            "page_count": page_count,
            "repeated_page_threshold": repeated_threshold,
            "suppressed_element_count": len(suppressed),
            "suppressed_elements": suppressed,
        }

    def _filter_inline_image_references(
        self,
        content: str,
        *,
        owner_start_line: int,
        decisions: Sequence[Any],
    ) -> str:
        """Remove only the concrete preview-only occurrences owned by this block."""

        image_reference = re.compile(
            r"!\[[^\]]*\]\(\s*(?:<(?P<md_angle>[^>]+)>|(?P<md_plain>[^\s)]+))"
            r"[^)]*\)|<img\b[^>]*\bsrc\s*=\s*(?:\"(?P<html_double>[^\"]+)\"|"
            r"'(?P<html_single>[^']+)'|(?P<html_bare>[^\s>]+))[^>]*>",
            flags=re.IGNORECASE,
        )
        pending: dict[tuple[int, str], list[Any]] = {}
        for decision in decisions:
            key = (
                decision.line_number,
                self._pdf_image_asset_policy.normalize_source_ref(decision.source_ref),
            )
            pending.setdefault(key, []).append(decision)

        def replace_reference(match: re.Match[str]) -> str:
            source_ref = (
                match.group("md_angle")
                or match.group("md_plain")
                or match.group("html_double")
                or match.group("html_single")
                or match.group("html_bare")
                or ""
            )
            line_number = owner_start_line + content.count("\n", 0, match.start())
            key = (
                line_number,
                self._pdf_image_asset_policy.normalize_source_ref(source_ref),
            )
            matches = pending.get(key)
            if not matches:
                return match.group(0)
            decision = matches.pop(0)
            return match.group(0) if decision.retrieval_asset else ""

        return image_reference.sub(replace_reference, content).strip()

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
                "backend": settings.PDF_PARSER_BACKEND,
                "storage": self._storage,
                "image_bucket": parsed_bucket,
                "image_prefix": image_prefix,
                # 入库返回 READY 时图片必须已经真实持久化。
                "image_upload_async": False,
            }
        elif identity.file_type in {"doc", "docx"}:
            parser_kwargs = {
                "storage": self._storage,
                "image_bucket": parsed_bucket,
                "image_prefix": image_prefix,
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
        if identity.file_type == "pdf" and metadata.get("pdf_parser_backend") not in {
            "mineru",
            "opendataloader",
        }:
            raise DocumentIngestionError("PDF 未由 MinerU 或 OpenDataLoader 完成解析")
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
        if isinstance(metadata.get("retrieval_eligible"), bool):
            result["retrieval_eligible"] = metadata["retrieval_eligible"]

        def json_native(value: Any, *, depth: int = 0) -> Any:
            if depth > 16:
                return None
            if value is None or isinstance(value, (str, int, float, bool)):
                return value
            if isinstance(value, dict):
                return {
                    str(key): cleaned
                    for key, item in value.items()
                    if (cleaned := json_native(item, depth=depth + 1)) is not None
                }
            if isinstance(value, (list, tuple)):
                return [
                    cleaned
                    for item in value
                    if (cleaned := json_native(item, depth=depth + 1)) is not None
                ]
            return None

        for key in (
            "table_structure",
            "image_asset",
            "image_assets",
            "page_visual_description",
        ):
            if isinstance(metadata.get(key), dict):
                result[key] = json_native(metadata[key])
            elif key == "image_assets" and isinstance(metadata.get(key), list):
                result[key] = json_native(metadata[key])
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
