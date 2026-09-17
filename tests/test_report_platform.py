from __future__ import annotations

import hashlib
import json
import os
from collections import deque
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

os.environ.setdefault("ADMIN_PASSWORD_HASH", "scrypt:test-only")

import app.api.reports as reports_api
from app.api.report_agent_internal import _chunk_page, get_run_clarifications
from app.api.reports import ReportCreateRequest, create_report
from app.domain.models import Document, ReportQuestion, ReportRun
from app.rag.config import settings
from app.rag.core.mq.messages import ReportGenerationMessage
from app.rag.models.db_models import LLMModelConfigDB
from app.services.report_agent_tokens import (
    ReportAgentTokenError,
    issue_report_agent_token,
    verify_report_agent_token,
)
from app.services.report_budget import (
    ReportDocumentTooLargeError,
    assert_document_fits_context,
    estimate_document_tokens,
)
from app.services.report_calculations import execute_registered_formula
from app.services.report_ir import (
    ReportEvidenceContext,
    ReportIRPatchError,
    apply_report_ir_patch,
    build_fixture_report_ir,
    validate_report_ir,
)
from app.services.report_model_policy import (
    ReportModelEndpointError,
    validate_report_model_endpoint,
)
from app.services.report_source_context import ReportChunkManifest, validate_chunk_coverage
from app.services.report_templates import (
    ReportTemplate,
    ReportTemplateError,
    ReportTemplateRegistry,
)


def _reporting_root() -> Path:
    return Path(__file__).resolve().parents[1] / "reporting"


def _document() -> Document:
    return Document(
        id=7,
        dataset_id=3,
        user_id=11,
        filename="清单材料.docx",
        file_type="docx",
        file_size=100,
        raw_bucket="raw",
        raw_object_key="raw/report.docx",
        parsed_bucket="private",
        parsed_object_key="parsed/11/3/7/versions/v2/report.md",
        status="READY",
        version=2,
    )


async def _load_payload_stats(_db, *, document_id, document_version):
    """默认的文档规模统计：很小的文档，必然通过上下文预算检查。"""
    return 4, 1200


def _run() -> ReportRun:
    return ReportRun(
        id="run-1",
        user_id=11,
        dataset_id=3,
        document_id=7,
        document_version=2,
        parsed_bucket="private",
        parsed_object_key="parsed/report.md",
        report_type="R2",
        template_id="r2-organizational-ghg",
        template_version="1.0.0",
        mode="GENERATE",
        language="zh-CN",
        output_formats=["ONLINE"],
        llm_config_id=5,
        llm_snapshot_version=2,
        state="PROCESSING",
        stage="PREPARING",
        input_hash="a" * 64,
    )


def test_templates_are_selectable_regardless_of_review_status() -> None:
    registry = ReportTemplateRegistry(_reporting_root())
    templates = {template.report_type: template for template in registry.list()}

    assert set(templates) == {f"R{index}" for index in range(1, 8)}
    assert all(template.to_public_dict()["selectable"] is True for template in templates.values())
    for report_type in ("R1", "R2"):
        template = templates[report_type]
        assert template.review["technical_review"]["status"] == "PASSED"
        assert template.review["business_review"]["status"] == "PENDING"


def test_fixture_report_ir_requires_blocking_fields_and_tracks_user_answers() -> None:
    template = ReportTemplateRegistry(_reporting_root()).get("R2")
    run = _run()
    empty_ir = build_fixture_report_ir(run=run, template=template, answered_questions=[])
    empty_validation = validate_report_ir(empty_ir, run=run, template=template)

    assert empty_validation.schema_valid is True
    assert empty_validation.publishable is False
    assert any("organization_name" in warning for warning in empty_validation.warnings)

    values = {
        "organization_name": "示例组织",
        "reporting_year": 2025,
        "organizational_boundary_method": "运营控制法",
        "emission_factor_sources": ["国家主管部门公开因子"],
        "gwp_version": "IPCC AR6",
    }
    blocking_fields = [field for field in template.definition["fields"] if field["blocking"]]
    answers = [
        ReportQuestion(
            id=index,
            run_id=run.id,
            field_id=field["id"],
            question_type="MISSING_FIELD",
            question=field["label"],
            required=True,
            status="ANSWERED",
            answer={"value": values[field["id"]], "notes": None},
        )
        for index, field in enumerate(blocking_fields, start=1)
    ]
    answered_ir = build_fixture_report_ir(run=run, template=template, answered_questions=answers)
    answered_validation = validate_report_ir(answered_ir, run=run, template=template)

    assert answered_validation.schema_valid is True
    assert answered_validation.publishable is True
    organization = next(
        item for item in answered_ir["field_ledger"] if item["field_id"] == "organization_name"
    )
    assert organization["status"] == "USER_SUPPLIED"
    assert organization["evidence_ids"] == ["E-U-1"]


