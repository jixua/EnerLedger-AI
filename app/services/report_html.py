"""报告产物：把 ReportIR 渲染成自包含的单文件 HTML。

Markdown 与 DOCX 两个渲染器都从同一份 ReportIR 出发（``report_artifacts`` /
``report_docx``），三份产物里的表格、图表与台账因此是同一批数据、同一套说法。

产物是**单文件**：样式内联，不引用任何外部资源、不加载脚本，双击即可打开，
也可以直接打印成 PDF。正文全部经 ``html.escape`` 转义，并附带一条禁止任何外部
加载的 CSP，避免模型生成的内容在读者本地被当作标记执行。

渲染是纯函数，不触发模型请求；落盘失败不影响报告本身的生成结果。
"""

from __future__ import annotations

import json
import re
from html import escape as _escape
from typing import Any

from app.domain.models import ReportRun
from app.services.report_blocks import (
    FIELD_STATUS_LABELS,
    UNRESOLVED_STATUSES,
    block_data,
    block_unit,
    evidence_location,
    flatten_text,
    list_items,
    metric_items,
    series_items,
    table_columns,
    table_rows,
    to_number,
    trim_text,
)
from app.services.report_templates import ReportTemplate

# 占比条最多画几段。超过槽位数时色相要重复，读者无法把颜色对回分项，改出数据表。
_SHARE_SLOTS = 5
_CJK = re.compile("[一-鿿]")


def _esc(value: Any) -> str:
    return _escape(flatten_text(value), quote=True)


def _display(value: Any) -> str:
    """表格/卡片里的取值：空值统一成一个破折号，避免空格子被读成漏项。"""
    text = flatten_text(value)
    return _escape(text) if text else "—"


def _format_value(value: Any, unit: str) -> str:
    text = flatten_text(value)
    if not text:
        return "—"
    return _escape(f"{text} {unit}".strip())


def _evidence_chips(ids: Any) -> str:
    if not isinstance(ids, list) or not ids:
        return ""
    chips = "".join(f"<code>{_esc(item)}</code>" for item in ids)
    return f'<span class="evidence" title="引用证据">{chips}</span>'


def _table(columns: list[str], rows: list[list[Any]]) -> str:
    if not rows:
        return ""
    head = "".join(f"<th>{_esc(name)}</th>" for name in columns)
    head_html = f"<thead><tr>{head}</tr></thead>" if columns else ""
    body = "".join(
        "<tr>" + "".join(f"<td>{_display(cell)}</td>" for cell in row) + "</tr>" for row in rows
    )
    return (
        f'<div class="table-wrap"><table class="report-table">{head_html}'
        f"<tbody>{body}</tbody></table></div>"
    )


def _figure_table(columns: list[str], rows: list[list[Any]]) -> str:
    """图表旁始终附一份等价数据表：给屏幕阅读器、给打印件、也给要复制数字的人。"""
    if not rows:
        return ""
    body = _table(columns, rows)
    return f'<details class="figure-data" open><summary>数据表</summary>{body}</details>'


def _figure(inner: str, block: dict[str, Any]) -> str:
    """把图表/表格连同它的标题与证据标注一起包成 figure。

    块的 ``text`` 在这里是标题（caption）。不排出来，模型写的那行标题就凭空消失。
    """
    caption = flatten_text(block.get("text"))
    lead = f'<p class="figure__caption">{_esc(caption)}</p>' if caption else ""
    chips = _evidence_chips(block.get("evidence_ids"))
    return f'<figure class="figure">{lead}{inner}{chips}</figure>'


def _render_list(block: dict[str, Any]) -> str:
    items = list_items(block)
    if not items:
        return ""
    body = "".join(f"<li>{_esc(item)}</li>" for item in items)
    return f'<ul class="block block--list">{body}{_evidence_chips(block.get("evidence_ids"))}</ul>'


def _render_metrics(block: dict[str, Any]) -> str:
    items = metric_items(block)
    if not items:
        return ""
    cards: list[str] = []
    for item in items:
        value = flatten_text(item.get("value"))
        # 中文取值说明它是一段话（产品名、功能单位），不是给眼球抓的数，字号退回正文级
        modifier = " is-text" if _CJK.search(value) else ""
        cards.append(
            f'<div class="metric"><span class="metric__label">{_esc(item.get("label"))}</span>'
            f'<strong class="metric__value{modifier}">{_escape(value) or "—"}</strong></div>'
        )
    return _figure(f'<div class="metrics">{"".join(cards)}</div>', block)


