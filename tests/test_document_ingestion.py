from __future__ import annotations

from types import SimpleNamespace

import pymupdf
import pytest

from app.domain.models import Document
from app.rag.config import settings
from app.rag.core.dataset_config.execution_context import DatasetExecutionPurpose
from app.rag.core.encoding.sparse.models import SparseVector
from app.rag.core.markdown_parser.models import ElementType
from app.rag.core.markdown_parser.parser import MarkdownParser
from app.rag.core.parser.pdf.image_asset_policy import (
    ImageAssetCategory,
    PdfImageAssetPolicy,
    StructuredVisualDescription,
    VisualRelationship,
)
from app.rag.core.parser.pdf.page_fallback import PageFallbackMethod
from app.rag.core.parser.pdf.table_structure import PdfTableStructureExtractor
from app.rag.core.preprocessor.ragflow_tokenizer import TokenizedText
from app.rag.core.splitter.models import Chunk, EmbeddedChunk
from app.rag.core.storage.bm25_models import Bm25IndexingResult
from app.services.document_ingestion import (
    DocumentIngestionError,
    DocumentIngestionLeaseLost,
    DocumentQualityGateError,
    SimpleDocumentIngestionService,
)


class _ScalarResult:
    def __init__(self, values):
        self._values = values

    def all(self):
        return list(self._values)


class _ExecuteResult:
    def __init__(self, values=()):
        self._values = values

    def scalars(self):
        return _ScalarResult(self._values)


class _FakeSession:
    def __init__(self, old_chunk_ids=()):
        self.old_chunk_ids = list(old_chunk_ids)
        self.statements = []
        self.added = []
        self.added_many = []
        self.commits = 0
        self.rollbacks = 0
        self.flushes = 0

    async def execute(self, statement):
        self.statements.append(statement)
        if getattr(statement, "is_select", False):
            return _ExecuteResult(self.old_chunk_ids)
        return _ExecuteResult()

    def add(self, value):
        self.added.append(value)

    def add_all(self, values):
        self.added_many.extend(values)

    async def flush(self):
        self.flushes += 1

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


class _FakeStorage:
    def __init__(self):
        self.uploads = []
        self.removed = []
        self.removed_objects = []

    def upload_bytes(self, bucket, object_key, content, content_type):
        self.uploads.append((bucket, object_key, content, content_type))

    def remove_prefix(self, bucket, prefix):
        self.removed.append((bucket, prefix))
        return 0

    def remove_object(self, bucket, object_key):
        self.removed_objects.append((bucket, object_key))
        return True


class _FakeContextLoader:
    def __init__(self, context):
        self.context = context
        self.calls = []

    async def load(self, user_id, dataset_id, purpose):
        self.calls.append((user_id, dataset_id, purpose))
        return self.context


class _FakeParseService:
    calls = []

    @classmethod
    async def aprocess(cls, source_path, file_type, **kwargs):
        cls.calls.append((source_path, file_type, kwargs))
        return {
            "markdown": (
                "<!-- ODL_PAGE:1 -->\n"
                "Complete source page 1 for deterministic quality validation.\n"
                "<!-- ODL_PAGE:2 -->\n"
                "Complete source page 2 for deterministic quality validation.\n"
                "<!-- ODL_PAGE:3 -->\n"
                "Complete source page 3 for deterministic quality validation."
            ),
            "parse_result": object(),
            "metadata": {
                "pdf_parser_backend": "opendataloader",
                "pages_or_length": 3,
            },
            "time_cost_ms": 27,
        }


class _FailingParseService:
    @staticmethod
    async def aprocess(*args, **kwargs):
        raise RuntimeError("parse exploded")


class _MissingPageMarkerParseService:
    @staticmethod
    async def aprocess(*args, **kwargs):
        return {
            "markdown": (
                "<!-- ODL_PAGE:1 -->\n第一页正文完整。\n"
                "<!-- ODL_PAGE:3 -->\n第三页正文完整。"
            ),
            "parse_result": object(),
            "metadata": {"pdf_parser_backend": "opendataloader", "pages_or_length": 3},
            "time_cost_ms": 4,
        }


class _ImageOnlyParseService:
    @staticmethod
    async def aprocess(*args, **kwargs):
        return {
            "markdown": "<!-- ODL_PAGE:1 -->\n![扫描页](images/page.png)",
            "parse_result": object(),
            "metadata": {"pdf_parser_backend": "opendataloader", "pages_or_length": 1},
            "time_cost_ms": 5,
        }


class _FakeChunkingEngine:
    def __init__(self, chunks):
        self.chunks = chunks
        self.calls = 0

    async def aprocess_parse_result(self, parse_result):
        self.calls += 1
        return self.chunks


class _FakeDensePipeline:
    def __init__(self):
        self.embedder = object()
        self.calls = 0

    async def aembed_chunks(self, chunks):
        self.calls += 1
        return [
            EmbeddedChunk(chunk=chunk, embedding=[0.1] * settings.DENSE_VECTOR_DIMENSION)
            for chunk in chunks
        ]


class _FakeSparseService:
    vector_name = "sparse"

    def __init__(self):
        self.contents = []

    async def vectorize_texts(self, contents):
        self.contents = list(contents)
        return [SparseVector(indices=[index + 1], values=[0.5]) for index, _ in enumerate(contents)]


