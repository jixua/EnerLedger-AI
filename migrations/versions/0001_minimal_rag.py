"""minimal rag baseline

Revision ID: 0001_minimal_rag
Revises:
Create Date: 2026-08-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0001_minimal_rag"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    unsigned_bigint = mysql.BIGINT(unsigned=True)
    op.create_table(
        "llm_config",
        sa.Column("id", unsigned_bigint, autoincrement=True, nullable=False),
        sa.Column("scope", sa.String(16), server_default="USER", nullable=False),
        sa.Column("owner_user_id", unsigned_bigint, nullable=False),
        sa.Column("provider_id", unsigned_bigint, server_default="1", nullable=False),
        sa.Column("provider_type", sa.String(32), nullable=False),
        sa.Column("model_name", sa.String(128), nullable=False),
        sa.Column("display_name", sa.String(128), nullable=True),
        sa.Column("capability", sa.String(32), nullable=False),
        sa.Column("protocol", sa.String(32), nullable=False),
        sa.Column("api_base_url", sa.String(512), nullable=False),
        sa.Column("api_key", sa.String(512), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("snapshot_version", unsigned_bigint, server_default="1", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "scope",
            "owner_user_id",
            "provider_type",
            "model_name",
            "capability",
            name="uk_llm_config_owner_model",
        ),
    )
    op.create_index(
        "idx_llm_config_owner_capability",
        "llm_config",
        ["owner_user_id", "capability", "is_active"],
    )

    op.create_table(
        "dataset",
        sa.Column("id", unsigned_bigint, autoincrement=True, nullable=False),
        sa.Column("user_id", unsigned_bigint, nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("description", sa.String(512), nullable=True),
        sa.Column("status", sa.String(16), server_default="ACTIVE", nullable=False),
        sa.Column("dense_embedding_config_id", unsigned_bigint, nullable=False),
        sa.Column("sparse_embedding_config_id", unsigned_bigint, nullable=False),
        sa.Column("chat_config_id", unsigned_bigint, nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "name", name="uk_dataset_user_name"),
    )
    op.create_index("idx_dataset_user_updated", "dataset", ["user_id", "updated_at"])

    op.create_table(
        "document",
        sa.Column("id", unsigned_bigint, autoincrement=True, nullable=False),
        sa.Column("dataset_id", unsigned_bigint, nullable=False),
        sa.Column("user_id", unsigned_bigint, nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("file_type", sa.String(32), nullable=False),
        sa.Column("file_size", unsigned_bigint, nullable=False),
        sa.Column("content_type", sa.String(128), nullable=True),
        sa.Column("raw_bucket", sa.String(64), nullable=False),
        sa.Column("raw_object_key", sa.String(512), nullable=False),
        sa.Column("parsed_bucket", sa.String(64), nullable=True),
        sa.Column("parsed_object_key", sa.String(512), nullable=True),
        sa.Column("parser_backend", sa.String(32), server_default="opendataloader", nullable=False),
        sa.Column("status", sa.String(16), server_default="PROCESSING", nullable=False),
        sa.Column("error_message", sa.String(1000), nullable=True),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("chunk_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("parse_time_ms", sa.Integer(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_document_dataset_created", "document", ["dataset_id", "created_at"])
    op.create_index("idx_document_user_status", "document", ["user_id", "status"])

    op.create_table(
        "document_chunk",
        sa.Column("id", unsigned_bigint, autoincrement=True, nullable=False),
        sa.Column("chunk_id", sa.String(128), nullable=False),
        sa.Column("document_id", unsigned_bigint, nullable=False),
        sa.Column("dataset_id", unsigned_bigint, nullable=False),
        sa.Column("user_id", unsigned_bigint, nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("chunk_type", sa.String(32), server_default="text", nullable=False),
        sa.Column("start_line", sa.Integer(), nullable=True),
        sa.Column("end_line", sa.Integer(), nullable=True),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("chunk_id", name="uk_document_chunk_chunk_id"),
    )
    op.create_index(
        "idx_document_chunk_user_dataset",
        "document_chunk",
        ["user_id", "dataset_id"],
    )
    op.create_index(
        "idx_document_chunk_document_index",
        "document_chunk",
        ["document_id", "chunk_index"],
    )


def downgrade() -> None:
    op.drop_table("document_chunk")
    op.drop_table("document")
    op.drop_table("dataset")
    op.drop_table("llm_config")
