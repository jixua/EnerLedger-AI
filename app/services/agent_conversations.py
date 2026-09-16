"""Durable conversation state and five-turn short-term memory."""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import AgentConversation, AgentConversationTurn
from app.domain.time import utc_now

SHORT_TERM_TURN_LIMIT = 5


def _title(content: str) -> str:
    normalized = " ".join(content.split())
    return (normalized[:57] + "…") if len(normalized) > 58 else normalized or "新对话"


def normalize_conversation_id(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return str(UUID(value))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="conversation_id 格式无效") from exc


def build_short_term_history(
    turns: list[AgentConversationTurn],
) -> list[dict[str, str]]:
    """Expand oldest-to-newest complete turns into Pi's message representation."""

    history: list[dict[str, str]] = []
    for item in turns[-SHORT_TERM_TURN_LIMIT:]:
        history.append({"role": "user", "content": item.user_content})
        if item.assistant_content:
            history.append({"role": "assistant", "content": item.assistant_content})
    return history


async def start_turn(
    db: AsyncSession,
    *,
    user_id: int,
    conversation_id: str | None,
    user_content: str,
    dataset_ids: list[int],
    document_ids: list[int],
    llm_config_id: int | None,
    attachments: list[dict[str, Any]],
) -> tuple[AgentConversation, AgentConversationTurn, list[dict[str, str]]]:
    conversation_id = normalize_conversation_id(conversation_id)
    conversation = None
    if conversation_id:
        conversation = await db.scalar(
            select(AgentConversation)
            .where(
                AgentConversation.id == conversation_id,
                AgentConversation.user_id == user_id,
            )
            .with_for_update()
        )
        if conversation is None:
            raise HTTPException(status_code=404, detail="对话不存在")
    else:
        conversation = AgentConversation(
            id=str(uuid4()), user_id=user_id, title=_title(user_content)
        )
        db.add(conversation)
        await db.flush()

    previous = (
        await db.scalars(
            select(AgentConversationTurn)
            .where(
                AgentConversationTurn.conversation_id == conversation.id,
                AgentConversationTurn.user_id == user_id,
                AgentConversationTurn.status == "SUCCEEDED",
            )
            .order_by(AgentConversationTurn.turn_index.desc())
            .limit(SHORT_TERM_TURN_LIMIT)
        )
    ).all()
    history = build_short_term_history(list(reversed(previous)))

    turn_index = int(
        await db.scalar(
            select(func.coalesce(func.max(AgentConversationTurn.turn_index), 0)).where(
                AgentConversationTurn.conversation_id == conversation.id
            )
        )
        or 0
    ) + 1
    turn = AgentConversationTurn(
        id=str(uuid4()),
        conversation_id=conversation.id,
        user_id=user_id,
        turn_index=turn_index,
        user_content=user_content,
        assistant_content=None,
        status="STREAMING",
        dataset_ids=dataset_ids,
        document_ids=document_ids,
        llm_config_id=llm_config_id,
        attachments=attachments,
    )
    conversation.updated_at = utc_now()
    db.add(turn)
    await db.commit()
    await db.refresh(turn)
    return conversation, turn, history


async def finish_turn(
    db: AsyncSession,
    *,
    turn: AgentConversationTurn,
    content: str,
    status: str = "SUCCEEDED",
    interaction: dict[str, Any] | None = None,
    report_run_id: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> None:
    turn.assistant_content = content
    turn.status = status
    turn.interaction = interaction
    turn.report_run_id = report_run_id
    turn.error_code = error_code
    turn.error_message = error_message
    conversation = await db.scalar(
        select(AgentConversation).where(AgentConversation.id == turn.conversation_id)
    )
    if conversation is not None:
        conversation.updated_at = utc_now()
    await db.commit()


def turn_dict(turn: AgentConversationTurn) -> dict[str, Any]:
    return {
        "turn_id": turn.id,
        "conversation_id": turn.conversation_id,
        "turn_index": turn.turn_index,
        "user_content": turn.user_content,
        "assistant_content": turn.assistant_content or "",
        "status": turn.status,
        "dataset_ids": turn.dataset_ids or [],
        "document_ids": turn.document_ids or [],
        "llm_config_id": turn.llm_config_id,
        "attachments": turn.attachments or [],
        "interaction": turn.interaction,
        "report_run_id": turn.report_run_id,
        "error_code": turn.error_code,
        "error_message": turn.error_message,
        "created_at": turn.created_at,
        "updated_at": turn.updated_at,
    }
