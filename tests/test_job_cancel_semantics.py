"""任务取消时「收尾失败」的状态码语义。

取消是两段动作：先在库里把任务置为 cancelled，再唤醒对话流程。
如果第二段失败，任务其实已经取消成功了。这时返回 409（冲突）会让前端
以为整个操作没生效、于是重试一个已经取消掉的任务；返回 500 才符合事实：
动作生效了，但后续步骤出错。这条测试把这个区别钉住。
"""

from __future__ import annotations

from app.domain.models import Job


def test_cancel_resume_failure_returns_500_not_409(client, monkeypatch):
    from app.api.v1 import jobs

    factory = client.app.state.testing_session_factory
    workspace = client.post("/api/v1/workspaces", json={"name": "取消恢复失败"}).json()
    thread = client.post("/api/v1/agent/threads", json={"workspaceId": workspace["id"]}).json()
    with factory() as db:
        job = Job(
            workspace_id=workspace["id"],
            thread_id=thread["id"],
            job_type="image_generation",
            status="queued",
            payload={"agentGraph": True, "storyboard": [], "sceneNumbers": []},
        )
        db.add(job)
        db.commit()
        job_id = job.id

    def boom(*args, **kwargs):
        raise RuntimeError("graph resume exploded")

    monkeypatch.setattr(jobs, "_resume_agent_image_flow", boom)

    response = client.post(f"/api/v1/jobs/{job_id}/cancel")

    assert response.status_code == 500, response.text
    with factory() as db:
        # 关键：取消本身必须已经落库，不能被收尾失败回滚掉
        assert db.get(Job, job_id).status == "cancelled"
