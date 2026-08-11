"""MinerU 精准解析 API 后端：通过官方云服务实现高质量 PDF 解析。

核心优势：
- VLM + OCR 双引擎，109 种语言 OCR 识别
- 表格 → HTML，公式 → LaTeX
- 图片/图表解析与描述
- 跨页表格合并、多栏布局、扫描件/手写体支持

接口契约：
- 只支持 MinerU 官方 V4 云端 API。
- 有本地源文件时通过官方签名 URL 上传，无本地文件时兼容公网 URL 旁路。

时间复杂度 O(n)，n 为 PDF 页数，受限于远端服务处理速度。
"""

from __future__ import annotations

import io
import json
import re
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from app.rag.core.parser.pdf.base import BasePdfBackend
from app.rag.core.parser.pdf.models import PdfBinaryAsset
from app.rag.core.parser.pdf.reliability import PdfReliabilityLimits
from app.rag.observability.logging import (
    safe_exception_stack,
    sanitize_url_for_log,
    truncate_log_value,
)

_DEFAULT_TIMEOUT_SECONDS = 300  # 长文档解析可能需要较长时间
_MAX_CONSECUTIVE_POLL_ERRORS = 5  # 轮询连续返回 code != 0 的熔断阈值，避免硬等满超时
_IMAGE_PATTERN = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")


