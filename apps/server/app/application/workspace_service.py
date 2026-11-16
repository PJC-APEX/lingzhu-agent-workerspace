"""Workspace use cases; transaction ownership stays here."""
import logging
from sqlalchemy.orm import Session

from app.repositories import workspace_repo
from app.domain.exceptions import NotFoundError

logger = logging.getLogger(__name__)


def require_workspace(db: Session, workspace_id: str, user_id: str):
    workspace = workspace_repo.get_owned(db, workspace_id, user_id) if user_id else None
    if workspace is None:
        raise NotFoundError("找不到工作区")
    return workspace


def list_workspaces(db: Session, *, user_id: str) -> list[dict]:
    return [{"id": item.id, "name": item.name} for item in workspace_repo.list_owned(db, user_id)]


def create_workspace(db: Session, *, name: str, user_id: str) -> dict:
    try:
        workspace = workspace_repo.add(db, name=name, user_id=user_id)
        db.commit()
        db.refresh(workspace)
    except Exception:
        db.rollback()
        raise
    logger.info("工作区已创建，workspace_id=%s", workspace.id)
    return {"id": workspace.id, "name": workspace.name}
