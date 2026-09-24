from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

import app.api.agent as agent_module
from app.rag.config import Settings
from app.rag.core.dataset_config.models import RecallConfig
from app.rag.core.llm.exceptions import ConfigurationException
from app.rag.core.pipeline.chunk_content import ChunkSource
from app.rag.core.pipeline.recall.models import RecallHit, RecallResponse
from app.services.agent_runs import AgentRunRegistry


def test_agent_is_disabled_by_default() -> None:
    assert Settings.model_fields["AGENT_ENABLED"].default is False
    assert Settings.model_fields["PI_SERVICE_BASE_URL"].default == "http://127.0.0.1:8010"


def test_internal_agent_auth_requires_exact_bearer_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "internal-agent-token-" + "x" * 32
    monkeypatch.setattr(agent_module.settings, "ENERLEDGER_INTERNAL_AGENT_TOKEN", token)

    assert agent_module._internal_authorized(f"Bearer {token}") is True
    assert agent_module._internal_authorized(f"Bearer {token}x") is False
    assert agent_module._internal_authorized(None) is False


def test_agent_stream_body_treats_missing_or_empty_dataset_ids_as_all_scope() -> None:
    assert agent_module.AgentStreamBody(query="你好").dataset_ids is None
    assert agent_module.AgentStreamBody(query="你好", dataset_ids=[]).dataset_ids == []


def test_agent_stream_body_keeps_legacy_history_bounded_to_ten_messages() -> None:
    history = [{"role": "user", "content": f"问题 {index}"} for index in range(11)]

    with pytest.raises(ValidationError):
        agent_module.AgentStreamBody(query="你好", history=history)


def test_agent_stream_body_accepts_two_uploads_plus_one_document_reference() -> None:
    """@ 引用的知识库文档与两次上传可以同轮并存：它不注入正文，不占正文预算。"""
    body = agent_module.AgentStreamBody(
        query="用 @伊顿碳足迹报告.docx 生成报告",
        attachments=[
            {"filename": "材料.docx", "material_id": "m-1"},
            {"filename": "模板.docx", "material_id": "m-2"},
            {"filename": "伊顿碳足迹报告.docx", "document_id": 7, "role": "SOURCE"},
        ],
    )

    assert [item.document_id for item in body.attachments] == [None, None, 7]
    assert body.attachments[2].role == "SOURCE"

    with pytest.raises(ValidationError):
        agent_module.AgentStreamBody(
            query="超出上限",
            attachments=[
                {"filename": "a.docx", "material_id": "m-1"},
                {"filename": "b.docx", "material_id": "m-2"},
                {"filename": "c.docx", "material_id": "m-3"},
                {"filename": "d.docx", "material_id": "m-4"},
            ],
        )


def test_agent_chat_model_requires_one_shared_binding_without_explicit_selection() -> None:
    shared = {
        1: SimpleNamespace(chat_config_id=9),
        2: SimpleNamespace(chat_config_id=9),
    }
    mixed = {
        1: SimpleNamespace(chat_config_id=9),
        2: SimpleNamespace(chat_config_id=10),
    }
    assert agent_module._chat_config_id(shared, None) == 9
    assert agent_module._chat_config_id(mixed, 12) == 12
    with pytest.raises(HTTPException) as raised:
        agent_module._chat_config_id(mixed, None)
    assert raised.value.detail == {"code": "CHAT_CONFIG_REQUIRED"}


@pytest.mark.asyncio
async def test_multi_knowledge_base_recall_skips_only_broken_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @asynccontextmanager
    async def fake_db_context():
        yield object()

    class FakeLoader:
        def __init__(self, _db) -> None:
            pass

        async def load(self, _user_id, dataset_id, _purpose):
            if dataset_id == 12:
                raise ConfigurationException("broken binding")
            return SimpleNamespace(dataset_id=dataset_id)

    monkeypatch.setattr(agent_module, "get_db_context", fake_db_context)
    monkeypatch.setattr(agent_module, "DatasetExecutionContextLoader", FakeLoader)

    config, contexts, failed = await agent_module._resolve_agent_recall_execution(7, [11, 12])

    assert config == RecallConfig.from_settings()
    assert list(contexts) == [11]
    assert failed == [12]


