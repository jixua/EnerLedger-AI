"""document parse queue fields

Revision ID: 0002_document_parse_queue
Revises: 0001_minimal_rag
Create Date: 2026-08-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_document_parse_queue"
down_revision: str | None = "0001_minimal_rag"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """在 document 表内加入 durable queue/lease 字段，不增加业务表。"""

    op.add_column(
        "document",
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "document",
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
    )
    op.add_column("document", sa.Column("available_at", sa.DateTime(), nullable=True))
    op.add_column("document", sa.Column("lease_token", sa.String(64), nullable=True))
    op.add_column("document", sa.Column("lease_owner", sa.String(128), nullable=True))
    op.add_column("document", sa.Column("lease_expires_at", sa.DateTime(), nullable=True))
    op.add_column("document", sa.Column("queued_at", sa.DateTime(), nullable=True))
    op.add_column("document", sa.Column("processing_started_at", sa.DateTime(), nullable=True))
    op.add_column("document", sa.Column("finished_at", sa.DateTime(), nullable=True))
    op.add_column("document", sa.Column("error_code", sa.String(64), nullable=True))
    op.add_column(
        "document",
        sa.Column("reparse_requested", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.add_column(
        "document_chunk",
        sa.Column("document_version", sa.Integer(), server_default="1", nullable=False),
    )
    op.add_column("document_chunk", sa.Column("start_page", sa.Integer(), nullable=True))
    op.add_column("document_chunk", sa.Column("end_page", sa.Integer(), nullable=True))

    # 老版本的 PROCESSING 记录没有可恢复租约；迁移为立即可消费的 QUEUED。
    op.execute(
        sa.text(
            "UPDATE document SET status = 'QUEUED', "
            "available_at = CURRENT_TIMESTAMP, queued_at = CURRENT_TIMESTAMP "
            "WHERE status = 'PROCESSING'"
        )
    )
    op.execute(
        sa.text(
            "UPDATE document SET available_at = CURRENT_TIMESTAMP, "
            "queued_at = COALESCE(queued_at, created_at) WHERE status = 'QUEUED'"
        )
    )
    op.alter_column(
        "document",
        "status",
        existing_type=sa.String(16),
        existing_nullable=False,
        server_default="QUEUED",
    )
    op.create_index(
        "idx_document_queue_available",
        "document",
        ["status", "available_at", "id"],
    )
    op.create_index(
        "idx_document_lease_expiry",
        "document",
        ["status", "lease_expires_at", "id"],
    )


def downgrade() -> None:
    op.drop_index("idx_document_lease_expiry", table_name="document")
    op.drop_index("idx_document_queue_available", table_name="document")
    op.alter_column(
        "document",
        "status",
        existing_type=sa.String(16),
        existing_nullable=False,
        server_default="PROCESSING",
    )
    op.drop_column("document", "reparse_requested")
    op.drop_column("document", "error_code")
    op.drop_column("document", "finished_at")
    op.drop_column("document", "processing_started_at")
    op.drop_column("document", "queued_at")
    op.drop_column("document", "lease_expires_at")
    op.drop_column("document", "lease_owner")
    op.drop_column("document", "lease_token")
    op.drop_column("document", "available_at")
    op.drop_column("document", "attempt_count")
    op.drop_column("document", "version")
    op.drop_column("document_chunk", "end_page")
    op.drop_column("document_chunk", "start_page")
    op.drop_column("document_chunk", "document_version")
