from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest

from app.rag.core.parser.pdf.backends.mineru_backend import MinerUBackend
from app.rag.core.parser.pdf.base import BasePdfBackend
from app.rag.core.parser.pdf.models import PdfParseOptions
from app.rag.core.parser.pdf.registry import PdfBackendRegistry
from app.rag.core.parser.pdf.reliability import PdfReliabilityLimits
from app.rag.core.parser.pdf.service import PdfParserService


class _Response:
    def __init__(self, payload=None, content: bytes = b"") -> None:
        self._payload = payload
        self._content = content
        self.status_code = 200

    def json(self):
        return self._payload

    def raise_for_status(self) -> None:
        return None

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def iter_bytes(self):
        yield self._content


class _Client:
    def __init__(self, zip_bytes: bytes) -> None:
        self.zip_bytes = zip_bytes
        self.uploaded = b""
        self.calls: list[tuple[str, str]] = []
        self.post_json = None

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def post(self, url, **kwargs):
        self.calls.append(("POST", url))
        self.post_json = kwargs.get("json")
        return _Response(
            {
                "code": 0,
                "data": {
                    "batch_id": "batch-1",
                    "file_urls": ["https://upload.example.test/document.pdf"],
                },
            }
        )

    def put(self, url, *, content, **_kwargs):
        self.calls.append(("PUT", url))
        self.uploaded = content.read()
        return _Response()

    def get(self, url, **_kwargs):
        self.calls.append(("GET", url))
        return _Response(
            {
                "code": 0,
                "data": {
                    "extract_result": [
                        {
                            "state": "done",
                            "full_zip_url": "https://download.example.test/result.zip",
                        }
                    ]
                },
            }
        )

    def stream(self, method, url):
        self.calls.append((method, url))
        return _Response(content=self.zip_bytes)


def _result_zip() -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("full.md", "# MinerU result\n\nparsed")
        archive.writestr(
            "sample_content_list.json",
            json.dumps(
                [
                    {
                        "type": "text",
                        "text": "MinerU result",
                        "text_level": 1,
                        "page_idx": 0,
                    },
                    {
                        "type": "image",
                        "img_path": "images/figure.png",
                        "image_caption": ["emission chart"],
                        "page_idx": 1,
                    },
                ]
            ),
        )
        archive.writestr(
            "sample_middle.json",
            json.dumps({"pdf_info": [{"page_idx": 0}, {"page_idx": 1}, {"page_idx": 2}]}),
        )
        archive.writestr("images/figure.png", b"image-bytes")
    return stream.getvalue()


def test_mineru_uploads_local_pdf_and_polls_batch(monkeypatch, tmp_path: Path) -> None:
    source = tmp_path / "sample.pdf"
    source.write_bytes(b"%PDF-1.7 test")
    client = _Client(_result_zip())
    monkeypatch.setattr(
        "app.rag.core.parser.pdf.backends.mineru_backend.httpx.Client",
        lambda **_kwargs: client,
    )

    backend = MinerUBackend(
        api_url="https://mineru.net/api/v4/extract/task",
        api_key="secret",
        timeout=30,
    )
    markdown, assets = backend.parse(
        source,
        PdfParseOptions(mineru_model_version="vlm"),
    )

    assert markdown == (
        "<!-- ODL_PAGE:1 -->\n\n# MinerU result\n\n"
        "<!-- ODL_PAGE:2 -->\n\n![emission chart](images/figure.png)\n\n"
        "<!-- ODL_PAGE:3 -->"
    )
    assert client.uploaded == b"%PDF-1.7 test"
    assert ("POST", "https://mineru.net/api/v4/file-urls/batch") in client.calls
    assert (
        "GET",
        "https://mineru.net/api/v4/extract-results/batch/batch-1",
    ) in client.calls
    assert backend.metadata["mineru_submission_mode"] == "signed_upload"
    assert assets[0].page_number == 2
    assert assets[0].source_path == "images/figure.png"
    assert client.post_json["enable_table"] is True
    assert client.post_json["enable_formula"] is True


