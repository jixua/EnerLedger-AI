"""当前项目自有的业务域模型。

LinkRag 的解析与检索执行面位于 :mod:`app.rag`；本包承接原来由
LinkRag-Service 管理的 Dataset、Document 和 LLM 配置控制面。

这里不能在包初始化阶段直接导入 SQLAlchemy 模型：``db_models`` 读取
``app.domain.time`` 时 Python 会先执行本文件，若再反向导入 ``models``，就会在
``Base`` 尚未定义时形成循环导入。对旧的 ``from app.domain import Dataset``
入口使用惰性属性即可兼容。
"""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.domain.models import Dataset, Document

__all__ = ["Dataset", "Document"]


def __getattr__(name: str) -> Any:
    if name in __all__:
        from app.domain import models

        return getattr(models, name)
    raise AttributeError(name)
