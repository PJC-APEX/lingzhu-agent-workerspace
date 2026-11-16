"""Agent 对话一轮执行的业务规则测试。

这些规则以前埋在 routes.py 的 stream_agent_message 里，只能通过发一次
SSE 请求间接覆盖；出错时既不报哪个规则坏了，也很难构造边界。

重点钉住四件事：
1. 幂等：同一个 request_id 内容相同就复用，内容不同就 409
2. 重试：retryMessageId 必须指向本会话里内容完全一致的原始提问
3. 续接计划：等 SRT / 等确认 / 生图排队中，三种中断各走各的路
4. 轮次记录：结果有消息就 completed，没有就 failed

注意：这里只测「服务层算出来的东西」，不测 SSE 文本——那是传输格式。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.application import agent_service
from app.domain.exceptions import ConflictError, NotFoundError, ValidationError
from app.domain.models import AgentAction, AgentMessage, AgentThread, AgentTurnRequest, Asset, Job


@pytest.fixture
def factory(client):
    return client.app.state.testing_session_factory


@pytest.fixture
def workspace_id(client):
    return client.post("/api/v1/workspaces", json={"name": "Agent 服务测试"}).json()["id"]


@pytest.fixture
def thread_id(factory, workspace_id):
    with factory() as db:
        thread = AgentThread(workspace_id=workspace_id)
        db.add(thread)
        db.commit()
        return thread.id


def _body(**overrides):
    """构造一个最小请求体，字段与 AgentMessageRequest 对齐。"""
    defaults = dict(
        message="帮我写一段",
        contextText="",
        subtitleFileId=None,
        modelId="deepseek-v4-flash",
        requestId=None,
        retryMessageId=None,
        rewritePromptVersionId=None,
        storyboardPromptVersionId=None,
        imageStylePromptVersionId=None,
    )
    defaults.update(overrides)

    def model_dump(mode="json", exclude=None):
        return {k: v for k, v in defaults.items() if k not in (exclude or set())}

    return SimpleNamespace(**defaults, model_dump=model_dump)


def test_fingerprint_ignores_request_id_only():
    a = agent_service.fingerprint_request(_body(requestId="r1"))
    b = agent_service.fingerprint_request(_body(requestId="r2"))
    c = agent_service.fingerprint_request(_body(requestId="r1", message="换了一句话"))
    assert a == b, "request_id 本身不该影响指纹"
    assert a != c, "消息内容变了指纹必须变"


def test_find_existing_turn_reuses_same_payload(factory, thread_id):
    with factory() as db:
        db.add(AgentTurnRequest(
            thread_id=thread_id, request_id="r1", request_hash="hash-a",
            user_message_id="m1", status="completed",
        ))
        db.commit()
    with factory() as db:
        found = agent_service.find_existing_turn(db, thread_id, "r1", "hash-a")
    assert found is not None
    assert found.request_id == "r1"


def test_find_existing_turn_conflicts_on_different_payload(factory, thread_id):
    with factory() as db:
        db.add(AgentTurnRequest(
            thread_id=thread_id, request_id="r1", request_hash="hash-a",
            user_message_id="m1", status="completed",
        ))
        db.commit()
    with factory() as db:
        with pytest.raises(ConflictError) as exc:
            agent_service.find_existing_turn(db, thread_id, "r1", "hash-b")
    assert exc.value.status_code == 409


def test_find_existing_turn_returns_none_for_new_request(factory, thread_id):
    with factory() as db:
        assert agent_service.find_existing_turn(db, thread_id, "never-seen", "x") is None


def test_get_thread_raises_not_found(factory):
    with factory() as db:
        with pytest.raises(NotFoundError):
            agent_service.get_thread(db, "no-such-thread", "any-user")


def test_prepare_turn_saves_message_and_names_thread(factory, thread_id, monkeypatch):
    """首轮对话用首句当标题；第二轮不该再改标题。"""
    monkeypatch.setattr(
        agent_service, "resolve_agent_prompt_version",
        lambda db, version_id, kind, user_id: SimpleNamespace(
            prompt_id=f"{kind}-p", version_id=f"{kind}-v", version=1, content=f"{kind} content",
        ),
    )
    with factory() as db:
        thread = db.get(AgentThread, thread_id)
        assert thread.title == "新对话"
        ctx = agent_service.prepare_turn(db, thread, _body(message="第一句话就是标题"), "user-1")
        assert db.get(AgentThread, thread_id).title == "第一句话就是标题"
        # 在 session 内读字段：TurnContext 持有 ORM 对象，真实调用里 session 一直开着
        assert ctx.turn_request.status == "processing"
        assert ctx.user_message.role == "user"
        user_message_id = ctx.user_message.id

    with factory() as db:
        thread = db.get(AgentThread, thread_id)
        agent_service.prepare_turn(db, thread, _body(message="第二句话"), "user-1")
        assert db.get(AgentThread, thread_id).title == "第一句话就是标题", "标题只在首轮设置"
        assert db.get(AgentMessage, user_message_id).content == "第一句话就是标题"


def test_prepare_turn_rejects_mismatched_retry_source(factory, thread_id, monkeypatch):
    """重试必须指向本会话里内容一致的原始提问，否则当作找不到。"""
    monkeypatch.setattr(
        agent_service, "resolve_agent_prompt_version",
        lambda db, version_id, kind, user_id: SimpleNamespace(
            prompt_id=f"{kind}-p", version_id=f"{kind}-v", version=1, content="",
        ),
    )
    with factory() as db:
        other = AgentMessage(thread_id=thread_id, role="user", content="原话", message_type="text")
        db.add(other)
        db.commit()
        other_id = other.id

    with factory() as db:
        thread = db.get(AgentThread, thread_id)
        with pytest.raises(NotFoundError):
            agent_service.prepare_turn(db, thread, _body(message="改过的内容", retryMessageId=other_id), "user-1")


def test_plan_resume_without_interrupt_is_plain_new_input(factory, thread_id):
    with factory() as db:
        thread = db.get(AgentThread, thread_id)
        plan = agent_service.plan_resume(db, thread, _body(), None, [], lambda *a: {})
    assert plan.resume is None
    assert plan.detached_chat is False


def test_plan_resume_wait_srt_requires_subtitle(factory, thread_id):
    with factory() as db:
        thread = db.get(AgentThread, thread_id)
        with pytest.raises(ConflictError) as exc:
            agent_service.plan_resume(
                db, thread, _body(subtitleFileId=None), None,
                [{"kind": "wait_srt"}], lambda *a: {},
            )
    assert exc.value.status_code == 409


def test_plan_resume_wait_srt_rejects_foreign_subtitle(factory, thread_id, workspace_id):
    from app.domain.models import SubtitleFile, Workspace

    with factory() as db:
        other_ws = Workspace(name="别人的工作区", owner_user_id="someone-else")
        db.add(other_ws)
        db.flush()
        sub = SubtitleFile(
            workspace_id=other_ws.id, file_name="a.srt",
            object_path="a.srt", audio_object_path="x.mp3", cues=[],
        )
        db.add(sub)
        db.commit()
        sub_id = sub.id

    with factory() as db:
        thread = db.get(AgentThread, thread_id)
        with pytest.raises(ValidationError):
            agent_service.plan_resume(
                db, thread, _body(subtitleFileId=sub_id), None,
                [{"kind": "wait_srt"}], lambda *a: {},
            )


def test_plan_resume_image_job_blocks_typed_advance(factory, thread_id):
    with factory() as db:
        thread = db.get(AgentThread, thread_id)
        with pytest.raises(ConflictError) as exc:
            agent_service.plan_resume(db, thread, _body(), None, [{"kind": "image_job"}], lambda *a: {})
    assert "取消按钮" in exc.value.message


def test_plan_resume_chat_keeps_card_open(factory, thread_id):
    with factory() as db:
        thread = db.get(AgentThread, thread_id)
        plan = agent_service.plan_resume(
            db, thread, _body(), None,
            [{"kind": "confirm_final"}],
            lambda message, kind, model_id: {"kind": "chat"},
        )
    assert plan.detached_chat is True
    assert plan.resume is None


def test_plan_resume_approve_confirms_card_and_assets(factory, thread_id, workspace_id):
    with factory() as db:
        action = AgentAction(
            thread_id=thread_id, action_type="graph_confirm_images",
            status="awaiting_confirmation", payload={"assetIds": ["a1"]},
        )
        db.add(action)
        db.flush()
        action_id = action.id
        asset = Asset(
            id="a1", workspace_id=workspace_id, job_id="j1", file_name="a1.png",
            object_path="p", scene_number=1, status="needs_review",
        )
        db.add(asset)
        db.commit()

    with factory() as db:
        thread = db.get(AgentThread, thread_id)
        plan = agent_service.plan_resume(
            db, thread, _body(), None,
            [{"kind": "confirm_images"}],
            lambda message, kind, model_id: {"kind": "approve"},
        )
    assert plan.resume == {"decision": "approve"}
    assert plan.resolved_confirmation is True
    with factory() as db:
        assert db.get(AgentAction, action_id).status == "confirmed"
        assert db.get(Asset, "a1").status == "approved"


def test_plan_resume_revise_style_sets_target(factory, thread_id):
    with factory() as db:
        thread = db.get(AgentThread, thread_id)
        plan = agent_service.plan_resume(
            db, thread, _body(message="换成水彩风"), None,
            [{"kind": "confirm_image_style"}],
            lambda message, kind, model_id: {"kind": "revise_style"},
        )
    assert plan.resume_target == "style"
    assert plan.resume == {"decision": "revise", "instruction": "换成水彩风", "target": "style"}


def test_build_input_state_carries_prompt_snapshot(factory, thread_id, monkeypatch):
    monkeypatch.setattr(
        agent_service, "resolve_agent_prompt_version",
        lambda db, version_id, kind, user_id: SimpleNamespace(
            prompt_id=f"{kind}-p", version_id=f"{kind}-v", version=3, content=f"{kind} body",
        ),
    )
    with factory() as db:
        thread = db.get(AgentThread, thread_id)
        ctx = agent_service.prepare_turn(db, thread, _body(), "user-1")
        state = agent_service.build_input_state(ctx, _body(), "user-1")
        assert state["current_message_id"] == ctx.user_message.id
    assert state["rewrite_prompt_version_id"] == "rewrite-v"
    assert state["image_style_prompt_version"] == 3
    assert state["image_style_prompt"] == "image_style body"


def test_persist_turn_result_marks_completed(factory, thread_id):
    with factory() as db:
        turn = AgentTurnRequest(thread_id=thread_id, request_id="r", request_hash="h", user_message_id="m", status="processing")
        db.add(turn)
        db.commit()
        turn_id = turn.id
        ok = agent_service.persist_turn_result(db, turn, {"id": "msg-1"})
    assert ok is True
    with factory() as db:
        row = db.get(AgentTurnRequest, turn_id)
        assert row.status == "completed"
        assert row.response_message_id == "msg-1"
        assert row.error_message is None


def test_persist_turn_result_marks_failed_without_message(factory, thread_id):
    with factory() as db:
        turn = AgentTurnRequest(thread_id=thread_id, request_id="r2", request_hash="h", user_message_id="m", status="processing")
        db.add(turn)
        db.commit()
        turn_id = turn.id
        ok = agent_service.persist_turn_result(db, turn, None)
    assert ok is False
    with factory() as db:
        row = db.get(AgentTurnRequest, turn_id)
        assert row.status == "failed"
        assert row.error_category == "request"
