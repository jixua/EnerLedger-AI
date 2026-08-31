"""Structured asset preparation, immutable publication and checksum deduplication."""

from __future__ import annotations

import asyncio
import tempfile
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import (
    StructuredAsset,
    StructuredAssetAlias,
    StructuredAssetVersion,
    StructuredTable,
    StructuredTermAlias,
)
from app.domain.time import utc_now
from app.rag.config import settings
from app.rag.services.storage.base import BaseObjectStorage
from app.rag.services.storage.factory import StorageFactory
from app.services.structured_excel import (
    EPA_TEMPLATE_CODE,
    PARQUET_SCHEMA,
    ParsedWorkbook,
    WorkbookProfile,
    parse_epa_workbook,
    profile_workbook,
    sha256_file,
    write_parquet_tables,
)

EPA_DEFAULT_ALIASES = {
    "无烟煤": ("Anthracite", "activity_name"),
    "烟煤": ("Bituminous", "activity_name"),
    "天然气": ("Natural Gas", "activity_name"),
    "车用汽油": ("Motor Gasoline", "activity_name"),
    "乘用车": ("Passenger Car", "activity_name"),
    "轻型卡车": ("Light-Duty Truck", "activity_name"),
    "重型卡车": ("Medium- and Heavy-Duty Truck", "activity_name"),
    "员工通勤": ("Employee Commuting", "table_code"),
    "商务旅行": ("Business Travel", "table_code"),
    "二氧化碳": ("CO2", "gas"),
    "甲烷": ("CH4", "gas"),
    "氧化亚氮": ("N2O", "gas"),
}


@dataclass(frozen=True)
class PreparedStructuredAsset:
    source_path: Path
    filename: str
    content_hash: str
    file_size: int
    profile: WorkbookProfile
    parsed: ParsedWorkbook | None
    parquet_files: dict[str, Path]


@dataclass(frozen=True)
class StructuredImportResult:
    asset: StructuredAsset
    version: StructuredAssetVersion
    tables: list[StructuredTable]
    duplicate: bool


def prepare_structured_asset(
    source_path: Path,
    filename: str,
    output_dir: Path,
) -> PreparedStructuredAsset:
    """Perform CPU/file-heavy profiling and conversion outside the event loop."""

    profile = profile_workbook(source_path, filename)
    parsed = None
    parquet_files: dict[str, Path] = {}
    if profile.template_code == EPA_TEMPLATE_CODE:
        parsed = parse_epa_workbook(source_path, filename)
        parquet_files = write_parquet_tables(parsed, output_dir)
    return PreparedStructuredAsset(
        source_path=source_path,
        filename=filename,
        content_hash=sha256_file(source_path),
        file_size=source_path.stat().st_size,
        profile=profile,
        parsed=parsed,
        parquet_files=parquet_files,
    )


