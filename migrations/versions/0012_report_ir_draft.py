"""add report ir draft column

Revision ID: 0012_report_ir_draft
Revises: 0011_agent_conversations
Create Date: 2026-09-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_report_ir_draft"
down_revision: str = "0011_agent_conversations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("report_run", sa.Column("report_ir_draft", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("report_run", "report_ir_draft")
