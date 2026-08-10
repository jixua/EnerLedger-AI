from __future__ import annotations

import hashlib
import subprocess
import tempfile
import zipfile
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any

import mammoth
import mammoth.images
from bs4 import BeautifulSoup
from lxml import etree

from app.rag.config import settings
from app.rag.core.parser.exceptions import ParseBaseException

from ..base import BaseParser
from ..html.models import HtmlParseOptions
from ..html.renderer import HtmlMarkdownRenderer
from ..html.service import HtmlParseService

_CONTENT_TYPE_EXT = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/gif": "gif",
    "image/bmp": "bmp",
    "image/webp": "webp",
    "image/svg+xml": "svg",
    "image/tiff": "tiff",
    "image/x-emf": "emf",
    "image/x-wmf": "wmf",
}
_VISION_CONTENT_TYPES = {
    "image/png",
    "image/jpeg",
    "image/jpg",
    "image/gif",
    "image/bmp",
    "image/webp",
    "image/tiff",
}
_VECTOR_CONTENT_TYPES = {"image/x-emf", "image/x-wmf", "image/svg+xml"}
_OOXML_NAMESPACES = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "v": "urn:schemas-microsoft-com:vml",
    "o": "urn:schemas-microsoft-com:office:office",
    "c": "http://schemas.openxmlformats.org/drawingml/2006/chart",
    "dgm": "http://schemas.openxmlformats.org/drawingml/2006/diagram",
}

_LIST_STYLE_MAP = "\n".join(
    [
        "p[style-name='List Bullet'] => ul > li:fresh",
        "p[style-name='List Bullet 2'] => ul|ol > li > ul > li:fresh",
        "p[style-name='List Bullet 3'] => ul|ol > li > ul|ol > li > ul > li:fresh",
        "p[style-name='List Number'] => ol > li:fresh",
        "p[style-name='List Number 2'] => ol|ul > li > ol > li:fresh",
        "p[style-name='List Number 3'] => ol|ul > li > ol|ul > li > ol > li:fresh",
    ]
)


