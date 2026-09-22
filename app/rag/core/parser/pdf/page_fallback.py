from __future__ import annotations

import asyncio
import base64
import json
import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

try:  # PyMuPDF 1.24+ canonical import name.
    import pymupdf
except ImportError:  # pragma: no cover - compatibility with the declared 1.23 floor
    import fitz as pymupdf

from app.rag.core.llm.exceptions import (
    AuthenticationError,
    ConfigurationException,
    RateLimitError,
)
from app.rag.core.parser.pdf.image_asset_policy import PdfImageAssetPolicy
from app.rag.core.parser.pdf.quality import (
    PdfOcrPageResult,
    PdfPageQuality,
    PdfQualityReport,
)

DEFAULT_OCR_PROMPT = (
    "请提取这一整页文档图像中的所有文字，按阅读顺序和原始层级输出 Markdown；"
    "保留标题、段落、列表、表格、数值、单位、公式上下标和运算符，不要总结或编造内容。"
    "若页面含图表、流程图或系统边界图，还要提取图中实体、关键数值和箭头关系；"
    "必须返回 has_visual_content 布尔值，并用 visual_content_type 标记 "
    "none/chart/flowchart/system_boundary/other。若不含这些需要检索的图形，"
    "has_visual_content 为 false、visual_content_type 为 none，结构字段返回空值。"
    "仅返回 JSON 对象，字段为 markdown、confidence、has_visual_content、"
    "visual_content_type、summary、entities、relationships、key_values、units、"
    "visual_assets；relationships 的元素包含"
    "source/relation/target，key_values 的元素包含 label/value。visual_assets 的每项"
    "包含资产清单中的 asset_key 和 description，description 使用同样的结构字段；"
    "无法精确归属到单个资产时不得猜测 asset_key。confidence 为 0 到 1 的"
    "整页可辨识置信度，任何正文、数值、公式或图形关系无法辨认时必须降低置信度。"
)
DEFAULT_VISION_PROMPT = (
    "请分析这一整页文档中的图表、流程图或架构图；保留关键实体、指标、单位、趋势、"
    "节点和箭头关系，不要编造看不清的内容。必须返回 has_visual_content 布尔值，"
    "并用 visual_content_type 标记 none/chart/flowchart/system_boundary/other。"
    "若只有 logo、签名、印章、页眉页脚或装饰图，has_visual_content 为 false。"
    "仅返回 JSON 对象，字段为 markdown、has_visual_content、visual_content_type、"
    "summary、entities、relationships、key_values、units、visual_assets；relationships 的元素包含"
    "source/relation/target，key_values 的元素包含 label/value。visual_assets 的每项"
    "包含资产清单中的 asset_key 和 description，无法精确归属时不得猜测。"
)


class PageFallbackMethod(StrEnum):
    OCR = "ocr"
    VISION = "vision"


@dataclass(frozen=True, slots=True)
class RenderedPdfPage:
    """One full original PDF page rendered for a provider call."""

    page_number: int
    image_bytes: bytes
    media_type: str
    dpi: int
    width: int
    height: int
    asset_manifest: tuple[dict[str, object], ...] = ()


@dataclass(frozen=True, slots=True)
class PageProviderOutput:
    """Normalized output returned by an injected OCR or vision provider."""

    text: str
    markdown: str | None = None
    confidence: float | None = None
    warnings: tuple[str, ...] = ()
    structured_data: dict[str, object] | None = None
    truncated: bool = False
    finish_reason: str | None = None
    completion_error_code: str | None = None

    def __post_init__(self) -> None:
        if self.confidence is not None and (
            not math.isfinite(self.confidence) or not 0 <= self.confidence <= 1
        ):
            raise ValueError("provider confidence must be between 0 and 1")


class AsyncOcrPageProvider(Protocol):
    async def recognize_page(
        self,
        *,
        page_number: int,
        image: RenderedPdfPage,
    ) -> PageProviderOutput: ...


class AsyncVisionPageProvider(Protocol):
    async def analyze_page(
        self,
        *,
        page_number: int,
        image: RenderedPdfPage,
        odl_markdown: str,
    ) -> PageProviderOutput: ...


