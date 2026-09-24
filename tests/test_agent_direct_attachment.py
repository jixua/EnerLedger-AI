"""对话直传附件：阈值判定、文本提取与上传接口协议。"""

from __future__ import annotations

import os
from io import BytesIO
from typing import Any

os.environ.setdefault("ADMIN_PASSWORD_HASH", "scrypt:test-only")

from pathlib import Path  # noqa: E402

import fitz  # noqa: E402
import pytest  # noqa: E402
from fastapi import HTTPException, UploadFile  # noqa: E402

from app.api.agent import AgentAttachment, upload_agent_material  # noqa: E402
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
    assert AgentAttachment(filename="a.md", material_id="m-1").is_direct is True
    assert AgentAttachment(document_id=3).is_direct is False

    with pytest.raises(ValueError):
        AgentAttachment(document_id=3, filename="a.md", material_id="m-1")
    with pytest.raises(ValueError):
        AgentAttachment(filename="a.md")


class _MaterialSession:
    """暂存材料用的最小会话替身：记录写操作，不连库。"""

    def __init__(self) -> None:
        self.added: list[Any] = []
        self.commits = 0

    def add(self, value: Any) -> None:
        self.added.append(value)

    async def commit(self) -> None:
        self.commits += 1


class _MaterialStorage:
    def __init__(self) -> None:
        self.uploaded: list[tuple[str, str, int, str]] = []

    def upload_bytes(self, bucket, object_key, content, content_type) -> None:
        self.uploaded.append((bucket, object_key, len(content), content_type))


@pytest.fixture()
def staged(monkeypatch: pytest.MonkeyPatch) -> tuple[_MaterialSession, _MaterialStorage]:
    """上传接口现在把正文暂存在服务端，测试里给一个不落盘的替身。"""
    monkeypatch.setattr(settings, "AGENT_ENABLED", True)
    session = _MaterialSession()
    storage = _MaterialStorage()
    monkeypatch.setattr(
        "app.api.agent.StorageFactory.get_storage", staticmethod(lambda: storage)
    )
    return session, storage


@pytest.mark.asyncio
async def test_upload_endpoint_stages_text_and_returns_material_id(staged) -> None:
    session, storage = staged

    result = await upload_agent_material(
        _upload("说明.md", "# 标题".encode()), user_id=1, db=session
    )

    assert result["filename"] == "说明.md"
    assert result["char_count"] > 0
    # 正文不回传浏览器，只回一个 id 与元数据
    assert "content" not in result
    assert len(result["material_id"]) == 36
    assert session.commits == 1
    assert [type(row).__name__ for row in session.added] == ["ReportMaterial"]
    assert len(storage.uploaded) == 1
    bucket, object_key, size, content_type = storage.uploaded[0]
    assert object_key.startswith(f"report-materials/1/{result['material_id']}")
    assert size > 0
    assert content_type == "text/plain; charset=utf-8"
    assert bucket == settings.MINIO_PRIVATE_BUCKET


@pytest.mark.asyncio
async def test_upload_endpoint_rejects_unsupported_type(staged) -> None:
    session, _storage = staged

    with pytest.raises(HTTPException) as excinfo:
        await upload_agent_material(_upload("说明.txt", b"hi"), user_id=1, db=session)

    assert excinfo.value.status_code == 415


@pytest.mark.asyncio
async def test_upload_endpoint_reports_too_large(staged, monkeypatch: pytest.MonkeyPatch) -> None:
    session, _storage = staged
    monkeypatch.setattr(settings, "AGENT_DIRECT_ATTACHMENT_MAX_BYTES", 1024)

    with pytest.raises(HTTPException) as excinfo:
        await upload_agent_material(_upload("说明.md", b"x" * 4096), user_id=1, db=session)

    assert excinfo.value.status_code == 413
    assert excinfo.value.detail["code"] == "ATTACHMENT_TOO_LARGE"
    assert excinfo.value.detail["message"] == "文件过大，请先导入知识库"
