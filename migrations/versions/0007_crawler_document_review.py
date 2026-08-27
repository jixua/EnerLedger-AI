"""add crawler source metadata and review state

Revision ID: 0007_crawler_document_review
Revises: 0006_document_dispatch_outbox
Create Date: 2026-08-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0007_crawler_document_review"
down_revision: str | None = "0006_document_dispatch_outbox"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    unsigned_bigint = mysql.BIGINT(unsigned=True)
    op.add_column(
        "document",
        sa.Column("source_type", sa.String(32), nullable=False, server_default="MANUAL_UPLOAD"),
    )
    op.add_column("document", sa.Column("source_url", sa.String(1024), nullable=True))
    op.add_column("document", sa.Column("source_title", sa.String(512), nullable=True))
    op.add_column("document", sa.Column("source_metadata", sa.JSON(), nullable=True))
    op.add_column(
        "document",
        sa.Column("review_status", sa.String(16), nullable=False, server_default="NOT_REQUIRED"),
    )
    op.add_column("document", sa.Column("review_note", sa.String(1000), nullable=True))
    op.add_column(
        "document", sa.Column("reviewed_by_user_id", unsigned_bigint, nullable=True)
    )
    op.add_column("document", sa.Column("reviewed_at", sa.DateTime(), nullable=True))
    op.create_index(
        "idx_document_review_status",
        "document",
        ["user_id", "review_status", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_document_review_status", table_name="document")
    op.drop_column("document", "reviewed_at")
    op.drop_column("document", "reviewed_by_user_id")
    op.drop_column("document", "review_note")
    op.drop_column("document", "review_status")
    op.drop_column("document", "source_metadata")
    op.drop_column("document", "source_title")
    op.drop_column("document", "source_url")
    op.drop_column("document", "source_type")
