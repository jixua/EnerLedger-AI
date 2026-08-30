"""Run query tokenization outside the API process.

Infinity's RAGFlow-compatible tokenizer may hold the Python GIL while lazily
loading dictionaries or tokenizing a cold query.  A worker thread therefore
does not reliably protect FastAPI's event loop.  This module owns one spawned
worker process and only sends/receives plain strings so no tokenizer instance
needs to be pickled.
"""

from __future__ import annotations

import asyncio
import multiprocessing
from concurrent.futures import ProcessPoolExecutor
from functools import lru_cache

_worker_tokenizer = None


def _tokenize_query_in_worker(query: str) -> list[str]:
    """Tokenize one query in the long-lived child process."""

    global _worker_tokenizer
    if _worker_tokenizer is None:
        from app.rag.core.preprocessor.ragflow_tokenizer import RagFlowTokenizer

        _worker_tokenizer = RagFlowTokenizer()
    tokenized = _worker_tokenizer.tokenize(query)
    return [token for token in tokenized.coarse_tokens.split() if token]


class ProcessQueryTokenizer:
    """Async facade for a single reusable tokenizer child process."""

    def __init__(self) -> None:
        self._executor = ProcessPoolExecutor(
            max_workers=1,
            mp_context=multiprocessing.get_context("spawn"),
        )
        self._closed = False

    async def tokenize(self, query: str) -> list[str]:
        if self._closed:
            raise RuntimeError("query tokenizer process is closed")
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, _tokenize_query_in_worker, query)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await asyncio.to_thread(self._executor.shutdown, wait=True, cancel_futures=True)


@lru_cache(maxsize=1)
def get_process_query_tokenizer() -> ProcessQueryTokenizer:
    return ProcessQueryTokenizer()


async def close_process_query_tokenizer() -> None:
    if get_process_query_tokenizer.cache_info().currsize:
        await get_process_query_tokenizer().close()
    get_process_query_tokenizer.cache_clear()
