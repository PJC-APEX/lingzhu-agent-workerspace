"""Subtitle persistence without transaction commits."""
from sqlalchemy.orm import Session

from app.domain.models import SubtitleFile




def add(db: Session, **values) -> SubtitleFile:
    subtitle = SubtitleFile(**values)
    db.add(subtitle)
    db.flush()
    return subtitle


def get(db: Session, subtitle_id: str) -> SubtitleFile | None:
    return db.get(SubtitleFile, subtitle_id)