class _FakeQdrantStore:
    def __init__(self):
        self.vector_size = None
        self.ensured_points = []
        self.dense_points = []
        self.sparse_vector_name = None
        self.sparse_points = []
        self.deleted = []

    async def ensure_collection(self, *, vector_size):
        self.vector_size = vector_size

    async def ensure_points(self, *, points):
        self.ensured_points = list(points)

    async def upsert_points(self, *, points):
        self.dense_points = list(points)

    async def ensure_sparse_vector_schema(self, *, vector_name):
        self.sparse_vector_name = vector_name

    async def upsert_sparse_vectors(self, *, points):
        self.sparse_points = list(points)

    async def delete_points(self, *, chunk_ids):
        self.deleted.extend(chunk_ids)


class _FakeBm25Pipeline:
    def __init__(self):
        self.deleted = []
        self.plan = None

    async def delete_document_index(self, *, user_id, dataset_id, doc_id):
        self.deleted.append((user_id, dataset_id, doc_id))
        return 0

    async def write_es_index(self, plan, *, db):
        assert db is None
        self.plan = plan
        count = len(plan.chunks_with_tokens)
        return Bm25IndexingResult(
            total_items=count,
            indexed_items=count,
            succeeded_item_ids=[item.chunk_id for item in plan.chunks_with_tokens],
        )


class _FailingBm25Pipeline(_FakeBm25Pipeline):
    async def write_es_index(self, plan, *, db):
        assert db is None
        self.plan = plan
        return Bm25IndexingResult(
            total_items=len(plan.chunks_with_tokens),
            indexed_items=0,
            failed_item_ids=[item.chunk_id for item in plan.chunks_with_tokens],
            failure_reason="manticore unavailable",
        )


class _FakeTokenizer:
    def tokenize(self, text):
        return TokenizedText(coarse_tokens=f"coarse:{text}", fine_tokens=f"fine:{text}")


def _document() -> Document:
    return Document(
        id=7,
        dataset_id=9,
        user_id=11,
        filename="report.pdf",
        file_type="pdf",
        file_size=128,
        content_type="application/pdf",
        raw_bucket="raw",
        raw_object_key="raw/report.pdf",
        parser_backend="opendataloader",
        status="PROCESSING",
    )


def _write_pdf(path, *, page_count: int = 3) -> None:
    pdf = pymupdf.open()
    for page_number in range(1, page_count + 1):
        page = pdf.new_page()
        page.insert_text(
            (72, 72),
            f"Complete source page {page_number} for deterministic quality validation.",
        )
    pdf.save(path)
    pdf.close()


def _write_image_only_pdf(path) -> None:
    pdf = pymupdf.open()
    page = pdf.new_page()
    pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 80, 80), False)
    pixmap.clear_with(245)
    page.insert_image(page.rect, stream=pixmap.tobytes("png"))
    pdf.save(path)
    pdf.close()


def _context():
    return SimpleNamespace(
        config=SimpleNamespace(chunking=object(), enhancement=object()),
        dense_embedding=SimpleNamespace(model_name="dense-model"),
        sparse_embedding=SimpleNamespace(model_name="sparse-model"),
    )


@pytest.mark.asyncio
async def test_ingest_uses_one_chunk_set_for_db_and_all_three_indexes(tmp_path):
    source_path = tmp_path / "report.pdf"
    _write_pdf(source_path)
    chunks = [
        Chunk(
            content="first",
            start_line=1,
            end_line=2,
            metadata={"element_types": ["paragraph"], "chunk_index": 0},
        ),
        Chunk(
            content="second",
            start_line=3,
            end_line=4,
            metadata={"element_types": ["paragraph"], "chunk_index": 1},
        ),
    ]
    context_loader = _FakeContextLoader(_context())
    chunking_engine = _FakeChunkingEngine(chunks)
    dense_pipeline = _FakeDensePipeline()
    sparse_service = _FakeSparseService()
    storage = _FakeStorage()
    qdrant = _FakeQdrantStore()
    bm25 = _FakeBm25Pipeline()
    db = _FakeSession()
    _FakeParseService.calls = []

    service = SimpleDocumentIngestionService(
        storage=storage,
        qdrant_store=qdrant,
        bm25_pipeline=bm25,
        execution_context_loader_factory=lambda db: context_loader,
        parse_service=_FakeParseService,
        dense_pipeline_builder=lambda resolved: dense_pipeline,
        chunking_engine_factory=lambda **kwargs: chunking_engine,
        sparse_service_builder=lambda resolved: sparse_service,
        tokenizer_factory=_FakeTokenizer,
    )
    document = _document()

    result = await service.ingest(document, source_path, db)

    assert context_loader.calls == [(11, 9, DatasetExecutionPurpose.PARSE)]
    assert chunking_engine.calls == 1
    assert dense_pipeline.calls == 1
    assert sparse_service.contents == ["first", "second"]
    assert qdrant.vector_size == settings.DENSE_VECTOR_DIMENSION
    assert qdrant.sparse_vector_name == "sparse"
    assert qdrant.deleted == []

    db_ids = [row.chunk_id for row in db.added_many]
    dense_ids = [point.chunk_id for point in qdrant.dense_points]
    sparse_ids = [point.chunk_id for point in qdrant.sparse_points]
    bm25_ids = [item.chunk_id for item in bm25.plan.chunks_with_tokens]
    assert db_ids == dense_ids == sparse_ids == bm25_ids

    _, _, parser_kwargs = _FakeParseService.calls[0]
    assert parser_kwargs["backend"] == "opendataloader"
    assert parser_kwargs["image_upload_async"] is False
    assert parser_kwargs["storage"] is storage
    assert parser_kwargs["image_bucket"] == settings.MINIO_PRIVATE_BUCKET
    assert parser_kwargs["image_prefix"] == (
        "parsed/11/9/7/versions/v1/attempt-1/images"
    )
    assert storage.uploads == [
        (
            settings.MINIO_PRIVATE_BUCKET,
                "parsed/11/9/7/versions/v1/attempt-1/report.md",
                (
                    b"<!-- ODL_PAGE:1 -->\n"
                    b"Complete source page 1 for deterministic quality validation.\n"
                    b"<!-- ODL_PAGE:2 -->\n"
                    b"Complete source page 2 for deterministic quality validation.\n"
                    b"<!-- ODL_PAGE:3 -->\n"
                    b"Complete source page 3 for deterministic quality validation."
                ),
            "text/markdown; charset=utf-8",
        )
    ]

    assert document.status == "READY"
    assert document.chunk_count == 2
    assert document.page_count == 3
    assert document.parse_time_ms == 27
    assert result.chunk_count == 2
    assert db.flushes == 1
    assert db.commits == 1
    assert db.rollbacks == 0