def _share_ratio(value: float | None, max_abs: float) -> float:
    if value is None or max_abs <= 0:
        return 0.0
    return max(abs(value), 0.0) / max_abs * 100


def _bar_row(item: dict[str, Any], value: float | None, max_abs: float, unit: str) -> str:
    width = _share_ratio(value, max_abs)
    return (
        f'<div class="bar"><span class="bar__label">{_esc(item.get("label"))}</span>'
        f'<span class="bar__track"><span class="bar__fill" style="width:{width:.2f}%">'
        f"</span></span>"
        f'<span class="bar__value">{_format_value(item.get("value"), unit)}</span></div>'
    )


def _diverge_row(item: dict[str, Any], value: float | None, max_abs: float, unit: str) -> str:
    """一条发散条：零基线居中，正值向右、负值向左，长度按绝对值比。"""
    width = _share_ratio(value, max_abs)
    positive = (value or 0) >= 0
    fill = (
        f'<span class="diverge__fill is-{"positive" if positive else "negative"}" '
        f'style="width:{width:.2f}%"></span>'
    )
    return (
        f'<div class="diverge"><span class="diverge__label">{_esc(item.get("label"))}</span>'
        f'<span class="diverge__axis">'
        f'<span class="diverge__side is-negative">{fill if not positive else ""}</span>'
        f'<span class="diverge__side is-positive">{fill if positive else ""}</span>'
        f"</span>"
        f'<span class="diverge__value">{_format_value(item.get("value"), unit)}</span></div>'
    )


def _render_bars(block: dict[str, Any]) -> str:
    """量值比较。全部同号时按长度比；出现负值（减排、抵扣）时改用围绕零基线的
    发散条——长度条会把负值画成零宽，读者只看到数字、看不到量级。"""
    series = series_items(block)
    if not series:
        return ""
    unit = block_unit(block)
    values = [to_number(item.get("value")) for item in series]
    pairs = list(zip(series, values, strict=True))
    has_negative = any(value is not None and value < 0 for value in values)
    max_abs = max([abs(value) for value in values if value is not None] or [0])
    number_column = f"数值（{unit}）" if unit else "数值"
    rows = [[item.get("label"), item.get("value")] for item in series]
    data_table = _figure_table(["分项", number_column], rows)

    if not has_negative:
        bars = "".join(_bar_row(item, value, max_abs, unit) for item, value in pairs)
        return _figure(f'<div class="bars">{bars}</div>{data_table}', block)

    legend = (
        '<ul class="legend legend--polarity">'
        '<li><i style="background:var(--positive)"></i><span>正值：排放 / 增加</span></li>'
        '<li><i style="background:var(--negative)"></i><span>负值：减排 / 抵扣</span></li>'
        "</ul>"
    )
    diverging = "".join(_diverge_row(item, value, max_abs, unit) for item, value in pairs)
    return _figure(
        f'{legend}<div class="diverging">{diverging}</div>{data_table}',
        block,
    )


def _render_share(block: dict[str, Any]) -> str:
    """构成占比：横向堆叠条而非环形图——类别多、名称长时环形读不出数。含负值
    （「占总量多少」不成立）或超出配色槽位时退回数据表，不给误导性的占比。"""
    series = series_items(block)
    if not series:
        return ""
    unit = block_unit(block)
    values = [to_number(item.get("value")) for item in series]
    pairs = list(zip(series, values, strict=True))
    total = sum(max(value or 0, 0) for value in values)
    number_column = f"数值（{unit}）" if unit else "数值"
    rows = [[item.get("label"), item.get("value")] for item in series]
    plain_table = _figure_table(["分项", number_column], rows)

    if any(value is not None and value < 0 for value in values):
        note = (
            '<p class="figure__note">含负值分项（减排 / 抵扣），'
            "无法作为占比呈现，改为数据表。</p>"
        )
        return _figure(f"{note}{plain_table}", block)
    if len(series) > _SHARE_SLOTS or total <= 0:
        return _figure(plain_table, block)

    segments = "".join(
        f'<span class="share__segment" style="width:{(value or 0) / total * 100:.2f}%;'
        f'background:var(--series-{index % _SHARE_SLOTS + 1})"></span>'
        for index, value in enumerate(values)
    )
    legend = "".join(
        f'<li><i style="background:var(--series-{index % _SHARE_SLOTS + 1})"></i>'
        f'<span class="legend__label">{_esc(item.get("label"))}</span>'
        f'<span class="legend__value">{_format_value(item.get("value"), unit)}</span>'
        f'<span class="legend__share">{(value or 0) / total * 100:.1f}%</span></li>'
        for index, (item, value) in enumerate(pairs)
    )
    share_rows = [
        [item.get("label"), item.get("value"), f"{(value or 0) / total * 100:.1f}%"]
        for item, value in pairs
    ]
    return _figure(
        f'<div class="share">{segments}</div><ul class="legend">{legend}</ul>'
        f'{_figure_table(["分项", number_column, "占比"], share_rows)}',
        block,
    )


