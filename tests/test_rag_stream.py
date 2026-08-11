from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

import app.api.rag as rag_module
from app.rag.core.llm.response import StreamChunk, UsageInfo
from app.rag.core.pipeline.chunk_content import ChunkSource
from app.rag.core.pipeline.recall import RecallHit, RecallRequest, RecallResponse


class _FakeRecallPipeline:
    def __init__(self, response: RecallResponse) -> None:
        self.response = response
        self.requests: list[RecallRequest] = []

    async def execute(self, request: RecallRequest) -> RecallResponse:
        self.requests.append(request)
        return self.response


class _FakeChatProvider:
    def __init__(self, chunks: list[StreamChunk]) -> None:
        self.chunks = chunks
        self.calls: list[dict[str, str]] = []
        self.close_calls = 0

    async def stream(self, *, prompt: str, system_prompt: str):
        self.calls.append({"prompt": prompt, "system_prompt": system_prompt})
        for chunk in self.chunks:
            yield chunk

    async def aclose(self) -> None:
        self.close_calls += 1


class _SlowChatProvider(_FakeChatProvider):
    async def stream(self, *, prompt: str, system_prompt: str):
        self.calls.append({"prompt": prompt, "system_prompt": system_prompt})
        yield StreamChunk(delta="部分回答")
        await asyncio.sleep(1)


def _decode_sse(frame: str) -> tuple[str, dict]:
    assert frame.endswith("\n\n")
    lines = frame.rstrip("\n").splitlines()
    event = next(line.removeprefix("event: ") for line in lines if line.startswith("event: "))
    data = next(line.removeprefix("data: ") for line in lines if line.startswith("data: "))
    return event, json.loads(data)


def _recall_request() -> RecallRequest:
    return RecallRequest(
        query="该报告的能碳核算边界是什么？",
        user_id=17,
        dataset_ids=[23],
    )


@pytest.mark.asyncio
async def test_event_stream_passes_recall_context_and_emits_answer_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hit = RecallHit(
        chunk_id="chunk-1",
        doc_id=31,
        dataset_id=23,
        fused_score=0.91,
        scores={"bm25": 4.2, "sparse": 0.7, "dense": 0.91},
    )
    response = RecallResponse(
        query="该报告的能碳核算边界是什么？",
        hits=[hit],
        per_source_counts={"bm25": 1, "sparse": 1, "dense": 1},
        failed_sources=[],
        elapsed_ms=19,
    )
    pipeline = _FakeRecallPipeline(response)
    provider = _FakeChatProvider(
        [
            StreamChunk(delta="核算边界包括组织边界和运营边界。"),
            StreamChunk(
                delta="",
                is_end=True,
                usage=UsageInfo(prompt_tokens=21, completion_tokens=9, total_tokens=30),
            ),
        ]
    )
    fetch_calls: list[tuple[list[str], int]] = []

    async def fake_fetch(chunk_ids: list[str], user_id: int) -> dict[str, ChunkSource]:
        fetch_calls.append((chunk_ids, user_id))
        return {
            "chunk-1": ChunkSource(
                content="报告将组织边界与运营边界共同作为能碳核算边界。",
                filename="边界报告.pdf",
                chunk_index=7,
            )
        }

    monkeypatch.setattr(rag_module, "get_recall_pipeline", lambda: pipeline)
    monkeypatch.setattr(rag_module, "fetch_chunk_sources", fake_fetch)

    recall_request = _recall_request()
    frames = [
        frame
        async for frame in rag_module._event_stream(
            request_id="request-1",
            recall_request=recall_request,
            resolved_chat=SimpleNamespace(provider=provider),
        )
    ]
    events = [_decode_sse(frame) for frame in frames]

    assert [name for name, _ in events] == [
        "stream_started",
        "recall_done",
        "answer_delta",
        "answer_done",
    ]
    assert events[0][1] == {"request_id": "request-1"}
    assert events[2][1] == {"text": "核算边界包括组织边界和运营边界。"}
    assert events[3][1]["answer"] == "核算边界包括组织边界和运营边界。"
    assert events[3][1]["usage"] == {
        "prompt_tokens": 21,
        "completion_tokens": 9,
        "total_tokens": 30,
    }
    assert events[3][1]["hits"][0]["content"] == ("报告将组织边界与运营边界共同作为能碳核算边界。")
    assert events[3][1]["hits"][0] == {
        "chunk_id": "chunk-1",
        "doc_id": 31,
        "dataset_id": 23,
        "result_rank": 1,
        "fused_score": 0.91,
        "scores": {"bm25": 4.2, "sparse": 0.7, "dense": 0.91},
        "filename": "边界报告.pdf",
        "page": None,
        "page_range": None,
        "chunk_index": 7,
        "document_version": 1,
        "citation_index": 1,
        "content": "报告将组织边界与运营边界共同作为能碳核算边界。",
    }
    assert events[3][1]["elapsed_ms"] == 19
    assert pipeline.requests == [recall_request]
    assert fetch_calls == [(["chunk-1"], 17)]

    assert len(provider.calls) == 1
    prompt = provider.calls[0]["prompt"]
    assert "[片段1] 报告将组织边界与运营边界共同作为能碳核算边界。" in prompt
    assert "该报告的能碳核算边界是什么？" in prompt
    assert provider.calls[0]["system_prompt"] == rag_module.RAG_GENERATION_SYSTEM_PROMPT
    assert "[片段N]" in provider.calls[0]["system_prompt"]
    assert "N 只能是参考片段中真实存在的阿拉伯数字编号" in provider.calls[0]["system_prompt"]
    assert provider.close_calls == 1


