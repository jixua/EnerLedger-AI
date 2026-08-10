"""文档 Chunk 真值表。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
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


class ChunkRecordDB(Base):
    """最小 Chunk 记录。

    Python 属性 ``doc_id`` / ``set_id`` 保持与迁入的 LinkRag 检索代码兼容；MySQL
    列名使用更清晰的 ``document_id`` / ``dataset_id``。
    """

    __tablename__ = "document_chunk"
    __table_args__ = (
        UniqueConstraint("chunk_id", name="uk_document_chunk_chunk_id"),
        Index("idx_document_chunk_user_dataset", "user_id", "dataset_id"),
        Index("idx_document_chunk_document_index", "document_id", "chunk_index"),
    )

    id: Mapped[int] = mapped_column(UnsignedBigInteger, primary_key=True, autoincrement=True)
    chunk_id: Mapped[str] = mapped_column(String(128), nullable=False)
    doc_id: Mapped[int] = mapped_column("document_id", UnsignedBigInteger, nullable=False)
    document_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    set_id: Mapped[int] = mapped_column("dataset_id", UnsignedBigInteger, nullable=False)
    user_id: Mapped[int] = mapped_column(UnsignedBigInteger, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    chunk_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default="text", server_default="text"
    )
    start_line: Mapped[int | None] = mapped_column(Integer, nullable=True)
    end_line: Mapped[int | None] = mapped_column(Integer, nullable=True)
    start_page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    end_page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    structure_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    create_time: Mapped[datetime] = mapped_column(
        "created_at",
        DateTime,
        nullable=False,
        default=utc_now,
        server_default=func.current_timestamp(),
    )
    update_time: Mapped[datetime] = mapped_column(
        "updated_at",
        DateTime,
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
        server_default=func.current_timestamp(),
    )