@pytest.mark.asyncio
async def test_ingest_marks_document_failed_and_reraises_original_error(tmp_path):
    source_path = tmp_path / "report.pdf"
    _write_pdf(source_path)
    context_loader = _FakeContextLoader(_context())
    db = _FakeSession()
    document = _document()
    storage = _FakeStorage()
    service = SimpleDocumentIngestionService(
        storage=storage,
        qdrant_store=_FakeQdrantStore(),
        bm25_pipeline=_FakeBm25Pipeline(),
        execution_context_loader_factory=lambda db: context_loader,
        parse_service=_FailingParseService,
    )

    with pytest.raises(RuntimeError, match="parse exploded"):
        await service.ingest(document, source_path, db)

    assert document.status == "FAILED"
    assert document.error_message == "RuntimeError: parse exploded"
    assert storage.removed == [
        (
            settings.MINIO_PRIVATE_BUCKET,
            "parsed/11/9/7/versions/v1/attempt-1",
        )
    ]
    assert db.rollbacks == 1
    assert db.commits == 1


@pytest.mark.asyncio
async def test_page_marker_mismatch_fails_before_upload_or_index(tmp_path) -> None:
    source_path = tmp_path / "report.pdf"
    _write_pdf(source_path)
    storage = _FakeStorage()
    qdrant = _FakeQdrantStore()
    bm25 = _FakeBm25Pipeline()
    db = _FakeSession()
    document = _document()
    service = SimpleDocumentIngestionService(
        storage=storage,
        qdrant_store=qdrant,
        bm25_pipeline=bm25,
        execution_context_loader_factory=lambda db: _FakeContextLoader(_context()),
        parse_service=_MissingPageMarkerParseService,
    )

    with pytest.raises(DocumentQualityGateError) as caught:
        await service.ingest(document, source_path, db)

    assert getattr(caught.value, "error_code", None) == "PDF_PAGE_COUNT_MISMATCH"
    assert document.status == "FAILED"
    assert document.parse_quality_status == "PAGE_COUNT_MISMATCH"
    assert document.parse_quality["pdf_page_count"] == 3
    assert document.parse_quality["markdown_page_count"] == 2
    assert storage.uploads == []
    assert qdrant.dense_points == []
    assert qdrant.sparse_points == []
    assert bm25.plan is None


@pytest.mark.asyncio
async def test_image_only_pdf_without_ocr_provider_cannot_become_ready(tmp_path) -> None:
    source_path = tmp_path / "scan.pdf"
    _write_image_only_pdf(source_path)
    storage = _FakeStorage()
    db = _FakeSession()
    document = _document()
    service = SimpleDocumentIngestionService(
        storage=storage,
        qdrant_store=_FakeQdrantStore(),
        bm25_pipeline=_FakeBm25Pipeline(),
        execution_context_loader_factory=lambda db: _FakeContextLoader(_context()),
        parse_service=_ImageOnlyParseService,
    )

    with pytest.raises(DocumentQualityGateError) as caught:
        await service.ingest(document, source_path, db)

    assert getattr(caught.value, "error_code", None) == "PDF_OCR_REQUIRED"
    assert document.status == "FAILED"
    assert document.parse_quality_status == "OCR_REQUIRED"
    assert document.parse_quality["ocr_required_pages"] == [1]
    fallback_result = document.parse_quality["fallback"]["results"][0]
    assert fallback_result["method"] == "ocr"
    assert "OCR_PROVIDER_MISSING" in fallback_result["warnings"]
    assert storage.uploads == []


@pytest.mark.asyncio
async def test_manticore_failure_is_raised_and_partial_indexes_are_cleaned(tmp_path):
    source_path = tmp_path / "report.pdf"
    _write_pdf(source_path)
    chunks = [
        Chunk(
            content="first",
            start_line=1,
            end_line=2,
            metadata={"element_types": ["paragraph"], "chunk_index": 0},
        )
    ]
    context_loader = _FakeContextLoader(_context())
    chunking_engine = _FakeChunkingEngine(chunks)
    dense_pipeline = _FakeDensePipeline()
    sparse_service = _FakeSparseService()
    qdrant = _FakeQdrantStore()
    bm25 = _FailingBm25Pipeline()
    db = _FakeSession()
    storage = _FakeStorage()
    service = SimpleDocumentIngestionService(
        storage=storage,
        qdrant_store=qdrant,
        bm25_pipeline=bm25,
        execution_context_loader_factory=lambda db: context_loader,
        parse_service=_FakeParseService,
        dense_pipeline_builder=lambda resolved: dense_pipeline,
        chunking_engine_factory=lambda **kwargs: chunking_engine,
        sparse_service_builder=lambda resolved: sparse_service,
        tokenizer_factory=_FakeTokenizer,
    )
    document = _document()

    with pytest.raises(DocumentIngestionError, match="manticore unavailable"):
        await service.ingest(document, source_path, db)

    indexed_ids = [point.chunk_id for point in qdrant.dense_points]
    assert indexed_ids
    assert qdrant.deleted == indexed_ids
    # 第一次是全量重建前删除，第二次是失败补偿清理。
    assert bm25.deleted == [(11, 9, 7), (11, 9, 7)]
    assert storage.removed == [
        (
            settings.MINIO_PRIVATE_BUCKET,
            "parsed/11/9/7/versions/v1/attempt-1",
        )
    ]
    assert db.added_many == []
    assert db.rollbacks == 1
    assert db.commits == 1
    assert document.status == "FAILED"
    assert document.error_message == "DocumentIngestionError: manticore unavailable"