def test_custom_template_can_rename_and_reorder_only_baseline_sections() -> None:
    template = ReportTemplateRegistry(_reporting_root()).get("R2")
    run = _run()
    report_ir = build_fixture_report_ir(run=run, template=template, answered_questions=[])
    report_ir["sections"][0]["title"] = "用户模板封面"
    report_ir["sections"] = list(reversed(report_ir["sections"]))

    baseline_validation = validate_report_ir(report_ir, run=run, template=template)
    assert any("标题与模板不一致" in error for error in baseline_validation.errors)

    run.custom_template_manifest = {"document_id": 8, "document_version": 1}
    custom_validation = validate_report_ir(report_ir, run=run, template=template)
    assert not any("标题与模板不一致" in error for error in custom_validation.errors)

    report_ir["sections"].pop()
    incomplete_validation = validate_report_ir(report_ir, run=run, template=template)
    assert "报告章节必须与模板章节完整一致" in incomplete_validation.errors


def test_report_generation_message_only_carries_frozen_identity() -> None:
    message = ReportGenerationMessage.build(
        run_id="run-1", user_id=11, document_id=7, document_version=2
    )
    payload = ReportGenerationMessage.parse_msg(message.serialize())

    assert payload.run_id == "run-1"
    assert payload.document_version == 2
    assert "template" not in message.serialize()
    assert "parsed_object_key" not in message.serialize()


def test_report_agent_token_is_short_lived_and_bound_to_run_and_lease() -> None:
    secret = "s" * 40
    token = issue_report_agent_token(
        run_id="run-1", lease_token="lease-1", secret=secret, ttl_seconds=60
    )
    claims = verify_report_agent_token(token, run_id="run-1", secret=secret, now=1)

    assert claims.run_id == "run-1"
    assert claims.lease_token == "lease-1"
    with pytest.raises(ReportAgentTokenError):
        verify_report_agent_token(token, run_id="run-2", secret=secret, now=1)


def test_template_snapshot_is_self_contained_and_tamper_evident() -> None:
    template = ReportTemplateRegistry(_reporting_root()).get("R2")
    restored = ReportTemplate.from_snapshot(template.to_snapshot())
    assert restored.definition == template.definition
    assert restored.common_skill == template.common_skill
    assert restored.report_skill == template.report_skill

    tampered = deepcopy(template.to_snapshot())
    tampered["definition"]["name"] = "被篡改"
    with pytest.raises(ReportTemplateError):
        ReportTemplate.from_snapshot(tampered)


def test_registered_formula_operators_do_not_fall_back_to_sum() -> None:
    divide = {"operator": "DIVIDE", "input_field_ids": ["total", "quantity"]}
    percent = {"operator": "PERCENT", "input_field_ids": ["saved", "baseline"]}
    assert execute_registered_formula(divide, inputs={"total": 10, "quantity": 4}) == 2.5
    assert execute_registered_formula(percent, inputs={"saved": 25, "baseline": 100}) == 25


