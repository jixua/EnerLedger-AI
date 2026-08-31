"""structured Excel assets and query catalog

Revision ID: 0009_structured_assets
Revises: 0008_document_folder_hierarchy, 0007_document_filename_unique
Create Date: 2026-08-31
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0009_structured_assets"
down_revision: tuple[str, str] = (
    "0008_document_folder_hierarchy",
    "0007_document_filename_unique",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bigint = mysql.BIGINT(unsigned=True)
    op.create_table(
        "structured_asset",
        sa.Column("id", bigint, autoincrement=True, nullable=False),
        sa.Column("dataset_id", bigint, nullable=False),
        sa.Column("user_id", bigint, nullable=False),
        sa.Column("asset_code", sa.String(128), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("asset_type", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), server_default="ACTIVE", nullable=False),
        sa.Column("current_version_id", bigint, nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id", "dataset_id", "asset_code", name="uk_structured_asset_dataset_code"
        ),
    )
    op.create_index(
        "idx_structured_asset_dataset", "structured_asset", ["user_id", "dataset_id", "updated_at"]
    )
    op.create_table(
        "structured_asset_version",
        sa.Column("id", bigint, autoincrement=True, nullable=False),
        sa.Column("asset_id", bigint, nullable=False),
        sa.Column("user_id", bigint, nullable=False),
        sa.Column("dataset_id", bigint, nullable=False),
        sa.Column("version_label", sa.String(64), nullable=False),
        sa.Column("edition_year", sa.Integer(), nullable=True),
        sa.Column("template_code", sa.String(64), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("file_size", bigint, nullable=False),
        sa.Column("raw_bucket", sa.String(64), nullable=False),
        sa.Column("raw_object_key", sa.String(512), nullable=False),
        sa.Column("profile", mysql.JSON(), nullable=True),
        sa.Column("row_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_message", sa.String(1000), nullable=True),
        sa.Column("published_at", sa.DateTime(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("asset_id", "content_hash", name="uk_structured_version_hash"),
    )
    op.create_index(
        "idx_structured_version_asset_state",
        "structured_asset_version",
        ["asset_id", "state", "created_at"],
    )
    op.create_index(
        "idx_structured_version_edition",
        "structured_asset_version",
        ["asset_id", "edition_year", "created_at"],
    )
    op.create_table(
        "structured_asset_alias",
        sa.Column("id", bigint, autoincrement=True, nullable=False),
        sa.Column("version_id", bigint, nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("version_id", "filename", name="uk_structured_alias_filename"),
    )
    op.create_table(
        "structured_table",
        sa.Column("id", bigint, autoincrement=True, nullable=False),
        sa.Column("version_id", bigint, nullable=False),
        sa.Column("table_code", sa.String(64), nullable=False),
        sa.Column("display_name", sa.String(255), nullable=False),
        sa.Column("source_sheet", sa.String(255), nullable=False),
        sa.Column("source_range", sa.String(64), nullable=True),
        sa.Column("object_bucket", sa.String(64), nullable=False),
        sa.Column("object_key", sa.String(512), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("schema_json", mysql.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("version_id", "table_code", name="uk_structured_table_code"),
    )
    op.create_index(
        "idx_structured_table_version", "structured_table", ["version_id", "table_code"]
    )
    op.create_table(
        "structured_term_alias",
        sa.Column("id", bigint, autoincrement=True, nullable=False),
        sa.Column("user_id", bigint, nullable=False),
        sa.Column("dataset_id", bigint, nullable=False),
        sa.Column("term", sa.String(255), nullable=False),
        sa.Column("canonical_value", sa.String(255), nullable=False),
        sa.Column("field_name", sa.String(64), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id", "dataset_id", "term", "canonical_value", name="uk_structured_term_alias"
        ),
    )
    op.create_index(
        "idx_structured_term_lookup", "structured_term_alias", ["user_id", "dataset_id", "term"]
    )
    op.create_table(
        "structured_query_audit",
        sa.Column("id", bigint, autoincrement=True, nullable=False),
        sa.Column("user_id", bigint, nullable=False),
        sa.Column("dataset_ids", mysql.JSON(), nullable=False),
        sa.Column("request_payload", mysql.JSON(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("result_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("elapsed_ms", sa.Integer(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_structured_query_user_created", "structured_query_audit", ["user_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_table("structured_query_audit")
    op.drop_table("structured_term_alias")
    op.drop_table("structured_table")
    op.drop_table("structured_asset_alias")
    op.drop_table("structured_asset_version")
    op.drop_table("structured_asset")