@pytest.mark.asyncio
async def test_ingest_rejects_overwriting_existing_chunks(tmp_path):
    source_path = tmp_path / "report.pdf"
    _write_pdf(source_path)
    db = _FakeSession(old_chunk_ids=["existing-chunk"])
    document = _document()
    storage = _FakeStorage()
    qdrant = _FakeQdrantStore()
    bm25 = _FakeBm25Pipeline()
    _FakeParseService.calls = []

    service = SimpleDocumentIngestionService(
        storage=storage,
        qdrant_store=qdrant,
        bm25_pipeline=bm25,
        execution_context_loader_factory=lambda db: _FakeContextLoader(_context()),
        parse_service=_FakeParseService,
    )

    with pytest.raises(DocumentIngestionError, match="不支持覆盖已有 chunk"):
        await service.ingest(document, source_path, db)

    assert _FakeParseService.calls == []
    assert qdrant.dense_points == []
    assert qdrant.sparse_points == []
    assert bm25.plan is None
    assert document.status == "FAILED"
    assert db.rollbacks == 1
    assert db.commits == 1
    assert "不支持覆盖已有 chunk" in document.error_message


@pytest.mark.asyncio
async def test_reparse_replaces_old_indexes_and_persists_version_and_explicit_pages(tmp_path):
    source_path = tmp_path / "report.pdf"
    _write_pdf(source_path)
    chunks = [
        Chunk(
            content="updated",
            start_line=1,
            end_line=3,
            metadata={
                "element_types": ["paragraph"],
                "chunk_index": 0,
                "page_range": [2, 4],
                "heading_trail": ["排放核算", "边界"],
                "split_strategy": "candidate_boundary + noop",
                "chunk_role": "mixed",
                "source_file": "/internal/path/report.pdf",
            },
        )
    ]
    qdrant = _FakeQdrantStore()
    db = _FakeSession(old_chunk_ids=["old-chunk"])
    storage = _FakeStorage()
    service = SimpleDocumentIngestionService(
        storage=storage,
        qdrant_store=qdrant,
        bm25_pipeline=_FakeBm25Pipeline(),
        execution_context_loader_factory=lambda db: _FakeContextLoader(_context()),
        parse_service=_FakeParseService,
        dense_pipeline_builder=lambda resolved: _FakeDensePipeline(),
        chunking_engine_factory=lambda **kwargs: _FakeChunkingEngine(chunks),
        sparse_service_builder=lambda resolved: _FakeSparseService(),
        tokenizer_factory=_FakeTokenizer,
    )
    document = _document()
    document.version = 2
    document.reparse_requested = True
    document.parsed_bucket = settings.MINIO_PRIVATE_BUCKET
    document.parsed_object_key = "parsed/11/9/7/versions/v1/lease-old/report.md"

    await service.ingest(document, source_path, db, replace_existing=True)

    assert qdrant.deleted == ["old-chunk"]
    assert len(db.added_many) == 1
    record = db.added_many[0]
    assert record.document_version == 2
    assert (record.start_page, record.end_page) == (2, 4)
    assert record.structure_metadata == {
        "split_strategy": "candidate_boundary + noop",
        "chunk_role": "mixed",
        "heading_trail": ["排放核算", "边界"],
        "element_types": ["paragraph"],
    }
    assert "source_file" not in record.structure_metadata
    assert document.status == "READY"
    assert document.reparse_requested is False
    assert document.parsed_object_key == (
        "parsed/11/9/7/versions/v2/attempt-1/report.md"
    )
    assert storage.removed == [
        (
            settings.MINIO_PRIVATE_BUCKET,
            "parsed/11/9/7/versions/v1/lease-old",
        )
    ]


@pytest.mark.asyncio
async def test_reparse_failure_preserves_previous_ready_pointer_and_metrics(tmp_path):
    source_path = tmp_path / "report.pdf"
    _write_pdf(source_path)
    storage = _FakeStorage()
    db = _FakeSession()
    document = _document()
    document.version = 2
    document.reparse_requested = True
    document.parsed_bucket = settings.MINIO_PRIVATE_BUCKET
    document.parsed_object_key = "parsed/11/9/7/versions/v1/lease-old/report.md"
    document.page_count = 8
    document.chunk_count = 12
    document.parse_time_ms = 44
    service = SimpleDocumentIngestionService(
        storage=storage,
        qdrant_store=_FakeQdrantStore(),
        bm25_pipeline=_FakeBm25Pipeline(),
        execution_context_loader_factory=lambda db: _FakeContextLoader(_context()),
        parse_service=_FailingParseService,
    )

    with pytest.raises(RuntimeError, match="parse exploded"):
        await service.ingest(document, source_path, db, replace_existing=True)

    assert document.status == "FAILED"
    assert document.parsed_bucket == settings.MINIO_PRIVATE_BUCKET
    assert document.parsed_object_key == (
        "parsed/11/9/7/versions/v1/lease-old/report.md"
    )
    assert (document.page_count, document.chunk_count, document.parse_time_ms) == (8, 12, 44)
    assert storage.removed == [
        (
            settings.MINIO_PRIVATE_BUCKET,
            "parsed/11/9/7/versions/v2/attempt-1",
        )
    ]


