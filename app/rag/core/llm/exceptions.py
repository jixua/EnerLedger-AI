"""
自定义异常体系与可安全返回给用户的错误摘要。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b", flags=re.IGNORECASE),
    re.compile(r"(?i)(?:bearer|api[_ -]?key)\s*[:=]?\s*[A-Za-z0-9._-]{8,}"),
)
_PROVIDER_LABELS = {
    "deepseek": "DeepSeek",
    "openai": "OpenAI 兼容",
    "qwen": "千问",
    "dashscope": "阿里云模型",
    "anthropic": "Anthropic",
    "google": "Google",
    "glm": "智谱",
}


class LLMException(Exception):
    """LLM 模块异常基类"""

    def __init__(self, message: str = "", **kwargs):
        self.message = message
        self.provider_type = kwargs.get("provider_type")
        self.provider_name = kwargs.get("provider_name")
        super().__init__(self.message)

    def __str__(self):
        parts = [self.message]
        if self.provider_type:
            parts.append(f"(provider={self.provider_type})")
        return " ".join(parts)


class ProviderException(LLMException):
    """Provider 相关异常"""

    pass


class AuthenticationError(ProviderException):
    """认证失败（API Key 无效等）"""

    pass


class RateLimitError(ProviderException):
    """限流异常"""

    pass


class InsufficientBalanceError(ProviderException):
    """模型服务账户余额或调用额度不足。"""

    pass


class ProviderConnectionError(ProviderException):
    """Provider 连接异常"""

    pass


class InvalidResponseError(ProviderException):
    """无效响应异常"""

    pass


@dataclass(frozen=True)
class PublicLLMError:
    """不包含密钥、请求体或堆栈的用户态模型错误。"""

    code: str
    message: str
    retryable: bool


def sanitize_provider_error_message(message: str, *, limit: int = 240) -> str:
    """压缩上游错误文本并遮蔽常见凭据，便于安全透出。"""

    safe = " ".join(str(message or "").split())
    for pattern in _SECRET_PATTERNS:
        safe = pattern.sub("[REDACTED]", safe)
    return safe[:limit]


def public_llm_error(error: BaseException) -> PublicLLMError:
    """将 Provider 异常转为稳定错误码和可操作的中文提示。"""

    provider_type = str(getattr(error, "provider_type", "") or "").lower()
    provider = _PROVIDER_LABELS.get(provider_type, provider_type or "大模型")
    service = f"{provider} 模型服务"
    if isinstance(error, InsufficientBalanceError):
        return PublicLLMError(
            code="LLM_INSUFFICIENT_BALANCE",
            message=f"{service}账户余额不足或调用额度已用完，请充值或更换可用模型后重试。",
            retryable=False,
        )
    if isinstance(error, AuthenticationError):
        return PublicLLMError(
            code="LLM_AUTHENTICATION_FAILED",
            message=f"{service}认证失败，请检查 API Key 是否正确、已失效或无权访问当前模型。",
            retryable=False,
        )
    if isinstance(error, RateLimitError):
        return PublicLLMError(
            code="LLM_RATE_LIMITED",
            message=f"{service}请求频率或并发数已达上限，请稍后重试。",
            retryable=True,
        )
    if isinstance(error, ProviderConnectionError):
        return PublicLLMError(
            code="LLM_PROVIDER_UNAVAILABLE",
            message=f"暂时无法连接{service}，可能是超时、服务繁忙或网络异常，请稍后重试。",
            retryable=True,
        )
    if isinstance(error, InvalidResponseError):
        detail = sanitize_provider_error_message(str(error))
        suffix = f"：{detail}" if detail else ""
        return PublicLLMError(
            code="LLM_REQUEST_REJECTED",
            message=f"{service}拒绝了本次请求{suffix}",
            retryable=False,
        )
    if isinstance(error, ProviderException):
        detail = sanitize_provider_error_message(str(error))
        suffix = f"：{detail}" if detail else ""
        return PublicLLMError(
            code="LLM_PROVIDER_ERROR",
            message=f"{service}调用失败{suffix}",
            retryable=False,
        )
    return PublicLLMError(
        code="LLM_UNKNOWN_ERROR",
        message="大模型调用发生未识别异常，请联系管理员查看服务日志。",
        retryable=False,
    )


class ConfigurationException(LLMException):
    """配置相关异常"""

    pass


class ConfigNotFoundError(ConfigurationException):
    """配置未找到"""

    pass


class LLMConfigResolutionError(ConfigurationException):
    """精确 config_id 解析失败基类。"""

    code = "LLM_CONFIG_ERROR"
    http_status = 400

    def __init__(self, config_id: int, message: str) -> None:
        self.config_id = config_id
        super().__init__(message)


class LLMConfigNotFoundError(LLMConfigResolutionError):
    code = "LLM_CONFIG_NOT_FOUND"
    http_status = 404

    def __init__(self, config_id: int) -> None:
        super().__init__(config_id, f"LLM config {config_id} does not exist")


class LLMConfigInactiveError(LLMConfigResolutionError):
    code = "LLM_CONFIG_INACTIVE"
    http_status = 409

    def __init__(self, config_id: int) -> None:
        super().__init__(config_id, f"LLM config {config_id} is inactive")


class LLMConfigForbiddenError(LLMConfigResolutionError):
    code = "LLM_CONFIG_FORBIDDEN"
    http_status = 403

    def __init__(self, config_id: int) -> None:
        super().__init__(config_id, f"LLM config {config_id} is not accessible by this user")


class LLMConfigCapabilityMismatchError(LLMConfigResolutionError):
    code = "LLM_CONFIG_CAPABILITY_MISMATCH"
    http_status = 400

    def __init__(self, config_id: int, expected: str, actual: str) -> None:
        self.expected_capability = expected
        self.actual_capability = actual
        super().__init__(
            config_id,
            f"LLM config {config_id} capability mismatch: expected {expected}, got {actual}",
        )


class DatasetModelBindingRequiredError(ConfigurationException):
    """Dataset 在当前用途下缺少必要的精确配置绑定。"""

    code = "DATASET_MODEL_BINDING_REQUIRED"
    http_status = 409

    def __init__(self, dataset_id: int, missing_bindings: list[str]) -> None:
        self.dataset_id = dataset_id
        self.missing_bindings = missing_bindings
        super().__init__(
            f"Dataset {dataset_id} is missing required model bindings: "
            + ",".join(missing_bindings)
        )


class UnsupportedProtocolCapabilityError(ConfigurationException):
    """(protocol, capability) 组合本期未实现。

    执行端按 ``protocol`` 选 adapter、按 ``capability`` 选能力分支；请求的 capability
    不在该 protocol 的能力集合内时抛出，不静默降级、不回退猜测。对应 acceptance 的
    ``UNSUPPORTED_PROTOCOL_CAPABILITY``。
    """

    def __init__(
        self,
        protocol: str,
        capability: str,
        *,
        model_name: str | None = None,
        config_id: int | str | None = None,
        supported_combinations: list[str] | None = None,
    ) -> None:
        self.protocol = protocol
        self.capability = capability
        self.model_name = model_name
        self.config_id = config_id
        self.supported_combinations = supported_combinations or []

        details = [
            f"protocol='{protocol}'",
            f"capability='{capability}'",
        ]
        if model_name is not None:
            details.append(f"model_name='{model_name}'")
        if config_id is not None:
            details.append(f"config_id='{config_id}'")
        if self.supported_combinations:
            details.append("supported=" + ",".join(self.supported_combinations))
        super().__init__("Unsupported LLM protocol/capability combination: " + "; ".join(details))


class InvalidConfigError(ConfigurationException):
    """无效配置"""

    pass


class CircuitBreakerOpenError(LLMException):
    """熔断器开启异常"""

    pass


class AllProvidersFailedError(LLMException):
    """所有 Provider 都失败"""

    pass


class TokenLimitExceededError(LLMException):
    """Token 超出限制"""

    pass
