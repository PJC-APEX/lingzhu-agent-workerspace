from __future__ import annotations

import logging

from app.agents.context import estimate_tokens
from app.core.config import AGENT_INPUT_TOKEN_BUDGET, AGENT_RECENT_MESSAGE_COUNT, AGENT_SUMMARY_TRIGGER_RATIO
from app.domain.models import AgentMessage, AgentThread
from app.infrastructure.database import SessionLocal
from app.prompts.agent_prompt.memory import build_summary_messages
from app.services.model_gateway import ModelUnavailable, complete_chat, model_configured
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


def maybe_schedule_summary(thread_id: str) -> None:
    with SessionLocal() as db:
        thread = db.get(AgentThread, thread_id)
        if not thread:
            return
        messages = db.query(AgentMessage).filter(
            AgentMessage.thread_id == thread_id,
            AgentMessage.message_type == "text",
        ).order_by(AgentMessage.created_at, AgentMessage.id).all()
        marker = next((index for index, item in enumerate(messages) if item.id == thread.summary_through_message_id), -1)
        pending = messages[marker + 1:]
        combined = (thread.summary_text or "") + "\n" + "\n".join(item.content for item in pending)
        if estimate_tokens(combined) < int(AGENT_INPUT_TOKEN_BUDGET * AGENT_SUMMARY_TRIGGER_RATIO):
            return
    summarize_agent_thread.delay(thread_id)


@celery_app.task(name="lingzhu.summarize_agent_thread")
def summarize_agent_thread(thread_id: str) -> None:
    """异步压缩较早的普通聊天，只摘要偏好、方向、决定和待办。"""
    if not model_configured():
        logger.info("event=agent.summary_skipped reason=model_unavailable thread_id=%s", thread_id)
        return
    with SessionLocal() as db:
        thread = db.get(AgentThread, thread_id)
        if not thread:
            return
        previous_summary = thread.summary_text or ""
        previous_marker = thread.summary_through_message_id
        messages = db.query(AgentMessage).filter(
            AgentMessage.thread_id == thread_id,
            AgentMessage.message_type == "text",
        ).order_by(AgentMessage.created_at, AgentMessage.id).all()
        marker_index = next((i for i, message in enumerate(messages) if message.id == previous_marker), -1)
        pending = messages[marker_index + 1:]
        if len(pending) <= AGENT_RECENT_MESSAGE_COUNT:
            return
        old_messages = pending[:-AGENT_RECENT_MESSAGE_COUNT]
        cutoff_id = old_messages[-1].id
        transcript = "\n".join(
            f"{item.role}: {('（较早发送过长文，正文不纳入摘要）' if (item.metadata_json or {}).get('longMessage') else item.content if len(item.content) <= 1200 else '（较长聊天内容已省略；摘要只保留用户偏好、修改方向和已确认决定）')}"
            for item in old_messages
        )

    try:
        summary = complete_chat(build_summary_messages(previous_summary, transcript)).text
    except ModelUnavailable:
        return
    except Exception:
        logger.exception("event=agent.summary_failed thread_id=%s", thread_id)
        return

    with SessionLocal() as db:
        thread = db.get(AgentThread, thread_id)
        if not thread or thread.summary_through_message_id != previous_marker:
            logger.info("event=agent.summary_stale thread_id=%s", thread_id)
            return
        thread.summary_text = summary
        thread.summary_through_message_id = cutoff_id
        db.commit()
    logger.info("event=agent.summary_updated thread_id=%s through_message_id=%s tokens=%s", thread_id, cutoff_id, estimate_tokens(summary))