@pytest.mark.asyncio
async def test_successful_reparse_safely_cleans_legacy_unversioned_output() -> None:
    storage = _FakeStorage()
    service = SimpleDocumentIngestionService(
        storage=storage,
        qdrant_store=_FakeQdrantStore(),
        bm25_pipeline=_FakeBm25Pipeline(),
    )
    identity = service._document_identity(_document())

    await service._cleanup_superseded_parsed_output(
        identity,
        previous_parsed_location=(
            settings.MINIO_PRIVATE_BUCKET,
            "parsed/11/9/7/report.md",
        ),
        current_parsed_location=(
            settings.MINIO_PRIVATE_BUCKET,
            "parsed/11/9/7/versions/v2/lease-new/report.md",
        ),
    )

    assert storage.removed_objects == [
        (settings.MINIO_PRIVATE_BUCKET, "parsed/11/9/7/report.md")
    ]
    assert storage.removed == [
        (settings.MINIO_PRIVATE_BUCKET, "parsed/11/9/7/image"),
        (settings.MINIO_PRIVATE_BUCKET, "parsed/11/9/7/images"),
    ]


@pytest.mark.asyncio
async def test_lost_lease_stops_before_parse_without_marking_failed_or_cleaning(tmp_path):
    source_path = tmp_path / "report.pdf"
    _write_pdf(source_path)
    storage = _FakeStorage()
    db = _FakeSession()
    service = SimpleDocumentIngestionService(
        storage=storage,
        qdrant_store=_FakeQdrantStore(),
        bm25_pipeline=_FakeBm25Pipeline(),
        execution_context_loader_factory=lambda db: _FakeContextLoader(_context()),
        parse_service=_FakeParseService,
    )
    document = _document()

    with pytest.raises(DocumentIngestionLeaseLost):
        await service.ingest(
            document,
            source_path,
            db,
            lease_token="stale-token",
            lease_guard=lambda: False,
            manage_failure=False,
        )

    assert document.status == "PROCESSING"
    assert storage.removed == []
    assert db.commits == 0
    assert db.rollbacks == 1


@pytest.mark.asyncio
async def test_lost_lease_after_target_allocation_cleans_only_its_attempt_prefix(tmp_path):
    source_path = tmp_path / "report.pdf"
    _write_pdf(source_path)
    storage = _FakeStorage()
    db = _FakeSession()
    service = SimpleDocumentIngestionService(
        storage=storage,
        qdrant_store=_FakeQdrantStore(),
        bm25_pipeline=_FakeBm25Pipeline(),
        execution_context_loader_factory=lambda db: _FakeContextLoader(_context()),
        parse_service=_FakeParseService,
    )
    document = _document()
    lease_checks = iter((True, True, False))

    with pytest.raises(DocumentIngestionLeaseLost):
        await service.ingest(
            document,
            source_path,
            db,
            lease_token="stale-token",
            lease_guard=lambda: next(lease_checks),
            manage_failure=False,
        )

    assert document.status == "PROCESSING"
    assert storage.removed == [
        (
            settings.MINIO_PRIVATE_BUCKET,
            "parsed/11/9/7/versions/v1/stale-token",
        )
    ]
    assert db.commits == 0
    assert db.rollbacks == 1


@pytest.mark.asyncio
async def test_ready_terminal_state_uses_lease_token_cas() -> None:
    class _LostLeaseSession(_FakeSession):
        async def execute(self, statement):
            self.statements.append(statement)
            return SimpleNamespace(rowcount=0)

    db = _LostLeaseSession()
    document = _document()
    service = SimpleDocumentIngestionService(
        storage=_FakeStorage(),
        qdrant_store=_FakeQdrantStore(),
        bm25_pipeline=_FakeBm25Pipeline(),
    )

    with pytest.raises(DocumentIngestionLeaseLost, match="READY"):
        await service.mark_ready(
            document,
            db,
            parsed_bucket="docs",
            parsed_object_key="parsed/7/report.md",
            page_count=1,
            chunk_count=2,
            parse_time_ms=3,
            parse_quality_status="PASSED",
            parse_quality={"status": "PASSED"},
            lease_token="stale-token",
        )

    assert document.status == "PROCESSING"
    assert db.commits == 0
    assert db.rollbacks == 1


@pytest.mark.asyncio
async def test_ready_terminal_state_rejects_pdf_that_failed_quality_gate() -> None:
    db = _FakeSession()
    document = _document()
    service = SimpleDocumentIngestionService(
        storage=_FakeStorage(),
        qdrant_store=_FakeQdrantStore(),
        bm25_pipeline=_FakeBm25Pipeline(),
    )

    with pytest.raises(DocumentIngestionError, match="禁止写入 READY"):
        await service.mark_ready(
            document,
            db,
            parsed_bucket="docs",
            parsed_object_key="parsed/7/report.md",
            page_count=1,
            chunk_count=2,
            parse_time_ms=3,
            parse_quality_status="OCR_REQUIRED",
            parse_quality={"status": "OCR_REQUIRED"},
        )

    assert document.status == "PROCESSING"
    assert db.commits == 0


