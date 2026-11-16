"""Ownership boundary shared by rewrite use cases and response snapshots."""
from sqlalchemy.orm import Session

from app.domain.exceptions import NotFoundError
from app.repositories import rewrite_repo


def require_owned(db: Session, model, record_id: str, user_id: str, label: str):
    item = rewrite_repo.get_owned_record(db, model, record_id, user_id) if user_id else None
    if item is None:
        raise NotFoundError(f"找不到{label}")
    return item