def test_blocking_not_applicable_and_forged_user_evidence_cannot_publish() -> None:
    template = ReportTemplateRegistry(_reporting_root()).get("R2")
    run = _run()
    report_ir = build_fixture_report_ir(run=run, template=template, answered_questions=[])
    organization = next(
        item for item in report_ir["field_ledger"] if item["field_id"] == "organization_name"
    )
    organization["status"] = "NOT_APPLICABLE"
    validation = validate_report_ir(report_ir, run=run, template=template)
    assert validation.publishable is False
    assert "organization_name" in validation.warnings[0]

    question = ReportQuestion(
        id=1,
        run_id=run.id,
        field_id="organization_name",
        question_type="MISSING_FIELD",
        question="组织名称",
        required=True,
        status="ANSWERED",
        answer={"value": "示例组织", "notes": None},
    )
    supplied = build_fixture_report_ir(
        run=run,
        template=template,
        answered_questions=[question],
    )
    context = ReportEvidenceContext(document_chunks={}, user_answer_hashes={1: "wrong"})
    strict = validate_report_ir(
        supplied,
        run=run,
        template=template,
        evidence_context=context,
    )
    assert any("真实用户回答" in error for error in strict.errors)


def test_chunk_coverage_requires_exact_order_hash_and_completion() -> None:
    items = (
        {"chunk_id": "c1", "chunk_index": 0, "content_hash": "a" * 64},
        {"chunk_id": "c2", "chunk_index": 1, "content_hash": "b" * 64},
    )
    encoded = json.dumps(items, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    manifest = ReportChunkManifest(
        items=items,
        content_hash=hashlib.sha256(encoded.encode()).hexdigest(),
    )
    valid = {"complete": True, "manifest_hash": manifest.content_hash, "chunks": list(items)}
    assert validate_chunk_coverage(valid, manifest) == []
    assert validate_chunk_coverage({**valid, "chunks": list(reversed(items))}, manifest)


def test_report_model_endpoint_policy_blocks_private_and_plain_http() -> None:
    validate_report_model_endpoint("https://api.example.com/v1")
    with pytest.raises(ReportModelEndpointError):
        validate_report_model_endpoint("http://127.0.0.1:8000/v1")


@pytest.mark.asyncio
async def test_pi_resume_context_exposes_answered_user_input_with_hash() -> None:
    question = ReportQuestion(
        id=9,
        run_id="run-1",
        field_id="organization_name",
        question_type="MISSING_FIELD",
        question="组织名称",
        required=True,
        status="ANSWERED",
        answer={"value": "示例组织", "notes": None},
        answered_by=11,
        answered_at=datetime(2026, 8, 28, tzinfo=UTC),
    )

    class Rows:
        def all(self):
            return [question]

    class Session:
        async def scalars(self, _statement):
            return Rows()

    result = await get_run_clarifications(run=_run(), db=Session())
    assert result["answered_count"] == 1
    assert result["items"][0]["evidence_source_type"] == "USER_INPUT"
    expected = hashlib.sha256(
        json.dumps("示例组织", ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    assert result["items"][0]["answer_content_hash"] == expected


class _Session:
    def __init__(self, values):
        self.values = deque(values)
        self.added = []

    async def scalar(self, _statement):
        return self.values.popleft()

    def add(self, value):
        self.added.append(value)

    async def commit(self):
        return None

    async def refresh(self, value):
        now = datetime(2026, 8, 28, tzinfo=UTC)
        value.created_at = now
        value.updated_at = now


@pytest.mark.asyncio
async def test_create_report_freezes_document_template_and_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    template = ReportTemplateRegistry(_reporting_root()).get("R2")

    class Registry:
        def get(self, report_type):
            assert report_type == "R2"
            return template

    dispatched = []

    class Dispatcher:
        async def dispatch(self, run):
            dispatched.append(run.id)
            return True

    monkeypatch.setattr(reports_api, "report_template_registry", Registry())
    monkeypatch.setattr(reports_api, "ReportRunDispatcher", Dispatcher)

    async def load_source_context(_db, *, run):
        assert run.document_version == 2
        return None, SimpleNamespace(items=({"chunk_id": "c1"},), content_hash="b" * 64)

    monkeypatch.setattr(reports_api, "load_report_source_context", load_source_context)
    monkeypatch.setattr(reports_api, "load_document_payload_stats", _load_payload_stats)
    model = LLMModelConfigDB(
        id=5,
        scope="USER",
        owner_user_id=11,
        provider_id=1,
        provider_type="openai",
        model_name="chat-model",
        capability="CHAT",
        protocol="openai",
        api_base_url="https://example.invalid/v1",
        api_key="encrypted",
        is_active=True,
        supports_tool_calling=True,
        snapshot_version=4,
    )
    session = _Session([_document(), model])

    response = await create_report(
        document_id=7,
        payload=ReportCreateRequest(
            report_type="R2",
            llm_config_id=5,
            reporting_year=2025,
            output_formats=["ONLINE"],
        ),
        user_id=11,
        db=session,
    )

    assert response["state"] == "PENDING"
    assert response["template_id"] == "r2-organizational-ghg"
    assert response["template_version"] == "1.0.0"
    assert response["document_version"] == 2
    assert response["llm_snapshot_version"] == 4
    assert len(session.added) == 1
    assert len(session.added[0].input_hash) == 64
    assert dispatched == [response["run_id"]]


@pytest.mark.asyncio
async def test_create_report_freezes_uploaded_template_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    template = ReportTemplateRegistry(_reporting_root()).get("R2")

    class Registry:
        def get(self, _report_type):
            return template

    class Dispatcher:
        async def dispatch(self, _run):
            return True

    monkeypatch.setattr(reports_api, "report_template_registry", Registry())
    monkeypatch.setattr(reports_api, "ReportRunDispatcher", Dispatcher)

    async def load_source_context(_db, *, run):
        return None, SimpleNamespace(items=({"chunk_id": "source-1"},), content_hash="b" * 64)

    async def load_custom_manifest(_db, *, document):
        assert document.id == 8
        return SimpleNamespace(items=({"chunk_id": "template-1"},), content_hash="c" * 64)

    monkeypatch.setattr(reports_api, "load_report_source_context", load_source_context)
    monkeypatch.setattr(reports_api, "load_document_payload_stats", _load_payload_stats)
    monkeypatch.setattr(reports_api, "load_document_chunk_manifest", load_custom_manifest)
    custom_template = Document(
        id=8,
        dataset_id=3,
        user_id=11,
        filename="组织碳盘查模板.docx",
        file_type="docx",
        file_size=80,
        raw_bucket="raw",
        raw_object_key="raw/template.docx",
        parsed_bucket="private",
        parsed_object_key="parsed/11/3/8/versions/v3/template.md",
        status="READY",
        version=3,
    )
    model = LLMModelConfigDB(
        id=5,
        scope="USER",
        owner_user_id=11,
        provider_id=1,
        provider_type="openai",
        model_name="chat-model",
        capability="CHAT",
        protocol="openai",
        api_base_url="https://example.invalid/v1",
        api_key="encrypted",
        is_active=True,
        supports_tool_calling=True,
        snapshot_version=4,
    )
    session = _Session([_document(), custom_template, model])

    response = await create_report(
        document_id=7,
        payload=ReportCreateRequest(
            report_type="R2",
            llm_config_id=5,
            custom_template_document_id=8,
        ),
        user_id=11,
        db=session,
    )

    run = session.added[0]
    assert response["custom_template_document_id"] == 8
    assert run.custom_template_document_version == 3
    assert run.custom_template_manifest == {
        "document_id": 8,
        "document_version": 3,
        "filename": "组织碳盘查模板.docx",
        "chunk_manifest_sha256": "c" * 64,
        "chunk_count": 1,
    }


@pytest.mark.asyncio
async def test_create_report_rejects_document_beyond_context_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """一轮读不完的文档必须在创建时就明确拒绝，而不是跑到一半截断失败。"""

    template = ReportTemplateRegistry(_reporting_root()).get("R2")

    class Registry:
        def get(self, report_type):
            return template

    monkeypatch.setattr(reports_api, "report_template_registry", Registry())

    async def load_payload_stats(_db, *, document_id, document_version):
        # 本地真实存在的一份 829 分片 / 144 万字符文档：约 72 万 tokens。
        return 829, 1_441_668

    monkeypatch.setattr(reports_api, "load_document_payload_stats", load_payload_stats)

    with pytest.raises(HTTPException) as excinfo:
        await create_report(
            document_id=7,
            payload=ReportCreateRequest(report_type="R2", llm_config_id=5),
            user_id=11,
            db=_Session([_document()]),
        )
    assert excinfo.value.status_code == 409
    assert excinfo.value.detail["code"] == "REPORT_DOCUMENT_TOO_LARGE"
    assert "请拆分文档" in excinfo.value.detail["message"]


def test_chunk_page_limits_page_by_char_budget() -> None:
    """分页必须按字符预算截断：分片正文长度差异极大，只按条数会让单次返回爆掉上下文。"""
    budget = settings.REPORT_AGENT_CHUNK_PAGE_MAX_CHARS

    def row(content: str):
        return SimpleNamespace(content=content)

    rows = [row("文" * (budget // 3)) for _ in range(10)]
    page, has_more = _chunk_page(rows, page_limit=10)
    assert len(page) == 2
    assert has_more is True

    page, has_more = _chunk_page(rows[:2], page_limit=10)
    assert len(page) == 2
    assert has_more is False


def test_chunk_page_always_returns_one_chunk_for_oversized_chunk() -> None:
    """单条超预算也必须返回，否则游标永远无法前进。"""
    budget = settings.REPORT_AGENT_CHUNK_PAGE_MAX_CHARS
    rows = [SimpleNamespace(content="文" * (budget * 3))]
    page, has_more = _chunk_page(rows, page_limit=10)
    assert len(page) == 1
    assert has_more is False


def test_document_budget_estimate_grows_with_content_and_rejects_oversized() -> None:
    small = estimate_document_tokens(content_chars=2_000, chunk_count=4)
    large = estimate_document_tokens(content_chars=400_000, chunk_count=200)
    assert small < large
    assert assert_document_fits_context(content_chars=2_000, chunk_count=4) == small
    with pytest.raises(ReportDocumentTooLargeError):
        assert_document_fits_context(content_chars=400_000, chunk_count=200)


def test_thinking_reserve_shrinks_prompt_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """开启思考后要少收一批文档：思考会随轮次累积进 prompt，且不受我们控制。"""
    document = {"content_chars": 50_000, "chunk_count": 70}

    monkeypatch.setattr(settings, "REPORT_AGENT_MODEL_THINKING", False)
    assert assert_document_fits_context(**document) > 0

    monkeypatch.setattr(settings, "REPORT_AGENT_MODEL_THINKING", True)
    with pytest.raises(ReportDocumentTooLargeError):
        assert_document_fits_context(**document)


@pytest.mark.asyncio
async def test_report_ir_draft_is_validated_from_storage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """候选 IR 只落库一次，validate 不再回传整份 IR（这是报告会话里最大的可省项）。"""
    from app.api import report_agent_internal as internal

    template = ReportTemplateRegistry(_reporting_root()).get("R2")
    run = _run()
    run.template_snapshot = template.to_snapshot()
    report_ir = build_fixture_report_ir(run=run, template=template, answered_questions=[])

    async def load_context(_db, *, run):
        return None, SimpleNamespace(items=())

    monkeypatch.setattr(internal, "load_report_source_context", load_context)

    class Session:
        async def commit(self):
            return None

    session = Session()
    receipt = await internal.save_report_ir_draft(
        payload=internal.ReportIRRequest(report_ir=report_ir), run=run, db=session
    )
    assert receipt["saved"] is True
    assert receipt["field_ledger_count"] == len(report_ir["field_ledger"])
    assert len(receipt["revision"]) == 12
    assert run.report_ir_draft == report_ir

    result = await internal.validate_candidate_report(
        payload=internal.ReportIRValidateRequest(), run=run, db=session
    )
    assert {"schema_valid", "publishable", "errors"} <= set(result)

    # 草稿缺失时必须明确报错，而不是拿旧内容校验或静默通过。
    run.report_ir_draft = None
    with pytest.raises(HTTPException) as excinfo:
        await internal.validate_candidate_report(
            payload=internal.ReportIRValidateRequest(), run=run, db=session
        )
    assert excinfo.value.status_code == 409
    assert excinfo.value.detail["code"] == "REPORT_IR_DRAFT_MISSING"


def test_report_ir_patch_updates_only_touched_items() -> None:
    """补丁只带变化条目：按主键覆盖/追加/删除，章节保序。"""
    draft = {
        "schema_version": 1,
        "meta": {"run_id": "run-1", "language": "zh-CN"},
        "evidence": [
            {"evidence_id": "E-1", "source_type": "DOCUMENT", "content_hash": "a" * 64},
            {"evidence_id": "E-2", "source_type": "DOCUMENT", "content_hash": "b" * 64},
        ],
        "field_ledger": [{"field_id": "f1", "status": "MISSING"}],
        "sections": [
            {"section_id": "s1", "title": "摘要", "blocks": []},
            {"section_id": "s2", "title": "范围", "blocks": []},
        ],
        "calculations": [],
    }
    updated, summary = apply_report_ir_patch(
        draft,
        {
            "upsert_evidence": [
                {"evidence_id": "E-2", "source_type": "USER_INPUT", "content_hash": "c" * 64},
                {"evidence_id": "E-3", "source_type": "DOCUMENT", "content_hash": "d" * 64},
            ],
            "delete_evidence": ["E-1"],
            "upsert_field_ledger": [{"field_id": "f2", "status": "FOUND"}],
            "upsert_sections": [{"section_id": "s1", "title": "摘要", "blocks": [{"type": "text"}]}],
            "limitations": ["草稿"],
        },
    )

    assert [item["evidence_id"] for item in updated["evidence"]] == ["E-2", "E-3"]
    assert updated["evidence"][0]["content_hash"] == "c" * 64
    assert [item["field_id"] for item in updated["field_ledger"]] == ["f1", "f2"]
    # 章节保序：s1 仍是第一位，内容被替换。
    assert [item["section_id"] for item in updated["sections"]] == ["s1", "s2"]
    assert updated["sections"][0]["blocks"] == [{"type": "text"}]
    assert updated["limitations"] == ["草稿"]
    assert summary["evidence"] == {"added": 1, "replaced": 1, "removed": 1, "total": 2}
    # 原草稿不被就地修改，便于回滚。
    assert [item["evidence_id"] for item in draft["evidence"]] == ["E-1", "E-2"]


def test_report_ir_patch_rejects_empty_or_keyless_items() -> None:
    draft = {"evidence": [], "field_ledger": [], "sections": []}
    with pytest.raises(ReportIRPatchError):
        apply_report_ir_patch(draft, {"upsert_evidence": []})
    with pytest.raises(ReportIRPatchError):
        apply_report_ir_patch(draft, {"upsert_evidence": [{"source_type": "DOCUMENT"}]})


@pytest.mark.asyncio
async def test_report_ir_patch_endpoint_applies_to_stored_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """落库后的修补直接改变后续校验看到的草稿。"""
    from app.api import report_agent_internal as internal

    template = ReportTemplateRegistry(_reporting_root()).get("R2")
    run = _run()
    run.template_snapshot = template.to_snapshot()
    report_ir = build_fixture_report_ir(run=run, template=template, answered_questions=[])
    run.report_ir_draft = report_ir

    async def load_context(_db, *, run):
        return None, SimpleNamespace(items=())

    monkeypatch.setattr(internal, "load_report_source_context", load_context)

    class Session:
        async def commit(self):
            return None

    session = Session()
    section_id = report_ir["sections"][0]["section_id"]
    receipt = await internal.patch_report_ir_draft(
        payload=internal.ReportIRPatchRequest(delete_sections=[section_id]),
        run=run,
        db=session,
    )
    assert receipt["saved"] is True
    assert receipt["changed"]["sections"]["removed"] == 1
    assert section_id not in [
        section["section_id"] for section in run.report_ir_draft["sections"]
    ]

    result = await internal.validate_candidate_report(
        payload=internal.ReportIRValidateRequest(), run=run, db=session
    )
    assert "报告章节必须与模板章节完整一致" in result["errors"]

    # 空补丁必须被拒绝，避免模型用无效调用浪费一轮。
    with pytest.raises(HTTPException) as excinfo:
        await internal.patch_report_ir_draft(
            payload=internal.ReportIRPatchRequest(),
            run=run,
            db=session,
        )
    assert excinfo.value.status_code == 422
    assert excinfo.value.detail["code"] == "REPORT_IR_PATCH_INVALID"
