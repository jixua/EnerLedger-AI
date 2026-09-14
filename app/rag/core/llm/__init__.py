"""
多 LLM 接入模块
"""

from app.rag.core.llm.base_provider import BaseProvider
from app.rag.core.llm.exceptions import (
    AllProvidersFailedError,
    AuthenticationError,
    CircuitBreakerOpenError,
    ConfigNotFoundError,
    ConfigurationException,
    InsufficientBalanceError,
    InvalidConfigError,
    InvalidResponseError,
    LLMException,
    ProviderConnectionError,
    ProviderException,
    PublicLLMError,
    RateLimitError,
    TokenLimitExceededError,
    public_llm_error,
)
from app.rag.core.llm.interfaces import (
    CapabilityType,
    IEmbedder,
    IReranker,
    ITextGenerator,
    IVisionProcessor,
)
from app.rag.core.llm.response import (
    APIResponse,
    EmbeddingResult,
    GenerateResult,
    RerankItem,
    RerankResult,
    StreamChunk,
    ToolCallResult,
    UsageInfo,
    VisionResult,
)

__all__ = [
    "CapabilityType",
    "ITextGenerator",
    "IEmbedder",
    "IReranker",
    "IVisionProcessor",
    "LLMException",
    "ProviderException",
    "AuthenticationError",
    "InsufficientBalanceError",
    "RateLimitError",
    "ProviderConnectionError",
    "InvalidResponseError",
    "ConfigurationException",
    "ConfigNotFoundError",
    "InvalidConfigError",
    "CircuitBreakerOpenError",
    "AllProvidersFailedError",
    "TokenLimitExceededError",
    "PublicLLMError",
    "public_llm_error",
    "UsageInfo",
    "GenerateResult",
    "StreamChunk",
    "EmbeddingResult",
    "RerankItem",
    "RerankResult",
    "VisionResult",
    "ToolCallResult",
    "APIResponse",
    "BaseProvider",
]