def test_explicit_mineru_uses_configured_local_fallback() -> None:
    registry = PdfBackendRegistry(default_backend="mineru", fallbacks="opendataloader")
    registry.register("mineru", MinerUBackend)
    registry.register("opendataloader", MinerUBackend)

    assert registry.resolve_order("mineru") == ["mineru", "opendataloader"]


def test_mineru_rejects_markdown_without_page_provenance() -> None:
    backend = MinerUBackend(api_url="https://mineru.net", api_key="secret")

    with pytest.raises(ValueError, match="无页级来源"):
        backend._download_result(
            object(),
            full_zip_url=None,
            markdown_url="https://download.example.test/full.md",
            task_id="task-1",
            model_version="vlm",
        )


def test_mineru_rejects_zip_without_page_provenance() -> None:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("full.md", "# parsed without page markers")
    backend = MinerUBackend(api_url="https://mineru.net", api_key="secret")

    with pytest.raises(ValueError, match="缺少可验证的页级来源"):
        backend._extract_zip_result(stream.getvalue())


def test_missing_mineru_page_provenance_continues_to_local_fallback(
    monkeypatch,
    tmp_path: Path,
) -> None:
    class _LocalFallbackBackend(BasePdfBackend):
        name = "opendataloader"

        def parse(self, source, options=None):
            return "<!-- ODL_PAGE:1 -->\n\nlocal fallback", []

    source = tmp_path / "sample.pdf"
    source.write_bytes(b"%PDF-1.7 test")
    mineru = MinerUBackend(api_url="https://mineru.net", api_key="secret")
    monkeypatch.setattr(
        mineru,
        "_call_cloud_upload_api",
        lambda *_args: (_ for _ in ()).throw(ValueError("结果缺少可验证的页级来源")),
    )
    registry = PdfBackendRegistry(default_backend="mineru", fallbacks="opendataloader")
    registry.register("mineru", lambda _options: mineru)
    registry.register("opendataloader", lambda _options: _LocalFallbackBackend())

    markdown, metadata = PdfParserService(registry).parse(
        source,
        PdfParseOptions(backend="mineru"),
    )

    assert markdown == "<!-- ODL_PAGE:1 -->\n\nlocal fallback"
    assert metadata["pdf_parser_backend"] == "opendataloader"
    assert metadata["pdf_parser_attempts"] == [
        {
            "backend": "mineru",
            "success": False,
            "reason": "结果缺少可验证的页级来源",
        },
        {"backend": "opendataloader", "success": True},
    ]


def test_mineru_stops_oversized_result_download() -> None:
    limits = PdfReliabilityLimits(max_output_dir_bytes=4)
    backend = MinerUBackend(
        api_url="https://mineru.net",
        api_key="secret",
        limits=limits,
    )
    client = _Client(b"12345")

    with pytest.raises(ValueError, match="超过下载限制"):
        backend._stream_download_bytes(client, "https://download.example.test/result.zip")


def test_mineru_preserves_retrieval_critical_captions_and_footnotes() -> None:
    backend = MinerUBackend(api_url="https://mineru.net", api_key="secret")

    markdown = backend._content_list_to_markdown(
        [
            {
                "type": "table",
                "table_caption": ["排放量统计"],
                "table_body": "| 排放量 | 12 |",
                "table_footnote": ["单位：tCO2e"],
            },
            {
                "type": "image",
                "img_path": "images/chart.png",
                "image_caption": ["排放趋势"],
                "image_footnote": ["口径：市场基准"],
            },
            {
                "type": "code",
                "code_caption": ["计算公式"],
                "code_body": "emission = activity * factor",
                "code_footnote": ["因子来源：年度指南"],
            },
        ]
    )

    assert "单位：tCO2e" in markdown
    assert "口径：市场基准" in markdown
    assert "计算公式" in markdown
    assert "因子来源：年度指南" in markdown
