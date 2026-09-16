"""报告 Agent 的上下文预算：把「一轮读完整个文档」的契约变成可校验的数值。

报告 Agent 的链路要求它按游标读完全部分片后才有提交资格，也就是说**整篇文档必然进入
prompt**。模型窗口是硬上限，超出的表现不是报错而是被服务端截断（finish_reason=length、
输出只剩几个 token），最终以 ``REPORT_IR_NOT_SUBMITTED`` 这种难以定位的形式失败。

这里把窗口拆成可用预算，并提供文档规模估算，让「装不下」在创建任务时就被明确拒绝。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.rag.config import settings
from app.rag.models.chunk_record import ChunkRecordDB

# 保守换算：实测中文约 2 字符/token（英文更高）。按 2 估会略微高估 token 数，
# 对「拒绝过大文档」来说是安全方向。
CHARS_PER_TOKEN = 2

# 裁剪后的结构提示（chunk_role / element_types / 标题链 / 表格行列数）按每个分片估算。
STRUCTURE_HINT_CHARS_PER_CHUNK = 250


@dataclass(frozen=True, slots=True)
class ReportContextBudget:
    window_tokens: int
    max_output_tokens: int
    reserve_tokens: int

    @property
    def prompt_tokens(self) -> int:
        """留给 prompt 的 token 预算。"""
        return max(
            self.window_tokens - self.max_output_tokens - self.reserve_tokens,
            0,
        )


class ReportDocumentTooLargeError(RuntimeError):
    code = "REPORT_DOCUMENT_TOO_LARGE"

    def __init__(self, *, estimated_tokens: int, budget_tokens: int) -> None:
        self.estimated_tokens = estimated_tokens
        self.budget_tokens = budget_tokens
        budget = report_context_budget()
        super().__init__(
            f"该文档约需 {max(estimated_tokens // 1000, 1)}k tokens，超过当前模型的单轮处理预算 "
            f"{max(budget_tokens // 1000, 1)}k（上下文 {budget.window_tokens // 1000}k "
            f"− 输出 {budget.max_output_tokens // 1000}k − 预留 {budget.reserve_tokens // 1000}k）。"
            "请拆分文档，或改用上下文更大的模型。"
        )


def report_context_budget() -> ReportContextBudget:
    return ReportContextBudget(
        window_tokens=settings.REPORT_AGENT_MODEL_CONTEXT_WINDOW,
        max_output_tokens=settings.REPORT_AGENT_MODEL_MAX_OUTPUT_TOKENS,
        reserve_tokens=settings.REPORT_AGENT_CONTEXT_RESERVE_TOKENS,
    )


def estimate_document_payload_chars(*, content_chars: int, chunk_count: int) -> int:
    """模型实际会读到的字符量：分片正文 + 裁剪后的结构提示。"""
    return int(content_chars) + int(chunk_count) * STRUCTURE_HINT_CHARS_PER_CHUNK


def estimate_document_tokens(*, content_chars: int, chunk_count: int) -> int:
    payload_chars = estimate_document_payload_chars(
        content_chars=content_chars, chunk_count=chunk_count
    )
    return payload_chars // CHARS_PER_TOKEN + settings.REPORT_AGENT_PROMPT_OVERHEAD_TOKENS


def assert_document_fits_context(*, content_chars: int, chunk_count: int) -> int:
    """返回估算 token 数；超出 prompt 预算时抛 ReportDocumentTooLargeError。"""
    estimated = estimate_document_tokens(content_chars=content_chars, chunk_count=chunk_count)
    budget = report_context_budget().prompt_tokens
    if estimated > budget:
        raise ReportDocumentTooLargeError(estimated_tokens=estimated, budget_tokens=budget)
    return estimated


async def load_document_payload_stats(
    db: AsyncSession, *, document_id: int, document_version: int
) -> tuple[int, int]:
    """返回 (分片数, 正文总字符数) 供预算估算使用。"""
    row = await db.execute(
        select(
            func.count(ChunkRecordDB.id),
            func.coalesce(func.sum(func.char_length(ChunkRecordDB.content)), 0),
        ).where(
            ChunkRecordDB.doc_id == document_id,
            ChunkRecordDB.document_version == document_version,
        )
    )
    chunk_count, content_chars = row.one()
    return int(chunk_count or 0), int(content_chars or 0)
