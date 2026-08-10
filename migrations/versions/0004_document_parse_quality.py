"""add document parse quality fields

Revision ID: 0004_document_parse_quality
Revises: 0003_chunk_structure_metadata
Create Date: 2026-08-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_document_parse_quality"
down_revision: str | None = "0003_chunk_structure_metadata"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add nullable parse quality fields without changing document readiness."""

    op.add_column(
        "document",
        sa.Column("parse_quality_status", sa.String(32), nullable=True),
    )
    op.add_column(
        "document",
        sa.Column("parse_quality", sa.JSON(), nullable=True),
    )
    op.execute(
        sa.text(
            "UPDATE document SET parse_quality_status = "
            "CASE WHEN LOWER(file_type) = 'pdf' "
            "THEN 'LEGACY_UNCHECKED' ELSE 'NOT_APPLICABLE' END "
            "WHERE parse_quality_status IS NULL"
        )
    )


def downgrade() -> None:
    op.drop_column("document", "parse_quality")
    op.drop_column("document", "parse_quality_status")
