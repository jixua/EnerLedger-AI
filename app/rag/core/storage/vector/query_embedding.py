"""Lightweight dense query embedding without loading the chunking runtime."""

from __future__ import annotations

from typing import Any


async def aembed_dense_query(resolved_model: Any, query: str) -> tuple[list[float], Any | None]:
    """Embed one query with the dataset-bound provider.

    Query recall only needs the resolved provider and model name.  Building the
    full ``ChunkEmbeddingPipeline`` also constructs the document splitter and
    initializes tiktoken, which is unrelated to query embedding and can block a
    cold API event loop for tens of seconds.
    """

    normalized = query.strip()
    if not normalized:
        raise ValueError("query must not be empty or whitespace")
    response = await resolved_model.provider.embed(
        texts=[normalized],
        model=resolved_model.model_name,
    )
    embeddings = getattr(response, "embeddings", None) or []
    if len(embeddings) != 1:
        raise ValueError(
            f"Embedding API returned {len(embeddings)} vectors for single query, expected 1."
        )
    return [float(value) for value in embeddings[0]], getattr(response, "usage", None)
