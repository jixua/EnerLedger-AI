from __future__ import annotations

from app.rag.core.pipeline.recall.generation import assemble_context
from app.rag.core.pipeline.recall.models import RecallHit


def _hit(chunk_id: str, score: float) -> RecallHit:
    return RecallHit(
        chunk_id=chunk_id,
        doc_id=1,
        dataset_id=1,
        fused_score=score,
        scores={"dense": score},
    )


def test_default_context_budget_uses_lightweight_conservative_estimator() -> None:
    assembled = assemble_context(
        [_hit("first", 1.0), _hit("second", 0.9)],
        {"first": "能碳", "second": "会计"},
        token_budget=4,
    )

    assert [block.chunk_id for block in assembled.blocks] == ["first"]
    assert assembled.truncated == 1


def test_context_budget_still_accepts_injected_exact_counter() -> None:
    class _Counter:
        @staticmethod
        def count_tokens(_text: str) -> int:
            return 1

    assembled = assemble_context(
        [_hit("first", 1.0), _hit("second", 0.9)],
        {"first": "能碳", "second": "会计"},
        token_budget=2,
        tokenizer=_Counter(),
    )

    assert [block.chunk_id for block in assembled.blocks] == ["first", "second"]
    assert assembled.truncated == 0
