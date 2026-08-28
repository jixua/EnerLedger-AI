#!/usr/bin/env python3
"""Validate versioned report-template assets without optional dependencies."""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPORT_TYPES = {f"R{index}" for index in range(1, 8)}
FIELD_STATUSES = {
    "FOUND",
    "CALCULATED",
    "USER_SUPPLIED",
    "MISSING",
    "CONFLICT",
    "NOT_APPLICABLE",
    "UNVERIFIED",
}
SKILL_NAME_PATTERN = re.compile(r"^[a-z0-9-]{1,64}$")


@dataclass(frozen=True)
class ValidationResult:
    template_count: int
    fixture_count: int
    skill_names: tuple[str, ...]


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{path}: invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{path}: root must be an object")
    return value


def _frontmatter(path: Path) -> dict[str, str]:
    content = path.read_text(encoding="utf-8")
    if not content.startswith("---\n"):
        raise ValueError(f"{path}: missing YAML frontmatter")
    try:
        raw = content.split("---\n", 2)[1]
    except IndexError as exc:
        raise ValueError(f"{path}: unterminated YAML frontmatter") from exc
    parsed: dict[str, str] = {}
    for line in raw.splitlines():
        if not line.strip():
            continue
        key, separator, value = line.partition(":")
        if not separator:
            raise ValueError(f"{path}: invalid frontmatter line: {line}")
        parsed[key.strip()] = value.strip()
    return parsed


def _require_unique(values: list[str], *, label: str, path: Path) -> set[str]:
    unique = set(values)
    if len(unique) != len(values):
        raise ValueError(f"{path}: duplicate {label}")
    return unique


def _validate_definition(path: Path, directory_name: str) -> tuple[dict[str, Any], set[str]]:
    definition = _load_json(path)
    required_keys = {
        "template_id",
        "report_type",
        "version",
        "name",
        "status",
        "fields",
        "sections",
        "calculations",
        "disclaimers",
        "render_profile",
    }
    missing = sorted(required_keys - set(definition))
    if missing:
        raise ValueError(f"{path}: missing keys: {', '.join(missing)}")
    if definition["template_id"] != directory_name:
        raise ValueError(f"{path}: template_id must match directory name")
    if definition["report_type"] not in REPORT_TYPES:
        raise ValueError(f"{path}: invalid report_type")
    if definition["status"] not in {"DRAFT", "ACTIVE", "RETIRED"}:
        raise ValueError(f"{path}: invalid status")
    if not re.fullmatch(r"\d+\.\d+\.\d+", str(definition["version"])):
        raise ValueError(f"{path}: version must be semantic x.y.z")

    fields = definition["fields"]
    sections = definition["sections"]
    calculations = definition["calculations"]
    if not isinstance(fields, list) or not fields:
        raise ValueError(f"{path}: fields must be a non-empty array")
    if not isinstance(sections, list) or not sections:
        raise ValueError(f"{path}: sections must be a non-empty array")
    if not isinstance(calculations, list):
        raise ValueError(f"{path}: calculations must be an array")

    field_ids = _require_unique(
        [str(item.get("id") or "") for item in fields],
        label="field id",
        path=path,
    )
    if "" in field_ids:
        raise ValueError(f"{path}: field id cannot be empty")
    section_ids = _require_unique(
        [str(item.get("id") or "") for item in sections],
        label="section id",
        path=path,
    )
    if "" in section_ids:
        raise ValueError(f"{path}: section id cannot be empty")

    for field in fields:
        if not isinstance(field.get("required"), bool) or not isinstance(
            field.get("blocking"), bool
        ):
            raise ValueError(f"{path}: field {field.get('id')} requires boolean flags")
        if not field.get("source_types"):
            raise ValueError(f"{path}: field {field.get('id')} has no source types")

    for section in sections:
        unknown = sorted(set(section.get("field_ids") or []) - field_ids)
        if unknown:
            raise ValueError(
                f"{path}: section {section.get('id')} references unknown fields: {unknown}"
            )

    formula_ids = _require_unique(
        [str(item.get("formula_id") or "") for item in calculations],
        label="formula id",
        path=path,
    )
    if "" in formula_ids:
        raise ValueError(f"{path}: formula id cannot be empty")
    for calculation in calculations:
        output = calculation.get("output_field_id")
        inputs = set(calculation.get("input_field_ids") or [])
        if output not in field_ids or not inputs or not inputs <= field_ids:
            raise ValueError(
                f"{path}: formula {calculation.get('formula_id')} has invalid field references"
            )
    for field in fields:
        formula_id = field.get("formula_id")
        if formula_id is not None and formula_id not in formula_ids:
            raise ValueError(f"{path}: field {field['id']} references unknown formula")
    return definition, field_ids


