"""改写会话与版本的数据访问。

只做查询与写入，不做业务判断、不自行提交事务。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.domain.models import Document, DocumentRevision, RewriteRun, RewriteSession, WorkingRevision, Workspace

__all__ = [
    "add_session",
    "add_rewrite_run",
    "add_working_revision",
    "get_rewrite_run",
    "get_owned_record",
    "get_session",
    "get_source_revision",
    "get_working_revision",
    "latest_working_revision",
]


def get_session(db: Session, session_id: str) -> RewriteSession | None:
    return db.get(RewriteSession, session_id)


def get_working_revision(db: Session, revision_id: str) -> WorkingRevision | None:
    return db.get(WorkingRevision, revision_id)


def get_source_revision(db: Session, revision_id: str) -> DocumentRevision | None:
    return db.get(DocumentRevision, revision_id)


def get_rewrite_run(db: Session, run_id: str) -> RewriteRun | None:
    return db.get(RewriteRun, run_id)


def latest_working_revision(db: Session, session_id: str) -> WorkingRevision | None:
    return (
        db.query(WorkingRevision)
        .filter(WorkingRevision.session_id == session_id)
        .order_by(WorkingRevision.created_at.desc())
        .first()
    )


def add_working_revision(
    db: Session,
    *,
    session_id: str,
    content: str,
    parent_revision_id: str | None = None,
) -> WorkingRevision:
    revision = WorkingRevision(
        session_id=session_id,
        parent_revision_id=parent_revision_id,
        content=content,
    )
    db.add(revision)
    db.flush()
    return revision


def add_rewrite_run(db: Session, **values) -> RewriteRun:
    run = RewriteRun(**values)
    db.add(run)
    db.flush()
    return run


def add_session(db: Session, **values) -> RewriteSession:
    session = RewriteSession(**values)
    db.add(session)
    db.flush()
    return session


def get_owned_record(db: Session, model, record_id: str, user_id: str):
    """Read a rewrite record through its document and workspace owner.

    Joined paths preserve ownership checks for source, baseline, working
    revisions and runs, rather than trusting IDs supplied by the caller.
    """
    paths = {
        Document: (Workspace,),
        DocumentRevision: (Document, Workspace),
        RewriteSession: (Document, Workspace),
        WorkingRevision: (RewriteSession, Document, Workspace),
        RewriteRun: (RewriteSession, Document, Workspace),
    }
    query = db.query(model)
    for target in paths[model]:
        query = query.join(target)
    return query.filter(model.id == record_id, Workspace.owner_user_id == user_id).first()
