"""Word 排版基础件：字体、样式、表格、页码。

报告产物（``app/services/report_docx.py``）与其他需要出 Word 的场景共用这里的
实现，避免各自维护一套排版规则。这里只管「一个段落、一张表格怎么长得像中文公文」，
不认识 ReportIR，也不解析任何中间格式。

页面按 A4、上下 3cm／2.5cm 边距、正文宋体小四、标题黑体排；表格统一三线以内的
细网格线，表头行加底色并在跨页时重复。
"""

from __future__ import annotations

from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

BODY_FONT = "宋体"
HEADING_FONT = "黑体"
COVER_FONT = "微软雅黑"
LATIN_FONT = "Times New Roman"
MONO_FONT = "Consolas"

# A4 去掉左右页边距后的正文宽度（twips）：21cm - 3cm - 2.5cm = 15.5cm ≈ 8789，
# 留一点余量避免 Word 因舍入把表格撑出版心。
TABLE_WIDTH_DXA = 8700


def set_run_font(
    run,
    *,
    size: float = 12,
    bold: bool | None = None,
    east_asia: str = BODY_FONT,
    latin: str = LATIN_FONT,
    color: str | None = None,
) -> None:
    """显式指定中西文字体：Word 不会自动用中文字体渲染汉字，只在样式上设 latin 字体，
    汉字会退回默认宋体以外的字体，导出的文件在不同机器上长相不一。"""
    run.font.name = latin
    run.font.size = Pt(size)
    fonts = run._element.get_or_add_rPr().rFonts
    fonts.set(qn("w:ascii"), latin)
    fonts.set(qn("w:hAnsi"), latin)
    fonts.set(qn("w:cs"), latin)
    fonts.set(qn("w:eastAsia"), east_asia)
    if bold is not None:
        run.bold = bold
    if color:
        run.font.color.rgb = RGBColor.from_string(color)


def set_cell_margins(
    cell,
    *,
    top: int = 90,
    start: int = 110,
    bottom: int = 90,
    end: int = 110,
) -> None:
    tc_pr = cell._tc.get_or_add_tcPr()
    margins = tc_pr.first_child_found_in("w:tcMar")
    if margins is None:
        margins = OxmlElement("w:tcMar")
        tc_pr.append(margins)
    for name, value in (("top", top), ("start", start), ("bottom", bottom), ("end", end)):
        node = margins.find(qn(f"w:{name}"))
        if node is None:
            node = OxmlElement(f"w:{name}")
            margins.append(node)
        node.set(qn("w:w"), str(value))
        node.set(qn("w:type"), "dxa")


def set_cell_shading(cell, fill: str) -> None:
    """单元格底色。图表里的条形就是靠它画的：底色连成一条就是一根条。"""
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), fill.lstrip("#").upper())
    cell._tc.get_or_add_tcPr().append(shd)


def set_paragraph_shading(paragraph, fill: str) -> None:
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), fill.lstrip("#").upper())
    paragraph._p.get_or_add_pPr().append(shd)


def set_paragraph_left_border(paragraph, color: str, *, size: int = 18) -> None:
    """左侧竖线：提示块用它代替整块底色（与 HTML 产物的提示样式一致）。"""
    p_pr = paragraph._p.get_or_add_pPr()
    borders = OxmlElement("w:pBdr")
    left = OxmlElement("w:left")
    left.set(qn("w:val"), "single")
    left.set(qn("w:sz"), str(size))
    left.set(qn("w:space"), "8")
    left.set(qn("w:color"), color.lstrip("#").upper())
    borders.append(left)
    p_pr.append(borders)


def repeat_table_header(row) -> None:
    tr_pr = row._tr.get_or_add_trPr()
    marker = OxmlElement("w:tblHeader")
    marker.set(qn("w:val"), "true")
    tr_pr.append(marker)


def add_page_number(paragraph, *, prefix: str = "", suffix: str = "") -> None:
    """在页脚段落里插入 PAGE 域；Word 打开文档时刷新。"""
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    if prefix:
        set_run_font(paragraph.add_run(prefix), size=9, color="808080")
    run = paragraph.add_run()
    set_run_font(run, size=9, color="808080")
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    instruction = OxmlElement("w:instrText")
    instruction.set(qn("xml:space"), "preserve")
    instruction.text = " PAGE "
    separate = OxmlElement("w:fldChar")
    separate.set(qn("w:fldCharType"), "separate")
    text = OxmlElement("w:t")
    text.text = "1"
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    run._r.extend((begin, instruction, separate, text, end))
    if suffix:
        set_run_font(paragraph.add_run(suffix), size=9, color="808080")


def enable_field_update(document) -> None:
    """让 Word 打开文档时自动刷新页码等域。没有这一条，页码会停在模板里的占位值。"""

    settings_element = document.settings._element
    update_fields = settings_element.find(qn("w:updateFields"))
    if update_fields is None:
        update_fields = OxmlElement("w:updateFields")
        settings_element.append(update_fields)
    update_fields.set(qn("w:val"), "true")


