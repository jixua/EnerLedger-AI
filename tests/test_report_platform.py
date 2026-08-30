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

os.environ.setdefault("ADMIN_PASSWORD_HASH", "scrypt:test-only")

import app.api.reports as reports_api
from app.api.report_agent_internal import get_run_clarifications
from app.api.reports import ReportCreateRequest, create_report
from app.domain.models import Document, ReportQuestion, ReportRun
from app.rag.core.mq.messages import ReportGenerationMessage
from app.rag.models.db_models import LLMModelConfigDB
from app.services.report_agent_tokens import (
    ReportAgentTokenError,
    issue_report_agent_token,
    verify_report_agent_token,
)
from app.services.report_calculations import execute_registered_formula
from app.services.report_ir import (
    ReportEvidenceContext,
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


def test_pilot_templates_pass_technical_review_but_are_not_business_active() -> None:
    registry = ReportTemplateRegistry(_reporting_root())
    templates = {template.report_type: template for template in registry.list()}

    assert set(templates) == {f"R{index}" for index in range(1, 8)}
    for report_type in ("R1", "R2"):
        template = templates[report_type]
        assert template.review["technical_review"]["status"] == "PASSED"
        assert template.review["business_review"]["status"] == "PENDING"
        assert template.selectable is False


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
    object.__setattr__(template, "status", "ACTIVE")
    review = dict(template.review)
    review["business_review"] = {"status": "APPROVED"}
    object.__setattr__(template, "review", review)

    class Registry:
        def get(self, report_type, *, require_selectable=False):
            assert report_type == "R2"
            assert require_selectable is True
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
