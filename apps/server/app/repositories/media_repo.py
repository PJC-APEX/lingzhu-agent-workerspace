"""Owned exports and preview references used by private downloads."""
from sqlalchemy.orm import Session

from app.domain.models import Asset, ExportFile, Job, Workspace


def get_owned_export(db: Session, export_id: str, user_id: str) -> ExportFile | None:
    return db.query(ExportFile).join(Job).join(Workspace).filter(
        ExportFile.id == export_id, Workspace.owner_user_id == user_id,
    ).first()


def owned_asset_references(db: Session, user_id: str):
    return db.query(Asset.id, Asset.object_path).join(Workspace).filter(Workspace.owner_user_id == user_id).all()


def owned_preview_results(db: Session, user_id: str):
    rows = db.query(Job).join(Workspace).filter(
        Workspace.owner_user_id == user_id, Job.job_type == "image_generation",
    ).all()
    return [(job.id, job.result) for job in rows if (job.payload or {}).get("agentGraphPreview")]