class RapidOcrPageProviderAdapter:
    """Run PP-OCRv6 locally through RapidOCR's bundled ONNX models.

    Model loading is lazy so born-digital PDFs do not pay the OCR startup cost.
    One engine instance is serialized because ONNX inference and RapidOCR's mutable
    call parameters are not guaranteed to be safe under concurrent calls.
    """

    _DET_MODEL = "PP-OCRv6_det_small.onnx"
    _REC_MODEL = "PP-OCRv6_rec_small.onnx"
    _CLS_MODEL = "ch_ppocr_mobile_v2.0_cls_mobile.onnx"

    def __init__(
        self,
        *,
        engine_factory: Callable[[], Any] | None = None,
        intra_op_num_threads: int = 3,
        inter_op_num_threads: int = 1,
    ) -> None:
        if intra_op_num_threads < 1 or inter_op_num_threads < 1:
            raise ValueError("RapidOCR thread counts must be greater than zero")
        self._engine_factory = engine_factory
        self._intra_op_num_threads = intra_op_num_threads
        self._inter_op_num_threads = inter_op_num_threads
        self._engine: Any | None = None
        self._lock = asyncio.Lock()

    async def recognize_page(
        self,
        *,
        page_number: int,
        image: RenderedPdfPage,
    ) -> PageProviderOutput:
        del page_number
        async with self._lock:
            return await asyncio.to_thread(self._recognize_sync, image.image_bytes)

    def _recognize_sync(self, image_bytes: bytes) -> PageProviderOutput:
        result = self._get_engine()(image_bytes)
        if result is None:
            return PageProviderOutput(
                text="",
                markdown="",
                confidence=None,
                warnings=("OCR_SOURCE:RAPIDOCR_PP_OCRV6",),
            )

        texts = list(getattr(result, "txts", None) or ())
        scores = [float(score) for score in (getattr(result, "scores", None) or ())]
        if not texts and isinstance(result, tuple):
            raw_rows = result[0] or ()
            texts = [str(row[1]) for row in raw_rows]
            scores = [float(row[2]) for row in raw_rows]

        text = "\n".join(str(item).strip() for item in texts if str(item).strip())
        to_markdown = getattr(result, "to_markdown", None)
        markdown = str(to_markdown() if callable(to_markdown) else text).strip()
        confidence = sum(scores) / len(scores) if scores else None
        return PageProviderOutput(
            text=text,
            markdown=markdown or text,
            confidence=confidence,
            warnings=("OCR_SOURCE:RAPIDOCR_PP_OCRV6",),
        )

    def _get_engine(self) -> Any:
        if self._engine is not None:
            return self._engine
        if self._engine_factory is not None:
            self._engine = self._engine_factory()
            return self._engine

        import rapidocr
        from rapidocr import RapidOCR

        model_dir = Path(rapidocr.__file__).resolve().parent / "models"
        model_paths = {
            "Det.model_path": model_dir / self._DET_MODEL,
            "Rec.model_path": model_dir / self._REC_MODEL,
            "Cls.model_path": model_dir / self._CLS_MODEL,
        }
        missing = [path.name for path in model_paths.values() if not path.is_file()]
        if missing:
            raise RuntimeError(f"RapidOCR bundled models missing: {','.join(missing)}")
        params = {
            key: str(path)
            for key, path in model_paths.items()
        }
        params.update(
            {
                "EngineConfig.onnxruntime.intra_op_num_threads": (
                    self._intra_op_num_threads
                ),
                "EngineConfig.onnxruntime.inter_op_num_threads": (
                    self._inter_op_num_threads
                ),
            }
        )
        self._engine = RapidOCR(params=params)
        return self._engine


class QualityGatedOcrPageProvider:
    """Prefer local OCR and call the vision model only for rejected OCR output."""

    def __init__(
        self,
        primary: AsyncOcrPageProvider,
        fallback: AsyncOcrPageProvider | None,
        *,
        min_effective_text_chars: int,
        min_confidence: float,
    ) -> None:
        if min_effective_text_chars < 1:
            raise ValueError("min_effective_text_chars must be greater than zero")
        if not 0 <= min_confidence <= 1:
            raise ValueError("min_confidence must be between zero and one")
        self._primary = primary
        self._fallback = fallback
        self._min_effective_text_chars = min_effective_text_chars
        self._min_confidence = min_confidence

    async def recognize_page(
        self,
        *,
        page_number: int,
        image: RenderedPdfPage,
    ) -> PageProviderOutput:
        rejection: str | None = None
        try:
            primary_output = await self._primary.recognize_page(
                page_number=page_number,
                image=image,
            )
            rejection = self._rejection_reason(primary_output)
            if rejection is None:
                return primary_output
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            primary_output = None
            rejection = f"ERROR_{type(exc).__name__}"

        if self._fallback is None:
            if primary_output is not None:
                return replace(
                    primary_output,
                    warnings=tuple(
                        dict.fromkeys(
                            [*primary_output.warnings, f"LOCAL_OCR_REJECTED:{rejection}"]
                        )
                    ),
                )
            raise RuntimeError(f"local OCR failed and no fallback is configured: {rejection}")

        fallback_output = await self._fallback.recognize_page(
            page_number=page_number,
            image=image,
        )
        return replace(
            fallback_output,
            warnings=tuple(
                dict.fromkeys(
                    [
                        *(primary_output.warnings if primary_output is not None else ()),
                        f"LOCAL_OCR_REJECTED:{rejection}",
                        *fallback_output.warnings,
                        "OCR_SOURCE:VISION_FALLBACK",
                    ]
                )
            ),
        )

    def _rejection_reason(self, output: PageProviderOutput) -> str | None:
        effective_chars = len(re.sub(r"\s+", "", output.text or output.markdown or ""))
        if effective_chars < self._min_effective_text_chars:
            return (
                "TEXT_TOO_SHORT:"
                f"actual={effective_chars},limit={self._min_effective_text_chars}"
            )
        if output.confidence is None:
            return "CONFIDENCE_MISSING"
        if output.confidence < self._min_confidence:
            return (
                "CONFIDENCE_LOW:"
                f"actual={output.confidence:.4f},limit={self._min_confidence:.4f}"
            )
        return None


