"""对话直传材料的报告内联来源：切分、冻结与读取。

对话里上传的文件不进知识库，但报告链路需要的东西一样不能少——agent 必须能按游标
读完全部分片，提交时附上「我读过哪些分片」的清单让服务端逐项核对。所以这里把提取出
的文本切成**报告专用分片**，连同清单一起冻结在这次任务上：对用户是「没入库」，对链路
是「该有的都有」。

这些分片只服务这一次报告，不进向量库、不进 BM25、不进召回，也不出现在文档列表里。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.rag.services.storage.base import BaseObjectStorage

INLINE_SOURCE_SCHEMA_VERSION = 1
# 与文档分片的 ID 不会撞，也让人一眼看出这是直传材料而不是知识库里的分片。
INLINE_CHUNK_PREFIX = "M-"

# `report_run.source_kind` 的取值。判断来源一律看它，不要靠 document_id 是否为空反推。
SOURCE_KIND_DOCUMENT = "DOCUMENT"
SOURCE_KIND_INLINE = "INLINE"

# 段落聚合的目标长度与单片上限。够小，单页读起来不费 token；也够大，不至于把
# 一篇材料切得七零八落。
DEFAULT_TARGET_CHARS = 1800
DEFAULT_MAX_CHARS = 3000
_PARAGRAPH_BREAK = re.compile(r"\n\s*\n")
_MAX_SOURCE_BYTES = 16 * 1024 * 1024


def canonical_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class InlineChunk:
    chunk_id: str
    chunk_index: int
    content: str
    content_hash: str


def _hard_wrap(paragraph: str, max_chars: int) -> list[str]:
    """把超长段落硬切成不超过 max_chars 的片，优先按行断开。"""
    pieces: list[str] = []
    current = ""
    for line in paragraph.split("\n"):
        while len(line) > max_chars:
            if current:
                pieces.append(current)
                current = ""
            pieces.append(line[:max_chars])
            line = line[max_chars:]
        if current and len(current) + len(line) + 1 > max_chars:
            pieces.append(current)
            current = ""
        current = f"{current}\n{line}" if current else line
    if current:
        pieces.append(current)
    return pieces


def split_inline_text(
    text: str,
    *,
    target_chars: int = DEFAULT_TARGET_CHARS,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> list[str]:
    """按段落聚合切分：先按空行分段，累积到目标长度落一片，超长段落再硬切。"""
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return []
    buffer = ""
    chunks: list[str] = []
    for raw_paragraph in _PARAGRAPH_BREAK.split(normalized):
        paragraph = raw_paragraph.strip()
        if not paragraph:
            continue
        pieces = _hard_wrap(paragraph, max_chars) if len(paragraph) > max_chars else [paragraph]
        for piece in pieces:
            if buffer and len(buffer) + len(piece) + 2 > target_chars:
                chunks.append(buffer)
                buffer = ""
            buffer = f"{buffer}\n\n{piece}" if buffer else piece
    if buffer:
        chunks.append(buffer)
    return chunks


def build_inline_chunks(text: str) -> list[InlineChunk]:
    return [
        InlineChunk(
            chunk_id=f"{INLINE_CHUNK_PREFIX}{index + 1}",
            chunk_index=index,
            content=piece,
            content_hash=hashlib.sha256(piece.encode("utf-8")).hexdigest(),
        )
        for index, piece in enumerate(split_inline_text(text))
    ]


def chunk_count_for_text(text: str) -> int:
    """只数片数，不建对象——创建任务前的上下文预算检查用得到。"""
    return len(split_inline_text(text))


def serialize_inline_source(*, filename: str, text: str) -> dict[str, Any]:
    chunks = build_inline_chunks(text)
    return {
        "schema_version": INLINE_SOURCE_SCHEMA_VERSION,
        "filename": filename,
        "char_count": len(text),
        "chunk_count": len(chunks),
        "chunks": [
            {
                "chunk_id": chunk.chunk_id,
                "chunk_index": chunk.chunk_index,
                "content": chunk.content,
                "content_hash": chunk.content_hash,
            }
            for chunk in chunks
        ],
    }


def payload_chunks(payload: dict[str, Any]) -> list[InlineChunk]:
    raw = payload.get("chunks") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        return []
    chunks: list[InlineChunk] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        chunks.append(
            InlineChunk(
                chunk_id=str(item.get("chunk_id")),
                chunk_index=int(item.get("chunk_index") or 0),
                content=str(item.get("content") or ""),
                content_hash=str(item.get("content_hash") or ""),
            )
        )
    return chunks


def manifest_items(chunks: Sequence[InlineChunk]) -> tuple[dict[str, Any], ...]:
    """与文档来源同样形状的清单项，让覆盖校验可以原样复用。"""
    return tuple(
        {
            "chunk_id": chunk.chunk_id,
            "chunk_index": chunk.chunk_index,
            "content_hash": chunk.content_hash,
        }
        for chunk in chunks
    )


def inline_source_object_key(
    *, user_id: int, run_id: str, digest: str, kind: str = "source"
) -> str:
    """内联来源在对象存储里的位置。``kind`` 区分来源正文（source）与版式模板（template）。"""
    return f"reports/{int(user_id)}/inline/{run_id}/{kind}-{digest[:12]}.json"


def encode_inline_source(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


async def write_inline_source(
    storage: BaseObjectStorage, *, bucket: str, object_key: str, payload: dict[str, Any]
) -> tuple[str, int]:
    """落盘冻结的内联来源，返回 (内容哈希, 字节数)。"""
    data = encode_inline_source(payload)
    await asyncio.to_thread(
        storage.upload_bytes, bucket, object_key, data, "application/json"
    )
    return hashlib.sha256(data).hexdigest(), len(data)


async def read_inline_source(
    storage: BaseObjectStorage,
    *,
    bucket: str,
    object_key: str,
    max_bytes: int = _MAX_SOURCE_BYTES,
) -> dict[str, Any]:
    """读回冻结的内联来源。走临时文件，避免违反存储层的内存约定。"""
    temp_path: Path | None = None
    try:
        descriptor, temp_name = tempfile.mkstemp(prefix="report-inline-", suffix=".json")
        temp_path = Path(temp_name)
        os.close(descriptor)
        await asyncio.to_thread(storage.download_to_path, bucket, object_key, temp_path)
        if temp_path.stat().st_size > max_bytes:
            raise ValueError("内联来源超过读取上限")
        payload = json.loads(temp_path.read_bytes().decode("utf-8"))
    finally:
        if temp_path is not None:
            with contextlib.suppress(OSError):
                temp_path.unlink()
    if not isinstance(payload, dict):
        raise ValueError("内联来源格式无效")
    return payload
