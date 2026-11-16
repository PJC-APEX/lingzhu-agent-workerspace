"""Registration, login and session lifecycle, independent of HTTP/Cookies."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import secrets

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.application.transactions import rollback_on_error
from app.domain.exceptions import ConflictError, UnauthorizedError
from app.repositories import auth_repo
from app.services.auth_crypto import hash_password, token_digest, verify_password

SESSION_DAYS = 30


@dataclass(frozen=True)
class LoginResult:
    user: dict
    token: str
    max_age: int = SESSION_DAYS * 24 * 60 * 60


def _user_payload(user) -> dict:
    return {"id": user.id, "email": user.email}


def _issue_session(db: Session, user) -> LoginResult:
    token = secrets.token_urlsafe(48)
    auth_repo.add_session(
        db, user_id=user.id, token_hash=token_digest(token),
        expires_at=datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS),
    )
    db.commit()
    return LoginResult(user=_user_payload(user), token=token)


@rollback_on_error
def register(db: Session, *, email: str, password: str) -> LoginResult:
    try:
        user = auth_repo.add_user(db, email=email.strip().lower(), password_hash=hash_password(password))
        return _issue_session(db, user)
    except IntegrityError as exc:
        raise ConflictError("该邮箱已注册") from exc


@rollback_on_error
def login(db: Session, *, email: str, password: str) -> LoginResult:
    user = auth_repo.find_user(db, email.strip().lower())
    if not user or not verify_password(password, user.password_hash):
        raise UnauthorizedError("邮箱或密码不正确")
    return _issue_session(db, user)


def authenticate(db: Session, token: str | None) -> dict:
    if not token:
        raise UnauthorizedError("请先登录")
    session = auth_repo.find_session(db, token_digest(token))
    if not session:
        raise UnauthorizedError("登录状态已失效，请重新登录")
    expires = session.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires <= datetime.now(timezone.utc):
        auth_repo.delete_session(db, session)
        db.commit()
        raise UnauthorizedError("登录状态已过期，请重新登录")
    user = auth_repo.get_user(db, session.user_id)
    if not user:
        raise UnauthorizedError("登录状态已失效，请重新登录")
    return _user_payload(user)


@rollback_on_error
def logout(db: Session, token: str | None) -> dict:
    if token:
        auth_repo.delete_token(db, token_digest(token))
        db.commit()
    return {"ok": True}
