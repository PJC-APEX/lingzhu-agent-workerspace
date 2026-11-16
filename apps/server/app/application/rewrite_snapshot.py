"""改写会话的读取与响应快照组装。

从原 `routes.py` 的 `_working_content` / `_comparison` / `_session_payload`
三个私有函数迁移而来，供文档、改写等模块共用。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.domain.exceptions import NotFoundError
from app.domain.models import DocumentRevision, RewriteSession, WorkingRevision
from app.application.rewrite_access import require_owned
from app.utils.diff import compare_text

__all__ = ["comparison_segments", "session_payload", "working_content"]


def working_content(db: Session, session: RewriteSession, user_id: str) -> WorkingRevision:
    if not session.current_revision_id:
        raise NotFoundError("找不到中间文案版本")
    working = require_owned(db, WorkingRevision, session.current_revision_id, user_id, "中间文案版本")
    return working


def comparison_segments(db: Session, session: RewriteSession, working: WorkingRevision, user_id: str) -> list[dict]:
    baseline_revision_id = session.comparison_revision_id or session.source_revision_id
    original = require_owned(db, DocumentRevision, baseline_revision_id, user_id, "对比原文版本")
    return compare_text(original.content, working.content)


def session_payload(db: Session, session: RewriteSession, user_id: str) -> dict:
    working = working_content(db, session, user_id)
    source_revision = require_owned(db, DocumentRevision, session.source_revision_id, user_id, "改写来源版本")
    comparison_revision_id = session.comparison_revision_id or session.source_revision_id
    comparison_revision = require_owned(db, DocumentRevision, comparison_revision_id, user_id, "对比原文版本")
    return {
        "id": session.id,
        "documentId": session.document_id,
        "sourceRevisionId": source_revision.id,
        "comparisonDocumentId": session.comparison_document_id or session.document_id,
        "comparisonRevisionId": comparison_revision.id,
        "workingRevisionId": working.id,
        "originalText": comparison_revision.content,
        "content": working.content,
        "diffSegments": comparison_segments(db, session, working, user_id),
    }