class StructuredAssetPublisher:
    def __init__(self, storage: BaseObjectStorage | None = None) -> None:
        self._storage = storage or StorageFactory.get_storage()

    async def publish(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        dataset_id: int,
        prepared: PreparedStructuredAsset,
    ) -> StructuredImportResult:
        asset = await db.scalar(
            select(StructuredAsset)
            .where(
                StructuredAsset.user_id == user_id,
                StructuredAsset.dataset_id == dataset_id,
                StructuredAsset.asset_code == prepared.profile.asset_code,
            )
            .with_for_update()
        )
        if asset is None:
            asset = StructuredAsset(
                user_id=user_id,
                dataset_id=dataset_id,
                asset_code=prepared.profile.asset_code,
                name=prepared.profile.asset_name,
                asset_type=prepared.profile.asset_type,
                status="ACTIVE",
            )
            db.add(asset)
            await db.flush()

        existing = await db.scalar(
            select(StructuredAssetVersion).where(
                StructuredAssetVersion.asset_id == asset.id,
                StructuredAssetVersion.content_hash == prepared.content_hash,
            )
        )
        if existing is not None:
            await self._ensure_alias(db, existing.id, prepared.filename)
            tables = list(
                (
                    await db.scalars(
                        select(StructuredTable).where(StructuredTable.version_id == existing.id)
                    )
                ).all()
            )
            await db.commit()
            return StructuredImportResult(asset, existing, tables, duplicate=True)

        version = StructuredAssetVersion(
            asset_id=asset.id,
            user_id=user_id,
            dataset_id=dataset_id,
            version_label=prepared.profile.version_label,
            edition_year=prepared.profile.edition_year,
            template_code=prepared.profile.template_code,
            state="VALIDATING",
            content_hash=prepared.content_hash,
            file_size=prepared.file_size,
            raw_bucket=settings.MINIO_RAW_BUCKET,
            raw_object_key=(
                f"structured/{user_id}/{dataset_id}/{asset.id}/raw/"
                f"{prepared.content_hash}.xlsx"
            ),
            profile={
                "sheet_names": prepared.profile.sheet_names,
                "table_count": prepared.profile.table_count,
                "formula_count": prepared.profile.formula_count,
                **prepared.profile.details,
            },
            row_count=(
                sum(len(table.records) for table in prepared.parsed.tables)
                if prepared.parsed is not None
                else 0
            ),
        )
        db.add(version)
        await db.flush()
        await self._ensure_alias(db, version.id, prepared.filename)

        uploaded: list[tuple[str, str]] = []
        try:
            await asyncio.to_thread(
                self._storage.upload_from_path,
                version.raw_bucket,
                version.raw_object_key,
                prepared.source_path,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
            uploaded.append((version.raw_bucket, version.raw_object_key))
            tables: list[StructuredTable] = []
            if prepared.parsed is not None:
                parsed_by_code = {table.table_code: table for table in prepared.parsed.tables}
                for table_code, parquet_path in prepared.parquet_files.items():
                    parsed_table = parsed_by_code[table_code]
                    object_key = (
                        f"structured/{user_id}/{dataset_id}/{asset.id}/versions/"
                        f"{version.id}/{table_code}.parquet"
                    )
                    await asyncio.to_thread(
                        self._storage.upload_from_path,
                        settings.MINIO_PRIVATE_BUCKET,
                        object_key,
                        parquet_path,
                        "application/vnd.apache.parquet",
                    )
                    uploaded.append((settings.MINIO_PRIVATE_BUCKET, object_key))
                    table = StructuredTable(
                        version_id=version.id,
                        table_code=table_code,
                        display_name=parsed_table.display_name,
                        source_sheet=parsed_table.source_sheet,
                        source_range=parsed_table.source_range,
                        object_bucket=settings.MINIO_PRIVATE_BUCKET,
                        object_key=object_key,
                        content_hash=sha256_file(parquet_path),
                        row_count=len(parsed_table.records),
                        schema_json=PARQUET_SCHEMA,
                    )
                    db.add(table)
                    tables.append(table)
                version.state = "PUBLISHED"
                version.published_at = utc_now()
                await self._ensure_default_aliases(db, user_id=user_id, dataset_id=dataset_id)
                await self._promote_if_newer(db, asset, version)
            else:
                tables = []
                version.state = "MODEL_VALIDATION_REQUIRED"
            await db.commit()
            return StructuredImportResult(asset, version, tables, duplicate=False)
        except Exception:
            await db.rollback()
            for bucket, object_key in reversed(uploaded):
                try:
                    await asyncio.to_thread(self._storage.remove_object, bucket, object_key)
                except Exception:
                    pass
            raise

    @staticmethod
    async def _ensure_alias(db: AsyncSession, version_id: int, filename: str) -> None:
        alias = await db.scalar(
            select(StructuredAssetAlias).where(
                StructuredAssetAlias.version_id == version_id,
                StructuredAssetAlias.filename == filename,
            )
        )
        if alias is None:
            db.add(StructuredAssetAlias(version_id=version_id, filename=filename))

    @staticmethod
    async def _promote_if_newer(
        db: AsyncSession,
        asset: StructuredAsset,
        candidate: StructuredAssetVersion,
    ) -> None:
        if asset.current_version_id is None:
            asset.current_version_id = candidate.id
            return
        current = await db.get(StructuredAssetVersion, asset.current_version_id)
        current_year = current.edition_year if current is not None else None
        candidate_year = candidate.edition_year
        if candidate_year is not None and (current_year is None or candidate_year >= current_year):
            asset.current_version_id = candidate.id

    @staticmethod
    async def _ensure_default_aliases(
        db: AsyncSession, *, user_id: int, dataset_id: int
    ) -> None:
        existing = set(
            (
                await db.scalars(
                    select(StructuredTermAlias.term).where(
                        StructuredTermAlias.user_id == user_id,
                        StructuredTermAlias.dataset_id == dataset_id,
                    )
                )
            ).all()
        )
        for term, (canonical, field_name) in EPA_DEFAULT_ALIASES.items():
            if term not in existing:
                db.add(
                    StructuredTermAlias(
                        user_id=user_id,
                        dataset_id=dataset_id,
                        term=term,
                        canonical_value=canonical,
                        field_name=field_name,
                    )
                )


def temporary_structured_workspace() -> tempfile.TemporaryDirectory[str]:
    return tempfile.TemporaryDirectory(prefix="structured-excel-")
