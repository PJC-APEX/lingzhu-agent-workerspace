"""任务执行期状态机的规则测试。

这些规则以前埋在 workers/local_tasks.py 里，只能通过「跑一个完整任务」
间接覆盖，出错时很难定位。下沉到 application 后可以单独验证。

重点钉住三件事：
1. 执行权令牌：不是我的令牌就不能写状态
2. 取消优先：任务进入取消流程后，除了「落地为 cancelled」别的写都不生效
3. 失败分类：永久错误不重试，暂时错误按退避重试并写 outbox 意图
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest

from app.application import job_execution_service as execution
from app.domain.models import Job, JobOutboxEvent


@pytest.fixture
def factory(client):
    return client.app.state.testing_session_factory


def _make_job(factory, workspace_id, **overrides) -> str:
    with factory() as db:
        job = Job(
            workspace_id=workspace_id,
            job_type="image_generation",
            status=overrides.pop("status", "queued"),
            **overrides,
        )
        db.add(job)
        db.commit()
        return job.id


@pytest.fixture
def workspace_id(client):
    return client.post("/api/v1/workspaces", json={"name": "执行状态机测试"}).json()["id"]


def test_claim_job_moves_queued_to_running_and_returns_job(factory, workspace_id):
    job_id = _make_job(factory, workspace_id)

    with factory() as db:
        job = execution.claim_job(db, job_id, "token-a")

    assert job is not None
    assert job.status == "running"
    assert job.execution_token == "token-a"
    assert job.attempt_count == 1


def test_second_claim_on_same_job_returns_none(factory, workspace_id):
    """两个 worker 同时拿到同一条消息时，只有一个能认领成功。"""
    job_id = _make_job(factory, workspace_id)

    with factory() as db:
        assert execution.claim_job(db, job_id, "token-a") is not None
    with factory() as db:
        assert execution.claim_job(db, job_id, "token-b") is None

    with factory() as db:
        assert db.get(Job, job_id).execution_token == "token-a"


def test_claim_refused_when_attempts_exhausted(factory, workspace_id):
    job_id = _make_job(factory, workspace_id, attempt_count=3, max_attempts=3)

    with factory() as db:
        assert execution.claim_job(db, job_id, "token-a") is None


def test_update_job_rejects_stale_token(factory, workspace_id):
    """执行权被别人拿走后再写状态，必须被拒绝，否则两方会互相覆盖进度。"""
    job_id = _make_job(factory, workspace_id)
    with factory() as db:
        execution.claim_job(db, job_id, "token-a")

    with factory() as db:
        wrote = execution.update_job(db, job_id, "wrong-token", progress=50, message="不该写进去")

    assert wrote is False
    with factory() as db:
        job = db.get(Job, job_id)
        assert job.progress != 50
        assert job.message != "不该写进去"


def test_update_job_respects_cancel_request(factory, workspace_id):
    """已请求取消的任务，不能再被别人改回 running 或改进度。"""
    job_id = _make_job(factory, workspace_id, status="cancel_requested")

    with factory() as db:
        wrote = execution.update_job(db, job_id, None, status="running", progress=80)

    assert wrote is False
    with factory() as db:
        assert db.get(Job, job_id).status == "cancel_requested"


def test_finish_cancel_request_settles_status(factory, workspace_id):
    job_id = _make_job(factory, workspace_id, status="cancel_requested")

    with factory() as db:
        assert execution.finish_cancel_request(db, job_id) is True

    with factory() as db:
        job = db.get(Job, job_id)
        assert job.status == "cancelled"
        assert job.message == "任务已取消"


def test_finish_cancel_request_ignores_running_job(factory, workspace_id):
    job_id = _make_job(factory, workspace_id, status="running")

    with factory() as db:
        assert execution.finish_cancel_request(db, job_id) is False


def test_owns_execution_accepts_cancel_requested(factory, workspace_id):
    """取消要等当前这张图跑完，所以 cancel_requested 期间仍算持有执行权。"""
    job_id = _make_job(factory, workspace_id)
    with factory() as db:
        execution.claim_job(db, job_id, "token-a")
        db.get(Job, job_id).status = "cancel_requested"
        db.commit()

    with factory() as db:
        assert execution.owns_execution(db, job_id, "token-a") is True
        assert execution.owns_execution(db, job_id, "token-b") is False


def test_permanent_error_goes_straight_to_dead_letter(factory, workspace_id):
    """配置类错误重试没意义，直接死信，不要浪费重试次数。"""
    job_id = _make_job(factory, workspace_id)
    with factory() as db:
        execution.claim_job(db, job_id, "token-a")

    with factory() as db:
        state = execution.retry_or_dead_letter(db, job_id, "token-a", ValueError("图片任务没有找到指定分镜"))

    assert state == "dead_letter"
    with factory() as db:
        job = db.get(Job, job_id)
        assert job.status == "dead_letter"
        assert job.execution_token is None
        assert "没有找到指定分镜" in job.error


def test_transient_error_requeues_with_backoff_and_outbox_intent(factory, workspace_id):
    """暂时性错误应重新排队，并且必须在同一事务里写 outbox 意图，否则任务会丢。"""
    job_id = _make_job(factory, workspace_id)
    with factory() as db:
        execution.claim_job(db, job_id, "token-a")

    with factory() as db:
        state = execution.retry_or_dead_letter(db, job_id, "token-a", RuntimeError("连接超时"))

    assert state == "retry"
    with factory() as db:
        job = db.get(Job, job_id)
        assert job.status == "queued"
        assert job.progress == 0
        assert job.execution_token is None
        events = db.query(JobOutboxEvent).filter(JobOutboxEvent.job_id == job_id).all()
        assert len(events) == 1
        assert events[0].status == "pending"
        # 退避时间必须落在未来，否则扫描器会立刻重投、变成忙等。
        # SQLite 读回来是 naive，用 as_utc 统一后再比。
        assert execution.as_utc(events[0].next_attempt_at) > datetime.now(timezone.utc)


def test_transient_error_dead_letters_after_attempts_exhausted(factory, workspace_id):
    """次数用尽后即使是暂时性错误也要转死信，不能无限重试下去。

    构造：max_attempts=2、还差一次。claim 会把 attempt_count 加到 2，
    此时再失败就已经用尽。
    """
    job_id = _make_job(factory, workspace_id, attempt_count=1, max_attempts=2)

    with factory() as db:
        assert execution.claim_job(db, job_id, "token-a") is not None
    with factory() as db:
        assert db.get(Job, job_id).attempt_count == 2

    with factory() as db:
        state = execution.retry_or_dead_letter(db, job_id, "token-a", RuntimeError("超时"))

    assert state == "dead_letter"
    with factory() as db:
        assert db.get(Job, job_id).status == "dead_letter"


def test_retry_is_ignored_when_token_lost(factory, workspace_id):
    """执行权已经不在自己手上时，不能替别人决定重试还是死信。"""
    job_id = _make_job(factory, workspace_id)
    with factory() as db:
        execution.claim_job(db, job_id, "token-a")

    with factory() as db:
        state = execution.retry_or_dead_letter(db, job_id, "stale-token", RuntimeError("超时"))

    assert state == "running"
    with factory() as db:
        assert db.get(Job, job_id).status == "running"


def test_mark_completed_requires_matching_token(factory, workspace_id):
    job_id = _make_job(factory, workspace_id)
    with factory() as db:
        execution.claim_job(db, job_id, "token-a")

    with factory() as db:
        assert execution.mark_completed(db, job_id, {"ok": 1}, "token-a", resume_workflow=False) is True
    with factory() as db:
        job = db.get(Job, job_id)
        assert job.status == "completed"
        assert job.progress == 100
        assert job.workflow_resume_status == "not_required"


def test_is_stale_uses_heartbeat_then_updated_at():
    now = datetime.now(timezone.utc)
    fresh = now - timedelta(seconds=5)
    stale = now - timedelta(days=1)

    assert execution.is_stale(fresh, None, now) is False
    assert execution.is_stale(stale, fresh, now) is True
    # 没有心跳时退回看 updated_at
    assert execution.is_stale(None, stale, now) is True
    assert execution.is_stale(None, fresh, now) is False


def test_start_heartbeat_refreshes_then_exits_when_lease_lost(factory, workspace_id):
    """心跳线程必须真的跑起来，并且发现令牌易主后自己退出。

    这条测试存在的理由：下沉到 application 之后曾经出现过「实现写好了、
    worker 却还在用自己那套循环」，结果 start_heartbeat 变成没人调的死代码。
    用 0.01 秒的间隔把它真跑一遍，钉住「有人调、且行为正确」。
    """
    job_id = _make_job(
        factory,
        workspace_id,
        status="running",
        attempt_count=1,
        max_attempts=3,
        execution_token="worker-a",
    )
    with factory() as db:
        db.get(Job, job_id).heartbeat_at = datetime.now(timezone.utc) - timedelta(hours=1)
        db.commit()

    stop = execution.start_heartbeat(factory, job_id, "worker-a", interval=0.01)
    try:
        deadline = datetime.now(timezone.utc) + timedelta(seconds=5)
        refreshed = False
        while datetime.now(timezone.utc) < deadline:
            with factory() as db:
                heartbeat_at = db.get(Job, job_id).heartbeat_at
            if execution.is_stale(
                heartbeat_at,
                None,
                datetime.now(timezone.utc),
            ) is False:
                refreshed = True
                break
            threading.Event().wait(0.02)
        assert refreshed, "心跳线程没有刷新 heartbeat_at"
    finally:
        stop.set()

    with factory() as db:
        job = db.get(Job, job_id)
        job.execution_token = "worker-b"
        job.heartbeat_at = datetime.now(timezone.utc) - timedelta(hours=1)
        db.commit()
        before = job.heartbeat_at

    stop = execution.start_heartbeat(factory, job_id, "worker-a", interval=0.01)
    try:
        threading.Event().wait(0.15)
    finally:
        stop.set()

    with factory() as db:
        after = db.get(Job, job_id).heartbeat_at
    assert execution.as_utc(after) == execution.as_utc(before), "令牌易主后心跳不应该再写库"


def test_run_job_actually_starts_heartbeat(factory, workspace_id, monkeypatch):
    """钉住「worker 真的调了下沉的 start_heartbeat」。

    这条测试来自一次真实事故：start_heartbeat 实现写好了，但 worker 还在用
    自己那套循环，函数于是变成死代码——功能照旧正常，只是下沉白做了。
    只断言「run_job 会把心跳拉起来」，不关心图怎么生成。
    """
    from app.workers import local_tasks

    job_id = _make_job(factory, workspace_id, status="queued", payload={"finalText": "x"})
    monkeypatch.setattr(local_tasks, "SessionLocal", factory)
    started: list = []

    def fake_start_heartbeat(db_factory, job_id, token, interval=None):
        started.append((job_id, token, db_factory))
        return threading.Event()

    monkeypatch.setattr(local_tasks.execution, "start_heartbeat", fake_start_heartbeat)
    # 让任务在认领后立刻以「暂时失败」退出，不必真去生成图片
    monkeypatch.setattr(
        local_tasks,
        "create_storyboard",
        lambda text: (_ for _ in ()).throw(RuntimeError("temporary provider failure")),
    )

    local_tasks.run_job(job_id)

    assert len(started) == 1, "run_job 没有启动心跳线程"
    assert started[0][0] == job_id
    assert started[0][2] is factory, "心跳必须用 worker 注入的 SessionLocal"
