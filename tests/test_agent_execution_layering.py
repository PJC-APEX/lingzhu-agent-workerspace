"""Agent use-case boundary and event contracts independent of HTTP encoding."""
import pytest

from app.application import agent_execution_service as execution
from app.application import agent_result_service
from app.application.agent_commands import MessageCommand, ActionCommand
from app.api.v1.agent import AgentMessageRequest
from app.domain.exceptions import ConflictError, NotFoundError
from app.domain.models import AgentAction, AgentMessage, AgentThread, AgentTurnRequest, Job, JobOutboxEvent, Workspace
from app.services.model_gateway import ModelCallError


@pytest.fixture
def conversation(client, monkeypatch):
    workspace = client.post("/api/v1/workspaces", json={"name": "执行边界"}).json()
    thread = client.post("/api/v1/agent/threads", json={"workspaceId": workspace["id"]}).json()
    factory = client.app.state.testing_session_factory
    with factory() as db:
        owner = db.get(Workspace, workspace["id"]).owner_user_id
    monkeypatch.setattr(execution, "maybe_schedule_summary", lambda thread_id: None)
    return factory, owner, workspace["id"], thread["id"]


def stub_graph(monkeypatch, *, failure=None, reply="完成"):
    calls = []
    monkeypatch.setattr(execution, "get_agent_graph_state", lambda thread_id: {"state": {}, "interrupts": []})
    def stream(thread_id, **kwargs):
        calls.append(kwargs)
        yield {"kind": "delta", "delta": "片段"}
        if failure:
            raise failure
        yield {"kind": "finished", "state": {"reply": reply, "mode": "demo", "stage": "chat"}, "interrupts": []}
    monkeypatch.setattr(execution, "stream_agent_graph", stream)
    return calls


def test_command_fingerprint_matches_existing_http_payload():
    body = AgentMessageRequest(message="你好", requestId="same")
    from app.application.agent_service import fingerprint_request
    assert fingerprint_request(body) == fingerprint_request(MessageCommand(**body.model_dump()))


def test_generated_request_id_matches_events_and_persisted_turn(conversation, monkeypatch):
    factory, owner, _, thread_id = conversation
    calls = stub_graph(monkeypatch)
    with factory() as db:
        events = list(execution.stream_agent_message(db, thread_id, MessageCommand(message="你好"), owner))
        turn = db.query(AgentTurnRequest).filter(AgentTurnRequest.thread_id == thread_id).one()
        assert events[0]["requestId"] == turn.request_id == calls[0]["request_id"]
        assert events[0]["userMessageId"] == turn.user_message_id
        assert turn.status == "completed"
        assert turn.response_message_id == events[-2]["message"]["id"]
    assert [item["type"] for item in events if item] == ["message.started", "message.delta", "message.completed"]
    assert events[-1] is None


@pytest.mark.parametrize("fails", [False, True])
def test_completed_and_failed_turns_replay_without_reexecution(conversation, monkeypatch, fails):
    factory, owner, _, thread_id = conversation
    failure = ModelCallError("timeout", "模型超时", retryable=True) if fails else None
    calls = stub_graph(monkeypatch, failure=failure)
    command = MessageCommand(message="你好", requestId="fixed")
    with factory() as db:
        first = list(execution.stream_agent_message(db, thread_id, command, owner))
        second = list(execution.stream_agent_message(db, thread_id, command, owner))
        assert len(calls) == 1
        assert db.query(AgentMessage).filter(AgentMessage.role == "user").count() == 1
        turn = db.query(AgentTurnRequest).one()
        assert turn.status == ("failed" if fails else "completed")
    assert second[0]["requestId"] == "fixed"
    assert second[-2] == first[-2]
    assert second[-1] is None
    if fails:
        assert second[-2]["modelFailure"] is True
        assert second[-2]["category"] == "timeout"


def test_in_progress_replay_is_rejected_before_streaming(conversation):
    factory, owner, _, thread_id = conversation
    command = MessageCommand(message="处理中", requestId="processing")
    with factory() as db:
        thread = execution.agent_service.get_thread(db, thread_id, owner)
        execution.agent_service.prepare_turn(db, thread, command, owner)
        with pytest.raises(ConflictError, match="该请求正在处理中，请稍后查询或重试"):
            execution.stream_agent_message(db, thread_id, command, owner)


def test_action_use_case_checks_owner_before_rejecting_or_claiming(conversation):
    factory, owner, _, thread_id = conversation
    with factory() as db:
        action = AgentAction(thread_id=thread_id, action_type="generate_images", payload={"finalText": "测试"})
        db.add(action)
        db.commit()
        action_id = action.id
        for decision in ("approve", "reject"):
            with pytest.raises(NotFoundError):
                execution.resolve_agent_action(db, thread_id, action_id, ActionCommand(decision), "stranger")
        assert db.get(AgentAction, action_id).status == "awaiting_confirmation"
        assert db.query(Job).count() == 0
        assert db.query(AgentMessage).count() == 0


