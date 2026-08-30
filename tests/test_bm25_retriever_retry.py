from __future__ import annotations

import asyncio
from dataclasses import dataclass
from threading import get_ident
from time import sleep

import pytest

from app.rag.core.preprocessor.ragflow_tokenizer import TokenizedText
from app.rag.core.storage.bm25_models import Bm25ChunkHit
from app.rag.core.storage.bm25_retriever import Bm25Retriever


@dataclass
class _Tokenizer:
    def tokenize(self, _text: str) -> TokenizedText:
        return TokenizedText(coarse_tokens="carbon quality", fine_tokens="carbon quality")


class _TransientBackend:
    score_scope = "global"

    def __init__(self) -> None:
        self.calls = 0

    async def recall_topk_chunks(self, _request):
        self.calls += 1
        if self.calls == 1:
            raise ConnectionError("temporary manticore disconnect")
        return [Bm25ChunkHit(chunk_id="chunk-1", doc_id=3, score=1.5)]


class _PermanentBackend:
    score_scope = "global"

    def __init__(self) -> None:
        self.calls = 0

    async def recall_topk_chunks(self, _request):
        self.calls += 1
        raise ValueError("invalid query")


class _EmptyBackend:
    score_scope = "global"

    async def recall_topk_chunks(self, _request):
        return []


class _SlowTokenizer:
    def __init__(self) -> None:
        self.thread_ids: list[int] = []

    def tokenize(self, _text: str) -> TokenizedText:
        self.thread_ids.append(get_ident())
        sleep(0.05)
        return TokenizedText(coarse_tokens="carbon quality", fine_tokens="carbon quality")


@pytest.mark.asyncio
async def test_standard_recall_retries_one_transient_backend_failure() -> None:
    backend = _TransientBackend()
    retriever = Bm25Retriever(backend=backend, tokenizer=_Tokenizer())

    hits = await retriever.recall(
        "carbon quality",
        [7],
        user_id=9,
        top_k=5,
    )

    assert backend.calls == 2
    assert [(hit.chunk_id, hit.doc_id, hit.dataset_id) for hit in hits] == [
        ("chunk-1", 3, 7)
    ]


@pytest.mark.asyncio
async def test_standard_recall_does_not_retry_permanent_failure() -> None:
    backend = _PermanentBackend()
    retriever = Bm25Retriever(backend=backend, tokenizer=_Tokenizer())

    with pytest.raises(ValueError, match="invalid query"):
        await retriever.recall(
            "carbon quality",
            [7],
            user_id=9,
            top_k=5,
        )

    assert backend.calls == 1


@pytest.mark.asyncio
async def test_standard_recall_offloads_tokenization_from_event_loop() -> None:
    tokenizer = _SlowTokenizer()
    retriever = Bm25Retriever(backend=_EmptyBackend(), tokenizer=tokenizer)
    event_loop_thread = get_ident()

    recall_task = asyncio.create_task(
        retriever.recall("carbon quality", [7], user_id=9, top_k=5)
    )
    await asyncio.sleep(0.01)

    assert not recall_task.done()
    await recall_task
    assert tokenizer.thread_ids
    assert all(thread_id != event_loop_thread for thread_id in tokenizer.thread_ids)


@pytest.mark.asyncio
async def test_warmup_runs_real_tokenization_off_event_loop() -> None:
    tokenizer = _SlowTokenizer()
    retriever = Bm25Retriever(backend=_EmptyBackend(), tokenizer=tokenizer)
    event_loop_thread = get_ident()

    await retriever.warmup()

    assert len(tokenizer.thread_ids) == 1
    assert tokenizer.thread_ids[0] != event_loop_thread


@pytest.mark.asyncio
async def test_grouped_recall_offloads_tokenization_from_event_loop() -> None:
    tokenizer = _SlowTokenizer()
    retriever = Bm25Retriever(backend=_EmptyBackend(), tokenizer=tokenizer)
    event_loop_thread = get_ident()

    await retriever.recall_by_dataset(
        "carbon quality",
        [7],
        user_id=9,
        top_k=5,
    )

    assert len(tokenizer.thread_ids) == 1
    assert tokenizer.thread_ids[0] != event_loop_thread
