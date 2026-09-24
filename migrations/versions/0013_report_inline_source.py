"""conversation-inline report source

对话里直传的材料不再进知识库，报告来源因此有了第二种形态：没有文档可指的
``INLINE``。这一版把 ``report_run`` 上「一定有文档」的三个字段放开为可空，加上
来源标记与用户原文件名，并建一张材料暂存表。

``parsed_bucket`` / ``parsed_object_key`` / ``document_manifest`` 刻意不动：它们名里
带 document，语义其实是「来源正文放在哪、来源分片清单是什么」，两种来源都有。

Revision ID: 0013_report_inline_source
Revises: 0012_report_ir_draft
Create Date: 2026-09-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_report_inline_source"
down_revision: str = "0012_report_ir_draft"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("report_run", "dataset_id", existing_type=sa.BigInteger(), nullable=True)
    op.alter_column("report_run", "document_id", existing_type=sa.BigInteger(), nullable=True)
    op.alter_column("report_run", "document_version", existing_type=sa.Integer(), nullable=True)
    op.add_column(
        "report_run",
        sa.Column("source_kind", sa.String(16), nullable=False, server_default="DOCUMENT"),
    )
    op.add_column(
        "report_run", sa.Column("inline_source_filename", sa.String(512), nullable=True)
    )

    op.create_table(
        "report_material",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("filename", sa.String(512), nullable=False),
        sa.Column("char_count", sa.Integer(), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("bucket", sa.String(64), nullable=False),
        sa.Column("object_key", sa.String(512), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("consumed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "idx_report_material_user_created", "report_material", ["user_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("idx_report_material_user_created", table_name="report_material")
    op.drop_table("report_material")

    # 回退到「一定有文档」：先把内联来源的任务删掉，否则这三列加不上 NOT NULL。
    op.execute("DELETE FROM report_run WHERE source_kind = 'INLINE'")
    op.drop_column("report_run", "inline_source_filename")
    op.drop_column("report_run", "source_kind")
    op.alter_column("report_run", "document_version", existing_type=sa.Integer(), nullable=False)
    op.alter_column("report_run", "document_id", existing_type=sa.BigInteger(), nullable=False)
    op.alter_column("report_run", "dataset_id", existing_type=sa.BigInteger(), nullable=False)
