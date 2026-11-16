"""智能体会话的跨用户越权防护。

两个层面都要守住：

1. HTTP 层：`require_thread_lease` 依赖在取会话前就校验了
   workspace.owner_user_id，陌生用户拿到别人的 thread_id 也是 404。
2. 服务层：`agent_service.get_thread` 自己也要校验。

第 2 条不能省。旧代码走 `_get_or_404(db, AgentThread, ...)`，那是带归属
校验的；搬进 application 层时若写成 `db.get(AgentThread, id)`，就等于
把安全性寄托在「上层刚好还有一道 lease 检查」上——哪天有人给新端点
忘了加那个依赖，漏洞就直接暴露。

所以这里跳过 HTTP 依赖，直接测服务层；HTTP 层单独用一条用例确认。
断言 404 而不是 403：不区分「不存在」和「不属于你」，避免探测 id 是否存在。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.application import agent_service
from app.domain.exceptions import NotFoundError
from app.domain.models import AgentAction, User, Workspace
from app.main import app


@pytest.fixture
def owner_and_stranger(client):
    """返回 (会话 id, 卡片 id, 属主 user_id, 陌生用户的 client)。"""
    factory = client.app.state.testing_session_factory
    workspace = client.post("/api/v1/workspaces", json={"name": "属主的工作区"}).json()
    thread = client.post("/api/v1/agent/threads", json={"workspaceId": workspace["id"]}).json()
    with factory() as db:
        action = AgentAction(thread_id=thread["id"], action_type="generate_images", payload={})
        db.add(action)
        db.commit()
        action_id = action.id
        owner_id = db.get(Workspace, workspace["id"]).owner_user_id

    stranger = TestClient(app)
    response = stranger.post(
        "/api/v1/auth/register",
        json={"email": "stranger-agent@example.com", "password": "stranger-password-123"},
    )
    assert response.status_code in (200, 201), response.text
    return thread["id"], action_id, owner_id, stranger


def _stranger_user_id(factory):
    with factory() as db:
        return db.query(User).filter(User.email == "stranger-agent@example.com").one().id


def test_service_rejects_thread_of_another_user(client, owner_and_stranger):
    thread_id, _action_id, _owner_id, _stranger = owner_and_stranger
    factory = client.app.state.testing_session_factory

    with factory() as db:
        with pytest.raises(NotFoundError):
            agent_service.get_thread(db, thread_id, _stranger_user_id(factory))


def test_service_returns_thread_for_its_owner(client, owner_and_stranger):
    thread_id, _action_id, owner_id, _stranger = owner_and_stranger
    factory = client.app.state.testing_session_factory

    with factory() as db:
        thread = agent_service.get_thread(db, thread_id, owner_id)
        assert thread.id == thread_id


def test_http_stream_rejects_stranger(owner_and_stranger):
    """HTTP 层也要 404：这里靠的是 require_thread_lease 的归属检查。"""
    thread_id, _action_id, _owner_id, stranger = owner_and_stranger

    response = stranger.post(
        f"/api/v1/agent/threads/{thread_id}/messages/stream",
        json={"message": "偷看别人的对话"},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "找不到智能体会话"


def test_http_resolve_rejects_stranger(owner_and_stranger):
    thread_id, action_id, _owner_id, stranger = owner_and_stranger

    response = stranger.post(
        f"/api/v1/agent/threads/{thread_id}/actions/{action_id}/resolve",
        json={"decision": "approve"},
    )

    assert response.status_code == 404
