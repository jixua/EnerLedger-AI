from __future__ import annotations

import os
from types import SimpleNamespace
from uuid import UUID

import pytest

os.environ.setdefault("ADMIN_PASSWORD_HASH", "scrypt:test-only")

from app.rag.core.storage.qdrant.models import IndexedPoint
from app.rag.core.storage.qdrant.qdrant_store import QdrantIndexStore


class _FakeQdrantClient:
    def __init__(self, existing_ids: list[object]) -> None:
        self.existing_ids = existing_ids
        self.upsert_calls: list[dict[str, object]] = []

    async def retrieve(self, **_: object) -> list[SimpleNamespace]:
        return [SimpleNamespace(id=point_id) for point_id in self.existing_ids]

    async def upsert(self, **kwargs: object) -> None:
        self.upsert_calls.append(kwargs)


def _point(chunk_id: str) -> IndexedPoint:
    return IndexedPoint(
        chunk_id=chunk_id,
        vector=[0.1, 0.2],
        payload={"user_id": 1, "set_id": 2, "doc_id": 3},
    )


@pytest.mark.asyncio
async def test_ensure_points_treats_sdk_uuid_as_existing_string_id() -> None:
    chunk_id = "2ec3cd5b-7c7c-4e5b-9d72-6baedb55b420"
    client = _FakeQdrantClient([UUID(chunk_id)])
    store = QdrantIndexStore(client=client, collection_name="chunks")

    await store.ensure_points(points=[_point(chunk_id)])

    assert client.upsert_calls == []


@pytest.mark.asyncio
async def test_ensure_points_upserts_only_truly_missing_ids() -> None:
    existing_id = "2ec3cd5b-7c7c-4e5b-9d72-6baedb55b420"
    missing_id = "7c031cd1-ae91-4ae0-a8aa-1f1e64eeb09b"
    client = _FakeQdrantClient([UUID(existing_id)])
    store = QdrantIndexStore(client=client, collection_name="chunks")

    await store.ensure_points(points=[_point(existing_id), _point(missing_id)])

    assert len(client.upsert_calls) == 1
    created = client.upsert_calls[0]["points"]
    assert [point.id for point in created] == [missing_id]
    assert created[0].vector == {}
