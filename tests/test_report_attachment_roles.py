"""附件用途判定：模型判断 + 确定性回落。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

import app.services.report_attachment_roles as roles_service
from app.rag.core.prompts import parse_attachment_role_reply
from app.services.report_attachment_roles import (
    AttachmentRoleError,
    AttachmentRoles,
    decide_attachment_roles,
    resolve_declared_roles,
)


class _FakeSession:
    """只需要支持取正文节选的 scalars。"""

    def __init__(self, excerpts: dict[int, str] | None = None) -> None:
        self._excerpts = excerpts or {}

    async def scalars(self, _statement):
        values = list(self._excerpts.values())

        class Rows:
            def all(self):
                return values

        return Rows()


def _document(document_id: int, filename: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=document_id,
        filename=filename,
        version=1,
        user_id=1,
        dataset_id=1,
    )


# --- 解析模型回复 -----------------------------------------------------------


def test_parse_accepts_plain_json() -> None:
    assert parse_attachment_role_reply(
        '{"subject_document_id": 7, "template_document_id": 8}', allowed_ids={7, 8}
    ) == (7, 8)


def test_parse_accepts_fenced_json() -> None:
    reply = '```json\n{"subject_document_id": 8, "template_document_id": null}\n```'
    assert parse_attachment_role_reply(reply, allowed_ids={7, 8}) == (8, None)


def test_parse_ignores_ids_outside_the_attachment_set() -> None:
    # 模型编造了编号：视为未判定，交调用方回落
    assert parse_attachment_role_reply(
        '{"subject_document_id": 99, "template_document_id": 8}', allowed_ids={7, 8}
    ) == (None, None)


def test_parse_requires_a_subject() -> None:
    assert parse_attachment_role_reply(
        '{"subject_document_id": null, "template_document_id": 8}', allowed_ids={7, 8}
    ) == (None, None)


def test_parse_drops_a_template_equal_to_the_subject() -> None:
    assert parse_attachment_role_reply(
        '{"subject_document_id": 7, "template_document_id": 7}', allowed_ids={7, 8}
    ) == (7, None)


def test_parse_returns_unresolved_on_garbage() -> None:
    assert parse_attachment_role_reply("我无法判断", allowed_ids={7, 8}) == (None, None)


# --- 判定流程 ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_single_attachment_skips_the_model(monkeypatch) -> None:
    async def _unexpected(**_kwargs: Any):
        raise AssertionError("只有一份文件时不应调用模型")

    monkeypatch.setattr(roles_service, "aresolve_model", _unexpected)

    decision = await decide_attachment_roles(
        db=_FakeSession(), user_id=1, config_id=1, documents=[_document(7, "材料.pdf")]
    )

    assert decision == AttachmentRoles(subject_id=7, template_id=None, decided_by="single")


@pytest.mark.asyncio
async def test_model_decision_is_used_when_parseable(monkeypatch) -> None:
    class _Provider:
        async def generate(self, **_kwargs: Any) -> Any:
            return SimpleNamespace(
                content='{"subject_document_id": 7, "template_document_id": 8}'
            )

    async def _resolve(**_kwargs: Any) -> Any:
        return SimpleNamespace(provider=_Provider())

    closed: list[Any] = []

    async def _close(*_args: Any, **kwargs: Any) -> None:
        closed.append(kwargs.get("extra_models"))

    monkeypatch.setattr(roles_service, "aresolve_model", _resolve)
    monkeypatch.setattr(roles_service, "aclose_dataset_execution_contexts", _close)

    decision = await decide_attachment_roles(
        db=_FakeSession({7: "数据", 8: "报告"}),
        user_id=1,
        config_id=1,
        documents=[_document(7, "数据表.md"), _document(8, "模板报告.md")],
    )

    assert decision == AttachmentRoles(subject_id=7, template_id=8, decided_by="model")
    assert len(closed) == 1  # 模型解析出的 provider 必须释放


@pytest.mark.asyncio
async def test_falls_back_to_report_likeness_when_model_fails(monkeypatch) -> None:
    async def _resolve(**_kwargs: Any) -> Any:
        raise RuntimeError("provider 不可用")

    liked: dict[int, float] = {7: 2.0, 8: 9.0}

    async def _likeness(_db: Any, document: Any) -> float:
        return liked[int(document.id)]

    monkeypatch.setattr(roles_service, "aresolve_model", _resolve)
    monkeypatch.setattr(roles_service, "_report_likeness", _likeness)

    decision = await decide_attachment_roles(
        db=_FakeSession({7: "数据", 8: "报告"}),
        user_id=1,
        config_id=1,
        documents=[_document(7, "数据表.md"), _document(8, "模板报告.md")],
    )

    # 报告特征更明显的第 8 份是模板，第 7 份作为主体材料
    assert decision == AttachmentRoles(subject_id=7, template_id=8, decided_by="fallback")


@pytest.mark.asyncio
async def test_fallback_keeps_upload_order_when_scores_are_indistinguishable(monkeypatch) -> None:
    async def _resolve(**_kwargs: Any) -> Any:
        raise RuntimeError("provider 不可用")

    async def _likeness(_db: Any, _document: Any) -> float:
        return 0.0

    monkeypatch.setattr(roles_service, "aresolve_model", _resolve)
    monkeypatch.setattr(roles_service, "_report_likeness", _likeness)

    decision = await decide_attachment_roles(
        db=_FakeSession(),
        user_id=1,
        config_id=1,
        documents=[_document(7, "第一份.md"), _document(8, "第二份.md")],
    )

    assert decision == AttachmentRoles(subject_id=7, template_id=8, decided_by="fallback")


# --- 用户显式指明用途 -------------------------------------------------------


def test_declared_template_makes_the_other_file_the_subject() -> None:
    # 用户只点了「这份是模板」，另一份自然作为主体材料
    assert resolve_declared_roles(attachment_ids=[7, 8], declared={8: "TEMPLATE"}) == (7, 8)


def test_declared_subject_and_template_are_both_respected() -> None:
    assert resolve_declared_roles(
        attachment_ids=[7, 8], declared={7: "SOURCE", 8: "TEMPLATE"}
    ) == (7, 8)


def test_declared_subject_only_leaves_no_template() -> None:
    assert resolve_declared_roles(attachment_ids=[7, 8], declared={7: "SOURCE"}) == (7, None)


def test_two_declared_templates_are_rejected() -> None:
    with pytest.raises(AttachmentRoleError):
        resolve_declared_roles(attachment_ids=[7, 8], declared={7: "TEMPLATE", 8: "TEMPLATE"})


def test_two_declared_subjects_are_rejected() -> None:
    with pytest.raises(AttachmentRoleError):
        resolve_declared_roles(attachment_ids=[7, 8], declared={7: "SOURCE", 8: "SOURCE"})


def test_the_only_file_cannot_be_the_template() -> None:
    # 没有主体材料就没有可写入的事实来源
    with pytest.raises(AttachmentRoleError):
        resolve_declared_roles(attachment_ids=[7], declared={7: "TEMPLATE"})
