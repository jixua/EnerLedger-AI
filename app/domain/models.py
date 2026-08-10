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


class Document(Base):
    """上传文件、解析结果、轻量任务队列和最终状态的单表记录。

    解析队列直接复用 MySQL 8：worker 通过 ``FOR UPDATE SKIP LOCKED`` 抢占
    ``QUEUED`` 记录，并用 lease token 对终态写入做 fencing。这样无需新增消息中间件
    或任务表，业务 schema 仍保持四张表。
    """

    __tablename__ = "document"
    __table_args__ = (
        Index("idx_document_dataset_created", "dataset_id", "created_at"),
        Index("idx_document_user_status", "user_id", "status"),
        Index("idx_document_queue_available", "status", "available_at", "id"),
        Index("idx_document_lease_expiry", "status", "lease_expires_at", "id"),
    )

    id: Mapped[int] = mapped_column(UnsignedBigInteger, primary_key=True, autoincrement=True)
    dataset_id: Mapped[int] = mapped_column(UnsignedBigInteger, nullable=False)
    user_id: Mapped[int] = mapped_column(UnsignedBigInteger, nullable=False)
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
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    parse_time_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    parse_quality_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    parse_quality: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
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
