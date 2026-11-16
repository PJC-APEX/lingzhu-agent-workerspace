"""Cookie/HTTP compatibility, session atomicity and lease ownership."""
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie

import pytest

from app.application import auth_service, thread_execution
from app.core.identity import current_user_id
from app.domain.exceptions import NotFoundError, UnauthorizedError
from app.domain.models import AuthSession, AgentThread, User, Workspace
from app.repositories import thread_lease_repo
from app.services.auth_crypto import token_digest, hash_password, verify_password


def test_existing_password_encoding_remains_readable():
    encoded = hash_password("a password", salt=b"0123456789abcdef")
    assert encoded.startswith("pbkdf2_sha256$600000$30313233343536373839616263646566$")
    assert verify_password("a password", encoded)
    assert not verify_password("wrong", encoded)
    assert not verify_password("anything", "not-a-hash")


@pytest.mark.parametrize("setting,secure", [("", False), ("true", True), ("false", False)])
def test_login_preserves_cookie_attributes_and_stores_only_digest(client, monkeypatch, setting, secure):
    monkeypatch.setenv("AUTH_COOKIE_SECURE", setting)
    response = client.post("/api/v1/auth/login", json={
        "email": "fixture@example.com", "password": "fixture-password-123",
    })
    assert response.status_code == 200
    cookies = SimpleCookie()
    cookies.load(response.headers["set-cookie"])
    cookie = cookies["lingzhu_session"]
    assert bool(cookie["httponly"]) is True
    assert bool(cookie["secure"]) is secure
    assert cookie["samesite"] == "lax"
    assert cookie["path"] == "/"
    assert cookie["max-age"] == str(30 * 24 * 60 * 60)
    with client.app.state.testing_session_factory() as db:
        session = db.query(AuthSession).filter(AuthSession.token_hash == token_digest(cookie.value)).one()
        assert session.token_hash != cookie.value
        assert session.user_id == response.json()["user"]["id"]


def test_expired_session_is_deleted_and_401_wording_preserved(client):
    token = client.cookies.get("lingzhu_session")
    factory = client.app.state.testing_session_factory
    with factory() as db:
        session = db.query(AuthSession).filter(AuthSession.token_hash == token_digest(token)).one()
        session.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
    response = client.get("/api/v1/auth/me")
    assert response.status_code == 401
    assert response.json() == {"detail": "登录状态已过期，请重新登录"}
    with factory() as db:
        assert db.query(AuthSession).filter(AuthSession.token_hash == token_digest(token)).count() == 0


def test_logout_revokes_token_and_clears_cookie(client):
    token = client.cookies.get("lingzhu_session")
    response = client.post("/api/v1/auth/logout")
    assert response.status_code == 200 and response.json() == {"ok": True}
    assert 'Max-Age=0' in response.headers["set-cookie"]
    assert client.get("/api/v1/auth/me").json() == {"detail": "请先登录"}
    with client.app.state.testing_session_factory() as db:
        with pytest.raises(UnauthorizedError, match="登录状态已失效，请重新登录"):
            auth_service.authenticate(db, token)


def test_registration_session_failure_rolls_back_flushed_user(client, monkeypatch):
    factory = client.app.state.testing_session_factory
    def fail(*args, **kwargs):
        raise RuntimeError("session insert failed")
    monkeypatch.setattr(auth_service.auth_repo, "add_session", fail)
    with factory() as db:
        with pytest.raises(RuntimeError, match="session insert failed"):
            auth_service.register(db, email="new@example.com", password="new-password-123")
        assert db.query(User).filter(User.email == "new@example.com").count() == 0
        assert db.query(AuthSession).count() == 1


def test_duplicate_registration_and_bad_login_keep_error_contract(client):
    payload = {"email": "fixture@example.com", "password": "fixture-password-123"}
    duplicate = client.post("/api/v1/auth/register", json=payload)
    assert duplicate.status_code == 409
    assert duplicate.json() == {"detail": "该邮箱已注册"}
    denied = client.post("/api/v1/auth/login", json={**payload, "password": "wrong-password"})
    assert denied.status_code == 401
    assert denied.json() == {"detail": "邮箱或密码不正确"}


def test_cross_site_business_write_is_rejected_before_creating_workspace(client):
    response = client.post("/api/v1/workspaces", json={"name": "not created"},
                           headers={"sec-fetch-site": "cross-site"})
    assert response.status_code == 403
    assert response.json() == {"detail": "请求来源校验失败"}
    with client.app.state.testing_session_factory() as db:
        assert db.query(Workspace).count() == 0
    assert current_user_id() is None


def test_owned_lease_rejects_stranger_and_stale_holder_cannot_release_successor(client):
    workspace = client.post("/api/v1/workspaces", json={"name": "lease"}).json()
    thread = client.post("/api/v1/agent/threads", json={"workspaceId": workspace["id"]}).json()
    factory = client.app.state.testing_session_factory
    with pytest.raises(NotFoundError, match="找不到智能体会话"):
        thread_execution.acquire_owned_thread_lease(thread["id"], "stranger")
    with factory() as db:
        db.get(AgentThread, thread["id"]).execution_token = "old"
        db.get(AgentThread, thread["id"]).execution_lease_until = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
        now = datetime.now(timezone.utc)
        assert thread_lease_repo.claim(db, thread["id"], "new", now, now + timedelta(seconds=900)) == 1
        db.commit()
        assert thread_lease_repo.renew(db, thread["id"], "old", now + timedelta(seconds=900)) == 0
        thread_lease_repo.release(db, thread["id"], "old")
        db.commit()
        assert db.get(AgentThread, thread["id"]).execution_token == "new"
