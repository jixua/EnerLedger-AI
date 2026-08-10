"""PDF parser public API without eager backend/runtime imports."""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.rag.core.parser.pdf.models import PdfParseOptions
    from app.rag.core.parser.pdf.service import PdfParserService

__all__ = ["PdfParserService", "PdfParseOptions"]


def __getattr__(name: str) -> Any:
    if name == "PdfParserService":
        from app.rag.core.parser.pdf.service import PdfParserService

        return PdfParserService
    if name == "PdfParseOptions":
        from app.rag.core.parser.pdf.models import PdfParseOptions

        return PdfParseOptions
    raise AttributeError(name)
