"""ReportIR 数据块的格式无关解读。

Markdown（``report_artifacts``）、HTML（``report_html``）与 DOCX（``report_docx``）
三个渲染器都要从同一份 IR 里读出「这一块有哪些行、哪些列、哪些分项」。这些判断集中
在这里，避免几边各写一套之后在边界情况上分叉——例如 ``table`` 的 ``rows`` 是对象
数组还是数组数组、列名缺省时从哪里推导、图表分项写成标量时怎么兜底。

这里只做取值，不做排版：具体输出成 Markdown 表格、``<table>`` 还是 Word 表格由各
渲染器决定。

**为什么这里要认好几种拼法**：ReportIR 的 ``data`` 交给模型自由发挥，schema 只约束
到「是个对象」。实测同一条链路上，表格列名先后出现过 ``columns`` 与 ``headers``，
指标卡出现过 ``items`` 与 ``cards``，图表出现过 ``series`` 与 ``categories/labels``
＋ ``values``。渲染器只认其中一种，另一种就整块消失或退化成文字——报告少了内容却
不报错。所以读取端把见过的拼法都收下来，统一归一成列名与分项；上报端（schema）
再要求「凡是能通过校验的，这里一定读得出来」。
"""

from __future__ import annotations

import math
import re
from typing import Any


def flatten_text(value: Any) -> str:
    """压成单行文本：折叠连续空白并去掉首尾空白。"""
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def to_number(value: Any) -> float | None:
    """能当数用的取值就转成数值：图表要按大小画条，字符串形态的数字（"392.04"）也算。

    布尔值不算数——``True`` 在 Python 里是 1，但在一张指标表里它只是一个词。
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        try:
            number = float(flatten_text(value))
        except (TypeError, ValueError):
            return None
    return number if math.isfinite(number) else None


def trim_text(value: Any, limit: int) -> str:
    return flatten_text(value)[:limit]


def block_data(block: dict[str, Any]) -> dict[str, Any]:
    data = block.get("data")
    return data if isinstance(data, dict) else {}


def block_unit(block: dict[str, Any]) -> str:
    return flatten_text(block_data(block).get("unit"))


# 字段台账的状态说法：前端 ReportIrView、HTML 产物、DOCX 产物共用一套，
# 同一份台账在页面和导出件里不应当出现两种译法。
FIELD_STATUS_LABELS = {
    "FOUND": "文档已载明",
    "CALCULATED": "由计算得出",
    "USER_SUPPLIED": "用户补充",
    "MISSING": "材料未提供",
    "CONFLICT": "多来源冲突",
    "UNVERIFIED": "未经验证",
    "NOT_APPLICABLE": "不适用",
}
UNRESOLVED_STATUSES = {"MISSING", "CONFLICT", "UNVERIFIED", "NOT_APPLICABLE"}

_COLUMN_KEYS = ("columns", "headers")
_ITEM_KEYS = ("items", "cards", "metrics")
_LABEL_KEYS = ("categories", "labels")


def _named_items(raw: Any) -> list[dict[str, Any]]:
    """``items`` / ``series`` 的统一形态：对象项原样保留，标量项补成 {label, value}。"""
    if not isinstance(raw, list):
        return []
    items: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, dict):
            items.append(item)
        elif item is not None:
            items.append({"label": item, "value": None})
    return items


def _first_named(data: dict[str, Any], keys: tuple[str, ...]) -> list[dict[str, Any]]:
    for key in keys:
        items = _named_items(data.get(key))
        if items:
            return items
    return []


def _parallel_items(data: dict[str, Any]) -> list[dict[str, Any]]:
    """平行数组形态：``values`` 配 ``categories``／``labels`` 取名，配不上就只留数值。"""
    values = data.get("values")
    if not isinstance(values, list):
        return []
    labels: list[Any] | None = None
    for key in _LABEL_KEYS:
        if isinstance(data.get(key), list):
            labels = data[key]
            break
    items: list[dict[str, Any]] = []
    for index, value in enumerate(values):
        label = labels[index] if labels is not None and index < len(labels) else None
        items.append({"label": label, "value": value})
    return items


def series_items(block: dict[str, Any]) -> list[dict[str, Any]]:
    """图表分项：``data.series``，或 ``values`` 与 ``categories``／``labels`` 平行给出。"""
    data = block_data(block)
    return _named_items(data.get("series")) or _parallel_items(data)


def metric_items(block: dict[str, Any]) -> list[dict[str, Any]]:
    """指标卡分项：``data.items``，别名 ``cards`` / ``metrics``。"""
    return _first_named(block_data(block), _ITEM_KEYS)


def list_items(block: dict[str, Any]) -> list[str]:
    """列表项取 ``data.items``（字符串或 {text|label}），没有则退回 ``block.text``。"""
    texts: list[str] = []
    for item in _first_named(block_data(block), _ITEM_KEYS):
        text = flatten_text(item.get("text") or item.get("label"))
        if text:
            texts.append(text)
    if texts:
        return texts
    text = flatten_text(block.get("text"))
    return [text] if text else []


def _explicit_columns(data: dict[str, Any]) -> list[str] | None:
    """显式列名：``columns``，别名 ``headers``；都没有则返回 None（交给行数据推导）。"""
    for key in _COLUMN_KEYS:
        explicit = data.get(key)
        if isinstance(explicit, list) and explicit:
            return [flatten_text(name) for name in explicit]
    return None


def table_columns(data: dict[str, Any]) -> list[str]:
    """列名：``columns``／``headers``，否则取首行对象的键。数组行且无列名时返回空。"""
    explicit = _explicit_columns(data)
    if explicit is not None:
        return explicit
    rows = data.get("rows")
    first = rows[0] if isinstance(rows, list) and rows else None
    if isinstance(first, dict):
        return [flatten_text(name) for name in first]
    return []


def table_rows(data: dict[str, Any]) -> list[list[Any]]:
    """行数据统一成二维数组：对象行按列名取值，数组行按列数截齐或原样保留。"""
    rows = data.get("rows")
    if not isinstance(rows, list):
        return []
    columns = _explicit_columns(data)
    normalized: list[list[Any]] = []
    for row in rows:
        if isinstance(row, (list, tuple)):
            cells = list(row)
            if columns is not None:
                cells = [cells[i] if i < len(cells) else None for i in range(len(columns))]
            normalized.append(cells)
        elif isinstance(row, dict):
            keys = columns if columns is not None else list(row.keys())
            normalized.append([row.get(key) for key in keys])
        else:
            normalized.append([row])
    return normalized


def evidence_location(item: dict[str, Any]) -> str:
    """证据落点：优先切片 ID，其次外部 URI，最后页码。"""
    for key in ("chunk_id", "reference_uri"):
        value = flatten_text(item.get(key))
        if value:
            return value
    page = item.get("page")
    return f"第 {page} 页" if page else ""
