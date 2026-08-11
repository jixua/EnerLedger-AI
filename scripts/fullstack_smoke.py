"""真实 API 全栈冒烟：控制面默认执行，传 FULLSTACK_SMOKE_FILE 时覆盖解析与对话链路。"""

from __future__ import annotations

import json
import mimetypes
import os
import sys
import time
from pathlib import Path
from uuid import uuid4

import httpx

BASE_URL = os.getenv("FULLSTACK_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
ADMIN_USERNAME = os.getenv("FULLSTACK_ADMIN_USERNAME", "root")
ADMIN_PASSWORD = os.getenv("FULLSTACK_ADMIN_PASSWORD", "")
POLL_TIMEOUT = int(os.getenv("FULLSTACK_POLL_TIMEOUT_SECONDS", "420"))
QUERY = os.getenv("FULLSTACK_SMOKE_QUERY", "天然气燃烧排放如何核算？").strip()
HEADERS = {"Accept": "application/json"}


def require(response: httpx.Response) -> httpx.Response:
    if response.is_error:
        raise RuntimeError(f"HTTP {response.status_code}: {response.text[:500]}")
    return response


def active_model_id(models: list[dict], capability: str) -> int:
    for model in models:
        if model.get("is_active") and model.get("capability") == capability:
            return int(model["id"])
    raise RuntimeError(f"缺少启用的 {capability} 模型配置")


def optional_active_model_id(models: list[dict], capability: str) -> int | None:
    for model in models:
        if model.get("is_active") and model.get("capability") == capability:
            return int(model["id"])
    return None


def consume_sse(response: httpx.Response) -> list[str]:
    events: list[str] = []
    current_event = "message"
    for line in response.iter_lines():
        if line.startswith("event:"):
            current_event = line.removeprefix("event:").strip()
        elif line.startswith("data:"):
            json.loads(line.removeprefix("data:").strip())
            events.append(current_event)
    return events


def main() -> int:
    dataset_id: int | None = None
    document_id: int | None = None
    document_terminal = False
    with httpx.Client(timeout=60.0, headers=HEADERS) as client:
        require(client.get(f"{BASE_URL}/health/live", headers={"Accept": "application/json"}))
        if not ADMIN_PASSWORD:
            raise RuntimeError("必须通过 FULLSTACK_ADMIN_PASSWORD 提供管理员密码")
        token = require(
            client.post(
                f"{BASE_URL}/api/v1/auth/login",
                json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
            )
        ).json()["access_token"]
        client.headers["Authorization"] = f"Bearer {token}"
        system = require(client.get(f"{BASE_URL}/api/v1/system/status")).json()
        if system.get("status") not in {"ok", "degraded"}:
            raise RuntimeError(f"系统状态异常: {system.get('status')}")

        models = require(client.get(f"{BASE_URL}/api/v1/llm/configs")).json()
        dense_id = active_model_id(models, "EMBEDDING")
        sparse_id = active_model_id(models, "SPARSE_EMBEDDING")
        chat_id = active_model_id(models, "CHAT")
        vision_id = optional_active_model_id(models, "VISION")
        dataset_name = f"前后端联调-{uuid4().hex[:8]}"

        try:
            dataset_payload = {
                "name": dataset_name,
                "description": "自动化全栈联调临时数据集",
                "dense_embedding_config_id": dense_id,
                "sparse_embedding_config_id": sparse_id,
                "chat_config_id": chat_id,
            }
            if vision_id is not None:
                dataset_payload["vision_config_id"] = vision_id
            dataset = require(
                client.post(
                    f"{BASE_URL}/api/v1/datasets",
                    json=dataset_payload,
                )
            ).json()
            dataset_id = int(dataset["id"])
            updated = require(
                client.patch(
                    f"{BASE_URL}/api/v1/datasets/{dataset_id}",
                    json={"description": "控制面 CRUD 已通过，准备验证数据面"},
                )
            ).json()
            if updated.get("description") != "控制面 CRUD 已通过，准备验证数据面":
                raise RuntimeError("数据集 PATCH 结果不一致")

            queue_documents = require(client.get(f"{BASE_URL}/api/v1/documents")).json()
            if any(int(item["dataset_id"]) == dataset_id for item in queue_documents):
                raise RuntimeError("临时数据集创建后不应已有文档")

            file_value = os.getenv("FULLSTACK_SMOKE_FILE")
            if not file_value:
                print(f"control-plane: passed; dataset={dataset_id}")
                return 0

            file_path = Path(file_value).expanduser().resolve()
            if not file_path.is_file():
                raise RuntimeError(f"联调文件不存在: {file_path}")
            with file_path.open("rb") as source:
                content_type = (
                    mimetypes.guess_type(file_path.name)[0]
                    or "application/octet-stream"
                )
                uploaded = require(
                    client.post(
                        f"{BASE_URL}/api/v1/datasets/{dataset_id}/documents",
                        files={"file": (file_path.name, source, content_type)},
                    )
                ).json()
            document_id = int(uploaded["document_id"])
            if uploaded.get("status") != "QUEUED":
                raise RuntimeError(f"上传后状态应为 QUEUED，实际为 {uploaded.get('status')}")

            deadline = time.monotonic() + POLL_TIMEOUT
            status = uploaded["status"]
            current: dict = uploaded
            while time.monotonic() < deadline:
                current = require(client.get(f"{BASE_URL}/api/v1/documents/{document_id}")).json()
                status = current["status"]
                if status in {"READY", "FAILED"}:
                    document_terminal = True
                    break
                time.sleep(2)
            if status != "READY":
                quality = current.get("parse_quality") or {}
                diagnostics = {
                    "status": status,
                    "error_code": current.get("error_code"),
                    "error_message": current.get("error_message"),
                    "quality_status": current.get("parse_quality_status"),
                    "quality_warnings": quality.get("warnings"),
                    "ocr_pages": quality.get("ocr_pages"),
                    "low_confidence_pages": quality.get("low_confidence_pages"),
                }
                raise RuntimeError(
                    "文档未成功完成解析："
                    + json.dumps(diagnostics, ensure_ascii=False, separators=(",", ":"))
                )

            recall = require(
                client.post(
                    f"{BASE_URL}/api/v1/recall",
                    json={
                        "query": QUERY,
                        "dataset_ids": [dataset_id],
                        "include_content": True,
                    },
                )
            ).json()
            if not recall.get("hits"):
                raise RuntimeError("真实三路召回未返回命中")

            with client.stream(
                "POST",
                f"{BASE_URL}/api/v1/rag/stream",
                headers={"Accept": "text/event-stream"},
                json={"query": QUERY, "dataset_ids": [dataset_id]},
                timeout=420.0,
            ) as response:
                require(response)
                events = consume_sse(response)
            if "recall_done" not in events or "answer_done" not in events:
                raise RuntimeError(f"SSE 缺少终态事件: {events}")

            print(f"data-plane: passed; dataset={dataset_id}; document={document_id}")
            return 0
        finally:
            if document_id is not None and document_terminal:
                cleanup = client.delete(f"{BASE_URL}/api/v1/documents/{document_id}")
                if cleanup.status_code != 204:
                    print(
                        f"warning: document cleanup failed HTTP {cleanup.status_code}",
                        file=sys.stderr,
                    )
            if dataset_id is not None:
                cleanup = client.delete(f"{BASE_URL}/api/v1/datasets/{dataset_id}")
                if cleanup.status_code != 204:
                    print(
                        f"warning: dataset cleanup failed HTTP {cleanup.status_code}",
                        file=sys.stderr,
                    )


if __name__ == "__main__":
    raise SystemExit(main())
