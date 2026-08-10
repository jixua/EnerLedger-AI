"""从单表 ``llm_config`` 精确读取可执行模型快照。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.rag.core.llm.runtime_config import RuntimeModelConfig
from app.rag.models.db_models import LLMModelConfigDB


class RuntimeConfigRepository:
    """MySQL 直接读取；当前最小版本不引入 Redis 缓存或默认配置表。"""

    def __init__(self, db: AsyncSession | None = None, **_compat) -> None:
        self._db = db

    @staticmethod
    def _project(row: LLMModelConfigDB) -> RuntimeModelConfig:
        return RuntimeModelConfig(
            configId=row.id,
            scope=row.scope.upper(),
            ownerUserId=row.owner_user_id,
            providerId=row.provider_id,
            providerType=row.provider_type,
            modelName=row.model_name,
            displayName=row.display_name,
            capability=row.capability.upper(),
            protocol=row.protocol,
            apiBaseUrl=row.api_base_url,
            apiKeyCiphertext=row.api_key,
            isActive=row.is_active,
            snapshotVersion=row.snapshot_version,
        )

    async def _load(self, db: AsyncSession, config_id: int) -> RuntimeModelConfig | None:
        row = (
            await db.execute(select(LLMModelConfigDB).where(LLMModelConfigDB.id == config_id))
        ).scalar_one_or_none()
        return self._project(row) if row is not None else None

    async def get(self, config_id: int) -> RuntimeModelConfig | None:
        config_id = int(config_id)
        if config_id <= 0:
            return None
        if self._db is not None:
            return await self._load(self._db, config_id)

        from app.rag.database import get_async_session_factory

        async with get_async_session_factory()() as db:
            return await self._load(db, config_id)
