"""Short-lived lease-bound tokens for Pi report tools."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass


class ReportAgentTokenError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ReportAgentTokenClaims:
    run_id: str
    lease_token: str
    expires_at: int


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def issue_report_agent_token(
    *, run_id: str, lease_token: str, secret: str, ttl_seconds: int
) -> str:
    if len(secret) < 32:
        raise ReportAgentTokenError("报告 Agent run token secret 未安全配置")
    payload = json.dumps(
        {
            "run_id": run_id,
            "lease_token": lease_token,
            "exp": int(time.time()) + ttl_seconds,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    body = _encode(payload)
    signature = hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
    return f"{body}.{_encode(signature)}"


def verify_report_agent_token(
    token: str, *, run_id: str, secret: str, now: int | None = None
) -> ReportAgentTokenClaims:
    if len(secret) < 32:
        raise ReportAgentTokenError("报告 Agent run token secret 未安全配置")
    try:
        body, signature = token.split(".", 1)
        expected = hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
        if not hmac.compare_digest(_decode(signature), expected):
            raise ReportAgentTokenError("报告 Agent run token 签名无效")
        payload = json.loads(_decode(body))
        claims = ReportAgentTokenClaims(
            run_id=str(payload["run_id"]),
            lease_token=str(payload["lease_token"]),
            expires_at=int(payload["exp"]),
        )
    except ReportAgentTokenError:
        raise
    except Exception as exc:
        raise ReportAgentTokenError("报告 Agent run token 无效") from exc
    checked_at = int(time.time()) if now is None else now
    if claims.run_id != run_id or claims.expires_at <= checked_at:
        raise ReportAgentTokenError("报告 Agent run token 已过期或任务不匹配")
    return claims
