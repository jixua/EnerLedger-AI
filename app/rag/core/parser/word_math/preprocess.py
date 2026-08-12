from __future__ import annotations

import shutil
import zipfile
from pathlib import Path

from bs4 import BeautifulSoup, Tag
from defusedxml import ElementTree as ET

from .omml import OMML_NS, oMath2Latex

MATH_PARTS = {"word/document.xml", "word/footnotes.xml", "word/endnotes.xml"}
WORD_PAGE_BREAK_SENTINEL = "[[LINKPARSE_WORD_PAGE_BREAK_7F3A2C91]]"
MATH_ROOT_TEMPLATE = "".join(
    (
        '<w:document xmlns:r="http://schemas.openxmlformats.org/'
        'officeDocument/2006/relationships" ',
        'xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math" ',
        'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">',
        "{0}</w:document>",
    )
)


def _convert_omath_to_latex(tag: Tag) -> str:
    root = ET.fromstring(MATH_ROOT_TEMPLATE.format(str(tag)))
    math_element = root.find(OMML_NS + "oMath")
    if math_element is None:
        raise ValueError("OMML formula root is missing")
    return oMath2Latex(math_element).latex


def _replacement(tag: Tag, *, block: bool) -> Tag:
    text = Tag(name="w:t")
    latex = _convert_omath_to_latex(tag)
    text.string = f"$${latex}$$" if block else f"${latex}$"
    run = Tag(name="w:r")
    run.append(text)
    return run


def _page_break_text() -> Tag:
    text = Tag(name="w:t")
    text.string = WORD_PAGE_BREAK_SENTINEL
    return text


def _preprocess_xml(content: bytes, *, include_page_breaks: bool) -> tuple[bytes, int, int]:
    soup = BeautifulSoup(content.decode("utf-8"), features="xml")
    formula_count = 0
    for paragraph in list(soup.find_all("oMathPara")):
        replacement = Tag(name="w:p")
        formulas = list(paragraph.find_all("oMath"))
        for formula in formulas:
            replacement.append(_replacement(formula, block=True))
            formula_count += 1
        paragraph.replace_with(replacement)
    for formula in list(soup.find_all("oMath")):
        formula.replace_with(_replacement(formula, block=False))
        formula_count += 1

    page_break_count = 0
    if include_page_breaks:
        for page_break in list(soup.find_all("br")):
            if page_break.get("w:type") != "page":
                continue
            page_break.replace_with(_page_break_text())
            page_break_count += 1
        for rendered_break in list(soup.find_all("lastRenderedPageBreak")):
            rendered_break.replace_with(_page_break_text())
            page_break_count += 1

    return str(soup).encode("utf-8"), formula_count, page_break_count


def preprocess_docx(source: Path, target: Path) -> tuple[Path, int, int, list[str]]:
    """Expose formulas and saved page breaks to Mammoth without reflowing the document."""
    replacements: dict[str, bytes] = {}
    formula_count = 0
    page_break_count = 0
    warnings: list[str] = []

    with zipfile.ZipFile(source) as archive:
        for name in MATH_PARTS:
            try:
                content = archive.read(name)
            except KeyError:
                continue
            if name != "word/document.xml" and b"oMath" not in content:
                continue
            try:
                updated, formulas, page_breaks = _preprocess_xml(
                    content, include_page_breaks=name == "word/document.xml"
                )
                if formulas or page_breaks:
                    replacements[name] = updated
                    formula_count += formulas
                    page_break_count += page_breaks
            except Exception as exc:
                warnings.append(f"Word preprocessing failed in {name}: {exc}")

        if not replacements:
            return source, 0, 0, warnings

        target.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(target, "w") as output:
            output.comment = archive.comment
            for info in archive.infolist():
                if info.filename in replacements:
                    output.writestr(info, replacements[info.filename])
                    continue
                with archive.open(info) as input_member, output.open(info, "w") as output_member:
                    shutil.copyfileobj(input_member, output_member, length=1024 * 1024)
    return target, formula_count, page_break_count, warnings
