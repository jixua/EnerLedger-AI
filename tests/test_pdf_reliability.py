from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pymupdf
import pytest

import app.rag.core.parser.pdf.reliability as reliability_module
from app.rag.core.parser.pdf.reliability import (
    OpenDataLoaderConversionError,
    OpenDataLoaderHealthChecker,
    OpenDataLoaderProcessRunner,
    OpenDataLoaderRuntimeError,
    OpenDataLoaderTimeoutError,
    PdfPreflightError,
    PdfReliabilityGuard,
    PdfReliabilityLimits,
    PdfResourceLimitError,
    enforce_output_limits,
    parse_java_major_version,
)


def _write_pdf(
    path: Path,
    *,
    page_count: int = 1,
    with_image: bool = False,
    encrypted: bool = False,
) -> None:
    document = pymupdf.open()
    image_bytes: bytes | None = None
    if with_image:
        pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 32, 32), False)
        pixmap.clear_with(180)
        image_bytes = pixmap.tobytes("png")
    for page_number in range(1, page_count + 1):
        page = document.new_page()
        page.insert_text((72, 72), f"energy accounting page {page_number}")
        if image_bytes is not None:
            page.insert_image(pymupdf.Rect(72, 100, 200, 228), stream=image_bytes)
    save_options: dict[str, object] = {}
    if encrypted:
        save_options.update(
            encryption=pymupdf.PDF_ENCRYPT_AES_256,
            owner_pw="owner-secret",
            user_pw="user-secret",
        )
    document.save(path, **save_options)
    document.close()


def test_preflight_reports_pages_and_embedded_image_bytes(tmp_path: Path) -> None:
    source = tmp_path / "valid.pdf"
    _write_pdf(source, page_count=2, with_image=True)

    report = PdfReliabilityGuard().inspect(source)

    assert report.page_count == 2
    assert report.image_count == 2
    assert report.unique_image_count == 1
    assert report.inline_image_count == 0
    assert report.largest_image_pixels == 32 * 32
    assert report.total_image_pixels == 2 * 32 * 32
    assert report.largest_decoded_image_bytes == 32 * 32 * 4
    assert report.total_decoded_image_bytes == 2 * 32 * 32 * 4
    assert report.largest_image_bytes > 0
    assert report.total_image_bytes == report.largest_image_bytes
    assert report.encrypted is False


class _FakeInlineImagePage:
    def __init__(self, images: list[dict[str, object]]) -> None:
        self._images = images

    def get_image_info(self, *, xrefs: bool) -> list[dict[str, object]]:
        assert xrefs is True
        return self._images


class _FakeInlineImageDocument:
    needs_pass = False
    is_encrypted = False
    is_repaired = False
    page_count = 1

    def __init__(self, images: list[dict[str, object]]) -> None:
        self._page = _FakeInlineImagePage(images)
        self.closed = False

    def load_page(self, page_index: int) -> _FakeInlineImagePage:
        assert page_index == 0
        return self._page

    def close(self) -> None:
        self.closed = True


def _image_info(*, width: int, height: int, xref: int = 0) -> dict[str, object]:
    return {
        "xref": xref,
        "width": width,
        "height": height,
        "bpc": 8,
        "colorspace": 3,
        "cs-name": "DeviceRGB",
    }


