"""确认卡片的业务规则测试。

卡片处理以前全部写在 routes.py 的 resolve_agent_action 里，一半是规则
（能不能处理、抢不到怎么办）一半是响应格式，缠在一起。

重点钉住：
1. 跨会话访问按「找不到」处理，不泄露存在性
2. 抢卡：并发只有一次生效，第二次按同一决定走幂等、不同决定走冲突
3. 素材审批：只有图片类确认才动素材状态，且必须同工作区
4. 任务工单：视频类必须先有字幕、图片批次必须就绪
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.application import agent_action_service as action_service
from app.domain.exceptions import ConflictError, NotFoundError, ValidationError
from app.domain.models import AgentAction, AgentMessage, AgentThread, Asset, Job, SubtitleFile, Workspace


@pytest.fixture
def factory(client):
    return client.app.state.testing_session_factory


@pytest.fixture
def workspace_id(client):
    return client.post("/api/v1/workspaces", json={"name": "卡片规则测试"}).json()["id"]


@pytest.fixture
def thread(factory, workspace_id):
    with factory() as db:
        row = AgentThread(workspace_id=workspace_id)
        db.add(row)
        db.commit()
        return SimpleNamespace(id=row.id, workspace_id=workspace_id)


@pytest.fixture
def make_action(factory, thread):
    def _make(**overrides):
        with factory() as db:
            action = AgentAction(
                thread_id=overrides.pop("thread_id", thread.id),
                action_type=overrides.pop("action_type", "generate_images"),
                status=overrides.pop("status", "awaiting_confirmation"),
                payload=overrides.pop("payload", {}),
                **overrides,
            )
            db.add(action)
            db.commit()
            return action.id

    return _make


def test_get_action_rejects_action_from_other_thread(factory, make_action, thread):
    action_id = make_action(thread_id="someone-elses-thread")
    with factory() as db:
        with pytest.raises(NotFoundError) as exc:
            action_service.get_action_for_thread(db, action_id, thread.id)
    assert exc.value.status_code == 404
    assert "该对话中的操作" in exc.value.message


def test_get_action_missing_is_not_found(factory, thread):
    with factory() as db:
        with pytest.raises(NotFoundError):
            action_service.get_action_for_thread(db, "no-such-action", thread.id)


def test_already_processed_same_decision_is_idempotent(make_action, factory):
    action_id = make_action(status="confirmed")
    with factory() as db:
        action = db.get(AgentAction, action_id)
        result = action_service.already_processed_response(action, "approve")
    assert result.status == "already_processed"


def test_already_processed_different_decision_conflicts(make_action, factory):
    action_id = make_action(status="confirmed")
    with factory() as db:
        action = db.get(AgentAction, action_id)
        with pytest.raises(ConflictError) as exc:
            action_service.already_processed_response(action, "reject")
    assert exc.value.status_code == 409


def test_reject_and_retry_preview_share_rejected_status(make_action, factory):
    action_id = make_action(status="rejected")
    with factory() as db:
        action = db.get(AgentAction, action_id)
        assert action_service.already_processed_response(action, "reject").status == "already_processed"
        assert action_service.already_processed_response(action, "retry_preview").status == "already_processed"


def test_claim_action_only_succeeds_once(factory, make_action, thread):
    action_id = make_action()
    with factory() as db:
        assert action_service.claim_action(db, action_id, thread.id, "approve") is True
        db.commit()  # claim_action 本身不提交，提交由调用方负责（与路由一致）
    with factory() as db:
        assert action_service.claim_action(db, action_id, thread.id, "reject") is False
    with factory() as db:
        assert db.get(AgentAction, action_id).status == "confirmed", "第二次抢失败不能改状态"


def test_claim_action_rejects_wrong_thread(factory, make_action, thread):
    action_id = make_action()
    with factory() as db:
        assert action_service.claim_action(db, action_id, "别的会话", "approve") is False


def test_validate_selected_preview_requires_index(make_action, factory):
    action_id = make_action(action_type="graph_confirm_image_style")
    with factory() as db:
        action = db.get(AgentAction, action_id)
        with pytest.raises(ValidationError) as exc:
            action_service.validate_selected_preview(action, "approve", None)
    assert "请先选择一张样图" in exc.value.message
    # 选了就不报
    action_service.validate_selected_preview(action, "approve", 1)
    # 拒绝时不需要选
    action_service.validate_selected_preview(action, "reject", None)


def test_resolve_retry_preview_style_ignored_for_other_decisions(make_action, factory):
    action_id = make_action(action_type="graph_confirm_image_style")
    with factory() as db:
        action = db.get(AgentAction, action_id)
        body = SimpleNamespace(decision="approve", stylePrompt="", imageStylePromptVersionId=None)
        assert action_service.resolve_retry_preview_style(db, action, body, "u1") is None


def test_resolve_retry_preview_style_requires_prompt(make_action, factory):
    action_id = make_action(action_type="graph_confirm_image_style")
    with factory() as db:
        action = db.get(AgentAction, action_id)
        body = SimpleNamespace(decision="retry_preview", stylePrompt="   ", imageStylePromptVersionId=None)
        with pytest.raises(ValidationError) as exc:
            action_service.resolve_retry_preview_style(db, action, body, "u1")
    assert "非空生图风格提示词" in exc.value.message


def test_apply_side_effects_approves_assets_in_same_workspace(factory, make_action, thread, workspace_id):
    other_ws = None
    with factory() as db:
        other_ws = Workspace(name="别处", owner_user_id="other")
        db.add(other_ws)
        db.flush()
        db.add_all([
            Asset(id="mine", workspace_id=workspace_id, job_id="j", file_name="m.png",
                  object_path="p", scene_number=1, status="needs_review"),
            Asset(id="theirs", workspace_id=other_ws.id, job_id="j2", file_name="t.png",
                  object_path="p", scene_number=1, status="needs_review"),
        ])
        db.commit()
        other_ws_id = other_ws.id

    action_id = make_action(
        action_type="graph_confirm_images",
        payload={"assetIds": ["mine", "theirs"]},
    )
    with factory() as db:
        action = db.get(AgentAction, action_id)
        action_service.apply_confirmation_side_effects(db, action, "approve")
        db.commit()
    with factory() as db:
        assert db.get(Asset, "mine").status == "approved"
        assert db.get(Asset, "theirs").status == "needs_review", "跨工作区素材不能被动"


def test_apply_side_effects_ignores_non_image_actions(factory, make_action):
    action_id = make_action(action_type="generate_video", payload={"assetIds": ["x"]})
    with factory() as db:
        action = db.get(AgentAction, action_id)
        action_service.apply_confirmation_side_effects(db, action, "approve")
        db.commit()
        assert db.get(Asset, "x") is None


def test_apply_side_effects_ignores_reject(factory, make_action, workspace_id):
    with factory() as db:
        db.add(Asset(id="keep", workspace_id=workspace_id, job_id="j", file_name="k.png",
                     object_path="p", scene_number=1, status="needs_review"))
        db.commit()
    action_id = make_action(action_type="graph_confirm_images", payload={"assetIds": ["keep"]})
    with factory() as db:
        action = db.get(AgentAction, action_id)
        action_service.apply_confirmation_side_effects(db, action, "reject")
        db.commit()
    with factory() as db:
        assert db.get(Asset, "keep").status == "needs_review", "拒绝时不该通过素材"


def test_image_batch_readiness_reports_missing_job(factory, thread):
    with factory() as db:
        job, error = action_service.image_batch_readiness(db, db.get(AgentThread, thread.id))
    assert job is None
    assert "找不到本次对话已完成" in error


def test_image_batch_readiness_reports_scene_mismatch(factory, thread, workspace_id):
    with factory() as db:
        db.add(Job(
            id="j-mismatch", workspace_id=workspace_id, thread_id=thread.id,
            job_type="image_generation", status="completed",
            result={"storyboard": [{"sceneNumber": 1}, {"sceneNumber": 2}]},
        ))
        db.add(Asset(id="a1", workspace_id=workspace_id, job_id="j-mismatch", file_name="a.png",
                     object_path="p", scene_number=1, status="approved"))
        db.commit()
    with factory() as db:
        job, error = action_service.image_batch_readiness(db, db.get(AgentThread, thread.id))
    assert job is None
    assert "一一对应" in error


def test_image_batch_readiness_reports_unapproved_assets(factory, thread, workspace_id):
    with factory() as db:
        db.add(Job(
            id="j-unapproved", workspace_id=workspace_id, thread_id=thread.id,
            job_type="image_generation", status="completed",
            result={"storyboard": [{"sceneNumber": 1}]},
        ))
        db.add(Asset(id="b1", workspace_id=workspace_id, job_id="j-unapproved", file_name="b.png",
                     object_path="p", scene_number=1, status="needs_review"))
        db.commit()
    with factory() as db:
        job, error = action_service.image_batch_readiness(db, db.get(AgentThread, thread.id))
    assert job is None
    assert "审核通过" in error


def test_image_batch_readiness_ready(factory, thread, workspace_id):
    with factory() as db:
        db.add(Job(
            id="j-ready", workspace_id=workspace_id, thread_id=thread.id,
            job_type="image_generation", status="completed",
            result={"storyboard": [{"sceneNumber": 1}]},
        ))
        db.add(Asset(id="c1", workspace_id=workspace_id, job_id="j-ready", file_name="c.png",
                     object_path="p", scene_number=1, status="approved"))
        db.commit()
    with factory() as db:
        job, error = action_service.image_batch_readiness(db, db.get(AgentThread, thread.id))
    assert error == ""
    assert job.id == "j-ready"


def test_prepare_action_job_for_images(factory, thread, make_action):
    action_id = make_action(action_type="generate_images")
    with factory() as db:
        job_type, message = action_service.prepare_action_job(
            db, db.get(AgentThread, thread.id), db.get(AgentAction, action_id), "u1"
        )
    assert job_type == "image_generation"
    assert "分镜和生图" in message


def test_prepare_action_job_for_video_requires_subtitle(factory, thread, make_action):
    action_id = make_action(action_type="generate_video", payload={"subtitleFileId": "missing"})
    with factory() as db:
        with pytest.raises(ConflictError) as exc:
            action_service.prepare_action_job(
                db, db.get(AgentThread, thread.id), db.get(AgentAction, action_id), "u1"
            )
    assert "字幕状态已变化" in exc.value.message


def test_prepare_action_job_for_video_requires_ready_images(factory, thread, make_action, workspace_id):
    with factory() as db:
        sub = SubtitleFile(workspace_id=workspace_id, file_name="a.srt",
                           object_path="a.srt", audio_object_path="a.mp3", cues=[])
        db.add(sub)
        db.commit()
        sub_id = sub.id
    action_id = make_action(action_type="generate_video", payload={"subtitleFileId": sub_id})
    with factory() as db:
        with pytest.raises(ConflictError) as exc:
            action_service.prepare_action_job(
                db, db.get(AgentThread, thread.id), db.get(AgentAction, action_id), "u1"
            )
    assert "找不到本次对话已完成" in exc.value.message


def test_prepare_action_job_rejects_unknown_type(factory, thread, make_action):
    action_id = make_action(action_type="teleport")
    with factory() as db:
        with pytest.raises(ValidationError) as exc:
            action_service.prepare_action_job(
                db, db.get(AgentThread, thread.id), db.get(AgentAction, action_id), "u1"
            )
    assert "暂不支持" in exc.value.message


# ---------------------------------------------------------------------------
# create_job_for_action：一个事务里建任务、写 outbox、关联卡片、落消息
#
# 这里刻意把四件事分开断言，而不是"只要不抛异常就算通过"。
# 一条宽泛的断言无法说明到底哪一件事做漏了，而漏掉关联卡片
# 会让前端永远看到"已确认但没有任务"，漏掉 outbox 会让任务永不执行。
# ---------------------------------------------------------------------------


def _spy_dispatch(calls):
    def dispatch(db, job, *args, **kwargs):
        calls.append(job.id)

    return dispatch


def test_create_job_creates_job_with_expected_fields(factory, thread, make_action):
    action_id = make_action(action_type="generate_images", payload={"assetIds": ["a1"]})
    with factory() as db:
        creation = action_service.create_job_for_action(
            db, db.get(AgentThread, thread.id), db.get(AgentAction, action_id), "u1",
            record_dispatch=_spy_dispatch([]),
        )

    with factory() as db:
        job = db.get(Job, creation.job_id)
    assert job is not None
    assert job.workspace_id == thread.workspace_id
    assert job.thread_id == thread.id
    assert job.job_type == "image_generation"
    assert job.status == "queued"
    assert job.progress == 0
    assert job.message == "等待执行"
    assert job.payload == {"assetIds": ["a1"]}


def test_create_job_records_outbox_intent(factory, thread, make_action):
    calls = []
    action_id = make_action(action_type="generate_images")
    with factory() as db:
        creation = action_service.create_job_for_action(
            db, db.get(AgentThread, thread.id), db.get(AgentAction, action_id), "u1",
            record_dispatch=_spy_dispatch(calls),
        )
    assert calls == [creation.job_id], "必须为这个任务写下 outbox 意图，否则任务永远不会被投递"


def test_create_job_links_action_to_job(factory, thread, make_action):
    action_id = make_action(action_type="generate_images")
    with factory() as db:
        creation = action_service.create_job_for_action(
            db, db.get(AgentThread, thread.id), db.get(AgentAction, action_id), "u1",
            record_dispatch=_spy_dispatch([]),
        )

    with factory() as db:
        assert db.get(AgentAction, action_id).job_id == creation.job_id, \
            "卡片没有关联到任务，前端会一直显示已确认但没有任务"


def test_create_job_saves_user_visible_message(factory, thread, make_action):
    action_id = make_action(action_type="generate_images")
    with factory() as db:
        creation = action_service.create_job_for_action(
            db, db.get(AgentThread, thread.id), db.get(AgentAction, action_id), "u1",
            record_dispatch=_spy_dispatch([]),
        )

    with factory() as db:
        messages = db.query(AgentMessage).filter(AgentMessage.thread_id == thread.id).all()
    assert len(messages) == 1
    assert messages[0].role == "assistant"
    assert messages[0].message_type == "job"
    assert messages[0].content == creation.message
    assert messages[0].metadata_json == {"jobId": creation.job_id}


def test_create_job_rolls_back_everything_on_failure(factory, thread, make_action, monkeypatch):
    """任何一步失败，四件事都不能留下痕迹。

    这里让"落消息"这一步抛错——它在 outbox 之后、提交之前。
    如果事务边界写错（比如中间偷偷 commit 过），任务和 outbox 会留下来，
    变成一条永远没人投递的孤儿任务。
    """
    from app.application import agent_service

    action_id = make_action(action_type="generate_images")
    calls = []

    def boom(*args, **kwargs):
        raise RuntimeError("模拟落消息失败")

    monkeypatch.setattr(agent_service, "save_agent_message", boom)

    with factory() as db:
        with pytest.raises(RuntimeError):
            action_service.create_job_for_action(
                db, db.get(AgentThread, thread.id), db.get(AgentAction, action_id), "u1",
                record_dispatch=_spy_dispatch(calls),
            )
        db.rollback()

    with factory() as db:
        assert db.query(Job).filter(Job.thread_id == thread.id).count() == 0, "任务不该留下"
        assert db.get(AgentAction, action_id).job_id is None, "卡片不该被关联"
        assert db.query(AgentMessage).filter(AgentMessage.thread_id == thread.id).count() == 0


def test_create_job_does_not_dispatch_to_queue(factory, thread, make_action):
    """建任务只写 outbox，不投递——投递是调用方的事。

    这条盯住"投递失败的处理仍留在路由"这个约定：如果哪天有人把投递
    也塞进来，路由里那段"队列不可用"的降级逻辑就变成死代码了。
    """
    from app.workers import local_tasks

    dispatched = []
    original = local_tasks.launch_job
    try:
        local_tasks.launch_job = lambda job_id: dispatched.append(job_id)
        action_id = make_action(action_type="generate_images")
        with factory() as db:
            action_service.create_job_for_action(
                db, db.get(AgentThread, thread.id), db.get(AgentAction, action_id), "u1",
                record_dispatch=_spy_dispatch([]),
            )
    finally:
        local_tasks.launch_job = original

    assert dispatched == [], "create_job_for_action 不应自己投递"


def test_dispatch_failure_keeps_job_queued_and_outbox_pending(factory, thread, make_action, monkeypatch):
    """投递失败时，已提交的任务和 outbox 意图都要留着。

    这是"不动投递失败语义"的护栏：任务仍是 queued（用户已收到 queued），
    outbox 仍是 pending（等扫描器重投）。把任务改成别的状态、或把 outbox
    标成已发送，都会让这个任务永远消失。
    """
    from app.application import agent_execution_service as agent
    from app.domain.models import JobOutboxEvent

    def boom(job_id):
        raise RuntimeError("队列不可用")

    monkeypatch.setattr(agent, "launch_job", boom)
    action_id = make_action(action_type="generate_images")
    with factory() as db:
        # 这里用真实的 record_job_dispatch：要验证的就是它写下的 outbox 记录
        creation = action_service.create_job_for_action(
            db, db.get(AgentThread, thread.id), db.get(AgentAction, action_id), "u1",
        )
        # 重建路由里那段投递失败处理，确认它对任务状态的影响在预期内
        try:
            agent.launch_job(creation.job_id)
        except RuntimeError:
            pass

    with factory() as db:
        job = db.get(Job, creation.job_id)
        assert job.status == "queued", "投递失败不该把任务改成别的状态"
        events = db.query(JobOutboxEvent).filter(JobOutboxEvent.job_id == job.id).all()
    assert len(events) == 1
    assert events[0].status == "pending", "outbox 必须留着 pending，等扫描器重投"
