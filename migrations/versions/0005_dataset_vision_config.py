"""add dataset vision model binding

Revision ID: 0005_dataset_vision_config
Revises: 0004_document_parse_quality
Create Date: 2026-08-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0005_dataset_vision_config"
down_revision: str | None = "0004_document_parse_quality"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Bind an optional VISION config directly on each dataset."""

    op.add_column(
        "dataset",
        sa.Column("vision_config_id", mysql.BIGINT(unsigned=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("dataset", "vision_config_id")
