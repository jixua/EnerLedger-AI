"""RabbitMQ-driven report worker with a replaceable Pi processor boundary."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
import socket
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import Document, ReportQuestion, ReportRun
from app.domain.time import utc_now
from app.rag.config import settings
from app.rag.core.llm.encryption import decrypt_api_key
from app.rag.core.mq.messages import ReportGenerationMessage, ReportGenerationPayload
from app.rag.database import close_database, get_db_context, init_database
from app.rag.models.db_models import LLMModelConfigDB
from app.rag.observability.logging import logger, safe_exception_stack, setup_logger
from app.rag.services.mq_service import MQService
from app.services.document_queue import DOCUMENT_STATUS_READY
from app.services.report_agent_tokens import issue_report_agent_token
from app.services.report_budget import (
    ReportDocumentTooLargeError,
    assert_document_fits_context,
    load_document_payload_stats,
    report_context_budget,
)
from app.services.report_ir import build_fixture_report_ir, validate_report_ir
from app.services.report_queue import ReportRunClaim, ReportRunQueueService
from app.services.report_source_context import (
    load_document_chunk_manifest,
    load_report_source_context,
    validate_chunk_coverage,
)
from app.services.report_templates import ReportTemplate

REPORT_GENERATION_GROUP = "energy-carbon-report-generator"


@dataclass(frozen=True, slots=True)
class ReportProcessingResult:
    state: str
    stage: str
    report_ir: dict[str, Any] | None
    validation_report: dict[str, Any]
    manifest: dict[str, Any]
    analysis_coverage: dict[str, Any] | None = None


class ReportProcessor(Protocol):
    async def process(self, db: AsyncSession, *, run: ReportRun) -> ReportProcessingResult: ...


class ReportProcessorUnavailable(RuntimeError):
    retryable = False
    error_code = "PI_AGENT_NOT_CONFIGURED"


class DisabledReportProcessor:
    async def process(self, _db: AsyncSession, *, run: ReportRun) -> ReportProcessingResult:
        raise ReportProcessorUnavailable(f"报告任务 {run.id} 已进入 Worker，但 Pi Agent 尚未配置")


class FixtureReportProcessor:
    """Explicit test/development processor; never claims to analyze source content."""

    async def process(self, db: AsyncSession, *, run: ReportRun) -> ReportProcessingResult:
        template = ReportTemplate.from_snapshot(run.template_snapshot)
        if template.template_id != run.template_id or template.version != run.template_version:
            raise ValueError("任务冻结模板与当前模板资产不一致")
        questions = (
            await db.scalars(
                select(ReportQuestion)
                .where(ReportQuestion.run_id == run.id)
                .order_by(ReportQuestion.id)
            )
        ).all()
        answered_fields = {
            question.field_id for question in questions if question.status == "ANSWERED"
        }
        existing_fields = {question.field_id for question in questions}
        for field in template.definition.get("fields") or []:
            if not field.get("blocking") or field["id"] in answered_fields:
                continue
            if field["id"] not in existing_fields:
                enum_values = (field.get("validation") or {}).get("enum") or []
                db.add(
                    ReportQuestion(
                        run_id=run.id,
                        field_id=field["id"],
                        question_type="MISSING_FIELD",
                        question=f"请补充{field['label']}，缺少该字段无法生成完整报告。",
                        options=[{"label": value, "value": value} for value in enum_values] or None,
                        required=True,
                        status="OPEN",
                    )
                )
        await db.flush()
        questions = (
            await db.scalars(
                select(ReportQuestion)
                .where(ReportQuestion.run_id == run.id)
                .order_by(ReportQuestion.id)
            )
        ).all()
        report_ir = build_fixture_report_ir(
            run=run,
            template=template,
            answered_questions=list(questions),
        )
        validation = validate_report_ir(report_ir, run=run, template=template)
        if not validation.schema_valid:
            raise ValueError("Fixture ReportIR 未通过结构校验")
        state = "SUCCEEDED" if validation.publishable else "NEEDS_INPUT"
        stage = "COMPLETED" if state == "SUCCEEDED" else "WAITING_FOR_INPUT"
        encoded = json.dumps(
            report_ir, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        manifest = {
            "run_id": str(run.id),
            "report_ir_sha256": hashlib.sha256(encoded).hexdigest(),
            "template_id": run.template_id,
            "template_version": run.template_version,
            "document_version": int(run.document_version),
            "generator": "platform-fixture",
        }
        return ReportProcessingResult(
            state=state,
            stage=stage,
            report_ir=report_ir,
            validation_report=validation.to_dict(),
            manifest=manifest,
            analysis_coverage=None,
        )


class PiReportProcessorError(RuntimeError):
    error_code = "PI_AGENT_REQUEST_FAILED"

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


class PiReportProcessor:
    """Bridge one fenced ReportRun to the independent Pi service."""

    async def process(self, db: AsyncSession, *, run: ReportRun) -> ReportProcessingResult:
        _context, frozen_manifest = await load_report_source_context(db, run=run)
        expected_manifest = run.document_manifest or {}
        if frozen_manifest.content_hash != expected_manifest.get("chunk_manifest_sha256") or len(
            frozen_manifest.items
        ) != expected_manifest.get("chunk_count"):
            error = PiReportProcessorError(
                "冻结文档的分片集合已经变化，请重新创建报告任务",
                retryable=False,
            )
            error.error_code = "REPORT_DOCUMENT_MANIFEST_CHANGED"
            raise error
        if run.custom_template_document_id:
            custom_document = await db.scalar(
                select(Document).where(
                    Document.id == run.custom_template_document_id,
                    Document.user_id == run.user_id,
                )
            )
            expected_custom = run.custom_template_manifest or {}
            if (
                custom_document is None
                or int(custom_document.dataset_id) != int(run.dataset_id)
                or int(custom_document.version) != int(run.custom_template_document_version or 0)
            ):
                error = PiReportProcessorError("冻结的用户模板已经变化", retryable=False)
                error.error_code = "REPORT_CUSTOM_TEMPLATE_CHANGED"
                raise error
            custom_manifest = await load_document_chunk_manifest(db, document=custom_document)
            if (
                custom_manifest.content_hash != expected_custom.get("chunk_manifest_sha256")
                or len(custom_manifest.items) != expected_custom.get("chunk_count")
            ):
                error = PiReportProcessorError("冻结的用户模板分片已经变化", retryable=False)
                error.error_code = "REPORT_CUSTOM_TEMPLATE_CHANGED"
                raise error
        # 与创建任务时同一套上下文预算：重试、历史任务等任何执行路径都不允许在
        # 「一轮读不完」的情况下调用模型（必然以输出截断告终）。
        source_chunks, source_chars = await load_document_payload_stats(
            db, document_id=int(run.document_id), document_version=int(run.document_version)
        )
        custom_chunks = custom_chars = 0
        if run.custom_template_document_id:
            custom_chunks, custom_chars = await load_document_payload_stats(
                db,
                document_id=int(run.custom_template_document_id),
                document_version=int(run.custom_template_document_version or 0),
            )
        try:
            estimated_tokens = assert_document_fits_context(
                content_chars=source_chars + custom_chars,
                chunk_count=source_chunks + custom_chunks,
            )
        except ReportDocumentTooLargeError as exc:
            error = PiReportProcessorError(str(exc), retryable=False)
            error.error_code = exc.code
            raise error from exc
        budget = report_context_budget()
        logger.info(
            "报告任务上下文预算检查通过 run_id={} 估算tokens={} 分片数={} "
            "模型窗口={} 最大输出={} 预留={} prompt预算={}",
            run.id,
            estimated_tokens,
            source_chunks + custom_chunks,
            budget.window_tokens,
            budget.max_output_tokens,
            budget.reserve_tokens,
            budget.prompt_tokens,
        )
        model = await db.scalar(
            select(LLMModelConfigDB).where(LLMModelConfigDB.id == run.llm_config_id)
        )
        model_snapshot = run.model_snapshot or {}
        current_snapshot = (
            {
                "llm_config_id": int(model.id),
                "snapshot_version": int(model.snapshot_version),
                "scope": model.scope,
                "owner_user_id": model.owner_user_id,
                "provider_type": model.provider_type,
                "model_name": model.model_name,
                "display_name": model.display_name,
                "capability": model.capability,
                "protocol": model.protocol,
                "api_base_url": model.api_base_url,
                "supports_tool_calling": bool(model.supports_tool_calling),
            }
            if model is not None
            else None
        )
        if (
            model is None
            or not model.is_active
            or model.capability.upper() != "CHAT"
            or not model.supports_tool_calling
            or int(model.snapshot_version) != int(run.llm_snapshot_version)
            or (model.scope == "USER" and int(model.owner_user_id) != int(run.user_id))
            or current_snapshot != model_snapshot
        ):
            raise PiReportProcessorError("任务冻结模型已停用、变更或失去授权", retryable=False)
        if not run.lease_token:
            raise PiReportProcessorError("报告运行租约不存在", retryable=False)
        run_token = issue_report_agent_token(
            run_id=str(run.id),
            lease_token=run.lease_token,
            secret=settings.REPORT_AGENT_RUN_TOKEN_SECRET,
            ttl_seconds=settings.REPORT_AGENT_RUN_TOKEN_TTL_SECONDS,
        )
        request = {
            "runId": str(run.id),
            "runToken": run_token,
            "model": {
                "id": model_snapshot["model_name"],
                "name": model_snapshot.get("display_name") or model_snapshot["model_name"],
                "protocol": model_snapshot["protocol"],
                "baseUrl": model_snapshot["api_base_url"],
                "apiKey": decrypt_api_key(model.api_key),
                # 报告 Agent 一次要产出的 ReportIR 远大于对话回复，必须显式下发输出预算，
                # 否则 pi 侧会退回 8192 默认值并把输出截断（stopReason=length）。
                "maxTokens": settings.REPORT_AGENT_MODEL_MAX_OUTPUT_TOKENS,
                "contextWindow": settings.REPORT_AGENT_MODEL_CONTEXT_WINDOW,
            },
        }
        await db.rollback()
        try:
            async with httpx.AsyncClient(
                timeout=settings.REPORT_AGENT_RUN_TIMEOUT_SECONDS
            ) as client:
                response = await client.post(
                    f"{settings.PI_SERVICE_URL.rstrip('/')}/internal/report-agent/runs",
                    headers={"Authorization": f"Bearer {settings.PI_SERVICE_TOKEN}"},
                    json=request,
                )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise PiReportProcessorError("Pi Agent 服务暂时不可用") from exc
        # 前面的 rollback 已使 run 实例过期；pi 调用（可能数分钟）结束后先异步刷新，
        # 避免随后读取属性/传入校验触发同步懒加载（MissingGreenlet）。
        await db.refresh(run)
        if response.status_code != 200:
            try:
                code = str(response.json().get("error") or "PI_AGENT_REQUEST_FAILED")
            except Exception:
                code = "PI_AGENT_REQUEST_FAILED"
            error = PiReportProcessorError(
                code,
                retryable=response.status_code >= 500,
            )
            error.error_code = code[:64]
            raise error
        result = response.json()
        outcome = result.get("outcome")
        if outcome == "NEEDS_INPUT":
            return ReportProcessingResult(
                state="NEEDS_INPUT",
                stage="WAITING_FOR_INPUT",
                report_ir=None,
                validation_report={
                    "schema_valid": False,
                    "publishable": False,
                    "errors": [],
                    "warnings": ["等待用户补充阻塞字段。"],
                },
                manifest={
                    "run_id": str(run.id),
                    "generator": "pi-agent",
                    "outcome": "NEEDS_INPUT",
                    "tool_calls": result.get("toolCalls"),
                },
                analysis_coverage=None,
            )
        if outcome != "SUBMITTED" or not isinstance(result.get("reportIr"), dict):
            raise PiReportProcessorError("Pi Agent 未提交有效 ReportIR", retryable=False)
        template = ReportTemplate.from_snapshot(run.template_snapshot)
        evidence_context, chunk_manifest = await load_report_source_context(db, run=run)
        validation = validate_report_ir(
            result["reportIr"],
            run=run,
            template=template,
            evidence_context=evidence_context,
        )
        coverage_errors = validate_chunk_coverage(result.get("coverage") or {}, chunk_manifest)
        if not validation.schema_valid or not validation.publishable or coverage_errors:
            raise PiReportProcessorError(
                "Pi Agent 返回值未通过 FastAPI 权威校验",
                retryable=False,
            )
        encoded = json.dumps(
            result["reportIr"], ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        manifest = {
            "run_id": str(run.id),
            "report_ir_sha256": hashlib.sha256(encoded).hexdigest(),
            "template_id": run.template_id,
            "template_version": run.template_version,
            "document_version": int(run.document_version),
            "chunk_manifest_sha256": chunk_manifest.content_hash,
            "chunk_count": len(chunk_manifest.items),
            "generator": "pi-agent",
            "validated_by": "fastapi-worker",
        }
        return ReportProcessingResult(
            state="SUCCEEDED",
            stage="COMPLETED",
            report_ir=result["reportIr"],
            validation_report=validation.to_dict(),
            manifest=manifest,
            analysis_coverage=result.get("coverage"),
        )


def configured_processor() -> ReportProcessor:
    mode = settings.REPORT_PROCESSOR_MODE.strip().lower()
    if mode == "fixture":
        return FixtureReportProcessor()
    if mode == "pi":
        return PiReportProcessor()
    return DisabledReportProcessor()


class ReportGenerationWorker:
    def __init__(
        self,
        *,
        processor: ReportProcessor | None = None,
        queue: ReportRunQueueService | None = None,
        mq_service: MQService | None = None,
        worker_id: str | None = None,
    ) -> None:
        self.processor = processor or configured_processor()
        self.queue = queue or ReportRunQueueService()
        self.mq_service = mq_service or MQService()
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}"
        self.stop_event = asyncio.Event()

    async def run(self) -> None:
        await self.mq_service.subscribe(
            ReportGenerationMessage.MQ_NAME,
            REPORT_GENERATION_GROUP,
            self._handle_message,
        )
        await self.mq_service.start_consuming()
        try:
            await self.stop_event.wait()
        finally:
            await self.mq_service.stop_consuming()

    def stop(self) -> None:
        self.stop_event.set()

    async def _handle_message(self, body: str, _metadata: dict[str, Any]) -> None:
        payload = ReportGenerationMessage.parse_msg(body)
        await self.process_payload(payload)

    async def process_payload(self, payload: ReportGenerationPayload) -> None:
        async with get_db_context() as db:
            run = await db.scalar(select(ReportRun).where(ReportRun.id == payload.run_id))
            if run is None:
                return
            if (
                int(run.user_id) != payload.user_id
                or int(run.document_id) != payload.document_id
                or int(run.document_version) != payload.document_version
            ):
                raise ValueError("报告消息与冻结任务身份不一致")
            document = await db.scalar(
                select(Document).where(
                    Document.id == run.document_id,
                    Document.user_id == run.user_id,
                )
            )
            if (
                document is None
                or document.status != DOCUMENT_STATUS_READY
                or int(document.version) != int(run.document_version)
                or document.parsed_bucket != run.parsed_bucket
                or document.parsed_object_key != run.parsed_object_key
            ):
                await db.execute(
                    update(ReportRun)
                    .where(ReportRun.id == run.id)
                    .values(
                        state="STALE_DOCUMENT",
                        stage="FAILED",
                        error_code="REPORT_DOCUMENT_VERSION_CHANGED",
                        error_message="文档版本或解析产物已变化，请重新创建报告",
                        finished_at=utc_now(),
                        lease_token=None,
                        lease_owner=None,
                        lease_expires_at=None,
                    )
                    .execution_options(synchronize_session=False)
                )
                await db.commit()
                return

        async with get_db_context() as db:
            claim = await self.queue.claim(
                db,
                run_id=payload.run_id,
                worker_id=self.worker_id,
            )
        if claim is None:
            return
        await self._process_claim(claim)

    async def _process_claim(self, claim: ReportRunClaim) -> None:
        heartbeat_stop = asyncio.Event()
        heartbeat = asyncio.create_task(
            self._heartbeat(claim, heartbeat_stop),
            name=f"report-lease-heartbeat-{claim.run_id}",
        )
        try:
            async with get_db_context() as db:
                run = await db.scalar(
                    select(ReportRun).where(
                        ReportRun.id == claim.run_id,
                        ReportRun.state == "PROCESSING",
                        ReportRun.lease_token == claim.lease_token,
                    )
                )
                if run is None:
                    return
                result = await self.processor.process(db, run=run)
                completed = await self.queue.complete(
                    db,
                    claim,
                    state=result.state,
                    stage=result.stage,
                    report_ir=result.report_ir,
                    validation_report=result.validation_report,
                    manifest=result.manifest,
                    analysis_coverage=result.analysis_coverage,
                )
                if not completed:
                    logger.bind(event="report_lease_lost", run_id=claim.run_id).warning(
                        "报告 Worker 失去租约，放弃写入结果"
                    )
        except Exception as exc:
            retryable = bool(getattr(exc, "retryable", True))
            error_code = str(getattr(exc, "error_code", "REPORT_PROCESSING_FAILED"))
            async with get_db_context() as db:
                await self.queue.fail(
                    db,
                    claim,
                    exc,
                    retryable=retryable,
                    error_code=error_code,
                )
            logger.bind(
                event="report_processing_failed",
                run_id=claim.run_id,
                error_type=type(exc).__name__,
                error_message=str(exc)[:500],
                stack_trace=safe_exception_stack(exc),
            ).error("报告 Worker 处理失败")
        finally:
            heartbeat_stop.set()
            await heartbeat

    async def _heartbeat(self, claim: ReportRunClaim, stop_event: asyncio.Event) -> None:
        interval = settings.REPORT_QUEUE_HEARTBEAT_SECONDS
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
                return
            except TimeoutError:
                pass
            async with get_db_context() as db:
                if not await self.queue.renew(db, claim):
                    logger.bind(event="report_lease_renew_failed", run_id=claim.run_id).warning(
                        "报告 Worker 租约续期失败"
                    )
                    return


async def _run_worker() -> None:
    setup_logger()
    await init_database()
    worker = ReportGenerationWorker()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, worker.stop)
    try:
        await worker.run()
    finally:
        await close_database()


if __name__ == "__main__":
    asyncio.run(_run_worker())
