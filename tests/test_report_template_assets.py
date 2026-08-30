from __future__ import annotations

from pathlib import Path

from scripts.validate_report_templates import REPORT_TYPES, validate_report_templates


def test_report_template_assets_are_complete_and_consistent() -> None:
    repository_root = Path(__file__).resolve().parents[1]
    result = validate_report_templates(repository_root / "reporting")

    assert result.template_count == len(REPORT_TYPES) == 7
    assert result.fixture_count >= 7
    assert len(result.skill_names) == 8