def test_action_dispatch_failure_preserves_durable_intent_and_existing_message(conversation, monkeypatch):
    factory, owner, _, thread_id = conversation
    def unavailable(job_id):
        raise RuntimeError("broker unavailable")
    monkeypatch.setattr(execution, "launch_job", unavailable)
    with factory() as db:
        action = AgentAction(thread_id=thread_id, action_type="generate_images", payload={"finalText": "测试"})
        db.add(action)
        db.commit()
        action_id = action.id
        result = execution.resolve_agent_action(db, thread_id, action_id, ActionCommand("approve"), owner)
        assert result["status"] == "queued"
        job = db.get(Job, result["jobId"])
        assert job.status == "queued"
        assert job.message == "队列暂不可用，系统将自动重试投递"
        assert job.error == "等待任务队列恢复"
        assert db.query(JobOutboxEvent).filter(JobOutboxEvent.job_id == job.id).one().status == "pending"
        assert db.get(AgentAction, action_id).job_id == job.id
        assert db.query(AgentMessage).filter(AgentMessage.message_type == "job").count() == 1
        replay = execution.resolve_agent_action(db, thread_id, action_id, ActionCommand("approve"), owner)
        assert replay == {"status": "already_processed", "jobId": job.id}


def test_legacy_reply_failure_rolls_back_flushed_user_message(conversation, monkeypatch):
    factory, owner, _, thread_id = conversation
    class BrokenGraph:
        def invoke(self, state):
            raise RuntimeError("legacy model failed")
    monkeypatch.setattr(execution, "agent_graph", BrokenGraph())
    with factory() as db:
        with pytest.raises(RuntimeError, match="legacy model failed"):
            execution.send_agent_message(db, thread_id, MessageCommand(message="你好"), owner)
        assert db.query(AgentMessage).count() == 0
        assert db.get(AgentThread, thread_id).title == "新对话"


def test_graph_card_persistence_rolls_back_on_message_write_failure(conversation, monkeypatch):
    factory, _, _, thread_id = conversation
    original = agent_result_service.agent_repo.add_record
    def fail_message(db, record):
        if isinstance(record, AgentMessage):
            raise RuntimeError("message insert failed")
        return original(db, record)
    monkeypatch.setattr(agent_result_service.agent_repo, "add_record", fail_message)
    with pytest.raises(RuntimeError, match="message insert failed"):
        agent_result_service.persist_agent_graph_result(thread_id, {
            "state": {"stage": "confirm_final", "final_text": "正文"},
            "interrupts": [{"kind": "confirm_final", "previewText": "正文"}],
        })
    with factory() as db:
        assert db.query(AgentAction).count() == 0
        assert db.query(AgentMessage).count() == 0


def test_history_use_cases_deny_stranger_without_http_dependencies(conversation):
    factory, _, workspace_id, thread_id = conversation
    with factory() as db:
        for call in (
            lambda: execution.list_agent_threads(db, workspace_id, "stranger"),
            lambda: execution.get_thread_messages(db, thread_id, "stranger"),
            lambda: execution.create_thread(db, workspace_id, "stranger"),
            lambda: execution.send_agent_message(db, thread_id, MessageCommand(message="越权"), "stranger"),
            lambda: execution.stream_agent_message(db, thread_id, MessageCommand(message="越权"), "stranger"),
        ):
            with pytest.raises(NotFoundError):
                call()
        assert db.query(AgentThread).count() == 1


def test_context_history_excludes_current_message_and_summarized_prefix(conversation, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from app.application import agent_context_service

    factory, _, _, thread_id = conversation
    now = datetime.now(timezone.utc)
    with factory() as db:
        messages = [AgentMessage(thread_id=thread_id, role="user", content=content,
                                 created_at=now + timedelta(seconds=index))
                    for index, content in enumerate(["已摘要", "保留的历史", "当前输入"])]
        db.add_all(messages)
        db.flush()
        thread = db.get(AgentThread, thread_id)
        thread.summary_text = "摘要内容"
        thread.summary_through_message_id = messages[0].id
        current_id = messages[-1].id
        db.commit()
    captured = {}
    def prepare(*args, **kwargs):
        captured.update(kwargs)
        return "prepared"
    monkeypatch.setattr(agent_context_service, "prepare_rewrite", prepare)
    assert agent_context_service.prepare_agent_rewrite(
        thread_id, "当前输入", None, "deepseek-v4-flash", current_message_id=current_id,
    ) == "prepared"
    assert captured["summary"] == "摘要内容"
    assert captured["history"] == [{"role": "user", "content": "保留的历史", "metadata": {}}]
