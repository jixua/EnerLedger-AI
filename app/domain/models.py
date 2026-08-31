"""当前项目的最小业务数据模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.time import utc_now
from app.rag.models.db_models import Base

UnsignedBigInteger = BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql")


class Dataset(Base):
    """知识数据集；模型绑定直接内聚在本表。"""

    __tablename__ = "dataset"
    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uk_dataset_user_name"),
        Index("idx_dataset_user_updated", "user_id", "updated_at"),
    )

    id: Mapped[int] = mapped_column(UnsignedBigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(UnsignedBigInteger, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(String(512), nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="ACTIVE", server_default="ACTIVE"
    )
    dense_embedding_config_id: Mapped[int] = mapped_column(UnsignedBigInteger, nullable=False)
    sparse_embedding_config_id: Mapped[int] = mapped_column(UnsignedBigInteger, nullable=False)
    chat_config_id: Mapped[int | None] = mapped_column(UnsignedBigInteger, nullable=True)
    vision_config_id: Mapped[int | None] = mapped_column(UnsignedBigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utc_now, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
        server_default=func.current_timestamp(),
    )


class DocumentFolder(Base):
    """数据集内的虚拟文件夹，支持用 ``parent_id`` 表示层级。"""

    __tablename__ = "document_folder"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "dataset_id",
            "name",
            name="uk_document_folder_user_dataset_name",
        ),
        Index("idx_document_folder_dataset", "user_id", "dataset_id", "created_at"),
        Index("idx_document_folder_parent", "dataset_id", "parent_id", "id"),
    )

    id: Mapped[int] = mapped_column(UnsignedBigInteger, primary_key=True, autoincrement=True)
    dataset_id: Mapped[int] = mapped_column(UnsignedBigInteger, nullable=False)
    user_id: Mapped[int] = mapped_column(UnsignedBigInteger, nullable=False)
    parent_id: Mapped[int | None] = mapped_column(UnsignedBigInteger, nullable=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utc_now, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
        server_default=func.current_timestamp(),
    )


class Document(Base):
    """上传文件、解析结果与最终状态的单表记录。

    RabbitMQ 负责主动投递只携带 ID 的解析命令；本表仍作为状态、重试次数、
    lease token 和最终结果的权威真值，防止重复投递覆盖新版本。
    """

    __tablename__ = "document"
    __table_args__ = (
        Index("idx_document_dataset_created", "dataset_id", "created_at"),
        Index("idx_document_user_status", "user_id", "status"),
        Index("idx_document_queue_available", "status", "available_at", "id"),
        Index("idx_document_lease_expiry", "status", "lease_expires_at", "id"),
        Index("idx_document_dispatch_available", "dispatch_status", "dispatch_available_at", "id"),
        Index("idx_document_review_status", "user_id", "review_status", "created_at"),
        Index("idx_document_folder", "folder_id", "id"),
    )

    id: Mapped[int] = mapped_column(UnsignedBigInteger, primary_key=True, autoincrement=True)
    dataset_id: Mapped[int] = mapped_column(UnsignedBigInteger, nullable=False)
    user_id: Mapped[int] = mapped_column(UnsignedBigInteger, nullable=False)
    folder_id: Mapped[int | None] = mapped_column(UnsignedBigInteger, nullable=True)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    file_type: Mapped[str] = mapped_column(String(32), nullable=False)
    file_size: Mapped[int] = mapped_column(UnsignedBigInteger, nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    raw_bucket: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    parsed_bucket: Mapped[str | None] = mapped_column(String(64), nullable=True)
    parsed_object_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    parser_backend: Mapped[str] = mapped_column(
        String(32), nullable=False, default="opendataloader", server_default="opendataloader"
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="QUEUED", server_default="QUEUED"
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    available_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    queued_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    processing_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    reparse_requested: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    dispatch_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="PENDING", server_default="PENDING"
    )
    dispatch_attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    dispatch_available_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    dispatch_lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    dispatch_lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    dispatch_error: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    parse_time_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    parse_quality_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    parse_quality: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    source_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default="MANUAL_UPLOAD", server_default="MANUAL_UPLOAD"
    )
    source_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    source_title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    source_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    review_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="NOT_REQUIRED", server_default="NOT_REQUIRED"
    )
    review_note: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    reviewed_by_user_id: Mapped[int | None] = mapped_column(UnsignedBigInteger, nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utc_now, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
        server_default=func.current_timestamp(),
    )


class ReportRun(Base):
    """A frozen, tenant-owned report-generation execution."""

    __tablename__ = "report_run"
    __table_args__ = (
        Index("idx_report_run_user_created", "user_id", "created_at"),
        Index("idx_report_run_document_created", "document_id", "created_at"),
        Index("idx_report_run_state_available", "state", "available_at", "created_at"),
        Index(
            "idx_report_run_dispatch_available",
            "dispatch_status",
            "dispatch_available_at",
            "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[int] = mapped_column(UnsignedBigInteger, nullable=False)
    dataset_id: Mapped[int] = mapped_column(UnsignedBigInteger, nullable=False)
    document_id: Mapped[int] = mapped_column(UnsignedBigInteger, nullable=False)
    document_version: Mapped[int] = mapped_column(Integer, nullable=False)
    parsed_bucket: Mapped[str] = mapped_column(String(64), nullable=False)
    parsed_object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    report_type: Mapped[str] = mapped_column(String(2), nullable=False)
    template_id: Mapped[str] = mapped_column(String(64), nullable=False)
    template_version: Mapped[str] = mapped_column(String(32), nullable=False)
    template_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    model_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    document_manifest: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default="GENERATE", server_default="GENERATE"
    )
    language: Mapped[str] = mapped_column(
        String(16), nullable=False, default="zh-CN", server_default="zh-CN"
    )
    reporting_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    user_instructions: Mapped[str | None] = mapped_column(Text, nullable=True)
    output_formats: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    llm_config_id: Mapped[int] = mapped_column(UnsignedBigInteger, nullable=False)
    llm_snapshot_version: Mapped[int] = mapped_column(UnsignedBigInteger, nullable=False)
    state: Mapped[str] = mapped_column(
        String(32), nullable=False, default="PENDING", server_default="PENDING"
    )
    stage: Mapped[str] = mapped_column(
        String(32), nullable=False, default="WAITING", server_default="WAITING"
    )
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    available_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    report_ir: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    checkpoint: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    analysis_coverage: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    validation_report: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    manifest: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    dispatch_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="PENDING", server_default="PENDING"
    )
    dispatch_attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    dispatch_available_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    dispatch_lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    dispatch_lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    dispatch_error: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utc_now, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
        server_default=func.current_timestamp(),
    )


class ReportQuestion(Base):
    """A field-bound clarification that can resume one report run."""

    __tablename__ = "report_question"
    __table_args__ = (
        UniqueConstraint("run_id", "field_id", "question_type", name="uk_report_question_field"),
        Index("idx_report_question_run_status", "run_id", "status", "id"),
    )

    id: Mapped[int] = mapped_column(UnsignedBigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(36), nullable=False)
    field_id: Mapped[str] = mapped_column(String(128), nullable=False)
    question_type: Mapped[str] = mapped_column(String(32), nullable=False)
    question: Mapped[str] = mapped_column(String(1000), nullable=False)
    options: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1"
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="OPEN", server_default="OPEN"
    )
    answer: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    answered_by: Mapped[int | None] = mapped_column(UnsignedBigInteger, nullable=True)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utc_now, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
        server_default=func.current_timestamp(),
    )


class ReportArtifact(Base):
    """An immutable object-store artifact produced by one report run."""

    __tablename__ = "report_artifact"
    __table_args__ = (
        UniqueConstraint("run_id", "artifact_type", name="uk_report_artifact_type"),
        Index("idx_report_artifact_run", "run_id", "id"),
    )

    id: Mapped[int] = mapped_column(UnsignedBigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(36), nullable=False)
    artifact_type: Mapped[str] = mapped_column(String(32), nullable=False)
    bucket: Mapped[str] = mapped_column(String(64), nullable=False)
    object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    content_type: Mapped[str] = mapped_column(String(128), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(UnsignedBigInteger, nullable=False)
    renderer_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utc_now, server_default=func.current_timestamp()
    )
