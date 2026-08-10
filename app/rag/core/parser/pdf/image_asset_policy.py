"""PDF image asset classification and retrieval eligibility policy.

Every image remains available as a preview asset.  Only substantive images with
complete page provenance and an adequate structured visual description may become
retrieval assets.  Page numbers come from ``ODL_PAGE`` or explicit page metadata,
never from names such as ``imageFile12.png``.
"""

from __future__ import annotations

import html
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from urllib.parse import unquote, urlsplit


class ImageAssetCategory(StrEnum):
    SIGNATURE = "SIGNATURE"
    SEAL = "SEAL"
    DECORATIVE = "DECORATIVE"
    CHART = "CHART"
    FLOWCHART = "FLOWCHART"
    SYSTEM_BOUNDARY = "SYSTEM_BOUNDARY"
    PHOTO = "PHOTO"
    UNKNOWN = "UNKNOWN"


class ImageAssetUsage(StrEnum):
    PREVIEW_ONLY = "PREVIEW_ONLY"
    PREVIEW_AND_RETRIEVAL = "PREVIEW_AND_RETRIEVAL"


class VisualDescriptionStatus(StrEnum):
    NOT_REQUIRED = "NOT_REQUIRED"
    REQUIRED = "REQUIRED"
    COMPLETE = "COMPLETE"
    INVALID = "INVALID"


@dataclass(frozen=True, slots=True)
class VisualRelationship:
    source: str
    relation: str
    target: str

    def to_dict(self) -> dict[str, str]:
        return {
            "source": self.source,
            "relation": self.relation,
            "target": self.target,
        }


@dataclass(frozen=True, slots=True)
class StructuredVisualDescription:
    source_page: int
    summary: str
    entities: tuple[str, ...] = ()
    relationships: tuple[VisualRelationship, ...] = ()
    key_values: tuple[tuple[str, str], ...] = ()
    units: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> StructuredVisualDescription:
        relationships: list[VisualRelationship] = []
        raw_relationships = value.get("relationships")
        if isinstance(raw_relationships, Sequence) and not isinstance(
            raw_relationships, (str, bytes)
        ):
            for item in raw_relationships:
                if not isinstance(item, Mapping):
                    continue
                source = str(item.get("source") or "").strip()
                relation = str(item.get("relation") or "").strip()
                target = str(item.get("target") or "").strip()
                if source and relation and target:
                    relationships.append(VisualRelationship(source, relation, target))

        key_values: list[tuple[str, str]] = []
        raw_key_values = value.get("key_values")
        if isinstance(raw_key_values, Mapping):
            key_values.extend(
                (str(key).strip(), str(item).strip())
                for key, item in raw_key_values.items()
                if str(key).strip() and str(item).strip()
            )
        elif isinstance(raw_key_values, Sequence) and not isinstance(
            raw_key_values, (str, bytes)
        ):
            for item in raw_key_values:
                if not isinstance(item, Mapping):
                    continue
                key = str(item.get("label") or item.get("key") or "").strip()
                item_value = str(item.get("value") or "").strip()
                if key and item_value:
                    key_values.append((key, item_value))

        return cls(
            source_page=int(value.get("source_page") or 0),
            summary=str(value.get("summary") or "").strip(),
            entities=cls._text_tuple(value.get("entities")),
            relationships=tuple(relationships),
            key_values=tuple(key_values),
            units=cls._text_tuple(value.get("units")),
        )

    @staticmethod
    def _text_tuple(value: object) -> tuple[str, ...]:
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
            return ()
        return tuple(
            text
            for item in value
            if (text := str(item or "").strip())
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "source_page": self.source_page,
            "summary": self.summary,
            "entities": list(self.entities),
            "relationships": [item.to_dict() for item in self.relationships],
            "key_values": [
                {"label": label, "value": value} for label, value in self.key_values
            ],
            "units": list(self.units),
        }

    def to_retrieval_text(self) -> str:
        lines = [self.summary]
        if self.entities:
            lines.append("图中对象：" + "、".join(self.entities))
        if self.relationships:
            lines.append(
                "关系："
                + "；".join(
                    f"{item.source}{item.relation}{item.target}" for item in self.relationships
                )
            )
        if self.key_values:
            lines.append(
                "关键数值："
                + "；".join(f"{label}={value}" for label, value in self.key_values)
            )
        if self.units:
            lines.append("单位：" + "、".join(self.units))
        return "\n".join(line for line in lines if line.strip())


