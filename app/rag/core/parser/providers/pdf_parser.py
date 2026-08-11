from pathlib import Path
from typing import Literal

import fitz

from app.rag.config import settings
from app.rag.core.parser.pdf.models import PdfParseOptions
from app.rag.core.parser.pdf.service import PdfParserService

from ..base import BaseParser


class PdfParser(BaseParser):
    """PDF -> Markdown 解析入口，可通过 backend 参数选择具体解析器。

    支持的 backend:
    - auto: MinerU → OpenDataLoader → Naive 全链路降级
    - mineru: MinerU HTTP API（默认不回退到本地解析器）
    - opendataloader: OpenDataLoader 本地解析
    - naive: PyMuPDF (最快，质量最低)

    入参从 ``bytes`` 切换为 ``Path | None``。``source is None`` 仅在"mineru 后端 +
    远端 URL 旁路"下合法（旧实现使用 ``file_stream == b""`` 表达同一语义）。
    """

    def __init__(
        self,
        backend: str | None = None,
        image_bucket: str | None = None,
        image_prefix: str | None = None,
        image_upload_async: bool | None = None,
        storage=None,
        source_file_url: str | None = None,
        docling_force_ocr: bool = False,
        mineru_api_url: str | None = None,
        mineru_api_key: str | None = None,
        mineru_timeout: int | None = None,
        mineru_model_version: str | None = None,
        opendataloader_table_method: Literal["default", "cluster"] | None = None,
        opendataloader_markdown_with_html: bool | None = None,
        opendataloader_timeout_seconds: float | None = None,
    ):
        super().__init__()
        self.backend = (backend or settings.PDF_PARSER_BACKEND).lower()
        self.image_bucket = image_bucket
        self.image_prefix = image_prefix
        self.image_upload_async = (
            settings.PDF_IMAGE_UPLOAD_ASYNC
            if image_upload_async is None
            else bool(image_upload_async)
        )
        self.storage = storage
        self.source_file_url = source_file_url
        self.docling_force_ocr = bool(docling_force_ocr)
        self.mineru_api_url = mineru_api_url or settings.MINERU_API_URL
        self.mineru_api_key = (
            mineru_api_key or settings.MINERU_API_TOKEN or settings.MINERU_API_KEY
        )
        self.mineru_timeout = mineru_timeout or settings.MINERU_TIMEOUT
        self.mineru_model_version = mineru_model_version or settings.MINERU_MODEL_VERSION
        resolved_table_method = (
            opendataloader_table_method or settings.OPENDATALOADER_TABLE_METHOD
        )
        if resolved_table_method not in {"default", "cluster"}:
            raise ValueError("OpenDataLoader table_method 只支持 default 或 cluster")
        self.opendataloader_table_method = resolved_table_method
        self.opendataloader_markdown_with_html = (
            settings.OPENDATALOADER_MARKDOWN_WITH_HTML
            if opendataloader_markdown_with_html is None
            else bool(opendataloader_markdown_with_html)
        )
        if opendataloader_timeout_seconds is not None and opendataloader_timeout_seconds <= 0:
            raise ValueError("OpenDataLoader timeout_seconds 必须大于 0")
        self.opendataloader_timeout_seconds = opendataloader_timeout_seconds
        self._service = PdfParserService()

    def parse(self, source: Path | None) -> str:
        # 旁路判定：mineru 后端 + 已有远端 URL + source 缺省时跳过本地 PDF 解析步骤。
        # 这里 ``source is None`` 与旧实现的 ``not file_stream`` 等价（旧路径用 b"" 表达旁路）。
        can_skip_local_pdf = (
            self.backend == "mineru" and bool(self.source_file_url) and source is None
        )
        if not can_skip_local_pdf:
            self.validate_source(source)
        markdown, metadata = self._service.parse(
            source,
            PdfParseOptions(
                backend=self.backend,
                image_bucket=self.image_bucket,
                image_prefix=self.image_prefix,
                image_upload_async=self.image_upload_async,
                storage=self.storage,
                source_file_url=self.source_file_url,
                docling_force_ocr=self.docling_force_ocr,
                mineru_api_url=self.mineru_api_url,
                mineru_api_key=self.mineru_api_key,
                mineru_timeout=self.mineru_timeout,
                mineru_model_version=self.mineru_model_version,
                opendataloader_table_method=self.opendataloader_table_method,
                opendataloader_markdown_with_html=self.opendataloader_markdown_with_html,
                opendataloader_timeout_seconds=self.opendataloader_timeout_seconds,
            ),
        )
        self.metadata.update(metadata)
        # OpenDataLoader backend 的可靠性预检必须先于这个只读 metadata
        # 的便利打开执行，否则损坏/加密 PDF 会先在此处抛出不带
        # ``error_code`` / ``retryable`` 的通用 PyMuPDF 异常。
        if source is not None:
            with fitz.open(filename=str(source)) as document:
                self.metadata["pages_or_length"] = len(document)
                self.metadata["pdf_info"] = dict(document.metadata or {})
        else:
            self.metadata["pages_or_length"] = 0
            self.metadata["pdf_info"] = {}

        if not markdown.strip():
            attempts = metadata.get("pdf_parser_attempts") or []
            reason = attempts[-1].get("reason") if attempts else "empty result"
            raise RuntimeError(f"PDF 解析失败: {reason}")
        return markdown.strip()
