from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pymupdf
import pytest

from app.rag.core.parser.pdf.backends.opendataloader_backend import OpenDataLoaderBackend
from app.rag.core.parser.pdf.reliability import (
    OpenDataLoaderRunReport,
    OpenDataLoaderRuntimeError,
    OpenDataLoaderRuntimeHealth,
    OutputDirectoryStats,
)


class _ReadyHealthChecker:
    def check(self) -> OpenDataLoaderRuntimeHealth:
        return OpenDataLoaderRuntimeHealth(
            ready=True,
            package_available=True,
            java_available=True,
            java_major_version=17,
            package_version="2.5.0",
            java_version='openjdk version "17.0.12"',
            error_code=None,
            message="ready",
        )


class _TooOldJavaHealthChecker:
    def check(self) -> OpenDataLoaderRuntimeHealth:
        return OpenDataLoaderRuntimeHealth(
            ready=False,
            package_available=True,
            java_available=True,
            java_major_version=8,
            package_version="2.5.0",
            java_version='openjdk version "1.8.0_402"',
            error_code="ODL_JAVA_VERSION_UNSUPPORTED",
            message="当前 Java 版本为 8，OpenDataLoader 要求 Java 11+",
        )


class _FakeProcessRunner:
    def __init__(self) -> None:
        self.captured: dict[str, object] = {}

    def run(self, **kwargs: object) -> OpenDataLoaderRunReport:
        self.captured.update(kwargs)
        output_dir = Path(str(kwargs["output_dir"]))
        image_dir = Path(str(kwargs["image_dir"]))
        image_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "energy-accounting.md").write_text(
            "<!-- ODL_PAGE:2 -->\n"
            "# 能碳核算\n\n![排放边界](images/page-2-boundary.png)\n"
            "<!-- ODL_PAGE:12 -->\n"
            "![表格](images/tables/page_12_table.jpeg)\n",
            encoding="utf-8",
        )
        (image_dir / "page-2-boundary.png").write_bytes(b"png-binary")
        nested = image_dir / "tables"
        nested.mkdir()
        (nested / "page_12_table.jpeg").write_bytes(b"jpeg-binary")
        return OpenDataLoaderRunReport(
            duration_ms=25,
            output=OutputDirectoryStats(
                file_count=3,
                total_bytes=100,
                image_count=2,
                largest_image_bytes=11,
                total_image_bytes=22,
            ),
        )


def _write_valid_pdf(path: Path) -> None:
    document = pymupdf.open()
    document.new_page().insert_text((72, 72), "energy accounting")
    document.save(path)
    document.close()


def test_parse_uses_exact_opendataloader_contract_and_keeps_markdown_assets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "energy-accounting.pdf"
    _write_valid_pdf(source)
    process_runner = _FakeProcessRunner()
    backend = OpenDataLoaderBackend(
        process_runner=process_runner,
        health_checker=_ReadyHealthChecker(),
    )
    markdown, assets = backend.parse(source)

    captured = process_runner.captured
    output_dir = Path(str(captured["output_dir"]))
    assert captured["source"] == source
    assert captured["page_marker_template"] == "<!-- ODL_PAGE:%page-number% -->"
    assert captured["table_method"] == "default"
    assert captured["markdown_with_html"] is False
    assert Path(str(captured["image_dir"])) == output_dir / "images"
    assert "# 能碳核算" in markdown
    assert [asset.source_path for asset in assets] == [
        "images/page-2-boundary.png",
        "images/tables/page_12_table.jpeg",
    ]
    assert [asset.content for asset in assets] == [b"png-binary", b"jpeg-binary"]
    assert [(asset.page_number, asset.index, asset.ext) for asset in assets] == [
        (2, 1, "png"),
        (12, 2, "jpeg"),
    ]
    assert backend.metadata["opendataloader_markdown_file"] == "energy-accounting.md"
    assert backend.metadata["opendataloader_image_count"] == 2
    assert backend.metadata["opendataloader_image_page_map"] == {
        "images/page-2-boundary.png": [2],
        "images/tables/page_12_table.jpeg": [12],
    }
    assert backend.metadata["opendataloader_process"]["duration_ms"] == 25
    assert backend.metadata["pdf_preflight"]["page_count"] == 1
    assert source.exists(), "backend must reuse, not move or delete, the pipeline source file"


