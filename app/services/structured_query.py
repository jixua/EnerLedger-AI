"""Allowlisted DuckDB queries over published Parquet tables."""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import (
    StructuredAsset,
    StructuredAssetVersion,
    StructuredQueryAudit,
    StructuredTable,
    StructuredTermAlias,
)
from app.rag.config import settings
from app.rag.services.storage.base import BaseObjectStorage
from app.rag.services.storage.factory import StorageFactory


@dataclass(frozen=True)
class StructuredFactorQuery:
    dataset_ids: list[int]
    edition_year: int | None = None
    table_code: str | None = None
    activity: str | None = None
    gas: str | None = None
    region_code: str | None = None
    factor_basis: str | None = None
    denominator_unit: str | None = None
    limit: int = 20

    def audit_payload(self) -> dict[str, Any]:
        return {
            "dataset_ids": self.dataset_ids,
            "edition_year": self.edition_year,
            "table_code": self.table_code,
            "activity": self.activity,
            "gas": self.gas,
            "region_code": self.region_code,
            "factor_basis": self.factor_basis,
            "denominator_unit": self.denominator_unit,
            "limit": self.limit,
        }


class StructuredQueryService:
    def __init__(self, storage: BaseObjectStorage | None = None) -> None:
        self._storage = storage or StorageFactory.get_storage()
        self._cache_dir = Path(settings.STRUCTURED_DATA_CACHE_DIR)

    async def query(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        query: StructuredFactorQuery,
    ) -> list[dict[str, Any]]:
        started = time.monotonic()
        audit = StructuredQueryAudit(
            user_id=user_id,
            dataset_ids=query.dataset_ids,
            request_payload=query.audit_payload(),
            status="RUNNING",
            result_count=0,
        )
        db.add(audit)
        try:
            tables = await self._authorized_tables(db, user_id=user_id, query=query)
            local_files = await asyncio.gather(
                *(self._materialize(table) for table in tables)
            )
            rows = await asyncio.to_thread(self._execute, local_files, query)
            audit.status = "SUCCESS"
            audit.result_count = len(rows)
            audit.elapsed_ms = int((time.monotonic() - started) * 1000)
            await db.commit()
            return rows
        except Exception as exc:
            audit.status = "FAILED"
            audit.error_message = str(exc)[:2000]
            audit.elapsed_ms = int((time.monotonic() - started) * 1000)
            await db.commit()
            raise

    async def query_natural_language(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        dataset_ids: list[int],
        text: str,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Conservatively map a factor question to the allowlisted query contract."""

        lowered = text.lower()
        if not any(token in lowered for token in ("排放因子", "因子", "gwp", "emission factor")):
            return []
        year_match = re.search(r"(?<!\d)(20\d{2})(?!\d)", text)
        edition_year = int(year_match.group(1)) if year_match else None
        aliases = list(
            (
                await db.scalars(
                    select(StructuredTermAlias).where(
                        StructuredTermAlias.user_id == user_id,
                        StructuredTermAlias.dataset_id.in_(dataset_ids),
                    )
                )
            ).all()
        )
        matches = sorted(
            (alias for alias in aliases if alias.term.lower() in lowered),
            key=lambda item: len(item.term),
            reverse=True,
        )
        activity = next(
            (item.canonical_value for item in matches if item.field_name == "activity_name"),
            None,
        )
        gas = next(
            (item.canonical_value for item in matches if item.field_name == "gas"),
            None,
        )
        if gas is None:
            for candidate in ("CO2E", "CO2", "CH4", "N2O", "HFC", "PFC", "SF6"):
                if candidate.lower() in lowered:
                    gas = candidate
                    break
        table_code = None
        keyword_tables = (
            (("电力", "egrid"), "electricity"),
            (("废弃物", "废物"), "scope3_waste"),
            (("通勤", "商务旅行"), "scope3_travel_commuting"),
            (("运输", "物流"), "scope3_transportation"),
            (("制冷剂",), "blended_refrigerant_gwp"),
            (("固定燃烧", "燃料", "无烟煤", "烟煤", "天然气"), "stationary_combustion"),
        )
        for keywords, candidate in keyword_tables:
            if any(keyword in lowered for keyword in keywords):
                table_code = candidate
                break
        if "gwp" in lowered and table_code is None:
            table_code = "gwp"
        if activity is None and table_code not in {"gwp", "electricity"}:
            return []
        return await self.query(
            db,
            user_id=user_id,
            query=StructuredFactorQuery(
                dataset_ids=dataset_ids,
                edition_year=edition_year,
                table_code=table_code,
                activity=activity,
                gas=gas,
                limit=limit,
            ),
        )

    async def _authorized_tables(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        query: StructuredFactorQuery,
    ) -> list[StructuredTable]:
        statement = (
            select(StructuredTable)
            .join(
                StructuredAssetVersion,
                StructuredAssetVersion.id == StructuredTable.version_id,
            )
            .join(StructuredAsset, StructuredAsset.id == StructuredAssetVersion.asset_id)
            .where(
                StructuredAsset.user_id == user_id,
                StructuredAsset.dataset_id.in_(query.dataset_ids),
                StructuredAsset.status == "ACTIVE",
                StructuredAssetVersion.state == "PUBLISHED",
            )
        )
        if query.edition_year is None:
            statement = statement.where(
                StructuredAsset.current_version_id == StructuredAssetVersion.id
            )
        else:
            statement = statement.where(
                StructuredAssetVersion.edition_year == query.edition_year
            )
        if query.table_code:
            statement = statement.where(StructuredTable.table_code == query.table_code)
        tables = list((await db.scalars(statement)).all())
        if not tables:
            raise LookupError("授权范围内没有可查询的结构化数据")
        return tables

    async def _materialize(self, table: StructuredTable) -> Path:
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        key = hashlib.sha256(
            f"{table.id}:{table.content_hash}".encode("utf-8")
        ).hexdigest()
        destination = self._cache_dir / f"{key}.parquet"
        if destination.exists() and destination.stat().st_size > 0:
            return destination
        partial = destination.with_suffix(".partial")
        await asyncio.to_thread(
            self._storage.download_to_path,
            table.object_bucket,
            table.object_key,
            partial,
        )
        partial.replace(destination)
        return destination

    @staticmethod
    def _execute(
        paths: list[Path], query: StructuredFactorQuery
    ) -> list[dict[str, Any]]:
        import duckdb

        if not paths:
            return []
        escaped_paths = [str(path).replace("'", "''") for path in paths]
        source = ", ".join(f"'{path}'" for path in escaped_paths)
        sql = f"SELECT * FROM read_parquet([{source}]) WHERE 1=1"
        params: list[Any] = []
        if query.table_code:
            sql += " AND table_code = ?"
            params.append(query.table_code)
        if query.activity:
            pattern = f"%{query.activity.lower()}%"
            sql += """ AND (
                lower(coalesce(activity_name, '')) LIKE ? OR
                lower(coalesce(fuel_type, '')) LIKE ? OR
                lower(coalesce(vehicle_type, '')) LIKE ? OR
                lower(coalesce(material_type, '')) LIKE ? OR
                lower(coalesce(category, '')) LIKE ?
            )"""
            params.extend([pattern] * 5)
        if query.gas:
            sql += " AND upper(gas) = upper(?)"
            params.append(query.gas)
        if query.region_code:
            sql += " AND upper(region_code) = upper(?)"
            params.append(query.region_code)
        if query.factor_basis:
            sql += " AND factor_basis = ?"
            params.append(query.factor_basis)
        if query.denominator_unit:
            sql += " AND lower(denominator_unit) = lower(?)"
            params.append(query.denominator_unit)
        sql += " ORDER BY edition_year DESC, table_code, activity_name, gas LIMIT ?"
        params.append(min(max(query.limit, 1), 200))
        connection = duckdb.connect(":memory:", config={"memory_limit": "256MB"})
        try:
            result = connection.execute(sql, params)
            columns = [item[0] for item in result.description]
            output = []
            for row in result.fetchall():
                output.append(
                    {
                        key: str(value) if isinstance(value, Decimal) else value
                        for key, value in zip(columns, row, strict=True)
                    }
                )
            return output
        finally:
            connection.close()
