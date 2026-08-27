"""add virtual folders for dataset documents

Revision ID: 0007_document_folders
Revises: 0006_document_dispatch_outbox
Create Date: 2026-08-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0007_document_folders"
down_revision: str | None = "0006_document_dispatch_outbox"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    unsigned_bigint = mysql.BIGINT(unsigned=True)
    op.create_table(
        "document_folder",
        sa.Column("id", unsigned_bigint, autoincrement=True, nullable=False),
        sa.Column("dataset_id", unsigned_bigint, nullable=False),
        sa.Column("user_id", unsigned_bigint, nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "dataset_id",
            "name",
            name="uk_document_folder_user_dataset_name",
        ),
    )
    op.create_index(
        "idx_document_folder_dataset",
        "document_folder",
        ["user_id", "dataset_id", "created_at"],
    )
    op.add_column("document", sa.Column("folder_id", unsigned_bigint, nullable=True))
    op.create_index("idx_document_folder", "document", ["folder_id", "id"])


def downgrade() -> None:
    op.drop_index("idx_document_folder", table_name="document")
    op.drop_column("document", "folder_id")
    op.drop_table("document_folder")
