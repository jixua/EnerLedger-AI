from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.rag.services.storage.base import BaseObjectStorage


@dataclass(slots=True)
class PdfBinaryAsset:
    kind: str  # page / picture / table
    # ``None`` means the backend did not provide page provenance.  In particular,
    # OpenDataLoader image filenames are not a supported source of truth.
    page_number: int | None
    index: int
    ext: str
    content: bytes
    source_path: str | None = None


@dataclass(slots=True)
class PdfImageAsset:
    page_number: int | None
    index: int
    object_key: str
    url: str
    width: int | None = None
    height: int | None = None
    source_path: str | None = None


@dataclass(slots=True)
class PdfPreparedImageAsset:
    page_number: int | None
    index: int
    object_key: str
    url: str
    content_type: str
    content: bytes
    width: int | None = None
    height: int | None = None
    source_path: str | None = None


@dataclass(slots=True)
class PdfParseOptions:
    backend: str = "opendataloader"
    image_bucket: str | None = None
    image_prefix: str | None = None
    image_upload_async: bool = True
    storage: BaseObjectStorage | None = None
    docling_force_ocr: bool = False
    opendataloader_table_method: Literal["default", "cluster"] = "default"
    opendataloader_markdown_with_html: bool = False
    # ``None`` keeps the deployment-wide default.  A per-run override is useful for
    # offline evaluation tools without mutating the shared Settings singleton.
    opendataloader_timeout_seconds: float | None = None
