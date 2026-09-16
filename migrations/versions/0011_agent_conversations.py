"""add durable agent conversations

Revision ID: 0011_agent_conversations
Revises: 0010_structured_report_merge
Create Date: 2026-09-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0011_agent_conversations"
down_revision: str = "0010_structured_report_merge"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "report_run",
        sa.Column("custom_template_document_id", mysql.BIGINT(unsigned=True), nullable=True),
    )
    op.add_column(
        "report_run", sa.Column("custom_template_document_version", sa.Integer(), nullable=True)
    )
    op.add_column("report_run", sa.Column("custom_template_manifest", sa.JSON(), nullable=True))
    op.create_table(
        "agent_conversation",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("user_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("title", sa.String(160), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_agent_conversation_user_updated",
        "agent_conversation",
        ["user_id", "updated_at"],
    )
    op.create_table(
        "agent_conversation_turn",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("conversation_id", sa.String(36), nullable=False),
        sa.Column("user_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("turn_index", sa.Integer(), nullable=False),
        sa.Column("user_content", sa.Text(), nullable=False),
        sa.Column("assistant_content", mysql.LONGTEXT(), nullable=True),
        sa.Column("status", sa.String(20), server_default="STREAMING", nullable=False),
        sa.Column("dataset_ids", sa.JSON(), nullable=False),
        sa.Column("document_ids", sa.JSON(), nullable=False),
        sa.Column("llm_config_id", mysql.BIGINT(unsigned=True), nullable=True),
        sa.Column("attachments", sa.JSON(), nullable=False),
        sa.Column("interaction", sa.JSON(), nullable=True),
        sa.Column("report_run_id", sa.String(36), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_message", sa.String(1000), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("conversation_id", "turn_index", name="uk_agent_turn_index"),
    )
    op.create_index(
        "idx_agent_turn_conversation",
        "agent_conversation_turn",
        ["conversation_id", "turn_index"],
    )
    op.create_index(
        "idx_agent_turn_user_created",
        "agent_conversation_turn",
        ["user_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("agent_conversation_turn")
    op.drop_table("agent_conversation")
    op.drop_column("report_run", "custom_template_manifest")
    op.drop_column("report_run", "custom_template_document_version")
    op.drop_column("report_run", "custom_template_document_id")
