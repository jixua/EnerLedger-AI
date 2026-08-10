from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import app.rag.core.parser.pdf.backends.opendataloader_backend as backend_module
from app.rag.core.parser.pdf.backends.opendataloader_backend import OpenDataLoaderBackend


def test_parse_uses_exact_opendataloader_contract_and_keeps_markdown_assets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "energy-accounting.pdf"
    source.write_bytes(b"%PDF-1.7\nfixture")
    captured: dict[str, object] = {}

    def fake_convert(**kwargs: object) -> None:
        captured.update(kwargs)
        output_dir = Path(str(kwargs["output_dir"]))
        image_dir = Path(str(kwargs["image_dir"]))
        image_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "energy-accounting.md").write_text(
            "# 能碳核算\n\n![排放边界](images/page-2-boundary.png)\n",
            encoding="utf-8",
        )
        (image_dir / "page-2-boundary.png").write_bytes(b"png-binary")
        nested = image_dir / "tables"
        nested.mkdir()
        (nested / "page_12_table.jpeg").write_bytes(b"jpeg-binary")

    fake_module = SimpleNamespace(convert=fake_convert)

    def fake_import_module(name: str) -> SimpleNamespace:
        assert name == "opendataloader_pdf"
        return fake_module

    monkeypatch.setattr(backend_module.importlib, "import_module", fake_import_module)
    monkeypatch.setattr(OpenDataLoaderBackend, "_ensure_java_11_plus", lambda self: None)

    backend = OpenDataLoaderBackend()
    markdown, assets = backend.parse(source)

    assert captured == {
        "input_path": [str(source)],
        "output_dir": captured["output_dir"],
        "format": "markdown-with-images",
        "markdown_page_separator": "<!-- ODL_PAGE:%page-number% -->",
        "image_output": "external",
        "image_dir": captured["image_dir"],
        "quiet": True,
    }
    output_dir = Path(str(captured["output_dir"]))
    assert Path(str(captured["image_dir"])) == output_dir / "images"
    assert markdown == "# 能碳核算\n\n![排放边界](images/page-2-boundary.png)\n"
    assert [asset.source_path for asset in assets] == [
        "images/page-2-boundary.png",
        "images/tables/page_12_table.jpeg",
    ]
    assert [asset.content for asset in assets] == [b"png-binary", b"jpeg-binary"]
    assert [(asset.page_number, asset.index, asset.ext) for asset in assets] == [
        (2, 1, "png"),
        (12, 2, "jpeg"),
    ]
    assert backend.metadata == {
        "opendataloader_markdown_file": "energy-accounting.md",
        "opendataloader_image_count": 2,
    }
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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "legacy-java.pdf"
    source.write_bytes(b"%PDF-1.7\nfixture")

    def unexpected_convert(**_kwargs: object) -> None:
        pytest.fail("convert must not run when the Java version gate fails")

    monkeypatch.setattr(
        backend_module.importlib,
        "import_module",
        lambda name: SimpleNamespace(convert=unexpected_convert),
    )

    def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
        assert command == ["java", "-version"]
        assert kwargs == {"capture_output": True, "text": True, "check": True}
        return SimpleNamespace(stderr='openjdk version "1.8.0_402"', stdout="")

    monkeypatch.setattr(backend_module.subprocess, "run", fake_run)

    backend = OpenDataLoaderBackend()
    markdown, assets = backend.parse(source)

    assert markdown == ""
    assert assets == []
    assert backend.metadata["opendataloader_java_version"] == 'openjdk version "1.8.0_402"'
    assert backend.metadata["opendataloader_backend_error"] == (
        "当前 Java 版本为 8，OpenDataLoader 官方要求 Java 11+"
    )