class AnalyzeImagePageProviderAdapter:
    """Reuse the project's SDK-free ``analyze_image`` provider contract.

    ``OpenAICompatibleProvider``, Anthropic and Google providers all expose this
    method and perform their official HTTP calls internally. Keeping the concrete
    provider injected here avoids configuration/database dependencies in this module.
    """

    _SUCCESS_FINISH_REASONS = frozenset({"stop", "end_turn", "stop_sequence"})
    _TRUNCATED_FINISH_REASONS = frozenset(
        {"length", "max_tokens", "max_output_tokens"}
    )
    _FILTERED_FINISH_MARKERS = (
        "content_filter",
        "safety",
        "blocked",
        "blocklist",
        "prohibited_content",
        "recitation",
        "spii",
        "policy",
        "moderation",
    )
    _STRUCTURED_DATA_KEYS = frozenset(
        {
            "has_visual_content",
            "visual_content_type",
            "summary",
            "entities",
            "relationships",
            "key_values",
            "units",
            "visual_assets",
        }
    )
    _MAX_STRUCTURED_DATA_BYTES = 64 * 1024

    def __init__(
        self,
        provider: object,
        *,
        model_name: str | None = None,
        ocr_prompt: str = DEFAULT_OCR_PROMPT,
        vision_prompt: str = DEFAULT_VISION_PROMPT,
    ) -> None:
        analyze_image = getattr(provider, "analyze_image", None)
        if not callable(analyze_image):
            raise TypeError("provider must expose an async analyze_image method")
        self._provider = provider
        self._model_name = model_name
        self._ocr_prompt = ocr_prompt
        self._vision_prompt = vision_prompt

    async def recognize_page(
        self,
        *,
        page_number: int,
        image: RenderedPdfPage,
    ) -> PageProviderOutput:
        return await self._analyze(
            page_number=page_number,
            image=image,
            prompt=self._with_asset_manifest(self._ocr_prompt, image.asset_manifest),
            require_confidence=True,
        )

    async def analyze_page(
        self,
        *,
        page_number: int,
        image: RenderedPdfPage,
        odl_markdown: str,
    ) -> PageProviderOutput:
        context = odl_markdown.strip()
        prompt = self._with_asset_manifest(self._vision_prompt, image.asset_manifest)
        if context:
            prompt = f"{prompt}\n\n当前 OpenDataLoader 页面内容：\n{context[:4000]}"
        return await self._analyze(
            page_number=page_number,
            image=image,
            prompt=prompt,
            require_confidence=False,
        )

    async def _analyze(
        self,
        *,
        page_number: int,
        image: RenderedPdfPage,
        prompt: str,
        require_confidence: bool,
    ) -> PageProviderOutput:
        kwargs: dict[str, object] = {
            "image_base64": base64.b64encode(image.image_bytes).decode("ascii"),
            "prompt": prompt,
            "media_type": image.media_type,
            "temperature": 0,
            "max_tokens": 8192,
        }
        if self._model_name:
            kwargs["model"] = self._model_name
            normalized_model = self._model_name.strip().casefold()
            if normalized_model.startswith("qwen"):
                # DashScope's raw OpenAI-compatible endpoint expects these as
                # top-level request fields (the official SDK's ``extra_body`` helper
                # merely flattens them).  Disabling thinking keeps the response a
                # single JSON object that can be validated deterministically.
                kwargs["enable_thinking"] = False
                kwargs["response_format"] = {"type": "json_object"}
            elif normalized_model.startswith("kimi-"):
                # Kimi Open Platform accepts 1.0, while subscription/Plan keys use
                # the Kimi Code compatibility endpoint whose K2.6 route only accepts
                # 0.6. OCR fallback still needs non-thinking JSON output on both.
                api_base_url = str(
                    getattr(self._provider, "api_base_url", "") or ""
                ).casefold()
                kwargs["temperature"] = (
                    0.6 if "api.kimi.com/coding/" in api_base_url else 1.0
                )
                kwargs["thinking"] = {"type": "disabled"}
                kwargs["response_format"] = {"type": "json_object"}
        response = await self._provider.analyze_image(**kwargs)
        if isinstance(response, Mapping):
            text = str(
                response.get("content")
                or response.get("markdown")
                or response.get("text")
                or ""
            )
            markdown = str(response.get("markdown") or "") or None
            confidence_value = response.get("confidence")
            finish_reason = response.get("finish_reason") or response.get("stop_reason")
        else:
            text = str(getattr(response, "content", "") or "")
            markdown = None
            confidence_value = getattr(response, "confidence", None)
            finish_reason = getattr(response, "finish_reason", None)
        parsed = self._parse_structured_output(text)
        structured_data = None
        if parsed is not None:
            parsed_markdown, parsed_confidence, structured_data = parsed
            text = parsed_markdown
            markdown = parsed_markdown
            if confidence_value is None:
                confidence_value = parsed_confidence
            # 页码只能来自当前渲染目标，不能信任模型生成的来源页。
            structured_data["source_page"] = page_number
        confidence = float(confidence_value) if confidence_value is not None else None
        normalized_finish_reason = self._normalize_finish_reason(finish_reason)
        completion_error_code = self._completion_error_code(normalized_finish_reason)
        truncated = completion_error_code == "PROVIDER_OUTPUT_TRUNCATED"
        warnings: list[str] = []
        if completion_error_code is not None:
            warnings.append(completion_error_code)
            if require_confidence:
                confidence = 0.0
            # Never expose partial/refusal/filter text to the merge path. The raw
            # provider response is intentionally discarded at this trust boundary.
            text = ""
            markdown = ""
            structured_data = None
        if require_confidence and confidence is None:
            warnings.append("CONFIDENCE_UNAVAILABLE")
        return PageProviderOutput(
            text=text.strip(),
            markdown=(markdown or text).strip(),
            confidence=confidence,
            warnings=tuple(warnings),
            structured_data=structured_data,
            truncated=truncated,
            finish_reason=normalized_finish_reason or None,
            completion_error_code=completion_error_code,
        )

    @staticmethod
    def _with_asset_manifest(
        prompt: str,
        manifest: tuple[dict[str, object], ...],
    ) -> str:
        if not manifest:
            return prompt
        payload = json.dumps(manifest, ensure_ascii=False, separators=(",", ":"))
        return (
            f"{prompt}\n\n本页资产清单如下。若返回 visual_assets，每项必须原样回填 "
            "asset_key，不得根据文件名猜页码：\n"
            f"{payload}"
        )

    @staticmethod
    def _normalize_finish_reason(value: object) -> str:
        return re.sub(r"[\s-]+", "_", str(value or "").strip().casefold())

    @classmethod
    def _completion_error_code(cls, normalized_finish_reason: str) -> str | None:
        if normalized_finish_reason in cls._SUCCESS_FINISH_REASONS:
            return None
        if not normalized_finish_reason:
            return "PROVIDER_FINISH_REASON_MISSING"
        if (
            normalized_finish_reason in cls._TRUNCATED_FINISH_REASONS
            or "max_token" in normalized_finish_reason
        ):
            return "PROVIDER_OUTPUT_TRUNCATED"
        if any(
            marker in normalized_finish_reason
            for marker in cls._FILTERED_FINISH_MARKERS
        ):
            return "PROVIDER_OUTPUT_FILTERED"
        return "PROVIDER_ABNORMAL_FINISH"

    @staticmethod
    def _parse_structured_output(
        value: str,
    ) -> tuple[str, float | None, dict[str, object]] | None:
        candidate = (value or "").strip()
        if candidate.startswith("```"):
            lines = candidate.splitlines()
            if len(lines) >= 3 and lines[-1].strip() == "```":
                candidate = "\n".join(lines[1:-1]).strip()
        try:
            payload = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(payload, Mapping):
            return None
        markdown = str(payload.get("markdown") or payload.get("text") or "").strip()
        confidence_value = payload.get("confidence")
        try:
            confidence = float(confidence_value) if confidence_value is not None else None
        except (TypeError, ValueError):
            confidence = None
        if confidence is not None and not 0 <= confidence <= 1:
            confidence = None
        structured_data = {
            str(key): item
            for key, item in payload.items()
            if str(key) in AnalyzeImagePageProviderAdapter._STRUCTURED_DATA_KEYS
        }
        encoded = json.dumps(
            structured_data,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > AnalyzeImagePageProviderAdapter._MAX_STRUCTURED_DATA_BYTES:
            return None
        return markdown, confidence, structured_data


@dataclass(frozen=True, slots=True)
class PdfPageFallbackResult:
    page_number: int
    text: str
    markdown: str
    confidence: float | None
    method: PageFallbackMethod
    warnings: tuple[str, ...]
    structured_data: dict[str, object] | None = None
    error_code: str | None = None
    retryable: bool = False
    truncated: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "page_number": self.page_number,
            # 正文已合并进最终 Markdown，不在 document.parse_quality 中重复保存。
            "text_char_count": len(self.text),
            "markdown_char_count": len(self.markdown),
            "confidence": self.confidence,
            "method": self.method.value,
            "warnings": list(self.warnings),
            "structured_data": self.structured_data,
            "error_code": self.error_code,
            "retryable": self.retryable,
            "truncated": self.truncated,
        }


