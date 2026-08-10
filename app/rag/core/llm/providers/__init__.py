"""
LLM Providers（按 protocol 组织的 adapter）
"""

from app.rag.core.llm.providers.anthropic import AnthropicProvider
from app.rag.core.llm.providers.dashscope import DashScopeProvider
from app.rag.core.llm.providers.google import GoogleProvider
from app.rag.core.llm.providers.jina import JinaProvider
from app.rag.core.llm.providers.openai import OpenAICompatibleProvider

__all__ = [
    "OpenAICompatibleProvider",
    "AnthropicProvider",
    "GoogleProvider",
    "JinaProvider",
    "DashScopeProvider",
]
