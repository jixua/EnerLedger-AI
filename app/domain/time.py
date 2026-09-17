"""业务表统一使用的 UTC ``DATETIME`` 时间源。"""

from datetime import UTC, datetime
from typing import Any


def utc_now() -> datetime:
    """返回适合 MySQL ``DATETIME`` 的无时区 UTC 时间。"""

    return datetime.now(UTC).replace(tzinfo=None)


def as_utc(value: Any) -> Any:
    """把库内的无时区 UTC 时间标记为带时区 UTC，供 API 出参使用。

    MySQL ``DATETIME`` 不保存时区，:func:`utc_now` 写入的是无时区 UTC。若原样
    序列化，客户端会按本地时间解析（UTC+8 下整表时间早 8 小时）。这里在出参
    边界补齐时区，内部计算仍沿用无时区 UTC 约定。

    非 ``datetime`` 值原样返回，便于直接用在按字段批量归一化的校验器里。
    """

    if not isinstance(value, datetime):
        return value
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
