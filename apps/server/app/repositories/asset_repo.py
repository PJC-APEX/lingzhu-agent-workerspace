"""Asset persistence; callers own authorization decisions and transactions."""
from sqlalchemy.orm import Session

from app.domain.models import Asset, Workspace


def list_by_workspace(db: Session, workspace_id: str) -> list[Asset]:
    return db.query(Asset).filter(Asset.workspace_id == workspace_id).order_by(Asset.created_at.desc()).all()


def list_selected(db: Session, workspace_id: str, asset_ids: list[str]) -> list[Asset]:
    return db.query(Asset).filter(
        Asset.workspace_id == workspace_id, Asset.id.in_(asset_ids),
    ).order_by(Asset.scene_number, Asset.created_at).all()


def get_owned(db: Session, asset_id: str, user_id: str) -> Asset | None:
    return db.query(Asset).join(Workspace).filter(
        Asset.id == asset_id, Workspace.owner_user_id == user_id,
    ).first()


def list_for_job(db: Session, job_id: str) -> list[Asset]:
    return db.query(Asset).filter(Asset.job_id == job_id).order_by(Asset.scene_number, Asset.created_at).all()
