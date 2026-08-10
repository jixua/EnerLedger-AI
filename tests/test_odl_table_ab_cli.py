from __future__ import annotations

from pathlib import Path

import pytest

import scripts.evaluate_odl_table_strategies as table_ab_cli
from app.rag.core.parser.pdf.models import PdfParseOptions
from app.rag.core.parser.pdf.registry import _create_opendataloader_backend
from scripts.evaluate_odl_table_strategies import (
    OpenDataLoaderTableAbRunner,
    TableStrategy,
    collect_pdf_files,
)


class FakeParser:
    def __init__(
        self,
        markdown: str | Exception,
        *,
        strategy: TableStrategy,
        selected_backend: str = "opendataloader",
    ) -> None:
        self._markdown = markdown
        self._metadata = {
            "pdf_parser_backend": selected_backend,
            "opendataloader_table_method": strategy,
            "ignored_secret_like_field": "must-not-leak",
        }

    def parse(self, _source: Path | None) -> str:
        if isinstance(self._markdown, Exception):
            raise self._markdown
        return self._markdown

    def extract_metadata(self) -> dict:
        return self._metadata


class FakeOdlFailure(RuntimeError):
    error_code = "ODL_CONVERT_FAILED"
    retryable = True


def test_collect_pdf_files_recurses_sorts_and_deduplicates(tmp_path: Path) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    first = tmp_path / "A.PDF"
    second = nested / "b.pdf"
    ignored = nested / "notes.txt"
    first.write_bytes(b"pdf")
    second.write_bytes(b"pdf")
    ignored.write_text("ignore", encoding="utf-8")

    files = collect_pdf_files([tmp_path, first])

    assert files == [first.resolve(), second.resolve()]
    assert collect_pdf_files([tmp_path], recursive=False) == [first.resolve()]


def test_runner_compares_both_strategies_and_propagates_options(tmp_path: Path) -> None:
    source = tmp_path / "sample.pdf"
    source.write_bytes(b"fake")
    calls: list[tuple[str, bool, float]] = []
    markdown_by_strategy = {
        "default": "<!-- ODL_PAGE:1 -->\n| A | B |\n| --- | --- |\n| 1 | 2 |",
        "cluster": (
            "<!-- ODL_PAGE:1 -->\n| A | B |\n| --- | --- |\n| 1 | 2 |\n| 3 | 4 |"
        ),
    }

    def factory(strategy: TableStrategy, html: bool, timeout: float) -> FakeParser:
        calls.append((strategy, html, timeout))
        return FakeParser(markdown_by_strategy[strategy], strategy=strategy)

    report = OpenDataLoaderTableAbRunner(parser_factory=factory).run(
        [source],
        timeout_seconds=17.5,
        default_markdown_with_html=False,
        cluster_markdown_with_html=True,
    )

    assert calls == [("default", False, 17.5), ("cluster", True, 17.5)]
    assert report["summary"] == {
        "document_count": 1,
        "compared_count": 1,
        "partial_count": 0,
        "failed_count": 0,
        "evaluation_failed_count": 0,
        "strategy_failure_count": 0,
        "recommendations": {"cluster": 1},
    }
    document = report["documents"][0]
    assert document["status"] == "compared"
    assert document["comparison"]["recommendation"] == "cluster"
    assert document["cluster"]["markdown_with_html"] is True
    assert "ignored_secret_like_field" not in document["default"]["metadata"]


def test_runner_keeps_strategy_failure_and_does_not_compare_partial_pair(
    tmp_path: Path,
) -> None:
    source = tmp_path / "sample.pdf"
    source.write_bytes(b"fake")

    def factory(strategy: TableStrategy, _html: bool, _timeout: float) -> FakeParser:
        if strategy == "cluster":
            return FakeParser(FakeOdlFailure("cluster failed"), strategy=strategy)
        return FakeParser("<!-- ODL_PAGE:1 -->\nno table", strategy=strategy)

    report = OpenDataLoaderTableAbRunner(parser_factory=factory).run(
        [source], timeout_seconds=10
    )

    document = report["documents"][0]
    assert document["status"] == "partial"
    assert document["comparison"] is None
    assert document["cluster"]["error"] == {
        "type": "FakeOdlFailure",
        "message": "cluster failed",
        "error_code": "ODL_CONVERT_FAILED",
        "retryable": True,
    }
    assert report["summary"]["strategy_failure_count"] == 1


def test_runner_rejects_silent_backend_fallback(tmp_path: Path) -> None:
    source = tmp_path / "sample.pdf"
    source.write_bytes(b"fake")

    def factory(strategy: TableStrategy, _html: bool, _timeout: float) -> FakeParser:
        return FakeParser("fallback markdown", strategy=strategy, selected_backend="naive")

    report = OpenDataLoaderTableAbRunner(parser_factory=factory).run(
        [source], timeout_seconds=10
    )

    assert report["summary"]["strategy_failure_count"] == 2
    assert report["documents"][0]["status"] == "failed"
    assert (
        report["documents"][0]["default"]["error"]["error_code"]
        == "ODL_AB_BACKEND_MISMATCH"
    )


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
def test_runner_rejects_invalid_timeout(timeout: float) -> None:
    with pytest.raises(ValueError, match="timeout_seconds"):
        OpenDataLoaderTableAbRunner().run(
            [Path("not-opened.pdf")], timeout_seconds=timeout
        )


def test_pdf_parser_option_overrides_odl_process_timeout() -> None:
    backend = _create_opendataloader_backend(
        PdfParseOptions(opendataloader_timeout_seconds=23.5)
    )

    assert backend._process_runner.timeout_seconds == 23.5


def test_runner_exposes_table_evaluation_exception_as_failure(tmp_path: Path) -> None:
    source = tmp_path / "sample.pdf"
    source.write_bytes(b"fake")

    class BrokenEvaluator:
        def evaluate(self, **_kwargs):
            raise ValueError("malformed table output")

    def factory(strategy: TableStrategy, _html: bool, _timeout: float) -> FakeParser:
        return FakeParser("<!-- ODL_PAGE:1 -->\n| A |\n| --- |\n| 1 |", strategy=strategy)

    report = OpenDataLoaderTableAbRunner(
        parser_factory=factory,
        evaluator=BrokenEvaluator(),  # type: ignore[arg-type]
    ).run([source], timeout_seconds=10)

    assert report["summary"]["strategy_failure_count"] == 0
    assert report["summary"]["evaluation_failed_count"] == 1
    assert report["documents"][0]["status"] == "evaluation_failed"


def test_cli_returns_nonzero_when_comparison_evaluation_failed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "sample.pdf"
    source.write_bytes(b"fake")

    class FakeRunner:
        def run(self, *_args, **_kwargs):
            return {
                "schema_version": 1,
                "summary": {
                    "document_count": 1,
                    "compared_count": 0,
                    "partial_count": 0,
                    "failed_count": 0,
                    "evaluation_failed_count": 1,
                    "strategy_failure_count": 0,
                    "recommendations": {},
                },
                "documents": [{"path": str(source), "status": "evaluation_failed"}],
            }

    monkeypatch.setattr(table_ab_cli, "OpenDataLoaderTableAbRunner", FakeRunner)

    assert table_ab_cli.main([str(source)]) == 1