def _render_table(block: dict[str, Any]) -> str:
    data = block_data(block)
    rows = table_rows(data)
    if not rows:
        text = flatten_text(block.get("text"))
        return f'<p class="block">{_esc(text)}</p>' if text else ""
    return _figure(_table(table_columns(data), rows), block)


def _json_text(payload: Any) -> str:
    try:
        return json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    except (TypeError, ValueError):
        return flatten_text(payload)


def _render_block(block: dict[str, Any]) -> str:
    if not isinstance(block, dict):
        return ""
    rendered = _render_typed_block(block)
    return rendered or _render_unrenderable(block)


def _render_unrenderable(block: dict[str, Any]) -> str:
    """认识的块类型也可能读不出内容（分项为空、data 形状与预期不符）。

    这里兜底按原样输出，而不是返回空：静默丢块会让报告少内容，而少掉的东西没人
    会发现——宁可排版难看，也不能让在线视图有、导出件没有。
    """
    payload = block.get("data") if block.get("data") is not None else block.get("text")
    if payload is None or payload == "" or payload == {} or payload == []:
        return ""
    return f'<pre class="block block--raw">{_escape(_json_text(payload), quote=False)}</pre>'


def _render_typed_block(block: dict[str, Any]) -> str:
    block_type = flatten_text(block.get("type"))
    text = flatten_text(block.get("text"))
    evidence = _evidence_chips(block.get("evidence_ids"))

    if block_type in ("paragraph", "heading", "signature_block", "source_note"):
        if not text:
            return ""
        tag = "h3" if block_type == "heading" else "p"
        modifier = "" if block_type == "paragraph" else f" block--{block_type}"
        return f'<{tag} class="block{modifier}">{_esc(text)}{evidence}</{tag}>'
    if block_type == "callout":
        if not text:
            return ""
        return (
            f'<aside class="callout"><strong>提示</strong><span>{_esc(text)}</span>'
            f"{evidence}</aside>"
        )
    if block_type == "list":
        return _render_list(block)
    if block_type == "metric_cards":
        return _render_metrics(block)
    if block_type == "bar_chart":
        return _render_bars(block)
    if block_type == "donut_chart":
        return _render_share(block)
    if block_type == "table":
        return _render_table(block)
    if block_type == "page_break":
        return '<div class="page-break" aria-hidden="true"></div>'
    return ""


def _render_sections(report_ir: dict[str, Any]) -> str:
    sections: list[str] = []
    for index, section in enumerate(report_ir.get("sections") or [], start=1):
        if not isinstance(section, dict):
            continue
        blocks = "".join(_render_block(block) for block in (section.get("blocks") or []))
        if not blocks.strip():
            continue
        title = flatten_text(section.get("title")) or flatten_text(section.get("section_id"))
        sections.append(
            f'<section class="section" id="section-{index}"><h2>{_esc(title)}</h2>'
            f"{blocks}</section>"
        )
    return "".join(sections)


def _render_warnings(report_ir: dict[str, Any]) -> str:
    warnings = [flatten_text(item) for item in report_ir.get("warnings") or []]
    warnings = [item for item in warnings if item]
    if not warnings:
        return ""
    body = "".join(f"<li>{_esc(item)}</li>" for item in warnings)
    return f'<section class="warnings"><h2>阅读提示</h2><ul>{body}</ul></section>'


