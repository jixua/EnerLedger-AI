"""报告类型不明确时的确认说明 Prompt 与兜底文案。

上传材料后分类器命中多个候选（得分差距不足）时，由本轮对话模型基于候选信息生成
一段简短的确认说明，前端在其后渲染选择卡片承载具体选项。模型不可用/超时/产出为空
一律回落固定文案——确认流程不依赖模型可用性。
"""

REPORT_CLARIFICATION_SYSTEM_PROMPT = (
    "你是「能碳会计 AI 智能体」的报告助手。系统已读取用户上传的材料并完成报告类型匹配，"
    "但结果不唯一。请写一段简短的中文确认说明（2~4 句）：先说明该材料同时具备多个报告类型的"
    "特征、暂时无法唯一确定；再请用户从下方候选中选择一种要生成的报告类型。"
    "只依据给出的候选信息，不要编造材料中不存在的内容；不要使用编号列表或 Markdown 标题；"
    "语气专业、克制。"
)

REPORT_CLARIFICATION_FALLBACK = "我还不能可靠判断报告类型，请从下面的候选中选择一项后继续。"

# 与标题生成同理：本轮对话模型可能是推理模型（先思考再出正文），需留足输出预算，
# 否则正文被截空而回落固定文案。
REPORT_CLARIFICATION_MAX_OUTPUT_TOKENS = 2048

REPORT_CLARIFICATION_TIMEOUT_SECONDS = 20

# 确认说明的字符上限：模型偶发超写时截断，保证对话气泡不被长文占满。
REPORT_CLARIFICATION_MAX_CHARS = 400


def build_report_clarification_user_prompt(*, filename: str, candidates: list[dict]) -> str:
    lines = [f"材料文件名：{filename}", "", "候选报告类型（括号内为匹配到的材料特征）："]
    for candidate in candidates:
        terms = "、".join(candidate.get("matched_terms") or []) or "未见明显命中词"
        lines.append(f"- {candidate.get('report_type')} {candidate.get('name')}（{terms}）")
    lines.append("")
    lines.append("请输出确认说明。")
    return "\n".join(lines)


def clean_report_clarification(text: str) -> str:
    cleaned = (text or "").strip()
    if not cleaned:
        return ""
    return cleaned[:REPORT_CLARIFICATION_MAX_CHARS]
