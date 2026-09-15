"""Durable conversation history and report-template confirmations."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.reports import ReportCreateRequest, create_report
from app.domain.auth import get_actor_user_id, get_shared_owner_user_id
from app.domain.models import AgentConversation, AgentConversationTurn, Document
from app.rag.database import get_db
from app.services.agent_conversations import finish_turn, turn_dict
from app.services.document_queue import DOCUMENT_STATUS_READY
from app.services.report_template_classifier import classify_document

router = APIRouter(prefix="/api/v1/agent", tags=["Agent 对话"])


class TemplateSelectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    report_type: Literal["R1", "R2", "R3", "R4", "R5", "R6", "R7"]


@router.get("/conversations")
async def list_conversations(
    user_id: Annotated[int, Depends(get_actor_user_id)],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> list[dict]:
    rows = (
        await db.scalars(
            select(AgentConversation)
            .where(AgentConversation.user_id == user_id)
            .order_by(AgentConversation.updated_at.desc())
            .limit(limit)
        )
    ).all()
    result = []
    for conversation in rows:
        turn_count = int(
            await db.scalar(
                select(func.count(AgentConversationTurn.id)).where(
                    AgentConversationTurn.conversation_id == conversation.id,
                    AgentConversationTurn.user_id == user_id,
                )
            )
            or 0
        )
        result.append(
            {
                "conversation_id": conversation.id,
                "title": conversation.title,
                "turn_count": turn_count,
                "created_at": conversation.created_at,
                "updated_at": conversation.updated_at,
            }
        )
    return result


@router.get("/conversations/{conversation_id}/turns")
async def list_conversation_turns(
    conversation_id: str,
    user_id: Annotated[int, Depends(get_actor_user_id)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[dict]:
    owned = await db.scalar(
        select(AgentConversation.id).where(
            AgentConversation.id == conversation_id,
            AgentConversation.user_id == user_id,
        )
    )
    if owned is None:
        raise HTTPException(status_code=404, detail="对话不存在")
    turns = (
        await db.scalars(
            select(AgentConversationTurn)
            .where(
                AgentConversationTurn.conversation_id == conversation_id,
                AgentConversationTurn.user_id == user_id,
            )
            .order_by(AgentConversationTurn.turn_index)
        )
    ).all()
    return [turn_dict(turn) for turn in turns]


@router.post("/documents/{document_id}/classify-report-template")
async def classify_report_template(
    document_id: int,
    user_id: Annotated[int, Depends(get_shared_owner_user_id)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    document = await db.scalar(
        select(Document).where(Document.id == document_id, Document.user_id == user_id)
    )
    if document is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    if document.status != DOCUMENT_STATUS_READY:
        raise HTTPException(status_code=409, detail="文档尚未解析完成")
    return (await classify_document(db, document=document)).to_dict()


@router.post("/conversations/{conversation_id}/turns/{turn_id}/template-selection")
async def confirm_template_selection(
    conversation_id: str,
    turn_id: str,
    payload: TemplateSelectionRequest,
    user_id: Annotated[int, Depends(get_actor_user_id)],
    owner_user_id: Annotated[int, Depends(get_shared_owner_user_id)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    turn = await db.scalar(
        select(AgentConversationTurn)
        .where(
            AgentConversationTurn.id == turn_id,
            AgentConversationTurn.conversation_id == conversation_id,
            AgentConversationTurn.user_id == user_id,
        )
        .with_for_update()
    )
    interaction = turn.interaction if turn else None
    if turn is None:
        raise HTTPException(status_code=404, detail="待确认问题不存在")
    if turn.status != "NEEDS_INPUT" or interaction is None or interaction.get("status") != "OPEN":
        raise HTTPException(status_code=409, detail="该问题已经处理或不再有效")
    allowed = {option["value"] for option in interaction.get("options") or []}
    if payload.report_type not in allowed:
        raise HTTPException(status_code=422, detail="选择不属于当前候选模板")
    interaction = {**interaction, "status": "ANSWERED", "selected": payload.report_type}
    turn.interaction = interaction
    turn.status = "PROCESSING_ACTION"
    await db.commit()
    try:
        run = await create_report(
            document_id=int(interaction["source_document_id"]),
            payload=ReportCreateRequest(
                report_type=payload.report_type,
                llm_config_id=int(interaction["llm_config_id"]),
                custom_template_document_id=interaction.get("template_document_id"),
                user_instructions=interaction.get("user_instructions"),
            ),
            user_id=owner_user_id,
            db=db,
        )
    except Exception:
        turn.status = "NEEDS_INPUT"
        turn.interaction = {**interaction, "status": "OPEN"}
        await db.commit()
        raise
    answer = f"已按 {payload.report_type} 创建报告任务，任务编号 {run['run_id'][:8]}。"
    await finish_turn(
        db,
        turn=turn,
        content=answer,
        interaction=interaction,
        report_run_id=run["run_id"],
    )
    return {"turn": turn_dict(turn), "report_run": run}