@pytest.mark.parametrize(
    ("version_output", "expected"),
    [
        ('java version "1.8.0_402"', 8),
        ('openjdk version "11.0.24" 2024-07-16', 11),
        ('openjdk version "17.0.12" 2024-07-16', 17),
        ('openjdk version "21" 2023-09-19', 21),
        ("unexpected java output", None),
    ],
)
def test_java_major_version_parsing(version_output: str, expected: int | None) -> None:
    assert OpenDataLoaderBackend._parse_java_major_version(version_output) == expected


def test_parse_stops_before_convert_when_java_is_too_old(
    tmp_path: Path,
) -> None:
    source = tmp_path / "legacy-java.pdf"
    _write_valid_pdf(source)
    process_runner = _FakeProcessRunner()
    backend = OpenDataLoaderBackend(
        process_runner=process_runner,
        health_checker=_TooOldJavaHealthChecker(),
    )

    with pytest.raises(OpenDataLoaderRuntimeError) as caught:
        backend.parse(source)

    assert caught.value.error_code == "ODL_JAVA_VERSION_UNSUPPORTED"
    assert caught.value.retryable is False
    assert process_runner.captured == {}
    assert backend.metadata["opendataloader_java_version"] == 'openjdk version "1.8.0_402"'
    assert "Java 版本为 8" in backend.metadata["opendataloader_backend_error"]


def test_parse_passes_cluster_and_markdown_html_options(tmp_path: Path) -> None:
    source = tmp_path / "cluster.pdf"
    _write_valid_pdf(source)
    process_runner = _FakeProcessRunner()
    backend = OpenDataLoaderBackend(
        process_runner=process_runner,
        health_checker=_ReadyHealthChecker(),
    )

    backend.parse(
        source,
        SimpleNamespace(
            opendataloader_table_method="cluster",
            opendataloader_markdown_with_html=True,
        ),
    )

    assert process_runner.captured["table_method"] == "cluster"
    assert process_runner.captured["markdown_with_html"] is True
    assert backend.metadata["opendataloader_table_method"] == "cluster"
    assert backend.metadata["opendataloader_markdown_with_html"] is True


def test_unreferenced_image_filename_never_supplies_page_number(tmp_path: Path) -> None:
    output_dir = tmp_path / "output"
    image_dir = output_dir / "images"
    image_dir.mkdir(parents=True)
    (image_dir / "imageFile88.png").write_bytes(b"png")
    backend = OpenDataLoaderBackend(
        process_runner=_FakeProcessRunner(),
        health_checker=_ReadyHealthChecker(),
    )

    assets = backend._collect_image_assets(
        output_dir,
        image_dir,
        "<!-- ODL_PAGE:2 -->\n这里没有图片引用。",
    )

    assert len(assets) == 1
    assert assets[0].source_path == "images/imageFile88.png"
    assert assets[0].page_number is None


def test_image_page_map_retains_all_marker_pages_for_reused_markdown_and_html_refs() -> None:
    markdown = (
        "<!-- ODL_PAGE:1 -->\n"
        "![图表](./images/shared.png)\n"
        "<!-- ODL_PAGE:2 -->\n"
        '<img src="images/shared.png" alt="续图">\n'
        "<!-- ODL_PAGE:3 -->\n"
        "![图表再引用](images/shared.png)"
    )

    assert OpenDataLoaderBackend._image_page_map(markdown) == {
        "images/shared.png": (1, 2, 3)
    }
