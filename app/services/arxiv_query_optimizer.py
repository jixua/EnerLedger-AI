"""把自然语言主题转换为安全、可解释的 arXiv 检索表达式。"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlparse

from app.rag.core.llm.base_provider import BaseProvider

ARXIV_QUERY_OPTIMIZATION_TIMEOUT_SECONDS = 30.0
ARXIV_QUERY_OPTIMIZATION_MAX_TOKENS = 512
ARXIV_QUERY_OPTIMIZER_SYSTEM_PROMPT = """你是学术检索词优化器。
把用户输入的中文或英文研究主题转换成适合 arXiv 全字段检索的英文术语。

规则：
1. 只返回 JSON：{"terms":["term one","term two"]}。
2. 返回 2-5 个术语；每项可以是一个单词或常见学术短语。
3. 中文必须翻译成学术界常用英文，不要逐字硬译。
4. 保留研究对象、方法或指标，不要添加用户没有表达的研究方向。
5. 不要返回 all:、AND、OR、括号、解释、Markdown 或代码围栏。
"""


class ArxivQueryOptimizationError(ValueError):
    """模型没有返回可安全用于 arXiv 的英文术语。"""


@dataclass(frozen=True)
class ArxivQueryOptimization:
    original_query: str
    terms: tuple[str, ...]
    search_query: str
    mode: Literal["AI", "RULES"]
    model_name: str | None = None
    warning: str | None = None

    @property
    def optimized_query(self) -> str:
        return " AND ".join(self.terms)


def _compact_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _safe_term(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    term = _compact_text(value.replace('"', " ").replace("'", " "))
    term = re.sub(r"\b(?:AND|OR|ANDNOT)\b", " ", term, flags=re.IGNORECASE)
    term = _compact_text(re.sub(r"[():]", " ", term)).strip(".,;!?-/ ")
    if not term or len(term) > 80 or not re.search(r"[A-Za-z]", term):
        return None
    return term


def _deduplicate_terms(values: list[object]) -> tuple[str, ...]:
    terms: list[str] = []
    seen: set[str] = set()
    for value in values:
        term = _safe_term(value)
        if term is None or term.casefold() in seen:
            continue
        seen.add(term.casefold())
        terms.append(term)
    return tuple(terms[:5])


def _terms_from_model_output(content: str) -> tuple[str, ...]:
    normalized = content.strip()
    if normalized.startswith("```"):
        normalized = re.sub(r"^```(?:json)?\s*|\s*```$", "", normalized, flags=re.I)
    try:
        payload = json.loads(normalized)
    except json.JSONDecodeError as exc:
        raise ArxivQueryOptimizationError("AI 未返回有效的检索词 JSON") from exc
    values = payload.get("terms") if isinstance(payload, dict) else None
    if not isinstance(values, list):
        raise ArxivQueryOptimizationError("AI 返回结果缺少 terms 数组")
    terms = _deduplicate_terms(values)
    if not terms:
        raise ArxivQueryOptimizationError("AI 没有返回有效的英文检索词")
    return terms


def build_arxiv_search_query(terms: tuple[str, ...]) -> str:
    """每个术语独立匹配，再用 AND 组合，避免把整段输入误作固定短语。"""

    if not terms:
        raise ArxivQueryOptimizationError("检索词不能为空")
    return " AND ".join(f'all:"{term}"' for term in terms)


def rule_based_arxiv_query(
    query: str,
    *,
    warning: str | None = None,
) -> ArxivQueryOptimization:
    original = _compact_text(query)
    english_tokens = re.findall(r"[A-Za-z0-9][A-Za-z0-9+#.\-]*", original)
    terms = _deduplicate_terms(list(english_tokens))
    if not terms:
        # 无模型时仍保留中文输入供 arXiv 尝试匹配，并通过 warning 告知限制。
        terms = (original.replace('"', " ").strip(),)
    return ArxivQueryOptimization(
        original_query=original,
        terms=terms,
        search_query=build_arxiv_search_query(terms),
        mode="RULES",
        warning=warning,
    )


def rule_based_fallback_warning(query: str, *, reason: str) -> str:
    """准确描述规则回退；纯中文输入不能声称已经完成英文分词。"""

    if re.search(r"[A-Za-z]", query):
        fallback = "英文分词规则检索"
    else:
        fallback = "原始主题检索（中文匹配结果可能有限）"
    return f"{reason}，已回退为{fallback}"


def _generation_options(provider: BaseProvider) -> dict[str, object]:
    """仅对官方 DeepSeek 端点启用其明确支持的结构化输出能力。"""

    endpoint = str(getattr(provider, "api_base_url", "") or "").strip()
    hostname = (urlparse(endpoint).hostname or "").lower()
    if hostname != "api.deepseek.com":
        return {}

    options: dict[str, object] = {"response_format": {"type": "json_object"}}
    provider_model = str(getattr(provider, "model_name", "") or "").lower()
    if provider_model.startswith("deepseek-v4"):
        # 短检索词转换不需要思考模式，避免推理内容耗尽正文 token 预算。
        options["thinking"] = {"type": "disabled"}
    return options


async def optimize_arxiv_query_with_ai(
    query: str,
    *,
    provider: BaseProvider,
    model_name: str,
) -> ArxivQueryOptimization:
    original = _compact_text(query)
    result = await asyncio.wait_for(
        provider.generate(
            prompt=f"<研究主题>\n{original}\n</研究主题>",
            system_prompt=ARXIV_QUERY_OPTIMIZER_SYSTEM_PROMPT,
            temperature=0.0,
            max_tokens=ARXIV_QUERY_OPTIMIZATION_MAX_TOKENS,
            **_generation_options(provider),
        ),
        timeout=ARXIV_QUERY_OPTIMIZATION_TIMEOUT_SECONDS,
    )
    if str(getattr(result, "finish_reason", "") or "").lower() == "length":
        raise ArxivQueryOptimizationError("AI 输出达到 token 上限，未生成完整检索词")
    if not str(result.content or "").strip():
        raise ArxivQueryOptimizationError("AI 返回了空的检索词结果")
    terms = _terms_from_model_output(result.content)
    return ArxivQueryOptimization(
        original_query=original,
        terms=terms,
        search_query=build_arxiv_search_query(terms),
        mode="AI",
        model_name=model_name,
    )
