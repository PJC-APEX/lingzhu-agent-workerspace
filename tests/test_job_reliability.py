from datetime import datetime, timedelta, timezone

from app.domain.models import Job, JobOutboxEvent


def test_job_and_outbox_intent_are_written_together_by_workflow_node(client, monkeypatch):
    from app.graph import nodes
    from app.graph.state import AgentGraphState

    monkeypatch.setattr(nodes, "get_session_factory", lambda: client.app.state.testing_session_factory)
    monkeypatch.setattr(nodes, "launch_job", lambda job_id: job_id)
    workspace = client.post("/api/v1/workspaces", json={"name": "outbox atomic test"}).json()
    thread = client.post("/api/v1/agent/threads", json={"workspaceId": workspace["id"]}).json()
    state: AgentGraphState = {
        "thread_id": thread["id"],
        "workspace_id": workspace["id"],
        "storyboard": [{"sceneNumber": 1, "narration": "一幕。", "imagePrompt": "清晨"}],
        "regen_scene_numbers": [],
        "image_style_prompt": "",
    }

    update = nodes.launch_images_node(state)

    with client.app.state.testing_session_factory() as db:
        job = db.get(Job, update["image_job_id"])
        event = db.query(JobOutboxEvent).filter(JobOutboxEvent.job_id == job.id).one()
        assert job.status == "queued"
        assert event.status == "pending"
        assert event.generation == 0


def test_outbox_publish_failure_stays_durable_and_can_be_retried(client, monkeypatch):
    from app.workers import local_tasks

    monkeypatch.setattr(local_tasks, "SessionLocal", client.app.state.testing_session_factory)
    workspace = client.post("/api/v1/workspaces", json={"name": "publish retry"}).json()
    factory = client.app.state.testing_session_factory
    with factory() as db:
        job = Job(workspace_id=workspace["id"], job_type="video_generation", status="queued")
        db.add(job)
        db.flush()
        event = JobOutboxEvent(job_id=job.id, generation=0, next_attempt_at=datetime.now(timezone.utc) - timedelta(seconds=1))
        db.add(event)
        db.commit()
        job_id = job.id
        event_id = event.id

    calls = []
    monkeypatch.setattr(local_tasks.process_media_job, "apply_async", lambda **kwargs: (_ for _ in ()).throw(ConnectionError("redis offline")))
    assert local_tasks._dispatch_outbox_event(event_id) is False
    with factory() as db:
        event = db.get(JobOutboxEvent, event_id)
        job = db.get(Job, job_id)
        assert event.status == "pending"
        assert event.attempts == 1
        assert event.last_error == "redis offline"
        assert job.status == "queued"

    monkeypatch.setattr(local_tasks.process_media_job, "apply_async", lambda **kwargs: calls.append(kwargs) or type("Result", (), {"id": kwargs["task_id"]})())
    with factory() as db:
        db.get(JobOutboxEvent, event_id).next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
    assert local_tasks._dispatch_outbox_event(event_id) is True
    assert calls[0]["args"] == [job_id]
    with factory() as db:
        assert db.get(JobOutboxEvent, event_id).status == "published"
        assert db.get(Job, job_id).last_enqueued_at is not None


def test_stale_running_job_is_requeued_from_database_state(client, monkeypatch):
    from app.workers import local_tasks

    factory = client.app.state.testing_session_factory
    monkeypatch.setattr(local_tasks, "SessionLocal", factory)
    workspace = client.post("/api/v1/workspaces", json={"name": "stale recovery"}).json()
    with factory() as db:
        job = Job(
            workspace_id=workspace["id"],
            job_type="video_generation",
            status="running",
            attempt_count=1,
            max_attempts=3,
            heartbeat_at=datetime.now(timezone.utc) - timedelta(hours=1),
            execution_token="lost-worker-token",
        )
        db.add(job)
        db.commit()
        job_id = job.id

    monkeypatch.setattr(local_tasks, "_dispatch_pending_outbox", lambda limit=100: 0)
    result = local_tasks.recover_media_jobs()

    with factory() as db:
        job = db.get(Job, job_id)
        event = db.query(JobOutboxEvent).filter(JobOutboxEvent.job_id == job_id).one()
        assert job.status == "queued"
        assert job.execution_token is None
        assert event.generation == 1
        assert result["recovered"] == 1


