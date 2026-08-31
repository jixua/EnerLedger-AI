"""进程内 Pi Agent 运行上下文。

当前产品不持久化对话；运行上下文只在一次 SSE 请求期间存在。Pi 只拿到不可预测的
``run_id``，内部工具再由 FastAPI 反查可信的用户、数据集和文档范围。
"""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass
from time import monotonic


@dataclass(frozen=True)
class AgentRunContext:
    run_id: str
    user_id: int
    dataset_ids: tuple[int, ...]
    doc_ids: tuple[int, ...] | None
    scope_mode: str
    knowledge_base_refs: tuple[tuple[str, int], ...]
    created_at: float

    def resolve_knowledge_base_refs(self, refs: list[str]) -> tuple[int, ...] | None:
        """把 run 内不透明引用解析为可信数据集范围；出现未知引用时返回 None。"""
        by_ref = dict(self.knowledge_base_refs)
        if any(ref not in by_ref for ref in refs):
            return None
        return tuple(dict.fromkeys(by_ref[ref] for ref in refs))

    def knowledge_base_ref_for(self, dataset_id: int) -> str | None:
        return next((ref for ref, value in self.knowledge_base_refs if value == dataset_id), None)


@dataclass(frozen=True)
class AgentEvidence:
    evidence_id: str
    chunk_id: str
    dataset_id: int
    doc_id: int
    document_version: str | None
    citation_index: int | None


class AgentRunRegistry:
    def __init__(self) -> None:
        self._runs: dict[str, AgentRunContext] = {}
        self._evidence: dict[str, dict[tuple[int, int, str, str | None], AgentEvidence]] = {}
        self._next_evidence: dict[str, int] = {}
        self._next_citation: dict[str, int] = {}
        self._document_refs: dict[str, dict[str, int]] = {}
        self._section_refs: dict[str, dict[str, tuple[int, str | None]]] = {}
        self._section_cursors: dict[str, dict[str, tuple[str, int]]] = {}
        self._lock = asyncio.Lock()

    async def register(
        self,
        *,
        run_id: str,
        user_id: int,
        dataset_ids: list[int],
        doc_ids: list[int] | None,
        scope_mode: str = "selected",
    ) -> AgentRunContext:
        context = AgentRunContext(
            run_id=run_id,
            user_id=user_id,
            dataset_ids=tuple(dataset_ids),
            doc_ids=tuple(doc_ids) if doc_ids else None,
            scope_mode=scope_mode,
            knowledge_base_refs=tuple(
                (f"kb_ref_{secrets.token_urlsafe(8)}", dataset_id) for dataset_id in dataset_ids
            ),
            created_at=monotonic(),
        )
        async with self._lock:
            self._runs[run_id] = context
            self._evidence[run_id] = {}
            self._next_evidence[run_id] = 1
            self._next_citation[run_id] = 1
            self._document_refs[run_id] = {}
            self._section_refs[run_id] = {}
            self._section_cursors[run_id] = {}
        return context

    async def get(self, run_id: str, *, max_age_seconds: float) -> AgentRunContext | None:
        async with self._lock:
            context = self._runs.get(run_id)
            if context is None:
                return None
            if monotonic() - context.created_at > max_age_seconds:
                self._runs.pop(run_id, None)
                self._evidence.pop(run_id, None)
                self._next_evidence.pop(run_id, None)
                self._next_citation.pop(run_id, None)
                self._document_refs.pop(run_id, None)
                self._section_refs.pop(run_id, None)
                self._section_cursors.pop(run_id, None)
                return None
            return context

    async def register_evidence(
        self,
        run_id: str,
        *,
        chunk_id: str,
        dataset_id: int,
        doc_id: int,
        document_version: str | None,
        selected_for_context: bool,
    ) -> AgentEvidence | None:
        """登记或复用证据；只有真正进入上下文时才分配全局引用序号。"""
        key = (dataset_id, doc_id, chunk_id, document_version)
        async with self._lock:
            if run_id not in self._runs:
                return None
            ledger = self._evidence.setdefault(run_id, {})
            current = ledger.get(key)
            if current is None:
                ordinal = self._next_evidence[run_id]
                self._next_evidence[run_id] = ordinal + 1
                current = AgentEvidence(
                    evidence_id=f"ev_{run_id[:8]}_{ordinal:04d}",
                    chunk_id=chunk_id,
                    dataset_id=dataset_id,
                    doc_id=doc_id,
                    document_version=document_version,
                    citation_index=None,
                )
            if selected_for_context and current.citation_index is None:
                citation_index = self._next_citation[run_id]
                self._next_citation[run_id] = citation_index + 1
                current = AgentEvidence(
                    evidence_id=current.evidence_id,
                    chunk_id=current.chunk_id,
                    dataset_id=current.dataset_id,
                    doc_id=current.doc_id,
                    document_version=current.document_version,
                    citation_index=citation_index,
                )
            ledger[key] = current
            return current

    async def get_evidence(self, run_id: str, evidence_id: str) -> AgentEvidence | None:
        async with self._lock:
            return next(
                (
                    evidence
                    for evidence in self._evidence.get(run_id, {}).values()
                    if evidence.evidence_id == evidence_id
                ),
                None,
            )

    async def register_document_ref(self, run_id: str, doc_id: int) -> str | None:
        async with self._lock:
            if run_id not in self._runs:
                return None
            refs = self._document_refs.setdefault(run_id, {})
            existing = next((ref for ref, value in refs.items() if value == doc_id), None)
            if existing:
                return existing
            ref = f"doc_ref_{secrets.token_urlsafe(10)}"
            refs[ref] = doc_id
            return ref

    async def resolve_document_ref(self, run_id: str, ref: str) -> int | None:
        async with self._lock:
            return self._document_refs.get(run_id, {}).get(ref)

    async def register_section_ref(
        self, run_id: str, doc_id: int, heading_key: str | None
    ) -> str | None:
        async with self._lock:
            if run_id not in self._runs:
                return None
            refs = self._section_refs.setdefault(run_id, {})
            value = (doc_id, heading_key)
            existing = next((ref for ref, current in refs.items() if current == value), None)
            if existing:
                return existing
            ref = f"sec_ref_{secrets.token_urlsafe(10)}"
            refs[ref] = value
            return ref

    async def resolve_section_ref(self, run_id: str, ref: str) -> tuple[int, str | None] | None:
        async with self._lock:
            return self._section_refs.get(run_id, {}).get(ref)

    async def register_section_cursor(
        self, run_id: str, section_ref: str, offset: int
    ) -> str | None:
        async with self._lock:
            if run_id not in self._runs or section_ref not in self._section_refs.get(run_id, {}):
                return None
            ref = f"cursor_{secrets.token_urlsafe(12)}"
            self._section_cursors.setdefault(run_id, {})[ref] = (section_ref, offset)
            return ref

    async def resolve_section_cursor(self, run_id: str, cursor: str) -> tuple[str, int] | None:
        async with self._lock:
            return self._section_cursors.get(run_id, {}).get(cursor)

    async def release(self, run_id: str) -> None:
        async with self._lock:
            self._runs.pop(run_id, None)
            self._evidence.pop(run_id, None)
            self._next_evidence.pop(run_id, None)
            self._next_citation.pop(run_id, None)
            self._document_refs.pop(run_id, None)
            self._section_refs.pop(run_id, None)
            self._section_cursors.pop(run_id, None)


agent_run_registry = AgentRunRegistry()
