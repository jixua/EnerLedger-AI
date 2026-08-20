from __future__ import annotations

from datetime import UTC
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.api import crawler as crawler_api
from app.domain.schemas import ArxivImportRequest, ArxivSearchResponse
from app.services.arxiv_crawler import ArxivRequestGate, parse_arxiv_feed

ATOM_FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">
  <opensearch:totalResults>42</opensearch:totalResults>
  <entry>
    <id>http://arxiv.org/abs/2608.12345v1</id>
    <updated>2026-08-18T10:20:30Z</updated>
    <published>2026-08-17T09:10:11Z</published>
    <title>  Carbon   Accounting with AI  </title>
    <summary>Evidence-based\n carbon accounting.</summary>
    <author><name>Alice Example</name></author>
    <author><name>Bob Example</name></author>
    <category term="cs.AI" />
    <category term="econ.GN" />
    <link href="http://arxiv.org/pdf/2608.12345v1" type="application/pdf" />
  </entry>
</feed>
"""


def test_parse_arxiv_feed_returns_normalized_metadata() -> None:
    result = parse_arxiv_feed(ATOM_FEED, query="carbon accounting")

    assert result.source == "arXiv"
    assert result.total_results == 42
    assert len(result.items) == 1
    paper = result.items[0]
    assert paper.arxiv_id == "2608.12345v1"
    assert paper.title == "Carbon Accounting with AI"
    assert paper.summary == "Evidence-based carbon accounting."
    assert paper.authors == ["Alice Example", "Bob Example"]
    assert paper.categories == ["cs.AI", "econ.GN"]
    assert paper.published_at.tzinfo == UTC
    assert str(paper.abstract_url) == "https://arxiv.org/abs/2608.12345v1"
    assert str(paper.pdf_url) == "https://arxiv.org/pdf/2608.12345v1"


@pytest.mark.asyncio
async def test_arxiv_api_uses_authenticated_fixed_crawler(monkeypatch) -> None:
    expected = parse_arxiv_feed(ATOM_FEED, query="carbon footprint")
    calls: list[tuple[str, int]] = []

    async def fake_search(query: str, *, max_results: int) -> ArxivSearchResponse:
        calls.append((query, max_results))
        return expected

    monkeypatch.setattr(crawler_api.arxiv_crawler, "search", fake_search)

    result = await crawler_api.search_arxiv_papers(
        query="carbon footprint",
        max_results=5,
        _=1,
    )

    assert result == expected
    assert calls == [("carbon footprint", 5)]


@pytest.mark.asyncio
async def test_arxiv_request_gate_waits_three_seconds_between_requests() -> None:
    current_time = [100.0]
    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)
        current_time[0] += delay

    gate = ArxivRequestGate(
        interval_seconds=3.0,
        clock=lambda: current_time[0],
        sleep=fake_sleep,
    )
    async with gate.request_slot():
        pass
    current_time[0] += 1.0
    async with gate.request_slot():
        pass

    assert delays == [2.0]


@pytest.mark.asyncio
async def test_arxiv_import_downloads_and_queues_selected_papers(monkeypatch) -> None:
    calls: list[tuple[str, object]] = []

    async def fake_owned_dataset(db, dataset_id: int, user_id: int):
        calls.append(("owned", (dataset_id, user_id)))
        return SimpleNamespace(id=dataset_id)

    async def fake_download(arxiv_id: str, destination: Path, *, max_bytes: int):
        destination.write_bytes(b"%PDF-1.7\n")
        calls.append(("download", (arxiv_id, max_bytes)))
        return destination.stat().st_size

    async def fake_queue(**kwargs):
        calls.append(("queue", kwargs))
        return SimpleNamespace(id=91)

    class FakeDb:
        async def rollback(self):
            calls.append(("rollback", None))

    monkeypatch.setattr(crawler_api, "_owned_dataset", fake_owned_dataset)
    monkeypatch.setattr(crawler_api.arxiv_crawler, "download_pdf", fake_download)
    monkeypatch.setattr(crawler_api, "queue_document_from_path", fake_queue)

    result = await crawler_api.import_arxiv_papers(
        payload=ArxivImportRequest(dataset_id=7, arxiv_ids=["2608.12345v1"]),
        user_id=3,
        db=FakeDb(),
    )

    assert result.queued_count == 1
    assert result.failed_count == 0
    assert result.items[0].document_id == 91
    assert result.items[0].filename == "arxiv-2608.12345v1.pdf"
    queue_call = next(value for name, value in calls if name == "queue")
    assert queue_call["dataset_id"] == 7
    assert queue_call["content_type"] == "application/pdf"
    assert any(name == "download" for name, _ in calls)
