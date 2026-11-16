"""工作区数据访问。

只做查询与写入，不做业务判断、不自行提交事务。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.domain.models import Workspace

__all__ = ["add", "get", "get_owned", "list_owned"]


def get(db: Session, workspace_id: str) -> Workspace | None:
    return db.get(Workspace, workspace_id)


def get_owned(db: Session, workspace_id: str, user_id: str) -> Workspace | None:
    """按 id 取工作区并校验归属。None 表示不存在或不属于该用户。"""
    workspace = db.get(Workspace, workspace_id)
    if workspace is None or workspace.owner_user_id != user_id:
        return None
    return workspace


def list_owned(db: Session, user_id: str) -> list[Workspace]:
    return db.query(Workspace).filter(Workspace.owner_user_id == user_id).order_by(Workspace.created_at).all()


def add(db: Session, *, name: str, user_id: str) -> Workspace:
    workspace = Workspace(name=name, owner_user_id=user_id)
    db.add(workspace)
    return workspace
