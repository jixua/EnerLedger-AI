"""对话直传附件：阈值判定、文本提取与上传接口协议。"""

from __future__ import annotations

import os
from io import BytesIO

os.environ.setdefault("ADMIN_PASSWORD_HASH", "scrypt:test-only")

from pathlib import Path  # noqa: E402

import fitz  # noqa: E402
import pytest  # noqa: E402
from fastapi import HTTPException, UploadFile  # noqa: E402

from app.api.agent import AgentAttachment, upload_agent_attachment  # noqa: E402
from app.rag.config import settings  # noqa: E402
from app.services.agent_attachment_text import (  # noqa: E402
    DirectAttachmentTooLargeError,
    extract_direct_attachment_text,
    normalize_extension,
)


def _upload(name: str, data: bytes) -> UploadFile:
    return UploadFile(file=BytesIO(data), filename=name)


def _pdf_bytes(page_count: int, text: str | None = None) -> bytes:
    document = fitz.open()
    for _index in range(page_count):
        page = document.new_page()
        if text:
            page.insert_text((72, 72), text)
    data = document.tobytes()
    document.close()
    return data


def test_markdown_under_limits_is_extracted(tmp_path: Path) -> None:
    source = tmp_path / "核算说明.md"
    source.write_text("# 核算说明\n\n正文内容", encoding="utf-8")

    text, page_count = extract_direct_attachment_text(source, "md")

    assert text.startswith("# 核算说明")
    assert page_count is None


def test_markdown_over_char_limit_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "长文.md"
    source.write_text("字" * (settings.AGENT_DIRECT_ATTACHMENT_MAX_CHARS + 1), encoding="utf-8")

    with pytest.raises(DirectAttachmentTooLargeError):
        extract_direct_attachment_text(source, "md")


def test_pdf_over_page_limit_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "年报.pdf"
    source.write_bytes(_pdf_bytes(settings.AGENT_DIRECT_ATTACHMENT_MAX_PAGES + 2, "数据"))

    with pytest.raises(DirectAttachmentTooLargeError):
        extract_direct_attachment_text(source, "pdf")


def test_pdf_under_page_limit_reports_page_count(tmp_path: Path) -> None:
    source = tmp_path / "小报告.pdf"
    source.write_bytes(_pdf_bytes(1, "Hello direct attachment"))

    text, page_count = extract_direct_attachment_text(source, "pdf")

    assert "Hello direct attachment" in text
    assert page_count == 1


def test_extension_normalization() -> None:
    assert normalize_extension("年报.PDF") == "pdf"
    assert normalize_extension("说明.md") == "md"
    assert normalize_extension("") == ""


def test_attachment_accepts_exactly_one_kind() -> None:
    assert AgentAttachment(filename="a.md", content="正文").is_direct is True
    assert AgentAttachment(document_id=3).is_direct is False

    with pytest.raises(ValueError):
        AgentAttachment(document_id=3, filename="a.md", content="正文")
    with pytest.raises(ValueError):
        AgentAttachment(filename="a.md")


@pytest.mark.asyncio
async def test_upload_endpoint_returns_extracted_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "AGENT_ENABLED", True)

    result = await upload_agent_attachment(_upload("说明.md", "# 标题".encode()), user_id=1)

    assert result["filename"] == "说明.md"
    assert result["content"].startswith("# 标题")
    assert result["char_count"] == len(result["content"])


@pytest.mark.asyncio
async def test_upload_endpoint_rejects_unsupported_type(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "AGENT_ENABLED", True)

    with pytest.raises(HTTPException) as excinfo:
        await upload_agent_attachment(_upload("说明.txt", b"hi"), user_id=1)

    assert excinfo.value.status_code == 415


@pytest.mark.asyncio
async def test_upload_endpoint_reports_too_large(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "AGENT_ENABLED", True)
    monkeypatch.setattr(settings, "AGENT_DIRECT_ATTACHMENT_MAX_BYTES", 1024)

    with pytest.raises(HTTPException) as excinfo:
        await upload_agent_attachment(_upload("说明.md", b"x" * 4096), user_id=1)

    assert excinfo.value.status_code == 413
    assert excinfo.value.detail["code"] == "ATTACHMENT_TOO_LARGE"
    assert excinfo.value.detail["message"] == "文件过大，请先导入知识库"
