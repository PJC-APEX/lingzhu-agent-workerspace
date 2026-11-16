"""恢复扫描的业务规则测试。

以前这段逻辑只能用 recover_media_jobs() 整体跑，且必须打桩 SessionLocal
才能用上测试库。搬到 application/job_recovery_service.py 之后可以传会话、
传 now、传投递函数，规则本身能单独钉住。

重点：
1. 超时的 running 任务：次数没用尽就打回 queued，用尽了就死信
2. 死信时必须清掉遗留的待投递意图（修的就是这个：以前留着 pending，
   扫描器会把 dead_letter 的任务投进队列，worker 认领时因为状态不是
   queued 直接跳过，队列里多一条永远执行不了的消息）
3. 还在冷却期的 queued 任务不重复写意图
4. 已经有意向的 queued 任务不重复写
5. 时间判定统一走 is_stale，不在这里重写比较条件
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.application import job_recovery_service as recovery
from app.domain.models import Job, JobOutboxEvent


@pytest.fixture
def factory(client):
    return client.app.state.testing_session_factory


@pytest.fixture
def workspace_id(client):
    return client.post("/api/v1/workspaces", json={"name": "恢复扫描测试"}).json()["id"]


def _record(calls):
    def dispatch(db, job, *args):
        calls.append((job.id, args[0] if args else None))

    return dispatch


def _job(factory, workspace_id, **kwargs):
    with factory() as db:
        job = Job(workspace_id=workspace_id, job_type="video_generation", **kwargs)
        db.add(job)
        db.commit()
        return job.id


def _stale_now():
    """统一用「一小时前」当 now，任务就都是超时的。"""
    return datetime.now(timezone.utc)


def test_stale_running_job_is_requeued_when_attempts_remain(factory, workspace_id):
    job_id = _job(
        factory, workspace_id,
        status="running", attempt_count=1, max_attempts=3,
        heartbeat_at=datetime.now(timezone.utc) - timedelta(hours=1),
        execution_token="lost",
    )
    calls = []
    with factory() as db:
        result = recovery.scan_and_recover(
            db, stale_seconds=60, record_dispatch=_record(calls)
        )
        db.commit()

    with factory() as db:
        job = db.get(Job, job_id)
    assert job.status == "queued"
    assert job.execution_token is None
    assert job.heartbeat_at is None
    assert "心跳超时" in job.error
    assert result.recovered == 1
    assert calls == [(job_id, 1)], "恢复时必须用当前 attempt_count 作为 generation"


def test_stale_running_job_dead_letters_when_attempts_exhausted(factory, workspace_id):
    job_id = _job(
        factory, workspace_id,
        status="running", attempt_count=3, max_attempts=3,
        heartbeat_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    calls = []
    with factory() as db:
        result = recovery.scan_and_recover(db, stale_seconds=60, record_dispatch=_record(calls))
        db.commit()

    with factory() as db:
        job = db.get(Job, job_id)
    assert job.status == "dead_letter"
    assert job.message == "任务重试次数已用尽"
    assert result.dead_lettered == 1
    assert calls == [], "死信任务不该再写投递意图"


def test_dead_letter_cancels_leftover_outbox_intents(factory, workspace_id):
    """回归：死信时残留的 pending 意图必须被取消。

    以前只改内存里的 job.status，那条 pending 意图原封不动，
    下一轮扫描就会把 dead_letter 的任务投进队列空转。
    """
    with factory() as db:
        job = Job(
            workspace_id=workspace_id, job_type="video_generation",
            status="running", attempt_count=3, max_attempts=3,
            heartbeat_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        db.add(job)
        db.flush()
        db.add(JobOutboxEvent(job_id=job.id, generation=2, status="pending"))
        db.commit()
        job_id = job.id

    with factory() as db:
        recovery.scan_and_recover(db, stale_seconds=60, record_dispatch=_record([]))
        db.commit()

    with factory() as db:
        events = db.query(JobOutboxEvent).filter(JobOutboxEvent.job_id == job_id).all()
    assert [e.status for e in events] == ["cancelled"]


def test_queued_job_within_cooldown_is_not_redispatched(factory, workspace_id):
    job_id = _job(
        factory, workspace_id,
        status="queued", attempt_count=0, max_attempts=3,
        last_enqueued_at=datetime.now(timezone.utc) - timedelta(seconds=5),
    )
    calls = []
    with factory() as db:
        recovery.scan_and_recover(db, stale_seconds=600, record_dispatch=_record(calls))
        db.commit()
    assert calls == [], "刚投过的任务不该重复投，否则会连环重复执行"


def test_queued_job_with_open_intent_is_not_redispatched(factory, workspace_id):
    with factory() as db:
        job = Job(workspace_id=workspace_id, job_type="video_generation", status="queued", attempt_count=0, max_attempts=3)
        db.add(job)
        db.flush()
        db.add(JobOutboxEvent(job_id=job.id, generation=0, status="pending"))
        db.commit()

    calls = []
    with factory() as db:
        recovery.scan_and_recover(db, stale_seconds=60, record_dispatch=_record(calls))
        db.commit()
    assert calls == [], "已经有意向在等投递，不该再补一条"


def test_queued_job_without_intent_gets_next_generation(factory, workspace_id):
    with factory() as db:
        job = Job(workspace_id=workspace_id, job_type="video_generation", status="queued", attempt_count=1, max_attempts=3)
        db.add(job)
        db.flush()
        db.add(JobOutboxEvent(job_id=job.id, generation=4, status="published"))
        db.commit()
        job_id = job.id

    calls = []
    with factory() as db:
        recovery.scan_and_recover(db, stale_seconds=60, record_dispatch=_record(calls))
        db.commit()
    assert calls == [(job_id, 5)], "generation 应接在已用过的最大值之后，避免复用被消费过的事件"


def test_queued_job_exhausted_dead_letters_without_dispatch(factory, workspace_id):
    job_id = _job(
        factory, workspace_id,
        status="queued", attempt_count=3, max_attempts=3,
    )
    calls = []
    with factory() as db:
        result = recovery.scan_and_recover(db, stale_seconds=60, record_dispatch=_record(calls))
        db.commit()

    with factory() as db:
        job = db.get(Job, job_id)
    assert job.status == "dead_letter"
    assert job.error == "任务达到最大尝试次数"
    assert result.dead_lettered == 1
    assert calls == []


def test_healthy_running_job_is_left_alone(factory, workspace_id):
    job_id = _job(
        factory, workspace_id,
        status="running", attempt_count=1, max_attempts=3,
        heartbeat_at=datetime.now(timezone.utc),
        execution_token="alive",
    )
    with factory() as db:
        result = recovery.scan_and_recover(db, stale_seconds=600, record_dispatch=_record([]))
        db.commit()

    with factory() as db:
        job = db.get(Job, job_id)
    assert job.status == "running"
    assert job.execution_token == "alive"
    assert result.recovered == 0


def test_pending_resumes_lists_image_jobs_only(factory, workspace_id):
    with factory() as db:
        image_job = Job(
            workspace_id=workspace_id, job_type="image_generation", status="completed",
            workflow_resume_status="pending",
        )
        video_job = Job(
            workspace_id=workspace_id, job_type="video_generation", status="completed",
            workflow_resume_status="pending",
        )
        db.add_all([image_job, video_job])
        db.commit()
        image_id, video_id = image_job.id, video_job.id

    with factory() as db:
        result = recovery.scan_and_recover(db, stale_seconds=60, record_dispatch=_record([]))
        db.commit()

    assert image_id in result.pending_resumes
    assert video_id not in result.pending_resumes


def test_queued_job_dead_letter_also_cancels_outbox_intents(factory, workspace_id):
    """从 queued 直接判死信时，同样要清掉遗留意图。

    queued 任务可能留着一条早先写下但一直没投出去的 pending 意图，
    或者一条 publishing 状态、锁已过期的。两种情况都得取消，
    否则扫描器还会把它投进队列。
    """
    with factory() as db:
        job = Job(
            workspace_id=workspace_id, job_type="video_generation",
            status="queued", attempt_count=3, max_attempts=3,
        )
        db.add(job)
        db.flush()
        db.add(JobOutboxEvent(job_id=job.id, generation=1, status="pending"))
        db.commit()
        job_id = job.id

    with factory() as db:
        recovery.scan_and_recover(db, stale_seconds=60, record_dispatch=_record([]))
        db.commit()

    with factory() as db:
        job = db.get(Job, job_id)
        events = db.query(JobOutboxEvent).filter(JobOutboxEvent.job_id == job_id).all()
    assert job.status == "dead_letter"
    assert [e.status for e in events] == ["cancelled"]
