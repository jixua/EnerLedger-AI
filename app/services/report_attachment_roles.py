"""多份附件生成报告时，判定哪一份是报告主体材料、哪一份是版式模板。

用户上传时不再声明用途，这一步改为由本轮对话模型判断。报告任务创建时必须冻结
``custom_template_document_id``，所以判断只能发生在创建之前——放在这里，与报告类型
分类同一层。

模型不可用、超时或产出不可解析时回落到确定性规则（报告特征更明显的文件视为模板），
保证生成流程不因一次判断失败而中断。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import Document
from app.rag.core.llm.provider_lifecycle import aclose_dataset_execution_contexts
from app.rag.core.llm.user_model_resolver import aresolve_model
from app.rag.core.prompts import (
    ATTACHMENT_ROLE_MAX_OUTPUT_TOKENS,
    ATTACHMENT_ROLE_SYSTEM_PROMPT,
    ATTACHMENT_ROLE_TIMEOUT_SECONDS,
    build_attachment_role_user_prompt,
    parse_attachment_role_reply,
)
from app.rag.models.chunk_record import ChunkRecordDB
from app.rag.observability.logging import logger
from app.services.report_template_classifier import classify_document

_EXCERPT_CHUNKS = 12


@dataclass(frozen=True)
class AttachmentRoles:
    """一份「主体材料 + 可选模板」的判定结果。

    ``decided_by`` 用于诊断与测试：``caller`` 调用方显式声明、``single`` 只有一份文件、
    ``model`` 模型判定、``fallback`` 回落规则判定。
    """

    subject_id: int
    template_id: int | None = None
    decided_by: str = "fallback"


class AttachmentRoleError(Exception):
    """调用方声明的用途不成立（映射为 422）。"""

    def __init__(self, message: str) -> None:
        super().__init__(message)


def resolve_declared_roles(
    *, attachment_ids: list[int], declared: dict[int, str]
) -> tuple[int, int | None]:
    """按用户显式声明的用途补全主体材料与模板。

    用户可能只指明其中一份（通常是"这份是模板"），其余按规则补全：模板之外的
    第一份作为主体材料。声明互相冲突或补不出主体材料时抛 AttachmentRoleError。
    """

    template_ids = [did for did, role in declared.items() if role == "TEMPLATE"]
    source_ids = [did for did, role in declared.items() if role == "SOURCE"]
    if len(template_ids) > 1:
        raise AttachmentRoleError("只能指定一份报告模板")
    if len(source_ids) > 1:
        raise AttachmentRoleError("只能指定一份来源文档")
    template_id = template_ids[0] if template_ids else None
    if source_ids:
        return source_ids[0], template_id
    remaining = [did for did in attachment_ids if did != template_id]
    if not remaining:
        raise AttachmentRoleError("还需要一份来源文档：唯一的上传文件不能只指定为模板")
    return remaining[0], template_id


async def _document_excerpt(db: AsyncSession, document: Document) -> str:
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
            .limit(_EXCERPT_CHUNKS)
        )
    ).all()
    return "\n".join(str(value) for value in rows)


async def _report_likeness(db: AsyncSession, document: Document) -> float:
    """文件"像一份报告"的程度：取报告类型分类的最高得分。

    得分越高说明越贴合某一类报告的用词与结构，越可能是提供版式的既有报告，
    而不是待写入报告的原始资料。
    """

    try:
        classification = await classify_document(db, document=document)
    except Exception as exc:  # noqa: BLE001 - 回落规则本身不应再抛
        logger.bind(
            event="attachment_role_fallback_failed",
            document_id=int(document.id),
            error_type=type(exc).__name__,
        ).warning("[agent] attachment role fallback classification failed")
        return 0.0
    scores = [float(candidate.get("score") or 0.0) for candidate in classification.candidates]
    return max(scores) if scores else 0.0


async def _fallback_by_report_likeness(
    *, db: AsyncSession, documents: list[Document]
) -> AttachmentRoles:
    """模型判断不可用时：报告特征更明显的文件视为模板，另一份作为主体材料。

    没有命中任何报告特征、或最高分并列（无法区分）时按上传顺序取：先上传的作为
    主体材料，后上传的作为模板。
    """

    scored = [(document, await _report_likeness(db, document)) for document in documents]
    top_score = max(score for _, score in scored)
    top_documents = [document for document, score in scored if score == top_score]
    if top_score <= 0 or len(top_documents) != 1:
        return AttachmentRoles(
            subject_id=int(documents[0].id),
            template_id=int(documents[1].id) if len(documents) > 1 else None,
            decided_by="fallback",
        )
    template = top_documents[0]
    subject = next(document for document in documents if document is not template)
    return AttachmentRoles(
        subject_id=int(subject.id), template_id=int(template.id), decided_by="fallback"
    )


async def decide_attachment_roles(
    *,
    db: AsyncSession,
    user_id: int,
    config_id: int,
    documents: list[Document],
) -> AttachmentRoles:
    """判定主体材料与模板。只有一份文件时不调用模型。"""

    ordered = sorted(documents, key=lambda item: int(item.id))
    if not ordered:
        raise ValueError("decide_attachment_roles 需要至少一份文件")
    if len(ordered) == 1:
        return AttachmentRoles(subject_id=int(ordered[0].id), decided_by="single")

    attachments: list[dict] = []
    for document in ordered:
        try:
            excerpt = await _document_excerpt(db, document)
        except Exception as exc:  # noqa: BLE001 - 取不到正文只是判断信息变少
            logger.bind(
                event="attachment_role_excerpt_failed",
                document_id=int(document.id),
                error_type=type(exc).__name__,
            ).warning("[agent] attachment role excerpt failed")
            excerpt = ""
        attachments.append(
            {
                "document_id": int(document.id),
                "filename": document.filename,
                "excerpt": excerpt,
            }
        )

    resolved = None
    try:
        resolved = await aresolve_model(
            user_id=user_id, config_id=config_id, capability="CHAT", db=db
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - 用途判定是增强项，取不到模型不阻塞生成
        logger.bind(
            event="attachment_role_model_resolution_failed",
            outcome="degraded",
            error_type=type(exc).__name__,
            error_message=str(exc)[:200],
        ).warning("[agent] attachment role model resolution failed")
    if resolved is not None:
        try:
            result = await asyncio.wait_for(
                resolved.provider.generate(
                    prompt=build_attachment_role_user_prompt(attachments=attachments),
                    system_prompt=ATTACHMENT_ROLE_SYSTEM_PROMPT,
                    temperature=0.0,
                    max_tokens=ATTACHMENT_ROLE_MAX_OUTPUT_TOKENS,
                ),
                timeout=ATTACHMENT_ROLE_TIMEOUT_SECONDS,
            )
            subject_id, template_id = parse_attachment_role_reply(
                result.content, allowed_ids={item["document_id"] for item in attachments}
            )
            if subject_id is not None:
                return AttachmentRoles(
                    subject_id=subject_id, template_id=template_id, decided_by="model"
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 判定失败回落规则，不阻塞生成
            logger.bind(
                event="attachment_role_decision_failed",
                outcome="degraded",
                error_type=type(exc).__name__,
                error_message=str(exc)[:200],
            ).warning("[agent] attachment role decision failed")
        finally:
            if resolved is not None:
                await aclose_dataset_execution_contexts([], extra_models=[resolved])

    return await _fallback_by_report_likeness(db=db, documents=ordered)