class MinerUBackend(BasePdfBackend):
    """通过 MinerU 官方云端 API 调用解析服务的后端。"""

    name = "mineru"

    def __init__(
        self,
        api_url: str | None = None,
        api_key: str | None = None,
        timeout: int = _DEFAULT_TIMEOUT_SECONDS,
        limits: PdfReliabilityLimits | None = None,
    ) -> None:
        super().__init__()
        self._api_url = (api_url or "").rstrip("/")
        self._api_key = api_key
        self._timeout = timeout
        self._limits = limits or PdfReliabilityLimits()

    def parse(self, source: Path | None, options: Any = None) -> tuple[str, list[PdfBinaryAsset]]:
        if not self._api_url:
            self.metadata["mineru_backend_error"] = "MINERU_API_URL 未配置"
            logger.warning("[MinerU Cloud] API URL 未配置，跳过此后端")
            return "", []
        if not self._api_key:
            self.metadata["mineru_backend_error"] = (
                "MINERU_API_KEY 未配置，MinerU 后端仅支持官方云端 API"
            )
            logger.warning("[MinerU Cloud] API Key 未配置，跳过此后端")
            return "", []

        source_file_url = getattr(options, "source_file_url", None)
        if source is None and not source_file_url:
            self.metadata["mineru_backend_error"] = (
                "MinerU 精准解析 API 需要本地源文件或可公网访问的 source_file_url"
            )
            logger.warning("[MinerU Cloud] 未提供可上传或可拉取的源文件")
            return "", []

        model_version = getattr(options, "mineru_model_version", "vlm") or "vlm"

        try:
            if source is not None:
                markdown, assets = self._call_cloud_upload_api(source, model_version)
                return markdown, assets
            markdown, assets = self._call_cloud_api(source_file_url, model_version)
            return markdown, assets
        except httpx.TimeoutException as exc:
            self.metadata["mineru_backend_error"] = "API 请求超时"
            logger.bind(
                event="pdf_backend_failed",
                outcome="failed",
                backend=self.name,
                stage="cloud_request",
                timeout_seconds=self._timeout,
                endpoint=sanitize_url_for_log(self._api_url),
                error_type=type(exc).__name__,
                stack_trace=safe_exception_stack(exc),
            ).error("MinerU API 请求超时")
            return "", []
        except httpx.ConnectError as exc:
            self.metadata["mineru_backend_error"] = f"无法连接 API: {exc}"
            logger.bind(
                event="pdf_backend_failed",
                outcome="failed",
                backend=self.name,
                stage="cloud_connect",
                endpoint=sanitize_url_for_log(self._api_url),
                error_type=type(exc).__name__,
                error_message=truncate_log_value(exc),
                stack_trace=safe_exception_stack(exc),
            ).error("无法连接 MinerU API")
            return "", []
        except Exception as exc:
            self.metadata["mineru_backend_error"] = str(exc)
            logger.bind(
                event="pdf_backend_failed",
                outcome="failed",
                backend=self.name,
                stage="cloud_parse",
                endpoint=sanitize_url_for_log(self._api_url),
                error_type=type(exc).__name__,
                error_message=truncate_log_value(exc),
                stack_trace=safe_exception_stack(exc),
            ).error("MinerU 云端解析异常")
            return "", []

    def _call_cloud_upload_api(
        self,
        source: Path,
        model_version: str,
    ) -> tuple[str, list[PdfBinaryAsset]]:
        """通过 MinerU V4 签名上传接口解析本地 PDF。

        流程：申请单文件批次上传 URL -> 流式 PUT 源文件 -> 轮询批次结果。
        这条路径不要求对公网暴露本地 MinIO。
        """
        headers = {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}
        api_base = self._build_api_base()
        upload_request_url = f"{api_base}/api/v4/file-urls/batch"

        with httpx.Client(timeout=self._timeout) as client:
            create_resp = client.post(
                upload_request_url,
                headers=headers,
                json={
                    "files": [{"name": source.name, "data_id": uuid.uuid4().hex}],
                    "model_version": model_version,
                    "enable_table": True,
                    "enable_formula": True,
                },
            )
            create_resp.raise_for_status()
            create_res = create_resp.json()
            if create_res.get("code") != 0:
                raise Exception(f"申请 MinerU 上传链接失败: {create_res.get('msg')}")

            data = create_res.get("data") or {}
            batch_id = data.get("batch_id")
            file_urls = data.get("file_urls") or []
            if not batch_id or not file_urls:
                raise Exception("MinerU 上传接口未返回 batch_id 或 file_urls")

            upload_url = file_urls[0]
            with source.open("rb") as source_stream:
                upload_resp = client.put(
                    upload_url,
                    content=source_stream,
                    headers={"Content-Length": str(source.stat().st_size)},
                )
            upload_resp.raise_for_status()

            self.metadata["mineru_batch_id"] = batch_id
            self.metadata["mineru_submission_mode"] = "signed_upload"
            poll_url = f"{api_base}/api/v4/extract-results/batch/{batch_id}"
            task_state = self._poll_batch_result(client, poll_url, headers)
            full_zip_url, markdown_url = self._extract_result_urls(task_state)
            return self._download_result(
                client,
                full_zip_url=full_zip_url,
                markdown_url=markdown_url,
                task_id=batch_id,
                model_version=model_version,
            )

    def _poll_batch_result(
        self,
        client: httpx.Client,
        poll_url: str,
        headers: dict[str, str],
    ) -> dict[str, Any]:
        start_time = time.time()
        poll_interval = 1.0
        consecutive_errors = 0

        while time.time() - start_time < self._timeout:
            poll_resp = client.get(
                poll_url,
                headers={"Authorization": headers["Authorization"]},
            )
            poll_resp.raise_for_status()
            poll_res = poll_resp.json()
            if poll_res.get("code") != 0:
                consecutive_errors += 1
                if consecutive_errors >= _MAX_CONSECUTIVE_POLL_ERRORS:
                    raise Exception(f"MinerU 批次轮询失败: {poll_res.get('msg')}")
                self._backoff(poll_interval, start_time)
                poll_interval = min(poll_interval * 1.5, 5.0)
                continue

            consecutive_errors = 0
            results = (poll_res.get("data") or {}).get("extract_result") or []
            task_state = results[0] if results else {}
            state = task_state.get("state")
            if state == "done":
                return task_state
            if state == "failed":
                raise Exception(f"MinerU 云端解析失败: {task_state.get('err_msg')}")
            self._backoff(poll_interval, start_time)
            poll_interval = min(poll_interval * 1.5, 5.0)

        raise Exception(f"MinerU 云端解析超时 ({self._timeout}s)")

    def _call_cloud_api(
        self,
        source_file_url: str,
        model_version: str,
    ) -> tuple[str, list[PdfBinaryAsset]]:
        """调用 MinerU 官方 V4 精准解析接口。

        流程：
        1. POST /api/v4/extract/task 提交文件 URL 获取 task_id
        2. 轮询 GET /api/v4/extract/task/{task_id} 直到 state == done
        3. 下载 full_zip_url 并解压提取 Markdown
        """
        headers = {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}

        task_url = self._build_task_url()

        with httpx.Client(timeout=self._timeout) as client:
            create_data = {
                "url": source_file_url,
                "model_version": model_version,
                "enable_table": True,
                "enable_formula": True,
                "language": "ch",
            }
            logger.bind(
                event="mineru_task_create",
                backend=self.name,
                endpoint=sanitize_url_for_log(task_url),
                model_version=model_version,
            ).info("正在创建 MinerU 精准解析任务")
            create_resp = client.post(task_url, headers=headers, json=create_data)
            create_resp.raise_for_status()
            create_res = create_resp.json()

            if create_res.get("code") != 0:
                raise Exception(f"创建精准解析任务失败: {create_res.get('msg')}")

            task_id = create_res["data"]["task_id"]
            poll_url = f"{task_url}/{task_id}"
            poll_headers = {"Authorization": headers["Authorization"]}

            logger.info(f"[MinerU Cloud] 任务创建成功，开始轮询解析结果: task_id={task_id}")
            start_time = time.time()
            full_zip_url = None
            markdown_url = None
            poll_interval = 1.0
            consecutive_errors = 0
            last_logged_pages = -1

            while time.time() - start_time < self._timeout:
                poll_resp = client.get(poll_url, headers=poll_headers)
                poll_resp.raise_for_status()
                poll_res = poll_resp.json()

                if poll_res.get("code") != 0:
                    consecutive_errors += 1
                    logger.warning(
                        "MinerU 轮询返回异常状态: consecutive_errors={}/{} message={}",
                        consecutive_errors,
                        _MAX_CONSECUTIVE_POLL_ERRORS,
                        truncate_log_value(poll_res.get("msg"), 256),
                    )
                    if consecutive_errors >= _MAX_CONSECUTIVE_POLL_ERRORS:
                        raise Exception(
                            f"轮询连续 {consecutive_errors} 次返回异常状态码，提前终止: "
                            f"{poll_res.get('msg')}"
                        )
                    self._backoff(poll_interval, start_time)
                    poll_interval = min(poll_interval * 1.5, 5.0)
                    continue

                consecutive_errors = 0
                task_state = poll_res.get("data", {})
                state = task_state.get("state")

                if state == "done":
                    full_zip_url, markdown_url = self._extract_result_urls(task_state)
                    logger.info("[MinerU Cloud] 解析成功！")
                    break
                elif state == "failed":
                    raise Exception(f"云端解析失败: {task_state.get('err_msg')}")
                else:
                    progress = task_state.get("extract_progress", {})
                    extracted_pages = int(progress.get("extracted_pages", 0) or 0)
                    total_pages = int(progress.get("total_pages", 0) or 0)
                    log_step = max(1, total_pages // 10) if total_pages else 10
                    if (
                        last_logged_pages < 0
                        or extracted_pages >= total_pages > 0
                        or extracted_pages - last_logged_pages >= log_step
                    ):
                        logger.bind(
                            event="mineru_task_progress",
                            backend=self.name,
                            mineru_task_id=task_id,
                            extracted_pages=extracted_pages,
                            total_pages=total_pages,
                        ).info(
                            "MinerU 解析进度: extracted_pages={} total_pages={}",
                            extracted_pages,
                            total_pages,
                        )
                        last_logged_pages = extracted_pages
                    self._backoff(poll_interval, start_time)
                    poll_interval = min(poll_interval * 1.5, 5.0)

            if not full_zip_url and not markdown_url:
                raise Exception(f"云端解析超时 ({self._timeout}s)")

            return self._download_result(
                client,
                full_zip_url=full_zip_url,
                markdown_url=markdown_url,
                task_id=task_id,
                model_version=model_version,
            )

    def _backoff(self, poll_interval: float, start_time: float) -> None:
        """统一的轮询退避：按剩余时间裁剪 sleep，确保任何分支都不会全速空转。"""
        remaining_time = self._timeout - (time.time() - start_time)
        if remaining_time > 0:
            time.sleep(min(poll_interval, remaining_time))

    def _build_task_url(self) -> str:
        """兼容域名、/api/v4 和完整 /api/v4/extract/task 三种配置。"""
        if self._api_url.endswith("/api/v4/extract/task"):
            return self._api_url
        if "/api/v4" in self._api_url:
            return self._api_url.split("/api/v4")[0] + "/api/v4/extract/task"
        return f"{self._api_url.rstrip('/')}/api/v4/extract/task"

    def _build_api_base(self) -> str:
        """从域名、/api/v4 或完整 task URL 提取 MinerU API 基地址。"""
        if "/api/v4" in self._api_url:
            return self._api_url.split("/api/v4", 1)[0]
        return self._api_url.rstrip("/")

    def _download_result(
        self,
        client: httpx.Client,
        *,
        full_zip_url: str | None,
        markdown_url: str | None,
        task_id: str,
        model_version: str,
    ) -> tuple[str, list[PdfBinaryAsset]]:
        """下载单任务或批次任务的 Markdown/ZIP 结果。"""
        if not full_zip_url and markdown_url:
            # 直接 Markdown 不包含项目质量门禁要求的原 PDF 页级来源。若把它当作
            # 成功结果，PdfParserService 会停止后端回退，随后 ingestion 又会因
            # PAGE_PROVENANCE_INVALID 终止，最终让整个业务任务无法完成。
            raise ValueError("MinerU 仅返回无页级来源的 Markdown，继续尝试后端回退")
        if not full_zip_url:
            raise Exception("MinerU 任务完成但未返回可下载结果")

        zip_bytes = self._download_zip(client, full_zip_url)
        markdown, assets = self._extract_zip_result(zip_bytes)

        self.metadata["mineru_api_status"] = 200
        self.metadata["mineru_task_id"] = task_id
        self.metadata["mineru_model_version"] = model_version
        self.metadata["mineru_download_mode"] = "zip_stream"
        return markdown, assets

    def _extract_zip_result(
        self,
        zip_bytes: bytes,
    ) -> tuple[str, list[PdfBinaryAsset]]:
        """Extract page-aware Markdown and image provenance from an official result ZIP."""
        import zipfile

        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
            members = archive.infolist()
            self._validate_zip_members(members)
            names = [member.filename for member in members]
            content_list_names = [
                name
                for name in names
                if name.endswith("_content_list.json") or name == "content_list.json"
            ]
            content_list: list[dict[str, Any]] = []
            if content_list_names:
                payload = json.loads(archive.read(content_list_names[0]).decode("utf-8"))
                if isinstance(payload, list):
                    content_list = [item for item in payload if isinstance(item, dict)]

            page_count = self._content_list_page_count(content_list)
            middle_names = [name for name in names if name.endswith("_middle.json")]
            if middle_names:
                middle = json.loads(archive.read(middle_names[0]).decode("utf-8"))
                if isinstance(middle, dict) and isinstance(middle.get("pdf_info"), list):
                    page_count = max(page_count, len(middle["pdf_info"]))

            image_pages = self._image_pages(content_list)
            if content_list and page_count:
                markdown = self._content_list_to_page_markdown(content_list, page_count)
                self.metadata["mineru_markdown_source"] = "content_list"
            else:
                self.metadata["mineru_markdown_source"] = "full_md_without_page_provenance"
                raise ValueError("MinerU 结果缺少可验证的页级来源，继续尝试后端回退")

            image_files = [
                name for name in names if name.startswith("images/") and not name.endswith("/")
            ]
            assets = [
                PdfBinaryAsset(
                    kind="picture",
                    page_number=image_pages.get(image_path),
                    index=index,
                    ext=image_path.rsplit(".", 1)[-1] if "." in image_path else "png",
                    content=archive.read(image_path),
                    source_path=image_path,
                )
                for index, image_path in enumerate(image_files, start=1)
            ]
            return markdown, assets

    def _validate_zip_members(self, members: list[Any]) -> None:
        """在读取任何 ZIP 条目前执行数量和展开大小上限检查。"""

        files = [member for member in members if not member.is_dir()]
        if len(files) > self._limits.max_output_files:
            raise ValueError(
                "MinerU 结果文件数超过限制: "
                f"{len(files)} > {self._limits.max_output_files}"
            )

        expanded_bytes = sum(max(int(member.file_size), 0) for member in files)
        if expanded_bytes > self._limits.max_output_dir_bytes:
            raise ValueError(
                "MinerU 结果展开大小超过限制: "
                f"{expanded_bytes} > {self._limits.max_output_dir_bytes}"
            )

        image_members = [
            member
            for member in files
            if member.filename.startswith("images/")
        ]
        if len(image_members) > self._limits.max_images:
            raise ValueError(
                "MinerU 图片数量超过限制: "
                f"{len(image_members)} > {self._limits.max_images}"
            )
        oversized_image = next(
            (
                member
                for member in image_members
                if member.file_size > self._limits.max_single_image_bytes
            ),
            None,
        )
        if oversized_image is not None:
            raise ValueError(
                "MinerU 单张图片超过限制: "
                f"{oversized_image.filename} ({oversized_image.file_size} bytes)"
            )
        total_image_bytes = sum(member.file_size for member in image_members)
        if total_image_bytes > self._limits.max_total_image_bytes:
            raise ValueError(
                "MinerU 图片总大小超过限制: "
                f"{total_image_bytes} > {self._limits.max_total_image_bytes}"
            )

    @staticmethod
    def _content_list_page_count(content_list: list[dict[str, Any]]) -> int:
        page_indices = [
            item.get("page_idx")
            for item in content_list
            if isinstance(item.get("page_idx"), int) and item["page_idx"] >= 0
        ]
        return max(page_indices, default=-1) + 1

    @staticmethod
    def _image_pages(content_list: list[dict[str, Any]]) -> dict[str, int]:
        return {
            str(item["img_path"]): int(item["page_idx"]) + 1
            for item in content_list
            if (
                item.get("type") in {"image", "chart", "table"}
                and item.get("img_path")
                and isinstance(item.get("page_idx"), int)
                and item["page_idx"] >= 0
            )
        }

    def _content_list_to_page_markdown(
        self,
        content_list: list[dict[str, Any]],
        page_count: int,
    ) -> str:
        pages: dict[int, list[dict[str, Any]]] = {
            page_number: [] for page_number in range(1, page_count + 1)
        }
        for item in content_list:
            page_idx = item.get("page_idx")
            if isinstance(page_idx, int) and 0 <= page_idx < page_count:
                pages[page_idx + 1].append(item)

        sections: list[str] = []
        for page_number, items in pages.items():
            body = self._content_list_to_markdown(items).strip()
            marker = f"<!-- ODL_PAGE:{page_number} -->"
            sections.append(f"{marker}\n\n{body}" if body else marker)
        return "\n\n".join(sections)

    def _extract_result_urls(self, task_state: dict[str, Any]) -> tuple[str | None, str | None]:
        """兼容 MinerU 结果 URL 可能位于 data 或 extract_result 下的返回结构。"""
        result = task_state.get("extract_result")
        if not isinstance(result, dict):
            result = {}

        full_zip_url = task_state.get("full_zip_url") or result.get("full_zip_url")
        markdown_url = (
            task_state.get("full_md_url")
            or task_state.get("markdown_url")
            or task_state.get("md_url")
            or result.get("full_md_url")
            or result.get("markdown_url")
            or result.get("md_url")
        )
        return full_zip_url, markdown_url

    def _download_zip(self, client: httpx.Client, full_zip_url: str) -> bytes:
        """分块下载 ZIP，并在累计内存前执行大小上限。"""
        logger.info("[MinerU Cloud] 正在流式下载结果 ZIP...")
        started_at = time.monotonic()
        zip_bytes = self._stream_download_bytes(client, full_zip_url)
        elapsed = time.monotonic() - started_at
        self.metadata["mineru_zip_download_bytes"] = len(zip_bytes)
        self.metadata["mineru_zip_download_seconds"] = round(elapsed, 3)
        logger.info(f"[MinerU Cloud] ZIP 下载完成: bytes={len(zip_bytes)}, elapsed={elapsed:.2f}s")
        return zip_bytes

    def _stream_download_bytes(self, client: httpx.Client, url: str) -> bytes:
        chunks: list[bytes] = []
        downloaded_bytes = 0
        with client.stream("GET", url) as response:
            response.raise_for_status()
            for chunk in response.iter_bytes():
                if chunk:
                    downloaded_bytes += len(chunk)
                    if downloaded_bytes > self._limits.max_output_dir_bytes:
                        raise ValueError(
                            "MinerU 结果包超过下载限制: "
                            f"{downloaded_bytes} > {self._limits.max_output_dir_bytes}"
                        )
                    chunks.append(chunk)
        return b"".join(chunks)

    def _extract_markdown(self, result: dict) -> str:
        """从 MinerU 返回结构中提取 Markdown 文本。

        兼容的返回格式可能为:
        1. {"markdown": "...", ...}
        2. {"md_content": "...", ...}
        3. {"content_list": [...], ...} (JSON 结构化)
        """
        # 优先取 markdown 或 md_content 字段
        md = result.get("markdown") or result.get("md_content") or ""
        if md:
            return md

        # 如果返回的是 content_list 结构化数据，拼接为 Markdown
        content_list = result.get("content_list", [])
        if content_list:
            return self._content_list_to_markdown(content_list)

        # 兜底：尝试直接转字符串
        return str(result) if result else ""

    def _content_list_to_markdown(self, content_list: list[dict]) -> str:
        """将 MinerU 的 content_list 结构化输出转为 Markdown。

        content_list 中每个元素的 type 可能为:
        - text: 普通文本
        - table: HTML 表格
        - image: 图片引用
        - equation/formula: LaTeX 公式
        """
        parts: list[str] = []
        for item in content_list:
            item_type = item.get("type", "text")
            content = item.get("content", "") or item.get("text", "")

            if item_type == "text":
                level = item.get("text_level")
                if isinstance(level, int) and level > 0:
                    parts.append(f"{'#' * min(level, 6)} {content}")
                else:
                    parts.append(content)
            elif item_type == "table":
                caption = " ".join(item.get("table_caption") or ())
                table_body = item.get("table_body") or content
                footnote = " ".join(item.get("table_footnote") or ())
                parts.append(
                    "\n\n".join(part for part in (caption, table_body, footnote) if part)
                )
            elif item_type in {"image", "chart"}:
                img_path = item.get("img_path", "")
                captions = item.get("image_caption") or item.get("chart_caption") or ()
                footnotes = item.get("image_footnote") or item.get("chart_footnote") or ()
                img_caption = " ".join(captions) if captions else "图片"
                if img_path:
                    parts.append(f"![{img_caption}]({img_path})")
                else:
                    parts.append(f"> [图片] {img_caption}")
                if item_type == "chart" and content:
                    parts.append(str(content))
                if footnotes:
                    parts.append(" ".join(str(footnote) for footnote in footnotes))
            elif item_type in ("equation", "formula"):
                # 行内或独立公式
                if "\n" in content or len(content) > 80:
                    parts.append(f"$$\n{content}\n$$")
                else:
                    parts.append(f"${content}$")
            elif item_type == "code":
                code_body = str(item.get("code_body") or content)
                caption = " ".join(item.get("code_caption") or ())
                footnote = " ".join(item.get("code_footnote") or ())
                parts.append(
                    "\n\n".join(
                        part for part in (caption, f"```\n{code_body}\n```", footnote) if part
                    )
                )
            elif item_type == "list":
                parts.append(
                    "\n".join(
                        f"- {list_item}" for list_item in (item.get("list_items") or ())
                    )
                )
            elif item_type not in {
                "header",
                "footer",
                "page_number",
                "aside_text",
                "page_footnote",
            }:
                parts.append(content)

        return "\n\n".join(parts)

    def _extract_assets(self, result: dict) -> list[PdfBinaryAsset]:
        """从 MinerU API 返回中提取已生成的图片资产。

        MinerU 通常会把图片保存到本地目录或返回 Base64。
        这里提取 content_list 中的 image 类型项。
        由于 MinerU 云端结果通常已在 Markdown 中保留图片路径引用，
        无需单独收集二进制资产——图片引用保留在 Markdown 文本中即可。
        """
        # MinerU 云端模式下，图片以 URL/路径形式直接内联在 Markdown 中，
        # 不需要额外收集 PdfBinaryAsset。
        # 如果需要将图片上传到自有对象存储，应在 service.py 层通过正则匹配图片链接后处理。
        return []
