"""控制面 API 的 Pydantic 契约。"""

import re
from datetime import datetime
from pathlib import PurePath
from typing import Annotated, Any, Literal, Self

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StringConstraints,
    field_validator,
    model_validator,
)

Capability = Literal["CHAT", "EMBEDDING", "SPARSE_EMBEDDING", "RERANK", "VISION"]
ProtocolName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, to_lower=True, min_length=1, max_length=32),
]


class AdminLogin(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: SecretStr = Field(min_length=1, max_length=256)


class AuthToken(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_at: datetime


class CurrentAdmin(BaseModel):
    user_id: int
    username: str
    role: Literal["admin"] = "admin"


class ArxivPaper(BaseModel):
    arxiv_id: str
    title: str
    summary: str
    authors: list[str]
    categories: list[str]
    published_at: datetime
    updated_at: datetime
    abstract_url: AnyHttpUrl
    pdf_url: AnyHttpUrl


class ArxivSearchResponse(BaseModel):
    source: Literal["arXiv"] = "arXiv"
    query: str
    total_results: int
    fetched_at: datetime
    items: list[ArxivPaper]


class ArxivImportPaper(BaseModel):
    arxiv_id: str
    title: str = Field(min_length=1, max_length=500)

    @field_validator("arxiv_id")
    @classmethod
    def validate_arxiv_id(cls, value: str) -> str:
        pattern = re.compile(
            r"^(?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[a-z]{2})?/\d{7})(?:v\d+)?$",
            re.IGNORECASE,
        )
        arxiv_id = value.strip()
        if not pattern.fullmatch(arxiv_id):
            raise ValueError(f"无效的 arXiv ID：{value}")
        return arxiv_id

    @field_validator("title")
    @classmethod
    def strip_title(cls, value: str) -> str:
        title = value.strip()
        if not title:
            raise ValueError("论文标题不能为空")
        return title


class ArxivImportRequest(BaseModel):
    dataset_id: int = Field(gt=0)
    papers: list[ArxivImportPaper] = Field(min_length=1, max_length=10)

    @field_validator("papers")
    @classmethod
    def deduplicate_papers(cls, values: list[ArxivImportPaper]) -> list[ArxivImportPaper]:
        normalized: list[ArxivImportPaper] = []
        seen_ids: set[str] = set()
        for paper in values:
            if paper.arxiv_id in seen_ids:
                continue
            seen_ids.add(paper.arxiv_id)
            normalized.append(paper)
        return normalized


class ArxivImportItem(BaseModel):
    arxiv_id: str
    status: Literal["QUEUED", "FAILED"]
    document_id: int | None = None
    filename: str
    message: str | None = None


class ArxivImportResponse(BaseModel):
    dataset_id: int
    queued_count: int
    failed_count: int
    items: list[ArxivImportItem]


class DatasetCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=512)
    dense_embedding_config_id: int = Field(gt=0)
    sparse_embedding_config_id: int = Field(gt=0)
    chat_config_id: int | None = Field(default=None, gt=0)
    vision_config_id: int | None = Field(default=None, gt=0)

    @field_validator("name")
    @classmethod
    def strip_non_empty_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("数据集名称不能为空")
        return value

    @field_validator("description")
    @classmethod
    def strip_description(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None


class DatasetUpdate(BaseModel):
    """数据集的部分更新；显式 ``null`` 只用于清空可空字段。"""

    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=512)
    dense_embedding_config_id: int | None = Field(default=None, gt=0)
    sparse_embedding_config_id: int | None = Field(default=None, gt=0)
    chat_config_id: int | None = Field(default=None, gt=0)
    vision_config_id: int | None = Field(default=None, gt=0)

    @field_validator("name")
    @classmethod
    def strip_non_empty_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("数据集名称不能为空")
        return value

    @field_validator("description")
    @classmethod
    def strip_description(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    @model_validator(mode="after")
    def validate_patch_fields(self) -> Self:
        fields = self.model_fields_set
        if not fields:
            raise ValueError("至少需要更新一个字段")
        for field_name in (
            "name",
            "dense_embedding_config_id",
            "sparse_embedding_config_id",
        ):
            if field_name in fields and getattr(self, field_name) is None:
                raise ValueError(f"{field_name} 不能为 null")
        return self


class DatasetRead(BaseModel):
    id: int
    name: str
    description: str | None
    status: str
    dense_embedding_config_id: int
    sparse_embedding_config_id: int
    chat_config_id: int | None
    vision_config_id: int | None
    created_at: datetime
    updated_at: datetime


class DocumentUpdate(BaseModel):
    """文档展示属性的部分更新；不改变原文件对象与解析格式。"""

    filename: str = Field(min_length=1, max_length=255)

    @field_validator("filename")
    @classmethod
    def normalize_filename(cls, value: str) -> str:
        value = PurePath(value.strip()).name
        if not value:
            raise ValueError("文件名不能为空")
        return value


class DocumentRead(BaseModel):
    document_id: int
    dataset_id: int
    filename: str
    file_type: str
    file_size: int
    content_type: str | None
    parser_backend: str
    status: str
    version: int
    attempt_count: int
    available_at: datetime | None
    queued_at: datetime | None
    processing_started_at: datetime | None
    lease_expires_at: datetime | None
    finished_at: datetime | None
    error_code: str | None
    error_message: str | None
    reparse_requested: bool
    page_count: int | None
    chunk_count: int
    parse_time_ms: int | None
    parse_quality_status: str | None
    parse_quality: dict[str, Any] | None
    retrieval_ready: bool
    created_at: datetime
    updated_at: datetime


class DocumentChunkRead(BaseModel):
    """文档当前版本的可追溯分片，不暴露内部向量或租户字段。"""

    chunk_id: str
    document_version: int
    chunk_index: int
    chunk_type: str
    content: str
    char_count: int
    start_line: int | None
    end_line: int | None
    start_page: int | None
    end_page: int | None
    structure: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime


class DocumentChunkPage(BaseModel):
    """按文档顺序分页返回的分片列表。"""

    document_id: int
    dataset_id: int
    document_version: int
    items: list[DocumentChunkRead]
    total: int
    offset: int
    limit: int


class DocumentPreviewBoundary(BaseModel):
    """连续文档预览中的一个主体分片边界。

    行号与 LinkRag splitter 保持一致，从 0 开始；这里不返回分片正文，
    避免把 neighbor overlap 重复拼接成“原文”。
    """

    boundary_index: int
    chunk_id: str
    chunk_index: int
    chunk_type: str
    start_line: int
    end_line: int
    start_page: int | None
    end_page: int | None
    heading_trail: list[str]
    split_strategy: str | None


class DocumentPreviewMap(BaseModel):
    """完整文档的分片边界图，不分页且不暴露对象存储位置。"""

    document_id: int
    dataset_id: int
    document_version: int
    boundary_precision: Literal["line", "approximate_line", "legacy_line"] = Field(
        description=(
            "line=精确行边界；approximate_line=语义细分仅有近似行位置；"
            "legacy_line=历史数据降级位置"
        )
    )
    map_reliable: bool = Field(
        description="只有可验证的精确行边界为 true；近似行与历史边界均为 false"
    )
    reparse_required: bool
    source_chunk_count: int
    derived_chunk_count: int
    boundaries: list[DocumentPreviewBoundary]


class LLMConfigCreate(BaseModel):
    provider_type: str = Field(min_length=1, max_length=32)
    model_name: str = Field(min_length=1, max_length=128)
    display_name: str | None = Field(default=None, max_length=128)
    capability: Capability
    protocol: ProtocolName
    api_base_url: AnyHttpUrl
    api_key: SecretStr
    is_active: bool = True

    @field_validator("provider_type")
    @classmethod
    def normalize_provider_type(cls, value: str) -> str:
        value = value.strip().lower()
        if not value:
            raise ValueError("厂商类型不能为空")
        return value

    @field_validator("model_name")
    @classmethod
    def strip_config_model_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("模型名不能为空")
        return value

    @field_validator("display_name")
    @classmethod
    def strip_display_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    @field_validator("api_key")
    @classmethod
    def reject_empty_api_key(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("API Key 不能为空")
        return value


class LLMConfigUpdate(BaseModel):
    """可执行模型快照的可变字段。厂商、协议和能力保持创建时语义。"""

    model_name: str | None = Field(default=None, min_length=1, max_length=128)
    display_name: str | None = Field(default=None, max_length=128)
    api_base_url: AnyHttpUrl | None = None
    api_key: SecretStr | None = None
    is_active: bool | None = None

    @field_validator("model_name")
    @classmethod
    def strip_config_model_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("模型名不能为空")
        return value

    @field_validator("display_name")
    @classmethod
    def strip_display_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    @field_validator("api_key")
    @classmethod
    def reject_empty_api_key(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and not value.get_secret_value().strip():
            raise ValueError("API Key 不能为空")
        return value

    @model_validator(mode="after")
    def validate_patch_fields(self) -> Self:
        fields = self.model_fields_set
        if not fields:
            raise ValueError("至少需要更新一个字段")
        for field_name in ("model_name", "api_base_url", "api_key", "is_active"):
            if field_name in fields and getattr(self, field_name) is None:
                raise ValueError(f"{field_name} 不能为 null")
        return self


class LLMConfigRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    scope: str
    owner_user_id: int
    provider_id: int
    provider_type: str
    model_name: str
    display_name: str | None
    capability: str
    protocol: str
    api_base_url: str
    api_key_masked: str
    is_active: bool
    snapshot_version: int
    created_at: datetime
    updated_at: datetime