def _set_style_fonts(style, *, east_asia: str, size: float, bold: bool | None) -> None:
    style.font.name = LATIN_FONT
    style.font.size = Pt(size)
    style.font.bold = bold
    fonts = style._element.get_or_add_rPr().rFonts
    fonts.set(qn("w:ascii"), LATIN_FONT)
    fonts.set(qn("w:hAnsi"), LATIN_FONT)
    fonts.set(qn("w:cs"), LATIN_FONT)
    fonts.set(qn("w:eastAsia"), east_asia)


def configure_document_styles(document) -> None:
    normal = document.styles["Normal"]
    _set_style_fonts(normal, east_asia=BODY_FONT, size=12, bold=None)
    normal.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
    normal.paragraph_format.line_spacing = 1.25
    normal.paragraph_format.space_before = Pt(0)
    normal.paragraph_format.space_after = Pt(0)

    for style_name, size, font, line_spacing, before, after in (
        ("Heading 1", 15, HEADING_FONT, 2.0, 1, 1),
        ("Heading 2", 14, HEADING_FONT, 1.25, 6, 6),
        ("Heading 3", 12, BODY_FONT, 1.25, 12, 0),
    ):
        style = document.styles[style_name]
        _set_style_fonts(style, east_asia=font, size=size, bold=True)
        style.font.color.rgb = RGBColor(0, 0, 0)
        style.paragraph_format.keep_with_next = True
        style.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.LEFT
        style.paragraph_format.line_spacing = line_spacing
        style.paragraph_format.space_before = Pt(before)
        style.paragraph_format.space_after = Pt(after)

    for style_name in ("List Bullet", "List Bullet 2", "List Bullet 3"):
        style = document.styles[style_name]
        _set_style_fonts(style, east_asia=BODY_FONT, size=12, bold=None)
        style.paragraph_format.line_spacing = 1.25
        style.paragraph_format.space_after = Pt(0)
        style.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.LEFT


def set_page_setup(document) -> None:
    """A4 纵向，上下 3cm／左右 3cm 与 2.5cm —— 中文公文与报告的常用版心。"""
    section = document.sections[0]
    section.page_width = Cm(21)
    section.page_height = Cm(29.7)
    section.left_margin = Cm(3.0)
    section.right_margin = Cm(2.5)
    section.top_margin = Cm(3.0)
    section.bottom_margin = Cm(2.5)


def create_table(
    document,
    *,
    columns: int,
    rows: int,
    weights: list[int] | None = None,
    bordered: bool = True,
):
    """建一张固定版心宽度的表。

    列宽按 ``weights`` 分配，缺省等宽。Word 的自动列宽会按内容重新洗牌，同一份报告
    两次打开列宽都可能不同，所以这里一律写死。
    """
    table = document.add_table(rows=rows, cols=columns)
    if bordered:
        table.style = "Table Grid"
    # 无边框表沿用模板默认的 Normal Table，再显式清掉边框：指标卡、图例、条形图
    # 都是靠底色分块的，画上网格线反而碎成一格一格。
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = False

    table_pr = table._tbl.tblPr
    table_width = table_pr.first_child_found_in("w:tblW")
    table_width.set(qn("w:w"), str(TABLE_WIDTH_DXA))
    table_width.set(qn("w:type"), "dxa")
    table_indent = OxmlElement("w:tblInd")
    table_indent.set(qn("w:w"), "0")
    table_indent.set(qn("w:type"), "dxa")
    table_pr.append(table_indent)

    if not bordered:
        _strip_borders(table)

    weights = list(weights or [1] * columns)
    weight_sum = sum(weights) or columns
    widths = [max(360, round(TABLE_WIDTH_DXA * weight / weight_sum)) for weight in weights]
    widths[-1] += TABLE_WIDTH_DXA - sum(widths)

    grid = table._tbl.tblGrid
    for child in list(grid):
        grid.remove(child)
    for width in widths:
        column = OxmlElement("w:gridCol")
        column.set(qn("w:w"), str(width))
        grid.append(column)

    for row in table.rows:
        for index, cell in enumerate(row.cells):
            cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
            set_cell_margins(cell)
            tc_width = cell._tc.get_or_add_tcPr().first_child_found_in("w:tcW")
            tc_width.set(qn("w:w"), str(widths[index]))
            tc_width.set(qn("w:type"), "dxa")
            paragraph = cell.paragraphs[0]
            paragraph.paragraph_format.line_spacing = 1.15
            paragraph.paragraph_format.space_after = Pt(0)
    return table


def _strip_borders(table) -> None:
    """无边框表格：指标卡、图例、条形图都靠底色分块，画上网格线反而碎。"""
    borders = OxmlElement("w:tblBorders")
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        node = OxmlElement(f"w:{edge}")
        node.set(qn("w:val"), "none")
        node.set(qn("w:sz"), "0")
        borders.append(node)
    table._tbl.tblPr.append(borders)


def column_weights(columns: list[str], rows: list[list[str]]) -> list[int]:
    """按各列最长内容估列宽，并压住上下限：太窄会一字一行，太宽会让别的列挤成竖排。"""
    weights: list[int] = []
    for index in range(len(columns)):
        longest = len(columns[index] or "")
        for row in rows:
            if index < len(row):
                longest = max(longest, len(row[index] or ""))
        weights.append(max(4, min(longest, 28)))
    return weights