@pytest.mark.asyncio
async def test_event_stream_citation_indexes_match_budgeted_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hits = [
        RecallHit(
            chunk_id="chunk-empty",
            doc_id=31,
            dataset_id=23,
            fused_score=0.93,
            scores={"bm25": 4.5},
        ),
        RecallHit(
            chunk_id="chunk-kept",
            doc_id=31,
            dataset_id=23,
            fused_score=0.91,
            scores={"bm25": 4.2},
        ),
        RecallHit(
            chunk_id="chunk-truncated",
            doc_id=31,
            dataset_id=23,
            fused_score=0.82,
            scores={"bm25": 3.8},
        ),
    ]
    pipeline = _FakeRecallPipeline(
        RecallResponse(
            query="问题",
            hits=hits,
            per_source_counts={"bm25": 3},
            failed_sources=[],
            elapsed_ms=5,
        )
    )
    provider = _FakeChatProvider(
        [
            StreamChunk(delta="回答"),
            StreamChunk(delta="", is_end=True),
        ]
    )

    async def fake_fetch(chunk_ids: list[str], user_id: int) -> dict[str, ChunkSource]:
        return {
            "chunk-empty": ChunkSource(
                content="  \n\t",
                filename="空片段.pdf",
                chunk_index=0,
            ),
            "chunk-kept": ChunkSource(
                content="实际纳入上下文的片段",
                filename="核算报告.pdf",
                chunk_index=1,
            ),
            "chunk-truncated": ChunkSource(
                content="因预算被截断的片段",
                filename="核算报告.pdf",
                chunk_index=2,
            ),
        }

    monkeypatch.setattr(rag_module, "get_recall_pipeline", lambda: pipeline)
    monkeypatch.setattr(rag_module, "fetch_chunk_sources", fake_fetch)
    monkeypatch.setattr(rag_module.settings, "RECALL_GENERATION_CONTEXT_TOKEN_BUDGET", 1)

    events = [
        _decode_sse(frame)
        async for frame in rag_module._event_stream(
            request_id="request-budget",
            recall_request=_recall_request(),
            resolved_chat=SimpleNamespace(provider=provider),
        )
    ]

    answer_done = events[-1][1]
    assert [hit["citation_index"] for hit in answer_done["hits"]] == [None, 1, None]
    assert [hit["chunk_index"] for hit in answer_done["hits"]] == [0, 1, 2]
    prompt = provider.calls[0]["prompt"]
    assert "[片段1] 实际纳入上下文的片段" in prompt
    assert "空片段" not in prompt
    assert "因预算被截断的片段" not in prompt