def validate_report_templates(root: Path) -> ValidationResult:
    common = root / "common"
    templates = root / "templates"
    for name in (
        "evidence.schema.json",
        "field-ledger.schema.json",
        "report-ir.schema.json",
        "template-definition.schema.json",
        "style-profile.schema.json",
        "evaluation-fixture.schema.json",
        "template-registry.schema.json",
        "template-review.schema.json",
    ):
        _load_json(common / name)
    common_skill_path = common / "evidence-grounded-report-generation" / "SKILL.md"
    common_skill = _frontmatter(common_skill_path)
    if not SKILL_NAME_PATTERN.fullmatch(common_skill.get("name", "")):
        raise ValueError(f"{common_skill_path}: invalid skill name")

    registry_path = templates / "registry.json"
    registry = _load_json(registry_path)
    registry_entries = registry.get("templates")
    if not isinstance(registry_entries, list) or len(registry_entries) != 7:
        raise ValueError(f"{registry_path}: expected 7 registry entries")
    registry_by_type = {str(item.get("report_type")): item for item in registry_entries}
    if set(registry_by_type) != REPORT_TYPES or len(registry_by_type) != 7:
        raise ValueError(f"{registry_path}: report types must be exactly R1-R7")

    template_directories = sorted(path for path in templates.iterdir() if path.is_dir())
    if len(template_directories) != 7:
        raise ValueError(f"{templates}: expected 7 template directories")

    report_types: set[str] = set()
    skill_names = {common_skill["name"]}
    fixture_count = 0
    for directory in template_directories:
        definition, field_ids = _validate_definition(directory / "definition.json", directory.name)
        report_type = str(definition["report_type"])
        if report_type in report_types:
            raise ValueError(f"{directory}: duplicate report_type {report_type}")
        report_types.add(report_type)

        registry_entry = registry_by_type[report_type]
        expected_definition_path = f"{directory.name}/definition.json"
        if (
            registry_entry.get("template_id") != definition.get("template_id")
            or registry_entry.get("version") != definition.get("version")
            or registry_entry.get("status") != definition.get("status")
            or registry_entry.get("definition_path") != expected_definition_path
        ):
            raise ValueError(f"{registry_path}: entry for {report_type} is inconsistent")

        skill = _frontmatter(directory / "SKILL.md")
        skill_name = skill.get("name", "")
        description = skill.get("description", "")
        if not SKILL_NAME_PATTERN.fullmatch(skill_name):
            raise ValueError(f"{directory / 'SKILL.md'}: invalid skill name")
        if skill_name != directory.name:
            raise ValueError(f"{directory / 'SKILL.md'}: skill name must match directory")
        if not description or report_type not in description:
            raise ValueError(
                f"{directory / 'SKILL.md'}: description must discriminate {report_type}"
            )
        if skill_name in skill_names:
            raise ValueError(f"{directory / 'SKILL.md'}: duplicate skill name")
        skill_names.add(skill_name)

        style = _load_json(directory / "style-profile.json")
        if style.get("profile_id") != definition.get("render_profile"):
            raise ValueError(f"{directory}: render_profile does not match style profile")
        references = directory / "references.md"
        if not references.is_file() or not references.read_text(encoding="utf-8").strip():
            raise ValueError(f"{references}: missing reference guidance")

        review_path = directory / "review.json"
        if review_path.is_file():
            review = _load_json(review_path)
            if review.get("template_id") != definition.get("template_id") or review.get(
                "template_version"
            ) != definition.get("version"):
                raise ValueError(f"{review_path}: review does not match template version")
            technical_status = (review.get("technical_review") or {}).get("status")
            business_status = (review.get("business_review") or {}).get("status")
            if technical_status not in {"PENDING", "PASSED", "FAILED"}:
                raise ValueError(f"{review_path}: invalid technical review status")
            if business_status not in {"PENDING", "APPROVED", "REJECTED"}:
                raise ValueError(f"{review_path}: invalid business review status")
        if definition["report_type"] in {"R1", "R2"} and not review_path.is_file():
            raise ValueError(f"{review_path}: pilot template requires review record")

        fixture_paths = sorted((directory / "fixtures").glob("*.json"))
        if not fixture_paths:
            raise ValueError(f"{directory}: at least one fixture is required")
        for fixture_path in fixture_paths:
            fixture = _load_json(fixture_path)
            if fixture.get("template_id") != definition.get("template_id"):
                raise ValueError(f"{fixture_path}: template_id mismatch")
            expected = fixture.get("expected") or {}
            statuses = expected.get("field_statuses") or {}
            unknown_fields = sorted(set(statuses) - field_ids)
            invalid_statuses = sorted(set(statuses.values()) - FIELD_STATUSES)
            clarification_fields = set(expected.get("clarification_field_ids") or [])
            if unknown_fields or not clarification_fields <= field_ids:
                raise ValueError(f"{fixture_path}: fixture references unknown fields")
            if invalid_statuses:
                raise ValueError(f"{fixture_path}: invalid field statuses {invalid_statuses}")
            fixture_count += 1

    if report_types != REPORT_TYPES:
        raise ValueError(f"{templates}: report types must be exactly R1-R7")
    return ValidationResult(
        template_count=len(template_directories),
        fixture_count=fixture_count,
        skill_names=tuple(sorted(skill_names)),
    )


def main() -> int:
    repository_root = Path(__file__).resolve().parents[1]
    try:
        result = validate_report_templates(repository_root / "reporting")
    except (OSError, ValueError) as exc:
        print(f"report template validation failed: {exc}", file=sys.stderr)
        return 1
    print(
        "report template validation passed: "
        f"templates={result.template_count}, fixtures={result.fixture_count}, "
        f"skills={len(result.skill_names)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
