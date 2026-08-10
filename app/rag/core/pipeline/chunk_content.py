"""chunk 正文回填：按 chunk_id 批量反查 MySQL 取本用户 ACTIVE 非空正文。

中立的数据访问 helper，供召回后多个下游消费方共享：

- :mod:`app.rag.core.pipeline.rerank`：重排前按融合候选回填正文喂给 rerank 模型；
- :mod:`app.rag.core.pipeline.recall.generation`：生成阶段拼装上下文前回填正文。

放在 ``pipeline/`` 根下（而非 ``recall/`` 或 ``rerank/`` 内），让两个子包平级引用，
避免 rerank 反向依赖 generation。召回结果按设计不含正文，正文统一留在 MySQL 按需反查。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select

from app.domain.models import Document
from app.rag.database import get_async_session_factory
from app.rag.models.chunk_record import ChunkRecordDB


@dataclass(frozen=True)
class ChunkSource:
    """召回片段正文及可验证的来源信息。

    ``document_version`` 来自 chunk 写入时固化的版本，而不是读取时文档记录的当前值；
    因此即使文档随后进入重新解析队列，来源版本仍不会漂移。页码只使用解析器明确写入的
    ``start_page`` / ``end_page``，不能用文档总页数或 Markdown 行号推断。
    """

    content: str
    filename: str
    chunk_index: int
    document_version: int = 1
    page: int | None = None
    page_range: dict[str, int] | None = None


async def fetch_chunk_sources(chunk_ids: list[str], user_id: int) -> dict[str, ChunkSource]:
    """批量回填正文、文件名与 chunk 序号，返回 ``chunk_id -> ChunkSource``。

    ``document_chunk`` 与 ``document`` 同时按 user/document/dataset 关联，避免仅凭可枚举的
    ``document_id`` 串读其他用户来源。即使正文为空也保留来源元数据，供调用方解释为何该命中
    没有进入生成上下文；仅来源文档不存在的片段会被排除。
    """
    if not chunk_ids:
        return {}

    session_factory = get_async_session_factory()
    async with session_factory() as session:
        stmt = (
            select(
                ChunkRecordDB.chunk_id,
                ChunkRecordDB.content,
                Document.filename,
                ChunkRecordDB.chunk_index,
                ChunkRecordDB.document_version,
                ChunkRecordDB.start_page,
                ChunkRecordDB.end_page,
            )
            .select_from(ChunkRecordDB)
            .join(
                Document,
                (Document.id == ChunkRecordDB.doc_id)
                & (Document.dataset_id == ChunkRecordDB.set_id)
                & (Document.user_id == ChunkRecordDB.user_id),
            )
            .where(
                ChunkRecordDB.chunk_id.in_(chunk_ids),
                ChunkRecordDB.user_id == user_id,
            )
        )
        rows = (await session.execute(stmt)).all()

    sources: dict[str, ChunkSource] = {}
    for (
        chunk_id,
        content,
        filename,
        chunk_index,
        document_version,
        start_page,
        end_page,
    ) in rows:
        page = int(start_page) if start_page is not None and start_page == end_page else None
        page_range = (
            {"start": int(start_page), "end": int(end_page)}
            if start_page is not None and end_page is not None
            else None
        )
        sources[str(chunk_id)] = ChunkSource(
            content=content,
            filename=filename,
            chunk_index=int(chunk_index),
            document_version=int(document_version),
            page=page,
            page_range=page_range,
        )
    return sources


async def fetch_chunk_contents(chunk_ids: list[str], user_id: int) -> dict[str, str]:
    """按 chunk_id 批量反查正文，返回 chunk_id -> 正文映射。

    只回填发起用户本人的非空片段；文档 READY 状态已由召回门禁统一校验。查不到的 chunk_id 不出现在
    返回 dict 中（由调用方按「跳过」处理）。批量一次查询，不逐条，避免放大 DB 往返。
    """
    sources = await fetch_chunk_sources(chunk_ids, user_id)
    return {
        chunk_id: source.content
        for chunk_id, source in sources.items()
        if source.content and source.content.strip()
    }
