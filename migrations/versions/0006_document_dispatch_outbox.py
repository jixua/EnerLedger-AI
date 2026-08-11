"""add recoverable document dispatch outbox state

Revision ID: 0006_document_dispatch_outbox
Revises: 0005_dataset_vision_config
Create Date: 2026-08-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_document_dispatch_outbox"
down_revision: str | None = "0005_dataset_vision_config"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "document",
        sa.Column("dispatch_status", sa.String(16), nullable=False, server_default="PENDING"),
    )
    op.add_column(
        "document",
        sa.Column("dispatch_attempt_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("document", sa.Column("dispatch_available_at", sa.DateTime(), nullable=True))
    op.add_column("document", sa.Column("dispatch_lease_token", sa.String(64), nullable=True))
    op.add_column("document", sa.Column("dispatch_lease_expires_at", sa.DateTime(), nullable=True))
    op.add_column("document", sa.Column("dispatch_error", sa.String(1000), nullable=True))
    op.execute(
        sa.text(
            "UPDATE document SET dispatch_status = "
            "CASE WHEN status IN ('QUEUED', 'PROCESSING') THEN 'PENDING' ELSE 'PUBLISHED' END, "
            "dispatch_available_at = CASE WHEN status IN ('QUEUED', 'PROCESSING') "
            "THEN CURRENT_TIMESTAMP ELSE NULL END"
        )
    )
    op.create_index(
        "idx_document_dispatch_available",
        "document",
        ["dispatch_status", "dispatch_available_at", "id"],
    )


def downgrade() -> None:
    op.drop_index("idx_document_dispatch_available", table_name="document")
    op.drop_column("document", "dispatch_error")
    op.drop_column("document", "dispatch_lease_expires_at")
    op.drop_column("document", "dispatch_lease_token")
    op.drop_column("document", "dispatch_available_at")
    op.drop_column("document", "dispatch_attempt_count")
    op.drop_column("document", "dispatch_status")