@pytest.mark.asyncio
async def test_internal_hybrid_recall_keeps_ranking_explanations_and_stable_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "internal-agent-token-" + "x" * 32
    monkeypatch.setattr(agent_module.settings, "ENERLEDGER_INTERNAL_AGENT_TOKEN", token)
    datasets = [
        SimpleNamespace(id=11, name="政策库", description="政策", status="ACTIVE"),
        SimpleNamespace(id=12, name="因子库", description="因子", status="ACTIVE"),
    ]

    class ScalarResult:
        def all(self):
            return datasets

    class FakeDb:
        async def scalars(self, _statement):
            return ScalarResult()

    response = RecallResponse(
        query="锅炉核算",
        hits=[
            RecallHit(
                chunk_id="chunk-1",
                doc_id=21,
                dataset_id=11,
                fused_score=0.8,
                scores={"bm25": 4.0, "sparse": 0.5, "dense": 0.9},
                normalized_scores={"bm25": 1.0, "sparse": 0.5, "dense": 0.8},
                weighted_contributions={"bm25": 0.15, "sparse": 0.075, "dense": 0.56},
            ),
            RecallHit(
                chunk_id="chunk-2",
                doc_id=22,
                dataset_id=12,
                fused_score=0.7,
                scores={"bm25": 3.0, "sparse": 0.4, "dense": 0.8},
            ),
            RecallHit(
                chunk_id="chunk-3",
                doc_id=23,
                dataset_id=11,
                fused_score=0.6,
                scores={"bm25": 2.0, "sparse": 0.3, "dense": 0.7},
            ),
        ],
        per_source_counts={"bm25": 1, "sparse": 1, "dense": 1},
        failed_sources=[],
        elapsed_ms=8,
        fusion_weights={"bm25": 0.15, "sparse": 0.15, "dense": 0.7},
    )

    class FakePipeline:
        async def execute(self, _request):
            return response

    async def fake_resolve(_user_id, _dataset_ids):
        config = RecallConfig.from_settings().model_copy(
            update={"rerank_top_n": 2, "recall_context_token_budget": 10_000}
        )
        return config, {11: object(), 12: object()}, []

    async def fake_sources(_chunk_ids, _user_id):
        return {
            "chunk-1": ChunkSource(
                content="燃料消耗量乘以适用排放因子。",
                filename="锅炉指南.pdf",
                chunk_index=3,
                document_version=2,
                page=9,
            ),
            "chunk-2": ChunkSource(
                content="排放因子应匹配燃料类型。",
                filename="因子指南.pdf",
                chunk_index=4,
                document_version=1,
                page=10,
            ),
            "chunk-3": ChunkSource(
                content="低排名候选不应进入回答上下文。",
                filename="锅炉指南.pdf",
                chunk_index=5,
                document_version=2,
                page=11,
            ),
        }

    monkeypatch.setattr(agent_module, "_resolve_agent_recall_execution", fake_resolve)
    monkeypatch.setattr(agent_module, "get_recall_pipeline", lambda: FakePipeline())
    monkeypatch.setattr(agent_module, "fetch_chunk_sources", fake_sources)
    monkeypatch.setattr(
        agent_module, "aclose_dataset_execution_contexts", lambda _contexts: _async_none()
    )

    context = await agent_module.agent_run_registry.register(
        run_id="tool-run",
        user_id=7,
        dataset_ids=[11, 12],
        doc_ids=None,
        scope_mode="all_accessible",
    )
    try:
        result = await agent_module.internal_agent_recall(
            "tool-run",
            agent_module.AgentRecallBody(query="锅炉核算"),
            authorization=f"Bearer {token}",
            db=FakeDb(),
        )
        repeated = await agent_module.internal_agent_recall(
            "tool-run",
            agent_module.AgentRecallBody(query="锅炉核算"),
            authorization=f"Bearer {token}",
            db=FakeDb(),
        )
    finally:
        await agent_module.agent_run_registry.release(context.run_id)

    hit = result["ranked_hits"][0]
    assert hit["result_rank"] == 1
    assert hit["knowledge_base_name"] == "政策库"
    assert hit["normalized_scores"]["dense"] == 0.8
    assert hit["weighted_contributions"]["dense"] == 0.56
    assert hit["selected_for_context"] is True
    assert len(result["ranked_hits"]) == 3
    assert result["retrieval"]["context_count"] == 2
    assert [item["selected_for_context"] for item in result["ranked_hits"]] == [True, True, False]
    assert result["retrieval"]["weights"]["dense"] == 0.7
    assert result["scope"] == {
        "mode": "all_accessible",
        "knowledge_base_count": 2,
        "policy": "system_cross_kb_v1",
    }
    assert repeated["ranked_hits"][0]["evidence_id"] == hit["evidence_id"]
    assert repeated["ranked_hits"][0]["citation_index"] == hit["citation_index"]


async def _async_none() -> None:
    return None


