from __future__ import annotations

from collections import deque
from datetime import UTC, datetime

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from app.api.datasets import delete_dataset, update_dataset
from app.api.llm import delete_config, update_config
from app.domain.models import Dataset, Document
from app.domain.schemas import DatasetUpdate, LLMConfigUpdate
from app.rag.core.llm.encryption import decrypt_api_key, encrypt_api_key
from app.rag.models.db_models import LLMModelConfigDB


class _FakeScalarResult:
    def __init__(self, values):
        self.values = values

    def all(self):
        return list(self.values)


class _FakeSession:
    def __init__(
        self,
        *,
        scalar_values=(),
        scalar_lists=(),
        models=None,
        commit_error=None,
    ):
        self.scalar_values = deque(scalar_values)
        self.scalar_lists = deque(scalar_lists)
        self.models = dict(models or {})
        self.commit_error = commit_error
        self.statements = []
        self.deleted = []
        self.commits = 0
        self.rollbacks = 0
        self.refreshes = 0

    async def scalar(self, statement):
        self.statements.append(statement)
        return self.scalar_values.popleft()

    async def get(self, model, model_id):
        return self.models.get((model, model_id))

    async def scalars(self, statement):
        self.statements.append(statement)
        return _FakeScalarResult(self.scalar_lists.popleft() if self.scalar_lists else [])

    async def commit(self):
        self.commits += 1
        if self.commit_error is not None:
            raise self.commit_error

    async def rollback(self):
        self.rollbacks += 1

    async def refresh(self, _model):
        self.refreshes += 1

    async def delete(self, model):
        self.deleted.append(model)


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _dataset(*, dataset_id: int = 7, user_id: int = 11) -> Dataset:
    return Dataset(
        id=dataset_id,
        user_id=user_id,
        name="旧名称",
        description="旧描述",
        status="ACTIVE",
        dense_embedding_config_id=101,
        sparse_embedding_config_id=102,
        chat_config_id=103,
        vision_config_id=104,
        created_at=_now(),
        updated_at=_now(),
    )


def _config(
    *,
    config_id: int = 101,
    user_id: int = 11,
    capability: str = "EMBEDDING",
) -> LLMModelConfigDB:
    return LLMModelConfigDB(
        id=config_id,
        scope="USER",
        owner_user_id=user_id,
        provider_id=1,
        provider_type="qwen",
        model_name="old-model",
        display_name="旧模型",
        capability=capability,
        protocol="openai",
        api_base_url="https://example.com/v1",
        api_key=encrypt_api_key("sk-old-secret-12345678"),
        is_active=True,
        snapshot_version=3,
        created_at=_now(),
        updated_at=_now(),
    )


def _document(*, status: str = "READY", version: int = 1) -> Document:
    return Document(
        id=91,
        dataset_id=7,
        user_id=11,
        filename="report.pdf",
        file_type="pdf",
        file_size=100,
        content_type="application/pdf",
        raw_bucket="raw",
        raw_object_key="raw/report.pdf",
        parser_backend="opendataloader",
        status=status,
        version=version,
        attempt_count=1,
        reparse_requested=False,
        created_at=_now(),
        updated_at=_now(),
    )


def test_patch_schemas_reject_empty_payload_and_null_required_fields() -> None:
    with pytest.raises(ValidationError):
        DatasetUpdate.model_validate({})
    with pytest.raises(ValidationError):
        DatasetUpdate.model_validate({"dense_embedding_config_id": None})
    with pytest.raises(ValidationError):
        LLMConfigUpdate.model_validate({})
    with pytest.raises(ValidationError):
        LLMConfigUpdate.model_validate({"api_key": None})


@pytest.mark.asyncio
async def test_dataset_patch_updates_metadata_and_validated_model_bindings() -> None:
    dataset = _dataset()
    dense = _config(config_id=201, capability="EMBEDDING")
    sparse = _config(config_id=202, capability="SPARSE_EMBEDDING")
    chat = _config(config_id=203, capability="CHAT")
    vision = _config(config_id=204, capability="VISION")
    db = _FakeSession(
        scalar_values=[dataset],
        scalar_lists=[[dense, sparse, chat, vision], []],
    )
    payload = DatasetUpdate.model_validate(
        {
            "name": "  新名称  ",
            "description": "  新描述  ",
            "dense_embedding_config_id": 201,
            "sparse_embedding_config_id": 202,
            "chat_config_id": 203,
            "vision_config_id": 204,
        }
    )

    response = await update_dataset(7, payload, 11, db)

    assert response.name == "新名称"
    assert response.description == "新描述"
    assert response.dense_embedding_config_id == 201
    assert response.sparse_embedding_config_id == 202
    assert response.chat_config_id == 203
    assert response.vision_config_id == 204
    assert db.commits == 1
    assert db.refreshes == 1
    assert "llm_config.id" in str(db.statements[0])
    assert "FOR UPDATE" in str(db.statements[0])
    assert "dataset.user_id" in str(db.statements[1])


