"""通过 arXiv 官方 Atom API 采集论文描述性元数据。"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from xml.etree.ElementTree import Element

import httpx
from defusedxml import ElementTree

from app.domain.schemas import ArxivPaper, ArxivSearchResponse

ARXIV_API_URL = "https://export.arxiv.org/api/query"
ARXIV_REQUEST_INTERVAL_SECONDS = 3.0
ARXIV_TIMEOUT_SECONDS = 20.0

ATOM = "{http://www.w3.org/2005/Atom}"
OPENSEARCH = "{http://a9.com/-/spec/opensearch/1.1/}"


class ArxivCrawlerError(RuntimeError):
    """arXiv 上游请求或响应无法完成时抛出。"""


def _compact_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _required_text(parent: Element, path: str) -> str:
    value = _compact_text(parent.findtext(path))
    if not value:
        raise ArxivCrawlerError(f"arXiv 响应缺少字段：{path}")
    return value


def _parse_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ArxivCrawlerError("arXiv 响应包含无效时间") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def parse_arxiv_feed(
    payload: bytes,
    *,
    query: str,
    optimized_query: str | None = None,
    search_query: str | None = None,
    optimization_mode: str = "RULES",
    optimization_model: str | None = None,
    optimization_warning: str | None = None,
) -> ArxivSearchResponse:
    """把 arXiv Atom XML 转为前端所需的安全、稳定契约。"""

    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError as exc:
        raise ArxivCrawlerError("arXiv 返回了无法解析的 XML") from exc

    total_text = _compact_text(root.findtext(f"{OPENSEARCH}totalResults")) or "0"
    try:
        total_results = int(total_text)
    except ValueError as exc:
        raise ArxivCrawlerError("arXiv 返回了无效的结果总数") from exc

    items: list[ArxivPaper] = []
    for entry in root.findall(f"{ATOM}entry"):
        abstract_url = _required_text(entry, f"{ATOM}id").replace("http://", "https://", 1)
        arxiv_id = abstract_url.rsplit("/abs/", 1)[-1]
        pdf_url = next(
            (
                link.attrib.get("href", "")
                for link in entry.findall(f"{ATOM}link")
                if link.attrib.get("type") == "application/pdf"
            ),
            f"https://arxiv.org/pdf/{arxiv_id}",
        ).replace("http://", "https://", 1)
        authors = [
            name
            for author in entry.findall(f"{ATOM}author")
            if (name := _compact_text(author.findtext(f"{ATOM}name")))
        ]
        categories = [
            term
            for category in entry.findall(f"{ATOM}category")
            if (term := _compact_text(category.attrib.get("term")))
        ]
        items.append(
            ArxivPaper(
                arxiv_id=arxiv_id,
                title=_required_text(entry, f"{ATOM}title"),
                summary=_required_text(entry, f"{ATOM}summary"),
                authors=authors,
                categories=categories,
                published_at=_parse_datetime(_required_text(entry, f"{ATOM}published")),
                updated_at=_parse_datetime(_required_text(entry, f"{ATOM}updated")),
                abstract_url=abstract_url,
                pdf_url=pdf_url,
            )
        )

    return ArxivSearchResponse(
        query=query,
        optimized_query=optimized_query or query,
        search_query=search_query or f'all:"{query}"',
        optimization_mode=optimization_mode,
        optimization_model=optimization_model,
        optimization_warning=optimization_warning,
        total_results=total_results,
        fetched_at=datetime.now(UTC),
        items=items,
    )


class ArxivRequestGate:
    """把当前进程内所有 arXiv 请求串行化，并保证请求间隔。"""

    def __init__(
        self,
        *,
        interval_seconds: float = ARXIV_REQUEST_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._interval_seconds = interval_seconds
        self._clock = clock
        self._sleep = sleep
        self._lock = asyncio.Lock()
        self._last_request_at: float | None = None

    @asynccontextmanager
    async def request_slot(self) -> AsyncIterator[None]:
        async with self._lock:
            if self._last_request_at is not None:
                elapsed = self._clock() - self._last_request_at
                if elapsed < self._interval_seconds:
                    await self._sleep(self._interval_seconds - elapsed)
            try:
                yield
            finally:
                self._last_request_at = self._clock()


class ArxivCrawler:
    """遵守 arXiv 单连接、三秒间隔约束的轻量采集器。"""

    def __init__(self, *, gate: ArxivRequestGate | None = None) -> None:
        self._gate = gate or ArxivRequestGate()
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=ARXIV_TIMEOUT_SECONDS,
                follow_redirects=True,
                limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
                headers={"User-Agent": "EnerLedger-AI/0.1 arXiv crawler demo"},
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    async def search(
        self,
        query: str,
        *,
        max_results: int,
        search_query: str | None = None,
        optimized_query: str | None = None,
        optimization_mode: str = "RULES",
        optimization_model: str | None = None,
        optimization_warning: str | None = None,
    ) -> ArxivSearchResponse:
        normalized_query = _compact_text(query.replace('"', " "))
        if not normalized_query:
            raise ValueError("检索关键词不能为空")

        async with self._gate.request_slot():
            try:
                response = await self._get_client().get(
                    ARXIV_API_URL,
                    params={
                        "search_query": search_query or f'all:"{normalized_query}"',
                        "start": 0,
                        "max_results": max_results,
                        "sortBy": "submittedDate",
                        "sortOrder": "descending",
                    },
                )
                response.raise_for_status()
            except httpx.HTTPError as exc:
                raise ArxivCrawlerError("暂时无法从 arXiv 获取论文，请稍后重试") from exc

        return parse_arxiv_feed(
            response.content,
            query=normalized_query,
            optimized_query=optimized_query,
            search_query=search_query,
            optimization_mode=optimization_mode,
            optimization_model=optimization_model,
            optimization_warning=optimization_warning,
        )

    async def download_pdf(
        self,
        arxiv_id: str,
        destination: Path,
        *,
        max_bytes: int,
    ) -> int:
        """串行下载一篇论文 PDF，并在写盘过程中执行大小和签名校验。"""

        total_bytes = 0
        try:
            async with self._gate.request_slot():
                async with self._get_client().stream(
                    "GET", f"https://arxiv.org/pdf/{arxiv_id}"
                ) as response:
                    response.raise_for_status()
                    content_length = response.headers.get("content-length")
                    if content_length and int(content_length) > max_bytes:
                        raise ArxivCrawlerError("论文 PDF 超过文档上传上限")
                    with destination.open("wb") as output:
                        async for chunk in response.aiter_bytes(1024 * 1024):
                            total_bytes += len(chunk)
                            if total_bytes > max_bytes:
                                raise ArxivCrawlerError("论文 PDF 超过文档上传上限")
                            output.write(chunk)
        except ArxivCrawlerError:
            destination.unlink(missing_ok=True)
            raise
        except (httpx.HTTPError, OSError, ValueError) as exc:
            destination.unlink(missing_ok=True)
            raise ArxivCrawlerError("暂时无法下载该论文 PDF") from exc

        if total_bytes <= 0:
            destination.unlink(missing_ok=True)
            raise ArxivCrawlerError("arXiv 返回了空的论文 PDF")
        try:
            with destination.open("rb") as source:
                is_pdf = source.read(5) == b"%PDF-"
        except OSError as exc:
            raise ArxivCrawlerError("下载的论文 PDF 无法读取") from exc
        if not is_pdf:
            destination.unlink(missing_ok=True)
            raise ArxivCrawlerError("arXiv 返回的内容不是有效 PDF")
        return total_bytes


arxiv_crawler = ArxivCrawler()
