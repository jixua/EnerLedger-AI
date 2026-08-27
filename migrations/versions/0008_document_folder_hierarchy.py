"""add parent hierarchy to document folders

Revision ID: 0008_document_folder_hierarchy
Revises: 0007_crawler_document_review, 0007_document_folders
Create Date: 2026-08-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0008_document_folder_hierarchy"
down_revision: tuple[str, str] = (
    "0007_crawler_document_review",
    "0007_document_folders",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    unsigned_bigint = mysql.BIGINT(unsigned=True)
    op.add_column(
        "document_folder",
        sa.Column("parent_id", unsigned_bigint, nullable=True),
    )
    op.create_index(
        "idx_document_folder_parent",
        "document_folder",
        ["dataset_id", "parent_id", "id"],
    )


def downgrade() -> None:
    op.drop_index("idx_document_folder_parent", table_name="document_folder")
    op.drop_column("document_folder", "parent_id")
