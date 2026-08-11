# -*- coding: utf-8 -*-
"""noop 第二阶段算法。"""

from __future__ import annotations

from app.rag.core.llm.tokenizer import Tokenizer

from .stage_models import CoarseChunkSet, FinalChunkSet
from .stage_two_semantic_depth import SemanticDepthWindowStageTwo


class _UnusedEmbedder:
    async def embed(self, **_kwargs):
        raise RuntimeError("noop deterministic fallback must not call embedding")


class NoopStageTwoAlgorithm:
    """
    小分片原样透传，超长分片按段落/行/句子/token 执行确定性兜底。

    Args:
        None.

    Returns:
        None.
    """

    name = "noop"

    def __init__(
        self,
        tokenizer: Tokenizer | None = None,
        max_chunk_tokens: int = 512,
        hard_max_tokens: int = 1024,
        min_chunk_tokens: int = 128,
    ) -> None:
        """保留小分片透传语义，但对超长分片执行确定性长度兜底。"""
        self._length_fallback = SemanticDepthWindowStageTwo(
            tokenizer=tokenizer or Tokenizer(),
            embedder=_UnusedEmbedder(),
            max_chunk_tokens=max_chunk_tokens,
            hard_max_tokens=hard_max_tokens,
            min_chunk_tokens=min_chunk_tokens,
            semantic_scoring=False,
            strategy_name=self.name,
        )

    async def run(self, coarse_set: CoarseChunkSet) -> FinalChunkSet:
        """
        将 CoarseChunkSet 等价转换为 FinalChunkSet。

        Args:
            coarse_set: 第一阶段输出的粗分片集合。

        Returns:
            FinalChunkSet: 可导出的最终内部分片集合。
        """
        return await self._length_fallback.run(coarse_set)