@pytest.mark.asyncio
async def test_ready_terminal_state_rejects_missing_pdf_quality_report_status() -> None:
    db = _FakeSession()
    document = _document()
    service = SimpleDocumentIngestionService(
        storage=_FakeStorage(),
        qdrant_store=_FakeQdrantStore(),
        bm25_pipeline=_FakeBm25Pipeline(),
    )

    with pytest.raises(DocumentIngestionError, match="禁止写入 READY"):
        await service.mark_ready(
            document,
            db,
            parsed_bucket="docs",
            parsed_object_key="parsed/7/report.md",
            page_count=1,
            chunk_count=2,
            parse_time_ms=3,
            parse_quality_status="PASSED",
            parse_quality={},
        )

    assert document.status == "PROCESSING"
    assert db.commits == 0


@pytest.mark.parametrize(
    ("metadata", "expected"),
    [
        ({"page": 3}, (3, 3)),
        ({"page_number": "5"}, (5, 5)),
        ({"page_numbers": [8, 6, 7]}, (6, 8)),
        ({"start_page": 2, "end_page": 9}, (2, 9)),
        ({"page_range": {"start": 4, "end": 6}}, (4, 6)),
        ({"heading": "no page metadata"}, (None, None)),
    ],
)
def test_page_range_only_uses_explicit_chunk_metadata(metadata, expected) -> None:
    assert SimpleDocumentIngestionService._chunk_page_range(metadata) == expected


def test_visual_fallback_requires_structured_result_and_no_pending_asset_task() -> None:
    fallback = SimpleNamespace(
        results=[
            SimpleNamespace(page_number=2, method=PageFallbackMethod.VISION),
            SimpleNamespace(page_number=4, method=PageFallbackMethod.OCR),
        ]
    )
    image_report = SimpleNamespace(
        assets=[
            SimpleNamespace(page_number=3, visual_task=object()),
            SimpleNamespace(page_number=4, visual_task=None),
        ]
    )

    assert SimpleDocumentIngestionService._pdf_vision_incomplete_pages(
        fallback,
        structured_by_page={},
        image_policy_report=image_report,
    ) == [2, 3, 4]
    assert SimpleDocumentIngestionService._pdf_vision_incomplete_pages(
        fallback,
        structured_by_page={2: object()},
        visually_assessed_pages=(4,),
        image_policy_report=image_report,
    ) == [3]
    assert SimpleDocumentIngestionService._pdf_vision_incomplete_pages(
        fallback,
        structured_by_page={},
        visually_assessed_pages=(2, 3, 4),
        image_policy_report=image_report,
    ) == []


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"has_visual_content": False, "visual_content_type": "none"}, False),
        ({"has_visual_content": False, "visual_content_type": "chart"}, None),
        ({"has_visual_content": True, "visual_content_type": "chart"}, True),
        ({"has_visual_content": True, "visual_content_type": "none"}, None),
        ({"visual_content_type": "none"}, None),
    ],
)
def test_visual_assessment_requires_a_consistent_boolean_and_type(
    payload,
    expected,
) -> None:
    assert SimpleDocumentIngestionService._visual_assessment_state(payload) is expected


def test_visual_description_requires_matching_page_and_real_structure() -> None:
    summary_only = StructuredVisualDescription(
        source_page=2,
        summary="这是一个图表。",
    )
    wrong_page = StructuredVisualDescription(
        source_page=99,
        summary="活动数据流向排放量。",
        entities=("活动数据", "排放量"),
    )
    complete = StructuredVisualDescription(
        source_page=2,
        summary="活动数据经过排放因子计算得到排放量。",
        entities=("活动数据", "排放因子", "排放量"),
        relationships=(
            VisualRelationship("活动数据", "参与计算", "排放量"),
        ),
    )

    assert SimpleDocumentIngestionService._structured_visual_is_complete(
        summary_only,
        expected_page=2,
    ) is False
    assert SimpleDocumentIngestionService._structured_visual_is_complete(
        wrong_page,
        expected_page=2,
    ) is False
    assert SimpleDocumentIngestionService._structured_visual_is_complete(
        complete,
        expected_page=2,
    ) is True
    assert SimpleDocumentIngestionService._structured_visual_is_complete(
        complete,
        expected_page=2,
        categories=(ImageAssetCategory.CHART,),
    ) is False


def test_quality_gate_can_preserve_transient_fallback_retry_semantics() -> None:
    with pytest.raises(DocumentQualityGateError) as caught:
        SimpleDocumentIngestionService._raise_pdf_quality_gate(
            "FALLBACK_INCOMPLETE",
            {"status": "FALLBACK_INCOMPLETE"},
            retryable=True,
        )

    assert caught.value.retryable is True
    assert caught.value.error_code == "PDF_FALLBACK_INCOMPLETE"


def test_html_table_structure_is_attached_to_chunking_input() -> None:
    markdown = """<!-- ODL_PAGE:1 -->
<table>
<tr><th rowspan="2">类别</th><th colspan="2">排放量</th></tr>
<tr><th>2024</th><th>2025</th></tr>
<tr><td>燃烧</td><td>10</td><td>9</td></tr>
</table>
"""
    parse_result = MarkdownParser().parse(
        SimpleDocumentIngestionService._pdf_markdown_for_indexing(markdown),
        source_file="report.pdf",
    )
    table_report = PdfTableStructureExtractor().extract(markdown)
    service = SimpleDocumentIngestionService(
        storage=_FakeStorage(),
        qdrant_store=_FakeQdrantStore(),
        bm25_pipeline=_FakeBm25Pipeline(),
    )

    service._apply_pdf_structured_assets(
        parse_result,
        table_structure_report=table_report,
        image_policy_report=SimpleNamespace(assets=[]),
    )

    table = next(
        element for element in parse_result.elements if element.type is ElementType.TABLE
    )
    structure = table.metadata["table_structure"]
    assert structure["source_pages"] == [1]
    assert structure["cells"][0]["row_span"] == 2
    assert structure["cells"][1]["column_span"] == 2
    assert structure["text_matrix"][1] == ["类别", "2024", "2025"]


