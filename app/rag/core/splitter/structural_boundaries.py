"""从丢失 Markdown heading 语义的普通段落中识别确定性文档结构。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.rag.core.markdown_parser import ElementType, MarkdownElement
from app.rag.core.markdown_parser.structural_classifier import StructuralTextClassifier

META_STRUCTURAL_HEADING = "structural_heading"


class BoundaryStrength(StrEnum):
    """边界强度：hard 不允许跨上位结构合并，candidate 受 token 软下限约束。"""

    HARD = "hard"
    CANDIDATE = "candidate"


@dataclass(frozen=True, slots=True)
class StructuralBoundary:
    """非 Markdown 结构边界。"""

    kind: str
    level: int
    strength: BoundaryStrength
    text: str


@dataclass(frozen=True, slots=True)
class _RawNumberedBoundary:
    index: int
    label: tuple[int, ...]
    text: str
    has_title: bool


class StructuralBoundaryDetector:
    """基于全文一致性识别法规、标准和政策文档的编号层级。"""

    _MAX_HEADING_TEXT_LENGTH = 120

    @staticmethod
    def _lines(element: MarkdownElement) -> list[str]:
        return [line.strip() for line in element.content.splitlines() if line.strip()]

    @classmethod
    def _short_heading_text(cls, lines: list[str]) -> str:
        if not lines:
            return ""
        first = lines[0].replace("．", ".")
        if len(first) > cls._MAX_HEADING_TEXT_LENGTH:
            return ""
        return first

    @staticmethod
    def _sequence_prefixes(
        candidates: list[_RawNumberedBoundary],
    ) -> set[tuple[int, ...]]:
        """返回确实出现同层连续编号的父级前缀。"""
        sibling_values: dict[tuple[int, ...], set[int]] = {}
        for candidate in candidates:
            prefix = candidate.label[:-1]
            sibling_values.setdefault(prefix, set()).add(candidate.label[-1])
        return {
            prefix
            for prefix, values in sibling_values.items()
            if any(value + 1 in values for value in values)
        }

    @classmethod
    def _adjacent_decimal_title(
        cls,
        elements: list[MarkdownElement],
        index: int,
    ) -> str | None:
        """仅为 1.1 类多级编号吸收紧邻的短标题元素。"""
        if index + 1 >= len(elements):
            return None
        following = elements[index + 1]
        if following.type != ElementType.PARAGRAPH:
            return None
        lines = cls._lines(following)
        if len(lines) != 1:
            return None
        title = cls._short_heading_text(lines)
        if not title or len(title) > 60 or title[-1:] in "。！？；;":
            return None
        if StructuralTextClassifier.classify(title) is not None:
            return None
        current_page = elements[index].metadata.get("page_number")
        following_page = following.metadata.get("page_number")
        if (
            current_page is not None
            and following_page is not None
            and current_page != following_page
        ):
            return None
        return title

    @classmethod
    def detect(cls, elements: list[MarkdownElement]) -> dict[int, StructuralBoundary]:
        """返回元素索引到结构边界的映射；原生 Markdown heading 永远不重复识别。"""
        boundaries: dict[int, StructuralBoundary] = {}
        numbered: list[_RawNumberedBoundary] = []
        chinese_outline: list[tuple[int, str, int, str]] = []

        for index, element in enumerate(elements):
            if element.type != ElementType.PARAGRAPH:
                continue
            lines = cls._lines(element)
            text = cls._short_heading_text(lines)
            if not text:
                continue

            clue = StructuralTextClassifier.classify(text)
            if clue is None:
                continue
            if clue.kind.startswith("legal_") and clue.kind != "legal_article":
                boundaries[index] = StructuralBoundary(
                    kind=clue.kind,
                    level=clue.level,
                    strength=BoundaryStrength.HARD,
                    text=text,
                )
                continue
            if clue.kind == "legal_article":
                boundaries[index] = StructuralBoundary(
                    kind="legal_article",
                    level=3,
                    strength=BoundaryStrength.CANDIDATE,
                    text=text,
                )
                continue
            if clue.kind == "appendix":
                boundaries[index] = StructuralBoundary(
                    kind="appendix",
                    level=2,
                    strength=BoundaryStrength.HARD,
                    text=text,
                )
                continue

            if clue.kind == "decimal_section" and clue.numeric_label is not None:
                label = clue.numeric_label
                display = text
                title = clue.title
                if title is None and len(label) > 1 and len(lines) > 1:
                    display = f"{text} {lines[1]}"[: cls._MAX_HEADING_TEXT_LENGTH]
                    title = lines[1]
                elif title is None and len(label) > 1:
                    title = cls._adjacent_decimal_title(elements, index)
                    if title:
                        display = f"{text} {title}"[: cls._MAX_HEADING_TEXT_LENGTH]

                # 纯数字页码永不参与结构判定；只允许多级编号吸收相邻短标题。
                if title is None:
                    continue
                numbered.append(
                    _RawNumberedBoundary(
                        index=index,
                        label=label,
                        text=display,
                        has_title=True,
                    )
                )
                continue

            if clue.kind in {
                "chinese_outline",
                "chinese_subsection",
            }:
                chinese_outline.append((index, text, clue.level, clue.kind))

        sequence_prefixes = cls._sequence_prefixes(numbered)
        has_nested_sequence = any(prefix for prefix in sequence_prefixes)
        for candidate in numbered:
            depth = len(candidate.label)
            is_supported_root = depth == 1 and has_nested_sequence and candidate.has_title
            is_supported_nested = depth > 1 and candidate.label[:-1] in sequence_prefixes
            if not (is_supported_root or is_supported_nested):
                continue
            boundaries[candidate.index] = StructuralBoundary(
                kind="decimal_section",
                level=min(5, depth + 1),
                strength=(BoundaryStrength.HARD if depth == 1 else BoundaryStrength.CANDIDATE),
                text=candidate.text,
            )

        # 必须有至少两个同类项；「一、」+「（一）」不能相互充当一致性证据。
        enabled_outline_kinds = {
            kind
            for kind in {item[3] for item in chinese_outline}
            if sum(1 for item in chinese_outline if item[3] == kind) >= 2
        }
        for index, text, level, kind in chinese_outline:
            if kind not in enabled_outline_kinds:
                continue
            boundaries[index] = StructuralBoundary(
                kind=kind,
                level=level,
                strength=(
                    BoundaryStrength.HARD
                    if kind == "chinese_outline"
                    else BoundaryStrength.CANDIDATE
                ),
                text=text,
            )

        return boundaries


__all__ = [
    "META_STRUCTURAL_HEADING",
    "BoundaryStrength",
    "StructuralBoundary",
    "StructuralBoundaryDetector",
]
