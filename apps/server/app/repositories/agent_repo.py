"""智能体会话（AgentThread）的数据访问。

只做查询与写入，不做业务判断、不自行提交事务。
"""

from __future__ import annotations

from sqlalchemy import and_, exists, func, or_
from sqlalchemy.orm import Session

from app.domain.models import AgentAction, AgentMessage, AgentThread, AgentTurnRequest, Workspace

__all__ = [
    "add_thread",
    "get_owned_thread",
    "get_thread",
]


def get_thread(db: Session, thread_id: str) -> AgentThread | None:
    return db.get(AgentThread, thread_id)


def get_owned_thread(db: Session, thread_id: str, user_id: str) -> AgentThread | None:
    """按 id 取会话，并校验它属于该用户的某个工作区。

    会话本身没有 owner 字段，归属要通过 workspace.owner_user_id 判断。
    None 表示不存在或不属于该用户——调用方不应区分这两者，
    否则就泄露了「这个 id 确实存在」的信息。
    """
    return (
        db.query(AgentThread)
        .join(Workspace, Workspace.id == AgentThread.workspace_id)
        .filter(AgentThread.id == thread_id, Workspace.owner_user_id == user_id)
        .first()
    )


def add_thread(db: Session, thread: AgentThread) -> AgentThread:
    db.add(thread)
    return thread


def list_thread_rows(db, workspace_id, q, offset, limit):
    latest_message = db.query(AgentMessage.thread_id.label('thread_id'), AgentMessage.content.label('preview'), AgentMessage.created_at.label('message_created_at'), func.row_number().over(partition_by=AgentMessage.thread_id, order_by=(AgentMessage.created_at.desc(), AgentMessage.id.desc())).label('row_number')).subquery()
    message_counts = db.query(AgentMessage.thread_id.label('thread_id'), func.count(AgentMessage.id).label('message_count')).group_by(AgentMessage.thread_id).subquery()
    query = db.query(AgentThread, latest_message.c.preview, func.coalesce(message_counts.c.message_count, 0).label('message_count'), func.coalesce(latest_message.c.message_created_at, AgentThread.created_at).label('last_activity_at')).outerjoin(latest_message, and_(latest_message.c.thread_id == AgentThread.id, latest_message.c.row_number == 1)).outerjoin(message_counts, message_counts.c.thread_id == AgentThread.id).filter(AgentThread.workspace_id == workspace_id)
    search = q.strip()
    if search:
        pattern = f'%{search}%'
        has_matching_message = exists().where(AgentMessage.thread_id == AgentThread.id, AgentMessage.content.ilike(pattern))
        query = query.filter(or_(AgentThread.title.ilike(pattern), has_matching_message))
    rows = query.order_by(func.coalesce(latest_message.c.message_created_at, AgentThread.created_at).desc(), AgentThread.id.desc()).offset(offset).limit(limit + 1).all()
    return rows


def add_record(db, record):
    db.add(record)
    return record


def flush(db):
    db.flush()


def get_message(db, message_id):
    return db.get(AgentMessage, message_id)


def get_action(db, action_id):
    return db.get(AgentAction, action_id)


def list_messages(db, thread_id):
    return db.query(AgentMessage).filter(AgentMessage.thread_id == thread_id).order_by(AgentMessage.created_at).all()


def recent_text_messages(db, thread_id, limit=12):
    rows = db.query(AgentMessage).filter(
        AgentMessage.thread_id == thread_id, AgentMessage.message_type == "text",
    ).order_by(AgentMessage.created_at.desc()).limit(limit).all()
    rows.reverse()
    return rows


def find_turn(db, thread_id, request_id):
    return db.query(AgentTurnRequest).filter(
        AgentTurnRequest.thread_id == thread_id, AgentTurnRequest.request_id == request_id,
    ).first()


def latest_pending_action(db, thread_id, action_type):
    return db.query(AgentAction).filter(
        AgentAction.thread_id == thread_id, AgentAction.action_type == action_type,
        AgentAction.status == "awaiting_confirmation",
    ).order_by(AgentAction.created_at.desc()).first()


def claim_action(db, action_id, thread_id, target_status):
    return db.query(AgentAction).filter(
        AgentAction.id == action_id, AgentAction.thread_id == thread_id,
        AgentAction.status == "awaiting_confirmation",
    ).update({"status": target_status}, synchronize_session=False)


def text_history(db, thread_id, excluded_message_id=None):
    query = db.query(AgentMessage).filter(
        AgentMessage.thread_id == thread_id, AgentMessage.message_type == "text",
    )
    if excluded_message_id:
        query = query.filter(AgentMessage.id != excluded_message_id)
    return query.order_by(AgentMessage.created_at, AgentMessage.id).all()