class WordParser(BaseParser):
    """DOCX -> structure-preserving Markdown/HTML with durable image assets."""

    def __init__(
        self,
        *,
        storage: Any | None = None,
        image_bucket: str | None = None,
        image_prefix: str | None = None,
        legacy_converter_binary: str | None = None,
        legacy_converter_timeout_seconds: float | None = None,
        **_: object,
    ) -> None:
        super().__init__()
        has_image_location = bool(image_bucket and image_prefix)
        if (storage is not None) != has_image_location:
            raise ValueError("Word 图片持久化需要同时提供 storage/image_bucket/image_prefix")
        self.storage = storage
        self.image_bucket = image_bucket
        self.image_prefix = (image_prefix or "word-images").strip("/")
        self.legacy_converter_binary = (
            legacy_converter_binary or settings.WORD_LEGACY_CONVERTER_BINARY
        )
        self.legacy_converter_timeout_seconds = (
            legacy_converter_timeout_seconds or settings.WORD_LEGACY_CONVERTER_TIMEOUT_SECONDS
        )
        self._options = HtmlParseOptions(
            preserve_table_html=True,
            preserve_image_urls=True,
        )
        self._image_bytes_by_url: dict[str, tuple[bytes, str]] = {}
        self._image_assets: dict[str, dict[str, object]] = {}
        self._image_occurrence_count = 0
        self._image_upload_count = 0
        self._warnings: list[str] = []

    def parse(self, source: Path | None) -> str:
        if source is None:
            raise ValueError("Word 解析需要本地源文件路径")
        self.validate_source(source)
        path = Path(source)
        self._reset_state()

        if path.suffix.lower() == ".doc":
            with tempfile.TemporaryDirectory(prefix="word-legacy-") as temp_dir:
                converted = self._convert_legacy_doc(path, Path(temp_dir))
                return self._parse_docx(converted, converted_from_legacy=True)
        return self._parse_docx(path, converted_from_legacy=False)

    def _reset_state(self) -> None:
        self.metadata = {}
        self._image_bytes_by_url = {}
        self._image_assets = {}
        self._image_occurrence_count = 0
        self._image_upload_count = 0
        self._warnings = []

    def _parse_docx(self, source: Path, *, converted_from_legacy: bool) -> str:
        file_stream = source.read_bytes()
        package = self._inspect_ooxml(file_stream)
        try:
            result = mammoth.convert_to_html(
                BytesIO(file_stream),
                convert_image=mammoth.images.img_element(self._image_hook),
                style_map=_LIST_STYLE_MAP,
            )
        except ParseBaseException:
            raise
        except Exception as exc:
            raise ParseBaseException(f"Word 解析失败：Mammoth 转换异常 {exc}") from exc

        mammoth_warnings = [str(message.message) for message in result.messages]
        self._warnings.extend(f"MAMMOTH:{warning}" for warning in mammoth_warnings)
        renderer = self._render_html(result.value or "")
        markdown = self._last_markdown
        mammoth_soup = BeautifulSoup(result.value or "", "lxml")
        mammoth_text_chars = len("".join(mammoth_soup.get_text().split()))

        critical_warnings = [
            warning
            for warning in mammoth_warnings
            if any(
                token in warning.casefold()
                for token in ("oleobject", "chart", "diagram", "alternatecontent")
            )
        ]
        output_unique_urls = set(self._image_bytes_by_url)
        self.metadata.update(
            {
                "format": "doc" if converted_from_legacy else "docx",
                "parser_backend": "libreoffice+mammoth" if converted_from_legacy else "mammoth",
                "unit_type": "block",
                "unit_count": (
                    int(package["paragraph_count"])
                    + int(package["source_top_level_table_count"])
                ),
                **package,
                "table_count": renderer.table_count,
                "record_table_count": 0,
                "table_failure_count": 0,
                "image_count": renderer.image_count,
                "image_occurrence_count": self._image_occurrence_count,
                "image_asset_count": len(self._image_assets),
                "image_upload_count": self._image_upload_count,
                "image_assets_persisted": (
                    self._image_occurrence_count == 0
                    or (
                        self.storage is not None
                        and self._image_upload_count == len(self._image_assets)
                    )
                ),
                "unsupported_vision_image_count": sum(
                    not bool(asset["vision_supported"])
                    for asset in self._image_assets.values()
                ),
                "mammoth_message_count": len(mammoth_warnings),
                "mammoth_messages": mammoth_warnings,
                "mammoth_text_chars": mammoth_text_chars,
                "mammoth_html_table_count": len(mammoth_soup.find_all("table")),
                "critical_warnings": critical_warnings,
                "warnings": list(dict.fromkeys(self._warnings)),
                "_image_bytes_by_url": {
                    url: self._image_bytes_by_url[url] for url in output_unique_urls
                },
                "word_image_assets": list(self._image_assets.values()),
            }
        )
        return markdown

    def _inspect_ooxml(self, content: bytes) -> dict[str, object]:
        try:
            if not zipfile.is_zipfile(BytesIO(content)):
                raise ParseBaseException("Word 解析失败：非 DOCX OOXML 文件或文件已损坏")
            with zipfile.ZipFile(BytesIO(content)) as archive:
                infos = archive.infolist()
                if len(infos) > settings.WORD_MAX_ZIP_ENTRIES:
                    raise ParseBaseException("Word 解析失败：OOXML ZIP 条目数超限")
                total_uncompressed = 0
                total_compressed = 0
                for info in infos:
                    path = PurePosixPath(info.filename)
                    if path.is_absolute() or ".." in path.parts:
                        raise ParseBaseException("Word 解析失败：OOXML 包含非法路径")
                    if info.flag_bits & 0x1:
                        raise ParseBaseException("Word 解析失败：OOXML ZIP 条目已加密")
                    total_uncompressed += int(info.file_size)
                    total_compressed += max(1, int(info.compress_size))
                if total_uncompressed > settings.WORD_MAX_UNCOMPRESSED_BYTES:
                    raise ParseBaseException("Word 解析失败：OOXML 解压大小超限")
                compression_ratio = total_uncompressed / max(1, total_compressed)
                if compression_ratio > settings.WORD_MAX_COMPRESSION_RATIO:
                    raise ParseBaseException("Word 解析失败：OOXML 压缩比异常")

                names = set(archive.namelist())
                if "[Content_Types].xml" not in names or "word/document.xml" not in names:
                    raise ParseBaseException("Word 解析失败：缺少 DOCX 必要 OOXML 结构")
                if any(name.casefold().endswith("vbaproject.bin") for name in names):
                    raise ParseBaseException("Word 解析失败：不允许含宏的 Office 文档")
                document_xml = archive.read("word/document.xml")
                media = [
                    info
                    for info in infos
                    if info.filename.startswith("word/media/") and not info.is_dir()
                ]
                if len(media) > settings.WORD_MAX_IMAGES:
                    raise ParseBaseException("Word 解析失败：内嵌媒体数量超限")
                media_total = sum(int(info.file_size) for info in media)
                if media_total > settings.WORD_MAX_TOTAL_IMAGE_BYTES:
                    raise ParseBaseException("Word 解析失败：内嵌媒体总大小超限")
                if any(info.file_size > settings.WORD_MAX_SINGLE_IMAGE_BYTES for info in media):
                    raise ParseBaseException("Word 解析失败：单个内嵌媒体超限")
        except ParseBaseException:
            raise
        except (zipfile.BadZipFile, OSError, ValueError, etree.XMLSyntaxError) as exc:
            raise ParseBaseException(f"Word 解析失败：OOXML 结构损坏 {exc}") from exc

        root = etree.fromstring(document_xml)

        def xpath(expression: str) -> list[Any]:
            return root.xpath(expression, namespaces=_OOXML_NAMESPACES)

        source_text = "".join(str(text) for text in xpath(".//w:t/text()"))
        return {
            "source_table_count": len(xpath(".//w:tbl")),
            "source_top_level_table_count": len(xpath(".//w:tbl[not(ancestor::w:tbl)]")),
            "source_nested_table_count": len(xpath(".//w:tbl[ancestor::w:tbl]")),
            "source_merged_cell_count": len(xpath(".//w:gridSpan | .//w:vMerge")),
            "source_image_reference_count": len(
                xpath(".//a:blip[@r:embed or @r:link] | .//v:imagedata[@r:id]")
            ),
            "source_media_count": len(media),
            # 与 Mammoth 输出使用同一统计口径：排除排版空白。
            "source_text_chars": len("".join(source_text.split())),
            "paragraph_count": len(xpath(".//w:body//w:p[not(ancestor::w:tbl)]")),
            "source_ole_object_count": len(xpath(".//o:OLEObject")),
            "source_chart_count": len(xpath(".//c:chart")),
            "source_diagram_count": len(xpath(".//dgm:relIds")),
            "source_textbox_count": len(xpath(".//w:txbxContent")),
        }

    def _image_hook(self, image) -> dict[str, str]:
        with image.open() as image_stream:
            source_content = image_stream.read()
        if not source_content:
            raise ParseBaseException("Word 解析失败：内嵌图片为空")
        if len(source_content) > settings.WORD_MAX_SINGLE_IMAGE_BYTES:
            raise ParseBaseException("Word 解析失败：单个内嵌图片超限")

        self._image_occurrence_count += 1
        source_content_type = (image.content_type or "application/octet-stream").lower()
        digest = hashlib.sha256(source_content).hexdigest()
        existing = self._image_assets.get(digest)
        if existing is not None:
            return {"src": str(existing["url"])}
        content, content_type = self._normalize_image_for_vision(
            source_content,
            source_content_type,
        )
        ext = _CONTENT_TYPE_EXT.get(content_type, "bin")

        filename = f"word-image-{len(self._image_assets) + 1:04d}-{digest[:12]}.{ext}"
        object_key = str(PurePosixPath(self.image_prefix) / filename)
        if self.storage is not None and self.image_bucket is not None:
            self.storage.upload_bytes(
                bucket=self.image_bucket,
                object_key=object_key,
                content=content,
                content_type=content_type,
            )
            url = self.storage.build_object_url(self.image_bucket, object_key)
            self._image_upload_count += 1
        else:
            url = f"word-embedded://{digest}.{ext}"

        vision_supported = content_type in _VISION_CONTENT_TYPES
        if not vision_supported:
            self._warnings.append(f"WORD_IMAGE_FORMAT_NOT_VISION_SUPPORTED:{content_type}")
        self._image_bytes_by_url[url] = (content, content_type)
        self._image_assets[digest] = {
            "asset_id": f"word-image-{len(self._image_assets) + 1:04d}",
            "digest": digest,
            "url": url,
            "object_key": object_key if self.storage is not None else None,
            "content_type": content_type,
            "source_content_type": source_content_type,
            "byte_size": len(content),
            "vision_supported": vision_supported,
        }
        return {"src": url}

    def _normalize_image_for_vision(
        self,
        content: bytes,
        content_type: str,
    ) -> tuple[bytes, str]:
        """将 EMF/WMF/SVG 等不能直接预览/识别的向量图转为 PNG。"""

        if content_type not in _VECTOR_CONTENT_TYPES:
            return content, content_type
        source_ext = _CONTENT_TYPE_EXT[content_type]
        try:
            with tempfile.TemporaryDirectory(prefix="word-image-convert-") as temp_dir:
                workspace = Path(temp_dir)
                source = workspace / f"source.{source_ext}"
                source.write_bytes(content)
                profile = workspace / "libreoffice-profile"
                profile.mkdir()
                completed = subprocess.run(
                    [
                        self.legacy_converter_binary,
                        f"-env:UserInstallation={profile.resolve().as_uri()}",
                        "--headless",
                        "--convert-to",
                        "png",
                        "--outdir",
                        str(workspace),
                        str(source),
                    ],
                    check=False,
                    capture_output=True,
                    timeout=min(self.legacy_converter_timeout_seconds, 60),
                )
                rendered = workspace / "source.png"
                if completed.returncode == 0 and rendered.is_file() and rendered.stat().st_size:
                    self._warnings.append(
                        f"WORD_IMAGE_TRANSCODED:{content_type}->image/png"
                    )
                    return rendered.read_bytes(), "image/png"
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
            pass
        self._warnings.append(f"WORD_IMAGE_TRANSCODE_FAILED:{content_type}")
        return content, content_type

    def _render_html(self, html: str) -> HtmlMarkdownRenderer:
        soup = BeautifulSoup(html, "lxml")
        HtmlParseService()._clean_soup(soup)
        root = soup.body or soup
        renderer = HtmlMarkdownRenderer(self._options)
        markdown = renderer.render_children(root)
        if not markdown.strip():
            raise ParseBaseException("Word 解析失败：文档无有效内容")
        self._last_markdown = markdown
        return renderer

    def _convert_legacy_doc(self, source: Path, output_dir: Path) -> Path:
        profile_dir = output_dir / "libreoffice-profile"
        profile_dir.mkdir(parents=True, exist_ok=True)
        try:
            completed = subprocess.run(
                [
                    self.legacy_converter_binary,
                    f"-env:UserInstallation={profile_dir.resolve().as_uri()}",
                    "--headless",
                    "--convert-to",
                    "docx",
                    "--outdir",
                    str(output_dir),
                    str(source),
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=self.legacy_converter_timeout_seconds,
            )
        except FileNotFoundError as exc:
            raise ParseBaseException("Word .doc 转换失败：未安装 LibreOffice") from exc
        except subprocess.TimeoutExpired as exc:
            raise ParseBaseException("Word .doc 转换失败：LibreOffice 执行超时") from exc
        converted = output_dir / f"{source.stem}.docx"
        if completed.returncode != 0 or not converted.is_file() or converted.stat().st_size <= 0:
            reason = (completed.stderr or completed.stdout or "unknown error").strip()[:500]
            raise ParseBaseException(f"Word .doc 转换失败：{reason}")
        return converted
