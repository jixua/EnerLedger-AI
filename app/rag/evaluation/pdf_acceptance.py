"""Gold-set based PDF parsing and retrieval acceptance evaluation.

The evaluator is deliberately independent from the runtime parser.  It consumes
JSON-compatible dictionaries so that an OpenDataLoader result, a fallback result,
or a manually prepared fixture can all be evaluated with the same rules.

An absent gold annotation produces ``NOT_EVALUABLE``.  It never turns into a
vacuous pass.  An explicitly present empty annotation (for example
``"scan_pages": []``) is different: it proves that the gold set contains no such
objects and may therefore pass when the prediction also contains none.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class AcceptanceStatus(StrEnum):
    """Status of one metric or of the complete acceptance report."""

    PASS = "PASS"
    FAIL = "FAIL"
    NOT_EVALUABLE = "NOT_EVALUABLE"


@dataclass(frozen=True, slots=True)
class PdfAcceptancePolicy:
    """Acceptance thresholds from the project quality specification."""

    page_count_accuracy: float = 1.0
    page_order_coverage: float = 1.0
    scan_page_ocr_coverage: float = 1.0
    body_character_accuracy: float = 0.97
    critical_token_exactness: float = 1.0
    formula_exactness: float = 1.0
    simple_table_cell_accuracy: float = 1.0
    complex_table_cell_accuracy: float = 0.98
    cross_page_table_exactness: float = 1.0
    visual_structure_exactness: float = 1.0
    citation_exactness: float = 1.0
    max_undescribed_image_chunks: int = 0
    max_top5_noise_results: int = 0


@dataclass(frozen=True, slots=True)
class AcceptanceMetric:
    """A JSON-serializable metric result."""

    name: str
    status: AcceptanceStatus
    value: float | int | None
    threshold: float | int
    comparator: str
    unit: str
    details: Mapping[str, Any] = field(default_factory=dict)
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status.value,
            "value": self.value,
            "threshold": self.threshold,
            "comparator": self.comparator,
            "unit": self.unit,
            "details": dict(self.details),
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class PdfAcceptanceReport:
    """Complete evaluation output."""

    status: AcceptanceStatus
    metrics: tuple[AcceptanceMetric, ...]
    schema_version: str = "1.0"

    @property
    def acceptance_ready(self) -> bool:
        return self.status is AcceptanceStatus.PASS

    @property
    def failed_metrics(self) -> tuple[AcceptanceMetric, ...]:
        return tuple(metric for metric in self.metrics if metric.status is AcceptanceStatus.FAIL)

    @property
    def not_evaluable_metrics(self) -> tuple[AcceptanceMetric, ...]:
        return tuple(
            metric for metric in self.metrics if metric.status is AcceptanceStatus.NOT_EVALUABLE
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "status": self.status.value,
            "acceptance_ready": self.acceptance_ready,
            "summary": {
                "metric_count": len(self.metrics),
                "passed": sum(metric.status is AcceptanceStatus.PASS for metric in self.metrics),
                "failed": len(self.failed_metrics),
                "not_evaluable": len(self.not_evaluable_metrics),
            },
            "metrics": [metric.to_dict() for metric in self.metrics],
        }


_PAGE_MARKER_RE = re.compile(
    r"(?im)^\s*<!--\s*(?:ODL_PAGE|page(?:_number)?)\s*[:=]\s*\d+\s*-->\s*$"
)
_MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]]*]\([^\n)]*\)")
_HTML_IMAGE_RE = re.compile(r"(?is)<img\b[^>]*>")
_PLACEHOLDER_IMAGE_RE = re.compile(
    r"(?:未提供|暂无|无)(?:图片|图表|图像)?(?:说明|描述|内容)"
    r"|(?:image|figure)\s+(?:description|caption)\s+(?:missing|unavailable)",
    re.IGNORECASE,
)
_NOISE_CATEGORIES = {
    "header",
    "footer",
    "public_attribute",
    "signature",
    "seal",
    "decoration",
    "页眉",
    "页脚",
    "公开属性",
    "签名",
    "印章",
    "装饰图",
}


@dataclass(frozen=True, slots=True)
class _ResolvedCollection:
    """One structured acceptance collection and its runtime provenance."""

    value: Any = None
    present: bool = False
    source: str = "missing"
    error: str | None = None


@dataclass(frozen=True, slots=True)
class _ValidatedCollection:
    items: tuple[Mapping[str, Any], ...] = ()
    present: bool = False
    source: str = "missing"
    error: str | None = None


class PdfAcceptanceEvaluator:
    """Evaluate one prediction bundle against a manually reviewed gold bundle."""

    def __init__(self, policy: PdfAcceptancePolicy | None = None) -> None:
        self._policy = policy or PdfAcceptancePolicy()

    def evaluate(
        self,
        gold: Mapping[str, Any] | None,
        prediction: Mapping[str, Any] | None,
    ) -> PdfAcceptanceReport:
        gold_data = _as_mapping(gold)
        prediction_data = _as_mapping(prediction)
        metrics = (
            *self._page_metrics(gold_data, prediction_data),
            self._ocr_metric(gold_data, prediction_data),
            self._body_text_metric(gold_data, prediction_data),
            self._critical_token_metric(gold_data, prediction_data),
            self._formula_metric(gold_data, prediction_data),
            *self._table_metrics(gold_data, prediction_data),
            self._visual_metric(gold_data, prediction_data),
            self._citation_metric(gold_data, prediction_data),
            self._undescribed_image_metric(prediction_data),
            self._top5_noise_metric(gold_data, prediction_data),
        )
        if any(metric.status is AcceptanceStatus.FAIL for metric in metrics):
            status = AcceptanceStatus.FAIL
        elif any(metric.status is AcceptanceStatus.NOT_EVALUABLE for metric in metrics):
            status = AcceptanceStatus.NOT_EVALUABLE
        else:
            status = AcceptanceStatus.PASS
        return PdfAcceptanceReport(status=status, metrics=metrics)

    def _page_metrics(
        self,
        gold: Mapping[str, Any],
        prediction: Mapping[str, Any],
    ) -> tuple[AcceptanceMetric, AcceptanceMetric]:
        expected, expected_known = _expected_page_sequence(gold)
        actual, actual_known = _predicted_page_sequence(prediction)
        if not expected_known:
            reason = "金标缺少 pages 或 document.source_page_count"
            return (
                _not_evaluable(
                    "page_count_accuracy", self._policy.page_count_accuracy, ">=", "ratio", reason
                ),
                _not_evaluable(
                    "page_order_coverage",
                    self._policy.page_order_coverage,
                    ">=",
                    "ratio",
                    reason,
                ),
            )
        if not actual_known:
            reason = "预测结果缺少 pages 或 page_numbers"
            return (
                _not_evaluable(
                    "page_count_accuracy", self._policy.page_count_accuracy, ">=", "ratio", reason
                ),
                _not_evaluable(
                    "page_order_coverage",
                    self._policy.page_order_coverage,
                    ">=",
                    "ratio",
                    reason,
                ),
            )

        maximum_count = max(len(expected), len(actual), 1)
        count_accuracy = 1.0 - abs(len(expected) - len(actual)) / maximum_count
        positional_matches = sum(
            expected_page == actual_page
            for expected_page, actual_page in zip(expected, actual, strict=False)
        )
        order_coverage = positional_matches / maximum_count
        details = {
            "expected_page_numbers": expected,
            "predicted_page_numbers": actual,
            "expected_count": len(expected),
            "predicted_count": len(actual),
        }
        return (
            _threshold_metric(
                "page_count_accuracy",
                count_accuracy,
                self._policy.page_count_accuracy,
                ">=",
                "ratio",
                details,
            ),
            _threshold_metric(
                "page_order_coverage",
                order_coverage,
                self._policy.page_order_coverage,
                ">=",
                "ratio",
                details,
            ),
        )

    def _ocr_metric(
        self,
        gold: Mapping[str, Any],
        prediction: Mapping[str, Any],
    ) -> AcceptanceMetric:
        scan_pages, annotation_known = _gold_scan_pages(gold)
        if not annotation_known:
            return _not_evaluable(
                "scan_page_ocr_coverage",
                self._policy.scan_page_ocr_coverage,
                ">=",
                "ratio",
                "金标缺少 scan_pages 或 pages[].is_scanned 标注",
            )
        ocr_pages, ocr_known = _predicted_ocr_pages(prediction)
        if not ocr_known:
            return _not_evaluable(
                "scan_page_ocr_coverage",
                self._policy.scan_page_ocr_coverage,
                ">=",
                "ratio",
                "预测结果缺少 ocr_pages 或 pages[].ocr_completed",
            )
        covered = sorted(scan_pages & ocr_pages)
        missing = sorted(scan_pages - ocr_pages)
        value = len(covered) / len(scan_pages) if scan_pages else 1.0
        return _threshold_metric(
            "scan_page_ocr_coverage",
            value,
            self._policy.scan_page_ocr_coverage,
            ">=",
            "ratio",
            {
                "gold_scan_pages": sorted(scan_pages),
                "completed_ocr_pages": sorted(ocr_pages),
                "covered_pages": covered,
                "missing_pages": missing,
            },
        )

    def _body_text_metric(
        self,
        gold: Mapping[str, Any],
        prediction: Mapping[str, Any],
    ) -> AcceptanceMetric:
        gold_texts, annotation_known = _page_texts(gold, gold_mode=True)
        if not annotation_known or not gold_texts:
            return _not_evaluable(
                "body_character_accuracy",
                self._policy.body_character_accuracy,
                ">=",
                "ratio",
                "金标缺少可用于 CER 的 pages[].body_text/text",
            )
        predicted_texts, prediction_known = _page_texts(prediction, gold_mode=False)
        if not prediction_known:
            return _not_evaluable(
                "body_character_accuracy",
                self._policy.body_character_accuracy,
                ">=",
                "ratio",
                "预测结果缺少 pages[].body_text/text",
            )

        total_gold_chars = 0
        total_edits = 0
        page_details: list[dict[str, Any]] = []
        for page_number, expected_text in sorted(gold_texts.items()):
            actual_text = predicted_texts.get(page_number, "")
            expected_normalized = _normalize_body_text(expected_text)
            actual_normalized = _normalize_body_text(actual_text)
            distance = _levenshtein_distance(expected_normalized, actual_normalized)
            denominator = max(len(expected_normalized), 1)
            page_accuracy = max(0.0, 1.0 - distance / denominator)
            total_gold_chars += denominator
            total_edits += distance
            page_details.append(
                {
                    "page_number": page_number,
                    "gold_characters": len(expected_normalized),
                    "predicted_characters": len(actual_normalized),
                    "edit_distance": distance,
                    "accuracy": page_accuracy,
                }
            )
        accuracy = max(0.0, 1.0 - total_edits / max(total_gold_chars, 1))
        return _threshold_metric(
            "body_character_accuracy",
            accuracy,
            self._policy.body_character_accuracy,
            ">=",
            "ratio",
            {
                "normalization": "Unicode NFC; ignore whitespace, page markers and image links",
                "gold_characters": total_gold_chars,
                "edit_distance": total_edits,
                "pages": page_details,
            },
        )

    def _critical_token_metric(
        self,
        gold: Mapping[str, Any],
        prediction: Mapping[str, Any],
    ) -> AcceptanceMetric:
        if "critical_tokens" not in gold:
            return _not_evaluable(
                "critical_token_exactness",
                self._policy.critical_token_exactness,
                ">=",
                "ratio",
                "金标缺少 critical_tokens 标注",
            )
        entries, gold_error = _validated_critical_token_entries(
            gold.get("critical_tokens"), path="gold.critical_tokens"
        )
        if gold_error:
            return _not_evaluable(
                "critical_token_exactness",
                self._policy.critical_token_exactness,
                ">=",
                "ratio",
                gold_error,
            )
        predicted_texts, text_known = _page_texts(prediction, gold_mode=False)
        explicit_prediction = "critical_tokens" in prediction
        predicted_entries: list[Mapping[str, Any]] = []
        if explicit_prediction:
            predicted_entries, prediction_error = _validated_critical_token_entries(
                prediction.get("critical_tokens"), path="prediction.critical_tokens"
            )
            if prediction_error:
                return _schema_failure_metric(
                    "critical_token_exactness",
                    self._policy.critical_token_exactness,
                    prediction_error,
                    source="critical_tokens",
                )
        if entries and not text_known and not explicit_prediction:
            return _not_evaluable(
                "critical_token_exactness",
                self._policy.critical_token_exactness,
                ">=",
                "ratio",
                "预测结果无页文本或 critical_tokens",
            )

        expected_counter = Counter(_critical_signature(entry) for entry in entries)
        explicit_counter = Counter(
            _critical_signature(entry) for entry in predicted_entries
        )
        signatures = set(expected_counter) | set(explicit_counter)
        text_counter = _critical_text_occurrences(signatures, predicted_texts)
        available_counter: Counter[tuple[str, str, int | None]] = Counter(
            {
                signature: max(text_counter[signature], explicit_counter[signature])
                for signature in signatures
            }
        )

        matched: list[dict[str, Any]] = []
        missing: list[dict[str, Any]] = []
        by_category: dict[str, dict[str, int]] = {}
        remaining = available_counter.copy()
        for entry in entries:
            signature = _critical_signature(entry)
            found = remaining[signature] > 0
            if found:
                remaining[signature] -= 1
            category = str(entry.get("category", "unspecified"))
            stats = by_category.setdefault(category, {"gold": 0, "matched": 0})
            stats["gold"] += 1
            if found:
                stats["matched"] += 1
                matched.append(dict(entry))
            else:
                missing.append(dict(entry))
        unexpected = _counter_difference(available_counter, expected_counter)
        denominator = max(sum(expected_counter.values()), sum(available_counter.values()), 1)
        value = (
            len(matched) / denominator
            if expected_counter or available_counter
            else 1.0
        )
        return _threshold_metric(
            "critical_token_exactness",
            value,
            self._policy.critical_token_exactness,
            ">=",
            "ratio",
            {
                "gold_count": len(entries),
                "predicted_occurrence_count": sum(available_counter.values()),
                "matched_count": len(matched),
                "missing": missing,
                "unexpected": unexpected,
                "by_category": by_category,
                "matching": "Unicode NFC; ASCII-token boundaries; exact occurrence counts",
            },
        )

    def _formula_metric(
        self,
        gold: Mapping[str, Any],
        prediction: Mapping[str, Any],
    ) -> AcceptanceMetric:
        return self._collection_exactness_metric(
            name="formula_exactness",
            gold=gold,
            prediction=prediction,
            key="formulas",
            threshold=self._policy.formula_exactness,
            signature=_formula_signature,
        )

    def _table_metrics(
        self,
        gold: Mapping[str, Any],
        prediction: Mapping[str, Any],
    ) -> tuple[AcceptanceMetric, AcceptanceMetric, AcceptanceMetric]:
        names = (
            ("simple_table_cell_accuracy", self._policy.simple_table_cell_accuracy, "simple"),
            ("complex_table_cell_accuracy", self._policy.complex_table_cell_accuracy, "complex"),
        )
        gold_collection = _validated_collection(gold, "tables")
        if not gold_collection.present:
            reason = "金标缺少 tables 标注"
            return (
                *(
                    _not_evaluable(name, threshold, ">=", "ratio", reason)
                    for name, threshold, _kind in names
                ),
                _not_evaluable(
                    "cross_page_table_exactness",
                    self._policy.cross_page_table_exactness,
                    ">=",
                    "ratio",
                    reason,
                ),
            )
        if gold_collection.error:
            return tuple(
                _not_evaluable(name, threshold, ">=", "ratio", gold_collection.error)
                for name, threshold, _kind in names
            ) + (
                _not_evaluable(
                    "cross_page_table_exactness",
                    self._policy.cross_page_table_exactness,
                    ">=",
                    "ratio",
                    gold_collection.error,
                ),
            )
        predicted_collection = _validated_collection(prediction, "tables")
        if predicted_collection.error:
            return tuple(
                _schema_failure_metric(
                    name,
                    threshold,
                    predicted_collection.error,
                    source=predicted_collection.source,
                )
                for name, threshold, _kind in names
            ) + (
                _schema_failure_metric(
                    "cross_page_table_exactness",
                    self._policy.cross_page_table_exactness,
                    predicted_collection.error,
                    source=predicted_collection.source,
                ),
            )
        gold_tables = list(gold_collection.items)
        predicted_tables = list(predicted_collection.items)
        results: list[AcceptanceMetric] = []
        for name, threshold, kind in names:
            expected = [table for table in gold_tables if _table_kind(table) == kind]
            actual = [table for table in predicted_tables if _table_kind(table) == kind]
            accuracy, details = _table_cell_accuracy(expected, actual)
            results.append(
                _threshold_metric(name, accuracy, threshold, ">=", "ratio", details)
            )

        indexed_gold_cross_page = [
            (index, table)
            for index, table in enumerate(gold_tables)
            if _is_cross_page_table(table)
        ]
        indexed_predicted_cross_page = [
            (index, table)
            for index, table in enumerate(predicted_tables)
            if _is_cross_page_table(table)
        ]
        predicted_by_id = {
            _table_identifier(table, index): table
            for index, table in indexed_predicted_cross_page
        }
        correct = 0
        differences: list[dict[str, Any]] = []
        seen_cross_page_ids: set[str] = set()
        for index, table in indexed_gold_cross_page:
            identifier = _table_identifier(table, index)
            seen_cross_page_ids.add(identifier)
            predicted = predicted_by_id.get(identifier)
            expected_signature = _cross_page_table_signature(table)
            if predicted:
                full_actual_signature = _cross_page_table_signature(predicted)
                actual_signature = {
                    key: full_actual_signature.get(key) for key in expected_signature
                }
            else:
                actual_signature = None
            if expected_signature == actual_signature:
                correct += 1
            else:
                differences.append(
                    {
                        "table_id": identifier,
                        "expected": expected_signature,
                        "predicted": actual_signature,
                    }
                )
        for identifier, predicted in predicted_by_id.items():
            if identifier not in seen_cross_page_ids:
                differences.append(
                    {
                        "table_id": identifier,
                        "expected": None,
                        "predicted": _cross_page_table_signature(predicted),
                    }
                )
        denominator = max(
            len(indexed_gold_cross_page), len(indexed_predicted_cross_page), 1
        )
        value = (
            correct / denominator
            if indexed_gold_cross_page or indexed_predicted_cross_page
            else 1.0
        )
        results.append(
            _threshold_metric(
                "cross_page_table_exactness",
                value,
                self._policy.cross_page_table_exactness,
                ">=",
                "ratio",
                {
                    "gold_cross_page_tables": len(indexed_gold_cross_page),
                    "predicted_cross_page_tables": len(indexed_predicted_cross_page),
                    "correct_tables": correct,
                    "differences": differences,
                },
            )
        )
        return tuple(results)  # type: ignore[return-value]

    def _visual_metric(
        self,
        gold: Mapping[str, Any],
        prediction: Mapping[str, Any],
    ) -> AcceptanceMetric:
        return self._collection_exactness_metric(
            name="visual_structure_exactness",
            gold=gold,
            prediction=prediction,
            key="visuals",
            threshold=self._policy.visual_structure_exactness,
            signature=_visual_atomic_signatures,
            flatten=True,
        )

    def _citation_metric(
        self,
        gold: Mapping[str, Any],
        prediction: Mapping[str, Any],
    ) -> AcceptanceMetric:
        return self._collection_exactness_metric(
            name="citation_exactness",
            gold=gold,
            prediction=prediction,
            key="citations",
            threshold=self._policy.citation_exactness,
            signature=_citation_signatures,
            flatten=True,
        )

    def _undescribed_image_metric(self, prediction: Mapping[str, Any]) -> AcceptanceMetric:
        records, source, error = _runtime_image_records(prediction)
        if error:
            return _schema_failure_count_metric(
                "undescribed_image_chunks",
                self._policy.max_undescribed_image_chunks,
                error,
                source=source,
            )
        if records is None:
            return _not_evaluable(
                "undescribed_image_chunks",
                self._policy.max_undescribed_image_chunks,
                "<=",
                "count",
                "预测结果缺少 chunks，无法确认图片块是否入索引",
            )
        offending: list[str] = []
        inspected = 0
        for index, chunk in enumerate(records):
            is_runtime_asset = source == "parse_quality.image_assets.assets"
            if is_runtime_asset:
                if chunk.get("retrieval_asset") is not True:
                    continue
            elif not _is_retrievable_image_chunk(chunk):
                continue
            inspected += 1
            if not _has_meaningful_visual_description(chunk):
                offending.append(
                    str(
                        chunk.get(
                            "source_ref",
                            chunk.get("id", chunk.get("chunk_id", index)),
                        )
                    )
                )
        return _threshold_metric(
            "undescribed_image_chunks",
            len(offending),
            self._policy.max_undescribed_image_chunks,
            "<=",
            "count",
            {
                "source": source,
                "retrievable_image_chunks": inspected,
                "offending_chunk_ids": offending,
            },
        )

    def _top5_noise_metric(
        self,
        gold: Mapping[str, Any],
        prediction: Mapping[str, Any],
    ) -> AcceptanceMetric:
        cases, annotation_known, reason = _retrieval_cases(gold, prediction)
        if not annotation_known:
            return _not_evaluable(
                "top5_noise_results",
                self._policy.max_top5_noise_results,
                "<=",
                "count",
                reason,
            )
        offending: list[dict[str, Any]] = []
        inspected = 0
        for case_id, results, noise_ids in cases:
            for rank, result in enumerate(results[:5], start=1):
                inspected += 1
                if _is_noise_result(result, noise_ids):
                    offending.append(
                        {
                            "case_id": case_id,
                            "rank": rank,
                            "chunk_id": result.get("chunk_id", result.get("id")),
                            "category": result.get("noise_category", result.get("category")),
                        }
                    )
        return _threshold_metric(
            "top5_noise_results",
            len(offending),
            self._policy.max_top5_noise_results,
            "<=",
            "count",
            {"inspected_results": inspected, "offending_results": offending},
        )

    def _collection_exactness_metric(
        self,
        *,
        name: str,
        gold: Mapping[str, Any],
        prediction: Mapping[str, Any],
        key: str,
        threshold: float,
        signature: Any,
        flatten: bool = False,
    ) -> AcceptanceMetric:
        gold_collection = _validated_collection(gold, key)
        if not gold_collection.present:
            return _not_evaluable(
                name,
                threshold,
                ">=",
                "ratio",
                f"金标缺少 {key} 标注",
            )
        if gold_collection.error:
            return _not_evaluable(
                name,
                threshold,
                ">=",
                "ratio",
                gold_collection.error,
            )
        predicted_collection = _validated_collection(prediction, key)
        if predicted_collection.error:
            return _schema_failure_metric(
                name,
                threshold,
                predicted_collection.error,
                source=predicted_collection.source,
            )
        gold_items = gold_collection.items
        predicted_items = predicted_collection.items
        if flatten:
            expected = Counter(
                atomic
                for item in gold_items
                for atomic in _ensure_signature_sequence(signature(item))
            )
            actual = Counter(
                atomic
                for item in predicted_items
                for atomic in _ensure_signature_sequence(signature(item))
            )
        else:
            expected = Counter(signature(item) for item in gold_items)
            actual = Counter(signature(item) for item in predicted_items)
        matched = sum((expected & actual).values())
        denominator = max(sum(expected.values()), sum(actual.values()), 1)
        value = matched / denominator if expected or actual else 1.0
        return _threshold_metric(
            name,
            value,
            threshold,
            ">=",
            "ratio",
            {
                "gold_atomic_count": sum(expected.values()),
                "predicted_atomic_count": sum(actual.values()),
                "matched_atomic_count": matched,
                "missing": _counter_difference(expected, actual),
                "unexpected": _counter_difference(actual, expected),
            },
        )


def evaluate_pdf_acceptance(
    gold: Mapping[str, Any] | None,
    prediction: Mapping[str, Any] | None,
    *,
    policy: PdfAcceptancePolicy | None = None,
) -> PdfAcceptanceReport:
    """Convenience entry point for callers that do not need an evaluator instance."""

    return PdfAcceptanceEvaluator(policy=policy).evaluate(gold, prediction)


def _threshold_metric(
    name: str,
    value: float | int,
    threshold: float | int,
    comparator: str,
    unit: str,
    details: Mapping[str, Any],
) -> AcceptanceMetric:
    if comparator == ">=":
        passed = value >= threshold
    elif comparator == "<=":
        passed = value <= threshold
    else:  # pragma: no cover - only internal policy constants reach here
        raise ValueError(f"unsupported comparator: {comparator}")
    return AcceptanceMetric(
        name=name,
        status=AcceptanceStatus.PASS if passed else AcceptanceStatus.FAIL,
        value=value,
        threshold=threshold,
        comparator=comparator,
        unit=unit,
        details=details,
    )


def _not_evaluable(
    name: str,
    threshold: float | int,
    comparator: str,
    unit: str,
    reason: str,
) -> AcceptanceMetric:
    return AcceptanceMetric(
        name=name,
        status=AcceptanceStatus.NOT_EVALUABLE,
        value=None,
        threshold=threshold,
        comparator=comparator,
        unit=unit,
        reason=reason,
    )


def _schema_failure_metric(
    name: str,
    threshold: float | int,
    error: str,
    *,
    source: str,
) -> AcceptanceMetric:
    return AcceptanceMetric(
        name=name,
        status=AcceptanceStatus.FAIL,
        value=0.0,
        threshold=threshold,
        comparator=">=",
        unit="ratio",
        details={"schema_error": error, "source": source},
        reason=error,
    )


def _schema_failure_count_metric(
    name: str,
    threshold: int,
    error: str,
    *,
    source: str,
) -> AcceptanceMetric:
    return AcceptanceMetric(
        name=name,
        status=AcceptanceStatus.FAIL,
        value=threshold + 1,
        threshold=threshold,
        comparator="<=",
        unit="count",
        details={"schema_error": error, "source": source},
        reason=error,
    )


def _as_mapping(value: Mapping[str, Any] | None) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _mapping_items(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _parse_quality_container(bundle: Mapping[str, Any]) -> Mapping[str, Any]:
    nested = bundle.get("parse_quality")
    return nested if isinstance(nested, Mapping) else bundle


def _resolve_collection(bundle: Mapping[str, Any], key: str) -> _ResolvedCollection:
    """Resolve fixture arrays and the equivalent persisted ``parse_quality`` shape."""

    if key in bundle:
        return _ResolvedCollection(bundle.get(key), True, key)

    if "parse_quality" in bundle and not isinstance(bundle.get("parse_quality"), Mapping):
        return _ResolvedCollection(
            present=True,
            source=f"parse_quality.{key}",
            error="prediction.parse_quality 必须是对象",
        )

    quality = _parse_quality_container(bundle)
    if quality is not bundle and key in quality:
        return _ResolvedCollection(quality.get(key), True, f"parse_quality.{key}")

    if key == "tables" and "table_structure" in quality:
        structure = quality.get("table_structure")
        source = "parse_quality.table_structure.tables"
        if structure is None:
            return _ResolvedCollection([], True, source)
        if not isinstance(structure, Mapping):
            return _ResolvedCollection(
                present=True,
                source=source,
                error="prediction.parse_quality.table_structure 必须是对象",
            )
        if "tables" not in structure:
            return _ResolvedCollection([], True, source)
        return _ResolvedCollection(structure.get("tables"), True, source)

    if key == "visuals" and "fallback" in quality:
        return _runtime_visual_collection(quality.get("fallback"))

    return _ResolvedCollection()


def _runtime_visual_collection(fallback: Any) -> _ResolvedCollection:
    source = "parse_quality.fallback.results[].structured_data"
    if fallback is None:
        return _ResolvedCollection([], True, source)
    if not isinstance(fallback, Mapping):
        return _ResolvedCollection(
            present=True,
            source=source,
            error="prediction.parse_quality.fallback 必须是对象或 null",
        )
    if "results" not in fallback:
        return _ResolvedCollection([], True, source)
    raw_results = fallback.get("results")
    if not _is_non_string_sequence(raw_results):
        return _ResolvedCollection(
            present=True,
            source=source,
            error="prediction.parse_quality.fallback.results 必须是对象数组",
        )

    visuals: list[Mapping[str, Any]] = []
    for index, raw_result in enumerate(raw_results):
        if not isinstance(raw_result, Mapping):
            return _ResolvedCollection(
                present=True,
                source=source,
                error=(
                    "prediction.parse_quality.fallback.results"
                    f"[{index}] 必须是对象"
                ),
            )
        structured = raw_result.get("structured_data")
        method = str(raw_result.get("method") or "").casefold()
        if structured is None:
            if method == "vision":
                return _ResolvedCollection(
                    present=True,
                    source=source,
                    error=(
                        "prediction.parse_quality.fallback.results"
                        f"[{index}].structured_data 在 vision 结果中不能为 null"
                    ),
                )
            continue
        if not isinstance(structured, Mapping):
            return _ResolvedCollection(
                present=True,
                source=source,
                error=(
                    "prediction.parse_quality.fallback.results"
                    f"[{index}].structured_data 必须是对象"
                ),
            )
        if method != "vision" and not _has_visual_dimensions(structured):
            continue
        normalized = dict(structured)
        result_page = _optional_int(raw_result.get("page_number"))
        structured_page = _optional_int(
            structured.get("source_page") or structured.get("page_number")
        )
        if result_page is None:
            return _ResolvedCollection(
                present=True,
                source=source,
                error=(
                    "prediction.parse_quality.fallback.results"
                    f"[{index}].page_number 必须是正整数"
                ),
            )
        if structured_page is not None and structured_page != result_page:
            return _ResolvedCollection(
                present=True,
                source=source,
                error=(
                    "prediction.parse_quality.fallback.results"
                    f"[{index}].structured_data 页码与回退任务页码不一致"
                ),
            )
        normalized["page_number"] = result_page
        visuals.append(normalized)
    return _ResolvedCollection(visuals, True, source)


def _validated_collection(bundle: Mapping[str, Any], key: str) -> _ValidatedCollection:
    resolved = _resolve_collection(bundle, key)
    if not resolved.present or resolved.error:
        return _ValidatedCollection(
            present=resolved.present,
            source=resolved.source,
            error=resolved.error,
        )
    if not _is_non_string_sequence(resolved.value):
        return _ValidatedCollection(
            present=True,
            source=resolved.source,
            error=f"{resolved.source} 必须是对象数组",
        )

    items: list[Mapping[str, Any]] = []
    for index, item in enumerate(resolved.value):
        if not isinstance(item, Mapping):
            return _ValidatedCollection(
                present=True,
                source=resolved.source,
                error=f"{resolved.source}[{index}] 必须是对象",
            )
        error = _collection_item_schema_error(key, item, f"{resolved.source}[{index}]")
        if error:
            return _ValidatedCollection(
                present=True,
                source=resolved.source,
                error=error,
            )
        items.append(item)
    return _ValidatedCollection(tuple(items), True, resolved.source)


def _collection_item_schema_error(
    key: str,
    item: Mapping[str, Any],
    path: str,
) -> str | None:
    if key == "formulas":
        return _formula_schema_error(item, path)
    if key == "tables":
        return _table_schema_error(item, path)
    if key == "visuals":
        return _visual_schema_error(item, path)
    if key == "citations":
        return _citation_schema_error(item, path)
    return None


def _formula_schema_error(item: Mapping[str, Any], path: str) -> str | None:
    page = _optional_int(item.get("page_number", item.get("source_page")))
    if page is None:
        return f"{path}.page_number 必须是正整数"
    expression = item.get("expression", item.get("text", item.get("latex")))
    if not isinstance(expression, str) or not expression.strip():
        return f"{path}.expression/text/latex 必须是非空字符串"
    identifier = item.get("formula_id", item.get("id"))
    if identifier is not None and (not isinstance(identifier, str) or not identifier.strip()):
        return f"{path}.formula_id/id 必须是非空字符串"
    return None


def _table_schema_error(item: Mapping[str, Any], path: str) -> str | None:
    cells, cell_error = _table_cells_checked(item, path=path)
    if cell_error:
        return cell_error
    if "text_matrix" in item and "cells" in item:
        structure_only = dict(item)
        structure_only.pop("text_matrix", None)
        structured_cells, structured_error = _table_cells_checked(
            structure_only,
            path=path,
        )
        if structured_error:
            return structured_error
        if structured_cells != cells:
            return f"{path}.text_matrix 与 cells/cell_reference_matrix 不一致"
    explicit_kind = item.get("kind", item.get("complexity"))
    if explicit_kind is not None and str(explicit_kind).casefold() not in {
        "simple",
        "complex",
    }:
        return f"{path}.kind/complexity 必须是 simple 或 complex"
    if "header_row_count" in item and _nonnegative_int(item.get("header_row_count")) is None:
        return f"{path}.header_row_count 必须是非负整数"
    if "source_pages" in item:
        pages = item.get("source_pages")
        if not _is_non_string_sequence(pages) or any(
            _optional_int(page) is None for page in pages
        ):
            return f"{path}.source_pages 必须是正整数数组"
    if "source_page_range" in item:
        page_range = item.get("source_page_range")
        if not isinstance(page_range, Mapping):
            return f"{path}.source_page_range 必须是对象"
        start = _optional_int(page_range.get("start"))
        end = _optional_int(page_range.get("end"))
        if (start is None) != (end is None) or (
            start is not None and end is not None and end < start
        ):
            return f"{path}.source_page_range 必须是有效页码范围"
    for key in ("header_levels", "headers", "header_hierarchy"):
        if key in item and not _valid_nested_array(item.get(key)):
            return f"{path}.{key} 必须是二维数组"
    if "part_table_ids" in item:
        part_ids = item.get("part_table_ids")
        if not _is_non_string_sequence(part_ids) or any(
            not isinstance(part_id, str) or not part_id.strip() for part_id in part_ids
        ):
            return f"{path}.part_table_ids 必须是非空字符串数组"
    return None


def _visual_schema_error(item: Mapping[str, Any], path: str) -> str | None:
    if _optional_int(item.get("page_number", item.get("source_page"))) is None:
        return f"{path}.page_number/source_page 必须是正整数"

    atom_count = 0
    dimensions = (
        ("entities", "nodes"),
        ("values", "key_values"),
        ("relations", "relationships", "arrows"),
        ("units",),
    )
    for aliases in dimensions:
        selected_key = next((key for key in aliases if key in item), None)
        if selected_key is None:
            continue
        raw_values = item.get(selected_key)
        if selected_key == "key_values" and isinstance(raw_values, Mapping):
            raw_values = [
                {"label": str(key), "value": value}
                for key, value in raw_values.items()
            ]
        if not _is_non_string_sequence(raw_values):
            return f"{path}.{selected_key} 必须是数组"
        for index, value in enumerate(raw_values):
            value_path = f"{path}.{selected_key}[{index}]"
            if selected_key in {"relations", "relationships", "arrows"}:
                if not isinstance(value, Mapping):
                    return f"{value_path} 必须是关系对象"
                source = str(value.get("source") or "").strip()
                target = str(value.get("target") or "").strip()
                relation = str(value.get("relation", value.get("type")) or "").strip()
                if not source or not target or not relation:
                    return f"{value_path} 必须包含 source、target 和 relation/type"
            elif selected_key == "key_values":
                if not isinstance(value, Mapping):
                    return f"{value_path} 必须是键值对象"
                label = str(value.get("label", value.get("key")) or "").strip()
                item_value = str(value.get("value") or "").strip()
                if not label or not item_value:
                    return f"{value_path} 必须包含非空 label/key 和 value"
            elif isinstance(value, Mapping) or _is_non_string_sequence(value):
                return f"{value_path} 必须是标量"
            elif not str(value or "").strip():
                return f"{value_path} 不能为空"
            atom_count += 1
    if atom_count == 0:
        return f"{path} 必须至少包含一个实体、数值、单位或关系"
    return None


def _citation_schema_error(item: Mapping[str, Any], path: str) -> str | None:
    file_name = item.get("file_name", item.get("filename"))
    if not isinstance(file_name, str) or not file_name.strip():
        return f"{path}.file_name/filename 必须是非空字符串"
    version = item.get("version", item.get("document_version"))
    if not isinstance(version, str) or not version.strip():
        return f"{path}.version/document_version 必须是非空字符串"
    raw_pages = item.get("page_numbers", item.get("source_pages"))
    if raw_pages is not None:
        if not _is_non_string_sequence(raw_pages) or not raw_pages:
            return f"{path}.page_numbers/source_pages 必须是非空正整数数组"
        if any(_optional_int(page) is None for page in raw_pages):
            return f"{path}.page_numbers/source_pages 必须是正整数数组"
    elif _optional_int(item.get("page_number", item.get("source_page"))) is None:
        return f"{path}.page_number/source_page 必须是正整数"
    return None


def _is_non_string_sequence(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray)


def _valid_nested_array(value: Any) -> bool:
    return _is_non_string_sequence(value) and all(
        _is_non_string_sequence(row) for row in value
    )


def _has_visual_dimensions(value: Mapping[str, Any]) -> bool:
    for key in (
        "entities",
        "nodes",
        "values",
        "key_values",
        "relations",
        "relationships",
        "arrows",
        "units",
    ):
        item = value.get(key)
        if isinstance(item, Mapping) and item:
            return True
        if _is_non_string_sequence(item) and item:
            return True
    return False


def _runtime_image_records(
    prediction: Mapping[str, Any],
) -> tuple[list[Mapping[str, Any]] | None, str, str | None]:
    if "chunks" in prediction:
        raw_records = prediction.get("chunks")
        source = "chunks"
    else:
        quality = _parse_quality_container(prediction)
        if "image_assets" not in quality:
            return None, "missing", None
        image_assets = quality.get("image_assets")
        source = "parse_quality.image_assets.assets"
        if image_assets is None:
            return [], source, None
        if not isinstance(image_assets, Mapping):
            return [], source, "prediction.parse_quality.image_assets 必须是对象或 null"
        raw_records = image_assets.get("assets", [])

    if not _is_non_string_sequence(raw_records):
        return [], source, f"{source} 必须是对象数组"
    records: list[Mapping[str, Any]] = []
    for index, record in enumerate(raw_records):
        if not isinstance(record, Mapping):
            return [], source, f"{source}[{index}] 必须是对象"
        records.append(record)
    return records, source, None


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _expected_page_sequence(gold: Mapping[str, Any]) -> tuple[list[int], bool]:
    if "pages" in gold:
        pages = _mapping_items(gold.get("pages"))
        numbers = [_optional_int(page.get("page_number")) for page in pages]
        if pages and all(number is not None for number in numbers):
            return [int(number) for number in numbers if number is not None], True
        if isinstance(gold.get("pages"), Sequence) and not gold.get("pages"):
            return [], True
    document = _as_mapping(gold.get("document"))
    page_count = _optional_int(document.get("source_page_count"))
    if page_count is None:
        page_count = _optional_int(gold.get("source_page_count"))
    return (list(range(1, page_count + 1)), True) if page_count is not None else ([], False)


def _predicted_page_sequence(prediction: Mapping[str, Any]) -> tuple[list[int], bool]:
    if "pages" in prediction:
        pages = _mapping_items(prediction.get("pages"))
        numbers = [_optional_int(page.get("page_number")) for page in pages]
        if pages and all(number is not None for number in numbers):
            return [int(number) for number in numbers if number is not None], True
        if isinstance(prediction.get("pages"), Sequence) and not prediction.get("pages"):
            return [], True
    if "page_numbers" in prediction:
        raw = prediction.get("page_numbers")
        if isinstance(raw, Sequence) and not isinstance(raw, str | bytes | bytearray):
            numbers = [_optional_int(value) for value in raw]
            if all(number is not None for number in numbers):
                return [int(number) for number in numbers if number is not None], True
    quality = _parse_quality_container(prediction)
    page_quality = _as_mapping(quality.get("page_quality"))
    raw_markers = page_quality.get("page_markers")
    if _is_non_string_sequence(raw_markers):
        markers = [_optional_int(value) for value in raw_markers]
        if all(number is not None for number in markers):
            return [int(number) for number in markers if number is not None], True
    per_page = page_quality.get("per_page")
    if _is_non_string_sequence(per_page) and all(
        isinstance(page, Mapping) for page in per_page
    ):
        numbers = [
            _optional_int(page.get("page_number"))
            for page in per_page
            if isinstance(page, Mapping)
        ]
        if all(number is not None for number in numbers):
            return [int(number) for number in numbers if number is not None], True
    return [], False


def _gold_scan_pages(gold: Mapping[str, Any]) -> tuple[set[int], bool]:
    if "scan_pages" in gold:
        raw = gold.get("scan_pages")
        if isinstance(raw, Sequence) and not isinstance(raw, str | bytes | bytearray):
            return {number for value in raw if (number := _optional_int(value)) is not None}, True
    if "pages" in gold:
        pages = _mapping_items(gold.get("pages"))
        annotated = [page for page in pages if "is_scanned" in page]
        if annotated:
            return {
                number
                for page in annotated
                if page.get("is_scanned") is True
                and (number := _optional_int(page.get("page_number"))) is not None
            }, True
    return set(), False


def _predicted_ocr_pages(prediction: Mapping[str, Any]) -> tuple[set[int], bool]:
    if "ocr_pages" in prediction:
        raw = prediction.get("ocr_pages")
        if isinstance(raw, Sequence) and not isinstance(raw, str | bytes | bytearray):
            return {number for value in raw if (number := _optional_int(value)) is not None}, True
    pages = _mapping_items(prediction.get("pages"))
    annotated = [page for page in pages if "ocr_completed" in page or "ocr" in page]
    if annotated:
        completed: set[int] = set()
        for page in annotated:
            ocr = page.get("ocr")
            is_completed = page.get("ocr_completed") is True
            if isinstance(ocr, Mapping):
                is_completed = is_completed or ocr.get("completed") is True
            number = _optional_int(page.get("page_number"))
            if is_completed and number is not None:
                completed.add(number)
        return completed, True
    quality = _parse_quality_container(prediction)
    page_quality = _as_mapping(quality.get("page_quality"))
    per_page = page_quality.get("per_page")
    if _is_non_string_sequence(per_page):
        completed = {
            number
            for page in per_page
            if isinstance(page, Mapping)
            and page.get("ocr_applied") is True
            and page.get("low_confidence") is not True
            and (number := _optional_int(page.get("page_number"))) is not None
        }
        if any(
            isinstance(page, Mapping) and "ocr_applied" in page for page in per_page
        ):
            return completed, True
    fallback = _as_mapping(quality.get("fallback"))
    if "ocr_results" in fallback:
        raw_results = fallback.get("ocr_results")
        if isinstance(raw_results, Mapping):
            raw_low_confidence = quality.get("low_confidence_pages", [])
            low_confidence_pages = {
                number
                for value in (
                    raw_low_confidence
                    if _is_non_string_sequence(raw_low_confidence)
                    else []
                )
                if (number := _optional_int(value)) is not None
            }
            completed = {
                number
                for key, value in raw_results.items()
                if (number := _optional_int(key)) is not None
                and isinstance(value, Mapping)
                and value.get("confidence") is not None
                and number not in low_confidence_pages
            }
            return completed, True
    return set(), False


def _page_texts(
    bundle: Mapping[str, Any],
    *,
    gold_mode: bool,
) -> tuple[dict[int, str], bool]:
    if "pages" not in bundle:
        return {}, False
    pages = _mapping_items(bundle.get("pages"))
    texts: dict[int, str] = {}
    had_text_field = False
    for page in pages:
        if gold_mode and page.get("include_text_accuracy") is False:
            continue
        number = _optional_int(page.get("page_number"))
        if number is None:
            continue
        for key in ("body_text", "text", "markdown"):
            if key in page and isinstance(page.get(key), str):
                texts[number] = str(page[key])
                had_text_field = True
                break
    return texts, had_text_field


def _normalize_body_text(value: str) -> str:
    text = unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n"))
    text = _PAGE_MARKER_RE.sub("", text)
    text = _MARKDOWN_IMAGE_RE.sub("", text)
    text = _HTML_IMAGE_RE.sub("", text)
    return "".join(text.split())


def _normalize_cell(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(unicodedata.normalize("NFC", str(value)).split())


def _levenshtein_distance(left: str, right: str) -> int:
    """Myers bit-vector Levenshtein distance, efficient for page-sized strings."""

    if left == right:
        return 0
    if not left:
        return len(right)
    if not right:
        return len(left)
    if len(left) > len(right):
        left, right = right, left
    pattern_length = len(left)
    high_bit = 1 << (pattern_length - 1)
    character_masks: dict[str, int] = {}
    for index, character in enumerate(left):
        character_masks[character] = character_masks.get(character, 0) | (1 << index)
    positive = ~0
    negative = 0
    score = pattern_length
    for character in right:
        equal = character_masks.get(character, 0)
        vertical = equal | negative
        horizontal = (((equal & positive) + positive) ^ positive) | equal
        positive_horizontal = negative | ~(horizontal | positive)
        negative_horizontal = positive & horizontal
        if positive_horizontal & high_bit:
            score += 1
        elif negative_horizontal & high_bit:
            score -= 1
        positive_horizontal = (positive_horizontal << 1) | 1
        negative_horizontal <<= 1
        positive = negative_horizontal | ~(vertical | positive_horizontal)
        negative = positive_horizontal & vertical
    return score


def _validated_critical_token_entries(
    value: Any,
    *,
    path: str,
) -> tuple[list[Mapping[str, Any]], str | None]:
    if isinstance(value, Mapping):
        entries: list[Mapping[str, Any]] = []
        for category, tokens in value.items():
            if not _is_non_string_sequence(tokens):
                return [], f"{path}.{category} 必须是标量数组"
            for index, token in enumerate(tokens):
                if isinstance(token, Mapping) or _is_non_string_sequence(token):
                    return [], f"{path}.{category}[{index}] 必须是标量"
                normalized = unicodedata.normalize("NFC", str(token or "")).strip()
                if not normalized:
                    return [], f"{path}.{category}[{index}] 不能为空"
                entries.append({"category": str(category), "value": normalized})
        return entries, None
    if not _is_non_string_sequence(value):
        return [], f"{path} 必须是对象数组或分类对象"

    entries = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            return [], f"{path}[{index}] 必须是对象"
        if "value" not in item:
            return [], f"{path}[{index}].value 缺失"
        token = item.get("value")
        if isinstance(token, Mapping) or _is_non_string_sequence(token):
            return [], f"{path}[{index}].value 必须是标量"
        normalized = unicodedata.normalize("NFC", str(token or "")).strip()
        if not normalized:
            return [], f"{path}[{index}].value 不能为空"
        page_number = item.get("page_number")
        if page_number is not None and _optional_int(page_number) is None:
            return [], f"{path}[{index}].page_number 必须是正整数"
        category = item.get("category", "unspecified")
        if not isinstance(category, str) or not category.strip():
            return [], f"{path}[{index}].category 必须是非空字符串"
        normalized_item = dict(item)
        normalized_item["category"] = category.strip()
        normalized_item["value"] = normalized
        entries.append(normalized_item)
    return entries, None


def _critical_text_occurrences(
    signatures: set[tuple[str, str, int | None]],
    page_texts: Mapping[int, str],
) -> Counter[tuple[str, str, int | None]]:
    occurrences: Counter[tuple[str, str, int | None]] = Counter()
    normalized_pages = {
        page_number: _critical_search_text(text)
        for page_number, text in page_texts.items()
    }
    for signature in signatures:
        _category, token, page_number = signature
        if not token:
            continue
        pattern = _critical_token_pattern(token)
        if page_number is None:
            occurrences[signature] = sum(
                len(pattern.findall(text)) for text in normalized_pages.values()
            )
        else:
            occurrences[signature] = len(
                pattern.findall(normalized_pages.get(page_number, ""))
            )
    return occurrences


def _critical_search_text(value: str) -> str:
    text = unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n"))
    text = _PAGE_MARKER_RE.sub("", text)
    text = _MARKDOWN_IMAGE_RE.sub("", text)
    return _HTML_IMAGE_RE.sub("", text)


def _critical_token_pattern(token: str) -> re.Pattern[str]:
    """Match exact critical tokens without numeric/unit substring false positives.

    Chinese prose commonly joins years and units directly (``2024年``), so Python's
    Unicode ``\\b`` is too strict.  ASCII identifier guards reject ``12024`` and
    ``12.500`` while still accepting those normal Chinese boundaries.
    """

    return re.compile(rf"(?<![0-9A-Za-z_]){re.escape(token)}(?![0-9A-Za-z_])")


def _critical_signature(entry: Mapping[str, Any]) -> tuple[str, str, int | None]:
    return (
        str(entry.get("category", "unspecified")),
        unicodedata.normalize("NFC", str(entry.get("value", ""))),
        _optional_int(entry.get("page_number")),
    )


def _formula_signature(item: Mapping[str, Any]) -> tuple[int | None, str, str]:
    expression = item.get("expression", item.get("text", item.get("latex", "")))
    return (
        _optional_int(item.get("page_number", item.get("source_page"))),
        str(item.get("formula_id", item.get("id", ""))),
        _normalize_cell(expression),
    )


def _table_kind(table: Mapping[str, Any]) -> str:
    explicit = table.get("kind", table.get("complexity"))
    if explicit is not None:
        return "simple" if str(explicit).casefold() == "simple" else "complex"
    if _is_cross_page_table(table):
        return "complex"
    if (_nonnegative_int(table.get("header_row_count")) or 0) > 1:
        return "complex"
    raw_cells = table.get("cells")
    if _is_non_string_sequence(raw_cells):
        for cell in raw_cells:
            if not isinstance(cell, Mapping):
                continue
            if (_optional_int(cell.get("row_span")) or 1) > 1 or (
                _optional_int(cell.get("column_span")) or 1
            ) > 1:
                return "complex"
    return "simple"


def _table_identifier(table: Mapping[str, Any], index: int) -> str:
    identifier = table.get("table_id", table.get("id"))
    return str(identifier) if identifier not in (None, "") else f"__position_{index}"


def _table_cells(table: Mapping[str, Any]) -> list[list[str]]:
    cells, _error = _table_cells_checked(table, path="table")
    return cells


def _table_cells_checked(
    table: Mapping[str, Any],
    *,
    path: str,
) -> tuple[list[list[str]], str | None]:
    if "text_matrix" in table:
        return _normalized_matrix(table.get("text_matrix"), f"{path}.text_matrix")

    raw = table.get("cells", table.get("rows"))
    if raw is None:
        return [], f"{path}.cells/rows/text_matrix 缺失"
    if not _is_non_string_sequence(raw):
        return [], f"{path}.cells/rows 必须是数组"
    if not raw:
        return [], None

    if all(_is_non_string_sequence(row) for row in raw):
        return _normalized_matrix(raw, f"{path}.cells")
    if not all(isinstance(cell, Mapping) for cell in raw):
        return [], f"{path}.cells 必须是二维数组或结构化单元格对象数组"

    structured_cells = [cell for cell in raw if isinstance(cell, Mapping)]
    by_id: dict[str, str] = {}
    positions: list[tuple[int, int, int, int, str]] = []
    for index, cell in enumerate(structured_cells):
        row_index = _nonnegative_int(cell.get("row_index"))
        column_index = _nonnegative_int(cell.get("column_index"))
        if row_index is None or column_index is None:
            return [], f"{path}.cells[{index}] row_index/column_index 必须是非负整数"
        text = cell.get("text")
        if text is None or isinstance(text, Mapping) or _is_non_string_sequence(text):
            return [], f"{path}.cells[{index}].text 必须是标量"
        row_span = _positive_int_or_default(cell.get("row_span"), 1)
        column_span = _positive_int_or_default(cell.get("column_span"), 1)
        if row_span is None or column_span is None:
            return [], f"{path}.cells[{index}] row_span/column_span 必须是正整数"
        normalized = _normalize_cell(text)
        cell_id = str(cell.get("cell_id") or "")
        if cell_id:
            if cell_id in by_id:
                return [], f"{path}.cells[{index}].cell_id 重复: {cell_id}"
            by_id[cell_id] = normalized
        positions.append((row_index, column_index, row_span, column_span, normalized))

    if "cell_reference_matrix" in table:
        raw_matrix = table.get("cell_reference_matrix")
        if not _is_non_string_sequence(raw_matrix) or not all(
            _is_non_string_sequence(row) for row in raw_matrix
        ):
            return [], f"{path}.cell_reference_matrix 必须是二维数组"
        matrix: list[list[str]] = []
        for row_index, row in enumerate(raw_matrix):
            normalized_row: list[str] = []
            for column_index, reference in enumerate(row):
                if reference is None:
                    normalized_row.append("")
                    continue
                if not isinstance(reference, str) or reference not in by_id:
                    return [], (
                        f"{path}.cell_reference_matrix[{row_index}]"
                        f"[{column_index}] 必须引用已存在的 cell_id 或 null"
                    )
                normalized_row.append(by_id[reference])
            matrix.append(normalized_row)
        return matrix, None

    declared_rows = _nonnegative_int(table.get("row_count"))
    declared_columns = _nonnegative_int(table.get("column_count"))
    row_count = max(
        declared_rows or 0,
        max((row + row_span for row, _col, row_span, _span, _text in positions), default=0),
    )
    column_count = max(
        declared_columns or 0,
        max((col + col_span for _row, col, _span, col_span, _text in positions), default=0),
    )
    matrix = [["" for _column in range(column_count)] for _row in range(row_count)]
    for row, column, row_span, column_span, text in positions:
        for target_row in range(row, min(row + row_span, row_count)):
            for target_column in range(column, min(column + column_span, column_count)):
                matrix[target_row][target_column] = text
    return matrix, None


def _normalized_matrix(value: Any, path: str) -> tuple[list[list[str]], str | None]:
    if not _is_non_string_sequence(value):
        return [], f"{path} 必须是二维数组"
    matrix: list[list[str]] = []
    column_count: int | None = None
    for row_index, row in enumerate(value):
        if not _is_non_string_sequence(row):
            return [], f"{path}[{row_index}] 必须是数组"
        normalized_row: list[str] = []
        for column_index, cell in enumerate(row):
            if isinstance(cell, Mapping) or _is_non_string_sequence(cell):
                return [], f"{path}[{row_index}][{column_index}] 必须是标量"
            normalized_row.append(_normalize_cell(cell))
        if column_count is None:
            column_count = len(normalized_row)
        elif len(normalized_row) != column_count:
            return [], f"{path}[{row_index}] 列数与前置行不一致"
        matrix.append(normalized_row)
    return matrix, None


def _nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _positive_int_or_default(value: Any, default: int) -> int | None:
    if value is None:
        return default
    return _optional_int(value)


def _flatten_cells(cells: Sequence[Sequence[str]]) -> dict[tuple[int, int], str]:
    return {
        (row_index, column_index): value
        for row_index, row in enumerate(cells)
        for column_index, value in enumerate(row)
    }


def _table_cell_accuracy(
    gold_tables: Sequence[Mapping[str, Any]],
    predicted_tables: Sequence[Mapping[str, Any]],
) -> tuple[float, dict[str, Any]]:
    predicted_by_id = {
        _table_identifier(table, index): table for index, table in enumerate(predicted_tables)
    }
    matches = 0
    denominator = 0
    table_details: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, gold_table in enumerate(gold_tables):
        identifier = _table_identifier(gold_table, index)
        seen.add(identifier)
        predicted_table = predicted_by_id.get(identifier)
        expected = _flatten_cells(_table_cells(gold_table))
        actual = _flatten_cells(_table_cells(predicted_table or {}))
        positions = expected.keys() | actual.keys()
        table_matches = sum(
            expected.get(position) == actual.get(position) for position in positions
        )
        matches += table_matches
        denominator += len(positions)
        table_details.append(
            {
                "table_id": identifier,
                "gold_cells": len(expected),
                "predicted_cells": len(actual),
                "matched_cells": table_matches,
                "accuracy": table_matches / len(positions) if positions else 1.0,
            }
        )
    for identifier, predicted_table in predicted_by_id.items():
        if identifier in seen:
            continue
        extra_count = len(_flatten_cells(_table_cells(predicted_table)))
        denominator += extra_count
        table_details.append(
            {
                "table_id": identifier,
                "gold_cells": 0,
                "predicted_cells": extra_count,
                "matched_cells": 0,
                "accuracy": 0.0 if extra_count else 1.0,
                "unexpected": True,
            }
        )
    accuracy = matches / denominator if denominator else 1.0
    return accuracy, {
        "gold_tables": len(gold_tables),
        "predicted_tables": len(predicted_tables),
        "matched_cells": matches,
        "evaluated_cells": denominator,
        "tables": table_details,
    }


def _table_page_range(table: Mapping[str, Any]) -> tuple[int, ...]:
    raw_pages = table.get("source_pages", table.get("pages"))
    if isinstance(raw_pages, Sequence) and not isinstance(raw_pages, str | bytes | bytearray):
        pages = tuple(
            number for value in raw_pages if (number := _optional_int(value)) is not None
        )
        if pages:
            return pages
    source_range = table.get("source_page_range")
    if isinstance(source_range, Mapping):
        start = _optional_int(source_range.get("start"))
        end = _optional_int(source_range.get("end"))
        if start is not None and end is not None and end >= start:
            return tuple(range(start, end + 1))
    start = _optional_int(table.get("source_page_start", table.get("page_start")))
    end = _optional_int(table.get("source_page_end", table.get("page_end")))
    if start is None:
        start = _optional_int(table.get("page_number"))
    if start is None:
        return ()
    end = end or start
    return tuple(range(start, end + 1))


def _is_cross_page_table(table: Mapping[str, Any]) -> bool:
    return len(_table_page_range(table)) > 1 or bool(
        table.get("continuation_of", table.get("continued_from"))
    )


def _cross_page_table_signature(table: Mapping[str, Any]) -> dict[str, Any]:
    signature: dict[str, Any] = {
        "source_pages": list(_table_page_range(table)),
    }
    header_key = next(
        (key for key in ("header_hierarchy", "header_levels", "headers") if key in table),
        None,
    )
    if header_key is not None:
        signature["header_levels"] = _json_canonical_value(table.get(header_key))
    if "part_table_ids" in table:
        signature["part_table_ids"] = _json_canonical_value(table.get("part_table_ids"))
    continuation_key = next(
        (key for key in ("continuation_of", "continued_from") if key in table),
        None,
    )
    if continuation_key is not None:
        signature["continuation_of"] = _normalize_cell(table.get(continuation_key))
    if "title" in table:
        signature["title"] = _normalize_cell(table.get("title"))
    return signature


def _visual_atomic_signatures(item: Mapping[str, Any]) -> tuple[str, ...]:
    page = _optional_int(item.get("page_number", item.get("source_page")))
    atoms: list[str] = []
    dimensions = {
        "entity": item.get("entities", item.get("nodes", [])),
        "value": item.get("values", item.get("key_values", [])),
        "relation": item.get("relations", item.get("relationships", item.get("arrows", []))),
        "unit": item.get("units", []),
    }
    for dimension, values in dimensions.items():
        if dimension == "value" and isinstance(values, Mapping):
            values = [
                {"label": str(key), "value": value}
                for key, value in sorted(values.items(), key=lambda pair: str(pair[0]))
            ]
        if not _is_non_string_sequence(values):
            continue
        for value in values:
            atoms.append(
                json.dumps(
                    [page, dimension, _canonical_visual_atom(dimension, value)],
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
    return tuple(atoms)


def _canonical_visual_atom(dimension: str, value: Any) -> Any:
    if dimension == "relation" and isinstance(value, Mapping):
        return {
            "source": _normalize_cell(value.get("source")),
            "relation": _normalize_cell(value.get("relation", value.get("type"))),
            "target": _normalize_cell(value.get("target")),
        }
    if dimension == "value" and isinstance(value, Mapping):
        return {
            "label": _normalize_cell(value.get("label", value.get("key"))),
            "value": _normalize_cell(value.get("value")),
        }
    return _json_canonical_value(value)


def _citation_signatures(item: Mapping[str, Any]) -> tuple[str, ...]:
    file_name = _normalize_cell(item.get("file_name", item.get("filename", "")))
    version = _normalize_cell(item.get("version", item.get("document_version", "")))
    raw_pages = item.get("page_numbers", item.get("source_pages"))
    if isinstance(raw_pages, Sequence) and not isinstance(raw_pages, str | bytes | bytearray):
        pages = [_optional_int(page) for page in raw_pages]
    else:
        pages = [_optional_int(item.get("page_number", item.get("source_page")))]
    return tuple(
        json.dumps([file_name, page, version], ensure_ascii=False, separators=(",", ":"))
        for page in pages
        if page is not None
    )


def _ensure_signature_sequence(value: Any) -> tuple[Any, ...]:
    if isinstance(value, tuple):
        return value
    if isinstance(value, list):
        return tuple(value)
    return (value,)


def _counter_difference(left: Counter[Any], right: Counter[Any]) -> list[Any]:
    values: list[Any] = []
    for item, count in (left - right).items():
        values.extend([_json_safe_signature(item)] * count)
    return values


def _json_safe_signature(value: Any) -> Any:
    if isinstance(value, tuple):
        return [_json_safe_signature(item) for item in value]
    return value


def _json_canonical_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_canonical_value(value[key]) for key in sorted(value)}
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_json_canonical_value(item) for item in value]
    if isinstance(value, str):
        return _normalize_cell(value)
    return value


def _is_retrievable_image_chunk(chunk: Mapping[str, Any]) -> bool:
    metadata = _chunk_metadata(chunk)
    image_asset = _as_mapping(metadata.get("image_asset"))
    kind = str(
        chunk.get(
            "chunk_type",
            chunk.get(
                "type",
                chunk.get(
                    "kind",
                    metadata.get("element_type", metadata.get("chunk_role", "")),
                ),
            ),
        )
    ).lower()
    if kind not in {"image", "figure", "chart", "diagram", "pdf_image"}:
        return False
    for container in (chunk, metadata, image_asset):
        if container.get("retrieval_eligible") is False:
            return False
        if container.get("retrieval_asset") is False:
            return False
        if container.get("indexed") is False or container.get("retrievable") is False:
            return False
        if container.get("suppress_retrieval") is True:
            return False
    return True


def _has_meaningful_visual_description(chunk: Mapping[str, Any]) -> bool:
    metadata = _chunk_metadata(chunk)
    image_asset = _as_mapping(metadata.get("image_asset"))
    candidates = (
        chunk.get("visual_description"),
        chunk.get("retrieval_text"),
        chunk.get("description"),
        chunk.get("alt_text"),
        metadata.get("visual_description"),
        metadata.get("description"),
        metadata.get("alt_text"),
        image_asset.get("visual_description"),
        image_asset.get("retrieval_text"),
        chunk.get("content"),
        chunk.get("text"),
    )
    for candidate in candidates:
        if not isinstance(candidate, str):
            continue
        text = _MARKDOWN_IMAGE_RE.sub("", candidate)
        text = _HTML_IMAGE_RE.sub("", text)
        text = "".join(text.split())
        if len(text) >= 8 and not _PLACEHOLDER_IMAGE_RE.search(text):
            return True
    return False


def _chunk_metadata(chunk: Mapping[str, Any]) -> Mapping[str, Any]:
    for key in ("metadata", "structure_metadata", "structure"):
        value = chunk.get(key)
        if isinstance(value, Mapping):
            return value
    return {}


def _retrieval_cases(
    gold: Mapping[str, Any],
    prediction: Mapping[str, Any],
) -> tuple[list[tuple[str, list[Mapping[str, Any]], set[str]]], bool, str]:
    if "retrieval_cases" in prediction:
        prediction_cases = _mapping_items(prediction.get("retrieval_cases"))
        gold_cases = {
            str(case.get("id", case.get("case_id", index))): case
            for index, case in enumerate(_mapping_items(gold.get("retrieval_cases")))
        }
        cases: list[tuple[str, list[Mapping[str, Any]], set[str]]] = []
        for index, prediction_case in enumerate(prediction_cases):
            case_id = str(prediction_case.get("id", prediction_case.get("case_id", index)))
            gold_case = gold_cases.get(case_id, {})
            results = _mapping_items(
                prediction_case.get("results", prediction_case.get("recall_results"))
            )
            explicit_flags = all("is_noise" in result for result in results[:5])
            if "noise_chunk_ids" not in gold_case and not explicit_flags:
                return [], False, f"检索用例 {case_id} 缺少 noise_chunk_ids 或 is_noise 标注"
            noise_ids = {str(value) for value in gold_case.get("noise_chunk_ids", [])}
            cases.append((case_id, results, noise_ids))
        return cases, True, ""

    if "recall_results" not in prediction:
        return [], False, "预测结果缺少 recall_results 或 retrieval_cases"
    results = _mapping_items(prediction.get("recall_results"))
    explicit_flags = all("is_noise" in result for result in results[:5])
    if "noise_chunk_ids" not in gold and not explicit_flags:
        return [], False, "金标缺少 noise_chunk_ids，且 Top5 结果未标注 is_noise"
    noise_ids = {str(value) for value in gold.get("noise_chunk_ids", [])}
    return [("default", results, noise_ids)], True, ""


def _is_noise_result(result: Mapping[str, Any], noise_ids: set[str]) -> bool:
    if "is_noise" in result:
        return result.get("is_noise") is True
    identifier = result.get("chunk_id", result.get("id"))
    if identifier is not None and str(identifier) in noise_ids:
        return True
    category = str(result.get("noise_category", result.get("category", ""))).lower()
    return category in _NOISE_CATEGORIES
