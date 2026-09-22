"""Run OpenDataLoader ``default``/``cluster`` table strategy A/B evaluations.

The command deliberately goes through this repository's ``PdfParser`` and PDF
backend registry.  It does not call a second LinkRag service or import LinkRag as
an SDK.  OpenDataLoader itself remains isolated by the backend's subprocess
runner, including its process-group timeout and resource guards.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.rag.core.parser.pdf.table_structure import OdlTableAbEvaluator  # noqa: E402
from app.rag.core.parser.providers.pdf_parser import PdfParser  # noqa: E402


class StrategyParser(Protocol):
    """Small protocol that keeps the runner unit-testable without real ODL."""

    def parse(self, source: Path | None) -> str: ...

    def extract_metadata(self) -> dict: ...


TableStrategy = Literal["default", "cluster"]
ParserFactory = Callable[[TableStrategy, bool, float], StrategyParser]

_METADATA_KEYS = {
    "pages_or_length",
    "pdf_parser_backend",
    "pdf_parser_attempts",
    "pdf_preflight",
    "pdf_reliability_limits",
    "opendataloader_process",
    "opendataloader_runtime_health",
    "opendataloader_java_version",
    "opendataloader_markdown_file",
    "opendataloader_image_count",
    "opendataloader_table_method",
    "opendataloader_markdown_with_html",
    "opendataloader_error_code",
    "opendataloader_retryable",
}


class UnexpectedPdfBackendError(RuntimeError):
    """Raised when deployment fallback configuration replaced OpenDataLoader."""

    error_code = "ODL_AB_BACKEND_MISMATCH"
    retryable = False


def collect_pdf_files(inputs: Iterable[str | Path], *, recursive: bool = True) -> list[Path]:
    """Resolve, de-duplicate and stably sort PDF files from files/directories."""

    resolved: dict[Path, None] = {}
    for raw_input in inputs:
        path = Path(raw_input).expanduser().resolve()
        if not path.exists():
            raise ValueError(f"输入路径不存在: {path}")
        if path.is_file():
            if path.suffix.lower() != ".pdf":
                raise ValueError(f"输入文件不是 PDF: {path}")
            resolved[path] = None
            continue
        if not path.is_dir():
            raise ValueError(f"输入路径不是普通文件或目录: {path}")
        candidates = path.rglob("*") if recursive else path.iterdir()
        for candidate in candidates:
            if candidate.is_file() and candidate.suffix.lower() == ".pdf":
                resolved[candidate.resolve()] = None

    files = sorted(resolved, key=lambda item: (str(item).casefold(), str(item)))
    if not files:
        raise ValueError("输入路径中未找到 PDF 文件")
    return files


class OpenDataLoaderTableAbRunner:
    """Parse every PDF twice and compare structured table extraction metrics."""

    def __init__(
        self,
        *,
        parser_factory: ParserFactory | None = None,
        evaluator: OdlTableAbEvaluator | None = None,
    ) -> None:
        self._parser_factory = parser_factory or self._create_parser
        self._evaluator = evaluator or OdlTableAbEvaluator()

    def run(
        self,
        files: Sequence[Path],
        *,
        timeout_seconds: float,
        default_markdown_with_html: bool = False,
        cluster_markdown_with_html: bool = False,
    ) -> dict[str, object]:
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds 必须是大于 0 的有限数")
        if not files:
            raise ValueError("至少需要一个 PDF 文件")

        documents: list[dict[str, object]] = []
        for source in files:
            documents.append(
                self._evaluate_file(
                    source,
                    timeout_seconds=timeout_seconds,
                    default_markdown_with_html=default_markdown_with_html,
                    cluster_markdown_with_html=cluster_markdown_with_html,
                )
            )

        status_counts = Counter(str(item["status"]) for item in documents)
        recommendations = Counter(
            str(item["comparison"]["recommendation"])
            for item in documents
            if isinstance(item.get("comparison"), dict)
        )
        strategy_failures = sum(
            int(not bool(item[strategy]["success"]))
            for item in documents
            for strategy in ("default", "cluster")
        )
        return {
            "schema_version": 1,
            "generated_at": datetime.now(UTC).isoformat(),
            "options": {
                "backend": "opendataloader",
                "timeout_seconds": timeout_seconds,
                "default_markdown_with_html": default_markdown_with_html,
                "cluster_markdown_with_html": cluster_markdown_with_html,
            },
            "summary": {
                "document_count": len(documents),
                "compared_count": status_counts["compared"],
                "partial_count": status_counts["partial"],
                "failed_count": status_counts["failed"],
                "evaluation_failed_count": status_counts["evaluation_failed"],
                "strategy_failure_count": strategy_failures,
                "recommendations": dict(sorted(recommendations.items())),
            },
            "documents": documents,
        }

    def _evaluate_file(
        self,
        source: Path,
        *,
        timeout_seconds: float,
        default_markdown_with_html: bool,
        cluster_markdown_with_html: bool,
    ) -> dict[str, object]:
        default_result, default_markdown = self._run_strategy(
            source,
            strategy="default",
            markdown_with_html=default_markdown_with_html,
            timeout_seconds=timeout_seconds,
        )
        cluster_result, cluster_markdown = self._run_strategy(
            source,
            strategy="cluster",
            markdown_with_html=cluster_markdown_with_html,
            timeout_seconds=timeout_seconds,
        )
        document: dict[str, object] = {
            "path": str(source.resolve()),
            "default": default_result,
            "cluster": cluster_result,
            "comparison": None,
        }
        successes = int(default_markdown is not None) + int(cluster_markdown is not None)
        if successes == 0:
            document["status"] = "failed"
            return document
        if successes == 1:
            document["status"] = "partial"
            return document

        try:
            comparison = self._evaluator.evaluate(
                default_markdown=default_markdown or "",
                cluster_markdown=cluster_markdown or "",
                default_markdown_with_html=default_markdown_with_html,
                cluster_markdown_with_html=cluster_markdown_with_html,
            )
        except Exception as exc:  # a malformed output must remain visible in the JSON report
            document["status"] = "evaluation_failed"
            document["evaluation_error"] = _exception_payload(exc)
            return document

        document["status"] = "compared"
        document["comparison"] = comparison.to_dict()
        return document

    def _run_strategy(
        self,
        source: Path,
        *,
        strategy: TableStrategy,
        markdown_with_html: bool,
        timeout_seconds: float,
    ) -> tuple[dict[str, object], str | None]:
        parser: StrategyParser | None = None
        started_at = time.monotonic()
        try:
            parser = self._parser_factory(strategy, markdown_with_html, timeout_seconds)
            markdown = parser.parse(source)
            metadata = _selected_metadata(parser.extract_metadata())
            selected_backend = metadata.get("pdf_parser_backend")
            if selected_backend not in {None, "opendataloader"}:
                raise UnexpectedPdfBackendError(
                    f"A/B 评估要求 opendataloader，实际降级到 {selected_backend}"
                )
            return (
                {
                    "success": True,
                    "table_method": strategy,
                    "markdown_with_html": markdown_with_html,
                    "duration_ms": int((time.monotonic() - started_at) * 1000),
                    "markdown_chars": len(markdown),
                    "metadata": metadata,
                    "error": None,
                },
                markdown,
            )
        except Exception as exc:
            metadata = _selected_metadata(parser.extract_metadata()) if parser is not None else {}
            return (
                {
                    "success": False,
                    "table_method": strategy,
                    "markdown_with_html": markdown_with_html,
                    "duration_ms": int((time.monotonic() - started_at) * 1000),
                    "markdown_chars": 0,
                    "metadata": metadata,
                    "error": _exception_payload(exc),
                },
                None,
            )

    @staticmethod
    def _create_parser(
        strategy: TableStrategy,
        markdown_with_html: bool,
        timeout_seconds: float,
    ) -> PdfParser:
        # Explicit backend selection keeps this path inside the repository's ODL
        # implementation.  ``_run_strategy`` additionally rejects any deployment
        # fallback result, so Naive output can never enter the comparison.
        return PdfParser(
            backend="opendataloader",
            opendataloader_table_method=strategy,
            opendataloader_markdown_with_html=markdown_with_html,
            opendataloader_timeout_seconds=timeout_seconds,
        )


def _selected_metadata(metadata: dict[str, Any] | None) -> dict[str, object]:
    return {
        key: _json_safe(value)
        for key, value in (metadata or {}).items()
        if key in _METADATA_KEYS
    }


def _json_safe(value: Any) -> object:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return f"<{type(value).__name__}>"


def _exception_payload(exc: Exception) -> dict[str, object]:
    payload: dict[str, object] = {
        "type": type(exc).__name__,
        "message": str(exc),
        "error_code": getattr(exc, "error_code", None),
        "retryable": getattr(exc, "retryable", None),
    }
    details = getattr(exc, "details", None)
    if details:
        payload["details"] = _json_safe(details)
    return payload


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="使用本仓库 PDF 解析链执行 OpenDataLoader 表格策略 A/B 评估",
    )
    parser.add_argument("inputs", nargs="+", help="PDF 文件或包含 PDF 的目录")
    parser.add_argument(
        "--recursive",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="是否递归扫描目录（默认：是）",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=600.0,
        help="每个策略的 OpenDataLoader 独立进程超时（默认：600）",
    )
    parser.add_argument(
        "--markdown-with-html",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="同时为 default/cluster 启用 HTML 表格输出",
    )
    parser.add_argument(
        "--default-markdown-with-html",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="仅覆盖 default 策略的 HTML 表格开关",
    )
    parser.add_argument(
        "--cluster-markdown-with-html",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="仅覆盖 cluster 策略的 HTML 表格开关",
    )
    parser.add_argument("-o", "--output", type=Path, help="JSON 报告路径；缺省时输出到 stdout")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    argument_parser = build_argument_parser()
    arguments = argument_parser.parse_args(argv)
    try:
        files = collect_pdf_files(arguments.inputs, recursive=arguments.recursive)
        default_html = (
            arguments.markdown_with_html
            if arguments.default_markdown_with_html is None
            else arguments.default_markdown_with_html
        )
        cluster_html = (
            arguments.markdown_with_html
            if arguments.cluster_markdown_with_html is None
            else arguments.cluster_markdown_with_html
        )
        report = OpenDataLoaderTableAbRunner().run(
            files,
            timeout_seconds=arguments.timeout_seconds,
            default_markdown_with_html=default_html,
            cluster_markdown_with_html=cluster_html,
        )
    except ValueError as exc:
        argument_parser.error(str(exc))

    serialized = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if arguments.output is None:
        print(serialized)
    else:
        output_path = arguments.output.expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(f"{serialized}\n", encoding="utf-8")
        print(str(output_path), file=sys.stderr)

    summary = report["summary"]
    assert isinstance(summary, dict)
    has_failure = int(summary["strategy_failure_count"]) > 0 or int(
        summary["evaluation_failed_count"]
    ) > 0
    return 1 if has_failure else 0


if __name__ == "__main__":
    raise SystemExit(main())
