"""add report platform foundation

Revision ID: 0007_report_platform_foundation
Revises: 0006_document_dispatch_outbox
Create Date: 2026-08-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0007_report_platform_foundation"
down_revision: str | None = "0006_document_dispatch_outbox"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _unsigned_bigint():
    return sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql")


def upgrade() -> None:
    bigint = _unsigned_bigint()
    op.add_column(
        "llm_config",
        sa.Column(
            "supports_tool_calling",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.create_table(
        "report_run",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", bigint, nullable=False),
        sa.Column("dataset_id", bigint, nullable=False),
        sa.Column("document_id", bigint, nullable=False),
        sa.Column("document_version", sa.Integer(), nullable=False),
        sa.Column("parsed_bucket", sa.String(64), nullable=False),
        sa.Column("parsed_object_key", sa.String(512), nullable=False),
        sa.Column("report_type", sa.String(2), nullable=False),
        sa.Column("template_id", sa.String(64), nullable=False),
        sa.Column("template_version", sa.String(32), nullable=False),
        sa.Column("template_snapshot", sa.JSON(), nullable=False),
        sa.Column("model_snapshot", sa.JSON(), nullable=False),
        sa.Column("document_manifest", sa.JSON(), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False, server_default="GENERATE"),
        sa.Column("language", sa.String(16), nullable=False, server_default="zh-CN"),
        sa.Column("reporting_year", sa.Integer(), nullable=True),
        sa.Column("user_instructions", sa.Text(), nullable=True),
        sa.Column("output_formats", sa.JSON(), nullable=False),
        sa.Column("llm_config_id", bigint, nullable=False),
        sa.Column("llm_snapshot_version", bigint, nullable=False),
        sa.Column("state", sa.String(32), nullable=False, server_default="PENDING"),
        sa.Column("stage", sa.String(32), nullable=False, server_default="WAITING"),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("available_at", sa.DateTime(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lease_token", sa.String(64), nullable=True),
        sa.Column("lease_owner", sa.String(128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("report_ir", sa.JSON(), nullable=True),
        sa.Column("checkpoint", sa.JSON(), nullable=True),
        sa.Column("analysis_coverage", sa.JSON(), nullable=True),
        sa.Column("validation_report", sa.JSON(), nullable=True),
        sa.Column("manifest", sa.JSON(), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_message", sa.String(1000), nullable=True),
        sa.Column("dispatch_status", sa.String(16), nullable=False, server_default="PENDING"),
        sa.Column("dispatch_attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("dispatch_available_at", sa.DateTime(), nullable=True),
        sa.Column("dispatch_lease_token", sa.String(64), nullable=True),
        sa.Column("dispatch_lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("dispatch_error", sa.String(1000), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_report_run_user_created", "report_run", ["user_id", "created_at"])
    op.create_index("idx_report_run_document_created", "report_run", ["document_id", "created_at"])
    op.create_index(
        "idx_report_run_state_available",
        "report_run",
        ["state", "available_at", "created_at"],
    )
    op.create_index(
        "idx_report_run_dispatch_available",
        "report_run",
        ["dispatch_status", "dispatch_available_at", "created_at"],
    )

    op.create_table(
        "report_question",
        sa.Column("id", bigint, primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("field_id", sa.String(128), nullable=False),
        sa.Column("question_type", sa.String(32), nullable=False),
        sa.Column("question", sa.String(1000), nullable=False),
        sa.Column("options", sa.JSON(), nullable=True),
        sa.Column("required", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("status", sa.String(16), nullable=False, server_default="OPEN"),
        sa.Column("answer", sa.JSON(), nullable=True),
        sa.Column("answered_by", bigint, nullable=True),
        sa.Column("answered_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("run_id", "field_id", "question_type", name="uk_report_question_field"),
    )
    op.create_index("idx_report_question_run_status", "report_question", ["run_id", "status", "id"])

    op.create_table(
        "report_artifact",
        sa.Column("id", bigint, primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("artifact_type", sa.String(32), nullable=False),
        sa.Column("bucket", sa.String(64), nullable=False),
        sa.Column("object_key", sa.String(512), nullable=False),
        sa.Column("content_type", sa.String(128), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("size_bytes", bigint, nullable=False),
        sa.Column("renderer_version", sa.String(32), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("run_id", "artifact_type", name="uk_report_artifact_type"),
    )
    op.create_index("idx_report_artifact_run", "report_artifact", ["run_id", "id"])


def downgrade() -> None:
    op.drop_index("idx_report_artifact_run", table_name="report_artifact")
    op.drop_table("report_artifact")
    op.drop_index("idx_report_question_run_status", table_name="report_question")
    op.drop_table("report_question")
    op.drop_index("idx_report_run_dispatch_available", table_name="report_run")
    op.drop_index("idx_report_run_state_available", table_name="report_run")
    op.drop_index("idx_report_run_document_created", table_name="report_run")
    op.drop_index("idx_report_run_user_created", table_name="report_run")
    op.drop_table("report_run")
    op.drop_column("llm_config", "supports_tool_calling")
