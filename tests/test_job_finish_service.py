"""任务收尾状态机的规则测试。

收尾说的是「任务跑完之后」那段逻辑：写结果消息、决定要不要回灌对话图、
以及抢收尾权时怎么避免重复执行。它以前埋在 workers/local_tasks.py 的
_finish_job_side_effects 里（约 70 行），只能靠跑完整任务间接覆盖。

重点钉住四件事：
1. 幂等：同一任务收尾两次，只写一条结果消息
2. 租约：`processing` 且没超时 = 别人在做，不能重入
3. 抢回：`processing` 且已超时 = 上次的进程死了，允许重做
4. 失败回滚：回灌对话图抛异常时退回 pending，下一轮能再捡起来
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.application import job_finish_service as finish
from app.domain.models import AgentAction, AgentMessage, Job


@pytest.fixture
def factory(client):
    return client.app.state.testing_session_factory


@pytest.fixture
def workspace_id(client):
    return client.post("/api/v1/workspaces", json={"name": "收尾状态机测试"}).json()["id"]


def _make_job(factory, workspace_id, **overrides) -> str:
    with factory() as db:
        job = Job(
            workspace_id=workspace_id,
            job_type=overrides.pop("job_type", "video_generation"),
            status=overrides.pop("status", "completed"),
            thread_id=overrides.pop("thread_id", "thread-1"),
            payload=overrides.pop("payload", {}),
            result=overrides.pop("result", {"assets": [], "storyboard": []}),
            **overrides,
        )
        db.add(job)
        db.commit()
        return job.id


def _add_agent_action(factory, job_id: str) -> None:
    """结果消息只写给「由 agent 动作发起」的任务，普通任务没有这条记录。"""
    with factory() as db:
        job = db.get(Job, job_id)
        db.add(
            AgentAction(
                thread_id=job.thread_id,
                action_type="image_generation",
                status="completed",
                job_id=job_id,
            )
        )
        db.commit()


def _result_message_count(factory) -> int:
    with factory() as db:
        return db.query(AgentMessage).filter(
            AgentMessage.message_type == "job_result",
            AgentMessage.thread_id == "thread-1",
        ).count()


def test_finish_is_idempotent_for_non_agent_jobs(factory, workspace_id):
    job_id = _make_job(factory, workspace_id, workflow_resume_status="pending")
    _add_agent_action(factory, job_id)
    resumed: list = []

    finish.finish_side_effects(
        factory, job_id, resume_image_flow=lambda *args: resumed.append(args)
    )
    finish.finish_side_effects(
        factory, job_id, resume_image_flow=lambda *args: resumed.append(args)
    )

    with factory() as db:
        job = db.get(Job, job_id)
        assert job.workflow_resume_status == "completed"
        assert job.result_message_id is not None
    # 第二次进来时已经是 completed，直接在入口返回，不会再写一条
    assert _result_message_count(factory) == 1
    assert resumed == [], "非 agent 任务不需要回灌对话图"


def test_finish_skips_job_already_marked_not_required(factory, workspace_id):
    job_id = _make_job(factory, workspace_id, workflow_resume_status="not_required")

    finish.finish_side_effects(factory, job_id, resume_image_flow=lambda *args: None)

    with factory() as db:
        job = db.get(Job, job_id)
        assert job.result_message_id is None


def test_finish_without_thread_marks_not_required(factory, workspace_id):
    job_id = _make_job(factory, workspace_id, thread_id=None, workflow_resume_status="pending")

    finish.finish_side_effects(factory, job_id, resume_image_flow=lambda *args: None)

    with factory() as db:
        job = db.get(Job, job_id)
        assert job.workflow_resume_status == "not_required"
        assert job.result_message_id is None


def test_finish_resumes_agent_graph_with_assets(factory, workspace_id):
    job_id = _make_job(
        factory,
        workspace_id,
        job_type="image_generation",
        payload={"agentGraph": True},
        result={"assets": [{"id": "a1"}]},
        workflow_resume_status="pending",
    )
    resumed: list = []

    finish.finish_side_effects(
        factory, job_id, resume_image_flow=lambda *args: resumed.append(args)
    )

    with factory() as db:
        job = db.get(Job, job_id)
        assert job.workflow_resume_status == "completed"
        assert job.workflow_resume_started_at is None
    assert resumed == [("thread-1", job_id, [{"id": "a1"}], "")]


def test_finish_passes_error_when_job_failed(factory, workspace_id):
    job_id = _make_job(
        factory,
        workspace_id,
        job_type="image_generation",
        status="failed",
        payload={"agentGraph": True},
        result={"assets": [{"id": "a1"}]},
        error="模型超时",
        workflow_resume_status="pending",
    )
    resumed: list = []

    finish.finish_side_effects(
        factory, job_id, resume_image_flow=lambda *args: resumed.append(args)
    )

    # 失败的任务不该把 asset 灌回对话图，但要把失败原因带过去
    assert resumed == [("thread-1", job_id, [], "模型超时")]


def test_finish_does_not_reenter_while_processing_and_fresh(factory, workspace_id):
    job_id = _make_job(
        factory,
        workspace_id,
        job_type="image_generation",
        payload={"agentGraph": True},
        workflow_resume_status="processing",
        workflow_resume_started_at=datetime.now(timezone.utc),
    )
    resumed: list = []

    finish.finish_side_effects(
        factory, job_id, resume_image_flow=lambda *args: resumed.append(args)
    )

    with factory() as db:
        job = db.get(Job, job_id)
        assert job.workflow_resume_status == "processing"
        assert job.result_message_id is None
    assert resumed == []


def test_finish_reclaims_stale_processing_job(factory, workspace_id):
    job_id = _make_job(
        factory,
        workspace_id,
        job_type="image_generation",
        payload={"agentGraph": True},
        workflow_resume_status="processing",
        workflow_resume_started_at=datetime.now(timezone.utc) - timedelta(days=1),
    )
    resumed: list = []

    finish.finish_side_effects(
        factory, job_id, resume_image_flow=lambda *args: resumed.append(args)
    )

    with factory() as db:
        job = db.get(Job, job_id)
        assert job.workflow_resume_status == "completed"
    assert len(resumed) == 1, "过期的 processing 应该被抢回来重做"


def test_finish_rolls_back_to_pending_when_resume_raises(factory, workspace_id):
    job_id = _make_job(
        factory,
        workspace_id,
        job_type="image_generation",
        payload={"agentGraph": True},
        workflow_resume_status="pending",
    )
    _add_agent_action(factory, job_id)

    def boom(*args):
        raise RuntimeError("graph lease busy")

    with pytest.raises(RuntimeError):
        finish.finish_side_effects(factory, job_id, resume_image_flow=boom)

    with factory() as db:
        job = db.get(Job, job_id)
        # 退回 pending，恢复扫描下一轮还能捡起来
        assert job.workflow_resume_status == "pending"
        assert "graph lease busy" in job.workflow_resume_error
        # 结果消息已经写过了，不能因为回灌失败就丢掉
        assert job.result_message_id is not None
