"""业务表统一使用的 UTC ``DATETIME`` 时间源。"""

from datetime import UTC, datetime


def utc_now() -> datetime:
    """返回适合 MySQL ``DATETIME`` 的无时区 UTC 时间。"""

    return datetime.now(UTC).replace(tzinfo=None)
