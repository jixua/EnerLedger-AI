"""当前项目的 SQLAlchemy 声明基类与最小 LLM 配置表。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Index, String, UniqueConstraint, func, true
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.domain.time import utc_now

UnsignedBigInteger = BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql")


class Base(DeclarativeBase):
    """当前核心业务表共用的声明基类。"""


class LLMModelConfigDB(Base):
    """一条可直接执行的模型配置。

    类名沿用 LinkRag 的 runtime 契约，物理表收敛为 ``llm_config``。厂商目录、
    默认关系和用量日志都不再单独建表。
    """

    __tablename__ = "llm_config"
    __table_args__ = (
        UniqueConstraint(
            "scope",
            "owner_user_id",
            "provider_type",
            "model_name",
            "capability",
            name="uk_llm_config_owner_model",
        ),
        Index("idx_llm_config_owner_capability", "owner_user_id", "capability", "is_active"),
    )

    id: Mapped[int] = mapped_column(UnsignedBigInteger, primary_key=True, autoincrement=True)
    scope: Mapped[str] = mapped_column(
        String(16), nullable=False, default="USER", server_default="USER"
    )
    owner_user_id: Mapped[int] = mapped_column(UnsignedBigInteger, nullable=False)
    # RuntimeModelConfig 保留该字段作为快照身份；当前单表版本不再建立 provider 外键。
    provider_id: Mapped[int] = mapped_column(
        UnsignedBigInteger, nullable=False, default=1, server_default="1"
    )
    provider_type: Mapped[str] = mapped_column(String(32), nullable=False)
    model_name: Mapped[str] = mapped_column(String(128), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    capability: Mapped[str] = mapped_column(String(32), nullable=False)
    protocol: Mapped[str] = mapped_column(String(32), nullable=False)
    api_base_url: Mapped[str] = mapped_column(String(512), nullable=False)
    api_key: Mapped[str] = mapped_column(String(512), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=true()
    )
    snapshot_version: Mapped[int] = mapped_column(
        UnsignedBigInteger, nullable=False, default=1, server_default="1"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utc_now, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
        server_default=func.current_timestamp(),
    )
