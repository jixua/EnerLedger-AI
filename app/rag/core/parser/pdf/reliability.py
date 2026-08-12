from __future__ import annotations

import argparse
import importlib.metadata
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

try:  # PyMuPDF 1.24+ exposes the canonical module name.
    import pymupdf
except ImportError:  # pragma: no cover - compatibility with the declared 1.23 floor
    import fitz as pymupdf


_MODULE_NAME = "app.rag.core.parser.pdf.reliability"
_WORKER_ERROR_PREFIX = "ODL_WORKER_ERROR:"
_IMAGE_SUFFIXES = {
    ".avif",
    ".bmp",
    ".gif",
    ".heic",
    ".heif",
    ".jpeg",
    ".jpg",
    ".png",
    ".tif",
    ".tiff",
    ".webp",
}


class PdfReliabilityError(RuntimeError):
    """Machine-readable parser failure understood by the durable document queue."""

    def __init__(
        self,
        message: str,
        *,
        error_code: str,
        retryable: bool = False,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.retryable = retryable
        self.details = dict(details or {})


class PdfPreflightError(PdfReliabilityError):
    """The source PDF is deterministically unsafe or structurally invalid."""


class PdfResourceLimitError(PdfReliabilityError):
    """A deterministic source/output resource ceiling was exceeded."""


class OpenDataLoaderRuntimeError(PdfReliabilityError):
    """OpenDataLoader cannot execute in the current worker runtime."""


class OpenDataLoaderTimeoutError(PdfReliabilityError):
    """The isolated OpenDataLoader process exceeded its wall-clock deadline."""


class OpenDataLoaderConversionError(PdfReliabilityError):
    """The isolated OpenDataLoader process exited unsuccessfully."""


@dataclass(frozen=True, slots=True)
class PdfReliabilityLimits:
    """Hard ceilings applied before and while OpenDataLoader runs."""

    max_pages: int = 1000
    max_images: int = 2000
    max_single_image_pixels: int = 50_000_000
    # 总量是逐页解码工作量，不是同时驻留内存；允许约 75 张 10MP 扫描页，
    # 同时继续由单图像素、压缩流和隔离进程超时限制解压炸弹。
    max_total_image_pixels: int = 750_000_000
    max_total_decoded_image_bytes: int = 3 * 1024 * 1024 * 1024
    max_single_image_bytes: int = 20 * 1024 * 1024
    max_total_image_bytes: int = 200 * 1024 * 1024
    max_output_files: int = 10_000
    max_output_dir_bytes: int = 500 * 1024 * 1024
    max_log_bytes: int = 16 * 1024 * 1024

    def __post_init__(self) -> None:
        for field_name in (
            "max_pages",
            "max_images",
            "max_single_image_pixels",
            "max_total_image_pixels",
            "max_total_decoded_image_bytes",
            "max_single_image_bytes",
            "max_total_image_bytes",
            "max_output_files",
            "max_output_dir_bytes",
            "max_log_bytes",
        ):
            if getattr(self, field_name) <= 0:
                raise ValueError(f"{field_name} must be greater than zero")

    def to_dict(self) -> dict[str, int]:
        return {
            "max_pages": self.max_pages,
            "max_images": self.max_images,
            "max_single_image_pixels": self.max_single_image_pixels,
            "max_total_image_pixels": self.max_total_image_pixels,
            "max_total_decoded_image_bytes": self.max_total_decoded_image_bytes,
            "max_single_image_bytes": self.max_single_image_bytes,
            "max_total_image_bytes": self.max_total_image_bytes,
            "max_output_files": self.max_output_files,
            "max_output_dir_bytes": self.max_output_dir_bytes,
            "max_log_bytes": self.max_log_bytes,
        }


@dataclass(frozen=True, slots=True)
class PdfPreflightReport:
    source_bytes: int
    pdf_header_offset: int
    page_count: int
    image_count: int
    unique_image_count: int
    inline_image_count: int
    largest_image_pixels: int
    total_image_pixels: int
    largest_decoded_image_bytes: int
    total_decoded_image_bytes: int
    largest_image_bytes: int
    total_image_bytes: int
    estimated_inline_image_bytes: int
    encrypted: bool
    repaired: bool
    warnings: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "source_bytes": self.source_bytes,
            "pdf_header_offset": self.pdf_header_offset,
            "page_count": self.page_count,
            "image_count": self.image_count,
            "unique_image_count": self.unique_image_count,
            "inline_image_count": self.inline_image_count,
            "largest_image_pixels": self.largest_image_pixels,
            "total_image_pixels": self.total_image_pixels,
            "largest_decoded_image_bytes": self.largest_decoded_image_bytes,
            "total_decoded_image_bytes": self.total_decoded_image_bytes,
            "largest_image_bytes": self.largest_image_bytes,
            "total_image_bytes": self.total_image_bytes,
            "estimated_inline_image_bytes": self.estimated_inline_image_bytes,
            "encrypted": self.encrypted,
            "repaired": self.repaired,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True, slots=True)