def test_mixed_table_structure_binds_by_exact_source_line_not_format_order() -> None:
    markdown = """<!-- ODL_PAGE:1 -->
| 类型 | 数值 |
| --- | --- |
| 电力 | 0.5 |

<table><tr><th>年份</th><th>用量</th></tr><tr><td>2025</td><td>10</td></tr></table>
"""
    parse_result = MarkdownParser().parse(
        SimpleDocumentIngestionService._pdf_markdown_for_indexing(markdown),
        source_file="mixed.pdf",
    )
    SimpleDocumentIngestionService._apply_pdf_page_numbers_from_markdown(
        parse_result,
        markdown,
    )
    table_report = PdfTableStructureExtractor().extract(markdown)
    service = SimpleDocumentIngestionService(
        storage=_FakeStorage(),
        qdrant_store=_FakeQdrantStore(),
        bm25_pipeline=_FakeBm25Pipeline(),
    )

    service._apply_pdf_structured_assets(
        parse_result,
        table_structure_report=table_report,
        image_policy_report=SimpleNamespace(assets=[]),
    )

    tables = [
        element for element in parse_result.elements if element.type is ElementType.TABLE
    ]
    assert tables[0].metadata["table_structure"]["source_format"] == "MARKDOWN"
    assert tables[0].metadata["table_structure"]["text_matrix"][1] == ["电力", "0.5"]
    assert tables[1].metadata["table_structure"]["source_format"] == "HTML"
    assert tables[1].metadata["table_structure"]["text_matrix"][1] == ["2025", "10"]


def test_page_visual_description_is_indexed_once_and_not_cloned_to_images() -> None:
    markdown = """<!-- ODL_PAGE:5 -->
![企业 logo](images/logo.png)
![年度排放趋势图](images/chart.png)

<!-- PAGE_FALLBACK:VISION -->

图片说明：排放量从 120 tCO2e 降至 100 tCO2e。
"""
    parse_result = MarkdownParser().parse(
        SimpleDocumentIngestionService._pdf_markdown_for_indexing(markdown),
        source_file="visual.pdf",
    )
    SimpleDocumentIngestionService._apply_pdf_page_numbers_from_markdown(
        parse_result,
        markdown,
    )
    description = StructuredVisualDescription(
        source_page=5,
        summary="年度排放量从 120 tCO2e 降至 100 tCO2e。",
        entities=("2024 年排放量", "2025 年排放量"),
        key_values=(("2024", "120 tCO2e"), ("2025", "100 tCO2e")),
        units=("tCO2e",),
    )
    policy = PdfImageAssetPolicy()
    image_report = policy.extract_and_classify(markdown)
    service = SimpleDocumentIngestionService(
        storage=_FakeStorage(),
        qdrant_store=_FakeQdrantStore(),
        bm25_pipeline=_FakeBm25Pipeline(),
        pdf_image_asset_policy=policy,
    )

    service._apply_pdf_page_visual_descriptions(parse_result, {5: description})
    service._apply_pdf_structured_assets(
        parse_result,
        table_structure_report=PdfTableStructureExtractor().extract(markdown),
        image_policy_report=image_report,
    )

    page_visuals = [
        element
        for element in parse_result.elements
        if element.metadata.get("page_visual_description") is not None
    ]
    image_elements = [
        element for element in parse_result.elements if element.type is ElementType.IMAGE
    ]
    assert len(page_visuals) == 1
    assert sum("2024=120 tCO2e" in element.content for element in page_visuals) == 1
    assert all("visual_description" not in element.metadata for element in image_elements)
    assert all(element.metadata.get("suppress_retrieval") is True for element in image_elements)
    assert all(
        not asset["retrieval_asset"]
        for element in image_elements
        for asset in element.metadata["image_assets"]
    )


def test_repeated_image_source_binds_exact_decision_to_each_page_owner() -> None:
    markdown = (
        "<!-- ODL_PAGE:1 -->\n![排放趋势图](images/shared.png)\n"
        "<!-- ODL_PAGE:2 -->\n![排放趋势图](images/shared.png)"
    )
    policy = PdfImageAssetPolicy()
    preliminary = policy.extract_and_classify(markdown)
    image_report = policy.extract_and_classify(
        markdown,
        visual_descriptions={
            preliminary.assets[0].asset_key: StructuredVisualDescription(
                source_page=1,
                summary="2024 年排放量为 100 tCO2e，作为对比基准。",
                key_values=(("2024", "100 tCO2e"),),
            ),
            preliminary.assets[1].asset_key: StructuredVisualDescription(
                source_page=2,
                summary="2025 年排放量为 90 tCO2e，较上年下降。",
                key_values=(("2025", "90 tCO2e"),),
            ),
        },
    )
    parse_result = MarkdownParser().parse(
        SimpleDocumentIngestionService._pdf_markdown_for_indexing(markdown),
        source_file="repeated.pdf",
    )
    SimpleDocumentIngestionService._apply_pdf_page_numbers_from_markdown(
        parse_result,
        markdown,
    )
    service = SimpleDocumentIngestionService(
        storage=_FakeStorage(),
        qdrant_store=_FakeQdrantStore(),
        bm25_pipeline=_FakeBm25Pipeline(),
        pdf_image_asset_policy=policy,
    )

    service._apply_pdf_structured_assets(
        parse_result,
        table_structure_report=PdfTableStructureExtractor().extract(markdown),
        image_policy_report=image_report,
    )

    image_elements = [
        element for element in parse_result.elements if element.type is ElementType.IMAGE
    ]
    assert [element.metadata["image_asset"]["page_number"] for element in image_elements] == [
        1,
        2,
    ]
    assert "2024=100 tCO2e" in image_elements[0].metadata["visual_description"]
    assert "2025=90 tCO2e" in image_elements[1].metadata["visual_description"]