@dataclass(frozen=True, slots=True)
class PdfPageFallbackReport:
    dpi: int
    pdf_page_count: int
    quality_page_count: int
    results: tuple[PdfPageFallbackResult, ...]
    warnings: tuple[str, ...]

    @property
    def ocr_results(self) -> dict[int, PdfOcrPageResult]:
        """Mapping accepted directly by ``PdfQualityAnalyzer.analyze``."""

        return {
            result.page_number: PdfOcrPageResult(
                page_number=result.page_number,
                text=result.text,
                confidence=result.confidence,
            )
            for result in self.results
            if (
                result.method is PageFallbackMethod.OCR
                and result.text.strip()
                and not result.truncated
            )
        }

    @property
    def has_retryable_failure(self) -> bool:
        return any(result.error_code and result.retryable for result in self.results)

    def to_dict(self) -> dict[str, object]:
        return {
            "dpi": self.dpi,
            "pdf_page_count": self.pdf_page_count,
            "quality_page_count": self.quality_page_count,
            "processed_page_count": len(self.results),
            "warnings": list(self.warnings),
            "results": [result.to_dict() for result in self.results],
            "ocr_results": {
                str(page_number): {
                    "page_number": ocr_result.page_number,
                    "text_char_count": len(ocr_result.text),
                    "confidence": ocr_result.confidence,
                }
                for page_number, ocr_result in self.ocr_results.items()
            },
        }


@dataclass(frozen=True, slots=True)
class _OdlPageSlice:
    full_markdown: str
    body: str


@dataclass(frozen=True, slots=True)
class _FallbackTarget:
    quality: PdfPageQuality
    method: PageFallbackMethod
    odl_page: _OdlPageSlice
    warnings: tuple[str, ...] = ()
    asset_manifest: tuple[dict[str, object], ...] = ()


