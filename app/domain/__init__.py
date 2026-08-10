"""当前项目自有的业务域模型。

LinkRag 的解析与检索执行面位于 :mod:`app.rag`；本包承接原来由
LinkRag-Service 管理的 Dataset、Document 和 LLM 配置控制面。
"""

from app.domain.models import Dataset, Document

__all__ = ["Dataset", "Document"]