def test_provider_asset_description_requires_explicit_occurrence_mapping() -> None:
    markdown = (
        "<!-- ODL_PAGE:1 -->\n![排放趋势图](images/shared.png)\n"
        "![排放趋势图](images/shared.png)"
    )
    policy = PdfImageAssetPolicy()
    preliminary = policy.extract_and_classify(markdown)
    selected = preliminary.assets[1]
    fallback = SimpleNamespace(
        results=[
            SimpleNamespace(
                page_number=1,
                structured_data={
                    "summary": "这是整页说明，不能绑定单图。",
                    "visual_assets": [
                        {
                            "asset_key": selected.asset_key,
                            "description": {
                                "summary": "2025 年排放量为 90 tCO2e，较上年下降。",
                                "key_values": [
                                    {"label": "2025", "value": "90 tCO2e"}
                                ],
                            },
                        }
                    ],
                },
            )
        ]
    )
    service = SimpleDocumentIngestionService(
        storage=_FakeStorage(),
        qdrant_store=_FakeQdrantStore(),
        bm25_pipeline=_FakeBm25Pipeline(),
        pdf_image_asset_policy=policy,
    )

    descriptions = service._explicit_asset_visual_descriptions(fallback, preliminary)

    assert list(descriptions) == [selected.asset_key]
    assert descriptions[selected.asset_key].source_page == 1
    report = policy.extract_and_classify(markdown, visual_descriptions=descriptions)
    assert report.assets[0].retrieval_asset is False
    assert report.assets[1].retrieval_asset is True


def test_inline_duplicate_source_filters_only_the_matching_occurrence() -> None:
    markdown = (
        "<!-- ODL_PAGE:1 -->\n"
        "附件 ![设备布置](images/shared.png) "
        "与 ![排放趋势图](images/shared.png)"
    )
    policy = PdfImageAssetPolicy()
    preliminary = policy.extract_and_classify(markdown)
    report = policy.extract_and_classify(
        markdown,
        visual_descriptions={
            preliminary.assets[1].asset_key: StructuredVisualDescription(
                source_page=1,
                summary="2025 年排放量为 90 tCO2e，较上年下降。",
                key_values=(("2025", "90 tCO2e"),),
            )
        },
    )
    parse_result = MarkdownParser().parse(
        SimpleDocumentIngestionService._pdf_markdown_for_indexing(markdown),
        source_file="inline.pdf",
    )
    SimpleDocumentIngestionService._apply_pdf_page_numbers_from_markdown(
        parse_result,
        markdown,
    )
    service = SimpleDocumentIngestionService(
        storage=_FakeStorage(),
        qdrant_store=_FakeQdrantStore(),
        bm25_pipeline=_FakeBm25Pipeline(),
        pdf_image_asset_policy=policy,
    )

    service._apply_pdf_structured_assets(
        parse_result,
        table_structure_report=PdfTableStructureExtractor().extract(markdown),
        image_policy_report=report,
    )

    owner = next(
        element for element in parse_result.elements if element.type is ElementType.PARAGRAPH
    )
    assert owner.content.count("images/shared.png") == 1
    assert "![设备布置]" not in owner.content
    assert "![排放趋势图]" in owner.content
    assert "2025=90 tCO2e" in owner.content
    assert [item["occurrence_index"] for item in owner.metadata["image_assets"]] == [1, 2]


def test_repeated_page_headers_and_explicit_footer_noise_are_not_retrieved() -> None:
    markdown = """<!-- ODL_PAGE:1 -->
公开属性：主动公开

第一页碳核算正文内容。

第1页 共3页
<!-- ODL_PAGE:2 -->
公开属性：主动公开

第二页排放因子正文内容。

第2页 共3页
<!-- ODL_PAGE:3 -->
公开属性：主动公开

第三页核查要求正文内容。

第3页 共3页
"""
    parse_result = MarkdownParser().parse(
        SimpleDocumentIngestionService._pdf_markdown_for_indexing(markdown),
        source_file="report.pdf",
    )
    marker_count = SimpleDocumentIngestionService._apply_pdf_page_numbers_from_markdown(
        parse_result,
        markdown,
    )

    report = SimpleDocumentIngestionService._apply_pdf_retrieval_noise_policy(parse_result)

    suppressed = [
        element
        for element in parse_result.elements
        if element.metadata.get("suppress_retrieval") is True
    ]
    retained = [
        element.content
        for element in parse_result.elements
        if element.metadata.get("suppress_retrieval") is not True
    ]
    assert marker_count == 3
    assert report["suppressed_element_count"] == 6
    assert all("公开属性" in item.content or "共3页" in item.content for item in suppressed)
    assert any("碳核算正文" in item for item in retained)
    assert any("排放因子正文" in item for item in retained)
    assert all("ODL_PAGE" not in element.content for element in parse_result.elements)