def _status_label(item: dict[str, Any]) -> str:
    status = flatten_text(item.get("status"))
    return _esc(FIELD_STATUS_LABELS.get(status, status))


def _render_ledger(report_ir: dict[str, Any], template: ReportTemplate) -> str:
    ledger = [item for item in report_ir.get("field_ledger") or [] if isinstance(item, dict)]
    if not ledger:
        return ""
    labels = {
        field.get("id"): field.get("label") or field.get("id")
        for field in template.definition.get("fields") or []
    }

    def name_of(item: dict[str, Any]) -> str:
        field_id = flatten_text(item.get("field_id"))
        return flatten_text(labels.get(field_id)) or field_id

    parts = ['<section class="panel"><h2>字段台账</h2>']
    pending = [item for item in ledger if item.get("status") in UNRESOLVED_STATUSES]
    if pending:
        # 缺口项就是这个台账按状态过滤出来的同一份数据，不另起一块
        parts.append(
            f'<p class="panel__lead">以下 {len(pending)} 项在现有材料中未能落实，'
            "正文相应位置按资料缺口处理。</p>"
        )
        gaps = "".join(_gap_item(item, name_of) for item in pending)
        parts.append(f'<ul class="gap-list">{gaps}</ul>')

    rows = [
        [
            name_of(item),
            flatten_text(item.get("status")),
            item.get("value"),
            item.get("unit"),
            "、".join(flatten_text(eid) for eid in item.get("evidence_ids") or []),
        ]
        for item in ledger
    ]
    parts.append(_table(["字段", "状态", "取值", "单位", "证据"], rows))
    parts.append("</section>")
    return "".join(parts)


def _gap_item(item: dict[str, Any], name_of: Any) -> str:
    notes = "；".join(flatten_text(note) for note in item.get("notes") or [])
    return (
        f"<li><strong>{_esc(name_of(item))}</strong>"
        f'<span class="gap-tag is-{_esc(item.get("status")).lower()}">{_status_label(item)}</span>'
        f"<small>{_esc(notes)}</small></li>"
    )


def _render_evidence(report_ir: dict[str, Any]) -> str:
    evidence = [item for item in report_ir.get("evidence") or [] if isinstance(item, dict)]
    if not evidence:
        return ""
    rows = [
        [
            item.get("evidence_id"),
            item.get("source_type"),
            evidence_location(item),
            trim_text(item.get("excerpt"), 200),
        ]
        for item in evidence
    ]
    return (
        '<section class="panel"><h2>证据台账</h2>'
        + _table(["证据", "来源", "位置", "摘录"], rows)
        + "</section>"
    )


def _render_calculations(report_ir: dict[str, Any]) -> str:
    calculations = [
        item for item in report_ir.get("calculations") or [] if isinstance(item, dict)
    ]
    if not calculations:
        return ""
    items = "".join(
        f'<li><code>{_esc(item.get("formula_id"))}</code> v{_esc(item.get("formula_version"))}：'
        f'{_esc(item.get("output_field_id"))} = {_esc(item.get("value"))} {_esc(item.get("unit"))}'
        f'<small>输入：{_esc(_json_text(item.get("inputs")))}</small></li>'
        for item in calculations
    )
    return f'<section class="panel"><h2>计算过程</h2><ul class="calc-list">{items}</ul></section>'


def _render_limitations(report_ir: dict[str, Any]) -> str:
    limitations = [flatten_text(item) for item in report_ir.get("limitations") or []]
    limitations = [item for item in limitations if item]
    if not limitations:
        return ""
    body = "".join(f"<li>{_esc(item)}</li>" for item in limitations)
    return (
        '<section class="limitations" aria-label="使用限制"><h3>使用限制</h3>'
        f"<ul>{body}</ul></section>"
    )