class OutputDirectoryStats:
    file_count: int
    total_bytes: int
    image_count: int
    largest_image_bytes: int
    total_image_bytes: int

    def to_dict(self) -> dict[str, int]:
        return {
            "file_count": self.file_count,
            "total_bytes": self.total_bytes,
            "image_count": self.image_count,
            "largest_image_bytes": self.largest_image_bytes,
            "total_image_bytes": self.total_image_bytes,
        }


@dataclass(frozen=True, slots=True)
class OpenDataLoaderRunReport:
    duration_ms: int
    output: OutputDirectoryStats

    def to_dict(self) -> dict[str, object]:
        return {
            "duration_ms": self.duration_ms,
            "output": self.output.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class OpenDataLoaderRuntimeHealth:
    ready: bool
    package_available: bool
    java_available: bool
    java_major_version: int | None
    package_version: str | None
    java_version: str | None
    error_code: str | None
    message: str

    def to_dict(self) -> dict[str, object]:
        return {
            "status": "ready" if self.ready else "failed",
            "package_available": self.package_available,
            "java_available": self.java_available,
            "java_major_version": self.java_major_version,
            "package_version": self.package_version,
            "java_version": self.java_version,
            "error_code": self.error_code,
            "message": self.message,
        }

    def require_ready(self) -> None:
        if self.ready:
            return
        raise OpenDataLoaderRuntimeError(
            self.message,
            error_code=self.error_code or "ODL_RUNTIME_UNAVAILABLE",
            retryable=False,
            details=self.to_dict(),
        )


class PdfReliabilityGuard:
    """Validate a PDF and bound embedded-image work before parser execution."""

    _HEADER_SCAN_BYTES = 1024

    def __init__(self, limits: PdfReliabilityLimits | None = None) -> None:
        self.limits = limits or PdfReliabilityLimits()

    def inspect(self, source: str | Path) -> PdfPreflightReport:
        source_path = Path(source)
        try:
            source_bytes = source_path.stat().st_size
            with source_path.open("rb") as stream:
                prefix = stream.read(self._HEADER_SCAN_BYTES)
        except (OSError, ValueError) as exc:
            raise PdfPreflightError(
                f"PDF 源文件不可读: {type(exc).__name__}",
                error_code="PDF_SOURCE_UNREADABLE",
                retryable=False,
            ) from exc

        header_offset = prefix.find(b"%PDF-")
        if header_offset < 0:
            raise PdfPreflightError(
                "源文件缺少 PDF magic bytes",
                error_code="PDF_MAGIC_INVALID",
                retryable=False,
                details={"scanned_prefix_bytes": len(prefix)},
            )

        try:
            document = pymupdf.open(filename=str(source_path))
        except Exception as exc:
            raise PdfPreflightError(
                f"PDF 结构无法打开: {type(exc).__name__}",
                error_code="PDF_STRUCTURE_INVALID",
                retryable=False,
            ) from exc

        warnings: list[str] = []
        try:
            encrypted = bool(document.needs_pass or document.is_encrypted)
            if encrypted:
                raise PdfPreflightError(
                    "PDF 已加密，当前解析链路不接受加密文档",
                    error_code="PDF_ENCRYPTED",
                    retryable=False,
                )

            page_count = int(document.page_count)
            if page_count <= 0:
                raise PdfPreflightError(
                    "PDF 不包含可解析页",
                    error_code="PDF_STRUCTURE_INVALID",
                    retryable=False,
                    details={"page_count": page_count},
                )
            if page_count > self.limits.max_pages:
                self._raise_limit(
                    "PDF_MAX_PAGES_EXCEEDED",
                    "PDF 页数超过限制",
                    actual=page_count,
                    limit=self.limits.max_pages,
                )

            repaired = bool(getattr(document, "is_repaired", False))
            if repaired:
                warnings.append("PDF_STRUCTURE_REPAIRED")

            image_count = 0
            unique_xrefs: set[int] = set()
            inline_image_count = 0
            largest_image_pixels = 0
            total_image_pixels = 0
            largest_decoded_image_bytes = 0
            total_decoded_image_bytes = 0
            largest_image_bytes = 0
            total_image_bytes = 0
            estimated_inline_image_bytes = 0
            for page_index in range(page_count):
                try:
                    page = document.load_page(page_index)
                    # ``get_image_info`` sees both regular XObject images and inline
                    # image operators. ``get_images`` alone can omit or underdescribe the
                    # latter, allowing xref=0 content to bypass pre-decode ceilings.
                    raw_images = page.get_image_info(xrefs=True)
                except Exception as exc:
                    raise PdfPreflightError(
                        f"PDF 第 {page_index + 1} 页结构无法读取",
                        error_code="PDF_STRUCTURE_INVALID",
                        retryable=False,
                        details={
                            "page_number": page_index + 1,
                            "error_type": type(exc).__name__,
                        },
                    ) from exc

                images: list[dict[str, object]] = []
                ignored_empty_images = 0
                for image in raw_images:
                    if self._is_empty_inline_image_placeholder(image):
                        ignored_empty_images += 1
                    else:
                        images.append(image)
                if ignored_empty_images:
                    warnings.append(
                        "PDF_EMPTY_INLINE_IMAGE_PLACEHOLDER_IGNORED:"
                        f"page={page_index + 1},count={ignored_empty_images}"
                    )

                image_count += len(images)
                if image_count > self.limits.max_images:
                    self._raise_limit(
                        "PDF_MAX_IMAGES_EXCEEDED",
                        "PDF 图片引用数超过限制",
                        actual=image_count,
                        limit=self.limits.max_images,
                    )

                for image in images:
                    xref = 0
                    try:
                        xref = int(image.get("xref") or 0)
                        width = int(image["width"])
                        height = int(image["height"])
                        bits_per_component = int(image["bpc"])
                        color_components = self._image_color_components(
                            image.get("colorspace"),
                            image.get("cs-name"),
                        )
                    except (AttributeError, KeyError, TypeError, ValueError) as exc:
                        raise PdfPreflightError(
                            f"PDF 第 {page_index + 1} 页图片元数据无效",
                            error_code="PDF_STRUCTURE_INVALID",
                            retryable=False,
                            details={
                                "page_number": page_index + 1,
                                "xref": xref,
                                "error_type": type(exc).__name__,
                            },
                        ) from exc
                    if width <= 0 or height <= 0 or bits_per_component <= 0:
                        raise PdfPreflightError(
                            f"PDF 第 {page_index + 1} 页图片尺寸或位深无效",
                            error_code="PDF_STRUCTURE_INVALID",
                            retryable=False,
                            details={
                                "page_number": page_index + 1,
                                "xref": xref,
                                "width": width,
                                "height": height,
                                "bits_per_component": bits_per_component,
                            },
                        )

                    image_pixels = width * height
                    largest_image_pixels = max(largest_image_pixels, image_pixels)
                    if image_pixels > self.limits.max_single_image_pixels:
                        self._raise_limit(
                            "PDF_SINGLE_IMAGE_PIXELS_EXCEEDED",
                            "PDF 单张图片像素数超过限制",
                            actual=image_pixels,
                            limit=self.limits.max_single_image_pixels,
                            page_number=page_index + 1,
                            xref=xref,
                            width=width,
                            height=height,
                        )
                    # Pixel work is per image occurrence. A shared xref repeated on many
                    # pages can still force repeated decoder/render work in ODL.
                    total_image_pixels += image_pixels
                    if total_image_pixels > self.limits.max_total_image_pixels:
                        self._raise_limit(
                            "PDF_TOTAL_IMAGE_PIXELS_EXCEEDED",
                            "PDF 图片总像素数超过限制",
                            actual=total_image_pixels,
                            limit=self.limits.max_total_image_pixels,
                        )

                    decoded_bytes = self._decoded_image_bytes(
                        width=width,
                        height=height,
                        bits_per_component=bits_per_component,
                        color_components=color_components,
                    )
                    largest_decoded_image_bytes = max(
                        largest_decoded_image_bytes,
                        decoded_bytes,
                    )
                    total_decoded_image_bytes += decoded_bytes
                    if (
                        total_decoded_image_bytes
                        > self.limits.max_total_decoded_image_bytes
                    ):
                        self._raise_limit(
                            "PDF_TOTAL_DECODED_IMAGE_BYTES_EXCEEDED",
                            "PDF 图片总解码估算字节超过限制",
                            actual=total_decoded_image_bytes,
                            limit=self.limits.max_total_decoded_image_bytes,
                        )
                    if xref <= 0:
                        inline_image_count += 1
                        estimated_inline_image_bytes += decoded_bytes
                        continue
                    else:
                        if xref in unique_xrefs:
                            continue
                        unique_xrefs.add(xref)
                        try:
                            compressed_bytes = self._image_stream_length(document, xref)
                        except Exception as exc:
                            raise PdfPreflightError(
                                f"PDF 内嵌图片 xref={xref} 字节长度无法读取",
                                error_code="PDF_STRUCTURE_INVALID",
                                retryable=False,
                                details={
                                    "xref": xref,
                                    "error_type": type(exc).__name__,
                                },
                            ) from exc
                    largest_image_bytes = max(largest_image_bytes, compressed_bytes)
                    if compressed_bytes > self.limits.max_single_image_bytes:
                        self._raise_limit(
                            "PDF_SINGLE_IMAGE_BYTES_EXCEEDED",
                            "PDF 单张内嵌图片压缩流超过字节限制",
                            actual=compressed_bytes,
                            limit=self.limits.max_single_image_bytes,
                            page_number=page_index + 1,
                            xref=xref,
                        )
                    total_image_bytes += compressed_bytes
                    if total_image_bytes > self.limits.max_total_image_bytes:
                        self._raise_limit(
                            "PDF_TOTAL_IMAGE_BYTES_EXCEEDED",
                            "PDF 内嵌图片总压缩流字节超过限制",
                            actual=total_image_bytes,
                            limit=self.limits.max_total_image_bytes,
                        )
        finally:
            document.close()

        return PdfPreflightReport(
            source_bytes=source_bytes,
            pdf_header_offset=header_offset,
            page_count=page_count,
            image_count=image_count,
            unique_image_count=len(unique_xrefs) + inline_image_count,
            inline_image_count=inline_image_count,
            largest_image_pixels=largest_image_pixels,
            total_image_pixels=total_image_pixels,
            largest_decoded_image_bytes=largest_decoded_image_bytes,
            total_decoded_image_bytes=total_decoded_image_bytes,
            largest_image_bytes=largest_image_bytes,
            total_image_bytes=total_image_bytes,
            estimated_inline_image_bytes=estimated_inline_image_bytes,
            encrypted=False,
            repaired=repaired,
            warnings=tuple(warnings),
        )

    @staticmethod
    def _is_empty_inline_image_placeholder(image: object) -> bool:
        """识别排版软件遗留的不可见 0x0 inline image 占位符。"""

        if not isinstance(image, dict):
            return False
        try:
            xref = int(image.get("xref") or 0)
            width = int(image["width"])
            height = int(image["height"])
            size = int(image["size"])
            bbox = tuple(float(value) for value in image["bbox"])
            transform = tuple(float(value) for value in image["transform"])
        except (KeyError, TypeError, ValueError):
            return False
        return (
            xref == 0
            and width == 0
            and height == 0
            and size == 0
            and len(bbox) == 4
            and not any(bbox)
            and len(transform) == 6
            and not any(transform)
        )

    @staticmethod
    def _image_color_components(color_space: object, color_space_name: object = "") -> int:
        if isinstance(color_space, int) and color_space > 0:
            return color_space
        normalized = f"{color_space or ''} {color_space_name or ''}".casefold()
        if "gray" in normalized or "mono" in normalized:
            return 1
        if "cmyk" in normalized:
            return 4
        if "rgb" in normalized:
            return 3
        # Indexed/ICCBased/unknown spaces may expand during decode. Four channels is
        # a conservative floor for the pre-decode memory estimate.
        return 4

    @staticmethod
    def _decoded_image_bytes(
        *,
        width: int,
        height: int,
        bits_per_component: int,
        color_components: int,
    ) -> int:
        bits = width * height * bits_per_component * max(color_components, 1)
        # Keep a four-byte-per-pixel safety floor for common RGB images: decoders and
        # renderers frequently expand them to aligned RGBA buffers even when the PDF
        # source declares only three components.
        return max(width * height * 4, math.ceil(bits / 8))

    @staticmethod
    def _image_stream_length(document: pymupdf.Document, xref: int) -> int:
        """Read an image stream's declared byte length without materializing it.

        ``extract_image`` can allocate the entire image before a caller gets a chance
        to compare its size with a limit. PDF streams are required to expose
        ``/Length``; rejecting a missing/invalid value is safer than defeating the
        memory guard while trying to measure an untrusted stream.
        """

        value_type, raw_value = document.xref_get_key(xref, "Length")
        if value_type == "int":
            length = int(raw_value)
        elif value_type == "xref":
            referenced_xref_match = re.match(r"\s*(\d+)\s+\d+\s+R\s*$", raw_value or "")
            if referenced_xref_match is None:
                raise ValueError("invalid indirect Length reference")
            referenced_xref = int(referenced_xref_match.group(1))
            raw_object = document.xref_object(referenced_xref, compressed=False).strip()
            if not raw_object.isdigit():
                raise ValueError("indirect Length object is not an integer")
            length = int(raw_object)
        else:
            raise ValueError("image stream Length is missing")
        if length < 0:
            raise ValueError("image stream Length must not be negative")
        return length

    @staticmethod
    def _raise_limit(
        error_code: str,
        message: str,
        *,
        actual: int,
        limit: int,
        **details: object,
    ) -> None:
        raise PdfResourceLimitError(
            f"{message}: actual={actual}, limit={limit}",
            error_code=error_code,
            retryable=False,
            details={"actual": actual, "limit": limit, **details},
        )


def inspect_output_directory(
    output_dir: str | Path,
    *,
    max_files: int | None = None,
) -> OutputDirectoryStats:
    """Return bounded-parser output sizes without following symbolic links."""

    root = Path(output_dir)
    if not root.exists():
        return OutputDirectoryStats(0, 0, 0, 0, 0)

    file_count = 0
    total_bytes = 0
    image_count = 0
    largest_image_bytes = 0
    total_image_bytes = 0
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = os.scandir(current)
        except FileNotFoundError:
            continue
        with entries:
            for entry in entries:
                try:
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    size = entry.stat(follow_symlinks=False).st_size
                except FileNotFoundError:
                    continue
                file_count += 1
                total_bytes += size
                if Path(entry.name).suffix.lower() in _IMAGE_SUFFIXES:
                    image_count += 1
                    total_image_bytes += size
                    largest_image_bytes = max(largest_image_bytes, size)
                if max_files is not None and file_count > max_files:
                    return OutputDirectoryStats(
                        file_count=file_count,
                        total_bytes=total_bytes,
                        image_count=image_count,
                        largest_image_bytes=largest_image_bytes,
                        total_image_bytes=total_image_bytes,
                    )
    return OutputDirectoryStats(
        file_count=file_count,
        total_bytes=total_bytes,
        image_count=image_count,
        largest_image_bytes=largest_image_bytes,
        total_image_bytes=total_image_bytes,
    )


def enforce_output_limits(
    output_dir: str | Path,
    limits: PdfReliabilityLimits,
) -> OutputDirectoryStats:
    stats = inspect_output_directory(output_dir, max_files=limits.max_output_files)
    checks = (
        (
            stats.file_count,
            limits.max_output_files,
            "ODL_MAX_OUTPUT_FILES_EXCEEDED",
            "OpenDataLoader 输出文件数超过限制",
        ),
        (
            stats.image_count,
            limits.max_images,
            "ODL_MAX_IMAGES_EXCEEDED",
            "OpenDataLoader 输出图片数超过限制",
        ),
        (
            stats.largest_image_bytes,
            limits.max_single_image_bytes,
            "ODL_SINGLE_IMAGE_BYTES_EXCEEDED",
            "OpenDataLoader 单张输出图片超过字节限制",
        ),
        (
            stats.total_image_bytes,
            limits.max_total_image_bytes,
            "ODL_TOTAL_IMAGE_BYTES_EXCEEDED",
            "OpenDataLoader 输出图片总字节超过限制",
        ),
        (
            stats.total_bytes,
            limits.max_output_dir_bytes,
            "ODL_OUTPUT_DIR_BYTES_EXCEEDED",
            "OpenDataLoader 输出目录超过字节限制",
        ),
    )
    for actual, limit, error_code, message in checks:
        if actual > limit:
            raise PdfResourceLimitError(
                f"{message}: actual={actual}, limit={limit}",
                error_code=error_code,
                retryable=False,
                details={"actual": actual, "limit": limit, "output": stats.to_dict()},
            )
    return stats


_CommandFactory = Callable[[Path, Path, Path, str, str, bool], Sequence[str]]


class OpenDataLoaderProcessRunner:
    """Run OpenDataLoader in an isolated process session with hard limits.

    Killing the new process group terminates both the Python worker and the Java
    process it launches. This is intentionally stronger than wrapping the in-process
    SDK call in ``asyncio.wait_for`` or a thread timeout.
    """

    def __init__(
        self,
        *,
        timeout_seconds: float = 300,
        poll_interval_seconds: float = 0.2,
        termination_grace_seconds: float = 2.0,
        command_factory: _CommandFactory | None = None,
    ) -> None:
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        if not math.isfinite(poll_interval_seconds) or poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be greater than zero")
        if termination_grace_seconds < 0:
            raise ValueError("termination_grace_seconds must not be negative")
        self.timeout_seconds = timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.termination_grace_seconds = termination_grace_seconds
        self._command_factory = command_factory or self._default_command

    def run(
        self,
        *,
        source: str | Path,
        output_dir: str | Path,
        image_dir: str | Path,
        page_marker_template: str,
        limits: PdfReliabilityLimits,
        table_method: str = "default",
        markdown_with_html: bool = False,
    ) -> OpenDataLoaderRunReport:
        if table_method not in {"default", "cluster"}:
            raise ValueError("table_method must be default or cluster")
        source_path = Path(source).resolve()
        output_path = Path(output_dir).resolve()
        image_path = Path(image_dir).resolve()
        output_path.mkdir(parents=True, exist_ok=True)
        image_path.mkdir(parents=True, exist_ok=True)
        command = list(
            self._command_factory(
                source_path,
                output_path,
                image_path,
                page_marker_template,
                table_method,
                markdown_with_html,
            )
        )
        if not command:
            raise ValueError("command_factory returned an empty command")

        started_at = time.monotonic()
        # Redirect noisy Java/Python output to disk-backed temporary files. PIPE plus
        # repeated ``communicate(timeout=...)`` retains every byte in Python memory and
        # lets an untrusted PDF drive unbounded worker RSS before any output limit fires.
        with tempfile.TemporaryFile(mode="w+b") as stdout_file, tempfile.TemporaryFile(
            mode="w+b"
        ) as stderr_file:
            try:
                process = subprocess.Popen(
                    command,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    start_new_session=True,
                )
            except OSError as exc:
                raise OpenDataLoaderRuntimeError(
                    f"OpenDataLoader 独立进程无法启动: {type(exc).__name__}",
                    error_code="ODL_PROCESS_START_FAILED",
                    retryable=False,
                ) from exc

            try:
                while True:
                    try:
                        self._enforce_log_limit(stdout_file, stderr_file, limits)
                        enforce_output_limits(output_path, limits)
                    except PdfResourceLimitError:
                        self._terminate_process_group(process)
                        raise

                    elapsed = time.monotonic() - started_at
                    remaining = self.timeout_seconds - elapsed
                    if remaining <= 0:
                        self._terminate_process_group(process)
                        raise OpenDataLoaderTimeoutError(
                            f"OpenDataLoader 超过 {self.timeout_seconds:g} 秒解析时限",
                            error_code="ODL_TIMEOUT",
                            retryable=True,
                            details={"timeout_seconds": self.timeout_seconds},
                        )

                    try:
                        process.wait(timeout=min(self.poll_interval_seconds, remaining))
                        break
                    except subprocess.TimeoutExpired:
                        continue

                self._enforce_log_limit(stdout_file, stderr_file, limits)
                stdout = self._read_log_tail(stdout_file)
                stderr = self._read_log_tail(stderr_file)
                stats = enforce_output_limits(output_path, limits)
                if process.returncode != 0:
                    error_payload = self._parse_worker_error(stderr)
                    raise OpenDataLoaderConversionError(
                        str(
                            error_payload.get("message")
                            or "OpenDataLoader 独立进程执行失败"
                        ),
                        error_code=str(
                            error_payload.get("error_code") or "ODL_CONVERT_FAILED"
                        ),
                        retryable=bool(error_payload.get("retryable", True)),
                        details={
                            "return_code": process.returncode,
                            "worker_stdout": stdout[-1000:],
                            "worker_stderr": stderr[-1000:],
                        },
                    )
                return OpenDataLoaderRunReport(
                    duration_ms=int((time.monotonic() - started_at) * 1000),
                    output=stats,
                )
            finally:
                if process.poll() is None:
                    self._terminate_process_group(process)

    @staticmethod
    def _default_command(
        source: Path,
        output_dir: Path,
        image_dir: Path,
        page_marker_template: str,
        table_method: str,
        markdown_with_html: bool,
    ) -> Sequence[str]:
        command = [
            sys.executable,
            "-m",
            _MODULE_NAME,
            "_odl_worker",
            "--input",
            str(source),
            "--output-dir",
            str(output_dir),
            "--image-dir",
            str(image_dir),
            "--page-marker-template",
            page_marker_template,
            "--table-method",
            table_method,
        ]
        if markdown_with_html:
            command.append("--markdown-with-html")
        return command

    @staticmethod
    def _read_log_tail(stream: BinaryIO, *, max_bytes: int = 64 * 1024) -> str:
        stream.flush()
        size = stream.seek(0, os.SEEK_END)
        stream.seek(max(0, size - max_bytes))
        return stream.read(max_bytes).decode("utf-8", errors="replace")

    @staticmethod
    def _enforce_log_limit(
        stdout_stream: BinaryIO,
        stderr_stream: BinaryIO,
        limits: PdfReliabilityLimits,
    ) -> None:
        stdout_bytes = os.fstat(stdout_stream.fileno()).st_size
        stderr_bytes = os.fstat(stderr_stream.fileno()).st_size
        total_bytes = stdout_bytes + stderr_bytes
        if total_bytes <= limits.max_log_bytes:
            return
        raise PdfResourceLimitError(
            "OpenDataLoader 日志总字节超过限制: "
            f"actual={total_bytes}, limit={limits.max_log_bytes}",
            error_code="ODL_LOG_BYTES_EXCEEDED",
            retryable=False,
            details={
                "actual": total_bytes,
                "limit": limits.max_log_bytes,
                "stdout_bytes": stdout_bytes,
                "stderr_bytes": stderr_bytes,
            },
        )

    def _terminate_process_group(self, process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            process.terminate()
        try:
            process.wait(timeout=self.termination_grace_seconds)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            process.kill()
        process.wait()

    @staticmethod
    def _parse_worker_error(stderr: str) -> dict[str, object]:
        for line in reversed((stderr or "").splitlines()):
            if not line.startswith(_WORKER_ERROR_PREFIX):
                continue
            try:
                payload = json.loads(line.removeprefix(_WORKER_ERROR_PREFIX))
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                return payload
        return {"message": (stderr or "").strip()[-1000:] or "OpenDataLoader 转换失败"}


class OpenDataLoaderHealthChecker:
    """Check the Python adapter and Java 11+ runtime without parsing user data."""

    def __init__(self, *, timeout_seconds: float = 5.0) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        self.timeout_seconds = timeout_seconds

    def check(self) -> OpenDataLoaderRuntimeHealth:
        package_version = self._package_version()
        package_result = self._run_health_worker()
        if package_result is not None:
            error_code, message = package_result
            return OpenDataLoaderRuntimeHealth(
                ready=False,
                package_available=False,
                java_available=False,
                java_major_version=None,
                package_version=package_version,
                java_version=None,
                error_code=error_code,
                message=message,
            )

        java_path = shutil.which("java")
        if not java_path:
            return OpenDataLoaderRuntimeHealth(
                ready=False,
                package_available=True,
                java_available=False,
                java_major_version=None,
                package_version=package_version,
                java_version=None,
                error_code="ODL_JAVA_MISSING",
                message="未检测到 java 命令，OpenDataLoader 需要 Java 11+",
            )

        try:
            result = subprocess.run(
                [java_path, "-version"],
                capture_output=True,
                text=True,
                check=False,
                timeout=self.timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            return OpenDataLoaderRuntimeHealth(
                ready=False,
                package_available=True,
                java_available=True,
                java_major_version=None,
                package_version=package_version,
                java_version=None,
                error_code="ODL_JAVA_CHECK_TIMEOUT",
                message="java -version 健康检查超时",
            )
        except OSError as exc:
            return OpenDataLoaderRuntimeHealth(
                ready=False,
                package_available=True,
                java_available=False,
                java_major_version=None,
                package_version=package_version,
                java_version=None,
                error_code="ODL_JAVA_CHECK_FAILED",
                message=f"java -version 健康检查失败: {type(exc).__name__}",
            )

        version_output = (result.stderr or result.stdout or "").strip()
        version_line = version_output.splitlines()[0] if version_output else None
        java_major = parse_java_major_version(version_output)
        if result.returncode != 0:
            return OpenDataLoaderRuntimeHealth(
                ready=False,
                package_available=True,
                java_available=True,
                java_major_version=java_major,
                package_version=package_version,
                java_version=version_line,
                error_code="ODL_JAVA_CHECK_FAILED",
                message=f"java -version 退出码为 {result.returncode}",
            )
        if java_major is None:
            return OpenDataLoaderRuntimeHealth(
                ready=False,
                package_available=True,
                java_available=True,
                java_major_version=None,
                package_version=package_version,
                java_version=version_line,
                error_code="ODL_JAVA_VERSION_UNKNOWN",
                message=f"无法识别 Java 版本: {version_line or ''}",
            )
        if java_major < 11:
            return OpenDataLoaderRuntimeHealth(
                ready=False,
                package_available=True,
                java_available=True,
                java_major_version=java_major,
                package_version=package_version,
                java_version=version_line,
                error_code="ODL_JAVA_VERSION_UNSUPPORTED",
                message=f"当前 Java 版本为 {java_major}，OpenDataLoader 要求 Java 11+",
            )
        return OpenDataLoaderRuntimeHealth(
            ready=True,
            package_available=True,
            java_available=True,
            java_major_version=java_major,
            package_version=package_version,
            java_version=version_line,
            error_code=None,
            message="OpenDataLoader Python 依赖与 Java 运行时可用",
        )

    def _run_health_worker(self) -> tuple[str, str] | None:
        try:
            result = subprocess.run(
                [sys.executable, "-m", _MODULE_NAME, "_health_worker"],
                capture_output=True,
                text=True,
                check=False,
                timeout=self.timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            return "ODL_DEPENDENCY_CHECK_TIMEOUT", "OpenDataLoader Python 依赖健康检查超时"
        except OSError as exc:
            return (
                "ODL_DEPENDENCY_CHECK_FAILED",
                f"OpenDataLoader Python 依赖健康检查失败: {type(exc).__name__}",
            )
        if result.returncode == 0:
            return None
        payload = OpenDataLoaderProcessRunner._parse_worker_error(result.stderr)
        return (
            str(payload.get("error_code") or "ODL_DEPENDENCY_UNAVAILABLE"),
            str(payload.get("message") or "opendataloader-pdf 不可用"),
        )

    @staticmethod
    def _package_version() -> str | None:
        try:
            return importlib.metadata.version("opendataloader-pdf")
        except importlib.metadata.PackageNotFoundError:
            return None


def parse_java_major_version(version_output: str) -> int | None:
    match = re.search(r'version "([^"]+)"', version_output or "")
    if not match:
        return None
    version = match.group(1)
    if version.startswith("1."):
        parts = version.split(".")
        return int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
    major = version.split(".", 1)[0]
    return int(major) if major.isdigit() else None


def _emit_worker_error(*, error_code: str, message: str, retryable: bool) -> None:
    payload = json.dumps(
        {"error_code": error_code, "message": message, "retryable": retryable},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    print(f"{_WORKER_ERROR_PREFIX}{payload}", file=sys.stderr, flush=True)


def _run_health_worker() -> int:
    try:
        import opendataloader_pdf
    except ImportError as exc:
        _emit_worker_error(
            error_code="ODL_DEPENDENCY_MISSING",
            message=f"opendataloader-pdf 未安装: {type(exc).__name__}",
            retryable=False,
        )
        return 20
    except Exception as exc:
        _emit_worker_error(
            error_code="ODL_DEPENDENCY_IMPORT_FAILED",
            message=f"opendataloader-pdf 导入失败: {type(exc).__name__}",
            retryable=False,
        )
        return 21
    if not callable(getattr(opendataloader_pdf, "convert", None)):
        _emit_worker_error(
            error_code="ODL_DEPENDENCY_INVALID",
            message="opendataloader-pdf 未暴露可调用的 convert",
            retryable=False,
        )
        return 22
    return 0


def _run_odl_worker(arguments: argparse.Namespace) -> int:
    try:
        import opendataloader_pdf
    except ImportError as exc:
        _emit_worker_error(
            error_code="ODL_DEPENDENCY_MISSING",
            message=f"opendataloader-pdf 未安装: {type(exc).__name__}",
            retryable=False,
        )
        return 20

    try:
        opendataloader_pdf.convert(
            input_path=[arguments.input],
            output_dir=arguments.output_dir,
            format="markdown-with-images",
            table_method=arguments.table_method,
            markdown_with_html=arguments.markdown_with_html,
            markdown_page_separator=arguments.page_marker_template,
            image_output="external",
            image_dir=arguments.image_dir,
            quiet=True,
        )
    except Exception as exc:
        error_code, retryable = _classify_odl_conversion_error(exc)
        _emit_worker_error(
            error_code=error_code,
            message=f"OpenDataLoader 转换失败: {type(exc).__name__}: {exc}",
            retryable=retryable,
        )
        return 30
    return 0


def _classify_odl_conversion_error(error: BaseException) -> tuple[str, bool]:
    """Separate deterministic input failures from transient runtime failures."""

    if isinstance(
        error,
        (FileNotFoundError, PermissionError, TypeError, ValueError, UnicodeError),
    ):
        return "ODL_INPUT_INVALID", False
    normalized = f"{type(error).__name__}: {error}".casefold()
    http_status_match = re.search(
        r"(?:http(?:\s+status)?|status(?:_code)?|api error)\D{0,12}([1-5]\d{2})\b",
        normalized,
    )
    if http_status_match is not None:
        status = int(http_status_match.group(1))
        return (
            f"ODL_HTTP_{status}",
            status in {408, 425, 429} or 500 <= status <= 599,
        )
    deterministic_markers = (
        "encrypted",
        "password",
        "malformed",
        "corrupt",
        "damaged pdf",
        "invalid xref",
        "syntax error",
        "unsupported pdf",
        "invalid pdf",
        "invalid api key",
        "unauthorized",
        "forbidden",
        "not configured",
        "configuration error",
        "content_filter",
        "content filter",
        "safety policy",
        "moderation blocked",
    )
    if any(marker in normalized for marker in deterministic_markers):
        return "ODL_INPUT_INVALID", False
    transient_markers = (
        "timeout",
        "temporarily",
        "connection",
        "unavailable",
        "resource busy",
    )
    if any(marker in normalized for marker in transient_markers):
        return "ODL_TRANSIENT_FAILURE", True
    # Unknown Java/parser crashes may be environmental; bounded queue retry remains safer.
    return "ODL_CONVERT_FAILED", True


def _build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("_health_worker", add_help=False)
    convert = subparsers.add_parser("_odl_worker", add_help=False)
    convert.add_argument("--input", required=True)
    convert.add_argument("--output-dir", required=True)
    convert.add_argument("--image-dir", required=True)
    convert.add_argument("--page-marker-template", required=True)
    convert.add_argument("--table-method", choices=("default", "cluster"), default="default")
    convert.add_argument("--markdown-with-html", action="store_true")
    return parser


def _main(argv: Sequence[str] | None = None) -> int:
    arguments = _build_cli_parser().parse_args(argv)
    if arguments.command == "_health_worker":
        return _run_health_worker()
    if arguments.command == "_odl_worker":
        return _run_odl_worker(arguments)
    return 2


if __name__ == "__main__":  # pragma: no cover - exercised through subprocess integration tests
    raise SystemExit(_main())


__all__ = [
    "OpenDataLoaderConversionError",
    "OpenDataLoaderHealthChecker",
    "OpenDataLoaderProcessRunner",
    "OpenDataLoaderRunReport",
    "OpenDataLoaderRuntimeError",
    "OpenDataLoaderRuntimeHealth",
    "OpenDataLoaderTimeoutError",
    "OutputDirectoryStats",
    "PdfPreflightError",
    "PdfPreflightReport",
    "PdfReliabilityError",
    "PdfReliabilityGuard",
    "PdfReliabilityLimits",
    "PdfResourceLimitError",
    "enforce_output_limits",
    "inspect_output_directory",
    "parse_java_major_version",
]
