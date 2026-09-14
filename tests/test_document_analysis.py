# ruff: noqa: E501 - 报告测试数据保持完整 Markdown 章节，避免拆分改变格式。

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

import app.services.document_analysis as analysis_module
from app.domain.models import Dataset, Document
from app.rag.core.llm.response import GenerateResult, UsageInfo
from app.rag.models.chunk_record import ChunkRecordDB
from app.services.document_analysis import (
    AnalysisSource,
    DocumentAnalysisService,
    DocumentAnalysisStore,
    DocumentAnalysisTooLargeError,
    _normalize_markdown,
    _parse_evidence_output,
    _partition_chunks,
)


class _ScalarResult:
    def __init__(self, values):
        self._values = values

    def all(self):
        return list(self._values)


class _Session:
    def __init__(self, records):
        self.records = records
        self.statements = []

    async def scalars(self, statement):
        self.statements.append(statement)
        return _ScalarResult(self.records)


class _Provider:
    def __init__(self, report: str):
        self.report = report
        self.calls = []
        self.close_calls = 0

    async def generate(self, **kwargs):
        self.calls.append(kwargs)
        if "企业能碳文档分析助手" in kwargs["system_prompt"]:
            content = self.report
        else:
            indexes = [int(value) for value in re.findall(r"\[文档片段(\d+)\]", kwargs["prompt"])]
            content = json.dumps(
                {
                    "chunks": [
                        {
                            "citation_index": index,
                            "disposition": "analyze",
                            "summary": f"片段 {index} 包含需分析内容。",
                            "issues": [],
                        }
                        for index in dict.fromkeys(indexes)
                    ]
                },
                ensure_ascii=False,
            )
        return GenerateResult(
            content=content,
            model="analysis-model",
            provider_type="test",
            latency_ms=5,
            usage=UsageInfo(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )

    async def aclose(self):
        self.close_calls += 1


def _record(
    index: int,
    content: str,
    *,
    role: str = "mixed",
    page: int | None = None,
) -> ChunkRecordDB:
    return ChunkRecordDB(
        id=index + 1,
        chunk_id=f"chunk-{index}",
        doc_id=7,
        document_version=2,
        set_id=3,
        user_id=11,
        content=content,
        content_hash=f"hash-{index}",
        chunk_type="mixed",
        start_line=index * 2,
        end_line=index * 2 + 1,
        start_page=page,
        end_page=page,
        structure_metadata={"chunk_role": role, "split_strategy": "candidate_boundary + noop"},
        chunk_index=index,
    )


def _document() -> Document:
    return Document(
        id=7,
        dataset_id=3,
        user_id=11,
        filename="企业碳足迹报告.docx",
        file_type="docx",
        file_size=1024,
        raw_bucket="raw",
        raw_object_key="raw/report.docx",
        parsed_bucket="private",
        parsed_object_key="parsed/11/3/7/versions/v2/attempt-1/report.md",
        status="READY",
        version=2,
    )


def _dataset() -> Dataset:
    return Dataset(
        id=3,
        user_id=11,
        name="企业材料",
        status="ACTIVE",
        dense_embedding_config_id=21,
        sparse_embedding_config_id=22,
        chat_config_id=23,
    )


def _report() -> str:
    sections = [
        "# 企业文档分析报告",
        "## 1. 报告摘要\n\n存在年份标注问题。[文档片段2]",
        "## 2. 评价对象和目标\n\n### 2.1 评价对象\n\n待确认。\n\n### 2.2 评价目标\n\n用于文档检查。",
        "## 3. 评价方法和工具\n\n### 3.1 评价依据\n\n未说明。\n\n### 3.2 结果量化与工具\n\n未说明。",
        "## 4. 评价边界界定\n\n### 4.1 组织边界\n\n待补充。\n\n### 4.2 时间边界\n\n报告期为 2023 年。[文档片段1]\n\n### 4.3 生命周期边界\n\n待补充。",
        "## 5. 功能单位或核算口径\n\n待补充。",
        "## 6. 生命周期与活动数据清单分析\n\n### 6.1 上游原材料与采购\n\n待补充。\n\n### 6.2 内部生产\n\n待补充。\n\n### 6.3 下游运输与销售\n\n待补充。\n\n### 6.4 使用过程\n\n待补充。\n\n### 6.5 废弃处理与回收\n\n待补充。\n\n### 6.6 清单筛分及物料流\n\n待补充。",
        "## 7. 碳足迹核算及评价\n\n### 7.1 建模与核算\n\n尚不足以复算。\n\n### 7.2 结果与分析\n\n待补充。",
        "## 8. 量化数据质量与可靠性\n\n### 8.1 数据质量说明\n\n待补充。\n\n### 8.2 量化排除与质量保障\n\n待补充。",
        "## 9. 不确定性分析\n\n### 9.1 方法不确定性\n\n待补充。\n\n### 9.2 参数不确定性\n\n待补充。\n\n### 9.3 情景不确定性\n\n待补充。",
        (
            "## 10. 文档规范性检查\n\n"
            "| 序号 | 检查项 | 结论 | 发现与证据 | 改进建议 |\n"
            "| --- | --- | --- | --- | --- |\n"
            "| 1 | 报告期 | 部分满足 | 表题年份不清。[文档片段2] | 补充年份 |"
        ),
        "## 11. 资料缺口与整改建议\n\n| 优先级 | 缺少或待确认资料 | 影响 | 建议动作 |\n| --- | --- | --- | --- |\n| 高 | 表格适用年度 | 口径不清 | 补充表题年份 |",
        "## 12. 结论与分析限制\n\n先补充可追溯信息；不能替代正式核查。",
    ]
    return "\n\n".join(sections)


@pytest.mark.asyncio
async def test_document_analysis_covers_source_chunks_and_returns_markdown_with_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records = [
        _record(0, "报告期为 2023 年。", page=1),
        _record(1, "能耗表标题没有写年度。", page=8),
        _record(2, "派生表格重复内容", role="derived_element", page=8),
    ]
    provider = _Provider(_report())
    provider.api_base_url = "https://api.deepseek.com/chat/completions"
    provider.model_name = "deepseek-v4-flash"

    async def fake_resolve(**_kwargs):
        return SimpleNamespace(
            provider=provider,
            model_name="analysis-model",
            config_id=23,
        )

    monkeypatch.setattr(analysis_module, "aresolve_model", fake_resolve)
    result = await DocumentAnalysisService().analyze(
        db=_Session(records),
        document=_document(),
        dataset=_dataset(),
        user_id=11,
    )

    assert result.markdown.startswith("# 企业文档分析报告\n")
    assert "## 10. 文档规范性检查" in result.markdown
    assert result.analyzed_chunk_count == 2
    assert result.evidence_batch_count == 1
    assert [source.citation_index for source in result.sources] == [1, 2]
    assert result.sources[0].page == 1
    assert result.sources[1].page == 8
    assert result.sources[1].excerpt == "能耗表标题没有写年度。"
    assert len(provider.calls) == 2
    assert "[文档片段1]（第 1 页） 报告期为 2023 年。" in provider.calls[0]["prompt"]
    assert "派生表格重复内容" not in provider.calls[0]["prompt"]
    assert provider.calls[0]["temperature"] == 0.1
    assert provider.calls[0]["response_format"] == {"type": "json_object"}
    assert provider.calls[0]["thinking"] == {"type": "disabled"}
    assert "response_format" not in provider.calls[1]
    assert provider.calls[1]["thinking"] == {"type": "disabled"}
    assert provider.close_calls == 1
    assert result.usage.total_tokens == 30


@pytest.mark.asyncio
async def test_document_analysis_repairs_evidence_ledger_when_any_chunk_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records = [
        _record(0, "报告期为 2023 年。", page=1),
        _record(1, "净购入电力属于间接排放。", page=7),
    ]

    class EvidenceRepairProvider:
        def __init__(self):
            self.calls = []

        async def generate(self, **kwargs):
            self.calls.append(kwargs)
            if "企业能碳文档分析助手" in kwargs["system_prompt"]:
                content = _report()
            else:
                indexes = [1, 2] if "<必须修复的问题>" in kwargs["prompt"] else [1]
                content = json.dumps(
                    {
                        "chunks": [
                            {
                                "citation_index": index,
                                "disposition": "analyze",
                                "summary": f"片段 {index} 包含需分析内容。",
                                "issues": [],
                            }
                            for index in indexes
                        ]
                    },
                    ensure_ascii=False,
                )
            return GenerateResult(
                content=content,
                model="analysis-model",
                provider_type="test",
                latency_ms=5,
                usage=UsageInfo(prompt_tokens=10, completion_tokens=5, total_tokens=15),
                finish_reason="stop",
            )

        async def aclose(self):
            return None

    provider = EvidenceRepairProvider()

    async def fake_resolve(**_kwargs):
        return SimpleNamespace(provider=provider, model_name="analysis-model", config_id=23)

    monkeypatch.setattr(analysis_module, "aresolve_model", fake_resolve)
    monkeypatch.setattr(analysis_module.settings, "DOCUMENT_ANALYSIS_EVIDENCE_REPAIR_ATTEMPTS", 1)

    result = await DocumentAnalysisService().analyze(
        db=_Session(records),
        document=_document(),
        dataset=_dataset(),
        user_id=11,
    )

    assert result.analyzed_chunk_count == 2
    assert len(provider.calls) == 3
    assert "遗漏文档片段：2" in provider.calls[1]["prompt"]
    assert "[文档片段2]" in provider.calls[2]["prompt"]


@pytest.mark.asyncio
async def test_document_analysis_rejects_oversized_document_before_model_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(analysis_module.settings, "DOCUMENT_ANALYSIS_MAX_INPUT_TOKENS", 1)

    with pytest.raises(DocumentAnalysisTooLargeError):
        await DocumentAnalysisService().analyze(
            db=_Session([_record(0, "这是一段超过一个 token 的企业报告正文。")]),
            document=_document(),
            dataset=_dataset(),
            user_id=11,
        )


def test_document_analysis_normalizes_fence_and_invalid_citation_without_faking_sections() -> None:
    markdown, citations = _normalize_markdown(
        "```markdown\n## 1. 报告摘要\n\n有效。[文档片段1] 错误。[文档片段99]\n```",
        valid_source_count=2,
    )

    assert markdown.startswith("# 企业文档分析报告")
    assert "[文档片段1]" in markdown
    assert "[来源未核验]" in markdown
    assert "[文档片段99]" not in markdown
    assert "## 12. 结论与分析限制" not in markdown
    assert citations == {1}


def test_document_analysis_balances_batches_instead_of_leaving_a_tiny_tail() -> None:
    chunks = [
        SimpleNamespace(
            token_count=token_count,
            source=AnalysisSource(
                citation_index=index,
                chunk_id=f"chunk-{index}",
                chunk_index=index - 1,
                chunk_type="mixed",
                page=None,
                page_range=None,
                excerpt="",
            ),
        )
        for index, token_count in enumerate([370] * 30 + [740] * 3, start=1)
    ]

    batches = _partition_chunks(chunks, token_budget=12000, max_chunks=10)

    assert len(batches) == 4
    assert [sum(chunk.token_count for chunk in batch) for batch in batches] == [3330] * 4
    assert [batch[0].source.citation_index for batch in batches] == [1, 10, 19, 28]
    assert all(len(batch) <= 10 for batch in batches)


def test_evidence_parser_accepts_thinking_block_and_short_prefix() -> None:
    batch = [
        SimpleNamespace(
            source=AnalysisSource(
                citation_index=1,
                chunk_id="chunk-1",
                chunk_index=0,
                chunk_type="mixed",
                page=1,
                page_range=None,
                excerpt="",
            )
        )
    ]
    content = (
        "<think>先核对输出结构。</think>\n"
        "以下为结果：\n"
        '{"chunks":[{"citation_index":1,"disposition":"context",'
        '"summary":"封面信息。","issues":[]}]}'
    )

    items = _parse_evidence_output(content, batch=batch)

    assert len(items) == 1
    assert items[0].citation_index == 1
    assert items[0].disposition == "context"


@pytest.mark.asyncio
async def test_document_analysis_splits_only_truncated_evidence_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records = [_record(index, f"正文片段 {index + 1}。") for index in range(4)]

    class TruncatingProvider:
        def __init__(self):
            self.evidence_calls = []

        async def generate(self, **kwargs):
            if "企业能碳文档分析助手" in kwargs["system_prompt"]:
                content = _report()
                finish_reason = "stop"
            else:
                context = kwargs["prompt"].split("<文档片段>", 1)[1].split(
                    "</文档片段>", 1
                )[0]
                indexes = [int(value) for value in re.findall(r"\[文档片段(\d+)\]", context)]
                self.evidence_calls.append(indexes)
                if indexes == [3, 4]:
                    content = '{"chunks":[{"citation_index":3'
                    finish_reason = "length"
                else:
                    content = json.dumps(
                        {
                            "chunks": [
                                {
                                    "citation_index": index,
                                    "disposition": "context",
                                    "summary": f"片段 {index} 的上下文信息。",
                                    "issues": [],
                                }
                                for index in indexes
                            ]
                        },
                        ensure_ascii=False,
                    )
                    finish_reason = "stop"
            return GenerateResult(
                content=content,
                model="analysis-model",
                provider_type="test",
                latency_ms=5,
                usage=UsageInfo(prompt_tokens=10, completion_tokens=5, total_tokens=15),
                finish_reason=finish_reason,
            )

        async def aclose(self):
            return None

    provider = TruncatingProvider()

    async def fake_resolve(**_kwargs):
        return SimpleNamespace(provider=provider, model_name="analysis-model", config_id=23)

    monkeypatch.setattr(analysis_module, "aresolve_model", fake_resolve)
    monkeypatch.setattr(analysis_module.settings, "DOCUMENT_ANALYSIS_MAX_CHUNKS_PER_BATCH", 2)
    monkeypatch.setattr(analysis_module.settings, "DOCUMENT_ANALYSIS_EVIDENCE_SPLIT_MAX_DEPTH", 2)

    result = await DocumentAnalysisService().analyze(
        db=_Session(records),
        document=_document(),
        dataset=_dataset(),
        user_id=11,
    )

    assert provider.evidence_calls == [[1, 2], [3, 4], [3], [4]]
    assert result.evidence_batch_count == 3


@pytest.mark.asyncio
async def test_document_analysis_splits_batch_after_bounded_json_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records = [_record(index, f"正文片段 {index + 1}。") for index in range(2)]

    class InvalidJsonProvider:
        def __init__(self):
            self.evidence_calls = []

        async def generate(self, **kwargs):
            if "企业能碳文档分析助手" in kwargs["system_prompt"]:
                content = _report()
            else:
                context = kwargs["prompt"].split("<文档片段>", 1)[1].split(
                    "</文档片段>", 1
                )[0]
                indexes = [int(value) for value in re.findall(r"\[文档片段(\d+)\]", context)]
                self.evidence_calls.append(indexes)
                if len(indexes) > 1:
                    content = "不是 JSON"
                else:
                    content = json.dumps(
                        {
                            "chunks": [
                                {
                                    "citation_index": indexes[0],
                                    "disposition": "context",
                                    "summary": "上下文信息。",
                                    "issues": [],
                                }
                            ]
                        },
                        ensure_ascii=False,
                    )
            return GenerateResult(
                content=content,
                model="analysis-model",
                provider_type="test",
                latency_ms=5,
                usage=UsageInfo(prompt_tokens=10, completion_tokens=5, total_tokens=15),
                finish_reason="stop",
            )

        async def aclose(self):
            return None

    provider = InvalidJsonProvider()

    async def fake_resolve(**_kwargs):
        return SimpleNamespace(provider=provider, model_name="analysis-model", config_id=23)

    monkeypatch.setattr(analysis_module, "aresolve_model", fake_resolve)
    monkeypatch.setattr(analysis_module.settings, "DOCUMENT_ANALYSIS_MAX_CHUNKS_PER_BATCH", 2)
    monkeypatch.setattr(analysis_module.settings, "DOCUMENT_ANALYSIS_EVIDENCE_REPAIR_ATTEMPTS", 1)
    monkeypatch.setattr(analysis_module.settings, "DOCUMENT_ANALYSIS_EVIDENCE_SPLIT_MAX_DEPTH", 2)

    result = await DocumentAnalysisService().analyze(
        db=_Session(records),
        document=_document(),
        dataset=_dataset(),
        user_id=11,
    )

    assert provider.evidence_calls == [[1, 2], [1, 2], [1], [2]]
    assert result.evidence_batch_count == 2


@pytest.mark.asyncio
async def test_document_analysis_repairs_truncated_report_and_missing_batch_citation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records = [
        _record(0, "报告期为 2023 年。", page=1),
        _record(1, "能耗表标题没有写年度。", page=8),
    ]

    class RepairProvider:
        def __init__(self):
            self.calls = []
            self.close_calls = 0

        async def generate(self, **kwargs):
            self.calls.append(kwargs)
            if "企业能碳文档的证据提取助手" in kwargs["system_prompt"]:
                citation = 1 if "当前批次：1/2" in kwargs["prompt"] else 2
                content = json.dumps(
                    {
                        "chunks": [
                            {
                                "citation_index": citation,
                                "disposition": "analyze",
                                "summary": "当前片段包含需分析事实。",
                                "issues": [],
                            }
                        ]
                    },
                    ensure_ascii=False,
                )
                finish_reason = "stop"
            elif "<必须修复的问题>" in kwargs["prompt"]:
                content = _report()
                finish_reason = "stop"
            else:
                content = "# 企业文档分析报告\n\n## 1. 报告摘要\n\n仅使用末批。[文档片段2]"
                finish_reason = "length"
            return GenerateResult(
                content=content,
                model="analysis-model",
                provider_type="test",
                latency_ms=5,
                usage=UsageInfo(prompt_tokens=10, completion_tokens=5, total_tokens=15),
                finish_reason=finish_reason,
            )

        async def aclose(self):
            self.close_calls += 1

    provider = RepairProvider()

    async def fake_resolve(**_kwargs):
        return SimpleNamespace(provider=provider, model_name="analysis-model", config_id=23)

    monkeypatch.setattr(analysis_module, "aresolve_model", fake_resolve)
    monkeypatch.setattr(analysis_module.settings, "DOCUMENT_ANALYSIS_BATCH_TOKEN_BUDGET", 1)
    monkeypatch.setattr(analysis_module.settings, "DOCUMENT_ANALYSIS_REPORT_REPAIR_ATTEMPTS", 1)

    result = await DocumentAnalysisService().analyze(
        db=_Session(records),
        document=_document(),
        dataset=_dataset(),
        user_id=11,
    )

    assert result.evidence_batch_count == 2
    assert [source.citation_index for source in result.sources] == [1, 2]
    assert len(provider.calls) == 4
    assert "达到输出 token 上限" in provider.calls[-1]["prompt"]
    assert "未分析证据台账中要求纳入报告的文档片段：1" in provider.calls[-1]["prompt"]
    assert provider.close_calls == 1


@pytest.mark.asyncio
async def test_document_analysis_repairs_empty_report_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records = [_record(0, "报告期为 2023 年。", page=1)]

    class EmptyReportProvider:
        def __init__(self):
            self.report_calls = 0

        async def generate(self, **kwargs):
            if "企业能碳文档的证据提取助手" in kwargs["system_prompt"]:
                content = json.dumps(
                    {
                        "chunks": [
                            {
                                "citation_index": 1,
                                "disposition": "analyze",
                                "summary": "报告期为 2023 年。",
                                "issues": [],
                            }
                        ]
                    },
                    ensure_ascii=False,
                )
            else:
                self.report_calls += 1
                content = "" if self.report_calls == 1 else _report()
            return GenerateResult(
                content=content,
                model="analysis-model",
                provider_type="test",
                latency_ms=5,
                usage=UsageInfo(prompt_tokens=10, completion_tokens=5, total_tokens=15),
                finish_reason="stop",
            )

        async def aclose(self):
            return None

    provider = EmptyReportProvider()

    async def fake_resolve(**_kwargs):
        return SimpleNamespace(provider=provider, model_name="analysis-model", config_id=23)

    monkeypatch.setattr(analysis_module, "aresolve_model", fake_resolve)
    monkeypatch.setattr(analysis_module.settings, "DOCUMENT_ANALYSIS_REPORT_REPAIR_ATTEMPTS", 1)

    result = await DocumentAnalysisService().analyze(
        db=_Session(records),
        document=_document(),
        dataset=_dataset(),
        user_id=11,
    )

    assert provider.report_calls == 2
    assert result.markdown.startswith("# 企业文档分析报告")


class _MemoryStorage:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.removed: list[tuple[str, str]] = []

    def upload_bytes(self, bucket, object_key, content, content_type):
        assert content_type
        self.objects[(bucket, object_key)] = bytes(content)

    def download_to_path(self, bucket, object_key, dst):
        try:
            content = self.objects[(bucket, object_key)]
        except KeyError as exc:
            raise ClientError(
                {"Error": {"Code": "NoSuchKey", "Message": "missing"}},
                "GetObject",
            ) from exc
        dst.write_bytes(content)

    def remove_object(self, bucket, object_key):
        self.objects.pop((bucket, object_key), None)
        self.removed.append((bucket, object_key))
        return True


def _stored_result(markdown: str) -> analysis_module.DocumentAnalysisResult:
    return analysis_module.DocumentAnalysisResult(
        markdown=markdown,
        sources=[
            analysis_module.AnalysisSource(
                citation_index=1,
                chunk_id="chunk-0",
                chunk_index=0,
                chunk_type="mixed",
                page=1,
                page_range=None,
                excerpt="报告期为 2023 年。",
            )
        ],
        model_name="analysis-model",
        model_config_id=23,
        usage=UsageInfo(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        analyzed_chunk_count=2,
        evidence_batch_count=1,
        generated_at=datetime(2026, 8, 20, 1, 2, 3, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_document_analysis_store_persists_markdown_and_manifest() -> None:
    storage = _MemoryStorage()
    store = DocumentAnalysisStore(storage=storage)
    document = _document()
    result = _stored_result("# 企业文档分析报告\n\n持久化内容。[文档片段1]\n")

    await store.save(document=document, result=result)
    restored = await store.load(document=document)
    summary = await store.load_summary(document=document)

    analysis_prefix = "parsed/11/3/7/versions/v2/attempt-1/analysis"
    assert ("private", f"{analysis_prefix}/latest.json") in storage.objects
    markdown_keys = [key for bucket, key in storage.objects if key.endswith(".md")]
    assert len(markdown_keys) == 1
    assert markdown_keys[0].startswith(f"{analysis_prefix}/report-")
    assert restored.markdown == result.markdown
    assert restored.sources[0].excerpt == "报告期为 2023 年。"
    assert restored.generated_at == result.generated_at
    assert summary.model_name == "analysis-model"
    assert summary.source_count == 1
    assert summary.analyzed_chunk_count == 2
    assert summary.generated_at == result.generated_at


@pytest.mark.asyncio
async def test_document_analysis_store_switches_pointer_then_removes_old_report() -> None:
    storage = _MemoryStorage()
    store = DocumentAnalysisStore(storage=storage)
    document = _document()

    await store.save(document=document, result=_stored_result("# 企业文档分析报告\n\n第一版\n"))
    first_markdown_key = next(key for _, key in storage.objects if key.endswith(".md"))
    await store.save(document=document, result=_stored_result("# 企业文档分析报告\n\n第二版\n"))

    restored = await store.load(document=document)
    assert restored.markdown.endswith("第二版\n")
    assert ("private", first_markdown_key) in storage.removed
    assert ("private", first_markdown_key) not in storage.objects
