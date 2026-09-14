"""基于文档全部主体分片的大模型 Markdown 分析服务。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

from botocore.exceptions import ClientError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import Dataset, Document
from app.rag.config import settings
from app.rag.core.llm.provider_lifecycle import aclose_resolved_models
from app.rag.core.llm.response import UsageInfo
from app.rag.core.llm.tokenizer import Tokenizer
from app.rag.core.llm.user_model_resolver import aresolve_model
from app.rag.core.prompts import (
    DOCUMENT_ANALYSIS_SYSTEM_PROMPT,
    DOCUMENT_EVIDENCE_SYSTEM_PROMPT,
    build_document_analysis_prompt,
    build_document_analysis_repair_prompt,
    build_document_evidence_prompt,
    build_document_evidence_repair_prompt,
)
from app.rag.models.chunk_record import ChunkRecordDB
from app.rag.services.storage.base import BaseObjectStorage
from app.rag.services.storage.factory import StorageFactory

_CITATION_PATTERN = re.compile(r"\[文档片段(?P<index>\d+)\]")
_OUTER_MARKDOWN_FENCE = re.compile(
    r"\A\s*```(?:markdown|md)?\s*\n(?P<body>.*)\n```\s*\Z",
    flags=re.IGNORECASE | re.DOTALL,
)
_OUTER_JSON_FENCE = re.compile(
    r"\A\s*```(?:json)?\s*\n(?P<body>.*)\n```\s*\Z",
    flags=re.IGNORECASE | re.DOTALL,
)
_THINK_BLOCK = re.compile(r"<think\b[^>]*>.*?</think\s*>", flags=re.IGNORECASE | re.DOTALL)
_LENGTH_FINISH_REASONS = frozenset({"length", "max_tokens", "max_output_tokens"})
_REQUIRED_HEADINGS = (
    "## 1. 报告摘要",
    "## 2. 评价对象和目标",
    "## 3. 评价方法和工具",
    "## 4. 评价边界界定",
    "## 5. 功能单位或核算口径",
    "## 6. 生命周期与活动数据清单分析",
    "## 7. 碳足迹核算及评价",
    "## 8. 量化数据质量与可靠性",
    "## 9. 不确定性分析",
    "## 10. 文档规范性检查",
    "## 11. 资料缺口与整改建议",
    "## 12. 结论与分析限制",
)


class DocumentAnalysisError(Exception):
    """企业文档分析业务异常。"""

    code = "DOCUMENT_ANALYSIS_FAILED"
    http_status = 502


class DocumentAnalysisEmptyError(DocumentAnalysisError):
    code = "DOCUMENT_ANALYSIS_EMPTY"
    http_status = 409


class DocumentAnalysisTooLargeError(DocumentAnalysisError):
    code = "DOCUMENT_ANALYSIS_TOO_LARGE"
    http_status = 413

    def __init__(self, *, token_count: int, token_limit: int) -> None:
        self.token_count = token_count
        self.token_limit = token_limit
        super().__init__(f"文档约 {token_count} tokens，超过当前分析上限 {token_limit} tokens")


class DocumentAnalysisInvalidOutputError(DocumentAnalysisError):
    code = "DOCUMENT_ANALYSIS_INVALID_OUTPUT"
    http_status = 502


class DocumentAnalysisNotFoundError(DocumentAnalysisError):
    code = "DOCUMENT_ANALYSIS_NOT_FOUND"
    http_status = 404


class DocumentAnalysisStorageError(DocumentAnalysisError):
    code = "DOCUMENT_ANALYSIS_STORAGE_UNAVAILABLE"
    http_status = 502


class DocumentAnalysisVersionChangedError(DocumentAnalysisError):
    code = "DOCUMENT_ANALYSIS_VERSION_CHANGED"
    http_status = 409


@dataclass(frozen=True)
class AnalysisSource:
    """最终 Markdown 中 ``[文档片段N]`` 对应的来源。"""

    citation_index: int
    chunk_id: str
    chunk_index: int
    chunk_type: str
    page: int | None
    page_range: dict[str, int] | None
    excerpt: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "citation_index": self.citation_index,
            "chunk_id": self.chunk_id,
            "chunk_index": self.chunk_index,
            "chunk_type": self.chunk_type,
            "page": self.page,
            "page_range": self.page_range,
            "excerpt": self.excerpt,
        }


@dataclass(frozen=True)
class _AnalysisChunk:
    source: AnalysisSource
    content: str
    token_count: int

    @property
    def prompt_block(self) -> str:
        location = ""
        if self.source.page is not None:
            location = f"（第 {self.source.page} 页）"
        elif self.source.page_range is not None:
            location = (
                f"（第 {self.source.page_range['start']}–"
                f"{self.source.page_range['end']} 页）"
            )
        return f"[文档片段{self.source.citation_index}]{location} {self.content.strip()}"


@dataclass(frozen=True)
class _EvidenceItem:
    citation_index: int
    disposition: str
    summary: str
    issues: tuple[str, ...]

    @property
    def requires_report_coverage(self) -> bool:
        return self.disposition == "analyze"

    @property
    def prompt_line(self) -> str:
        status = {
            "analyze": "必须纳入报告",
            "context": "仅作上下文",
            "duplicate": "重复内容",
        }[self.disposition]
        issues = "；".join(self.issues) if self.issues else "无单独问题"
        return (
            f"- [文档片段{self.citation_index}] 状态：{status}；"
            f"摘要：{self.summary}；问题：{issues}"
        )


@dataclass(frozen=True)
class DocumentAnalysisResult:
    markdown: str
    sources: list[AnalysisSource]
    model_name: str
    model_config_id: int
    usage: UsageInfo
    analyzed_chunk_count: int
    evidence_batch_count: int
    generated_at: datetime


@dataclass(frozen=True)
class DocumentAnalysisSummary:
    """无需下载报告正文即可展示的已保存分析报告摘要。"""

    model_name: str
    model_config_id: int
    analyzed_chunk_count: int
    evidence_batch_count: int
    source_count: int
    generated_at: datetime


def _is_derived(record: ChunkRecordDB) -> bool:
    structure = record.structure_metadata
    return bool(
        isinstance(structure, dict)
        and str(structure.get("chunk_role") or "").strip().lower() == "derived_element"
    )


def _page_metadata(record: ChunkRecordDB) -> tuple[int | None, dict[str, int] | None]:
    start = record.start_page
    end = record.end_page
    if start is None or end is None:
        return None, None
    if int(start) == int(end):
        return int(start), None
    return None, {"start": int(start), "end": int(end)}


def _build_chunks(records: list[ChunkRecordDB], tokenizer: Tokenizer) -> list[_AnalysisChunk]:
    chunks: list[_AnalysisChunk] = []
    for record in records:
        content = str(record.content or "").strip()
        if not content or _is_derived(record):
            continue
        page, page_range = _page_metadata(record)
        source = AnalysisSource(
            citation_index=len(chunks) + 1,
            chunk_id=str(record.chunk_id),
            chunk_index=int(record.chunk_index),
            chunk_type=str(record.chunk_type or "text"),
            page=page,
            page_range=page_range,
            excerpt=re.sub(r"\s+", " ", content)[:240],
        )
        chunks.append(
            _AnalysisChunk(
                source=source,
                content=content,
                token_count=tokenizer.count_tokens(content),
            )
        )
    return chunks


def _partition_chunks(
    chunks: list[_AnalysisChunk],
    *,
    token_budget: int,
    max_chunks: int,
) -> list[list[_AnalysisChunk]]:
    if not chunks:
        return []

    total_tokens = sum(chunk.token_count for chunk in chunks)
    expected_batch_count = max(
        1,
        math.ceil(total_tokens / token_budget),
        math.ceil(len(chunks) / max_chunks),
    )
    balanced_target = min(token_budget, math.ceil(total_tokens / expected_batch_count))
    batches: list[list[_AnalysisChunk]] = []
    current: list[_AnalysisChunk] = []
    current_tokens = 0
    for chunk in chunks:
        can_open_balanced_batch = len(batches) < expected_batch_count - 1
        exceeds_target = current_tokens + chunk.token_count > balanced_target
        exceeds_budget = current_tokens + chunk.token_count > token_budget
        reaches_chunk_limit = len(current) >= max_chunks
        if current and (
            reaches_chunk_limit
            or exceeds_budget
            or (can_open_balanced_batch and exceeds_target)
        ):
            batches.append(current)
            current = []
            current_tokens = 0
        current.append(chunk)
        current_tokens += chunk.token_count
    if current:
        batches.append(current)
    return batches


def _split_batch(batch: list[_AnalysisChunk]) -> tuple[list[_AnalysisChunk], list[_AnalysisChunk]]:
    """按 token 重心拆分失败批次，同时保持原始文档顺序。"""

    if len(batch) < 2:
        raise ValueError("单片段批次不能继续拆分")
    total_tokens = sum(chunk.token_count for chunk in batch)
    running_tokens = 0
    best_index = 1
    best_difference = total_tokens
    for index, chunk in enumerate(batch[:-1], start=1):
        running_tokens += chunk.token_count
        difference = abs(total_tokens - 2 * running_tokens)
        if difference < best_difference:
            best_index = index
            best_difference = difference
    return batch[:best_index], batch[best_index:]


def _normalize_finish_reason(value: str | None) -> str:
    return str(value or "").strip().lower().replace("-", "_")


def _is_length_finish_reason(value: str | None) -> bool:
    return _normalize_finish_reason(value) in _LENGTH_FINISH_REASONS


def _evidence_generation_options(provider: Any) -> dict[str, Any]:
    """为已确认支持 JSON Output 的官方端点开启协议级格式约束。"""

    endpoint = str(getattr(provider, "api_base_url", "") or "").strip()
    hostname = (urlparse(endpoint).hostname or "").lower()
    if hostname == "api.deepseek.com":
        options: dict[str, Any] = {"response_format": {"type": "json_object"}}
        if str(getattr(provider, "model_name", "")).lower().startswith("deepseek-v4"):
            options["thinking"] = {"type": "disabled"}
        return options
    return {}


def _report_generation_options(provider: Any) -> dict[str, Any]:
    """长报告不需要模型思考模式占用输出预算；仅对已确认支持的 V4 端点关闭。"""

    endpoint = str(getattr(provider, "api_base_url", "") or "").strip()
    hostname = (urlparse(endpoint).hostname or "").lower()
    model_name = str(getattr(provider, "model_name", "")).lower()
    if hostname == "api.deepseek.com" and model_name.startswith("deepseek-v4"):
        return {"thinking": {"type": "disabled"}}
    return {}


def _load_json_object(content: str) -> dict[str, Any]:
    """读取模型输出中的唯一 JSON 对象，容忍思考块、围栏和简短前言。"""

    normalized = _THINK_BLOCK.sub("", str(content or "")).strip()
    fenced = _OUTER_JSON_FENCE.match(normalized)
    if fenced:
        normalized = fenced.group("body").strip()
    try:
        payload = json.loads(normalized)
    except (TypeError, json.JSONDecodeError):
        object_start = normalized.find("{")
        if object_start < 0:
            raise DocumentAnalysisInvalidOutputError("证据输出不是有效 JSON") from None
        try:
            payload, consumed = json.JSONDecoder().raw_decode(normalized[object_start:])
        except json.JSONDecodeError as exc:
            raise DocumentAnalysisInvalidOutputError(
                f"证据输出不是有效 JSON（第 {exc.lineno} 行第 {exc.colno} 列）"
            ) from exc
        if normalized[object_start + consumed :].strip():
            raise DocumentAnalysisInvalidOutputError("证据 JSON 后包含额外内容") from None
    if not isinstance(payload, dict):
        raise DocumentAnalysisInvalidOutputError("证据输出必须是 JSON 对象")
    return payload


def _parse_evidence_output(
    content: str,
    *,
    batch: list[_AnalysisChunk],
) -> list[_EvidenceItem]:
    payload = _load_json_object(content)
    raw_items = payload.get("chunks")
    if not isinstance(raw_items, list):
        raise DocumentAnalysisInvalidOutputError("证据输出缺少 chunks 数组")

    expected_indexes = [chunk.source.citation_index for chunk in batch]
    parsed: dict[int, _EvidenceItem] = {}
    errors: list[str] = []
    for position, raw_item in enumerate(raw_items, start=1):
        if not isinstance(raw_item, dict):
            errors.append(f"第 {position} 条证据不是对象")
            continue
        try:
            citation_index = int(raw_item.get("citation_index"))
        except (TypeError, ValueError):
            errors.append(f"第 {position} 条证据缺少有效 citation_index")
            continue
        if citation_index in parsed:
            errors.append(f"文档片段 {citation_index} 重复登记")
            continue
        disposition = str(raw_item.get("disposition") or "").strip().lower()
        if disposition not in {"analyze", "context", "duplicate"}:
            errors.append(f"文档片段 {citation_index} 的 disposition 无效")
            continue
        summary = re.sub(r"\s+", " ", str(raw_item.get("summary") or "")).strip()
        if not summary:
            errors.append(f"文档片段 {citation_index} 缺少 summary")
            continue
        raw_issues = raw_item.get("issues")
        if not isinstance(raw_issues, list):
            errors.append(f"文档片段 {citation_index} 的 issues 必须是数组")
            continue
        issues = tuple(
            normalized_issue
            for issue in raw_issues
            if (normalized_issue := re.sub(r"\s+", " ", str(issue)).strip())
        )
        parsed[citation_index] = _EvidenceItem(
            citation_index=citation_index,
            disposition=disposition,
            summary=summary,
            issues=issues,
        )

    expected_set = set(expected_indexes)
    actual_set = set(parsed)
    missing = sorted(expected_set - actual_set)
    unexpected = sorted(actual_set - expected_set)
    if missing:
        errors.append("遗漏文档片段：" + "、".join(map(str, missing)))
    if unexpected:
        errors.append("包含非本批文档片段：" + "、".join(map(str, unexpected)))
    if errors:
        raise DocumentAnalysisInvalidOutputError("；".join(errors))
    return [parsed[index] for index in expected_indexes]


def _normalize_markdown(markdown: str, *, valid_source_count: int) -> tuple[str, set[int]]:
    normalized = str(markdown or "").strip()
    fenced = _OUTER_MARKDOWN_FENCE.match(normalized)
    if fenced:
        normalized = fenced.group("body").strip()
    if not normalized:
        raise DocumentAnalysisInvalidOutputError("大模型没有返回分析内容")
    if not normalized.startswith("# 企业文档分析报告"):
        normalized = f"# 企业文档分析报告\n\n{normalized}"

    valid_citations: set[int] = set()

    def replace_citation(match: re.Match[str]) -> str:
        index = int(match.group("index"))
        if 1 <= index <= valid_source_count:
            valid_citations.add(index)
            return match.group(0)
        return "[来源未核验]"

    normalized = _CITATION_PATTERN.sub(replace_citation, normalized)
    return normalized.rstrip() + "\n", valid_citations


def _report_validation_errors(
    markdown: str,
    *,
    citation_indexes: set[int],
    required_citation_indexes: set[int],
    valid_source_count: int,
    finish_reason: str | None,
) -> list[str]:
    errors: list[str] = []
    if _is_length_finish_reason(finish_reason):
        errors.append("模型因达到输出 token 上限而停止，报告可能被截断")

    missing_headings = [heading for heading in _REQUIRED_HEADINGS if heading not in markdown]
    if missing_headings:
        errors.append("缺少必需章节：" + "、".join(missing_headings))
    else:
        positions = [markdown.index(heading) for heading in _REQUIRED_HEADINGS]
        if positions != sorted(positions):
            errors.append("必需章节顺序不正确")
        conclusion = markdown.split(_REQUIRED_HEADINGS[-1], maxsplit=1)[-1].strip()
        if len(conclusion) < 8:
            errors.append("第 12 章内容不完整")

    if "本批" in markdown:
        errors.append("最终报告仍包含“本批”类证据提取阶段措辞")

    missing_required = sorted(required_citation_indexes - citation_indexes)
    if missing_required:
        errors.append(
            "未分析证据台账中要求纳入报告的文档片段："
            + "、".join(map(str, missing_required))
        )
    if valid_source_count and not citation_indexes:
        errors.append("最终报告没有形成任何可核验的文档片段引用")
    return errors


def _sum_usage(items: list[UsageInfo]) -> UsageInfo:
    return UsageInfo(
        prompt_tokens=sum(item.prompt_tokens for item in items),
        completion_tokens=sum(item.completion_tokens for item in items),
        total_tokens=sum(item.total_tokens for item in items),
    )


class DocumentAnalysisService:
    """覆盖全部主体分片，分批提证后生成 Markdown 报告。"""

    async def analyze(
        self,
        *,
        db: AsyncSession,
        document: Document,
        dataset: Dataset,
        user_id: int,
        llm_config_id: int | None = None,
    ) -> DocumentAnalysisResult:
        records = list(
            (
                await db.scalars(
                    select(ChunkRecordDB)
                    .where(
                        ChunkRecordDB.doc_id == document.id,
                        ChunkRecordDB.set_id == document.dataset_id,
                        ChunkRecordDB.user_id == user_id,
                        ChunkRecordDB.document_version == document.version,
                    )
                    .order_by(ChunkRecordDB.chunk_index.asc(), ChunkRecordDB.id.asc())
                )
            ).all()
        )
        tokenizer = Tokenizer()
        chunks = _build_chunks(records, tokenizer)
        if not chunks:
            raise DocumentAnalysisEmptyError("文档当前版本没有可分析的主体分片")

        total_tokens = sum(chunk.token_count for chunk in chunks)
        if total_tokens > settings.DOCUMENT_ANALYSIS_MAX_INPUT_TOKENS:
            raise DocumentAnalysisTooLargeError(
                token_count=total_tokens,
                token_limit=settings.DOCUMENT_ANALYSIS_MAX_INPUT_TOKENS,
            )

        config_id = llm_config_id or dataset.chat_config_id
        if config_id is None:
            raise DocumentAnalysisError("数据集尚未绑定可用的对话模型")

        initial_batches = _partition_chunks(
            chunks,
            token_budget=settings.DOCUMENT_ANALYSIS_BATCH_TOKEN_BUDGET,
            max_chunks=settings.DOCUMENT_ANALYSIS_MAX_CHUNKS_PER_BATCH,
        )
        resolved = await aresolve_model(
            user_id=user_id,
            config_id=int(config_id),
            capability="CHAT",
            db=db,
        )
        usage_items: list[UsageInfo] = []
        evidence_items: list[_EvidenceItem] = []
        successful_batches: list[tuple[list[_AnalysisChunk], list[_EvidenceItem]]] = []
        try:
            evidence_generation_options = _evidence_generation_options(resolved.provider)
            report_generation_options = _report_generation_options(resolved.provider)
            pending_batches = [(batch, 0) for batch in initial_batches]
            while pending_batches:
                batch, split_depth = pending_batches.pop(0)
                batch_index = len(successful_batches) + 1
                projected_batch_count = batch_index + len(pending_batches)
                context = "\n\n".join(chunk.prompt_block for chunk in batch)
                evidence_prompt = build_document_evidence_prompt(
                    filename=document.filename,
                    batch_number=batch_index,
                    batch_count=projected_batch_count,
                    context=context,
                )
                current_prompt = evidence_prompt
                batch_items: list[_EvidenceItem] | None = None
                validation_error: DocumentAnalysisInvalidOutputError | None = None
                truncated_reason: str | None = None
                for evidence_attempt in range(
                    settings.DOCUMENT_ANALYSIS_EVIDENCE_REPAIR_ATTEMPTS + 1
                ):
                    evidence = await asyncio.wait_for(
                        resolved.provider.generate(
                            prompt=current_prompt,
                            system_prompt=DOCUMENT_EVIDENCE_SYSTEM_PROMPT,
                            temperature=0.1,
                            max_tokens=settings.DOCUMENT_ANALYSIS_EVIDENCE_MAX_OUTPUT_TOKENS,
                            **evidence_generation_options,
                        ),
                        timeout=settings.DOCUMENT_ANALYSIS_MODEL_TIMEOUT_MS / 1000,
                    )
                    usage_items.append(evidence.usage)
                    if _is_length_finish_reason(evidence.finish_reason):
                        truncated_reason = _normalize_finish_reason(evidence.finish_reason)
                        break
                    try:
                        batch_items = _parse_evidence_output(evidence.content, batch=batch)
                        break
                    except DocumentAnalysisInvalidOutputError as exc:
                        validation_error = exc
                        if (
                            evidence_attempt
                            >= settings.DOCUMENT_ANALYSIS_EVIDENCE_REPAIR_ATTEMPTS
                        ):
                            break
                        current_prompt = build_document_evidence_repair_prompt(
                            original_prompt=evidence_prompt,
                            rejected_output=evidence.content[:12000],
                            validation_errors=[str(exc)],
                        )

                if batch_items is None:
                    can_split = (
                        len(batch) > 1
                        and split_depth
                        < settings.DOCUMENT_ANALYSIS_EVIDENCE_SPLIT_MAX_DEPTH
                    )
                    if can_split:
                        left, right = _split_batch(batch)
                        pending_batches[0:0] = [
                            (left, split_depth + 1),
                            (right, split_depth + 1),
                        ]
                        continue

                    start_citation = batch[0].source.citation_index
                    end_citation = batch[-1].source.citation_index
                    batch_label = f"文档片段{start_citation}-{end_citation}"
                    if truncated_reason:
                        raise DocumentAnalysisInvalidOutputError(
                            f"证据批次 {batch_label} 因输出达到 token 上限而截断，"
                            "且已达到安全拆分上限"
                        )
                    detail = str(validation_error or "证据台账未生成")
                    raise DocumentAnalysisInvalidOutputError(
                        f"证据批次 {batch_label} 在格式修复和安全拆分后仍无效：{detail}"
                    )

                evidence_items.extend(batch_items)
                successful_batches.append((batch, batch_items))

            evidence_notes: list[str] = []
            for batch_index, (batch, batch_items) in enumerate(successful_batches, start=1):
                start_citation = batch[0].source.citation_index
                end_citation = batch[-1].source.citation_index
                evidence_notes.append(
                    f"### 证据批次 {batch_index}（文档片段{start_citation}-{end_citation}）"
                    "\n\n"
                    + "\n".join(item.prompt_line for item in batch_items)
                )

            joined_evidence = "\n\n".join(evidence_notes)
            required_citation_indexes = {
                item.citation_index
                for item in evidence_items
                if item.requires_report_coverage
            }
            report = await asyncio.wait_for(
                resolved.provider.generate(
                    prompt=build_document_analysis_prompt(
                        filename=document.filename,
                        document_version=int(document.version),
                        evidence_notes=joined_evidence,
                    ),
                    system_prompt=DOCUMENT_ANALYSIS_SYSTEM_PROMPT,
                    temperature=0.1,
                    max_tokens=settings.DOCUMENT_ANALYSIS_REPORT_MAX_OUTPUT_TOKENS,
                    **report_generation_options,
                ),
                timeout=settings.DOCUMENT_ANALYSIS_MODEL_TIMEOUT_MS / 1000,
            )
            usage_items.append(report.usage)

            for repair_attempt in range(settings.DOCUMENT_ANALYSIS_REPORT_REPAIR_ATTEMPTS + 1):
                try:
                    markdown, citation_indexes = _normalize_markdown(
                        report.content,
                        valid_source_count=len(chunks),
                    )
                    validation_errors = _report_validation_errors(
                        markdown,
                        citation_indexes=citation_indexes,
                        required_citation_indexes=required_citation_indexes,
                        valid_source_count=len(chunks),
                        finish_reason=report.finish_reason,
                    )
                except DocumentAnalysisInvalidOutputError as exc:
                    markdown = str(report.content or "").strip()
                    citation_indexes = set()
                    validation_errors = [str(exc)]
                    if _is_length_finish_reason(report.finish_reason):
                        validation_errors.append("模型因达到输出 token 上限而停止")
                if not validation_errors:
                    break
                if repair_attempt >= settings.DOCUMENT_ANALYSIS_REPORT_REPAIR_ATTEMPTS:
                    raise DocumentAnalysisInvalidOutputError(
                        "大模型报告未通过完整性与证据覆盖校验："
                        + "；".join(validation_errors)
                    )
                report = await asyncio.wait_for(
                    resolved.provider.generate(
                        prompt=build_document_analysis_repair_prompt(
                            filename=document.filename,
                            document_version=int(document.version),
                            evidence_notes=joined_evidence,
                            rejected_report=markdown,
                            validation_errors=validation_errors,
                        ),
                        system_prompt=DOCUMENT_ANALYSIS_SYSTEM_PROMPT,
                        temperature=0.1,
                        max_tokens=settings.DOCUMENT_ANALYSIS_REPORT_MAX_OUTPUT_TOKENS,
                        **report_generation_options,
                    ),
                    timeout=settings.DOCUMENT_ANALYSIS_MODEL_TIMEOUT_MS / 1000,
                )
                usage_items.append(report.usage)
        finally:
            await aclose_resolved_models([resolved])

        cited_sources = [
            chunk.source for chunk in chunks if chunk.source.citation_index in citation_indexes
        ]
        return DocumentAnalysisResult(
            markdown=markdown,
            sources=cited_sources,
            model_name=resolved.model_name,
            model_config_id=resolved.config_id,
            usage=_sum_usage(usage_items),
            analyzed_chunk_count=len(chunks),
            evidence_batch_count=len(successful_batches),
            generated_at=datetime.now(UTC),
        )


class DocumentAnalysisStore:
    """在当前解析版本目录中持久化 Markdown 报告与提交指针。"""

    _SCHEMA_VERSION = 1
    _MAX_MANIFEST_BYTES = 1024 * 1024
    _MAX_MARKDOWN_BYTES = 16 * 1024 * 1024

    def __init__(self, storage: BaseObjectStorage | None = None) -> None:
        self._storage = storage or StorageFactory.get_storage()

    @staticmethod
    def _locations(document: Document) -> tuple[str, str, str]:
        if not document.parsed_bucket or not document.parsed_object_key:
            raise DocumentAnalysisStorageError("文档解析产物位置不完整，请重新解析后再试")
        parsed_key = PurePosixPath(document.parsed_object_key)
        if parsed_key.is_absolute() or ".." in parsed_key.parts:
            raise DocumentAnalysisStorageError("文档解析产物路径不合法")
        expected_root = PurePosixPath(
            f"parsed/{document.user_id}/{document.dataset_id}/{document.id}"
        )
        try:
            parsed_key.relative_to(expected_root)
        except ValueError as exc:
            raise DocumentAnalysisStorageError("文档解析产物不在当前文档目录中") from exc
        analysis_prefix = (parsed_key.parent / "analysis").as_posix()
        return (
            str(document.parsed_bucket),
            analysis_prefix,
            f"{analysis_prefix}/latest.json",
        )

    async def save(
        self,
        *,
        document: Document,
        result: DocumentAnalysisResult,
    ) -> None:
        bucket, analysis_prefix, manifest_key = self._locations(document)
        markdown_bytes = result.markdown.encode("utf-8")
        if len(markdown_bytes) > self._MAX_MARKDOWN_BYTES:
            raise DocumentAnalysisStorageError("分析报告超过对象存储写入上限")
        markdown_sha256 = hashlib.sha256(markdown_bytes).hexdigest()
        markdown_key = f"{analysis_prefix}/report-{markdown_sha256}.md"
        previous_markdown_key = await self._load_previous_markdown_key(
            bucket=bucket,
            manifest_key=manifest_key,
            analysis_prefix=analysis_prefix,
        )
        manifest = {
            "schema_version": self._SCHEMA_VERSION,
            "document_id": int(document.id),
            "dataset_id": int(document.dataset_id),
            "document_version": int(document.version),
            "filename": document.filename,
            "markdown_object_key": markdown_key,
            "markdown_sha256": markdown_sha256,
            "sources": [source.to_dict() for source in result.sources],
            "model_name": result.model_name,
            "model_config_id": result.model_config_id,
            "usage": result.usage.model_dump(),
            "analyzed_chunk_count": result.analyzed_chunk_count,
            "evidence_batch_count": result.evidence_batch_count,
            "generated_at": result.generated_at.isoformat(),
        }
        manifest_bytes = json.dumps(
            manifest,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        try:
            await asyncio.to_thread(
                self._storage.upload_bytes,
                bucket,
                markdown_key,
                markdown_bytes,
                "text/markdown; charset=utf-8",
            )
            await asyncio.to_thread(
                self._storage.upload_bytes,
                bucket,
                manifest_key,
                manifest_bytes,
                "application/json; charset=utf-8",
            )
        except Exception as exc:
            raise DocumentAnalysisStorageError("保存分析报告到 MinIO 失败，请稍后重试") from exc

        if previous_markdown_key and previous_markdown_key != markdown_key:
            try:
                await asyncio.to_thread(
                    self._storage.remove_object,
                    bucket,
                    previous_markdown_key,
                )
            except Exception:
                # latest.json 已经原子切换到新报告；旧文件由重新解析或文档删除时兜底清理。
                pass

    async def load(self, *, document: Document) -> DocumentAnalysisResult:
        bucket, analysis_prefix, manifest_key = self._locations(document)
        manifest = await self._download_manifest(bucket=bucket, manifest_key=manifest_key)
        try:
            if int(manifest["schema_version"]) != self._SCHEMA_VERSION:
                raise ValueError("schema mismatch")
            if int(manifest["document_id"]) != int(document.id):
                raise ValueError("document mismatch")
            if int(manifest["dataset_id"]) != int(document.dataset_id):
                raise ValueError("dataset mismatch")
            if int(manifest["document_version"]) != int(document.version):
                raise DocumentAnalysisNotFoundError("当前文档版本尚未生成分析报告")
            markdown_key = self._validated_markdown_key(
                str(manifest["markdown_object_key"]),
                analysis_prefix=analysis_prefix,
            )
            expected_hash = str(manifest["markdown_sha256"])
            generated_at = datetime.fromisoformat(str(manifest["generated_at"]))
            sources = [AnalysisSource(**item) for item in manifest.get("sources", [])]
            usage = UsageInfo.model_validate(manifest.get("usage") or {})
        except DocumentAnalysisError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise DocumentAnalysisStorageError("MinIO 中的分析报告元数据无效") from exc

        markdown_bytes = await self._download_object(
            bucket=bucket,
            object_key=markdown_key,
            max_bytes=self._MAX_MARKDOWN_BYTES,
            not_found_message="分析报告 Markdown 不存在，请重新生成",
            missing_is_not_found=False,
        )
        if hashlib.sha256(markdown_bytes).hexdigest() != expected_hash:
            raise DocumentAnalysisStorageError("分析报告完整性校验失败，请重新生成")
        try:
            markdown = markdown_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise DocumentAnalysisStorageError("分析报告不是有效的 UTF-8 Markdown") from exc
        return DocumentAnalysisResult(
            markdown=markdown,
            sources=sources,
            model_name=str(manifest["model_name"]),
            model_config_id=int(manifest["model_config_id"]),
            usage=usage,
            analyzed_chunk_count=int(manifest["analyzed_chunk_count"]),
            evidence_batch_count=int(manifest["evidence_batch_count"]),
            generated_at=generated_at,
        )

    async def load_summary(self, *, document: Document) -> DocumentAnalysisSummary:
        """读取当前文档版本的报告清单元数据，不下载 Markdown 正文。"""

        bucket, analysis_prefix, manifest_key = self._locations(document)
        manifest = await self._download_manifest(bucket=bucket, manifest_key=manifest_key)
        try:
            if int(manifest["schema_version"]) != self._SCHEMA_VERSION:
                raise ValueError("schema mismatch")
            if int(manifest["document_id"]) != int(document.id):
                raise ValueError("document mismatch")
            if int(manifest["dataset_id"]) != int(document.dataset_id):
                raise ValueError("dataset mismatch")
            if int(manifest["document_version"]) != int(document.version):
                raise DocumentAnalysisNotFoundError("当前文档版本尚未生成分析报告")
            self._validated_markdown_key(
                str(manifest["markdown_object_key"]),
                analysis_prefix=analysis_prefix,
            )
            raw_sources = manifest.get("sources", [])
            if not isinstance(raw_sources, list):
                raise ValueError("invalid sources")
            generated_at = datetime.fromisoformat(str(manifest["generated_at"]))
            return DocumentAnalysisSummary(
                model_name=str(manifest["model_name"]),
                model_config_id=int(manifest["model_config_id"]),
                analyzed_chunk_count=int(manifest["analyzed_chunk_count"]),
                evidence_batch_count=int(manifest["evidence_batch_count"]),
                source_count=len(raw_sources),
                generated_at=generated_at,
            )
        except DocumentAnalysisError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise DocumentAnalysisStorageError("MinIO 中的分析报告元数据无效") from exc

    async def _load_previous_markdown_key(
        self,
        *,
        bucket: str,
        manifest_key: str,
        analysis_prefix: str,
    ) -> str | None:
        try:
            manifest = await self._download_manifest(bucket=bucket, manifest_key=manifest_key)
            return self._validated_markdown_key(
                str(manifest.get("markdown_object_key") or ""),
                analysis_prefix=analysis_prefix,
            )
        except DocumentAnalysisNotFoundError:
            return None
        except DocumentAnalysisError:
            # 旧指针损坏不阻止新报告以完整的新指针覆盖。
            return None

    async def _download_manifest(self, *, bucket: str, manifest_key: str) -> dict[str, Any]:
        content = await self._download_object(
            bucket=bucket,
            object_key=manifest_key,
            max_bytes=self._MAX_MANIFEST_BYTES,
            not_found_message="当前文档版本尚未生成分析报告",
        )
        try:
            payload = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DocumentAnalysisStorageError("MinIO 中的分析报告元数据无法解析") from exc
        if not isinstance(payload, dict):
            raise DocumentAnalysisStorageError("MinIO 中的分析报告元数据无效")
        return payload

    async def _download_object(
        self,
        *,
        bucket: str,
        object_key: str,
        max_bytes: int,
        not_found_message: str,
        missing_is_not_found: bool = True,
    ) -> bytes:
        temp_path: Path | None = None
        try:
            descriptor, temp_name = tempfile.mkstemp(prefix="document-analysis-", suffix=".tmp")
            temp_path = Path(temp_name)
            # download_to_path 会重新打开目标文件；关闭 mkstemp 返回的描述符避免泄漏。
            os.close(descriptor)
            await asyncio.to_thread(
                self._storage.download_to_path,
                bucket,
                object_key,
                temp_path,
            )
            if temp_path.stat().st_size > max_bytes:
                raise DocumentAnalysisStorageError("MinIO 中的分析报告超过读取上限")
            return await asyncio.to_thread(temp_path.read_bytes)
        except ClientError as exc:
            error_code = str(exc.response.get("Error", {}).get("Code", ""))
            if error_code in {"404", "NoSuchKey", "NoSuchObject", "NotFound"}:
                if missing_is_not_found:
                    raise DocumentAnalysisNotFoundError(not_found_message) from exc
                raise DocumentAnalysisStorageError(not_found_message) from exc
            raise DocumentAnalysisStorageError("读取 MinIO 分析报告失败，请稍后重试") from exc
        except DocumentAnalysisError:
            raise
        except Exception as exc:
            raise DocumentAnalysisStorageError("读取 MinIO 分析报告失败，请稍后重试") from exc
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)

    @staticmethod
    def _validated_markdown_key(object_key: str, *, analysis_prefix: str) -> str:
        path = PurePosixPath(object_key)
        if (
            not object_key
            or path.is_absolute()
            or ".." in path.parts
            or path.parent.as_posix() != analysis_prefix
            or not path.name.startswith("report-")
            or path.suffix.lower() != ".md"
        ):
            raise DocumentAnalysisStorageError("分析报告对象路径不合法")
        return path.as_posix()