def render_report_html(
    *, run: ReportRun, template: ReportTemplate, report_ir: dict[str, Any]
) -> str:
    """把 ReportIR 渲染成自包含的单文件 HTML。"""
    if not isinstance(report_ir, dict):
        report_ir = {}
    name = flatten_text(template.definition.get("name")) or flatten_text(run.template_id)
    language = flatten_text(run.language) or "zh-CN"
    render_profile = flatten_text(report_ir.get("render_profile"))
    meta = (
        f"文档版本 v{_esc(run.document_version)}　模板 {_esc(run.template_id)} "
        f"v{_esc(run.template_version)}　任务 {_esc(run.id)}"
    )

    document = "".join(
        [
            '<header class="doc__header">',
            '<p class="doc__eyebrow">报告</p>',
            f"<h1>{_esc(name)}</h1>",
            f'<p class="doc__meta">{meta}</p>',
            "</header>",
            # 阅读提示放在正文之前、使用限制放在正文之后，与报告详情页的阅读顺序一致
            _render_warnings(report_ir),
            f'<article class="doc__body">{_render_sections(report_ir)}</article>',
            _render_ledger(report_ir, template),
            _render_evidence(report_ir),
            _render_calculations(report_ir),
            _render_limitations(report_ir),
            '<footer class="doc__footer">本文件由 EnerLedger 生成　'
            f"报告任务 {_esc(run.id)}</footer>",
        ]
    )

    return (
        "<!DOCTYPE html>\n"
        f'<html lang="{_escape(language, quote=True)}">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        # 产物是给人在本地打开的文件，内容来自模型：断掉一切外部加载与脚本执行，
        # 即使某处转义出错也执行不起来。
        '<meta http-equiv="Content-Security-Policy" '
        "content=\"default-src 'none'; style-src 'unsafe-inline'\">\n"
        '<meta name="generator" content="EnerLedger">\n'
        f"<title>{_esc(name)}</title>\n"
        f"<style>\n{_STYLESHEET}\n</style>\n</head>\n"
        f'<body data-render-profile="{_escape(render_profile, quote=True)}">\n'
        f'<main class="doc">\n{document}\n</main>\n</body>\n</html>\n'
    )