@dataclass(frozen=True, slots=True)
class ImageAssetCandidate:
    asset_key: str
    source_ref: str
    page_number: int | None
    page_origin: str | None
    line_number: int
    occurrence_index: int
    alt_text: str = ""
    caption: str = ""
    visual_description: StructuredVisualDescription | None = None
    mapping_warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class VisualDescriptionTask:
    asset_key: str
    source_ref: str
    page_number: int
    line_number: int
    occurrence_index: int
    category: ImageAssetCategory
    prompt: str
    response_schema: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return {
            "asset_key": self.asset_key,
            "source_ref": self.source_ref,
            "page_number": self.page_number,
            "line_number": self.line_number,
            "occurrence_index": self.occurrence_index,
            "category": self.category.value,
            "prompt": self.prompt,
            "response_schema": self.response_schema,
        }


@dataclass(frozen=True, slots=True)
class ImageAssetDecision:
    asset_key: str
    source_ref: str
    page_number: int | None
    page_origin: str | None
    line_number: int
    occurrence_index: int
    category: ImageAssetCategory
    usage: ImageAssetUsage
    preview_asset: bool
    retrieval_asset: bool
    visual_description_status: VisualDescriptionStatus
    retrieval_text: str | None
    visual_description: StructuredVisualDescription | None
    warnings: tuple[str, ...]
    visual_task: VisualDescriptionTask | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "asset_key": self.asset_key,
            "source_ref": self.source_ref,
            "page_number": self.page_number,
            "page_origin": self.page_origin,
            "line_number": self.line_number,
            "occurrence_index": self.occurrence_index,
            "category": self.category.value,
            "usage": self.usage.value,
            "preview_asset": self.preview_asset,
            "retrieval_asset": self.retrieval_asset,
            "visual_description_status": self.visual_description_status.value,
            "retrieval_text": self.retrieval_text,
            "visual_description": (
                self.visual_description.to_dict() if self.visual_description else None
            ),
            "warnings": list(self.warnings),
            "visual_task": self.visual_task.to_dict() if self.visual_task else None,
        }

    def to_chunk_structure_metadata(self) -> dict[str, object]:
        """Payload intended for ``document_chunk.structure_metadata``."""

        return {
            "chunk_role": "image",
            "retrieval_eligible": self.retrieval_asset,
            "image_asset": self.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class ImageAssetPolicyReport:
    assets: tuple[ImageAssetDecision, ...]
    warnings: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        retrieval_count = sum(asset.retrieval_asset for asset in self.assets)
        return {
            "schema_version": 1,
            "asset_count": len(self.assets),
            "preview_asset_count": len(self.assets),
            "retrieval_asset_count": retrieval_count,
            "preview_only_count": len(self.assets) - retrieval_count,
            "visual_task_count": sum(asset.visual_task is not None for asset in self.assets),
            "warnings": list(self.warnings),
            "assets": [asset.to_dict() for asset in self.assets],
        }


class PdfImageAssetPolicy:
    """Classify assets and block noisy/undescribed image content from retrieval."""

    _PAGE_MARKER_RE = re.compile(r"<!--\s*ODL_PAGE:(\d+)\s*-->")
    _MARKDOWN_IMAGE_RE = re.compile(
        r"!\[(?P<alt>[^\]]*)\]\(\s*(?:<(?P<angle>[^>]+)>|(?P<plain>[^\s)]+))"
    )
    _HTML_IMAGE_RE = re.compile(
        r"<img\b(?P<attrs>[^>]*)>",
        re.IGNORECASE | re.DOTALL,
    )
    _HTML_SRC_RE = re.compile(
        r"\bsrc\s*=\s*(?:\"(?P<double>[^\"]+)\"|'(?P<single>[^']+)'|(?P<bare>[^\s>]+))",
        re.IGNORECASE,
    )
    _HTML_ALT_RE = re.compile(
        r"\balt\s*=\s*(?:\"(?P<double>[^\"]*)\"|'(?P<single>[^']*)'|(?P<bare>[^\s>]+))",
        re.IGNORECASE,
    )
    _PLACEHOLDER_RE = re.compile(
        r"(?:未提供|暂无|无|未生成|未识别|缺少).{0,6}(?:图片|图像|视觉).{0,6}(?:说明|描述|内容)?",
        re.IGNORECASE,
    )
    _NUMERIC_VALUE_RE = re.compile(
        r"(?<![A-Za-z])[-+]?(?:\d{1,3}(?:[,，]\d{3})+|\d+)(?:\.\d+)?(?:%|\b)"
    )
    _ASSET_KEY_PREFIX = "asset-occurrence:"
    _CATEGORY_KEYWORDS: tuple[tuple[ImageAssetCategory, tuple[str, ...]], ...] = (
        (ImageAssetCategory.SIGNATURE, ("签名", "签字", "签章", "signature")),
        (ImageAssetCategory.SEAL, ("印章", "公章", "盖章", "stamp", "seal")),
        (
            ImageAssetCategory.DECORATIVE,
            ("装饰", "页眉", "页脚", "水印", "logo", "图标", "icon", "background"),
        ),
        (
            ImageAssetCategory.SYSTEM_BOUNDARY,
            ("系统边界", "组织边界", "核算边界", "boundary diagram"),
        ),
        (
            ImageAssetCategory.FLOWCHART,
            ("流程图", "工艺流程", "workflow", "flowchart", "process flow"),
        ),
        (
            ImageAssetCategory.CHART,
            ("图表", "柱状图", "折线图", "饼图", "趋势图", "chart", "graph"),
        ),
        (ImageAssetCategory.PHOTO, ("照片", "现场图", "photo", "photograph")),
    )
    _VISUAL_CATEGORIES = {
        ImageAssetCategory.CHART,
        ImageAssetCategory.FLOWCHART,
        ImageAssetCategory.SYSTEM_BOUNDARY,
    }
    _PREVIEW_ONLY_CATEGORIES = {
        ImageAssetCategory.SIGNATURE,
        ImageAssetCategory.SEAL,
        ImageAssetCategory.DECORATIVE,
    }

    def extract_and_classify(
        self,
        markdown: str,
        *,
        page_metadata: Mapping[str, int] | None = None,
        visual_descriptions: Mapping[
            object, StructuredVisualDescription | Mapping[str, object]
        ]
        | None = None,
    ) -> ImageAssetPolicyReport:
        page_metadata = {
            self.normalize_source_ref(key): int(value)
            for key, value in (page_metadata or {}).items()
            if int(value) > 0
        }
        text = markdown or ""
        raw_candidates: list[tuple[int, str, str]] = []
        for match in self._MARKDOWN_IMAGE_RE.finditer(text):
            raw_candidates.append(
                (
                    match.start(),
                    match.group("angle") or match.group("plain") or "",
                    match.group("alt") or "",
                )
            )
        for match in self._HTML_IMAGE_RE.finditer(text):
            attrs = match.group("attrs") or ""
            source_match = self._HTML_SRC_RE.search(attrs)
            if source_match is None:
                continue
            alt_match = self._HTML_ALT_RE.search(attrs)
            source_ref = (
                source_match.group("double")
                or source_match.group("single")
                or source_match.group("bare")
                or ""
            )
            alt_text = ""
            if alt_match is not None:
                alt_text = (
                    alt_match.group("double")
                    or alt_match.group("single")
                    or alt_match.group("bare")
                    or ""
                )
            raw_candidates.append((match.start(), source_ref, alt_text))

        page_markers = [
            (match.start(), int(match.group(1)))
            for match in self._PAGE_MARKER_RE.finditer(text)
        ]
        marker_index = 0
        current_page: int | None = None
        candidates: list[ImageAssetCandidate] = []
        for occurrence_index, (offset, source_ref, alt_text) in enumerate(
            sorted(raw_candidates, key=lambda item: item[0]),
            start=1,
        ):
            while (
                marker_index < len(page_markers)
                and page_markers[marker_index][0] <= offset
            ):
                current_page = page_markers[marker_index][1]
                marker_index += 1
            candidates.append(
                self._candidate(
                    source_ref,
                    alt_text,
                    current_page,
                    page_metadata,
                    line_number=text.count("\n", 0, offset),
                    occurrence_index=occurrence_index,
                )
            )

        candidates = self._bind_visual_descriptions(
            candidates,
            visual_descriptions or {},
        )

        decisions = tuple(self.classify(candidate) for candidate in candidates)
        warnings = tuple(
            dict.fromkeys(warning for decision in decisions for warning in decision.warnings)
        )
        return ImageAssetPolicyReport(assets=decisions, warnings=warnings)

    def classify(self, candidate: ImageAssetCandidate) -> ImageAssetDecision:
        category = self._category(candidate)
        warnings: list[str] = list(candidate.mapping_warnings)
        if candidate.page_number is None or not candidate.page_origin:
            warnings.append("MISSING_IMAGE_PAGE_PROVENANCE")

        description = candidate.visual_description
        status = VisualDescriptionStatus.NOT_REQUIRED
        retrieval_text: str | None = None
        visual_task: VisualDescriptionTask | None = None

        if category in self._PREVIEW_ONLY_CATEGORIES:
            warnings.append(f"PREVIEW_ONLY_CATEGORY:{category.value}")
        else:
            # UNKNOWN is deliberately not interpreted as decorative.  Without an
            # explicit description it receives a visual judgment task and remains
            # preview-only; this prevents unclassified substantive figures from
            # silently becoming NOT_REQUIRED.
            requires_description = True
            if description is None and requires_description:
                status = VisualDescriptionStatus.REQUIRED
                warnings.append("VISUAL_DESCRIPTION_REQUIRED")
                if candidate.page_number is not None:
                    visual_task = self.build_visual_task(candidate, category)
            elif description is not None:
                description_warnings = self._validate_description(
                    description,
                    category,
                    candidate.page_number,
                )
                warnings.extend(description_warnings)
                if description_warnings:
                    status = VisualDescriptionStatus.INVALID
                    if candidate.page_number is not None:
                        visual_task = self.build_visual_task(candidate, category)
                else:
                    status = VisualDescriptionStatus.COMPLETE
                    retrieval_text = description.to_retrieval_text()

        retrieval_asset = (
            candidate.page_number is not None
            and category not in self._PREVIEW_ONLY_CATEGORIES
            and status is VisualDescriptionStatus.COMPLETE
            and self.is_retrieval_text_eligible(retrieval_text or "")
        )
        usage = (
            ImageAssetUsage.PREVIEW_AND_RETRIEVAL
            if retrieval_asset
            else ImageAssetUsage.PREVIEW_ONLY
        )
        return ImageAssetDecision(
            asset_key=candidate.asset_key,
            source_ref=candidate.source_ref,
            page_number=candidate.page_number,
            page_origin=candidate.page_origin,
            line_number=candidate.line_number,
            occurrence_index=candidate.occurrence_index,
            category=category,
            usage=usage,
            preview_asset=True,
            retrieval_asset=retrieval_asset,
            visual_description_status=status,
            retrieval_text=retrieval_text if retrieval_asset else None,
            visual_description=description,
            warnings=tuple(dict.fromkeys(warnings)),
            visual_task=visual_task,
        )

    def _candidate(
        self,
        source_ref: str,
        alt_text: str,
        marker_page: int | None,
        page_metadata: Mapping[str, int],
        *,
        line_number: int,
        occurrence_index: int,
    ) -> ImageAssetCandidate:
        normalized = self.normalize_source_ref(source_ref)
        if marker_page is not None:
            page_number = marker_page
            origin = "ODL_PAGE_MARKER"
        elif normalized in page_metadata:
            page_number = page_metadata[normalized]
            origin = "PAGE_METADATA"
        else:
            page_number = None
            origin = None
        return ImageAssetCandidate(
            asset_key=self.occurrence_key(
                source_ref,
                page_number=page_number,
                line_number=line_number,
                occurrence_index=occurrence_index,
            ),
            source_ref=source_ref,
            page_number=page_number,
            page_origin=origin,
            line_number=line_number,
            occurrence_index=occurrence_index,
            alt_text=html.unescape(alt_text or "").strip(),
        )

    def _bind_visual_descriptions(
        self,
        candidates: Sequence[ImageAssetCandidate],
        raw_descriptions: Mapping[
            object,
            StructuredVisualDescription | Mapping[str, object],
        ],
    ) -> list[ImageAssetCandidate]:
        """Bind descriptions by an exact occurrence key, never by an ambiguous URL.

        URL-only mappings remain supported for a document that references the URL
        exactly once.  Repeated references require the ``asset_key`` exposed by the
        preliminary policy report, so a later page cannot overwrite an earlier one.
        """

        exact: dict[str, StructuredVisualDescription] = {}
        legacy: dict[str, StructuredVisualDescription] = {}
        for raw_key, raw_description in raw_descriptions.items():
            try:
                description = (
                    raw_description
                    if isinstance(raw_description, StructuredVisualDescription)
                    else StructuredVisualDescription.from_mapping(raw_description)
                )
            except (TypeError, ValueError):
                continue
            key = str(raw_key)
            if key.startswith(self._ASSET_KEY_PREFIX):
                exact[key] = description
            else:
                legacy[self.normalize_source_ref(key)] = description

        ref_counts = Counter(
            self.normalize_source_ref(candidate.source_ref) for candidate in candidates
        )
        bound: list[ImageAssetCandidate] = []
        for candidate in candidates:
            normalized_ref = self.normalize_source_ref(candidate.source_ref)
            description = exact.get(candidate.asset_key)
            warnings = list(candidate.mapping_warnings)
            if description is None and normalized_ref in legacy:
                if ref_counts[normalized_ref] == 1:
                    description = legacy[normalized_ref]
                else:
                    warnings.append("AMBIGUOUS_VISUAL_DESCRIPTION_MAPPING")
            bound.append(
                replace(
                    candidate,
                    visual_description=description,
                    mapping_warnings=tuple(dict.fromkeys(warnings)),
                )
            )
        return bound

    def _category(self, candidate: ImageAssetCandidate) -> ImageAssetCategory:
        # The filename may help classify an asset, but never supplies its page number.
        semantic_evidence = " ".join(
            (
                candidate.alt_text,
                candidate.caption,
                candidate.visual_description.summary
                if candidate.visual_description is not None
                else "",
            )
        ).lower()
        for category, keywords in self._CATEGORY_KEYWORDS:
            if any(keyword.lower() in semantic_evidence for keyword in keywords):
                return category
        filename_evidence = candidate.source_ref.lower()
        for category, keywords in self._CATEGORY_KEYWORDS:
            if any(keyword.lower() in filename_evidence for keyword in keywords):
                return category
        return ImageAssetCategory.UNKNOWN

    def _validate_description(
        self,
        description: StructuredVisualDescription,
        category: ImageAssetCategory,
        page_number: int | None,
    ) -> list[str]:
        warnings: list[str] = []
        if page_number is None or description.source_page != page_number:
            warnings.append("VISUAL_DESCRIPTION_PAGE_MISMATCH")
        if len(description.summary.strip()) < 12:
            warnings.append("VISUAL_DESCRIPTION_TOO_SHORT")
        if not self.is_retrieval_text_eligible(description.summary):
            warnings.append("VISUAL_DESCRIPTION_PLACEHOLDER")
        if category is ImageAssetCategory.CHART and not description.key_values:
            warnings.append("CHART_KEY_VALUES_REQUIRED")
        if category is ImageAssetCategory.CHART and description.key_values and not any(
            self._NUMERIC_VALUE_RE.search(value)
            for _, value in description.key_values
        ):
            warnings.append("CHART_NUMERIC_VALUES_REQUIRED")
        if category in {ImageAssetCategory.FLOWCHART, ImageAssetCategory.SYSTEM_BOUNDARY}:
            normalized_entities = {
                re.sub(r"\s+", "", entity).casefold()
                for entity in description.entities
                if entity.strip()
            }
            if len(normalized_entities) < 2:
                warnings.append("VISUAL_ENTITIES_REQUIRED")
            if not description.relationships:
                warnings.append("VISUAL_RELATIONSHIPS_REQUIRED")
        if category is ImageAssetCategory.UNKNOWN and not (
            description.key_values
            or description.relationships
            or len({item.strip().casefold() for item in description.entities if item.strip()})
            >= 2
        ):
            warnings.append("UNKNOWN_VISUAL_STRUCTURE_REQUIRED")
        return warnings

    def validate_page_visual_description(
        self,
        description: StructuredVisualDescription,
        *,
        page_number: int,
        categories: Sequence[ImageAssetCategory],
    ) -> tuple[str, ...]:
        """Validate one full-page result against every substantive category on it."""

        relevant = tuple(
            dict.fromkeys(
                category
                for category in categories
                if category not in self._PREVIEW_ONLY_CATEGORIES
            )
        )
        if not relevant:
            relevant = (ImageAssetCategory.UNKNOWN,)
        warnings: list[str] = []
        for category in relevant:
            warnings.extend(
                self._validate_description(description, category, page_number)
            )
        return tuple(dict.fromkeys(warnings))

    def build_visual_task(
        self,
        candidate: ImageAssetCandidate,
        category: ImageAssetCategory,
    ) -> VisualDescriptionTask:
        if candidate.page_number is None:
            raise ValueError("visual task requires explicit page provenance")
        emphasis = {
            ImageAssetCategory.CHART: "提取图例、坐标轴、单位、系列和所有关键数值",
            ImageAssetCategory.FLOWCHART: "提取每个节点及箭头方向、分支条件",
            ImageAssetCategory.SYSTEM_BOUNDARY: "提取边界内外对象和跨边界关系",
        }.get(category, "提取可验证的对象、关系、数值和单位")
        prompt = (
            f"这是 PDF 第 {candidate.page_number} 页的{category.value}资产。{emphasis}。"
            "不得臆测模糊数值；无法确认时留空。只返回结构化 JSON。"
        )
        return VisualDescriptionTask(
            asset_key=candidate.asset_key,
            source_ref=candidate.source_ref,
            page_number=candidate.page_number,
            line_number=candidate.line_number,
            occurrence_index=candidate.occurrence_index,
            category=category,
            prompt=prompt,
            response_schema={
                "type": "object",
                "required": [
                    "source_page",
                    "summary",
                    "entities",
                    "relationships",
                    "key_values",
                    "units",
                ],
                "properties": {
                    "source_page": {"type": "integer", "const": candidate.page_number},
                    "summary": {"type": "string"},
                    "entities": {"type": "array", "items": {"type": "string"}},
                    "relationships": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["source", "relation", "target"],
                        },
                    },
                    "key_values": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["label", "value"],
                        },
                    },
                    "units": {"type": "array", "items": {"type": "string"}},
                },
            },
        )

    @classmethod
    def is_retrieval_text_eligible(cls, text: str) -> bool:
        normalized = re.sub(r"\s+", "", text or "")
        return len(normalized) >= 12 and cls._PLACEHOLDER_RE.search(normalized) is None

    @staticmethod
    def normalize_source_ref(raw_reference: str) -> str:
        value = html.unescape(raw_reference or "").strip().strip("<>")
        if not value:
            return ""
        parsed = urlsplit(value)
        path = unquote(parsed.path or value).replace("\\", "/")
        while path.startswith("./"):
            path = path[2:]
        return path.lstrip("/")

    @classmethod
    def occurrence_key(
        cls,
        source_ref: str,
        *,
        page_number: int | None,
        line_number: int,
        occurrence_index: int,
    ) -> str:
        """Return the stable selector used for one concrete source occurrence."""

        normalized_ref = cls.normalize_source_ref(source_ref)
        return (
            f"{cls._ASSET_KEY_PREFIX}{occurrence_index}:"
            f"page:{page_number or 0}:line:{line_number}:{normalized_ref}"
        )


__all__ = [
    "ImageAssetCandidate",
    "ImageAssetCategory",
    "ImageAssetDecision",
    "ImageAssetPolicyReport",
    "ImageAssetUsage",
    "PdfImageAssetPolicy",
    "StructuredVisualDescription",
    "VisualDescriptionStatus",
    "VisualDescriptionTask",
    "VisualRelationship",
]