class PdfPageFallbackProcessor:
    """Add provider output only to pages that need OCR or visual explanation.

    OpenDataLoader remains the primary representation: provider Markdown is appended
    to the original page slice behind an HTML provenance marker and never replaces it.
    """

    _PAGE_MARKER_RE = re.compile(
        r"^[\t ]*<!--[\t ]*ODL_PAGE:(\d+)[\t ]*-->[\t ]*\r?$",
        flags=re.MULTILINE,
    )
    _MARKDOWN_IMAGE_RE = re.compile(
        r"!\[[^\]\r\n]*\]\(\s*(?:<[^>\r\n]*>|[^)\r\n]*)\s*\)",
        flags=re.IGNORECASE,
    )
    _MARKDOWN_REFERENCE_IMAGE_RE = re.compile(
        r"!\[[^\]\r\n]*\]\s*\[[^\]\r\n]*\]",
        flags=re.IGNORECASE,
    )
    _HTML_IMAGE_RE = re.compile(r"<img\b[^>]*>", flags=re.IGNORECASE | re.DOTALL)
    _CHART_HINT_RE = re.compile(
        r"(?:图表|趋势图|柱状图|折线图|饼图|流程图|架构图|示意图|chart|graph|diagram|figure)",
        flags=re.IGNORECASE,
    )
    _PROVIDER_DATA_URI_PREFIX_BYTES = len("data:image/png;base64,")
    _MAX_PROVIDER_DATA_URI_BYTES = 20_000_000

    def __init__(
        self,
        *,
        ocr_provider: AsyncOcrPageProvider | None = None,
        vision_provider: AsyncVisionPageProvider | None = None,
        dpi: int = 280,
        min_chart_image_coverage_ratio: float = 0.08,
        min_substantive_vector_coverage_ratio: float = 0.01,
        max_concurrency: int = 4,
        max_rendered_page_pixels: int = 50_000_000,
        max_rendered_page_bytes: int = 14 * 1024 * 1024,
        max_structured_report_bytes: int = 4 * 1024 * 1024,
    ) -> None:
        if isinstance(dpi, bool) or not 250 <= dpi <= 300:
            raise ValueError("dpi must be between 250 and 300")
        if not 0 <= min_chart_image_coverage_ratio <= 1:
            raise ValueError("min_chart_image_coverage_ratio must be between 0 and 1")
        if not 0 <= min_substantive_vector_coverage_ratio <= 1:
            raise ValueError(
                "min_substantive_vector_coverage_ratio must be between 0 and 1"
            )
        if min_substantive_vector_coverage_ratio > min_chart_image_coverage_ratio:
            raise ValueError(
                "min_substantive_vector_coverage_ratio must not exceed "
                "min_chart_image_coverage_ratio"
            )
        if isinstance(max_concurrency, bool) or max_concurrency < 1:
            raise ValueError("max_concurrency must be greater than zero")
        if isinstance(max_rendered_page_pixels, bool) or max_rendered_page_pixels < 1:
            raise ValueError("max_rendered_page_pixels must be greater than zero")
        if isinstance(max_rendered_page_bytes, bool) or max_rendered_page_bytes < 1:
            raise ValueError("max_rendered_page_bytes must be greater than zero")
        if (
            isinstance(max_structured_report_bytes, bool)
            or max_structured_report_bytes < 1
        ):
            raise ValueError("max_structured_report_bytes must be greater than zero")
        self._ocr_provider = ocr_provider
        self._vision_provider = vision_provider
        self._dpi = dpi
        self._min_chart_image_coverage_ratio = min_chart_image_coverage_ratio
        self._min_substantive_vector_coverage_ratio = (
            min_substantive_vector_coverage_ratio
        )
        self._max_concurrency = max_concurrency
        self._max_rendered_page_pixels = max_rendered_page_pixels
        self._max_rendered_page_bytes = max_rendered_page_bytes
        self._max_structured_report_bytes = max_structured_report_bytes

    async def process(
        self,
        pdf_path: str | Path,
        odl_markdown: str,
        quality_report: PdfQualityReport,
    ) -> PdfPageFallbackReport:
        pages, split_warnings = self._split_odl_markdown(odl_markdown)
        asset_manifests = self._asset_manifests(odl_markdown)
        targets = self._select_targets(
            quality_report,
            pages,
            asset_manifests=asset_manifests,
        )

        if not targets:
            return PdfPageFallbackReport(
                dpi=self._dpi,
                pdf_page_count=quality_report.pdf_page_count,
                quality_page_count=len(quality_report.per_page),
                results=(),
                warnings=tuple(split_warnings),
            )

        pdf_path = Path(pdf_path)
        pdf_page_count = await asyncio.to_thread(self._pdf_page_count, pdf_path)
        warnings = list(split_warnings)
        if pdf_page_count != quality_report.pdf_page_count:
            warnings.append(
                "PDF_PAGE_COUNT_DIFFERS_FROM_QUALITY_REPORT:"
                f"pdf={pdf_page_count},quality={quality_report.pdf_page_count}"
            )

        semaphore = asyncio.Semaphore(self._max_concurrency)
        tasks = [
            self._render_and_process_target(pdf_path, target, semaphore)
            for target in targets
        ]
        results = await asyncio.gather(*tasks)
        results.sort(key=lambda result: result.page_number)
        structured_report_bytes = sum(
            len(
                json.dumps(
                    result.structured_data,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            for result in results
            if result.structured_data is not None
        )
        if structured_report_bytes > self._max_structured_report_bytes:
            warnings.append(
                "STRUCTURED_REPORT_BYTES_EXCEEDED:"
                f"actual={structured_report_bytes},limit={self._max_structured_report_bytes}"
            )
            results = [
                replace(
                    result,
                    structured_data=None,
                    error_code="STRUCTURED_REPORT_BYTES_EXCEEDED",
                    retryable=False,
                    warnings=tuple(
                        dict.fromkeys(
                            [*result.warnings, "STRUCTURED_REPORT_BYTES_EXCEEDED"]
                        )
                    ),
                )
                for result in results
            ]
        return PdfPageFallbackReport(
            dpi=self._dpi,
            pdf_page_count=pdf_page_count,
            quality_page_count=len(quality_report.per_page),
            results=tuple(results),
            warnings=tuple(dict.fromkeys(warnings)),
        )

    def _select_targets(
        self,
        quality_report: PdfQualityReport,
        pages: dict[int, _OdlPageSlice],
        *,
        asset_manifests: Mapping[int, tuple[dict[str, object], ...]] | None = None,
    ) -> list[_FallbackTarget]:
        targets: list[_FallbackTarget] = []
        for page_quality in quality_report.per_page:
            page_number = page_quality.page_number
            page_slice = pages.get(page_number)
            target_warnings: list[str] = []
            if page_slice is None:
                page_slice = _OdlPageSlice(
                    full_markdown=f"<!-- ODL_PAGE:{page_number} -->",
                    body="",
                )
                target_warnings.append("ODL_PAGE_SECTION_MISSING")

            if page_quality.ocr_required:
                method = PageFallbackMethod.OCR
            elif not page_quality.is_image_only and self._is_chart_page(
                page_quality,
                page_slice.body,
            ):
                if self._vision_provider is not None:
                    method = PageFallbackMethod.VISION
                elif self._ocr_provider is not None:
                    # 纯本地模式下用 OCR 至少保留图表的标签、数值和单位。
                    # 趋势、箭头和实体关系无法由 OCR 可靠推断，由质量报告保留诊断。
                    method = PageFallbackMethod.OCR
                    target_warnings.append("LOCAL_VISUAL_OCR_ONLY")
                else:
                    method = PageFallbackMethod.VISION
            else:
                continue
            targets.append(
                _FallbackTarget(
                    quality=page_quality,
                    method=method,
                    odl_page=page_slice,
                    warnings=tuple(target_warnings),
                    asset_manifest=(asset_manifests or {}).get(page_number, ()),
                )
            )
        return targets

    def _is_chart_page(self, quality: PdfPageQuality, markdown_body: str) -> bool:
        has_visual_reference = bool(
            self._MARKDOWN_IMAGE_RE.search(markdown_body)
            or self._MARKDOWN_REFERENCE_IMAGE_RE.search(markdown_body)
            or self._HTML_IMAGE_RE.search(markdown_body)
        )
        has_material_image_area = (
            quality.image_bbox_area > 0
            and quality.image_coverage_ratio >= self._min_chart_image_coverage_ratio
        )
        has_vector_diagram = bool(
            (
                quality.vector_drawing_count >= 2
                and quality.vector_segment_count >= 4
                and quality.vector_coverage_ratio
                >= self._min_substantive_vector_coverage_ratio
            )
            or (
                # PyMuPDF may group a whole chart/flow into one drawing. Requiring
                # multiple drawing objects missed small unlabeled but material diagrams;
                # area + four segments excludes tiny decorative vector logos.
                quality.vector_segment_count >= 4
                and quality.vector_coverage_ratio
                >= self._min_substantive_vector_coverage_ratio
            )
            or quality.vector_coverage_ratio >= self._min_chart_image_coverage_ratio
        )
        # 图片链接本身可能只是页眉 logo；只有面积达到阈值，或正文/alt 明确表示
        # 图表时才调用视觉模型，避免把每个装饰图都升级成阻断型模型任务。
        # Raster assets cannot be safely classified from a filename alone.  One
        # page-level visual judgment is therefore required even for a small/unknown
        # image; the provider may explicitly classify the page as decoration-only.
        # Tiny vector-only logos remain below the vector threshold and are skipped.
        return bool(
            quality.image_count > 0
            or has_visual_reference
            or has_material_image_area
            or has_vector_diagram
            or (has_visual_reference and self._CHART_HINT_RE.search(markdown_body))
        )

    async def _render_and_process_target(
        self,
        pdf_path: Path,
        target: _FallbackTarget,
        semaphore: asyncio.Semaphore,
    ) -> PdfPageFallbackResult:
        async with semaphore:
            try:
                image = await asyncio.to_thread(
                    self._render_page,
                    pdf_path,
                    target.quality.page_number,
                )
            except _PageRenderLimitError as exc:
                return self._empty_result(
                    target,
                    [*target.warnings, str(exc)],
                    error_code="PAGE_RENDER_LIMIT_EXCEEDED",
                    retryable=False,
                )
            except Exception as exc:
                return self._empty_result(
                    target,
                    [
                        *target.warnings,
                        f"PAGE_RENDER_ERROR:{type(exc).__name__}",
                    ],
                    error_code="PAGE_RENDER_FAILED",
                    retryable=True,
                )
            if target.asset_manifest:
                image = replace(image, asset_manifest=target.asset_manifest)
            return await self._process_target(target, image)

    async def _process_target(
        self,
        target: _FallbackTarget,
        image: RenderedPdfPage | None,
    ) -> PdfPageFallbackResult:
        page_number = target.quality.page_number
        warnings = list(target.warnings)
        if image is None:
            warnings.append("PAGE_RENDER_FAILED")
            return self._empty_result(
                target,
                warnings,
                error_code="PAGE_RENDER_FAILED",
                retryable=True,
            )

        if target.method is PageFallbackMethod.OCR:
            provider = self._ocr_provider
            if provider is None:
                warnings.append("OCR_PROVIDER_MISSING")
                return self._empty_result(
                    target,
                    warnings,
                    error_code="OCR_PROVIDER_MISSING",
                    retryable=False,
                )
        else:
            provider = self._vision_provider
            if provider is None:
                warnings.append("VISION_PROVIDER_MISSING")
                return self._empty_result(
                    target,
                    warnings,
                    error_code="VISION_PROVIDER_MISSING",
                    retryable=False,
                )

        try:
            if target.method is PageFallbackMethod.OCR:
                provider_output = await provider.recognize_page(
                    page_number=page_number,
                    image=image,
                )
            else:
                provider_output = await provider.analyze_page(
                    page_number=page_number,
                    image=image,
                    odl_markdown=target.odl_page.body,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            warnings.append(f"{target.method.value.upper()}_PROVIDER_ERROR:{type(exc).__name__}")
            error_code, retryable = self._classify_provider_error(exc)
            return self._empty_result(
                target,
                warnings,
                error_code=error_code,
                retryable=retryable,
            )

        if not isinstance(provider_output, PageProviderOutput):
            warnings.append("INVALID_PROVIDER_OUTPUT")
            return self._empty_result(
                target,
                warnings,
                error_code="INVALID_PROVIDER_OUTPUT",
                retryable=False,
            )

        warnings.extend(provider_output.warnings)
        completion_error_code = provider_output.completion_error_code
        if completion_error_code is None and provider_output.truncated:
            completion_error_code = "PROVIDER_OUTPUT_TRUNCATED"
        if completion_error_code is not None:
            warnings.append(completion_error_code)
            return self._empty_result(
                target,
                warnings,
                error_code=completion_error_code,
                retryable=False,
                truncated=provider_output.truncated,
            )

        text = provider_output.text.strip()
        supplement = (provider_output.markdown or text).strip()
        text, text_marker_removed = self._remove_page_markers(text)
        supplement, markdown_marker_removed = self._remove_page_markers(supplement)
        if text_marker_removed or markdown_marker_removed:
            warnings.append("PROVIDER_PAGE_MARKER_REMOVED")
        if not text and supplement:
            text = supplement
        explicit_no_visual = bool(
            target.method is PageFallbackMethod.VISION
            and isinstance(provider_output.structured_data, Mapping)
            and provider_output.structured_data.get("has_visual_content") is False
        )
        if not supplement and not explicit_no_visual:
            warnings.append("EMPTY_PROVIDER_OUTPUT")
        error_code = None
        retryable = False
        if not supplement and not explicit_no_visual:
            error_code = "EMPTY_PROVIDER_OUTPUT"
            retryable = True

        merged_markdown, duplicate = self._append_supplement(
            target.odl_page.full_markdown,
            supplement,
            target.method,
        )
        if duplicate:
            warnings.append("PROVIDER_OUTPUT_DUPLICATES_ODL")
        return PdfPageFallbackResult(
            page_number=page_number,
            text=text,
            markdown=merged_markdown,
            confidence=provider_output.confidence,
            method=target.method,
            warnings=tuple(dict.fromkeys(warnings)),
            structured_data=provider_output.structured_data,
            error_code=error_code,
            retryable=retryable,
            truncated=provider_output.truncated,
        )

    @staticmethod
    def _empty_result(
        target: _FallbackTarget,
        warnings: list[str],
        *,
        error_code: str,
        retryable: bool,
        truncated: bool = False,
    ) -> PdfPageFallbackResult:
        return PdfPageFallbackResult(
            page_number=target.quality.page_number,
            text="",
            markdown=target.odl_page.full_markdown,
            confidence=None,
            method=target.method,
            warnings=tuple(dict.fromkeys(warnings)),
            structured_data=None,
            error_code=error_code,
            retryable=retryable,
            truncated=truncated,
        )

    @staticmethod
    def _classify_provider_error(error: BaseException) -> tuple[str, bool]:
        status_code = getattr(error, "status_code", None)
        response = getattr(error, "response", None)
        if status_code is None and response is not None:
            status_code = getattr(response, "status_code", None)
        try:
            normalized_status = int(status_code) if status_code is not None else None
        except (TypeError, ValueError):
            normalized_status = None
        if normalized_status is not None:
            retryable = normalized_status in {408, 425, 429} or (
                500 <= normalized_status <= 599
            )
            return f"PROVIDER_HTTP_{normalized_status}", retryable

        normalized_error = f"{type(error).__name__}: {error}".casefold()
        status_match = re.search(
            r"(?:http(?:\s+status)?|status(?:_code)?|api error)\D{0,12}"
            r"([1-5]\d{2})\b",
            normalized_error,
        )
        if status_match is not None:
            parsed_status = int(status_match.group(1))
            retryable = parsed_status in {408, 425, 429} or 500 <= parsed_status <= 599
            return f"PROVIDER_HTTP_{parsed_status}", retryable

        if isinstance(error, AuthenticationError):
            return "PROVIDER_AUTHENTICATION_FAILED", False
        if isinstance(error, ConfigurationException):
            return "PROVIDER_CONFIGURATION_INVALID", False
        if isinstance(error, RateLimitError):
            return "PROVIDER_RATE_LIMITED", True

        deterministic_markers = (
            "invalid api key",
            "unauthorized",
            "forbidden",
            "permission denied",
            "not configured",
            "configuration error",
            "missing configuration",
            "content_filter",
            "content filter",
            "contentfiltererror",
            "safety policy",
            "safety blocked",
            "safetyerror",
            "moderation blocked",
            "policy violation",
            "prohibited content",
        )
        if any(marker in normalized_error for marker in deterministic_markers):
            return "PROVIDER_DETERMINISTIC_FAILURE", False
        if isinstance(error, (TimeoutError, ConnectionError)) or isinstance(
            error,
            asyncio.TimeoutError,
        ):
            return "PROVIDER_TRANSIENT_FAILURE", True
        if any(
            token in normalized_error
            for token in (
                "timeout",
                "timed out",
                "connection failed",
                "connection reset",
                "network unavailable",
                "temporary network",
            )
        ):
            return "PROVIDER_TRANSIENT_FAILURE", True
        explicit_retryable = getattr(error, "retryable", None)
        if isinstance(explicit_retryable, bool):
            return "PROVIDER_REQUEST_FAILED", explicit_retryable
        # 未知厂商异常不能证明是输入确定性错误，交给有限次数队列退避。
        return "PROVIDER_REQUEST_FAILED", True

    @classmethod
    def _remove_page_markers(cls, value: str) -> tuple[str, bool]:
        cleaned, count = cls._PAGE_MARKER_RE.subn("", value or "")
        return cleaned.strip(), count > 0

    @staticmethod
    def _append_supplement(
        original: str,
        supplement: str,
        method: PageFallbackMethod,
    ) -> tuple[str, bool]:
        normalized_original = original.strip()
        normalized_supplement = supplement.strip()
        if not normalized_supplement:
            return normalized_original, False
        if normalized_supplement in normalized_original:
            return normalized_original, True
        marker = f"<!-- PAGE_FALLBACK:{method.value.upper()} -->"
        if method is PageFallbackMethod.VISION and not normalized_supplement.startswith(
            "图片说明："
        ):
            normalized_supplement = f"图片说明：{normalized_supplement}"
        return f"{normalized_original}\n\n{marker}\n\n{normalized_supplement}", False

    @classmethod
    def merge_markdown(
        cls,
        odl_markdown: str,
        report: PdfPageFallbackReport,
    ) -> str:
        """按原 PDF 页序替换已补齐页面，未命中页面保持 ODL 原文不变。"""

        replacements = {result.page_number: result.markdown for result in report.results}
        if not replacements:
            return odl_markdown
        matches = list(cls._PAGE_MARKER_RE.finditer(odl_markdown or ""))
        if not matches:
            return odl_markdown
        parts = [odl_markdown[: matches[0].start()]]
        for index, match in enumerate(matches):
            page_number = int(match.group(1))
            end = matches[index + 1].start() if index + 1 < len(matches) else len(odl_markdown)
            original = odl_markdown[match.start() : end].strip()
            parts.append((replacements.get(page_number) or original).strip())
        return "\n\n".join(part for part in parts if part).strip()

    @classmethod
    def _split_odl_markdown(
        cls,
        markdown: str,
    ) -> tuple[dict[int, _OdlPageSlice], list[str]]:
        matches = list(cls._PAGE_MARKER_RE.finditer(markdown or ""))
        pages: dict[int, _OdlPageSlice] = {}
        warnings: list[str] = []
        for index, match in enumerate(matches):
            page_number = int(match.group(1))
            end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown)
            full_markdown = markdown[match.start() : end].strip()
            body = markdown[match.end() : end].strip()
            if page_number in pages:
                warnings.append(f"DUPLICATE_ODL_PAGE_SECTION:page={page_number}")
                continue
            pages[page_number] = _OdlPageSlice(
                full_markdown=full_markdown,
                body=body,
            )
        return pages, warnings

    @staticmethod
    def _asset_manifests(markdown: str) -> dict[int, tuple[dict[str, object], ...]]:
        report = PdfImageAssetPolicy().extract_and_classify(markdown)
        grouped: dict[int, list[dict[str, object]]] = {}
        for asset in report.assets:
            if asset.page_number is None:
                continue
            grouped.setdefault(asset.page_number, []).append(
                {
                    "asset_key": asset.asset_key,
                    "source_ref": asset.source_ref,
                    "line_number": asset.line_number,
                    "occurrence_index": asset.occurrence_index,
                    "category_hint": asset.category.value,
                }
            )
        return {page: tuple(items) for page, items in grouped.items()}

    @staticmethod
    def _pdf_page_count(pdf_path: Path) -> int:
        document = pymupdf.open(filename=str(pdf_path))
        try:
            return int(document.page_count)
        finally:
            document.close()

    def _render_page(self, pdf_path: Path, page_number: int) -> RenderedPdfPage:
        document = pymupdf.open(filename=str(pdf_path))
        try:
            if page_number < 1 or page_number > document.page_count:
                raise IndexError(f"PAGE_OUT_OF_RANGE:page={page_number}")
            scale = self._dpi / 72.0
            matrix = pymupdf.Matrix(scale, scale)
            page = document.load_page(page_number - 1)
            expected_width = math.ceil(float(page.rect.width) * scale)
            expected_height = math.ceil(float(page.rect.height) * scale)
            expected_pixels = expected_width * expected_height
            if expected_pixels > self._max_rendered_page_pixels:
                raise _PageRenderLimitError(
                    "PAGE_RENDER_PIXELS_EXCEEDED:"
                    f"page={page_number},actual={expected_pixels},"
                    f"limit={self._max_rendered_page_pixels}"
                )
            pixmap = page.get_pixmap(
                matrix=matrix,
                colorspace=pymupdf.csRGB,
                alpha=False,
            )
            image_bytes = pixmap.tobytes("png")
            if len(image_bytes) > self._max_rendered_page_bytes:
                raise _PageRenderLimitError(
                    "PAGE_RENDER_BYTES_EXCEEDED:"
                    f"page={page_number},actual={len(image_bytes)},"
                    f"limit={self._max_rendered_page_bytes}"
                )
            provider_data_uri_bytes = self._provider_data_uri_size(len(image_bytes))
            if provider_data_uri_bytes > self._MAX_PROVIDER_DATA_URI_BYTES:
                raise _PageRenderLimitError(
                    "PAGE_PROVIDER_DATA_URI_BYTES_EXCEEDED:"
                    f"page={page_number},actual={provider_data_uri_bytes},"
                    f"limit={self._MAX_PROVIDER_DATA_URI_BYTES}"
                )
            return RenderedPdfPage(
                page_number=page_number,
                image_bytes=image_bytes,
                media_type="image/png",
                dpi=self._dpi,
                width=pixmap.width,
                height=pixmap.height,
            )
        finally:
            document.close()

    @classmethod
    def _provider_data_uri_size(cls, raw_image_bytes: int) -> int:
        if isinstance(raw_image_bytes, bool) or raw_image_bytes < 0:
            raise ValueError("raw_image_bytes must not be negative")
        encoded_bytes = 4 * math.ceil(raw_image_bytes / 3)
        return cls._PROVIDER_DATA_URI_PREFIX_BYTES + encoded_bytes


class _PageRenderLimitError(RuntimeError):
    pass


__all__ = [
    "AnalyzeImagePageProviderAdapter",
    "AsyncOcrPageProvider",
    "AsyncVisionPageProvider",
    "DEFAULT_OCR_PROMPT",
    "DEFAULT_VISION_PROMPT",
    "PageFallbackMethod",
    "PageProviderOutput",
    "PdfPageFallbackProcessor",
    "PdfPageFallbackReport",
    "PdfPageFallbackResult",
    "RenderedPdfPage",
]