# 与前端 pages.css 的报告样式同一套配色与字号节奏。内联在这里而不是引用外部
# 样式表，产物才能是一个可独立打开、可打印、可归档的文件。
_STYLESHEET = """
:root {
  --ink: #123a31;
  --body: #40554f;
  --muted: #6c7a75;
  --muted-soft: #929b97;
  --hairline: #ddd7ca;
  --hairline-strong: #cfc6b6;
  --canvas: #f3efe6;
  --surface: #fbf9f4;
  --surface-soft: #eeeadf;
  --primary: #0d5a46;
  --warning: #a16a3e;
  --series-1: #2b8a63;
  --series-2: #4874b0;
  --series-3: #cf6a35;
  --series-4: #6d5bb5;
  --series-5: #b08a20;
  --positive: #cf6a35;
  --negative: #4874b0;
  --font: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
    "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
}

*, *::before, *::after { box-sizing: border-box; }

body {
  margin: 0;
  padding: 40px 20px 72px;
  background: var(--canvas);
  color: var(--body);
  font-family: var(--font);
  font-size: 15px;
  line-height: 1.8;
  -webkit-font-smoothing: antialiased;
}

.doc {
  max-width: 840px;
  margin: 0 auto;
  padding: 46px clamp(22px, 5vw, 58px) 54px;
  background: var(--surface);
  border: 1px solid var(--hairline);
  border-radius: 14px;
}

.doc__eyebrow {
  margin: 0 0 8px;
  color: var(--muted);
  font-size: 12px;
  font-weight: 600;
  letter-spacing: .04em;
}

.doc__header {
  padding-bottom: 26px;
  border-bottom: 1px solid var(--hairline);
}

.doc__header h1 {
  margin: 0 0 10px;
  color: var(--ink);
  font-size: 29px;
  font-weight: 650;
  line-height: 1.32;
  letter-spacing: -.01em;
}

.doc__meta {
  margin: 0;
  color: var(--muted-soft);
  font-size: 12px;
  line-height: 1.7;
  overflow-wrap: anywhere;
}

.doc__footer {
  margin-top: 34px;
  padding-top: 16px;
  border-top: 1px solid var(--hairline);
  color: var(--muted-soft);
  font-size: 11.5px;
}

/* 阅读提示：正文之前的说明段。不加外框、不加强调色条——它是一段说明，不是告警框 */
.warnings {
  margin-top: 28px;
  display: grid;
  gap: 10px;
}

.warnings h2 {
  margin: 0;
  color: var(--ink);
  font-size: 17px;
}

.warnings ul {
  margin: 0;
  padding-left: 20px;
  font-size: 14px;
}

.doc__body { margin-top: 34px; }

.section + .section { margin-top: 40px; }

.section > h2 {
  margin: 0 0 16px;
  padding-bottom: 9px;
  border-bottom: 1px solid var(--hairline);
  color: var(--ink);
  font-size: 22px;
  font-weight: 650;
}

.block { margin: 0 0 14px; color: var(--body); }

.block--heading { margin: 26px 0 12px; color: var(--ink); font-size: 17px; }

.block--signature, .block--source-note { color: var(--muted); font-size: 13px; }

.block--list { margin: 0 0 16px; padding-left: 22px; }

.block--list li { margin-bottom: 4px; }

.block--raw {
  overflow-x: auto;
  padding: 12px 14px;
  border: 1px solid var(--hairline);
  border-radius: 9px;
  background: var(--surface-soft);
  font-size: 13px;
  line-height: 1.6;
}

.callout {
  display: grid;
  gap: 6px;
  margin: 0 0 16px;
  padding: 1px 0 1px 16px;
  border-left: 2px solid var(--warning);
}

.callout strong { color: var(--warning); font-size: 12px; font-weight: 650; }

.callout span { font-size: 15px; }

.evidence {
  display: inline-flex;
  flex-wrap: wrap;
  gap: 7px;
  margin-left: 8px;
  vertical-align: baseline;
}

.evidence code { padding: 0; background: none; color: var(--muted-soft); font-size: 11px; }

.metrics {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
  gap: 20px;
}

.metric {
  display: grid;
  gap: 6px;
  padding-top: 10px;
  border-top: 2px solid var(--hairline-strong);
}

.metric__label { color: var(--muted); font-size: 13px; }

.metric__value {
  color: var(--ink);
  font-size: 25px;
  font-weight: 650;
  letter-spacing: -.02em;
  line-height: 1.3;
}

.metric__value.is-text { font-size: 17px; }

.figure { margin: 0 0 22px; }

.figure__note { margin: 0 0 10px; color: var(--muted); font-size: 13px; }
/* 图表与表格的标题：模型给这一块起的名字，也是 Word 产物里表格上方那一行 */
.figure__caption { margin: 0 0 8px; color: var(--ink); font-size: 13px; font-weight: 600; }

.figure-data { margin-top: 12px; }

.figure-data > summary {
  width: fit-content;
  cursor: pointer;
  color: var(--primary);
  font-size: 12.5px;
  font-weight: 650;
}

.bars { display: grid; gap: 8px; }

.bar {
  display: grid;
  grid-template-columns: minmax(80px, 168px) minmax(0, 1fr) auto;
  align-items: center;
  gap: 12px;
}

.bar__label { font-size: 13px; overflow-wrap: anywhere; }

.bar__track { height: 14px; border-radius: 4px; background: var(--surface-soft); }

.bar__fill {
  display: block;
  height: 100%;
  min-width: 3px;
  border-radius: 4px;
  background: var(--series-1);
}

.bar__value {
  color: var(--ink);
  font-size: 13.5px;
  font-variant-numeric: tabular-nums;
  font-weight: 600;
}

.diverging { display: grid; gap: 8px; }

.diverge {
  display: grid;
  grid-template-columns: minmax(80px, 168px) minmax(0, 1fr) auto;
  align-items: center;
  gap: 12px;
}

.diverge__label { font-size: 13px; overflow-wrap: anywhere; }

.diverge__axis { display: grid; grid-template-columns: 1fr 1fr; height: 14px; }

.diverge__side { display: flex; height: 100%; }

.diverge__side.is-negative {
  justify-content: flex-end;
  border-right: 1px solid var(--hairline-strong);
}

.diverge__side.is-positive { justify-content: flex-start; }

.diverge__fill { display: block; height: 100%; min-width: 2px; }

.diverge__fill.is-positive { border-radius: 0 4px 4px 0; background: var(--positive); }

.diverge__fill.is-negative { border-radius: 4px 0 0 4px; background: var(--negative); }

.diverge__value {
  color: var(--ink);
  font-size: 13.5px;
  font-variant-numeric: tabular-nums;
  font-weight: 600;
}

.share { display: flex; gap: 2px; height: 22px; margin-bottom: 12px; }

.share__segment { height: 100%; }

.share__segment:first-child { border-radius: 4px 0 0 4px; }

.share__segment:last-child { border-radius: 0 4px 4px 0; }

.legend { display: grid; gap: 6px; margin: 0; padding: 0; list-style: none; }

.legend li {
  display: grid;
  grid-template-columns: 10px minmax(0, 1fr) auto auto;
  align-items: center;
  gap: 10px;
}

.legend i { width: 10px; height: 10px; border-radius: 3px; }

.legend--polarity { display: flex; gap: 16px; margin-bottom: 10px; }

.legend--polarity li { display: flex; gap: 8px; }

.legend__label { font-size: 13px; overflow-wrap: anywhere; }

.legend__value {
  color: var(--ink);
  font-size: 13.5px;
  font-variant-numeric: tabular-nums;
  font-weight: 600;
}

.legend__share {
  min-width: 48px;
  color: var(--muted);
  font-size: 12.5px;
  text-align: right;
  font-variant-numeric: tabular-nums;
}

.table-wrap { overflow-x: auto; }

.report-table { width: 100%; border-collapse: collapse; font-size: 13.5px; }

.report-table th {
  padding: 10px 13px;
  border-bottom: 1px solid var(--hairline-strong);
  color: var(--ink);
  font-size: 13px;
  font-weight: 650;
  text-align: left;
  vertical-align: top;
}

.report-table td {
  padding: 11px 13px;
  border-bottom: 1px solid var(--hairline);
  vertical-align: top;
  line-height: 1.65;
}

.report-table th:first-child, .report-table td:first-child { min-width: 5.5rem; }

.panel {
  margin-top: 34px;
  padding: 22px 24px;
  border: 1px solid var(--hairline);
  border-radius: 12px;
  background: var(--surface);
}

.panel > h2 { margin: 0 0 12px; color: var(--ink); font-size: 17px; }

.panel__lead { margin: 0 0 12px; color: var(--muted); font-size: 13px; }

.gap-list { display: grid; gap: 8px; margin: 0 0 18px; padding: 0; list-style: none; }

.gap-list li { display: flex; flex-wrap: wrap; align-items: baseline; gap: 8px; }

.gap-list strong { color: var(--ink); font-size: 13px; }

.gap-list small { width: 100%; color: var(--muted-soft); font-size: 12px; line-height: 1.6; }

.gap-tag {
  padding: 2px 8px;
  border-radius: 999px;
  background: var(--surface-soft);
  color: var(--muted);
  font-size: 11px;
  font-weight: 600;
}

.gap-tag.is-missing, .gap-tag.is-unverified {
  background: rgba(183, 126, 32, .14);
  color: #b77e20;
}

.gap-tag.is-conflict { background: rgba(165, 84, 84, .10); color: #a55454; }

.calc-list { display: grid; gap: 10px; margin: 0; padding-left: 20px; font-size: 13.5px; }

.calc-list small {
  display: block;
  color: var(--muted-soft);
  font-size: 12px;
  overflow-wrap: anywhere;
}

/* 免责性质的限制：正文之后的一行小字 */
.limitations { margin-top: 26px; color: var(--muted); }

.limitations h3 { margin: 0 0 8px; color: var(--muted); font-size: 13px; }

.limitations ul { margin: 0; padding-left: 20px; font-size: 12.5px; line-height: 1.75; }

@media (max-width: 720px) {
  body { padding: 18px 12px 40px; font-size: 14px; }
  .doc { padding: 26px 18px 32px; border-radius: 10px; }
  .doc__header h1 { font-size: 23px; }
  .bar, .diverge { grid-template-columns: minmax(64px, 96px) minmax(0, 1fr) auto; gap: 8px; }
}

/* 打印/另存 PDF：去掉屏幕底色与圆角，并让图表不被分页切开 */
@media print {
  body { padding: 0; background: #fff; font-size: 11pt; }
  .doc { max-width: none; padding: 0; border: 0; border-radius: 0; background: #fff; }
  .figure, .metric, .bar, .diverge, .report-table tr { break-inside: avoid; }
  .section > h2 { break-after: avoid; }
  .page-break { break-after: page; }
  .figure-data > summary { display: none; }
}
"""
