"""Versioned report-template registry and activation gate."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ReportTemplateError(ValueError):
    """Raised when a template is missing or inconsistent."""

    def __init__(self, message: str, *, code: str = "REPORT_TEMPLATE_INVALID") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class ReportTemplate:
    report_type: str
    template_id: str
    version: str
    status: str
    name: str
    definition: dict[str, Any]
    review: dict[str, Any] | None
    style_profile: dict[str, Any]
    common_skill: str
    report_skill: str
    asset_hash: str

    def to_public_dict(self) -> dict[str, Any]:
        fields = self.definition.get("fields") or []
        sections = self.definition.get("sections") or []
        return {
            "report_type": self.report_type,
            "template_id": self.template_id,
            "template_version": self.version,
            "name": self.name,
            "status": self.status,
            # Kept for API compatibility. Template status and review metadata are
            # informational and do not gate report creation.
            "selectable": True,
            "applicable_document_types": self.definition.get("applicable_documents", []),
            "required_field_count": sum(bool(field.get("required")) for field in fields),
            "blocking_field_count": sum(bool(field.get("blocking")) for field in fields),
            "sections": [
                {"id": section.get("id"), "title": section.get("title")} for section in sections
            ],
            "review": self.review,
        }

    def to_snapshot(self) -> dict[str, Any]:
        return deepcopy(
            {
                "report_type": self.report_type,
                "template_id": self.template_id,
                "version": self.version,
                "status": self.status,
                "name": self.name,
                "definition": self.definition,
                "review": self.review,
                "style_profile": self.style_profile,
                "common_skill": self.common_skill,
                "report_skill": self.report_skill,
                "asset_hash": self.asset_hash,
            }
        )

    @classmethod
    def from_snapshot(cls, snapshot: dict[str, Any]) -> ReportTemplate:
        expected_hash = _template_asset_hash(snapshot)
        if snapshot.get("asset_hash") != expected_hash:
            raise ReportTemplateError("报告任务中的模板快照哈希无效")
        return cls(
            report_type=str(snapshot["report_type"]),
            template_id=str(snapshot["template_id"]),
            version=str(snapshot["version"]),
            status=str(snapshot["status"]),
            name=str(snapshot["name"]),
            definition=dict(snapshot["definition"]),
            review=dict(snapshot["review"]) if snapshot.get("review") else None,
            style_profile=dict(snapshot["style_profile"]),
            common_skill=str(snapshot["common_skill"]),
            report_skill=str(snapshot["report_skill"]),
            asset_hash=str(snapshot["asset_hash"]),
        )


def _template_asset_hash(snapshot: dict[str, Any]) -> str:
    canonical = {
        key: snapshot.get(key)
        for key in (
            "report_type",
            "template_id",
            "version",
            "status",
            "name",
            "definition",
            "review",
            "style_profile",
            "common_skill",
            "report_skill",
        )
    }
    encoded = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class ReportTemplateRegistry:
    """Load immutable template definitions from repository-managed assets."""

    def __init__(self, reporting_root: Path | None = None) -> None:
        self.reporting_root = reporting_root or Path(__file__).resolve().parents[2] / "reporting"

    @staticmethod
    def _load_json(path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ReportTemplateError(f"报告模板文件无效：{path.name}") from exc
        if not isinstance(value, dict):
            raise ReportTemplateError(f"报告模板文件根节点必须为对象：{path.name}")
        return value

    def list(self) -> list[ReportTemplate]:
        registry_path = self.reporting_root / "templates" / "registry.json"
        registry = self._load_json(registry_path)
        templates: list[ReportTemplate] = []
        common_skill = (
            self.reporting_root / "common" / "evidence-grounded-report-generation" / "SKILL.md"
        ).read_text(encoding="utf-8")
        for entry in registry.get("templates") or []:
            directory = self.reporting_root / "templates" / str(entry["template_id"])
            definition = self._load_json(directory / "definition.json")
            review_path = directory / "review.json"
            review = self._load_json(review_path) if review_path.is_file() else None
            style_profile = self._load_json(directory / "style-profile.json")
            report_skill = (directory / "SKILL.md").read_text(encoding="utf-8")
            if (
                definition.get("report_type") != entry.get("report_type")
                or definition.get("template_id") != entry.get("template_id")
                or definition.get("version") != entry.get("version")
                or definition.get("status") != entry.get("status")
            ):
                raise ReportTemplateError(f"模板注册表与定义不一致：{entry.get('report_type')}")
            if review is not None and (
                review.get("template_id") != definition.get("template_id")
                or review.get("template_version") != definition.get("version")
            ):
                raise ReportTemplateError(f"模板评审版本与定义不一致：{entry.get('report_type')}")
            snapshot = {
                "report_type": str(definition["report_type"]),
                "template_id": str(definition["template_id"]),
                "version": str(definition["version"]),
                "status": str(definition["status"]),
                "name": str(definition["name"]),
                "definition": definition,
                "review": review,
                "style_profile": style_profile,
                "common_skill": common_skill,
                "report_skill": report_skill,
            }
            snapshot["asset_hash"] = _template_asset_hash(snapshot)
            templates.append(ReportTemplate.from_snapshot(snapshot))
        return templates

    def get(self, report_type: str) -> ReportTemplate:
        normalized = report_type.strip().upper()
        template = next(
            (item for item in self.list() if item.report_type == normalized),
            None,
        )
        if template is None:
            raise ReportTemplateError(
                f"未知报告类型：{normalized}", code="REPORT_TEMPLATE_NOT_FOUND"
            )
        return template


report_template_registry = ReportTemplateRegistry()
