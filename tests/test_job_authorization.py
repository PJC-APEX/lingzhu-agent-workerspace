"""任务接口的跨用户越权防护。

背景：`routes.py` 里原来的四个任务端点是先 `_get_or_404(db, Job, ...)`
再操作，而 `_get_or_404` 内部会走 `_belongs_to_user` 校验任务所属工作区的
owner。2B 把业务下沉到 `job_service` 时如果只做 `db.get(Job, id)`，
归属校验就会丢失——任何已登录用户只要拿到 job_id，就能读、取消、重试
别人的任务。这组测试把这条线钉住。

注意：这里断言的是「别人的任务一律 404」，而不是 403。
404 不区分「不存在」和「不属于你」，避免通过状态码探测 id 是否存在。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.domain.models import Job
from app.main import app


@pytest.fixture
def owner_and_stranger(client):
    """返回 (任务 id, 属主的 client, 另一个用户的 client)。"""
    factory = client.app.state.testing_session_factory
    workspace = client.post("/api/v1/workspaces", json={"name": "属主的工作区"}).json()
    with factory() as db:
        job = Job(workspace_id=workspace["id"], job_type="video_generation", status="dead_letter")
        db.add(job)
        db.commit()
        job_id = job.id

    stranger = TestClient(app)
    response = stranger.post(
        "/api/v1/auth/register",
        json={"email": "stranger@example.com", "password": "stranger-password-123"},
    )
    assert response.status_code in (200, 201), response.text
    return job_id, client, stranger, workspace["id"]


def test_stranger_cannot_read_others_job(owner_and_stranger):
    job_id, _owner, stranger, _workspace_id = owner_and_stranger

    assert stranger.get(f"/api/v1/jobs/{job_id}").status_code == 404


def test_stranger_cannot_cancel_others_job(owner_and_stranger):
    job_id, _owner, stranger, _workspace_id = owner_and_stranger

    assert stranger.post(f"/api/v1/jobs/{job_id}/cancel").status_code == 404


def test_stranger_cannot_retry_others_job(owner_and_stranger):
    job_id, _owner, stranger, _workspace_id = owner_and_stranger

    assert stranger.post(f"/api/v1/jobs/{job_id}/retry").status_code == 404


def test_stranger_cannot_list_others_workspace_jobs(owner_and_stranger):
    _job_id, _owner, stranger, workspace_id = owner_and_stranger

    assert stranger.get(f"/api/v1/workspaces/{workspace_id}/jobs").status_code == 404


def test_owner_can_still_reach_own_job(owner_and_stranger):
    job_id, owner, _stranger, workspace_id = owner_and_stranger

    assert owner.get(f"/api/v1/jobs/{job_id}").status_code == 200
    assert owner.get(f"/api/v1/workspaces/{workspace_id}/jobs").status_code == 200


def test_retry_conflict_is_409_not_404(owner_and_stranger):
    """状态不允许时不应该是 404 —— 否则和「任务不存在」混淆。"""
    job_id, owner, _stranger, _workspace_id = owner_and_stranger
    factory = owner.app.state.testing_session_factory
    with factory() as db:
        db.get(Job, job_id).status = "completed"
        db.commit()

    response = owner.post(f"/api/v1/jobs/{job_id}/retry")

    assert response.status_code == 409
