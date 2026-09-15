"""Explainable routing from parsed source text to one of the seven report types."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import Document
from app.rag.models.chunk_record import ChunkRecordDB
from app.services.report_templates import ReportTemplate, report_template_registry

MIN_SCORE = 8.0
MIN_MARGIN = 2.5

ROUTING_TERMS = {
    "R1": ["产品碳足迹", "生命周期", "功能单位", "系统边界", "原材料", "单位产品"],
    "R2": [
        "温室气体清单", "组织边界", "scope 1", "scope1", "scope 2",
        "scope2", "scope 3", "年度排放", "盘查",
    ],
    "R3": ["esg", "可持续发展", "治理", "气候风险", "利益相关方", "指标与目标"],
    "R4": ["能源审计", "节能诊断", "能源账单", "综合能耗", "能耗强度", "回收期", "建筑面积"],
    "R5": ["cbam", "欧盟碳边境", "cn 编码", "嵌入排放", "进口商", "生产设施"],
    "R6": ["sbti", "科学碳目标", "净零", "近期目标", "基准年", "减排路径"],
    "R7": ["核查报告", "验证报告", "核查机构", "保证等级", "实质性", "核查意见", "整改"],
}


@dataclass(frozen=True, slots=True)
class Classification:
    state: str
    selected_report_type: str | None
    candidates: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "selected_report_type": self.selected_report_type,
            "candidates": list(self.candidates),
            "policy": {"min_score": MIN_SCORE, "min_margin": MIN_MARGIN},
        }


def _normalized(value: str) -> str:
    return re.sub(r"\s+", " ", value.lower()).strip()


def classify_text(text: str, *, filename: str = "") -> Classification:
    haystack = _normalized(f"{filename}\n{text}")
    templates: list[ReportTemplate] = report_template_registry.list()
    ranked: list[dict[str, Any]] = []
    for template in templates:
        matches: list[str] = []
        score = 0.0
        for term in ROUTING_TERMS[template.report_type]:
            if _normalized(term) in haystack:
                matches.append(term)
                score += 3.0 if len(term) >= 4 else 2.0
        for phrase in template.definition.get("applicable_documents") or []:
            if _normalized(phrase) in haystack:
                matches.append(phrase)
                score += 5.0
        definition_terms = [
            *(field.get("label") for field in template.definition.get("fields") or []),
            *(section.get("title") for section in template.definition.get("sections") or []),
        ]
        for phrase in definition_terms:
            normalized_phrase = _normalized(str(phrase)) if phrase else ""
            if len(normalized_phrase) >= 4 and normalized_phrase in haystack:
                matches.append(str(phrase))
                score += 1.5
        negative = [
            phrase for phrase in template.definition.get("inapplicable_documents") or []
            if _normalized(phrase) in haystack
        ]
        score -= 6.0 * len(negative)
        ranked.append({
            "report_type": template.report_type,
            "template_id": template.template_id,
            "name": template.name,
            "score": max(score, 0.0),
            "matched_terms": list(dict.fromkeys(matches))[:8],
            "negative_terms": negative[:3],
        })
    ranked.sort(key=lambda item: (-item["score"], item["report_type"]))
    top = ranked[0]
    margin = top["score"] - ranked[1]["score"]
    confident = top["score"] >= MIN_SCORE and margin >= MIN_MARGIN
    return Classification(
        state="CONFIDENT" if confident else "AMBIGUOUS",
        selected_report_type=top["report_type"] if confident else None,
        candidates=tuple(ranked[:3]),
    )


async def classify_document(db: AsyncSession, *, document: Document) -> Classification:
    rows = (
        await db.scalars(
            select(ChunkRecordDB.content)
            .where(
                ChunkRecordDB.doc_id == document.id,
                ChunkRecordDB.document_version == document.version,
                ChunkRecordDB.user_id == document.user_id,
                ChunkRecordDB.set_id == document.dataset_id,
            )
            .order_by(ChunkRecordDB.chunk_index)
            .limit(40)
        )
    ).all()
    text = "\n".join(str(value) for value in rows)[:40000]
    return classify_text(text, filename=document.filename)
