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


class AgentConversation(Base):
    """A durable, tenant-owned conversation."""

    __tablename__ = "agent_conversation"
    __table_args__ = (Index("idx_agent_conversation_user_updated", "user_id", "updated_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[int] = mapped_column(UnsignedBigInteger, nullable=False)
    title: Mapped[str] = mapped_column(String(160), nullable=False)
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


class AgentConversationTurn(Base):
    """One persisted user/assistant round, including attachments and UI actions."""

    __tablename__ = "agent_conversation_turn"
    __table_args__ = (
        UniqueConstraint("conversation_id", "turn_index", name="uk_agent_turn_index"),
        Index("idx_agent_turn_conversation", "conversation_id", "turn_index"),
        Index("idx_agent_turn_user_created", "user_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(String(36), nullable=False)
    user_id: Mapped[int] = mapped_column(UnsignedBigInteger, nullable=False)
    turn_index: Mapped[int] = mapped_column(Integer, nullable=False)
    user_content: Mapped[str] = mapped_column(Text, nullable=False)
    assistant_content: Mapped[str | None] = mapped_column(
        Text().with_variant(mysql.LONGTEXT(), "mysql"), nullable=True
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="STREAMING", server_default="STREAMING"
    )
    dataset_ids: Mapped[list[int]] = mapped_column(JSON, nullable=False)
    document_ids: Mapped[list[int]] = mapped_column(JSON, nullable=False)
    llm_config_id: Mapped[int | None] = mapped_column(UnsignedBigInteger, nullable=True)
    attachments: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    interaction: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    report_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(1000), nullable=True)
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


class ReportMaterial(Base):
    """对话里直传的报告材料：暂存区，不进知识库。

    用户上传的文件就地提取成文本后放在这里，等报告任务创建时被消费。步骤刻意拆成
    「先暂存、后建任务」两段，而不是把全文再塞进创建请求：报告材料动辄十几万字符，
    每轮对话重传一遍既撑大请求体，也会把会话记录撑爆。

    正文本身按对象存储存放（与 ``Document`` 同样的「元数据进库、字节进 MinIO」约定），
    表里只留指针。消费后置 ``consumed_at`` 并删对象；未被消费的行按 ``created_at`` 清理。
    """

    __tablename__ = "report_material"
    __table_args__ = (Index("idx_report_material_user_created", "user_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[int] = mapped_column(UnsignedBigInteger, nullable=False)
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    char_count: Mapped[int] = mapped_column(Integer, nullable=False)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bucket: Mapped[str] = mapped_column(String(64), nullable=False)
    object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utc_now, server_default=func.current_timestamp()
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
    # 来源分两种：DOCUMENT 指向已入库解析的文档；INLINE 是对话里直传的材料，
    # 不进知识库。INLINE 没有文档可指，所以这三个字段可空——判断来源一律看
    # source_kind，不要靠「document_id 是否为空」反推。正文与分片清单两种来源都要，
    # 因此 parsed_bucket / parsed_object_key / document_manifest 两边共用。
    source_kind: Mapped[str] = mapped_column(
        String(16), nullable=False, default="DOCUMENT", server_default="DOCUMENT"
    )
    dataset_id: Mapped[int | None] = mapped_column(UnsignedBigInteger, nullable=True)
    document_id: Mapped[int | None] = mapped_column(UnsignedBigInteger, nullable=True)
    document_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # INLINE 来源的用户原文件名：报告中心要显示「来源：年度材料.pdf」，没有它这一列是空的。
    inline_source_filename: Mapped[str | None] = mapped_column(String(512), nullable=True)
    parsed_bucket: Mapped[str] = mapped_column(String(64), nullable=False)
    parsed_object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    report_type: Mapped[str] = mapped_column(String(2), nullable=False)
    template_id: Mapped[str] = mapped_column(String(64), nullable=False)
    template_version: Mapped[str] = mapped_column(String(32), nullable=False)
    template_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    model_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    document_manifest: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    custom_template_document_id: Mapped[int | None] = mapped_column(
        UnsignedBigInteger, nullable=True
    )
    custom_template_document_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    custom_template_manifest: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
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
    # 候选 ReportIR 草稿：由 save_report_ir_draft 落库，validate/submit 直接引用它，
    # 避免整份 IR 在每次校验/提交时重复占用模型上下文。
    report_ir_draft: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
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


class StructuredAsset(Base):
    """数据集内的结构化资源；原始文件与发布版本分离。"""

    __tablename__ = "structured_asset"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "dataset_id", "asset_code", name="uk_structured_asset_dataset_code"
        ),
        Index("idx_structured_asset_dataset", "user_id", "dataset_id", "updated_at"),
    )

    id: Mapped[int] = mapped_column(UnsignedBigInteger, primary_key=True, autoincrement=True)
    dataset_id: Mapped[int] = mapped_column(UnsignedBigInteger, nullable=False)
    user_id: Mapped[int] = mapped_column(UnsignedBigInteger, nullable=False)
    asset_code: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    asset_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="ACTIVE", server_default="ACTIVE"
    )
    current_version_id: Mapped[int | None] = mapped_column(UnsignedBigInteger, nullable=True)
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


class StructuredAssetVersion(Base):
    """不可变结构化资源版本；发布仅切换 ``current_version_id``。"""

    __tablename__ = "structured_asset_version"
    __table_args__ = (
        UniqueConstraint("asset_id", "content_hash", name="uk_structured_version_hash"),
        Index("idx_structured_version_asset_state", "asset_id", "state", "created_at"),
        Index("idx_structured_version_edition", "asset_id", "edition_year", "created_at"),
    )

    id: Mapped[int] = mapped_column(UnsignedBigInteger, primary_key=True, autoincrement=True)
    asset_id: Mapped[int] = mapped_column(UnsignedBigInteger, nullable=False)
    user_id: Mapped[int] = mapped_column(UnsignedBigInteger, nullable=False)
    dataset_id: Mapped[int] = mapped_column(UnsignedBigInteger, nullable=False)
    version_label: Mapped[str] = mapped_column(String(64), nullable=False)
    edition_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    template_code: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    file_size: Mapped[int] = mapped_column(UnsignedBigInteger, nullable=False)
    raw_bucket: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    profile: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
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


class StructuredAssetAlias(Base):
    """同一内容哈希对应的原始文件名别名。"""

    __tablename__ = "structured_asset_alias"
    __table_args__ = (
        UniqueConstraint("version_id", "filename", name="uk_structured_alias_filename"),
    )

    id: Mapped[int] = mapped_column(UnsignedBigInteger, primary_key=True, autoincrement=True)
    version_id: Mapped[int] = mapped_column(UnsignedBigInteger, nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utc_now, server_default=func.current_timestamp()
    )


class StructuredTable(Base):
    """发布版本中的一个逻辑表及其 Parquet 位置。"""

    __tablename__ = "structured_table"
    __table_args__ = (
        UniqueConstraint("version_id", "table_code", name="uk_structured_table_code"),
        Index("idx_structured_table_version", "version_id", "table_code"),
    )

    id: Mapped[int] = mapped_column(UnsignedBigInteger, primary_key=True, autoincrement=True)
    version_id: Mapped[int] = mapped_column(UnsignedBigInteger, nullable=False)
    table_code: Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    source_sheet: Mapped[str] = mapped_column(String(255), nullable=False)
    source_range: Mapped[str | None] = mapped_column(String(64), nullable=True)
    object_bucket: Mapped[str] = mapped_column(String(64), nullable=False)
    object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    schema_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utc_now, server_default=func.current_timestamp()
    )


class StructuredTermAlias(Base):
    """面向 Agent 的中英文术语映射。"""

    __tablename__ = "structured_term_alias"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "dataset_id", "term", "canonical_value", name="uk_structured_term_alias"
        ),
        Index("idx_structured_term_lookup", "user_id", "dataset_id", "term"),
    )

    id: Mapped[int] = mapped_column(UnsignedBigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(UnsignedBigInteger, nullable=False)
    dataset_id: Mapped[int] = mapped_column(UnsignedBigInteger, nullable=False)
    term: Mapped[str] = mapped_column(String(255), nullable=False)
    canonical_value: Mapped[str] = mapped_column(String(255), nullable=False)
    field_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utc_now, server_default=func.current_timestamp()
    )


class StructuredQueryAudit(Base):
    """结构化查询审计；不保存凭证或任意 SQL。"""

    __tablename__ = "structured_query_audit"
    __table_args__ = (Index("idx_structured_query_user_created", "user_id", "created_at"),)

    id: Mapped[int] = mapped_column(UnsignedBigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(UnsignedBigInteger, nullable=False)
    dataset_ids: Mapped[list[int]] = mapped_column(JSON, nullable=False)
    request_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    result_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    elapsed_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utc_now, server_default=func.current_timestamp()
    )
