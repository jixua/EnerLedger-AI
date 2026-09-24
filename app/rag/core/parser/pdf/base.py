from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path


class BasePdfBackend(ABC):
    """PDF 解析后端抽象。"""

    name: str

    def __init__(self) -> None:
        self.metadata: dict = {}

    @abstractmethod
    def parse(self, source: Path | None, options):
        """将本地 PDF 文件解析为 Markdown。"""
