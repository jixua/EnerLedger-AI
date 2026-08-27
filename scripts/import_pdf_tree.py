"""将本地 PDF 目录树可恢复地导入能碳会计数据集。"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import httpx

DEFAULT_DATASET_NAME = "能碳数据集"
DEFAULT_MAX_UPLOAD_BYTES = 128 * 1024 * 1024


@dataclass(frozen=True)
class PdfEntry:
    path: Path
    relative_path: PurePosixPath
    upload_name: str
    size: int
    content_type: str = "application/pdf"


def _upload_names(paths: list[Path], root: Path) -> dict[Path, str]:
    """仅为同名项生成稳定唯一名，不改动源文件。"""

    counts = Counter(path.name.casefold() for path in paths)
    used: set[str] = set()
    result: dict[Path, str] = {}
    for path in paths:
        candidate = path.name
        if counts[path.name.casefold()] > 1:
            parent_label = "-".join(path.parent.relative_to(root).parts)
            candidate = f"{path.stem}（{parent_label}）{path.suffix.lower()}"
        base = candidate
        suffix = 2
        while candidate.casefold() in used:
            candidate = f"{Path(base).stem}-{suffix}{Path(base).suffix}"
            suffix += 1
        if len(candidate) > 255:
            candidate = f"{Path(candidate).stem[: 254 - len(path.suffix)]}{path.suffix.lower()}"
        used.add(candidate.casefold())
        result[path] = candidate
    return result


def scan_pdf_tree(root: Path, *, max_upload_bytes: int) -> tuple[list[PdfEntry], list[Path]]:
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"目录不存在: {root}")
    paths = sorted(
        (path for path in root.rglob("*") if path.is_file() and path.suffix.lower() == ".pdf"),
        key=lambda path: path.relative_to(root).as_posix().casefold(),
    )
    upload_names = _upload_names(paths, root)
    entries: list[PdfEntry] = []
    oversized: list[tuple[Path, int]] = []
    invalid: list[Path] = []
    for path in paths:
        size = path.stat().st_size
        if size > max_upload_bytes:
            oversized.append((path, size))
        with path.open("rb") as source:
            header = source.read(512).lstrip()
        content_type = "application/pdf"
        upload_name = upload_names[path]
        if not header.startswith(b"%PDF-"):
            if header.lower().startswith((b"<!doctype html", b"<html")):
                upload_name = f"{Path(upload_name).stem}.html"
                content_type = "text/html"
            else:
                invalid.append(path)
                continue
        entries.append(
            PdfEntry(
                path=path,
                relative_path=PurePosixPath(path.relative_to(root).as_posix()),
                upload_name=upload_name,
                size=size,
                content_type=content_type,
            )
        )
    if oversized:
        names = "\n".join(f"- {path} ({size} bytes)" for path, size in oversized)
        raise ValueError(f"以下文件超过上传上限 {max_upload_bytes} bytes:\n{names}")
    return entries, invalid


class ApiClient:
    def __init__(self, base_url: str, token: str, *, timeout_seconds: float) -> None:
        self.client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=httpx.Timeout(timeout_seconds, connect=20.0),
        )

    def close(self) -> None:
        self.client.close()

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self.client.request(method, path, **kwargs)
        if response.is_error:
            try:
                detail = response.json()
            except ValueError:
                detail = response.text
            raise RuntimeError(f"{method} {path} -> {response.status_code}: {detail}")
        if response.status_code == 204:
            return None
        return response.json()


def authenticate(base_url: str, username: str, password: str, timeout_seconds: float) -> str:
    with httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout_seconds) as client:
        response = client.post(
            "/api/v1/auth/login",
            json={"username": username, "password": password},
        )
        response.raise_for_status()
        return str(response.json()["access_token"])


def _dataset_payload(client: ApiClient, args: argparse.Namespace) -> dict[str, Any]:
    if args.template_dataset_id is not None:
        template = client.request("GET", f"/api/v1/datasets/{args.template_dataset_id}")
        return {
            "name": args.dataset_name,
            "description": args.description,
            "dense_embedding_config_id": template["dense_embedding_config_id"],
            "sparse_embedding_config_id": template["sparse_embedding_config_id"],
            "chat_config_id": template.get("chat_config_id"),
            "vision_config_id": template.get("vision_config_id"),
        }
    if args.dense_model_id is None or args.sparse_model_id is None:
        raise ValueError(
            "新建数据集需要 --template-dataset-id，"
            "或同时提供 --dense-model-id 和 --sparse-model-id"
        )
    return {
        "name": args.dataset_name,
        "description": args.description,
        "dense_embedding_config_id": args.dense_model_id,
        "sparse_embedding_config_id": args.sparse_model_id,
        "chat_config_id": args.chat_model_id,
        "vision_config_id": args.vision_model_id,
    }


def ensure_dataset(client: ApiClient, args: argparse.Namespace) -> dict[str, Any]:
    datasets = client.request("GET", "/api/v1/datasets")
    existing = next((item for item in datasets if item.get("name") == args.dataset_name), None)
    if existing is not None:
        return existing
    return client.request("POST", "/api/v1/datasets", json=_dataset_payload(client, args))


def ensure_folders(
    client: ApiClient,
    dataset_id: int,
    entries: list[PdfEntry],
) -> dict[PurePosixPath, int]:
    existing = client.request("GET", f"/api/v1/datasets/{dataset_id}/folders")
    by_key = {(item.get("parent_id"), item["name"]): item for item in existing}
    ids: dict[PurePosixPath, int] = {}
    folder_paths = sorted(
        {
            parent
            for entry in entries
            for parent in entry.relative_path.parents
            if str(parent) != "."
        },
        key=lambda path: (len(path.parts), path.as_posix().casefold()),
    )
    for folder_path in folder_paths:
        parent_path = folder_path.parent
        parent_id = None if str(parent_path) == "." else ids[parent_path]
        key = (parent_id, folder_path.name)
        folder = by_key.get(key)
        if folder is None:
            folder = client.request(
                "POST",
                f"/api/v1/datasets/{dataset_id}/folders",
                json={"name": folder_path.name, "parent_id": parent_id},
            )
            by_key[key] = folder
        ids[folder_path] = int(folder["id"])
    return ids


def import_entries(
    client: ApiClient,
    dataset_id: int,
    entries: list[PdfEntry],
    folder_ids: dict[PurePosixPath, int],
) -> tuple[int, int]:
    documents = client.request("GET", f"/api/v1/datasets/{dataset_id}/documents")
    existing = {str(item.get("filename", "")).casefold(): item for item in documents}
    uploaded = 0
    skipped = 0
    for index, entry in enumerate(entries, start=1):
        key = entry.upload_name.casefold()
        folder_id = folder_ids[entry.relative_path.parent]
        if key in existing:
            document = existing[key]
            if int(document.get("folder_id") or 0) != folder_id:
                client.request(
                    "PATCH",
                    f"/api/v1/documents/{document.get('document_id') or document.get('id')}",
                    json={"folder_id": folder_id},
                )
            skipped += 1
            print(f"[{index}/{len(entries)}] 跳过已存在: {entry.relative_path}")
            continue
        with entry.path.open("rb") as source:
            client.request(
                "POST",
                f"/api/v1/datasets/{dataset_id}/documents",
                data={"folder_id": str(folder_id)},
                files={
                    "file": (
                        entry.upload_name,
                        source,
                        entry.content_type,
                    )
                },
            )
        uploaded += 1
        print(f"[{index}/{len(entries)}] 已提交: {entry.relative_path} -> {entry.upload_name}")
    return uploaded, skipped


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, help="包含 PDF 的本地根目录")
    parser.add_argument("--api-base", default=os.getenv("ENERLEDGER_API_BASE", "http://127.0.0.1:8000"))
    parser.add_argument("--dataset-name", default=DEFAULT_DATASET_NAME)
    parser.add_argument("--description", default="能碳会计与碳管理分层资料库")
    parser.add_argument("--token", default=os.getenv("ENERLEDGER_ACCESS_TOKEN", ""))
    parser.add_argument("--username", default=os.getenv("ENERLEDGER_ADMIN_USERNAME", "admin"))
    parser.add_argument("--password", default=os.getenv("ENERLEDGER_ADMIN_PASSWORD", ""))
    parser.add_argument("--template-dataset-id", type=int)
    parser.add_argument("--dense-model-id", type=int)
    parser.add_argument("--sparse-model-id", type=int)
    parser.add_argument("--chat-model-id", type=int)
    parser.add_argument("--vision-model-id", type=int)
    parser.add_argument("--max-upload-bytes", type=int, default=DEFAULT_MAX_UPLOAD_BYTES)
    parser.add_argument("--timeout", type=float, default=900.0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--strict", action="store_true", help="发现损坏或未知格式时终止")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        entries, invalid = scan_pdf_tree(args.root, max_upload_bytes=args.max_upload_bytes)
        if invalid and args.strict:
            names = "\n".join(f"- {path}" for path in invalid)
            raise ValueError(f"以下文件不是可用 PDF/HTML：\n{names}")
        manifest = {
            "root": str(args.root.expanduser().resolve()),
            "dataset_name": args.dataset_name,
            "source_pdf_candidates": len(entries) + len(invalid),
            "importable_count": len(entries),
            "pdf_count": sum(entry.content_type == "application/pdf" for entry in entries),
            "total_bytes": sum(entry.size for entry in entries),
            "folder_count": len({entry.relative_path.parent for entry in entries}),
            "renamed_count": sum(entry.path.name != entry.upload_name for entry in entries),
            "html_recovered_count": sum(entry.content_type == "text/html" for entry in entries),
            "invalid_skipped_count": len(invalid),
            "invalid_skipped": [str(path) for path in invalid],
        }
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
        if args.dry_run:
            return 0
        token = args.token or authenticate(
            args.api_base,
            args.username,
            args.password,
            args.timeout,
        )
        client = ApiClient(args.api_base, token, timeout_seconds=args.timeout)
        try:
            dataset = ensure_dataset(client, args)
            folder_ids = ensure_folders(client, int(dataset["id"]), entries)
            uploaded, skipped = import_entries(client, int(dataset["id"]), entries, folder_ids)
        finally:
            client.close()
        print(
            json.dumps(
                {"dataset_id": dataset["id"], "uploaded": uploaded, "skipped": skipped},
                ensure_ascii=False,
            )
        )
        return 0
    except (ValueError, RuntimeError, httpx.HTTPError) as exc:
        print(f"导入失败: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
