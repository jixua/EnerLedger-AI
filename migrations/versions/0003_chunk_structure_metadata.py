"""persist structured chunk metadata

Revision ID: 0003_chunk_structure_metadata
Revises: 0002_document_parse_queue
Create Date: 2026-08-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_chunk_structure_metadata"
down_revision: str | None = "0002_document_parse_queue"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """在既有 Chunk 真值表内保存受控的 LinkRag 分片结构信息。"""

    op.add_column(
        "document_chunk",
        sa.Column("structure_metadata", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("document_chunk", "structure_metadata")