def test_transient_worker_failure_retries_then_dead_letters_after_limit(client, monkeypatch):
    from app.workers import local_tasks

    factory = client.app.state.testing_session_factory
    monkeypatch.setattr(local_tasks, "SessionLocal", factory)
    monkeypatch.setattr(local_tasks, "create_storyboard", lambda text: (_ for _ in ()).throw(RuntimeError("temporary provider failure")))
    workspace = client.post("/api/v1/workspaces", json={"name": "worker retry"}).json()
    with factory() as db:
        job = Job(
            workspace_id=workspace["id"],
            job_type="image_generation",
            status="queued",
            max_attempts=2,
            payload={"finalText": "test"},
        )
        db.add(job)
        db.commit()
        job_id = job.id

    local_tasks.run_job(job_id)
    with factory() as db:
        job = db.get(Job, job_id)
        assert job.status == "queued"
        assert job.attempt_count == 1
        assert db.query(JobOutboxEvent).filter(JobOutboxEvent.job_id == job_id).count() == 1
        job.last_enqueued_at = None
        event = db.query(JobOutboxEvent).filter(JobOutboxEvent.job_id == job_id).one()
        event.next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()

    local_tasks.run_job(job_id)
    with factory() as db:
        job = db.get(Job, job_id)
        assert job.status == "dead_letter"
        assert job.attempt_count == 2
        assert "temporary provider failure" in job.error


def test_dead_letter_job_can_be_manually_requeued(client, monkeypatch):
    from app.api.v1 import jobs

    monkeypatch.setattr(jobs, "launch_job", lambda job_id: job_id)
    workspace = client.post("/api/v1/workspaces", json={"name": "manual retry"}).json()
    factory = client.app.state.testing_session_factory
    with factory() as db:
        job = Job(
            workspace_id=workspace["id"],
            job_type="video_generation",
            status="dead_letter",
            attempt_count=5,
            max_attempts=5,
            error="temporary error",
        )
        db.add(job)
        db.commit()
        job_id = job.id

    response = client.post(f"/api/v1/jobs/{job_id}/retry")

    assert response.status_code == 200
    with factory() as db:
        job = db.get(Job, job_id)
        event = db.query(JobOutboxEvent).filter(JobOutboxEvent.job_id == job_id).one()
        assert job.status == "queued"
        assert job.attempt_count == 0
        assert job.error is None
        assert event.status == "pending"


def test_dead_lettered_job_leaves_no_pending_outbox_intent(client, monkeypatch):
    """任务升级成死信时，必须把它残留的待投递意图一起清掉。

    场景：worker 跑到一半挂了，心跳超时被打回 queued 并写下一条 outbox
    意图；但那条意图还没被投出去，任务又被判定为次数用尽而升级成
    dead_letter。如果此时不清掉 intent，扫描器照样会把 dead_letter 的
    任务投进队列——worker 认领时因为状态不是 queued 直接跳过，
    队列里就多出一条永远执行不了的消息，用户也看不到任何解释。
    """
    from app.workers import local_tasks

    factory = client.app.state.testing_session_factory
    monkeypatch.setattr(local_tasks, "SessionLocal", factory)
    workspace = client.post("/api/v1/workspaces", json={"name": "dead letter outbox"}).json()
    with factory() as db:
        job = Job(
            workspace_id=workspace["id"],
            job_type="video_generation",
            status="running",
            attempt_count=3,
            max_attempts=3,
            heartbeat_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        db.add(job)
        db.flush()
        db.add(JobOutboxEvent(job_id=job.id, generation=2, status="pending"))
        db.commit()
        job_id = job.id

    monkeypatch.setattr(local_tasks, "_dispatch_pending_outbox", lambda limit=100: 0)
    result = local_tasks.recover_media_jobs()

    with factory() as db:
        job = db.get(Job, job_id)
        events = db.query(JobOutboxEvent).filter(JobOutboxEvent.job_id == job_id).all()
        assert job.status == "dead_letter"
        assert result["deadLettered"] == 1
        assert [e.status for e in events] == ["cancelled"], (
            "死信任务的待投递意图必须被标记为取消，否则会被投进队列空转"
        )
