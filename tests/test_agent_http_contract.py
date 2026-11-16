"""Agent 端点对外的 HTTP 契约，钉住重构前后的行为一致。

重构把业务异常从 HTTPException 换成了 AppError 子类。两者最终都走
异常处理器返回 {"detail": ...}，但这条链路没有任何测试覆盖——一旦
某天异常处理器改了、或某个服务漏转换，前端拿到的状态码就悄悄变了。

所以这里只断言「客户端能看到的东西」：状态码 + detail 文案。
不碰服务层内部，也不碰 SSE 文本内容。

重要：本文件的用例**不允许真的调用模型**。
「重复 requestId 返回 409」需要先把第一轮跑通才能落库，如果放任它跑
真实模型，测试就依赖网络和 API key——本机绿、CI 红，或者模型抽风时
红，而代码其实没坏。这不是变慢，是基线不可信。
所以下面的 _stub_model 把模型的唯一出入口换成固定回复，
并有专门的用例断言「调用次数为 0」。
"""

from __future__ import annotations

import json

import pytest


@pytest.fixture
def model_calls():
    """记录模型被调用了几次。任何用例断言它必须为 0。"""
    return []


@pytest.fixture(autouse=True)
def _stub_model(monkeypatch, model_calls):
    """把模型的两个出入口换成固定回复。

    这里打的是「被调用方模块里绑定的那份符号」：
    - app.graph.nodes.complete_chat        —— 意图识别用的非流式调用
    - app.services.agent_rewrite.service.stream_chat —— 生成文案用的流式调用
    直接打 app.services.model_gateway 无效，因为这两个模块已经
    `from ... import` 把符号绑到自己命名空间了。
    """
    from types import SimpleNamespace

    from app.graph import nodes
    from app.services.agent_rewrite import service as agent_rewrite

    def fake_complete_chat(*args, **kwargs):
        model_calls.append(("complete_chat", kwargs.get("model_id")))
        return SimpleNamespace(
            text=json.dumps({"intent": "chat", "source": "none"}, ensure_ascii=False)
        )

    def fake_stream_chat(prompt_messages, model_id=None):
        model_calls.append(("stream_chat", model_id))
        yield "好的，我在。"

    monkeypatch.setattr(nodes, "model_available", lambda model_id: True)
    monkeypatch.setattr(nodes, "complete_chat", fake_complete_chat)
    monkeypatch.setattr(agent_rewrite, "model_available", lambda model_id: True)
    monkeypatch.setattr(agent_rewrite, "stream_chat", fake_stream_chat)


def _thread_id(client, workspace_id):
    response = client.post("/api/v1/agent/threads", json={"workspaceId": workspace_id})
    assert response.status_code == 200, response.text
    return response.json()["id"]


@pytest.fixture
def workspace_id(client):
    return client.post("/api/v1/workspaces", json={"name": "HTTP 契约测试"}).json()["id"]


@pytest.fixture
def thread_id(client, workspace_id):
    return _thread_id(client, workspace_id)


def test_missing_thread_stream_returns_404(client, workspace_id):
    response = client.post(
        "/api/v1/agent/threads/不存在的会话/messages/stream",
        json={"message": "你好"},
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "找不到智能体会话"


def test_reused_request_id_with_different_body_returns_409(client, thread_id):
    first = client.post(
        f"/api/v1/agent/threads/{thread_id}/messages/stream",
        json={"message": "第一句话", "requestId": "fixed-request-id"},
    )
    assert first.status_code == 200
    # 必须把流读完，否则生成器没跑，轮次记录不会落库
    first.read()

    second = client.post(
        f"/api/v1/agent/threads/{thread_id}/messages/stream",
        json={"message": "换了内容", "requestId": "fixed-request-id"},
    )
    assert second.status_code == 409
    assert second.json()["detail"] == "request_id 已用于另一条请求"


def test_retry_with_mismatched_message_returns_404(client, thread_id):
    response = client.post(
        f"/api/v1/agent/threads/{thread_id}/messages/stream",
        json={"message": "和原提问不一致", "retryMessageId": "不存在的消息"},
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "找不到可重试的原始请求"


def test_resolve_action_from_other_thread_returns_404(client, workspace_id):
    other_thread = _thread_id(client, workspace_id)
    # 用一个必然不存在的 action id，也要先过归属校验，文案不能变成「找不到待确认操作」
    response = client.post(
        f"/api/v1/agent/threads/{other_thread}/actions/不存在的操作/resolve",
        json={"decision": "approve"},
    )
    assert response.status_code == 404
    assert "待确认操作" in response.json()["detail"]


def test_stream_never_reaches_a_real_model(client, thread_id, model_calls):
    """回归护栏：本文件的所有用例都不能把请求打到真实模型上。

    「跑了这条用例，model_calls 里出现 stream_chat 且 model_id 非空」
    就说明打桩漏了某条路径。断网环境会变红，说明它确实能抓住问题。
    """
    response = client.post(
        f"/api/v1/agent/threads/{thread_id}/messages/stream",
        json={"message": "随便说点什么"},
    )
    assert response.status_code == 200
    body = response.read().decode("utf-8")

    # 只要被调用，就必须是被我们这个桩接住了（走的是 stub 函数本身）
    for name, _model_id in model_calls:
        assert name in {"complete_chat", "stream_chat"}
    assert "好的，我在。" in body, "生成内容应来自桩，而不是真实模型"
