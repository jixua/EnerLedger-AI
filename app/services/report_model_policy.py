"""Network policy for user-configurable report model endpoints."""

from __future__ import annotations

import ipaddress
from urllib.parse import urlparse


class ReportModelEndpointError(ValueError):
    pass


def validate_report_model_endpoint(
    value: str,
    *,
    allowed_hosts: frozenset[str] = frozenset(),
    allow_private: bool = False,
) -> None:
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme not in {"http", "https"} or not host:
        raise ReportModelEndpointError("模型端点必须是有效 HTTP(S) URL")
    if allowed_hosts and host not in allowed_hosts:
        raise ReportModelEndpointError("模型端点域名不在报告服务允许列表中")
    if not allow_private and parsed.scheme != "https":
        raise ReportModelEndpointError("报告模型端点必须使用 HTTPS")
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        if not allow_private:
            raise ReportModelEndpointError("报告模型端点不能指向本机或内部域名")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return
    if not allow_private and (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    ):
        raise ReportModelEndpointError("报告模型端点不能指向私网或保留地址")
