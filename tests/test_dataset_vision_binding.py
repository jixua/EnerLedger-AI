from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.domain.models import Dataset
from app.rag.core.dataset_config.execution_context import (
    DatasetExecutionContextLoader,
    DatasetExecutionPurpose,
)
from app.rag.core.dataset_config.models import (
    DatasetModelBindingConfig,
    DatasetParseConfigBundle,
    EnhancementConfig,
)
from app.rag.core.dataset_config.repository import DatasetParseConfigRepository


class _ConfigService:
    def __init__(self, config: DatasetParseConfigBundle) -> None:
        self.config = config

    async def get_config(self, _user_id: int, _dataset_id: int, _db):
        return self.config


def test_dataset_projection_maps_vision_binding_to_linkrag_execution_slot() -> None:
    dataset = Dataset(
        id=7,
        user_id=11,
        name="能碳资料",
        status="ACTIVE",
        dense_embedding_config_id=101,
        sparse_embedding_config_id=102,
        chat_config_id=103,
        vision_config_id=104,
    )

    snapshot = DatasetParseConfigRepository._project(dataset)

    assert snapshot.enhancement_vision_config_id == 104


@pytest.mark.asyncio
async def test_parse_context_resolves_bound_vision_when_image_enhancement_is_disabled(
    monkeypatch,
) -> None:
    config = DatasetParseConfigBundle(
        enhancement=EnhancementConfig(
            enable_table_enhancement=False,
            enable_image_enhancement=False,
            enable_heading_hierarchy=False,
        ),
        model_bindings=DatasetModelBindingConfig(
            dense_embedding_config_id=101,
            sparse_embedding_config_id=102,
            enhancement_vision_config_id=104,
        ),
    )
    resolved_calls: list[tuple[int, str]] = []

    async def fake_resolve_model(*, config_id: int, capability: str, **_kwargs):
        resolved_calls.append((config_id, capability))
        return SimpleNamespace(config_id=config_id, provider=object())

    monkeypatch.setattr(
        "app.rag.core.dataset_config.execution_context.aresolve_model",
        fake_resolve_model,
    )
    loader = DatasetExecutionContextLoader(
        db=object(),
        config_service=_ConfigService(config),
        repository=object(),
    )

    context = await loader.load(11, 7, DatasetExecutionPurpose.PARSE)

    assert resolved_calls == [
        (101, "EMBEDDING"),
        (102, "SPARSE_EMBEDDING"),
        (104, "VISION"),
    ]
    assert context.enhancement_vision is not None
    assert context.enhancement_vision.config_id == 104
