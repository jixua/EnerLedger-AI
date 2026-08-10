"""Parser public API without importing every concrete backend at package startup."""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .base import BaseParser, IFileParser
    from .factory import ParserFactory

__all__ = [
    "BaseParser",
    "IFileParser",
    "ParserFactory",
]


def __getattr__(name: str) -> Any:
    if name in {"BaseParser", "IFileParser"}:
        from . import base

        return getattr(base, name)
    if name == "ParserFactory":
        from .factory import ParserFactory

        return ParserFactory
    raise AttributeError(name)
