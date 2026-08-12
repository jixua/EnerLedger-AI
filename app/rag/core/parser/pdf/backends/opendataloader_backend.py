from __future__ import annotations

import html
import re
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from loguru import logger

from app.rag.core.parser.pdf.base import BasePdfBackend
from app.rag.core.parser.pdf.models import PdfBinaryAsset
from app.rag.core.parser.pdf.reliability import (
    OpenDataLoaderConversionError,
    OpenDataLoaderHealthChecker,
    OpenDataLoaderProcessRunner,
    OpenDataLoaderRuntimeError,
    PdfPreflightError,
    PdfReliabilityError,
    PdfReliabilityGuard,
    PdfReliabilityLimits,
    parse_java_major_version,
)
from app.rag.observability.logging import safe_exception_stack, truncate_log_value


class OpenDataLoaderBackend(BasePdfBackend):
    """OpenDataLoader 本地后端。

    官方 Python API 只接收文件路径；本次治理后 pipeline 已经把源文件流式下载到本地临时
    文件，这里直接复用该路径，不再额外写一份 ``temp_dir/document.pdf`` 副本。
    """

    name = "opendataloader"
    PAGE_MARKER_TEMPLATE = "<!-- ODL_PAGE:%page-number% -->"
    _PAGE_MARKER_PATTERN = re.compile(r"<!--\s*ODL_PAGE:(\d+)\s*-->")
    _MARKDOWN_IMAGE_PATTERN = re.compile(
        r"!\[[^\]]*\]\(\s*(?:<(?P<angle>[^>]+)>|(?P<plain>[^\s)]+))"
    )
    _HTML_IMAGE_PATTERN = re.compile(
        r"<img\b[^>]*\bsrc\s*=\s*(?:\"(?P<double>[^\"]+)\"|'(?P<single>[^']+)'|"
        r"(?P<bare>[^\s>]+))",
        re.IGNORECASE,
    )
    _BREAK_TAG_PATTERN = re.compile(r"<br\s*/?>", re.IGNORECASE)
    _BREAK_TAG_RUN_PATTERN = re.compile(
        r"(?:<br\s*/?>\s*)+",
        re.IGNORECASE,
    )
    _SINGLE_COLUMN_SEPARATOR_PATTERN = re.compile(
        r"^\s*\|\s*:?-{3,}:?\s*\|\s*$"
    )

    def __init__(
        self,
        *,
        limits: PdfReliabilityLimits | None = None,
        reliability_guard: PdfReliabilityGuard | None = None,
        process_runner: OpenDataLoaderProcessRunner | None = None,
        health_checker: OpenDataLoaderHealthChecker | None = None,
    ) -> None:
        super().__init__()
        self._limits = limits or PdfReliabilityLimits()
        self._reliability_guard = reliability_guard or PdfReliabilityGuard(self._limits)
        self._process_runner = process_runner or OpenDataLoaderProcessRunner()
        self._health_checker = health_checker or OpenDataLoaderHealthChecker()

    def parse(self, source: Path | None, options: Any = None) -> tuple[str, list[PdfBinaryAsset]]:
        if source is None:
            raise PdfPreflightError(
                "OpenDataLoader 缺少本地 PDF 源路径",
                error_code="PDF_SOURCE_MISSING",
                retryable=False,
            )

        try:
            preflight = self._reliability_guard.inspect(source)
            self.metadata["pdf_preflight"] = preflight.to_dict()
            self.metadata["pdf_reliability_limits"] = self._limits.to_dict()

            runtime_health = self._health_checker.check()
            self.metadata["opendataloader_runtime_health"] = runtime_health.to_dict()
            self.metadata["opendataloader_java_version"] = runtime_health.java_version or ""
            runtime_health.require_ready()

            # 仅借用 temp_dir 隔离 output_dir / image_dir；输入 PDF 直接复用 pipeline 已经
            # 落盘的 ``source`` 路径，避免再写一份完整 bytes。
            with tempfile.TemporaryDirectory(prefix="opendataloader-") as temp_dir:
                temp_path = Path(temp_dir)
                output_dir = temp_path / "output"
                image_dir = output_dir / "images"
                output_dir.mkdir(parents=True, exist_ok=True)

                table_method = getattr(options, "opendataloader_table_method", "default")
                markdown_with_html = bool(
                    getattr(options, "opendataloader_markdown_with_html", False)
                )
                if table_method not in {"default", "cluster"}:
                    raise OpenDataLoaderRuntimeError(
                        "OpenDataLoader table_method 只支持 default 或 cluster",
                        error_code="ODL_CONFIG_INVALID",
                        retryable=False,
                    )

                run_report = self._process_runner.run(
                    source=source,
                    output_dir=output_dir,
                    image_dir=image_dir,
                    page_marker_template=self.PAGE_MARKER_TEMPLATE,
                    limits=self._limits,
                    table_method=table_method,
                    markdown_with_html=markdown_with_html,
                )
                self.metadata["opendataloader_process"] = run_report.to_dict()

                markdown_path = self._find_markdown_file(output_dir)
                if markdown_path is None:
                    raise OpenDataLoaderConversionError(
                        "OpenDataLoader 未生成 Markdown 输出文件",
                        error_code="ODL_MARKDOWN_MISSING",
                        retryable=False,
                    )

                markdown = markdown_path.read_text(encoding="utf-8")
                if not markdown.strip():
                    raise OpenDataLoaderConversionError(
                        "OpenDataLoader 生成了空 Markdown",
                        error_code="ODL_MARKDOWN_EMPTY",
                        retryable=False,
                    )
                markdown, flattened_layout_table_count = (
                    self._normalize_single_column_layout_tables(markdown)
                )
                assets = self._collect_image_assets(output_dir, image_dir, markdown)
                self.metadata["opendataloader_markdown_file"] = str(
                    markdown_path.relative_to(output_dir)
                )
                self.metadata["opendataloader_image_count"] = len(assets)
                self.metadata["opendataloader_table_method"] = table_method
                self.metadata["opendataloader_markdown_with_html"] = markdown_with_html
                self.metadata["opendataloader_flattened_layout_table_count"] = (
                    flattened_layout_table_count
                )
                page_map = self._image_page_map(markdown)
                self.metadata["opendataloader_image_page_map"] = {
                    source_path: list(page_numbers)
                    for source_path, page_numbers in sorted(page_map.items())
                }
                unmapped = [asset.source_path for asset in assets if asset.page_number is None]
                if unmapped:
                    self.metadata["opendataloader_unmapped_image_assets"] = unmapped
                return markdown, assets
        except PdfReliabilityError as exc:
            self.metadata["opendataloader_backend_error"] = str(exc)
            self.metadata["opendataloader_error_code"] = exc.error_code
            self.metadata["opendataloader_retryable"] = exc.retryable
            logger.bind(
                event="pdf_backend_failed",
                outcome="failed",
                backend=self.name,
                stage="reliability_guard",
                error_code=exc.error_code,
                retryable=exc.retryable,
                error_message=truncate_log_value(exc),
                error_type=type(exc).__name__,
                stack_trace=safe_exception_stack(exc),
            ).error("OpenDataLoader 可靠性保护阻断解析")
            raise
        except Exception as exc:
            self.metadata["opendataloader_backend_error"] = str(exc)
            logger.bind(
                event="pdf_backend_failed",
                outcome="failed",
                backend=self.name,
                stage="local_parse",
                error_type=type(exc).__name__,
                error_message=truncate_log_value(exc),
                stack_trace=safe_exception_stack(exc),
            ).error("OpenDataLoader 解析异常")
            raise OpenDataLoaderConversionError(
                f"OpenDataLoader 解析异常: {type(exc).__name__}",
                error_code="ODL_UNEXPECTED_FAILURE",
                retryable=True,
            ) from exc

    def _ensure_java_11_plus(self) -> str | None:
        """Backward-compatible health helper used by focused runtime tests."""

        health = self._health_checker.check()
        self.metadata["opendataloader_runtime_health"] = health.to_dict()
        self.metadata["opendataloader_java_version"] = health.java_version or ""
        return None if health.ready else health.message

    @classmethod
    def _normalize_single_column_layout_tables(cls, markdown: str) -> tuple[str, int]:
        """Flatten page-layout blocks that OpenDataLoader encoded as one-column tables.

        A real one-column table without embedded layout breaks is preserved.  We only
        unwrap tables whose cell payload contains an image or at least two ``br`` tags,
        which is the shape produced for slide-like PDF pages.
        """

        lines = (markdown or "").splitlines()
        normalized: list[str] = []
        flattened_count = 0
        index = 0
        while index < len(lines):
            header = cls._single_column_cell(lines[index])
            if (
                header is None
                or index + 1 >= len(lines)
                or cls._SINGLE_COLUMN_SEPARATOR_PATTERN.fullmatch(lines[index + 1]) is None
            ):
                normalized.append(lines[index])
                index += 1
                continue

            cells = [header]
            cursor = index + 2
            while cursor < len(lines):
                cell = cls._single_column_cell(lines[cursor])
                if cell is None:
                    break
                cells.append(cell)
                cursor += 1

            payload = "\n".join(cell for cell in cells if cell.strip())
            break_count = len(cls._BREAK_TAG_PATTERN.findall(payload))
            has_image = bool(
                re.search(r"!\[[^\]]*\]\(", payload)
                or re.search(r"<img\b", payload, re.IGNORECASE)
            )
            if payload and (has_image or break_count >= 2):
                blocks = []
                for cell in cells:
                    # OpenDataLoader normally emits ``<br><br>`` between visual
                    # paragraphs.  Collapse a whole run at once so it does not
                    # expand into four or more blank lines.
                    block = cls._BREAK_TAG_RUN_PATTERN.sub("\n\n", cell).strip()
                    if block:
                        blocks.append(block)
                normalized.extend("\n\n".join(blocks).splitlines())
                flattened_count += 1
                index = cursor
                continue

            normalized.extend(lines[index:cursor])
            index = cursor

        return "\n".join(normalized), flattened_count

    @staticmethod
    def _single_column_cell(line: str) -> str | None:
        stripped = line.strip()
        if not stripped.startswith("|") or not stripped.endswith("|"):
            return None
        body = stripped[1:-1]
        if re.search(r"(?<!\\)\|", body):
            return None
        return body.strip()

    @classmethod
    def _parse_java_major_version(cls, version_output: str) -> int | None:
        return parse_java_major_version(version_output)

    @staticmethod
    def _find_markdown_file(output_dir: Path) -> Path | None:
        candidates = sorted(output_dir.rglob("*.md"))
        return candidates[0] if candidates else None

    def _collect_image_assets(
        self,
        output_dir: Path,
        image_dir: Path,
        markdown: str,
    ) -> list[PdfBinaryAsset]:
        if not image_dir.exists():
            return []

        assets: list[PdfBinaryAsset] = []
        page_map = self._image_page_map(markdown)
        next_index = 1
        for image_path in sorted(image_dir.rglob("*")):
            if not image_path.is_file():
                continue
            ext = image_path.suffix.lstrip(".").lower() or "png"
            source_path = image_path.relative_to(output_dir).as_posix()
            pages = page_map.get(self._normalize_image_reference(source_path), ())
            assets.append(
                PdfBinaryAsset(
                    kind="picture",
                    page_number=pages[0] if pages else None,
                    index=next_index,
                    ext=ext,
                    content=image_path.read_bytes(),
                    source_path=source_path,
                )
            )
            next_index += 1
        return assets

    @classmethod
    def _image_page_map(cls, markdown: str) -> dict[str, tuple[int, ...]]:
        """Map image references to ODL pages without using filename conventions."""

        current_page: int | None = None
        page_numbers_by_source: dict[str, list[int]] = {}
        for line in (markdown or "").splitlines():
            marker = cls._PAGE_MARKER_PATTERN.search(line)
            if marker is not None:
                current_page = int(marker.group(1))

            references: list[str] = []
            references.extend(
                match.group("angle") or match.group("plain") or ""
                for match in cls._MARKDOWN_IMAGE_PATTERN.finditer(line)
            )
            references.extend(
                match.group("double")
                or match.group("single")
                or match.group("bare")
                or ""
                for match in cls._HTML_IMAGE_PATTERN.finditer(line)
            )
            if current_page is None:
                continue
            for raw_reference in references:
                reference = cls._normalize_image_reference(raw_reference)
                if not reference:
                    continue
                pages = page_numbers_by_source.setdefault(reference, [])
                if current_page not in pages:
                    pages.append(current_page)
        return {
            source_path: tuple(page_numbers)
            for source_path, page_numbers in page_numbers_by_source.items()
        }

    @staticmethod
    def _normalize_image_reference(raw_reference: str) -> str:
        value = html.unescape(raw_reference or "").strip().strip("<>")
        if not value:
            return ""
        parsed = urlsplit(value)
        path = unquote(parsed.path or value).replace("\\", "/")
        while path.startswith("./"):
            path = path[2:]
        return path.lstrip("/")
