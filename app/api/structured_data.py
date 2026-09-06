"""Structured Excel upload, catalog and allowlisted factor query APIs."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.auth import get_user_id
from app.domain.models import Dataset, StructuredAsset, StructuredAssetVersion
from app.rag.config import settings
from app.rag.database import get_db
from app.services.structured_assets import (
    StructuredAssetPublisher,
    prepare_structured_asset,
)
from app.services.structured_excel import StructuredWorkbookError
from app.services.structured_query import StructuredFactorQuery, StructuredQueryService

router = APIRouter(prefix="/api/v1", tags=["结构化数据"])

_XLSX_MAGICS = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")


class StructuredTableRead(BaseModel):
    table_code: str
    display_name: str
    source_sheet: str
    source_range: str | None
    row_count: int


class StructuredImportRead(BaseModel):
    asset_id: int
    asset_code: str
    asset_name: str
    asset_type: str
    version_id: int
    version_label: str
    edition_year: int | None
    state: str
    content_hash: str
    duplicate: bool
    profile: dict[str, Any] | None
    tables: list[StructuredTableRead]


class StructuredAssetRead(BaseModel):
    asset_id: int
    asset_code: str
    name: str
    asset_type: str
    current_version_id: int | None
    versions: list[dict[str, Any]]


class StructuredQueryBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset_ids: list[int] = Field(min_length=1)
    edition_year: int | None = Field(default=None, ge=2000, le=2200)
    table_code: str | None = Field(default=None, max_length=64)
    activity: str | None = Field(default=None, max_length=255)
    gas: str | None = Field(default=None, max_length=32)
    region_code: str | None = Field(default=None, max_length=32)
    factor_basis: str | None = Field(default=None, max_length=64)
    denominator_unit: str | None = Field(default=None, max_length=64)
    limit: int = Field(default=20, ge=1, le=200)

    @field_validator("dataset_ids")
    @classmethod
    def normalize_dataset_ids(cls, value: list[int]) -> list[int]:
        result = list(dict.fromkeys(value))
        if any(item <= 0 for item in result):
            raise ValueError("dataset_ids 必须是正整数")
        return result

    @field_validator(
        "table_code",
        "activity",
        "gas",
        "region_code",
        "factor_basis",
        "denominator_unit",
    )
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


class StructuredQueryRead(BaseModel):
    count: int
    rows: list[dict[str, Any]]


async def _require_owned_datasets(
    db: AsyncSession, *, user_id: int, dataset_ids: list[int]
) -> None:
    owned = set(
        (
            await db.scalars(
                select(Dataset.id).where(
                    Dataset.id.in_(dataset_ids),
                    Dataset.user_id == user_id,
                    Dataset.status == "ACTIVE",
                )
            )
        ).all()
    )
    if owned != set(dataset_ids):
        raise HTTPException(status_code=404, detail="数据集不存在或不属于当前用户")


async def _save_xlsx_upload(upload: UploadFile, destination: Path) -> int:
    filename = upload.filename or ""
    if Path(filename).suffix.lower() != ".xlsx":
        raise HTTPException(status_code=415, detail="结构化数据入口当前只支持 XLSX")
    total = 0
    with destination.open("wb") as target:
        while chunk := await upload.read(1024 * 1024):
            total += len(chunk)
            if total > settings.DOCUMENT_UPLOAD_MAX_BYTES:
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail=f"文件超过上传上限 {settings.DOCUMENT_UPLOAD_MAX_BYTES} bytes",
                )
            target.write(chunk)
    if total == 0:
        raise HTTPException(status_code=422, detail="上传文件不能为空")
    with destination.open("rb") as source:
        if not source.read(8).startswith(_XLSX_MAGICS):
            raise HTTPException(status_code=422, detail="文件内容不是有效的 XLSX")
    return total


def _import_response(result) -> StructuredImportRead:
    return StructuredImportRead(
        asset_id=result.asset.id,
        asset_code=result.asset.asset_code,
        asset_name=result.asset.name,
        asset_type=result.asset.asset_type,
        version_id=result.version.id,
        version_label=result.version.version_label,
        edition_year=result.version.edition_year,
        state=result.version.state,
        content_hash=result.version.content_hash,
        duplicate=result.duplicate,
        profile=result.version.profile,
        tables=[
            StructuredTableRead(
                table_code=table.table_code,
                display_name=table.display_name,
                source_sheet=table.source_sheet,
                source_range=table.source_range,
                row_count=table.row_count,
            )
            for table in result.tables
        ],
    )


@router.post(
    "/datasets/{dataset_id}/structured-assets",
    response_model=StructuredImportRead,
    status_code=status.HTTP_201_CREATED,
)
async def upload_structured_asset(
    dataset_id: int,
    file: Annotated[UploadFile, File(...)],
    user_id: Annotated[int, Depends(get_user_id)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StructuredImportRead:
    await _require_owned_datasets(db, user_id=user_id, dataset_ids=[dataset_id])
    filename = Path(file.filename or "upload.xlsx").name
    with tempfile.TemporaryDirectory(prefix="structured-upload-") as temp_dir:
        temp_root = Path(temp_dir)
        source_path = temp_root / "source.xlsx"
        await _save_xlsx_upload(file, source_path)
        try:
            prepared = await asyncio.to_thread(
                prepare_structured_asset,
                source_path,
                filename,
                temp_root / "parquet",
            )
            result = await StructuredAssetPublisher().publish(
                db,
                user_id=user_id,
                dataset_id=dataset_id,
                prepared=prepared,
            )
        except StructuredWorkbookError as exc:
            await db.rollback()
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _import_response(result)


@router.get(
    "/datasets/{dataset_id}/structured-assets",
    response_model=list[StructuredAssetRead],
)
async def list_structured_assets(
    dataset_id: int,
    user_id: Annotated[int, Depends(get_user_id)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[StructuredAssetRead]:
    await _require_owned_datasets(db, user_id=user_id, dataset_ids=[dataset_id])
    assets = list(
        (
            await db.scalars(
                select(StructuredAsset)
                .where(
                    StructuredAsset.user_id == user_id,
                    StructuredAsset.dataset_id == dataset_id,
                )
                .order_by(StructuredAsset.updated_at.desc())
            )
        ).all()
    )
    output: list[StructuredAssetRead] = []
    for asset in assets:
        versions = list(
            (
                await db.scalars(
                    select(StructuredAssetVersion)
                    .where(StructuredAssetVersion.asset_id == asset.id)
                    .order_by(
                        StructuredAssetVersion.edition_year.desc(),
                        StructuredAssetVersion.created_at.desc(),
                    )
                )
            ).all()
        )
        output.append(
            StructuredAssetRead(
                asset_id=asset.id,
                asset_code=asset.asset_code,
                name=asset.name,
                asset_type=asset.asset_type,
                current_version_id=asset.current_version_id,
                versions=[
                    {
                        "id": version.id,
                        "version_label": version.version_label,
                        "edition_year": version.edition_year,
                        "state": version.state,
                        "content_hash": version.content_hash,
                        "row_count": version.row_count,
                        "profile": version.profile,
                    }
                    for version in versions
                ],
            )
        )
    return output


@router.post("/structured-data/query", response_model=StructuredQueryRead)
async def query_structured_data(
    body: StructuredQueryBody,
    user_id: Annotated[int, Depends(get_user_id)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StructuredQueryRead:
    await _require_owned_datasets(db, user_id=user_id, dataset_ids=body.dataset_ids)
    query = StructuredFactorQuery(**body.model_dump())
    try:
        rows = await StructuredQueryService().query(db, user_id=user_id, query=query)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return StructuredQueryRead(count=len(rows), rows=rows)


@router.post("/structured-data/calculate-product-uncertainty")
async def calculate_product_uncertainty() -> None:
    """The formula model remains disabled until Excel parity validation passes."""

    raise HTTPException(
        status_code=409,
        detail={
            "code": "CALCULATION_MODEL_VALIDATION_REQUIRED",
            "message": "不确定性模型已登记，但公式迁移尚未通过 Excel 对照验收",
        },
    )
