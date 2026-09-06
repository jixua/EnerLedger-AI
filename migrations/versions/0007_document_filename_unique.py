"""prevent duplicate filenames within a user's dataset

Revision ID: 0007_document_filename_unique
Revises: 0006_document_dispatch_outbox
Create Date: 2026-08-12
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0007_document_filename_unique"
down_revision: str | None = "0006_document_dispatch_outbox"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uk_document_user_dataset_filename",
        "document",
        ["user_id", "dataset_id", "filename"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uk_document_user_dataset_filename",
        "document",
        type_="unique",
    )
