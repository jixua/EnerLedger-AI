from pathlib import Path

from scripts.import_pdf_tree import scan_pdf_tree


def test_scan_pdf_tree_preserves_paths_and_renames_duplicate_filenames(tmp_path: Path) -> None:
    first = tmp_path / "A1" / "A1.1" / "report.pdf"
    second = tmp_path / "A2" / "A2.1" / "report.pdf"
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    first.write_bytes(b"%PDF-1.7\nfirst")
    second.write_bytes(b"%PDF-1.7\nsecond")

    entries, invalid = scan_pdf_tree(tmp_path, max_upload_bytes=1024)

    assert invalid == []
    assert [entry.relative_path.as_posix() for entry in entries] == [
        "A1/A1.1/report.pdf",
        "A2/A2.1/report.pdf",
    ]
    assert len({entry.upload_name for entry in entries}) == 2
    assert all(entry.upload_name.endswith(".pdf") for entry in entries)


def test_scan_pdf_tree_recovers_html_and_skips_corrupt_pseudo_pdf(tmp_path: Path) -> None:
    html = tmp_path / "HTML.pdf"
    corrupt = tmp_path / "broken.pdf"
    html.write_bytes(b"<!DOCTYPE html><html><body>report</body></html>")
    corrupt.write_bytes(b"PK\x03\x04truncated")

    entries, invalid = scan_pdf_tree(tmp_path, max_upload_bytes=1024)

    assert len(entries) == 1
    assert entries[0].upload_name == "HTML.html"
    assert entries[0].content_type == "text/html"
    assert invalid == [corrupt]
