"""Users and login sessions; callers own commit and rollback."""
from sqlalchemy.orm import Session
from app.domain.models import AuthSession, User


def add_user(db: Session, *, email: str, password_hash: str) -> User:
    user = User(email=email, password_hash=password_hash)
    db.add(user)
    db.flush()
    return user


def find_user(db: Session, email: str) -> User | None:
    return db.query(User).filter(User.email == email).first()


def get_user(db: Session, user_id: str) -> User | None:
    return db.get(User, user_id)


def add_session(db: Session, **values) -> None:
    db.add(AuthSession(**values))


def find_session(db: Session, token_hash: str) -> AuthSession | None:
    return db.query(AuthSession).filter(AuthSession.token_hash == token_hash).first()


def delete_session(db: Session, session: AuthSession) -> None:
    db.delete(session)


def delete_token(db: Session, token_hash: str) -> None:
    db.query(AuthSession).filter(AuthSession.token_hash == token_hash).delete()
