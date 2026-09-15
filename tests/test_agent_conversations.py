from __future__ import annotations

from types import SimpleNamespace

from app.services.agent_conversations import (
    SHORT_TERM_TURN_LIMIT,
    build_short_term_history,
)
from app.services.report_template_classifier import classify_text


def test_short_term_memory_is_five_complete_turns() -> None:
    assert SHORT_TERM_TURN_LIMIT == 5


def test_short_term_memory_expands_latest_five_turns_in_chronological_order() -> None:
    turns = [
        SimpleNamespace(user_content=f"问题 {index}", assistant_content=f"回答 {index}")
        for index in range(1, 8)
    ]

    history = build_short_term_history(turns)

    assert history == [
        {"role": role, "content": f"{'问题' if role == 'user' else '回答'} {index}"}
        for index in range(3, 8)
        for role in ("user", "assistant")
    ]


def test_report_template_classifier_selects_clear_product_footprint_material() -> None:
    result = classify_text(
        "产品碳足迹核算材料，包含生命周期清单、功能单位、系统边界和原材料数据。",
        filename="产品碳足迹清单.docx",
    )

    assert result.state == "CONFIDENT"
    assert result.selected_report_type == "R1"
    assert result.candidates[0]["matched_terms"]


def test_report_template_classifier_returns_ranked_choices_when_ambiguous() -> None:
    result = classify_text("公司年度材料和若干统计数据", filename="年度材料.pdf")

    assert result.state == "AMBIGUOUS"
    assert result.selected_report_type is None
    assert len(result.candidates) == 3


def test_report_template_classifier_distinguishes_energy_audit_from_ghg_inventory() -> None:
    result = classify_text(
        "能源审计和节能诊断，统计能源账单、综合能耗、建筑面积、能耗强度及回收期。"
    )

    assert result.state == "CONFIDENT"
    assert result.selected_report_type == "R4"
