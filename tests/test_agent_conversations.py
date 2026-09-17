from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.agent_conversations import delete_conversation
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


class _FakeSession:
    """记录删除语句的会话替身：只需要 scalar / execute / commit 三个动作。"""

    def __init__(self, owned_id: str | None) -> None:
        self._owned_id = owned_id
        self.executed: list[object] = []
        self.commits = 0

    async def scalar(self, _statement: object) -> str | None:
        return self._owned_id

    async def execute(self, statement: object) -> None:
        self.executed.append(statement)

    async def commit(self) -> None:
        self.commits += 1


def _compiled(statement: object) -> str:
    return str(statement.compile())  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_delete_conversation_removes_turns_before_the_conversation() -> None:
    session = _FakeSession("conv-1")

    await delete_conversation("conv-1", user_id=7, db=session)

    # 先清回合再删会话：两张表之间没有数据库级外键
    assert [statement.table.name for statement in session.executed] == [
        "agent_conversation_turn",
        "agent_conversation",
    ]
    assert all("user_id" in _compiled(statement) for statement in session.executed)
    assert session.commits == 1


@pytest.mark.asyncio
async def test_delete_conversation_keeps_report_runs() -> None:
    """报告是独立交付物：删对话不应连带删掉报告任务。"""

    session = _FakeSession("conv-1")

    await delete_conversation("conv-1", user_id=7, db=session)

    assert all(statement.table.name != "report_run" for statement in session.executed)


@pytest.mark.asyncio
async def test_delete_conversation_rejects_conversation_of_another_user() -> None:
    session = _FakeSession(None)

    with pytest.raises(HTTPException) as failure:
        await delete_conversation("conv-of-someone-else", user_id=7, db=session)

    assert failure.value.status_code == 404
    assert session.executed == []
    assert session.commits == 0
