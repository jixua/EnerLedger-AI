"""带附件的对话回合：判断用户这一轮是要生成报告，还是就着文件提问。

此前「上传来源文件」本身就等于「发起报告生成」：只要附件里判定出主体材料，服务端
无条件走报告分类，于是「这两份材料分别是什么」这种问题也会被当成生成请求去问
用户选报告类型。这里用本轮对话模型判一次意图，回落方向保持现状（按生成报告处理），
以免模型不可用时反而把客户的主要用法挡掉。

判定只决定路由，不产生副作用——报告类型分类、交互状态机与报告创建仍由 FastAPI
权威实现，模型不参与决定是否创建任务。
"""

from __future__ import annotations

import json
import re

TURN_INTENT_SYSTEM_PROMPT = (
    "你是「能碳会计 AI 智能体」的意图判定器。用户上传了文件并提了一个问题，"
    "你要判断这一轮他想做什么：生成一份报告，还是就着上传的文件提问。"
    "生成报告指用户要产出一份成品文档——例如要求生成、撰写、出一份报告，"
    "或要求按上传的版式填写、套用模板产出报告。"
    "就着文件提问指用户想从文件里得到信息——例如询问内容、含义、某个数值、"
    "要求概括、对比、提取、解释，或追问细节。"
    "判断依据是用户这句话本身，不要因为上传了文件就默认他要生成报告。"
    "只输出 JSON，不要任何解释或代码块标记，格式为："
    '{"intent": "REPORT" 或 "QUESTION"}。'
)

TURN_INTENT_MAX_OUTPUT_TOKENS = 1024

TURN_INTENT_TIMEOUT_SECONDS = 15

# 问题本身通常很短；截断只是防止把整篇正文塞进判定请求。
TURN_INTENT_QUERY_CHARS = 1000

_VALID_INTENTS = ("REPORT", "QUESTION")


def build_turn_intent_user_prompt(*, query: str, filenames: list[str]) -> str:
    lines = ["本轮上传的文件："]
    for name in filenames:
        lines.append(f"- {name}")
    lines.extend(
        [
            "",
            "用户这一轮说的话：",
            " ".join(str(query or "").split())[:TURN_INTENT_QUERY_CHARS] or "（空）",
            "",
            "请输出 JSON。",
        ]
    )
    return "\n".join(lines)


def parse_turn_intent_reply(text: str) -> str | None:
    """从模型回复里取意图。

    解析不出、取值不在枚举内时返回 None，交调用方回落。
    """

    cleaned = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(?P<body>.*?)```", cleaned, flags=re.DOTALL | re.IGNORECASE)
    if fence:
        cleaned = fence.group("body").strip()
    try:
        payload = json.loads(cleaned)
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    intent = str(payload.get("intent") or "").strip().upper()
    return intent if intent in _VALID_INTENTS else None
