"""Deterministic ReportIR construction and validation boundaries."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from jsonschema import Draft202012Validator

from app.domain.models import ReportQuestion, ReportRun
from app.services.report_calculations import ReportCalculationError, execute_registered_formula
from app.services.report_templates import ReportTemplate

FIELD_STATUSES = {
    "FOUND",
    "CALCULATED",
    "USER_SUPPLIED",
    "MISSING",
    "CONFLICT",
    "NOT_APPLICABLE",
    "UNVERIFIED",
}

# 可增量修补的集合：草稿键 → (upsert 参数名, delete 参数名, 主键字段)。
# 修补单位是「整个条目」而不是字段路径：校验错误本身按 evidence_id / field_id /
# section_id 定位，按条目替换既贴合错误信息，也不需要模型理解路径语义。
IR_PATCH_COLLECTIONS: dict[str, tuple[str, str, str]] = {
    "evidence": ("upsert_evidence", "delete_evidence", "evidence_id"),
    "field_ledger": ("upsert_field_ledger", "delete_field_ledger", "field_id"),
    "sections": ("upsert_sections", "delete_sections", "section_id"),
}
# 整体替换的顶层键（体积小，或本就不适合按条目合并）。
IR_PATCH_SCALAR_KEYS = ("calculations", "warnings", "limitations", "render_profile")


class ReportIRPatchError(RuntimeError):
    code = "REPORT_IR_PATCH_INVALID"


@dataclass(frozen=True, slots=True)
class ReportIRValidation:
    schema_valid: bool
    publishable: bool
    errors: tuple[str, ...]
    warnings: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_valid": self.schema_valid,
            "publishable": self.publishable,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True, slots=True)
class ReportEvidenceContext:
    document_chunks: dict[str, dict[str, Any]]
    user_answer_hashes: dict[int, str]
    allowed_reference_uris: frozenset[str] = frozenset()


@lru_cache(maxsize=1)
def _report_ir_schema_validator() -> Draft202012Validator:
    root = Path(__file__).resolve().parents[2] / "reporting" / "common"
    report_schema = json.loads((root / "report-ir.schema.json").read_text(encoding="utf-8"))
    report_schema["properties"]["evidence"]["items"] = json.loads(
        (root / "evidence.schema.json").read_text(encoding="utf-8")
    )
    report_schema["properties"]["field_ledger"]["items"] = json.loads(
        (root / "field-ledger.schema.json").read_text(encoding="utf-8")
    )
    return Draft202012Validator(report_schema)


def report_ir_contract_schema() -> dict[str, Any]:
    """返回校验实际使用的 ReportIR JSON Schema（evidence / field_ledger 已内联）。

    交给报告 Agent，让它在构造 ReportIR 前就看到精确字段名与取值枚举，
    避免按猜测的字段名（如 evidence 的 value/answer/content）反复被校验拒绝。
    """
    return copy.deepcopy(_report_ir_schema_validator().schema)


def apply_report_ir_patch(
    draft: dict[str, Any], patch: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """在已有草稿上应用增量补丁，返回 (新草稿, 变更摘要)。

    修补语义：按主键 upsert / delete；未出现的集合保持原样；章节保序（新章节追加）。
    补丁只承载发生变化的部分，避免模型为修一条证据而重发整份 ReportIR。
    """
    if not isinstance(draft, dict):
        raise ReportIRPatchError("草稿不存在，请先整份落库候选 ReportIR")
    updated = copy.deepcopy(draft)
    summary: dict[str, Any] = {}

    for key, (upsert_key, delete_key, id_field) in IR_PATCH_COLLECTIONS.items():
        items = updated.get(key)
        if not isinstance(items, list):
            items = []
        items = [item for item in items if isinstance(item, dict)]
        positions = {str(item.get(id_field)): index for index, item in enumerate(items)}

        added = replaced = 0
        for item in patch.get(upsert_key) or []:
            if not isinstance(item, dict):
                raise ReportIRPatchError(f"{upsert_key} 的每一项都必须是对象")
            item_id = str(item.get(id_field) or "").strip()
            if not item_id:
                raise ReportIRPatchError(f"{upsert_key} 的每一项都必须带 {id_field}")
            if item_id in positions:
                items[positions[item_id]] = item
                replaced += 1
            else:
                positions[item_id] = len(items)
                items.append(item)
                added += 1

        deleted_ids = {str(raw).strip() for raw in patch.get(delete_key) or []}
        removed = sum(
            1 for item in items if str(item.get(id_field)) in deleted_ids
        )
        if removed:
            items = [item for item in items if str(item.get(id_field)) not in deleted_ids]

        updated[key] = items
        if added or replaced or removed:
            summary[key] = {
                "added": added,
                "replaced": replaced,
                "removed": removed,
                "total": len(items),
            }

    for key in IR_PATCH_SCALAR_KEYS:
        if patch.get(key) is not None:
            updated[key] = patch[key]
            summary[key] = "replaced"

    meta_patch = patch.get("meta")
    if isinstance(meta_patch, dict) and meta_patch:
        meta = dict(updated.get("meta") or {})
        meta.update(meta_patch)
        updated["meta"] = meta
        summary["meta"] = sorted(str(key) for key in meta_patch)

    if not summary:
        raise ReportIRPatchError("补丁为空：请至少提供一个待修补的集合或字段")
    return updated, summary


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def validate_report_field_value(field: dict[str, Any], value: Any) -> list[str]:
    field_id = str(field["id"])
    field_type = field.get("type")
    validation = field.get("validation") or {}
    errors: list[str] = []
    valid_type = True
    if field_type in {"string", "text", "url", "date"}:
        valid_type = isinstance(value, str) and bool(value.strip())
    elif field_type == "date_range":
        valid_type = (
            isinstance(value, dict)
            and isinstance(value.get("start"), str)
            and isinstance(value.get("end"), str)
        ) or (
            isinstance(value, list) and len(value) == 2 and all(isinstance(v, str) for v in value)
        )
    elif field_type == "integer":
        valid_type = isinstance(value, int) and not isinstance(value, bool)
    elif field_type == "number":
        valid_type = _is_number(value)
    elif field_type == "enum":
        valid_type = isinstance(value, str) and value in set(validation.get("enum") or [])
    elif field_type == "array":
        valid_type = isinstance(value, list)
    if not valid_type:
        return [f"字段 {field_id} 的值不符合 {field_type} 类型"]
    if _is_number(value):
        if "minimum" in validation and value < validation["minimum"]:
            errors.append(f"字段 {field_id} 小于最小值")
        if "exclusive_minimum" in validation and value <= validation["exclusive_minimum"]:
            errors.append(f"字段 {field_id} 必须大于最小值")
        if "maximum" in validation and value > validation["maximum"]:
            errors.append(f"字段 {field_id} 大于最大值")
    if isinstance(value, str):
        if len(value) < int(validation.get("min_length", 0)):
            errors.append(f"字段 {field_id} 文本长度不足")
        pattern = validation.get("pattern")
        if pattern and re.fullmatch(pattern, value) is None:
            errors.append(f"字段 {field_id} 格式不匹配")
        digits = validation.get("digits")
        if digits and (not value.isdigit() or len(value) != int(digits)):
            errors.append(f"字段 {field_id} 必须为 {digits} 位数字")
        if validation.get("quantity_required") and re.search(r"\d", value) is None:
            errors.append(f"字段 {field_id} 必须包含明确数量")
        if field_type == "date":
            try:
                date.fromisoformat(value)
            except ValueError:
                errors.append(f"字段 {field_id} 必须使用 ISO 日期")
        if field_type == "url":
            parsed = urlparse(value)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                errors.append(f"字段 {field_id} 必须使用有效 HTTP(S) URL")
    if field_type == "date_range" and valid_type:
        start = value.get("start") if isinstance(value, dict) else value[0]
        end = value.get("end") if isinstance(value, dict) else value[1]
        try:
            if date.fromisoformat(start) > date.fromisoformat(end):
                errors.append(f"字段 {field_id} 的开始日期不能晚于结束日期")
        except ValueError:
            errors.append(f"字段 {field_id} 必须使用 ISO 日期范围")
    if isinstance(value, list):
        if "min_items" in validation and len(value) < int(validation["min_items"]):
            errors.append(f"字段 {field_id} 数组项不足")
        if "max_items" in validation and len(value) > int(validation["max_items"]):
            errors.append(f"字段 {field_id} 数组项过多")
        required_keys = set(validation.get("item_required_keys") or [])
        if required_keys and any(
            not isinstance(item, dict) or not required_keys <= set(item) for item in value
        ):
            errors.append(f"字段 {field_id} 数组项缺少必要字段")
    return errors


def _contains_prohibited_claim(text: str) -> bool:
    normalized = re.sub(r"\s+", "", text)
    patterns = (
        r"AI已完成(?:审计|认证|核查|验证)",
        r"经AI(?:审计|认证|核查|验证)",
        r"本报告已通过(?:审计|认证|核查|SBTi验证)",
        r"保证(?:完全)?符合(?:所有)?(?:法律|法规|合规要求)",
    )
    return any(re.search(pattern, normalized, re.IGNORECASE) for pattern in patterns)


def _answer_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def build_fixture_report_ir(
    *,
    run: ReportRun,
    template: ReportTemplate,
    answered_questions: list[ReportQuestion],
) -> dict[str, Any]:
    """Build a non-LLM fixture IR used to verify orchestration and resume behavior."""

    # 直传材料没有文档可指，这两个值就是空——IR 的 meta 允许为空，校验时也按空比对。
    document_id = int(run.document_id) if run.document_id is not None else None
    document_version = int(run.document_version) if run.document_version is not None else None
    answered_by_field = {
        question.field_id: question
        for question in answered_questions
        if question.status == "ANSWERED" and question.answer is not None
    }
    evidence: list[dict[str, Any]] = []
    field_ledger: list[dict[str, Any]] = []
    for field in template.definition.get("fields") or []:
        field_id = str(field["id"])
        question = answered_by_field.get(field_id)
        if question is None:
            field_ledger.append(
                {
                    "field_id": field_id,
                    "status": "MISSING",
                    "value": None,
                    "unit": field.get("unit"),
                    "evidence_ids": [],
                    "confidence": 0.0,
                    "notes": ["平台 Fixture 未执行文档抽取。"],
                    "calculation": None,
                }
            )
            continue
        value = question.answer.get("value")
        evidence_id = f"E-U-{question.id}"
        evidence.append(
            {
                "evidence_id": evidence_id,
                "source_type": "USER_INPUT",
                "document_id": document_id,
                "document_version": document_version,
                "chunk_id": None,
                "page": None,
                "reference_uri": None,
                "excerpt": None,
                "content_hash": _answer_hash(value),
                "metadata": {"question_id": int(question.id)},
            }
        )
        field_ledger.append(
            {
                "field_id": field_id,
                "status": "USER_SUPPLIED",
                "value": value,
                "unit": field.get("unit"),
                "evidence_ids": [evidence_id],
                "confidence": 1.0,
                "notes": ["该值由用户在报告任务中补充。"],
                "calculation": None,
            }
        )

    return {
        "schema_version": 1,
        "meta": {
            "run_id": str(run.id),
            "report_type": run.report_type,
            "template_id": run.template_id,
            "template_version": run.template_version,
            "document_id": document_id,
            "document_version": document_version,
            "source_filename": run.inline_source_filename,
            "language": run.language,
        },
        "evidence": evidence,
        "field_ledger": field_ledger,
        "sections": [
            {"section_id": section["id"], "title": section["title"], "blocks": []}
            for section in template.definition.get("sections") or []
        ],
        "calculations": [],
        "warnings": ["这是平台链路 Fixture，不包含 LLM 文档分析结果。"],
        "limitations": [
            "当前产物仅用于验证任务、模板、补充问题和 ReportIR 契约。",
            "不得作为审计、核查、认证或合规结论。",
        ],
        "render_profile": template.definition["render_profile"],
    }


# 摘录逐字校验会撞上的「形状差异」：解析器留下的标记、全角半角标点、空白换行。
# 这些只在形状上不同，文字是同一段，不该被判成不实引用。
_MARKUP_MARKERS = re.compile(r"<!--.*?-->|\[(?:表格|图片|图表)引用:[^\]]*\]")
_PUNCTUATION_SHAPE = str.maketrans(
    {
        "，": ",",
        "。": ".",
        "：": ":",
        "；": ";",
        "（": "(",
        "）": ")",
        "、": ",",
        "！": "!",
        "？": "?",
        "“": '"',
        "”": '"',
        "‘": "'",
        "’": "'",
        "《": "<",
        "》": ">",
        "—": "-",
        "－": "-",
        "～": "~",
        "　": " ",
    }
)
_EMPHASIS_MARKERS = str.maketrans("", "", "*`_#")


def excerpt_skeleton(value: Any) -> str:
    """只留文字、不留形状：去掉解析标记、强调符号、标点与空白后的那一串字。

    用来判断摘录是否出自同一段原文。刻意只抹形状不抹文字——比对的是「连续的一段
    字」，所以编出来的句子照样过不了；但标点被规整过、换行被并成一句、解析留下的
    ``**`` 与 ``[表格引用: …]`` 被略去，都不再算问题。
    """
    if value is None:
        return ""
    plain = _MARKUP_MARKERS.sub("", str(value))
    plain = plain.translate(_PUNCTUATION_SHAPE).translate(_EMPHASIS_MARKERS)
    return re.sub(r"\s+", "", plain)


def excerpt_belongs_to_chunk(excerpt: Any, content: Any) -> bool:
    """摘录是否确实出自这段原文。

    严格逐字比对会把「同一句话、标点不同」判成不实引用，而模型照抄时顺手整理标点、
    去掉强调标记是常态——一次实跑里 19 条摘录全部因此被打回，模型只能回头重抄一遍。
    这里改成先抹平形状再比：约束的实质没变（摘录必须来自那一分片），但不再因为标点
    形状让人白跑一轮。
    """
    quote = excerpt_skeleton(excerpt)
    if not quote:
        return True
    return quote in excerpt_skeleton(content)


def validate_report_ir(
    report_ir: dict[str, Any],
    *,
    run: ReportRun,
    template: ReportTemplate,
    evidence_context: ReportEvidenceContext | None = None,
) -> ReportIRValidation:
    errors: list[str] = []
    warnings: list[str] = []
    for schema_error in sorted(
        _report_ir_schema_validator().iter_errors(report_ir),
        key=lambda item: list(item.absolute_path),
    ):
        path = ".".join(str(part) for part in schema_error.absolute_path) or "$"
        errors.append(f"ReportIR Schema {path}: {schema_error.message}")
    meta = report_ir.get("meta") or {}
    frozen_meta = {
        "run_id": str(run.id),
        "report_type": run.report_type,
        "template_id": run.template_id,
        "template_version": run.template_version,
        # 直传材料期望值就是空；agent 若凭空编一个 document_id，会被这里拦下。
        "document_id": int(run.document_id) if run.document_id is not None else None,
        "document_version": (
            int(run.document_version) if run.document_version is not None else None
        ),
    }
    for key, expected in frozen_meta.items():
        if meta.get(key) != expected:
            errors.append(f"ReportIR meta.{key} 与冻结任务不一致")

    expected_fields = {str(field["id"]): field for field in template.definition.get("fields") or []}
    ledger = report_ir.get("field_ledger")
    if not isinstance(ledger, list):
        errors.append("field_ledger 必须为数组")
        ledger = []
    ledger_by_id = {str(item.get("field_id")): item for item in ledger if isinstance(item, dict)}
    if len(ledger_by_id) != len(ledger) or set(ledger_by_id) != set(expected_fields):
        errors.append("field_ledger 必须完整且每个模板字段只能出现一次")

    evidence = report_ir.get("evidence")
    if not isinstance(evidence, list):
        errors.append("evidence 必须为数组")
        evidence = []
    evidence_items = [item for item in evidence if isinstance(item, dict)]
    evidence_ids = [str(item.get("evidence_id")) for item in evidence_items]
    if len(evidence_ids) != len(set(evidence_ids)):
        errors.append("evidence_id 不能重复")
    evidence_id_set = set(evidence_ids)
    evidence_by_id = {str(item.get("evidence_id")): item for item in evidence_items}
    # 直传材料来源没有文档可指，冻结值就是空；证据里的 document_id 也按空比对，
    # 凭空编一个数字会被下面的检查拦下。
    frozen_document_id = int(run.document_id) if run.document_id is not None else None
    frozen_document_version = (
        int(run.document_version) if run.document_version is not None else None
    )
    if evidence_context is not None:
        for item in evidence_items:
            evidence_id = str(item.get("evidence_id"))
            source_type = item.get("source_type")
            if source_type == "DOCUMENT":
                chunk_id = str(item.get("chunk_id") or "")
                chunk = evidence_context.document_chunks.get(chunk_id)
                if (
                    chunk is None
                    or item.get("document_id") != frozen_document_id
                    or item.get("document_version") != frozen_document_version
                    or item.get("content_hash") != chunk.get("content_hash")
                ):
                    errors.append(f"证据 {evidence_id} 不是冻结文档分片的真实引用")
                excerpt = item.get("excerpt")
                if excerpt and chunk is not None and not excerpt_belongs_to_chunk(
                    excerpt, chunk.get("content")
                ):
                    errors.append(
                        f"证据 {evidence_id} 摘录不属于对应文档分片"
                        "（须逐字取自该分片，不要改写或拼接）"
                    )
            elif source_type == "USER_INPUT":
                question_id = (item.get("metadata") or {}).get("question_id")
                expected_hash = evidence_context.user_answer_hashes.get(question_id)
                if expected_hash is None or item.get("content_hash") != expected_hash:
                    errors.append(f"证据 {evidence_id} 不是当前任务的真实用户回答")
            elif source_type == "KNOWLEDGE_BASE":
                if item.get("reference_uri") not in evidence_context.allowed_reference_uris:
                    errors.append(f"证据 {evidence_id} 不是本次检索返回的规范知识引用")
            elif source_type == "CALCULATION" and not (item.get("metadata") or {}).get(
                "formula_id"
            ):
                errors.append(f"证据 {evidence_id} 缺少公式标识")

    blocking_missing: list[str] = []
    for field_id, item in ledger_by_id.items():
        status = item.get("status")
        if status not in FIELD_STATUSES:
            errors.append(f"字段 {field_id} 状态无效")
        item_evidence = set(item.get("evidence_ids") or [])
        if not item_evidence <= evidence_id_set:
            errors.append(f"字段 {field_id} 引用了不存在的证据")
        if status in {"FOUND", "CALCULATED", "USER_SUPPLIED"} and not item_evidence:
            errors.append(f"字段 {field_id} 有值状态必须绑定证据")
        field = expected_fields.get(field_id) or {}
        if status in {"FOUND", "CALCULATED", "USER_SUPPLIED"}:
            if item.get("value") is None:
                errors.append(f"字段 {field_id} 有值状态不能使用空值")
            else:
                errors.extend(validate_report_field_value(field, item.get("value")))
            if item.get("unit") != field.get("unit"):
                errors.append(f"字段 {field_id} 单位与模板不一致")
            evidence_source_types = {
                evidence_by_id[evidence_id].get("source_type")
                for evidence_id in item_evidence
                if evidence_id in evidence_by_id
            }
            if not evidence_source_types <= set(field.get("source_types") or []):
                errors.append(f"字段 {field_id} 使用了模板不允许的证据来源")
            if status == "USER_SUPPLIED" and evidence_source_types != {"USER_INPUT"}:
                errors.append(f"字段 {field_id} USER_SUPPLIED 状态必须只绑定用户输入")
            if status == "CALCULATED" and "CALCULATION" not in evidence_source_types:
                errors.append(f"字段 {field_id} CALCULATED 状态必须绑定计算证据")
        elif item.get("value") is not None:
            errors.append(f"字段 {field_id} 的无值状态不能携带确定值")
        if field.get("blocking") and status in {
            "MISSING",
            "CONFLICT",
            "UNVERIFIED",
            "NOT_APPLICABLE",
        }:
            blocking_missing.append(field_id)

    expected_sections = {
        str(section["id"]) for section in template.definition.get("sections") or []
    }
    sections = report_ir.get("sections")
    if not isinstance(sections, list):
        errors.append("sections 必须为数组")
        sections = []
    section_ids = [
        str(section.get("section_id")) for section in sections if isinstance(section, dict)
    ]
    if len(section_ids) != len(set(section_ids)) or set(section_ids) != expected_sections:
        errors.append("报告章节必须与模板章节完整一致")
    section_definitions = {
        str(section["id"]): section for section in template.definition.get("sections") or []
    }
    for section in sections:
        if not isinstance(section, dict):
            continue
        section_id = str(section.get("section_id"))
        definition = section_definitions.get(section_id) or {}
        if (
            run.custom_template_manifest is None
            and section.get("title") != definition.get("title")
        ):
            errors.append(f"章节 {section_id} 标题与模板不一致")
        for block in section.get("blocks") or []:
            block_evidence = set(block.get("evidence_ids") or [])
            if not block_evidence <= evidence_id_set:
                errors.append(f"章节 {section_id} 引用了不存在的证据")
            if block.get("type") not in set(definition.get("allowed_blocks") or []):
                errors.append(f"章节 {section_id} 使用了模板不允许的内容块")
            if _contains_prohibited_claim(str(block.get("text") or "")):
                errors.append(f"章节 {section_id} 包含禁止的认证、审计或合规声明")

    calculations = report_ir.get("calculations") or []
    calculations_by_output = {
        str(item.get("output_field_id")): item for item in calculations if isinstance(item, dict)
    }
    formulas_by_output = {
        str(item["output_field_id"]): item for item in template.definition.get("calculations") or []
    }
    for field_id, ledger_item in ledger_by_id.items():
        if ledger_item.get("status") != "CALCULATED":
            continue
        formula = formulas_by_output.get(field_id)
        calculation = calculations_by_output.get(field_id)
        if formula is None or calculation is None:
            errors.append(f"计算字段 {field_id} 缺少注册公式执行记录")
            continue
        if (
            calculation.get("formula_id") != formula.get("formula_id")
            or calculation.get("formula_version") != formula.get("version")
            or calculation.get("operator") != formula.get("operator")
            or calculation.get("input_field_ids") != formula.get("input_field_ids")
        ):
            errors.append(f"计算字段 {field_id} 的公式元数据不一致")
            continue
        expected_inputs = {
            input_id: ledger_by_id.get(input_id, {}).get("value")
            for input_id in formula.get("input_field_ids") or []
        }
        if calculation.get("inputs") != expected_inputs:
            errors.append(f"计算字段 {field_id} 的输入值与 FieldLedger 不一致")
            continue
        try:
            expected_value = execute_registered_formula(
                formula,
                inputs=expected_inputs,
                parameters=calculation.get("parameters") or {},
            )
            actual_value = Decimal(str(calculation.get("value")))
            ledger_value = Decimal(str(ledger_item.get("value")))
        except (ReportCalculationError, ValueError, TypeError):
            errors.append(f"计算字段 {field_id} 无法按注册公式重算")
            continue
        if expected_value != actual_value or expected_value != ledger_value:
            errors.append(f"计算字段 {field_id} 的结果与确定性重算不一致")
        if calculation.get("unit") != expected_fields[field_id].get("unit"):
            errors.append(f"计算字段 {field_id} 的输出单位不一致")
        if not set(calculation.get("parameter_evidence_ids") or []) <= evidence_id_set:
            errors.append(f"计算字段 {field_id} 的公式参数证据不存在")
    if report_ir.get("render_profile") != template.definition.get("render_profile"):
        errors.append("render_profile 与冻结模板不一致")
    if not report_ir.get("limitations"):
        errors.append("报告必须包含限制说明")
    if blocking_missing:
        warnings.append("仍有阻塞字段待补充：" + ", ".join(sorted(blocking_missing)))
    return ReportIRValidation(
        schema_valid=not errors,
        publishable=not errors and not blocking_missing,
        errors=tuple(errors),
        warnings=tuple(warnings),
    )