@pytest.mark.asyncio
async def test_agent_run_registry_scopes_and_releases_context() -> None:
    registry = AgentRunRegistry()
    context = await registry.register(
        run_id="opaque-run-id",
        user_id=7,
        dataset_ids=[11, 12],
        doc_ids=[21],
    )

    assert context.user_id == 7
    assert context.dataset_ids == (11, 12)
    assert context.doc_ids == (21,)
    assert context.scope_mode == "selected"
    refs = [ref for ref, _dataset_id in context.knowledge_base_refs]
    assert context.resolve_knowledge_base_refs(refs) == (11, 12)
    assert context.resolve_knowledge_base_refs(["kb_ref_unknown"]) is None
    assert await registry.get("opaque-run-id", max_age_seconds=60) == context

    first = await registry.register_evidence(
        "opaque-run-id",
        chunk_id="chunk-1",
        dataset_id=11,
        doc_id=21,
        document_version="1",
        selected_for_context=False,
    )
    selected = await registry.register_evidence(
        "opaque-run-id",
        chunk_id="chunk-1",
        dataset_id=11,
        doc_id=21,
        document_version="1",
        selected_for_context=True,
    )
    repeated = await registry.register_evidence(
        "opaque-run-id",
        chunk_id="chunk-1",
        dataset_id=11,
        doc_id=21,
        document_version="1",
        selected_for_context=True,
    )
    assert first is not None and first.citation_index is None
    assert selected is not None and selected.citation_index == 1
    assert repeated == selected
    assert first.evidence_id == selected.evidence_id

    assert await registry.get_evidence("opaque-run-id", selected.evidence_id) == selected
    document_ref = await registry.register_document_ref("opaque-run-id", 21)
    assert document_ref is not None
    assert await registry.register_document_ref("opaque-run-id", 21) == document_ref
    assert await registry.resolve_document_ref("opaque-run-id", document_ref) == 21
    section_ref = await registry.register_section_ref("opaque-run-id", 21, "heading-a")
    assert section_ref is not None
    assert await registry.resolve_section_ref("opaque-run-id", section_ref) == (
        21,
        "heading-a",
    )
    cursor = await registry.register_section_cursor("opaque-run-id", section_ref, 8)
    assert cursor is not None
    assert await registry.resolve_section_cursor("opaque-run-id", cursor) == (section_ref, 8)

    await registry.release("opaque-run-id")
    assert await registry.get("opaque-run-id", max_age_seconds=60) is None
    assert (
        await registry.register_evidence(
            "opaque-run-id",
            chunk_id="chunk-2",
            dataset_id=11,
            doc_id=21,
            document_version="1",
            selected_for_context=True,
        )
        is None
    )


@pytest.mark.asyncio
async def test_document_outline_and_section_read_use_opaque_refs_and_stable_citations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "internal-agent-token-" + "x" * 32
    monkeypatch.setattr(agent_module.settings, "ENERLEDGER_INTERNAL_AGENT_TOKEN", token)
    chunk_ids = [f"chunk-{index}" for index in range(1, 10)]

    async def fake_document_tree(_db, _context, doc_id):
        assert doc_id == 21
        return {
            "doc_id": 21,
            "dataset_id": 11,
            "original_filename": "核算指南.pdf",
            "root_chunk_ids": [],
            "headings": [
                {
                    "heading_key": "heading-a",
                    "title": "核算边界",
                    "heading_level": 1,
                    "direct_chunk_ids": chunk_ids,
                    "children": [],
                }
            ],
            "chunks": [],
        }

    async def fake_sources(requested_chunk_ids, _user_id):
        return {
            chunk_id: ChunkSource(
                content=f"正文 {chunk_id}",
                filename="核算指南.pdf",
                chunk_index=index,
                document_version=2,
                page=index,
            )
            for index, chunk_id in enumerate(requested_chunk_ids, start=1)
        }

    monkeypatch.setattr(agent_module, "_agent_document_tree", fake_document_tree)
    monkeypatch.setattr(agent_module, "fetch_chunk_sources", fake_sources)
    context = await agent_module.agent_run_registry.register(
        run_id="document-run",
        user_id=7,
        dataset_ids=[11],
        doc_ids=None,
    )
    evidence = await agent_module.agent_run_registry.register_evidence(
        context.run_id,
        chunk_id="seed-chunk",
        dataset_id=11,
        doc_id=21,
        document_version="2",
        selected_for_context=True,
    )
    assert evidence is not None
    try:
        outline = await agent_module.internal_agent_document_outline(
            context.run_id,
            agent_module.AgentDocumentOutlineBody(evidence_id=evidence.evidence_id),
            authorization=f"Bearer {token}",
            db=object(),
        )
        section_ref = outline["outline"][0]["section_ref"]
        first_page = await agent_module.internal_agent_read_section(
            context.run_id,
            agent_module.AgentReadSectionBody(section_ref=section_ref),
            authorization=f"Bearer {token}",
            db=object(),
        )
        second_page = await agent_module.internal_agent_read_section(
            context.run_id,
            agent_module.AgentReadSectionBody(
                section_ref=section_ref,
                cursor=first_page["coverage"]["next_cursor"],
            ),
            authorization=f"Bearer {token}",
            db=object(),
        )
    finally:
        await agent_module.agent_run_registry.release(context.run_id)

    assert outline["document_ref"].startswith("doc_ref_")
    assert section_ref.startswith("sec_ref_")
    assert "heading_key" not in outline["outline"][0]
    assert len(first_page["chunks"]) == 8
    assert first_page["coverage"]["has_more"] is True
    assert first_page["coverage"]["next_cursor"].startswith("cursor_")
    assert second_page["coverage"]["has_more"] is False
    assert second_page["chunks"][0]["citation_index"] == 10