def test_preflight_ignores_only_invisible_empty_inline_placeholders(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "empty-placeholder.pdf"
    source.write_bytes(b"%PDF-1.7\n")
    empty_placeholder = {
        **_image_info(width=0, height=0),
        "size": 0,
        "bbox": (0.0, 0.0, 0.0, 0.0),
        "transform": (0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
    }
    document = _FakeInlineImageDocument(
        [empty_placeholder, _image_info(width=20, height=10)]
    )
    monkeypatch.setattr(reliability_module.pymupdf, "open", lambda **_kwargs: document)

    report = PdfReliabilityGuard().inspect(source)

    assert report.image_count == 1
    assert report.inline_image_count == 1
    assert report.total_image_pixels == 200
    assert report.warnings == (
        "PDF_EMPTY_INLINE_IMAGE_PLACEHOLDER_IGNORED:page=1,count=1",
    )
    assert document.closed is True


def test_preflight_counts_and_bounds_xref_zero_inline_images_before_decode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "inline.pdf"
    source.write_bytes(b"%PDF-1.7\n")
    document = _FakeInlineImageDocument(
        [
            _image_info(width=100, height=80),
            _image_info(width=20, height=10),
        ]
    )
    monkeypatch.setattr(reliability_module.pymupdf, "open", lambda **_kwargs: document)

    report = PdfReliabilityGuard().inspect(source)

    assert report.image_count == 2
    assert report.inline_image_count == 2
    assert report.unique_image_count == 2
    assert report.largest_image_pixels == 8000
    assert report.total_image_pixels == 8200
    assert report.estimated_inline_image_bytes == 8200 * 4
    assert report.total_decoded_image_bytes == report.estimated_inline_image_bytes
    assert report.total_image_bytes == 0
    assert document.closed is True


@pytest.mark.parametrize(
    ("limits", "expected_code"),
    [
        (
            PdfReliabilityLimits(max_single_image_pixels=100),
            "PDF_SINGLE_IMAGE_PIXELS_EXCEEDED",
        ),
        (
            PdfReliabilityLimits(max_total_image_pixels=100),
            "PDF_TOTAL_IMAGE_PIXELS_EXCEEDED",
        ),
        (
            PdfReliabilityLimits(max_total_decoded_image_bytes=100),
            "PDF_TOTAL_DECODED_IMAGE_BYTES_EXCEEDED",
        ),
    ],
)
def test_preflight_inline_image_limits_cannot_be_bypassed_by_zero_xref(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    limits: PdfReliabilityLimits,
    expected_code: str,
) -> None:
    source = tmp_path / "inline-limit.pdf"
    source.write_bytes(b"%PDF-1.7\n")
    document = _FakeInlineImageDocument(
        [_image_info(width=20, height=20)]
    )
    monkeypatch.setattr(reliability_module.pymupdf, "open", lambda **_kwargs: document)

    with pytest.raises(PdfResourceLimitError) as caught:
        PdfReliabilityGuard(limits).inspect(source)

    assert caught.value.error_code == expected_code
    assert caught.value.retryable is False
    assert document.closed is True


def test_preflight_inline_images_are_included_in_image_count_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "inline-count.pdf"
    source.write_bytes(b"%PDF-1.7\n")
    document = _FakeInlineImageDocument(
        [_image_info(width=5, height=5), _image_info(width=5, height=5)]
    )
    monkeypatch.setattr(reliability_module.pymupdf, "open", lambda **_kwargs: document)

    with pytest.raises(PdfResourceLimitError) as caught:
        PdfReliabilityGuard(PdfReliabilityLimits(max_images=1)).inspect(source)

    assert caught.value.error_code == "PDF_MAX_IMAGES_EXCEEDED"
    assert caught.value.retryable is False
    assert document.closed is True


def test_preflight_rejects_tiny_compressed_xref_with_huge_decode_footprint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "decompression-bomb.pdf"
    source.write_bytes(b"%PDF-1.7\n")
    document = _FakeInlineImageDocument(
        [_image_info(width=1000, height=1000, xref=7)]
    )
    monkeypatch.setattr(reliability_module.pymupdf, "open", lambda **_kwargs: document)
    monkeypatch.setattr(
        PdfReliabilityGuard,
        "_image_stream_length",
        staticmethod(lambda _document, _xref: 32),
    )

    with pytest.raises(PdfResourceLimitError) as caught:
        PdfReliabilityGuard(
            PdfReliabilityLimits(
                max_single_image_pixels=2_000_000,
                max_total_decoded_image_bytes=1_000_000,
            )
        ).inspect(source)

    assert caught.value.error_code == "PDF_TOTAL_DECODED_IMAGE_BYTES_EXCEEDED"
    assert caught.value.details["actual"] == 4_000_000
    assert caught.value.retryable is False
    assert document.closed is True


@pytest.mark.parametrize(
    ("content", "error_code"),
    [
        (b"not a pdf", "PDF_MAGIC_INVALID"),
        (b"%PDF-1.7\nnot-a-valid-structure", "PDF_STRUCTURE_INVALID"),
    ],
)
def test_preflight_rejects_invalid_pdf_deterministically(
    tmp_path: Path,
    content: bytes,
    error_code: str,
) -> None:
    source = tmp_path / "invalid.pdf"
    source.write_bytes(content)

    with pytest.raises(PdfPreflightError) as caught:
        PdfReliabilityGuard().inspect(source)

    assert caught.value.error_code == error_code
    assert caught.value.retryable is False


def test_pdf_parser_preserves_preflight_error_contract(tmp_path: Path) -> None:
    from app.rag.core.parser.providers.pdf_parser import PdfParser

    source = tmp_path / "broken.pdf"
    source.write_bytes(b"%PDF-1.7\nbroken")

    with pytest.raises(PdfPreflightError) as caught:
        PdfParser(backend="opendataloader").parse(source)

    assert caught.value.error_code == "PDF_STRUCTURE_INVALID"
    assert caught.value.retryable is False


def test_preflight_rejects_encrypted_pdf_deterministically(tmp_path: Path) -> None:
    source = tmp_path / "encrypted.pdf"
    _write_pdf(source, encrypted=True)

    with pytest.raises(PdfPreflightError) as caught:
        PdfReliabilityGuard().inspect(source)

    assert caught.value.error_code == "PDF_ENCRYPTED"
    assert caught.value.retryable is False


def test_preflight_enforces_page_and_image_limits(tmp_path: Path) -> None:
    too_many_pages = tmp_path / "pages.pdf"
    _write_pdf(too_many_pages, page_count=2)
    with pytest.raises(PdfResourceLimitError) as page_error:
        PdfReliabilityGuard(PdfReliabilityLimits(max_pages=1)).inspect(too_many_pages)
    assert page_error.value.error_code == "PDF_MAX_PAGES_EXCEEDED"
    assert page_error.value.retryable is False

    image_pdf = tmp_path / "image.pdf"
    _write_pdf(image_pdf, page_count=2, with_image=True)
    with pytest.raises(PdfResourceLimitError) as image_count_error:
        PdfReliabilityGuard(PdfReliabilityLimits(max_images=1)).inspect(image_pdf)
    assert image_count_error.value.error_code == "PDF_MAX_IMAGES_EXCEEDED"

    with pytest.raises(PdfResourceLimitError) as image_error:
        PdfReliabilityGuard(
            PdfReliabilityLimits(max_single_image_bytes=1)
        ).inspect(image_pdf)
    assert image_error.value.error_code == "PDF_SINGLE_IMAGE_BYTES_EXCEEDED"
    assert image_error.value.retryable is False

    with pytest.raises(PdfResourceLimitError) as total_image_error:
        PdfReliabilityGuard(PdfReliabilityLimits(max_total_image_bytes=1)).inspect(image_pdf)
    assert total_image_error.value.error_code == "PDF_TOTAL_IMAGE_BYTES_EXCEEDED"
    assert total_image_error.value.retryable is False


def test_output_limits_cover_image_count_bytes_and_whole_directory(tmp_path: Path) -> None:
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    (output_dir / "result.md").write_bytes(b"m" * 20)
    (output_dir / "figure.png").write_bytes(b"i" * 40)

    with pytest.raises(PdfResourceLimitError) as image_error:
        enforce_output_limits(
            output_dir,
            PdfReliabilityLimits(max_single_image_bytes=30),
        )
    assert image_error.value.error_code == "ODL_SINGLE_IMAGE_BYTES_EXCEEDED"
    assert image_error.value.retryable is False

    with pytest.raises(PdfResourceLimitError) as directory_error:
        enforce_output_limits(
            output_dir,
            PdfReliabilityLimits(max_output_dir_bytes=50),
        )
    assert directory_error.value.error_code == "ODL_OUTPUT_DIR_BYTES_EXCEEDED"

    with pytest.raises(PdfResourceLimitError) as file_count_error:
        enforce_output_limits(
            output_dir,
            PdfReliabilityLimits(max_output_files=1),
        )
    assert file_count_error.value.error_code == "ODL_MAX_OUTPUT_FILES_EXCEEDED"
    assert file_count_error.value.retryable is False


def test_process_runner_times_out_and_terminates_isolated_process_group(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pdf"
    _write_pdf(source)

    def command_factory(
        _source: Path,
        _output: Path,
        _images: Path,
        _marker: str,
        _table_method: str,
        _markdown_with_html: bool,
    ) -> list[str]:
        return [sys.executable, "-c", "import time; time.sleep(30)"]

    runner = OpenDataLoaderProcessRunner(
        timeout_seconds=0.15,
        poll_interval_seconds=0.02,
        termination_grace_seconds=0.1,
        command_factory=command_factory,
    )
    started_at = time.monotonic()
    with pytest.raises(OpenDataLoaderTimeoutError) as caught:
        runner.run(
            source=source,
            output_dir=tmp_path / "output",
            image_dir=tmp_path / "output" / "images",
            page_marker_template="<!-- ODL_PAGE:%page-number% -->",
            limits=PdfReliabilityLimits(),
        )

    assert time.monotonic() - started_at < 2
    assert caught.value.error_code == "ODL_TIMEOUT"
    assert caught.value.retryable is True


def test_process_runner_stops_deterministically_when_output_grows_past_limit(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pdf"
    _write_pdf(source)

    def command_factory(
        _source: Path,
        output: Path,
        _images: Path,
        _marker: str,
        _table_method: str,
        _markdown_with_html: bool,
    ) -> list[str]:
        code = (
            "from pathlib import Path; import sys, time; "
            "Path(sys.argv[1]).write_bytes(b'x' * 256); time.sleep(30)"
        )
        return [sys.executable, "-c", code, str(output / "oversized.bin")]

    runner = OpenDataLoaderProcessRunner(
        timeout_seconds=5,
        poll_interval_seconds=0.02,
        termination_grace_seconds=0.1,
        command_factory=command_factory,
    )
    with pytest.raises(PdfResourceLimitError) as caught:
        runner.run(
            source=source,
            output_dir=tmp_path / "output",
            image_dir=tmp_path / "output" / "images",
            page_marker_template="<!-- ODL_PAGE:%page-number% -->",
            limits=PdfReliabilityLimits(max_output_dir_bytes=128),
        )

    assert caught.value.error_code == "ODL_OUTPUT_DIR_BYTES_EXCEEDED"
    assert caught.value.retryable is False


def test_process_runner_spools_large_logs_to_files_and_keeps_only_bounded_tail(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pdf"
    _write_pdf(source)

    def command_factory(
        _source: Path,
        _output: Path,
        _images: Path,
        _marker: str,
        _table_method: str,
        _markdown_with_html: bool,
    ) -> list[str]:
        payload = json.dumps(
            {
                "error_code": "ODL_INPUT_INVALID",
                "message": "deterministic worker failure",
                "retryable": False,
            },
            separators=(",", ":"),
        )
        code = (
            "import sys; "
            "sys.stdout.write('o' * 2000000); "
            "sys.stderr.write('e' * 2000000 + '\\n'); "
            f"sys.stderr.write('ODL_WORKER_ERROR:{payload}\\n'); "
            "raise SystemExit(30)"
        )
        return [sys.executable, "-c", code]

    runner = OpenDataLoaderProcessRunner(
        timeout_seconds=5,
        poll_interval_seconds=0.02,
        command_factory=command_factory,
    )

    with pytest.raises(OpenDataLoaderConversionError) as caught:
        runner.run(
            source=source,
            output_dir=tmp_path / "output",
            image_dir=tmp_path / "output" / "images",
            page_marker_template="<!-- ODL_PAGE:%page-number% -->",
            limits=PdfReliabilityLimits(),
        )

    assert caught.value.error_code == "ODL_INPUT_INVALID"
    assert caught.value.retryable is False
    assert len(str(caught.value.details["worker_stdout"])) <= 1000
    assert len(str(caught.value.details["worker_stderr"])) <= 1000


def test_process_runner_stops_non_retryably_when_logs_exceed_limit(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pdf"
    _write_pdf(source)

    def command_factory(
        _source: Path,
        _output: Path,
        _images: Path,
        _marker: str,
        _table_method: str,
        _markdown_with_html: bool,
    ) -> list[str]:
        code = (
            "import os, time; "
            "chunk = b'x' * 8192; "
            "[(os.write(1, chunk), time.sleep(0.001)) for _ in range(128)]; "
            "time.sleep(30)"
        )
        return [sys.executable, "-c", code]

    runner = OpenDataLoaderProcessRunner(
        timeout_seconds=5,
        poll_interval_seconds=0.01,
        termination_grace_seconds=0.1,
        command_factory=command_factory,
    )
    started_at = time.monotonic()

    with pytest.raises(PdfResourceLimitError) as caught:
        runner.run(
            source=source,
            output_dir=tmp_path / "output",
            image_dir=tmp_path / "output" / "images",
            page_marker_template="<!-- ODL_PAGE:%page-number% -->",
            limits=PdfReliabilityLimits(max_log_bytes=32 * 1024),
        )

    assert time.monotonic() - started_at < 2
    assert caught.value.error_code == "ODL_LOG_BYTES_EXCEEDED"
    assert caught.value.retryable is False
    assert int(caught.value.details["actual"]) > int(caught.value.details["limit"])


def test_process_runner_checks_log_limit_again_after_fast_worker_exit(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pdf"
    _write_pdf(source)

    def command_factory(
        _source: Path,
        _output: Path,
        _images: Path,
        _marker: str,
        _table_method: str,
        _markdown_with_html: bool,
    ) -> list[str]:
        return [sys.executable, "-c", "import os; os.write(2, b'e' * 65536)"]

    runner = OpenDataLoaderProcessRunner(
        timeout_seconds=5,
        poll_interval_seconds=0.1,
        command_factory=command_factory,
    )

    with pytest.raises(PdfResourceLimitError) as caught:
        runner.run(
            source=source,
            output_dir=tmp_path / "output",
            image_dir=tmp_path / "output" / "images",
            page_marker_template="<!-- ODL_PAGE:%page-number% -->",
            limits=PdfReliabilityLimits(max_log_bytes=1024),
        )

    assert caught.value.error_code == "ODL_LOG_BYTES_EXCEEDED"
    assert caught.value.retryable is False
    assert caught.value.details["stderr_bytes"] == 65536


def test_process_runner_stops_non_retryably_when_output_file_count_exceeds_limit(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pdf"
    _write_pdf(source)

    def command_factory(
        _source: Path,
        output: Path,
        _images: Path,
        _marker: str,
        _table_method: str,
        _markdown_with_html: bool,
    ) -> list[str]:
        code = (
            "from pathlib import Path; import sys, time; "
            "root = Path(sys.argv[1]); "
            "[(root / f'part-{index}.txt').touch() for index in range(20)]; "
            "time.sleep(30)"
        )
        return [sys.executable, "-c", code, str(output)]

    runner = OpenDataLoaderProcessRunner(
        timeout_seconds=5,
        poll_interval_seconds=0.01,
        termination_grace_seconds=0.1,
        command_factory=command_factory,
    )
    started_at = time.monotonic()

    with pytest.raises(PdfResourceLimitError) as caught:
        runner.run(
            source=source,
            output_dir=tmp_path / "output",
            image_dir=tmp_path / "output" / "images",
            page_marker_template="<!-- ODL_PAGE:%page-number% -->",
            limits=PdfReliabilityLimits(max_output_files=5),
        )

    assert time.monotonic() - started_at < 2
    assert caught.value.error_code == "ODL_MAX_OUTPUT_FILES_EXCEEDED"
    assert caught.value.retryable is False


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
def test_java_major_version_parser(version_output: str, expected: int | None) -> None:
    assert parse_java_major_version(version_output) == expected


def test_health_checker_reports_package_and_java_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(command: list[str], **_kwargs: object) -> SimpleNamespace:
        if command[-1] == "_health_worker":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        assert command[-1] == "-version"
        return SimpleNamespace(
            returncode=0,
            stdout="",
            stderr='openjdk version "17.0.12" 2024-07-16',
        )

    monkeypatch.setattr(reliability_module.subprocess, "run", fake_run)
    monkeypatch.setattr(reliability_module.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(OpenDataLoaderHealthChecker, "_package_version", lambda self: "2.5.0")

    health = OpenDataLoaderHealthChecker().check()

    assert health.ready is True
    assert health.package_available is True
    assert health.java_available is True
    assert health.java_major_version == 17
    assert health.package_version == "2.5.0"


def test_health_checker_marks_missing_dependency_non_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = json.dumps(
        {
            "error_code": "ODL_DEPENDENCY_MISSING",
            "message": "opendataloader-pdf missing",
            "retryable": False,
        }
    )

    def fake_run(_command: list[str], **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            returncode=20,
            stdout="",
            stderr=f"ODL_WORKER_ERROR:{payload}\n",
        )

    monkeypatch.setattr(reliability_module.subprocess, "run", fake_run)
    monkeypatch.setattr(OpenDataLoaderHealthChecker, "_package_version", lambda self: None)

    health = OpenDataLoaderHealthChecker().check()
    assert health.ready is False
    assert health.error_code == "ODL_DEPENDENCY_MISSING"
    with pytest.raises(OpenDataLoaderRuntimeError) as caught:
        health.require_ready()
    assert caught.value.error_code == "ODL_DEPENDENCY_MISSING"
    assert caught.value.retryable is False


def test_worker_dependency_probe_is_executable_in_a_real_subprocess() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "app.rag.core.parser.pdf.reliability",
            "_health_worker",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (ValueError("invalid PDF object"), ("ODL_INPUT_INVALID", False)),
        (RuntimeError("encrypted document"), ("ODL_INPUT_INVALID", False)),
        (RuntimeError("HTTP status 400"), ("ODL_HTTP_400", False)),
        (RuntimeError("API error: 401"), ("ODL_HTTP_401", False)),
        (RuntimeError("API error: 403"), ("ODL_HTTP_403", False)),
        (RuntimeError("API error: 404"), ("ODL_HTTP_404", False)),
        (RuntimeError("API error: 422"), ("ODL_HTTP_422", False)),
        (RuntimeError("HTTP status 408"), ("ODL_HTTP_408", True)),
        (RuntimeError("HTTP status 425"), ("ODL_HTTP_425", True)),
        (RuntimeError("HTTP status 429"), ("ODL_HTTP_429", True)),
        (RuntimeError("HTTP status 503"), ("ODL_HTTP_503", True)),
        (RuntimeError("content safety policy blocked"), ("ODL_INPUT_INVALID", False)),
        (RuntimeError("temporary connection unavailable"), ("ODL_TRANSIENT_FAILURE", True)),
        (RuntimeError("unknown JVM crash"), ("ODL_CONVERT_FAILED", True)),
    ],
)
def test_odl_conversion_errors_distinguish_deterministic_failures(
    error: BaseException,
    expected: tuple[str, bool],
) -> None:
    assert reliability_module._classify_odl_conversion_error(error) == expected
