"""与页面请求生命周期解耦的企业文档分析运行管理。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import select

from app.domain.models import Dataset, Document
from app.rag.core.llm.exceptions import (
    LLMConfigResolutionError,
    ProviderException,
    public_llm_error,
)
from app.rag.database import get_db_context
from app.rag.observability.logging import logger, safe_exception_stack, truncate_log_value
from app.services.document_analysis import (
    DocumentAnalysisError,
    DocumentAnalysisService,
    DocumentAnalysisStore,
    DocumentAnalysisVersionChangedError,
)
from app.services.document_queue import DOCUMENT_STATUS_READY

RUN_STATE_IDLE = "IDLE"
RUN_STATE_PENDING = "PENDING"
RUN_STATE_RUNNING = "RUNNING"
RUN_STATE_SUCCEEDED = "SUCCEEDED"
RUN_STATE_FAILED = "FAILED"
ACTIVE_RUN_STATES = frozenset({RUN_STATE_PENDING, RUN_STATE_RUNNING})


@dataclass
class DocumentAnalysisRun:
    run_id: str | None
    document_id: int
    dataset_id: int
    user_id: int
    document_version: int
    state: str
    stage: str
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error_code: str | None = None
    error_message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "document_id": self.document_id,
            "dataset_id": self.dataset_id,
            "document_version": self.document_version,
            "state": self.state,
            "stage": self.stage,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error_code": self.error_code,
            "error_message": self.error_message,
        }


class DocumentAnalysisRunManager:
    """在单个 API 进程内持有后台任务，使浏览器断开不会取消分析。"""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._runs: dict[tuple[int, int, int], DocumentAnalysisRun] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    @staticmethod
    def _key(document: Document) -> tuple[int, int, int]:
        return int(document.user_id), int(document.id), int(document.version)

    async def start(
        self,
        *,
        document: Document,
        llm_config_id: int | None,
    ) -> DocumentAnalysisRun:
        key = self._key(document)
        async with self._lock:
            existing = self._runs.get(key)
            if existing is not None and existing.state in ACTIVE_RUN_STATES:
                return existing
            run = DocumentAnalysisRun(
                run_id=uuid4().hex,
                document_id=int(document.id),
                dataset_id=int(document.dataset_id),
                user_id=int(document.user_id),
                document_version=int(document.version),
                state=RUN_STATE_PENDING,
                stage="WAITING",
            )
            self._runs[key] = run
            task = asyncio.create_task(
                self._execute(run=run, llm_config_id=llm_config_id),
                name=f"document-analysis-{run.document_id}-v{run.document_version}",
            )
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            return run

    async def get_status(self, *, document: Document) -> DocumentAnalysisRun:
        async with self._lock:
            run = self._runs.get(self._key(document))
            if run is not None:
                return run
        return DocumentAnalysisRun(
            run_id=None,
            document_id=int(document.id),
            dataset_id=int(document.dataset_id),
            user_id=int(document.user_id),
            document_version=int(document.version),
            state=RUN_STATE_IDLE,
            stage="IDLE",
        )

    async def _execute(self, *, run: DocumentAnalysisRun, llm_config_id: int | None) -> None:
        run.state = RUN_STATE_RUNNING
        run.stage = "ANALYZING"
        run.started_at = datetime.now(UTC)
        try:
            async with get_db_context() as db:
                document = await db.scalar(
                    select(Document).where(
                        Document.id == run.document_id,
                        Document.user_id == run.user_id,
                    )
                )
                if (
                    document is None
                    or document.status != DOCUMENT_STATUS_READY
                    or int(document.version) != run.document_version
                ):
                    raise DocumentAnalysisVersionChangedError(
                        "分析开始时文档版本或状态已经变化，请重新生成"
                    )
                dataset = await db.scalar(
                    select(Dataset).where(
                        Dataset.id == document.dataset_id,
                        Dataset.user_id == run.user_id,
                        Dataset.status == "ACTIVE",
                    )
                )
                if dataset is None:
                    raise DocumentAnalysisError("文档所属数据集不存在或不可用")

                analyzed_bucket = document.parsed_bucket
                analyzed_object_key = document.parsed_object_key
                result = await DocumentAnalysisService().analyze(
                    db=db,
                    document=document,
                    dataset=dataset,
                    user_id=run.user_id,
                    llm_config_id=llm_config_id,
                )
                run.stage = "SAVING"
                current_document = await db.scalar(
                    select(Document)
                    .where(Document.id == run.document_id, Document.user_id == run.user_id)
                    .execution_options(populate_existing=True)
                )
                if (
                    current_document is None
                    or current_document.status != DOCUMENT_STATUS_READY
                    or int(current_document.version) != run.document_version
                    or current_document.parsed_bucket != analyzed_bucket
                    or current_document.parsed_object_key != analyzed_object_key
                ):
                    raise DocumentAnalysisVersionChangedError(
                        "分析期间文档版本或解析产物已变化，请重新生成"
                    )
                await DocumentAnalysisStore().save(document=current_document, result=result)
            run.state = RUN_STATE_SUCCEEDED
            run.stage = "COMPLETED"
        except asyncio.CancelledError:
            run.state = RUN_STATE_FAILED
            run.stage = "INTERRUPTED"
            run.error_code = "DOCUMENT_ANALYSIS_INTERRUPTED"
            run.error_message = "分析服务已停止，请重新生成"
            raise
        except (DocumentAnalysisError, LLMConfigResolutionError) as exc:
            run.state = RUN_STATE_FAILED
            run.stage = "FAILED"
            run.error_code = getattr(exc, "code", "DOCUMENT_ANALYSIS_FAILED")
            run.error_message = str(exc)
        except ProviderException as exc:
            failure = public_llm_error(exc)
            run.state = RUN_STATE_FAILED
            run.stage = "FAILED"
            run.error_code = failure.code
            run.error_message = failure.message
            logger.bind(
                event="document_analysis_provider_failed",
                document_id=run.document_id,
                dataset_id=run.dataset_id,
                provider_type=getattr(exc, "provider_type", "") or "",
                public_error_code=failure.code,
                retryable=failure.retryable,
                error_type=type(exc).__name__,
                error_message=truncate_log_value(exc),
            ).warning("后台企业文档分析的模型调用失败")
        except TimeoutError:
            run.state = RUN_STATE_FAILED
            run.stage = "FAILED"
            run.error_code = "DOCUMENT_ANALYSIS_TIMEOUT"
            run.error_message = "大模型分析超时，请重试"
        except Exception as exc:  # noqa: BLE001 - 后台任务必须收敛为可查询状态
            run.state = RUN_STATE_FAILED
            run.stage = "FAILED"
            run.error_code = "DOCUMENT_ANALYSIS_FAILED"
            run.error_message = "大模型分析失败，请稍后重试"
            logger.bind(
                event="document_analysis_background_failed",
                document_id=run.document_id,
                dataset_id=run.dataset_id,
                error_type=type(exc).__name__,
                error_message=truncate_log_value(exc),
                stack_trace=safe_exception_stack(exc),
            ).exception("后台企业文档分析失败")
        finally:
            run.finished_at = datetime.now(UTC)


document_analysis_run_manager = DocumentAnalysisRunManager()
