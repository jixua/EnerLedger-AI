from __future__ import annotations

from dataclasses import dataclass

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