@pytest.mark.asyncio
async def test_event_stream_emits_recall_done_without_calling_provider_when_no_hits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = RecallResponse(
        query="没有答案的问题",
        hits=[],
        per_source_counts={"bm25": 0, "sparse": 0, "dense": 0},
        failed_sources=["sparse"],
        elapsed_ms=7,
    )
    pipeline = _FakeRecallPipeline(response)
    provider = _FakeChatProvider([])
    fetch_calls: list[tuple[list[str], int]] = []

    async def fake_fetch(chunk_ids: list[str], user_id: int) -> dict[str, ChunkSource]:
        fetch_calls.append((chunk_ids, user_id))
        return {}

    monkeypatch.setattr(rag_module, "get_recall_pipeline", lambda: pipeline)
    monkeypatch.setattr(rag_module, "fetch_chunk_sources", fake_fetch)

    recall_request = RecallRequest(query="没有答案的问题", user_id=17, dataset_ids=[23])
    events = [
        _decode_sse(frame)
        async for frame in rag_module._event_stream(
            request_id="request-empty",
            recall_request=recall_request,
            resolved_chat=None,
        )
    ]

    assert [name for name, _ in events] == [
        "stream_started",
        "recall_done",
        "answer_delta",
        "answer_done",
    ]
    assert events[1][1] == {
        "request_id": "request-empty",
        "hits": [],
        "failed_sources": ["sparse"],
    }
    assert events[2][1] == {"text": rag_module.NO_CONTEXT_ANSWER}
    assert events[3][1] == {
        "request_id": "request-empty",
        "answer": rag_module.NO_CONTEXT_ANSWER,
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        "hits": [],
        "failed_sources": ["sparse"],
        "elapsed_ms": 7,
    }
    assert pipeline.requests == [recall_request]
    assert fetch_calls == [([], 17)]
    assert provider.calls == []
    assert provider.close_calls == 0


@pytest.mark.asyncio
async def test_event_stream_rejects_provider_eof_without_terminal_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hit = RecallHit(
        chunk_id="chunk-1",
        doc_id=31,
        dataset_id=23,
        fused_score=0.91,
        scores={"bm25": 4.2, "sparse": 0.7, "dense": 0.91},
    )
    pipeline = _FakeRecallPipeline(
        RecallResponse(
            query="问题",
            hits=[hit],
            per_source_counts={"bm25": 1, "sparse": 1, "dense": 1},
            failed_sources=[],
            elapsed_ms=3,
        )
    )
    provider = _FakeChatProvider([StreamChunk(delta="不完整回答", is_end=False)])

    async def fake_fetch(chunk_ids: list[str], user_id: int) -> dict[str, ChunkSource]:
        return {
            "chunk-1": ChunkSource(
                content="召回正文",
                filename="召回资料.pdf",
                chunk_index=0,
            )
        }

    monkeypatch.setattr(rag_module, "get_recall_pipeline", lambda: pipeline)
    monkeypatch.setattr(rag_module, "fetch_chunk_sources", fake_fetch)

    events = [
        _decode_sse(frame)
        async for frame in rag_module._event_stream(
            request_id="request-incomplete",
            recall_request=_recall_request(),
            resolved_chat=SimpleNamespace(provider=provider),
        )
    ]

    assert [name for name, _ in events] == [
        "stream_started",
        "recall_done",
        "answer_delta",
        "error",
    ]
    assert events[-1][1]["code"] == "GENERATION_INCOMPLETE"
    assert provider.close_calls == 1


@pytest.mark.asyncio
async def test_event_stream_enforces_generation_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hit = RecallHit(
        chunk_id="chunk-1",
        doc_id=31,
        dataset_id=23,
        fused_score=0.91,
        scores={"bm25": 4.2, "sparse": 0.7, "dense": 0.91},
    )
    pipeline = _FakeRecallPipeline(
        RecallResponse(
            query="问题",
            hits=[hit],
            per_source_counts={"bm25": 1, "sparse": 1, "dense": 1},
            failed_sources=[],
            elapsed_ms=3,
        )
    )
    provider = _SlowChatProvider([])

    async def fake_fetch(chunk_ids: list[str], user_id: int) -> dict[str, ChunkSource]:
        return {
            "chunk-1": ChunkSource(
                content="召回正文",
                filename="召回资料.pdf",
                chunk_index=0,
            )
        }

    monkeypatch.setattr(rag_module, "get_recall_pipeline", lambda: pipeline)
    monkeypatch.setattr(rag_module, "fetch_chunk_sources", fake_fetch)
    monkeypatch.setattr(rag_module.settings, "RECALL_GENERATION_TIMEOUT_MS", 10)

    events = [
        _decode_sse(frame)
        async for frame in rag_module._event_stream(
            request_id="request-timeout",
            recall_request=_recall_request(),
            resolved_chat=SimpleNamespace(provider=provider),
        )
    ]

    assert [name for name, _ in events] == [
        "stream_started",
        "recall_done",
        "answer_delta",
        "error",
    ]
    assert events[-1][1]["code"] == "GENERATION_TIMEOUT"
    assert provider.close_calls == 1
