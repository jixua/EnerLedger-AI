"""对话直传附件的轻量文本提取。

对话框里上传的小文件不进入知识库链路（MinIO/Document/RabbitMQ/索引），而是就地
提取纯文本直接喂给模型分析。超过阈值的文件一律提示用户先导入知识库。

页数阈值只对 PDF 可靠；Word 的页数来自保存态分页符，无分页符时恒为 1。因此 Word /
Markdown 实际以字节数与提取字符数作为主要闸门。
"""

from __future__ import annotations

from pathlib import Path

import fitz

from app.rag.config import settings
from app.rag.core.parser.factory import ParserFactory

DIRECT_ATTACHMENT_SUPPORTED_TYPES = frozenset(
    {"pdf", "doc", "docx", "html", "htm", "md", "markdown"}
)

TOO_LARGE_MESSAGE = "文件过大，请先导入知识库"
NO_TEXT_MESSAGE = "文件未提取到文本内容，请先导入知识库"


class DirectAttachmentTooLargeError(Exception):
    """附件超过直传阈值；调用方应提示用户先导入知识库。"""


def normalize_extension(filename: str) -> str:
    return Path(filename or "").suffix.lower().lstrip(".")


def extract_direct_attachment_text(path: Path, ext: str) -> tuple[str, int | None]:
    """提取附件纯文本，返回 (文本, 页数或 None)。

    素材超过大小/页数/字符任一阈值时抛 DirectAttachmentTooLargeError；
    未提取到文本（如扫描件无文本层）时抛 ValueError。
    """
    if path.stat().st_size > settings.AGENT_DIRECT_ATTACHMENT_MAX_BYTES:
        raise DirectAttachmentTooLargeError(TOO_LARGE_MESSAGE)

    if ext == "pdf":
        text, page_count = _extract_pdf(path)
    else:
        text, page_count = _extract_via_parser(path, ext)

    text = text.strip()
    if not text:
        raise ValueError(NO_TEXT_MESSAGE)
    if len(text) > settings.AGENT_DIRECT_ATTACHMENT_MAX_CHARS:
        raise DirectAttachmentTooLargeError(TOO_LARGE_MESSAGE)
    return text, page_count


def _extract_pdf(path: Path) -> tuple[str, int]:
    with fitz.open(filename=str(path)) as document:
        page_count = int(document.page_count)
        if page_count > settings.AGENT_DIRECT_ATTACHMENT_MAX_PAGES:
            raise DirectAttachmentTooLargeError(TOO_LARGE_MESSAGE)
        pages = [page.get_text() for page in document]
    return "\n\n".join(pages), page_count


def _extract_via_parser(path: Path, ext: str) -> tuple[str, int | None]:
    parser = ParserFactory.get_parser(ext)
    text = parser.parse(path)
    page_count = parser.extract_metadata().get("page_count")
    if isinstance(page_count, int) and page_count > settings.AGENT_DIRECT_ATTACHMENT_MAX_PAGES:
        raise DirectAttachmentTooLargeError(TOO_LARGE_MESSAGE)
    return text, page_count if isinstance(page_count, int) else None
