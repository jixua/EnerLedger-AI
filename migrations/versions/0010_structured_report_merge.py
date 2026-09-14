"""merge report platform and structured data branches

Revision ID: 0010_structured_report_merge
Revises: 0009_report_platform_foundation, 0009_structured_assets
Create Date: 2026-08-31
"""

from collections.abc import Sequence

revision: str = "0010_structured_report_merge"
down_revision: tuple[str, str] = (
    "0009_report_platform_foundation",
    "0009_structured_assets",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
