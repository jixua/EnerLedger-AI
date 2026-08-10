"""
BaseProvider 抽象基类
所有 LLM Provider 的基类，实现公共逻辑
"""

import inspect
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

from app.rag.core.llm.interfaces import (
    CapabilityType,
    GenerateResult,
    StreamChunk,
)


class BaseProvider(ABC):
    """LLM Provider 抽象基类

    提供通用能力：
    - 能力注册与查询
    - 重试机制（由子类调用）
    - 熔断器状态管理（由子类调用）
    """

    def __init__(
        self,
        provider_type: str,
        provider_name: str,
        api_key: str,
        api_base_url: str | None = None,
        timeout_ms: int = 60000,
        max_retries: int = 3,
        **kwargs,
    ):
        self.provider_type = provider_type
        self.provider_name = provider_name
        self.api_key = api_key
        self.api_base_url = api_base_url
        self.timeout_ms = timeout_ms
        self.max_retries = max_retries
        self._capabilities: set[CapabilityType] = set()
        self._extra_config = kwargs.get("extra_config", {})

    def has_capability(self, capability: CapabilityType) -> bool:
        """检查是否具备指定能力"""
        return capability in self._capabilities

    def get_capabilities(self) -> set[CapabilityType]:
        """获取所有能力"""
        return self._capabilities.copy()

    async def aclose(self) -> None:
        """确定性释放 adapter 懒创建的 HTTP 客户端。

        各迁入协议对客户端的包装层级不同：有的直接持有 ``_http_client``，
        有的经 ``_client`` 再持有 httpx client。本契约同时覆盖两种形态。
        """

        resources = [
            getattr(self, "_client", None),
            getattr(self, "_http_client", None),
        ]
        seen: set[int] = set()
        for resource in resources:
            if resource is None or id(resource) in seen:
                continue
            seen.add(id(resource))
            closer = getattr(resource, "aclose", None) or getattr(resource, "close", None)
            if closer is not None:
                result = closer()
                if inspect.isawaitable(result):
                    await result
                continue

            nested = getattr(resource, "_http_client", None)
            if nested is None or id(nested) in seen:
                continue
            seen.add(id(nested))
            nested_closer = getattr(nested, "aclose", None) or getattr(nested, "close", None)
            if nested_closer is not None:
                result = nested_closer()
                if inspect.isawaitable(result):
                    await result

    async def test_connection(self) -> bool:
        """测试连接是否可用"""
        try:
            result = await self.generate(prompt="test", max_tokens=1)
            return result is not None
        except Exception:
            return False

    @abstractmethod
    async def generate(
        self,
        prompt: str,
        system_prompt: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        **kwargs,
    ) -> GenerateResult:
        """生成文本（子类必须实现）"""
        pass

    @abstractmethod
    async def stream(
        self,
        prompt: str,
        system_prompt: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        **kwargs,
    ) -> AsyncIterator[StreamChunk]:
        """流式生成文本（子类必须实现）"""
        pass

    async def embed(self, texts, model=None, **kwargs):
        """向量化（子类可选实现）"""
        raise NotImplementedError(f"{self.provider_type} does not support embedding")

    async def embed_sparse(self, texts, model=None, **kwargs):
        """稀疏向量化（子类可选实现）

        返回 :class:`~app.rag.core.llm.response.SparseEmbeddingResult`，其中每条文本对应一组
        整数 token_id 与权重。声明 :class:`CapabilityType.SPARSE_EMBEDDING` 的子类必须实现，
        否则能力门禁放行后会在此抛错。
        """
        raise NotImplementedError(f"{self.provider_type} does not support sparse embedding")

    async def rerank(self, query, documents, model=None, top_n=None, **kwargs):
        """语义重排（子类可选实现）"""
        raise NotImplementedError(f"{self.provider_type} does not support rerank")

    async def analyze_image(self, image_base64, prompt, **kwargs):
        """视觉分析（子类可选实现；OCR 即视觉 + 文字提取 prompt，不再单列）"""
        raise NotImplementedError(f"{self.provider_type} does not support vision")
