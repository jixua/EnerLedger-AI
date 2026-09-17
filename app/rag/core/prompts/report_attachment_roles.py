"""多份附件生成报告时，由模型判断各文件用途的 Prompt。

用户上传文件时不再声明用途（模板曾是必选项，用起来多一步）。报告任务创建时需要知道
哪一份是「报告主体材料」、哪一份是「版式模板」，因此在这里用本轮对话模型判断一次；
模型不可用/超时/产出不可解析一律回落调用方给定的确定性规则，不阻塞生成。
"""

from __future__ import annotations

import json
import re

ATTACHMENT_ROLE_SYSTEM_PROMPT = (
    "你是「能碳会计 AI 智能体」的报告助手。用户一次上传了多份文件要求生成报告，"
    "但没有说明各文件的用途。请判断哪一份是「报告主体材料」，哪一份是「版式模板」。"
    "报告主体材料是待写入报告的原始资料，含具体数据、清单、活动数据或待评对象的事实；"
    "版式模板是一份成型的报告或声明样例，提供章节结构、标题体系与表达形式。"
    "只输出 JSON，不要任何解释或代码块标记，格式为："
    '{"subject_document_id": <主体材料的文件编号>, "template_document_id": <模板的文件编号或 null>}。'
    "无法确定模板时 template_document_id 用 null；只能使用给出的文件编号，不要编造。"
)

# 与报告类型澄清同理：本轮模型可能是推理模型，需留足输出预算，否则正文被截空。
ATTACHMENT_ROLE_MAX_OUTPUT_TOKENS = 2048

ATTACHMENT_ROLE_TIMEOUT_SECONDS = 20

# 每份文件送进判断的正文上限：只要够看出"这是成型报告"还是"这是原始资料"。
ATTACHMENT_ROLE_EXCERPT_CHARS = 1200


def build_attachment_role_user_prompt(*, attachments: list[dict]) -> str:
    lines = ["本次上传的文件：", ""]
    for item in attachments:
        lines.append(f"文件编号 {int(item['document_id'])}：{item.get('filename') or '未命名'}")
        excerpt = " ".join(str(item.get("excerpt") or "").split())[:ATTACHMENT_ROLE_EXCERPT_CHARS]
        lines.append(f"正文节选：{excerpt or '（无正文）'}")
        lines.append("")
    lines.append("请输出 JSON。")
    return "\n".join(lines)


def parse_attachment_role_reply(
    text: str, *, allowed_ids: set[int]
) -> tuple[int | None, int | None]:
    """从模型回复里取（主体材料编号，模板编号）。

    只认给出的编号；解析不出或编号越界都按"未判定"返回 (None, None)，交调用方回落。
    """

    cleaned = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(?P<body>.*?)```", cleaned, flags=re.DOTALL | re.IGNORECASE)
    if fence:
        cleaned = fence.group("body").strip()
    try:
        payload = json.loads(cleaned)
    except (ValueError, TypeError):
        return None, None
    if not isinstance(payload, dict):
        return None, None

    def _pick(key: str) -> int | None:
        raw = payload.get(key)
        if raw is None:
            return None
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return None
        return value if value in allowed_ids else None

    subject_id = _pick("subject_document_id")
    template_id = _pick("template_document_id")
    if subject_id is not None and template_id == subject_id:
        template_id = None
    # 没有主体材料就没有可写入的事实来源，视为未判定
    if subject_id is None:
        return None, None
    return subject_id, template_id
