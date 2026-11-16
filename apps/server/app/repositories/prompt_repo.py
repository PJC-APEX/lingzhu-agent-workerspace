"""Prompt persistence. No business decisions and no transaction commits."""
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.domain.models import AgentPromptPreset, AgentPromptVersion


def get_owned_preset(db: Session, prompt_id: str, user_id: str | None):
    return db.query(AgentPromptPreset).filter(
        AgentPromptPreset.id == prompt_id, AgentPromptPreset.owner_user_id == user_id,
    ).first()


def get_version(db: Session, version_id: str):
    return db.get(AgentPromptVersion, version_id)


def resolve_owned_version(db: Session, version_id: str, kind: str, user_id: str | None):
    return db.query(AgentPromptPreset, AgentPromptVersion).join(
        AgentPromptVersion, AgentPromptVersion.prompt_id == AgentPromptPreset.id,
    ).filter(
        AgentPromptVersion.id == version_id, AgentPromptPreset.kind == kind,
        AgentPromptPreset.owner_user_id == user_id,
    ).first()


def list_owned(db: Session, kind: str, user_id: str | None):
    return db.query(AgentPromptPreset).filter(
        AgentPromptPreset.kind == kind, AgentPromptPreset.owner_user_id == user_id,
    ).order_by(AgentPromptPreset.created_at, AgentPromptPreset.id).all()


def list_versions(db: Session, prompt_id: str):
    return db.query(AgentPromptVersion).filter(
        AgentPromptVersion.prompt_id == prompt_id,
    ).order_by(AgentPromptVersion.version_number).all()


def latest_version_number(db: Session, prompt_id: str) -> int:
    return db.query(func.max(AgentPromptVersion.version_number)).filter(
        AgentPromptVersion.prompt_id == prompt_id,
    ).scalar() or 0


def add_preset(db: Session, **values):
    preset = AgentPromptPreset(**values)
    db.add(preset)
    db.flush()
    return preset


def add_version(db: Session, **values):
    version = AgentPromptVersion(**values)
    db.add(version)
    db.flush()
    return version