@pytest.mark.asyncio
async def test_dataset_embedding_rebind_requeues_terminal_documents_as_new_versions() -> None:
    dataset = _dataset()
    dense = _config(config_id=201, capability="EMBEDDING")
    document = _document(status="READY", version=2)
    db = _FakeSession(
        scalar_values=[dataset],
        scalar_lists=[[dense], [document]],
    )

    response = await update_dataset(
        7,
        DatasetUpdate.model_validate({"dense_embedding_config_id": 201}),
        11,
        db,
    )

    assert response.dense_embedding_config_id == 201
    assert document.status == "QUEUED"
    assert document.version == 3
    assert document.reparse_requested is True
    assert document.attempt_count == 0


@pytest.mark.asyncio
async def test_dataset_embedding_rebind_rejects_active_documents() -> None:
    dataset = _dataset()
    dense = _config(config_id=201, capability="EMBEDDING")
    document = _document(status="PROCESSING")
    db = _FakeSession(
        scalar_values=[dataset],
        scalar_lists=[[dense], [document]],
    )

    with pytest.raises(HTTPException) as exc_info:
        await update_dataset(
            7,
            DatasetUpdate.model_validate({"dense_embedding_config_id": 201}),
            11,
            db,
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "DATASET_DOCUMENTS_ACTIVE"
    assert db.commits == 0


@pytest.mark.asyncio
async def test_dataset_vision_rebind_validates_capability_and_requeues_documents() -> None:
    dataset = _dataset()
    vision = _config(config_id=204, capability="VISION")
    document = _document(status="FAILED", version=4)
    db = _FakeSession(
        scalar_values=[dataset],
        scalar_lists=[[vision], [document]],
    )

    response = await update_dataset(
        7,
        DatasetUpdate.model_validate({"vision_config_id": 204}),
        11,
        db,
    )

    assert response.vision_config_id == 204
    assert document.status == "QUEUED"
    assert document.version == 5
    assert document.reparse_requested is True


@pytest.mark.asyncio
async def test_dataset_vision_rebind_rejects_non_vision_config() -> None:
    dataset = _dataset()
    chat = _config(config_id=204, capability="CHAT")
    db = _FakeSession(
        scalar_values=[dataset],
        scalar_lists=[[chat]],
    )

    with pytest.raises(HTTPException) as exc_info:
        await update_dataset(
            7,
            DatasetUpdate.model_validate({"vision_config_id": 204}),
            11,
            db,
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == {
        "code": "LLM_CONFIG_CAPABILITY_MISMATCH",
        "field": "vision_config_id",
        "expected": "VISION",
        "actual": "CHAT",
    }
    assert db.commits == 0


@pytest.mark.asyncio
async def test_dataset_patch_can_unbind_vision_and_requeue_terminal_documents() -> None:
    dataset = _dataset()
    document = _document(status="READY", version=2)
    db = _FakeSession(
        scalar_values=[dataset],
        scalar_lists=[[document]],
    )

    response = await update_dataset(
        7,
        DatasetUpdate.model_validate({"vision_config_id": None}),
        11,
        db,
    )

    assert response.vision_config_id is None
    assert document.status == "QUEUED"
    assert document.version == 3
    assert document.reparse_requested is True


@pytest.mark.asyncio
async def test_dataset_patch_can_unbind_chat_without_revalidating_other_models() -> None:
    dataset = _dataset()
    db = _FakeSession(scalar_values=[dataset])

    response = await update_dataset(
        7,
        DatasetUpdate.model_validate({"chat_config_id": None}),
        11,
        db,
    )

    assert response.chat_config_id is None
    assert db.models == {}
    assert db.commits == 1


@pytest.mark.asyncio
async def test_dataset_patch_rejects_a_model_binding_owned_by_another_tenant() -> None:
    dataset = _dataset()
    foreign_dense = _config(config_id=201, user_id=12, capability="EMBEDDING")
    db = _FakeSession(
        scalar_values=[dataset],
        scalar_lists=[[foreign_dense]],
    )

    with pytest.raises(HTTPException) as exc_info:
        await update_dataset(
            7,
            DatasetUpdate.model_validate({"dense_embedding_config_id": 201}),
            11,
            db,
        )

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail["code"] == "LLM_CONFIG_FORBIDDEN"
    assert db.commits == 0


@pytest.mark.asyncio
async def test_dataset_patch_preserves_tenant_boundary_and_rolls_back_name_conflict() -> None:
    forbidden_db = _FakeSession(scalar_values=[None])
    with pytest.raises(HTTPException) as exc_info:
        await update_dataset(
            7,
            DatasetUpdate.model_validate({"name": "不可见"}),
            12,
            forbidden_db,
        )
    assert exc_info.value.status_code == 404
    assert forbidden_db.commits == 0

    dataset = _dataset()
    conflict = IntegrityError("UPDATE dataset", {}, RuntimeError("duplicate"))
    conflict_db = _FakeSession(scalar_values=[dataset], commit_error=conflict)
    with pytest.raises(HTTPException) as conflict_info:
        await update_dataset(
            7,
            DatasetUpdate.model_validate({"name": "重复名称"}),
            11,
            conflict_db,
        )
    assert conflict_info.value.status_code == 409
    assert conflict_db.rollbacks == 1


@pytest.mark.asyncio
async def test_dataset_delete_requires_an_empty_owned_dataset(monkeypatch) -> None:
    dropped: list[tuple[int, int]] = []

    async def fake_drop(*, user_id: int, dataset_id: int) -> None:
        dropped.append((user_id, dataset_id))

    monkeypatch.setattr("app.api.datasets._drop_dataset_bm25_table", fake_drop)
    dataset = _dataset()
    in_use_db = _FakeSession(scalar_values=[dataset, 99])

    with pytest.raises(HTTPException) as exc_info:
        await delete_dataset(7, 11, in_use_db)

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "DATASET_NOT_EMPTY"
    assert in_use_db.deleted == []

    empty_db = _FakeSession(scalar_values=[dataset, None])
    assert await delete_dataset(7, 11, empty_db) is None
    assert dropped == [(11, 7)]
    assert empty_db.deleted == [dataset]
    assert empty_db.commits == 1


@pytest.mark.asyncio
async def test_dataset_delete_keeps_row_when_external_index_cleanup_fails(monkeypatch) -> None:
    async def fail_drop(*, user_id: int, dataset_id: int) -> None:
        raise ConnectionError(f"manticore unavailable for {user_id}/{dataset_id}")

    monkeypatch.setattr("app.api.datasets._drop_dataset_bm25_table", fail_drop)
    dataset = _dataset()
    db = _FakeSession(scalar_values=[dataset, None])

    with pytest.raises(HTTPException) as exc_info:
        await delete_dataset(7, 11, db)

    assert exc_info.value.status_code == 502
    assert exc_info.value.detail["code"] == "DATASET_INDEX_CLEANUP_FAILED"
    assert db.deleted == []
    assert db.commits == 0
    assert db.rollbacks == 1


@pytest.mark.asyncio
async def test_llm_patch_rotates_key_and_increments_snapshot_without_echoing_key() -> None:
    config = _config()
    db = _FakeSession(scalar_values=[config, None, None])
    new_secret = "sk-new-secret-abcdefgh"
    payload = LLMConfigUpdate.model_validate(
        {
            "model_name": "  new-model  ",
            "display_name": "  新模型  ",
            "api_base_url": "https://new.example.com/v1",
            "api_key": new_secret,
            "is_active": False,
        }
    )

    response = await update_config(101, payload, 11, db)

    assert config.model_name == "new-model"
    assert config.display_name == "新模型"
    assert config.api_base_url == "https://new.example.com/v1"
    assert decrypt_api_key(config.api_key) == new_secret
    assert config.is_active is False
    assert response.snapshot_version == 4
    assert new_secret not in response.model_dump_json()
    assert "api_key" not in response.model_dump()
    assert response.api_key_masked != new_secret
    assert db.commits == 1
    assert "llm_config.owner_user_id" in str(db.statements[0])


@pytest.mark.asyncio
async def test_llm_patch_blocks_deactivation_while_bound() -> None:
    config = _config(capability="CHAT")
    db = _FakeSession(scalar_values=[config, 7])

    with pytest.raises(HTTPException) as exc_info:
        await update_config(
            101,
            LLMConfigUpdate.model_validate({"is_active": False}),
            11,
            db,
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "LLM_CONFIG_IN_USE"
    assert db.commits == 0


@pytest.mark.asyncio
async def test_llm_patch_blocks_in_place_embedding_model_change_with_documents() -> None:
    config = _config(capability="EMBEDDING")
    db = _FakeSession(scalar_values=[config, 99])

    with pytest.raises(HTTPException) as exc_info:
        await update_config(
            101,
            LLMConfigUpdate.model_validate({"model_name": "new-embedding-model"}),
            11,
            db,
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "LLM_MODEL_REINDEX_REQUIRED"
    assert db.commits == 0


@pytest.mark.asyncio
async def test_llm_patch_preserves_tenant_boundary() -> None:
    db = _FakeSession(scalar_values=[None])

    with pytest.raises(HTTPException) as exc_info:
        await update_config(
            101,
            LLMConfigUpdate.model_validate({"display_name": "不可见"}),
            12,
            db,
        )

    assert exc_info.value.status_code == 404
    assert db.commits == 0


@pytest.mark.asyncio
async def test_llm_delete_is_blocked_while_any_dataset_uses_config() -> None:
    config = _config()
    in_use_db = _FakeSession(scalar_values=[config, 7])

    with pytest.raises(HTTPException) as exc_info:
        await delete_config(101, 11, in_use_db)

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "LLM_CONFIG_IN_USE"
    assert in_use_db.deleted == []

    unused_db = _FakeSession(scalar_values=[config, None])
    assert await delete_config(101, 11, unused_db) is None
    assert unused_db.deleted == [config]
    assert unused_db.commits == 1
