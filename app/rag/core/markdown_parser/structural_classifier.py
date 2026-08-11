"""共享的文档结构文本分类器。

本模块只负责判断单行文本“像哪一类结构”；是否真正作为分片边界，
由 splitter 结合全文编号一致性决定。标题层级增强与 splitter 共用这里的
规则，避免两套正则漂移。
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class StructuralTextClue:
    """单行文本的结构线索。"""

    kind: str
    level: int
    text: str
    numeric_label: tuple[int, ...] | None = None
    title: str | None = None


class StructuralTextClassifier:
    """法规、标准和政策大纲的共享单行分类器。"""

    LEGAL_NUMBER = r"[零〇○一二三四五六七八九十百千万亿两\d]+"
    LEGAL_DIVISION_RE = re.compile(
        rf"^第{LEGAL_NUMBER}(?P<unit>[编篇章节])(?:[\s\u3000]+.*)?$"
    )
    LEGAL_ARTICLE_RE = re.compile(rf"^第{LEGAL_NUMBER}条(?:[\s\u3000]*|(?=[，。：:]))")
    DECIMAL_RE = re.compile(
        r"^(?P<label>\d{1,3}(?:\.\d{1,3}){0,4})(?:[\s\u3000]+(?P<title>\S.*))?$"
    )
    ROOT_NUMBERED_RE = re.compile(r"^(?P<label>\d{1,3})[.、]\s*(?P<title>\S.*)$")
    APPENDIX_RE = re.compile(r"^附录\s*[A-Z一二三四五六七八九十\d]+(?:[\s\u3000]+.*)?$")
    CHINESE_OUTLINE_RE = re.compile(
        r"^(?P<label>[一二三四五六七八九十]+)、\s*(?P<title>\S.*)$"
    )
    PAREN_CHINESE_RE = re.compile(
        r"^[（(](?P<label>[一二三四五六七八九十]+)[）)]\s*(?P<title>\S.*)$"
    )
    DIGIT_PAREN_RE = re.compile(r"^(?P<label>\d{1,3})[）)]\s*(?P<title>\S.*)$")

    DIVISION_LEVEL = {"编": 2, "篇": 2, "章": 2, "节": 3}

    @classmethod
    def classify(cls, text: str) -> StructuralTextClue | None:
        """识别单行结构线索，不在此判定全文边界强度。"""
        normalized = re.sub(r"\s*[．]\s*", ".", text.strip())
        if not normalized:
            return None

        division = cls.LEGAL_DIVISION_RE.fullmatch(normalized)
        if division:
            unit = division.group("unit")
            return StructuralTextClue(
                kind=f"legal_{unit}",
                level=cls.DIVISION_LEVEL[unit],
                text=normalized,
            )
        if cls.LEGAL_ARTICLE_RE.match(normalized):
            return StructuralTextClue(kind="legal_article", level=3, text=normalized)
        if cls.APPENDIX_RE.fullmatch(normalized):
            return StructuralTextClue(kind="appendix", level=2, text=normalized)

        decimal = cls.DECIMAL_RE.fullmatch(normalized)
        if decimal:
            return StructuralTextClue(
                kind="decimal_section",
                level=min(5, len(decimal.group("label").split(".")) + 1),
                text=normalized,
                numeric_label=tuple(int(part) for part in decimal.group("label").split(".")),
                title=decimal.group("title"),
            )

        root_numbered = cls.ROOT_NUMBERED_RE.fullmatch(normalized)
        if root_numbered:
            return StructuralTextClue(
                kind="decimal_section",
                level=2,
                text=normalized,
                numeric_label=(int(root_numbered.group("label")),),
                title=root_numbered.group("title"),
            )

        for pattern, level, kind in (
            (cls.CHINESE_OUTLINE_RE, 2, "chinese_outline"),
            (cls.PAREN_CHINESE_RE, 3, "chinese_subsection"),
            (cls.DIGIT_PAREN_RE, 4, "digit_parenthesized_outline"),
        ):
            match = pattern.fullmatch(normalized)
            if match:
                return StructuralTextClue(
                    kind=kind,
                    level=level,
                    text=normalized,
                    title=match.group("title"),
                )
        return None

    @classmethod
    def is_hierarchy_clue(cls, text: str) -> bool:
        """返回文本是否可作为标题层级线索。

        纯数字不能单独成为层级线索，避免页码触发标题增强。
        """
        clue = cls.classify(text)
        if clue is None:
            return False
        if clue.kind == "decimal_section":
            return bool(clue.title and clue.numeric_label and len(clue.numeric_label) > 1)
        if clue.kind == "digit_parenthesized_outline":
            return False
        return True


__all__ = ["StructuralTextClassifier", "StructuralTextClue"]
