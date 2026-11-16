import json
import zipfile


def _parse_sse(text):
    events = []
    for frame in text.split("\n\n"):
        data = next((line[5:].strip() for line in frame.splitlines() if line.startswith("data:")), None)
        if data and data != "[DONE]":
            events.append(json.loads(data))
    return events


def _stub_agent_classifications(monkeypatch, *responses):
    from types import SimpleNamespace

    from app.graph import nodes

    replies = iter(responses)
    monkeypatch.setattr(nodes, "model_available", lambda model_id: True)
    monkeypatch.setattr(
        nodes,
        "complete_chat",
        lambda *args, **kwargs: SimpleNamespace(text=json.dumps(next(replies), ensure_ascii=False)),
    )


def test_upload_copy_rewrite_and_compare(client, monkeypatch, caplog):
    from app.application import rewrite_service

    monkeypatch.setattr(rewrite_service, "run_normal_rewrite", lambda text, prompt_id, user_prompt, model, strength: (text.replace("原文", "新稿"), "demo"))
    workspace = client.post("/api/v1/workspaces", json={"name": "测试工作区"}).json()
    uploaded = client.post(
        f"/api/v1/workspaces/{workspace['id']}/documents",
        files={"file": ("sample.txt", "这是一段原文。", "text/plain")},
    )
    assert uploaded.status_code == 200
    assert "event=source.uploaded" in caplog.text
    assert "file=sample.txt" in caplog.text
    doc = uploaded.json()

    copied = client.post(
        f"/api/v1/workspaces/{workspace['id']}/rewrite-sessions",
        json={"documentId": doc["documentId"], "sourceRevisionId": doc["revisionId"]},
    )
    assert copied.status_code == 200
    assert "event=workbench.copied_to_rewrite" in caplog.text
    session = copied.json()
    assert session["content"] == doc["content"]

    rewritten = client.post(
        f"/api/v1/rewrite-sessions/{session['id']}/rewrite-runs",
        json={
            "baseWorkingRevisionId": session["workingRevisionId"],
            "scope": {"type": "document"},
            "modelId": "deepseek-v4-flash",
            "styleId": "humanize",
            "prompt": "",
            "strength": 55,
            "candidateCount": 1,
        },
    )
    assert rewritten.status_code == 200
    result = rewritten.json()
    assert result["content"] == "这是一段新稿。"
    assert any(segment["type"] in {"changed", "added"} for segment in result["diffSegments"])

    comparison = client.get(f"/api/v1/rewrite-sessions/{session['id']}/comparison").json()
    assert comparison["originalText"] == "这是一段原文。"
    assert comparison["content"] == "这是一段新稿。"
    assert "event=rewrite.completed" in caplog.text


def test_uploading_new_document_keeps_working_copy_and_changes_comparison_baseline(client):
    workspace = client.post("/api/v1/workspaces", json={"name": "跨文案对比测试"}).json()
    upload_a = client.post(
        f"/api/v1/workspaces/{workspace['id']}/documents",
        files={"file": ("a.txt", "A篇原始内容。", "text/plain")},
    ).json()
    session = client.post(
        f"/api/v1/workspaces/{workspace['id']}/rewrite-sessions",
        json={"documentId": upload_a["documentId"], "sourceRevisionId": upload_a["revisionId"]},
    ).json()
    edited = client.patch(
        f"/api/v1/rewrite-sessions/{session['id']}",
        json={"baseWorkingRevisionId": session["workingRevisionId"], "content": "A篇已经改写的内容。"},
    ).json()

    upload_b = client.post(
        f"/api/v1/workspaces/{workspace['id']}/documents",
        files={"file": ("b.txt", "B篇作为比较基准。", "text/plain")},
    ).json()
    compared = client.put(
        f"/api/v1/rewrite-sessions/{session['id']}/comparison-baseline",
        json={"documentId": upload_b["documentId"], "revisionId": upload_b["revisionId"]},
    )

    assert compared.status_code == 200
    result = compared.json()
    assert result["sourceRevisionId"] == upload_a["revisionId"]
    assert result["comparisonDocumentId"] == upload_b["documentId"]
    assert result["comparisonRevisionId"] == upload_b["revisionId"]
    assert result["originalText"] == "B篇作为比较基准。"
    assert result["content"] == edited["content"] == "A篇已经改写的内容。"

    refreshed = client.get(f"/api/v1/rewrite-sessions/{session['id']}/comparison").json()
    assert refreshed["originalText"] == "B篇作为比较基准。"
    assert refreshed["content"] == "A篇已经改写的内容。"


def test_manual_source_edit_updates_comparison_without_overwriting_working_copy(client, monkeypatch):
    from app.application import rewrite_service

    model_inputs = []
    monkeypatch.setattr(
        rewrite_service,
        "run_normal_rewrite",
        lambda text, prompt_id, user_prompt, model, strength: (model_inputs.append(text) or f"模型结果：{text}", "demo"),
    )
    workspace = client.post("/api/v1/workspaces", json={"name": "手动编辑测试"}).json()
    uploaded = client.post(
        f"/api/v1/workspaces/{workspace['id']}/documents",
        files={"file": ("source.txt", "上传的原文。", "text/plain")},
    ).json()
    copied = client.post(
        f"/api/v1/workspaces/{workspace['id']}/rewrite-sessions",
        json={"documentId": uploaded["documentId"], "sourceRevisionId": uploaded["revisionId"]},
    ).json()
    working = client.patch(
        f"/api/v1/rewrite-sessions/{copied['id']}",
        json={"baseWorkingRevisionId": copied["workingRevisionId"], "content": "中间稿保留这段内容。"},
    ).json()

    saved = client.post(
        f"/api/v1/documents/{uploaded['documentId']}/revisions",
        json={
            "baseRevisionId": uploaded["revisionId"],
            "content": "我粘贴回来的 A 篇内容。",
            "rewriteSessionId": copied["id"],
        },
    )

    assert saved.status_code == 200
    result = saved.json()
    assert result["revisionNumber"] == 2
    assert result["content"] == "我粘贴回来的 A 篇内容。"
    assert result["session"]["content"] == working["content"] == "中间稿保留这段内容。"
    assert result["session"]["originalText"] == "我粘贴回来的 A 篇内容。"

    rewrite = client.post(
        f"/api/v1/rewrite-sessions/{copied['id']}/rewrite-runs",
        json={
            "baseWorkingRevisionId": result["session"]["workingRevisionId"],
            "scope": {"type": "document"},
            "modelId": "deepseek-v4-flash",
            "styleId": "humanize",
            "prompt": "",
            "strength": 55,
            "candidateCount": 1,
        },
    )
    assert rewrite.status_code == 200
    assert model_inputs == ["中间稿保留这段内容。"]
    assert rewrite.json()["content"] == "模型结果：中间稿保留这段内容。"


def test_agent_rewrite_stays_in_agent_route(client, monkeypatch):
    from app.agents import graph

    monkeypatch.setattr(graph, "model_configured", lambda: False)
    workspace = client.post("/api/v1/workspaces", json={"name": "对话测试"}).json()
    thread = client.post("/api/v1/agent/threads", json={"workspaceId": workspace["id"]}).json()
    response = client.post(
        f"/api/v1/agent/threads/{thread['id']}/messages",
        json={"message": "帮我改写一下", "contextText": "这是一段用于智能体对话的文案。"},
    )
    assert response.status_code == 200
    assert response.json()["message"]["content"].startswith("我先按保留原意")
    assert client.get(f"/api/v1/agent/threads/{thread['id']}/messages").status_code == 200


def test_agent_thread_history_lists_recent_threads_searches_messages_and_scopes_workspace(client):
    from app.domain.models import AgentMessage

    workspace = client.post("/api/v1/workspaces", json={"name": "会话历史测试"}).json()
    other_workspace = client.post("/api/v1/workspaces", json={"name": "其他工作区"}).json()
    first = client.post("/api/v1/agent/threads", json={"workspaceId": workspace["id"]}).json()
    second = client.post("/api/v1/agent/threads", json={"workspaceId": workspace["id"]}).json()
    unrelated = client.post("/api/v1/agent/threads", json={"workspaceId": other_workspace["id"]}).json()

    with client.app.state.testing_session_factory() as db:
        db.add(AgentMessage(thread_id=first["id"], role="user", content="讨论月光邮局的开头"))
        db.add(AgentMessage(thread_id=second["id"], role="user", content="关于森林邮局的分镜"))
        db.add(AgentMessage(thread_id=unrelated["id"], role="user", content="不应该出现在这里的历史"))
        db.commit()

    response = client.get(f"/api/v1/workspaces/{workspace['id']}/agent/threads?limit=1")
    assert response.status_code == 200
    page = response.json()
    assert len(page["items"]) == 1
    assert page["hasMore"] is True
    assert page["items"][0]["messageCount"] == 1
    assert page["items"][0]["preview"] in {"讨论月光邮局的开头", "关于森林邮局的分镜"}

    next_page = client.get(f"/api/v1/workspaces/{workspace['id']}/agent/threads?limit=1&offset={page['nextOffset']}").json()
    assert len(next_page["items"]) == 1
    assert next_page["items"][0]["id"] != page["items"][0]["id"]
    assert next_page["hasMore"] is False

    searched = client.get(f"/api/v1/workspaces/{workspace['id']}/agent/threads?q=月光邮局").json()
    assert [item["id"] for item in searched["items"]] == [first["id"]]


def test_image_tool_requires_confirmation(client, monkeypatch):
    from app.application import agent_execution_service as agent

    monkeypatch.setattr(agent, "launch_job", lambda job_id: None)
    workspace = client.post("/api/v1/workspaces", json={"name": "确认测试"}).json()
    thread = client.post("/api/v1/agent/threads", json={"workspaceId": workspace["id"]}).json()
    response = client.post(
        f"/api/v1/agent/threads/{thread['id']}/messages",
        json={"message": "请帮我生成图片", "contextText": "这是已经确认过的测试文案。"},
    ).json()["message"]
    assert response["type"] == "confirmation"
    action_id = response["metadata"]["actionId"]

    queued = client.post(
        f"/api/v1/agent/threads/{thread['id']}/actions/{action_id}/resolve",
        json={"decision": "approve"},
    )
    assert queued.status_code == 200
    assert queued.json()["status"] == "queued"
    repeated = client.post(
        f"/api/v1/agent/threads/{thread['id']}/actions/{action_id}/resolve",
        json={"decision": "approve"},
    )
    assert repeated.status_code == 200
    assert repeated.json() == {"status": "already_processed", "jobId": queued.json()["jobId"]}
    assert len(client.get(f"/api/v1/workspaces/{workspace['id']}/jobs").json()) == 1
    messages = client.get(f"/api/v1/agent/threads/{thread['id']}/messages").json()
    confirmation = next(item for item in messages if item["metadata"].get("actionId") == action_id)
    assert confirmation["metadata"]["actionStatus"] == "confirmed"


def test_confirmed_media_tools_create_reviewable_assets_and_timeline(client, monkeypatch, tmp_path):
    from app.application import agent_execution_service as agent
    from app.domain.models import Asset, Job
    from app.workers import local_tasks

    monkeypatch.setattr(local_tasks, "SessionLocal", client.app.state.testing_session_factory)
    monkeypatch.setattr(local_tasks, "ASSET_DIR", tmp_path / "assets")
    monkeypatch.setattr(local_tasks, "EXPORT_DIR", tmp_path / "exports")
    local_tasks.ASSET_DIR.mkdir()
    local_tasks.EXPORT_DIR.mkdir()
    monkeypatch.setattr(agent, "launch_job", lambda job_id: local_tasks.run_job(job_id))
    workspace = client.post("/api/v1/workspaces", json={"name": "素材流程测试"}).json()
    thread = client.post("/api/v1/agent/threads", json={"workspaceId": workspace["id"]}).json()

    image_request = client.post(
        f"/api/v1/agent/threads/{thread['id']}/messages",
        json={"message": "按这版文案生成图片", "contextText": "第一幕\n\n第二幕"},
    ).json()["message"]
    image_job = client.post(
        f"/api/v1/agent/threads/{thread['id']}/actions/{image_request['metadata']['actionId']}/resolve",
        json={"decision": "approve"},
    ).json()["jobId"]
    assert client.get(f"/api/v1/jobs/{image_job}").json()["status"] == "completed"
    assets = client.get(f"/api/v1/workspaces/{workspace['id']}/assets").json()
    assert len(assets) == 2
    for asset in assets:
        assert client.post(f"/api/v1/assets/{asset['id']}/review", json={"decision": "approve"}).status_code == 200

    unrelated_thread = client.post("/api/v1/agent/threads", json={"workspaceId": workspace["id"]}).json()
    with client.app.state.testing_session_factory() as db:
        unrelated_job = Job(
            workspace_id=workspace["id"],
            thread_id=unrelated_thread["id"],
            job_type="image_generation",
            status="completed",
            result={"storyboard": [{"sceneNumber": 1, "narration": "其他文案", "imagePrompt": "其他图片"}]},
        )
        db.add(unrelated_job)
        db.flush()
        db.add(Asset(
            workspace_id=workspace["id"],
            job_id=unrelated_job.id,
            file_name="unrelated.png",
            object_path="D:/other/unrelated.png",
            scene_number=1,
            status="approved",
            is_demo=False,
        ))
        db.commit()

    subtitle = client.post(
        f"/api/v1/workspaces/{workspace['id']}/subtitle-files",
        files={"file": ("voice.srt", "1\n00:00:00,000 --> 00:00:02,000\n第一幕\n\n2\n00:00:02,000 --> 00:00:04,000\n第二幕", "text/plain")},
    ).json()
    video_request = client.post(
        f"/api/v1/agent/threads/{thread['id']}/messages",
        json={"message": "请生成视频草稿", "subtitleFileId": subtitle["id"]},
    ).json()["message"]
    video_job = client.post(
        f"/api/v1/agent/threads/{thread['id']}/actions/{video_request['metadata']['actionId']}/resolve",
        json={"decision": "approve"},
    ).json()["jobId"]
    result = client.get(f"/api/v1/jobs/{video_job}").json()
    assert result["status"] == "completed"
    draft = client.get(result["result"]["downloadUrl"])
    assert draft.status_code == 200
    timeline = draft.json()
    assert timeline["format"] == "lingzhu-timeline-v1"
    assert len(timeline["videoTrack"]) == 2
    assert [item["sceneNumber"] for item in timeline["subtitleTrack"]] == [1, 2]
    assert timeline["videoTrack"][0]["image"] in {asset["fileName"] for asset in assets}
    assert all(item["image"] != "unrelated.png" for item in timeline["videoTrack"])


def test_asset_download_supports_single_batch_and_workspace_validation(client, monkeypatch, tmp_path):
    from app.infrastructure import storage
    from app.domain.models import Asset, Job

    workspace = client.post("/api/v1/workspaces", json={"name": "素材下载"}).json()
    other_workspace = client.post("/api/v1/workspaces", json={"name": "其他工作区"}).json()
    paths = []
    with client.app.state.testing_session_factory() as db:
        for scene_number, content in ((1, b"image-one"), (2, b"image-two")):
            path = tmp_path / f"scene-{scene_number}.png"
            path.write_bytes(content)
            paths.append(path)
            job = Job(
                workspace_id=workspace["id"],
                job_type="image_generation",
                status="completed",
            )
            db.add(job)
            db.flush()
            db.add(Asset(
                workspace_id=workspace["id"],
                job_id=job.id,
                file_name=path.name,
                object_path=str(path),
                scene_number=scene_number,
                status="approved",
            ))
        other_job = Job(workspace_id=other_workspace["id"], job_type="image_generation", status="completed")
        db.add(other_job)
        db.flush()
        foreign_asset = Asset(
            workspace_id=other_workspace["id"],
            job_id=other_job.id,
            file_name="foreign.png",
            object_path=str(tmp_path / "foreign.png"),
            scene_number=1,
        )
        db.add(foreign_asset)
        db.commit()
        foreign_asset_id = foreign_asset.id

    listed = client.get(f"/api/v1/workspaces/{workspace['id']}/assets").json()
    ids_by_file = {item["fileName"]: item["id"] for item in listed}
    single = client.post(
        f"/api/v1/workspaces/{workspace['id']}/assets/download",
        json={"assetIds": [ids_by_file["scene-1.png"]]},
    )
    assert single.status_code == 200
    assert single.content == b"image-one"
    assert "scene-1.png" in single.headers["content-disposition"]

    batch = client.post(
        f"/api/v1/workspaces/{workspace['id']}/assets/download",
        json={"assetIds": list(ids_by_file.values())},
    )
    assert batch.status_code == 200
    archive_path = tmp_path / "download.zip"
    archive_path.write_bytes(batch.content)
    with zipfile.ZipFile(archive_path) as archive:
        assert set(archive.namelist()) == {"scene-01-scene-1.png", "scene-02-scene-2.png"}
        assert archive.read("scene-01-scene-1.png") == b"image-one"
        assert archive.read("scene-02-scene-2.png") == b"image-two"

    forbidden = client.post(
        f"/api/v1/workspaces/{workspace['id']}/assets/download",
        json={"assetIds": [foreign_asset_id]},
    )
    assert forbidden.status_code == 404

    with client.app.state.testing_session_factory() as db:
        cloud_job = Job(workspace_id=workspace["id"], job_type="image_generation", status="completed")
        db.add(cloud_job)
        db.flush()
        cloud_asset = Asset(
            workspace_id=workspace["id"],
            job_id=cloud_job.id,
            file_name="cloud.png",
            object_path=f"assets/{workspace['id']}/cloud.png",
            scene_number=3,
        )
        db.add(cloud_asset)
        db.commit()
        cloud_asset_id = cloud_asset.id

    monkeypatch.setattr(storage, "is_configured", lambda: True)
    downloaded_keys = []

    def fake_object_download(key, destination):
        downloaded_keys.append(key)
        destination.write_bytes(b"cloud-image")

    monkeypatch.setattr(storage, "download_file", fake_object_download)
    cloud_download = client.post(
        f"/api/v1/workspaces/{workspace['id']}/assets/download",
        json={"assetIds": [cloud_asset_id]},
    )
    assert cloud_download.status_code == 200
    assert cloud_download.content == b"cloud-image"
    assert downloaded_keys == [f"assets/{workspace['id']}/cloud.png"]


def test_subtitle_upload_rejects_invalid_timestamp(client):
    workspace = client.post("/api/v1/workspaces", json={"name": "SRT 时间校验"}).json()
    response = client.post(
        f"/api/v1/workspaces/{workspace['id']}/subtitle-files",
        files={"file": ("invalid.srt", "1\nnot-a-time --> 00:00:01,000\n字幕", "text/plain")},
    )

    assert response.status_code == 422
    assert "时间格式无效" in response.json()["detail"]


def test_model_list_includes_doubao(client):
    response = client.get("/api/v1/models")
    assert response.status_code == 200
    models = response.json()["models"]
    doubao = next(item for item in models if item["id"] == "doubao-2.1-pro")
    assert doubao["name"] == "Doubao 2.1 Pro（豆包）"


def test_rewrite_presets_are_loaded_from_prompt_file(client):
    response = client.get("/api/v1/rewrite-presets")
    assert response.status_code == 200
    presets = response.json()
    assert [(item["id"], item["promptId"]) for item in presets] == [
        ("humanize", "normal_humanize_v1"),
        ("deduplicate", "normal_deduplicate_v1"),
    ]


def test_agent_prompt_library_keeps_previous_versions_and_separates_kinds(client):
    catalog = client.get("/api/v1/agent/prompts").json()
    assert catalog["rewrite"][0]["versions"][0]["versionId"] == "agent_rewrite_default_v1"
    assert catalog["storyboard"][0]["versions"][0]["versionId"] == "agent_storyboard_default_v1"

    created = client.post("/api/v1/agent/prompts", json={
        "kind": "rewrite",
        "name": "温暖叙事",
        "content": "使用更温暖、自然的表达。",
    })
    assert created.status_code == 200
    version_one = created.json()
    assert version_one["version"] == 1
    assert version_one["kind"] == "rewrite"

    version_two_response = client.post(
        f"/api/v1/agent/prompts/{version_one['promptId']}/versions",
        json={"content": "语言更简洁，减少形容词。"},
    )
    assert version_two_response.status_code == 200
    version_two = version_two_response.json()
    assert version_two["version"] == 2
    assert version_two["versionId"] != version_one["versionId"]

    refreshed = client.get("/api/v1/agent/prompts").json()
    saved = next(item for item in refreshed["rewrite"] if item["promptId"] == version_one["promptId"])
    assert [item["version"] for item in saved["versions"]] == [1, 2]
    assert saved["versions"][0]["content"] == "使用更温暖、自然的表达。"
    assert saved["versions"][1]["content"] == "语言更简洁，减少形容词。"
    assert all(item["kind"] == "storyboard" for item in refreshed["storyboard"])


def test_stream_uses_and_records_selected_agent_rewrite_prompt_version(client, monkeypatch):
    from types import SimpleNamespace

    from app.application import agent_execution_service as agent
    from app.graph import nodes

    _stub_agent_classifications(monkeypatch, {"intent": "rewrite", "source": "message"})

    prompt = client.post("/api/v1/agent/prompts", json={
        "kind": "rewrite",
        "name": "温暖表达",
        "content": "用温暖、亲切的语气表达。",
    }).json()
    captured = []

    def fake_prepare(thread_id, message, context_text, model_id, **kwargs):
        captured.append(kwargs)
        return SimpleNamespace(
            prompt_messages=[{"role": "system", "content": "固定系统规则"}],
            estimated_tokens=1,
            token_budget=100,
            model_id=model_id,
            mode="live",
        )

    monkeypatch.setattr(nodes, "prepare_agent_rewrite", fake_prepare)
    monkeypatch.setattr(nodes, "stream_agent_rewrite", lambda prepared: iter(("温暖的改写文案。",)))
    monkeypatch.setattr(agent, "maybe_schedule_summary", lambda thread_id: None)

    workspace = client.post("/api/v1/workspaces", json={"name": "智能体提示词测试"}).json()
    thread = client.post("/api/v1/agent/threads", json={"workspaceId": workspace["id"]}).json()
    response = client.post(
        f"/api/v1/agent/threads/{thread['id']}/messages/stream",
        json={
            "message": "请修改文案，语气更温暖",
            "rewritePromptVersionId": prompt["versionId"],
        },
    )

    events = _parse_sse(response.text)
    completed = next(item["message"] for item in events if item["type"] == "message.completed")
    assert completed["content"] == "温暖的改写文案。"
    assert captured[0]["custom_instruction"] == "用温暖、亲切的语气表达。"
    assert captured[0]["prompt_id"] == prompt["promptId"]
    assert captured[0]["prompt_version_id"] == prompt["versionId"]
    assert completed["metadata"]["rewritePrompt"]["versionId"] == prompt["versionId"]

    history = client.get(f"/api/v1/agent/threads/{thread['id']}/messages").json()
    user_message = next(item for item in history if item["role"] == "user")
    assert user_message["metadata"]["rewritePrompt"]["versionId"] == prompt["versionId"]


def test_stream_rejects_prompt_version_from_wrong_kind(client):
    workspace = client.post("/api/v1/workspaces", json={"name": "提示词类型校验"}).json()
    thread = client.post("/api/v1/agent/threads", json={"workspaceId": workspace["id"]}).json()

    response = client.post(
        f"/api/v1/agent/threads/{thread['id']}/messages/stream",
        json={
            "message": "请修改文案",
            "rewritePromptVersionId": "agent_storyboard_default_v1",
        },
    )

    assert response.status_code == 422
    assert "类型" in response.json()["detail"]


def test_media_queue_uses_redis_broker():
    from app.workers.celery_app import celery_app

    assert celery_app.conf.broker_url.startswith("redis://127.0.0.1:6379/0")
    assert celery_app.conf.result_backend.startswith("redis://127.0.0.1:6379/1")


def test_summary_scheduling_uses_token_threshold_not_fixed_message_count(client, monkeypatch):
    from app.agents import memory_tasks
    from app.domain.models import AgentMessage, AgentThread

    monkeypatch.setattr(memory_tasks, "SessionLocal", client.app.state.testing_session_factory)
    workspace = client.post("/api/v1/workspaces", json={"name": "摘要预警测试"}).json()
    thread = client.post("/api/v1/agent/threads", json={"workspaceId": workspace["id"]}).json()
    with client.app.state.testing_session_factory() as db:
        thread_row = db.get(AgentThread, thread["id"])
        db.add(AgentMessage(thread_id=thread_row.id, role="user", content="旧偏好方向 " * 120))
        db.add(AgentMessage(thread_id=thread_row.id, role="assistant", content="收到偏好 " * 120))
        db.commit()

    scheduled = []
    monkeypatch.setattr(memory_tasks, "AGENT_INPUT_TOKEN_BUDGET", 100)
    monkeypatch.setattr(memory_tasks, "AGENT_SUMMARY_TRIGGER_RATIO", 0.5)
    monkeypatch.setattr(memory_tasks, "AGENT_RECENT_MESSAGE_COUNT", 1)
    monkeypatch.setattr(memory_tasks.summarize_agent_thread, "delay", lambda thread_id: scheduled.append(thread_id))

    memory_tasks.maybe_schedule_summary(thread["id"])

    assert scheduled == [thread["id"]]


def test_streaming_full_rewrite_uses_attachment_for_this_request_only(client, monkeypatch):
    from app.services.agent_rewrite import service as agent_rewrite
    from app.application import agent_execution_service as agent

    _stub_agent_classifications(
        monkeypatch,
        {"intent": "rewrite", "source": "attachment"},
        {"intent": "chat", "source": "none"},
    )

    full_text = "CURRENT_DOCUMENT_SECRET\n\n第二段原文。"
    captured = []

    selected_models = []

    def fake_stream(prompt_messages, model_id=None):
        captured.extend(prompt_messages)
        selected_models.append(model_id)
        yield "完整改写后的文案。"

    monkeypatch.setattr(agent_rewrite, "model_available", lambda model_id: True)
    monkeypatch.setattr(agent_rewrite, "stream_chat", fake_stream)
    monkeypatch.setattr(agent, "maybe_schedule_summary", lambda thread_id: None)
    workspace = client.post("/api/v1/workspaces", json={"name": "流式改写测试"}).json()
    thread = client.post("/api/v1/agent/threads", json={"workspaceId": workspace["id"]}).json()

    response = client.post(
        f"/api/v1/agent/threads/{thread['id']}/messages/stream",
        json={"message": "请整篇改写", "modelId": "qwen3.7-plus", "contextText": full_text},
    )
    events = _parse_sse(response.text)
    completed = next(item["message"] for item in events if item["type"] == "message.completed")
    assert response.headers["content-type"].startswith("text/event-stream")
    assert completed["type"] == "text"
    assert completed["content"] == "完整改写后的文案。"
    assert selected_models[0] == "qwen3.7-plus"
    assert captured[0]["content"].startswith("你是灵珠智能助手中的中文文案助手")
    assert sum(item["content"].count("CURRENT_DOCUMENT_SECRET") for item in captured) == 1
    assert sum(item["content"].count("请整篇改写") for item in captured) == 1

    history = client.get(f"/api/v1/agent/threads/{thread['id']}/messages").json()
    user_message = next(item for item in history if item["role"] == "user")
    assert user_message["content"] == "请整篇改写"
    assert user_message["metadata"]["hasAttachment"] is True
    assert user_message["metadata"]["attachedChars"] == len(full_text)

    captured.clear()
    followup = client.post(
        f"/api/v1/agent/threads/{thread['id']}/messages/stream",
        json={"message": "谢谢"},
    )
    assert followup.status_code == 200
    assert sum(item["content"].count("CURRENT_DOCUMENT_SECRET") for item in captured) == 0


def test_repeated_agent_request_id_replays_saved_result_without_duplicate_model_call(client, monkeypatch):
    from app.application import agent_execution_service as agent
    from app.services.agent_rewrite import service as agent_rewrite

    _stub_agent_classifications(monkeypatch, {"intent": "chat", "source": "none"})
    monkeypatch.setattr(agent, "maybe_schedule_summary", lambda thread_id: None)
    calls = []

    def fake_stream(prompt_messages, model_id=None):
        calls.append(model_id)
        yield "唯一一次回复。"

    monkeypatch.setattr(agent_rewrite, "model_available", lambda model_id: True)
    monkeypatch.setattr(agent_rewrite, "stream_chat", fake_stream)
    workspace = client.post("/api/v1/workspaces", json={"name": "幂等请求测试"}).json()
    thread = client.post("/api/v1/agent/threads", json={"workspaceId": workspace["id"]}).json()
    request_body = {
        "message": "你好",
        "modelId": "qwen3.7-plus",
        "requestId": "same-request-001",
    }

    first = client.post(f"/api/v1/agent/threads/{thread['id']}/messages/stream", json=request_body)
    second = client.post(f"/api/v1/agent/threads/{thread['id']}/messages/stream", json=request_body)

    first_events = _parse_sse(first.text)
    second_events = _parse_sse(second.text)
    first_message = next(item["message"] for item in first_events if item["type"] == "message.completed")
    second_message = next(item["message"] for item in second_events if item["type"] == "message.completed")
    history = client.get(f"/api/v1/agent/threads/{thread['id']}/messages").json()

    assert first.status_code == second.status_code == 200
    assert first_message["id"] == second_message["id"]
    assert calls == ["qwen3.7-plus"]
    assert sum(item["role"] == "user" for item in history) == 1
    assert sum(item["role"] == "assistant" for item in history) == 1

    reused_id = client.post(
        f"/api/v1/agent/threads/{thread['id']}/messages/stream",
        json={**request_body, "message": "另一条请求"},
    )
    assert reused_id.status_code == 409


def test_pasted_inline_document_is_an_ordinary_chat_message(client, monkeypatch):
    from app.services.agent_rewrite import service as agent_rewrite
    from app.application import agent_execution_service as agent

    # This test checks message/history handling, not model classification.
    # Both turns follow the ordinary chat path without consulting a real model.
    _stub_agent_classifications(
        monkeypatch,
        {"intent": "chat", "source": "none"},
        {"intent": "chat", "source": "none"},
    )
    full_text = "INLINE_DOCUMENT_SECRET\n" + "文章正文。" * 100
    captured = []

    def fake_stream(prompt_messages, model_id=None):
        captured.extend(prompt_messages)
        yield "已完成全文改写。"

    monkeypatch.setattr(agent_rewrite, "model_available", lambda model_id: True)
    monkeypatch.setattr(agent_rewrite, "stream_chat", fake_stream)
    monkeypatch.setattr(agent, "maybe_schedule_summary", lambda thread_id: None)
    workspace = client.post("/api/v1/workspaces", json={"name": "直接粘贴文案测试"}).json()
    thread = client.post("/api/v1/agent/threads", json={"workspaceId": workspace["id"]}).json()

    response = client.post(
        f"/api/v1/agent/threads/{thread['id']}/messages/stream",
        json={"message": f"帮我改写下面文案：\n{full_text}"},
    )
    assert response.status_code == 200
    events = _parse_sse(response.text)
    completed = next(item["message"] for item in events if item["type"] == "message.completed")
    assert completed["content"] == "已完成全文改写。"
    assert sum(item["content"].count("INLINE_DOCUMENT_SECRET") for item in captured) == 1

    history = client.get(f"/api/v1/agent/threads/{thread['id']}/messages").json()
    user_message = next(item for item in history if item["role"] == "user")
    assert "INLINE_DOCUMENT_SECRET" in user_message["content"]
    assert user_message["metadata"]["hasAttachment"] is False

    captured.clear()
    followup = client.post(
        f"/api/v1/agent/threads/{thread['id']}/messages/stream",
        json={"message": "把刚才第二段改得更有画面感"},
    )
    assert followup.status_code == 200
    followup_history = client.get(f"/api/v1/agent/threads/{thread['id']}/messages").json()
    original_message = next(item for item in followup_history if item["role"] == "user")
    assert original_message["metadata"]["longMessage"] is True
    assert all("INLINE_DOCUMENT_SECRET" not in item["content"] for item in captured)
    assert client.get(f"/api/v1/agent/threads/{thread['id']}/document").status_code == 404


def test_attachment_is_temporary_and_model_follows_user_requested_reply_scope(client, monkeypatch):
    from app.services.agent_rewrite import service as agent_rewrite
    from app.application import agent_execution_service as agent

    _stub_agent_classifications(
        monkeypatch,
        {"intent": "rewrite", "source": "attachment"},
        {"intent": "rewrite", "source": "attachment"},
    )

    original = f"LEFT_SENTINEL {'L' * 200}\n\nTARGET_PHRASE 需要改写的片段。\n\n{'R' * 200} RIGHT_SENTINEL"
    captured = []

    def fake_stream(prompt_messages, model_id=None):
        captured.extend(prompt_messages)
        current_request = prompt_messages[-1]["content"]
        if "只返回这段改写结果" in current_request:
            yield "已改好的片段。"
        else:
            yield "整合后的完整文案，包含已改好的片段。"

    monkeypatch.setattr(agent_rewrite, "model_available", lambda model_id: True)
    monkeypatch.setattr(agent_rewrite, "stream_chat", fake_stream)
    monkeypatch.setattr(agent, "maybe_schedule_summary", lambda thread_id: None)
    workspace = client.post("/api/v1/workspaces", json={"name": "选段改写测试"}).json()
    thread = client.post("/api/v1/agent/threads", json={"workspaceId": workspace["id"]}).json()

    rewrite = client.post(
        f"/api/v1/agent/threads/{thread['id']}/messages/stream",
        json={
            "message": "请把“TARGET_PHRASE 需要改写的片段。”改写得更自然，只返回这段改写结果。",
            "contextText": original,
        },
    )
    rewrite_events = _parse_sse(rewrite.text)
    segment_message = next(item["message"] for item in rewrite_events if item["type"] == "message.completed")
    prompt_text = "\n".join(item["content"] for item in captured)
    assert segment_message["type"] == "text"
    assert segment_message["content"] == "已改好的片段。"
    assert "TARGET_PHRASE" in prompt_text
    assert "LEFT_SENTINEL" in prompt_text
    assert sum(item["content"].count("LEFT_SENTINEL") for item in captured) == 1

    captured.clear()
    integration = client.post(
        f"/api/v1/agent/threads/{thread['id']}/messages/stream",
        json={"message": "请返回刚才改写后的完整全文", "contextText": original},
    )
    integration_events = _parse_sse(integration.text)
    full_message = next(item["message"] for item in integration_events if item["type"] == "message.completed")
    assert full_message["type"] == "text"
    assert "整合后的完整文案" in full_message["content"]
    assert "已改好的片段。" in full_message["content"]
    assert sum(item["content"].count("LEFT_SENTINEL") for item in captured) == 1


def test_graph_requires_final_and_storyboard_confirmation_before_image_job(client, monkeypatch):
    from types import SimpleNamespace

    from app.graph import nodes
    from app.services.storyboard import StoryboardResult

    _stub_agent_classifications(
        monkeypatch,
        {"intent": "finalize_and_generate_storyboard", "source": "attachment"},
        {"kind": "approve"},
        {"kind": "revise_workflow"},
    )

    monkeypatch.setattr(nodes, "get_session_factory", lambda: client.app.state.testing_session_factory)
    monkeypatch.setattr(nodes, "launch_job", lambda job_id: job_id)
    monkeypatch.setattr(
        nodes,
        "prepare_agent_rewrite",
        lambda *args, **kwargs: SimpleNamespace(
            prompt_messages=[{"role": "system", "content": ""}],
            estimated_tokens=1,
            token_budget=100,
            model_id="qwen3.7-plus",
            mode="live",
        ),
    )
    monkeypatch.setattr(nodes, "stream_agent_rewrite", lambda prepared: iter(("第一幕。第二幕。",)))
    monkeypatch.setattr(
        nodes,
        "generate_storyboard",
        lambda text, model_id, **kwargs: StoryboardResult(
            scenes=[
                {"sceneNumber": 1, "narration": "第一幕。", "imagePrompt": "清晨院子，柔和日光。"},
                {"sceneNumber": 2, "narration": "第二幕。", "imagePrompt": "室内近景。"},
            ],
            model_id=model_id,
            mode="live",
        ),
    )
    monkeypatch.setattr(
        nodes,
        "revise_storyboard_scene",
        lambda scenes, scene_number, instruction, model_id: [
            {**scenes[0], "imagePrompt": "夜晚院子，温暖灯光。"}, scenes[1]
        ],
    )
    workspace = client.post("/api/v1/workspaces", json={"name": "图流程确认测试"}).json()
    thread = client.post("/api/v1/agent/threads", json={"workspaceId": workspace["id"]}).json()

    response = client.post(
        f"/api/v1/agent/threads/{thread['id']}/messages/stream",
        json={"message": "请把下面文案作为最终文案，然后生成分镜", "modelId": "qwen3.7-plus", "contextText": "第一幕。第二幕。"},
    )
    events = _parse_sse(response.text)
    final_confirmation = next(event["message"] for event in events if event["type"] == "message.completed")
    assert final_confirmation["type"] == "confirmation"
    assert final_confirmation["metadata"]["flowStage"] == "confirm_final"
    assert final_confirmation["metadata"]["previewText"] == "第一幕。第二幕。"

    final_approval = client.post(
        f"/api/v1/agent/threads/{thread['id']}/messages/stream",
        json={"message": "确认", "modelId": "qwen3.7-plus"},
    )
    final_events = _parse_sse(final_approval.text)
    storyboard_confirmation = next(event["message"] for event in final_events if event["type"] == "message.completed")
    assert storyboard_confirmation["type"] == "confirmation"
    assert storyboard_confirmation["metadata"]["flowStage"] == "confirm_storyboard"
    assert "清晨院子" in storyboard_confirmation["metadata"]["previewText"]
    assert client.get(f"/api/v1/workspaces/{workspace['id']}/jobs").json() == []

    revised = client.post(
        f"/api/v1/agent/threads/{thread['id']}/messages/stream",
        json={"message": "第一分镜改成夜景", "modelId": "qwen3.7-plus"},
    )
    revised_events = _parse_sse(revised.text)
    storyboard_confirmation = next(event["message"] for event in revised_events if event["type"] == "message.completed")
    assert storyboard_confirmation["metadata"]["flowStage"] == "confirm_storyboard"
    assert "夜晚院子" in storyboard_confirmation["metadata"]["previewText"]

    storyboard_approval = client.post(
        f"/api/v1/agent/threads/{thread['id']}/actions/{storyboard_confirmation['metadata']['actionId']}/resolve",
        json={"decision": "approve"},
    )
    assert storyboard_approval.status_code == 200
    assert storyboard_approval.json()["status"] == "queued"
    repeated_approval = client.post(
        f"/api/v1/agent/threads/{thread['id']}/actions/{storyboard_confirmation['metadata']['actionId']}/resolve",
        json={"decision": "approve"},
    )
    assert repeated_approval.status_code == 200
    assert repeated_approval.json()["status"] == "already_processed"
    jobs = client.get(f"/api/v1/workspaces/{workspace['id']}/jobs").json()
    assert len(jobs) == 1
    assert jobs[0]["type"] == "image_generation"


def test_question_during_confirmation_keeps_workflow_paused(client, monkeypatch):
    from types import SimpleNamespace

    from app.application import agent_execution_service as agent
    from app.graph import nodes
    from app.graph.graph import get_agent_graph_state

    _stub_agent_classifications(
        monkeypatch,
        {"intent": "finalize_and_generate_storyboard", "source": "attachment"},
        {"kind": "chat"},
    )
    replies = iter(("你刚才要求把文案定为最终稿并生成分镜。",))
    monkeypatch.setattr(agent, "maybe_schedule_summary", lambda thread_id: None)
    monkeypatch.setattr(
        nodes,
        "prepare_agent_rewrite",
        lambda *args, **kwargs: SimpleNamespace(
            prompt_messages=[{"role": "system", "content": ""}],
            estimated_tokens=1,
            token_budget=100,
            model_id="qwen3.7-plus",
            mode="live",
        ),
    )
    monkeypatch.setattr(nodes, "stream_agent_rewrite", lambda prepared: iter((next(replies),)))

    workspace = client.post("/api/v1/workspaces", json={"name": "确认中普通提问测试"}).json()
    thread = client.post("/api/v1/agent/threads", json={"workspaceId": workspace["id"]}).json()
    initial = client.post(
        f"/api/v1/agent/threads/{thread['id']}/messages/stream",
        json={
            "message": "把附带文案作为最终文案，并生成分镜",
            "contextText": "最终文案。",
            "modelId": "qwen3.7-plus",
        },
    )
    card = next(event["message"] for event in _parse_sse(initial.text) if event["type"] == "message.completed")
    assert card["type"] == "confirmation"

    question = client.post(
        f"/api/v1/agent/threads/{thread['id']}/messages/stream",
        json={"message": "我前面一共问了你些什么", "modelId": "qwen3.7-plus"},
    )
    answer = next(event["message"] for event in _parse_sse(question.text) if event["type"] == "message.completed")
    assert answer["type"] == "text"
    assert answer["content"] == "你刚才要求把文案定为最终稿并生成分镜。"
    assert get_agent_graph_state(thread["id"])["interrupts"][0]["kind"] == "confirm_final"


def test_image_style_prompt_catalog_updates_selected_version_in_place(client):
    created = client.post("/api/v1/agent/prompts", json={
        "kind": "image_style",
        "name": "柔和水彩",
        "content": "柔和水彩风格。",
    })
    assert created.status_code == 200
    original = created.json()

    updated = client.put(
        f"/api/v1/agent/prompts/{original['promptId']}/versions/{original['versionId']}",
        json={"content": "清透水彩风格，纸面颗粒明显。"},
    )
    assert updated.status_code == 200
    assert updated.json()["versionId"] == original["versionId"]
    assert updated.json()["version"] == original["version"]
    assert updated.json()["content"] == "清透水彩风格，纸面颗粒明显。"

    catalog = client.get("/api/v1/agent/prompts").json()
    preset = next(item for item in catalog["image_style"] if item["promptId"] == original["promptId"])
    assert len(preset["versions"]) == 1
    assert preset["versions"][0]["content"] == "清透水彩风格，纸面颗粒明显。"


def test_new_final_copy_after_cancel_replaces_old_draft_before_confirmation(client, monkeypatch):
    from types import SimpleNamespace

    from app.application import agent_execution_service as agent
    from app.graph import nodes

    _stub_agent_classifications(
        monkeypatch,
        {"intent": "finalize_and_generate_storyboard", "source": "attachment"},
        {"intent": "finalize_and_generate_storyboard", "source": "message", "copy_text": "新稿。"},
    )
    monkeypatch.setattr(agent, "maybe_schedule_summary", lambda thread_id: None)

    workspace = client.post("/api/v1/workspaces", json={"name": "取消后提交新稿测试"}).json()
    thread = client.post("/api/v1/agent/threads", json={"workspaceId": workspace["id"]}).json()
    initial = client.post(
        f"/api/v1/agent/threads/{thread['id']}/messages/stream",
        json={
            "message": "把附带文案定为最终文案并生成分镜",
            "contextText": "旧稿。",
            "modelId": "qwen3.7-plus",
        },
    )
    old_card = next(event["message"] for event in _parse_sse(initial.text) if event["type"] == "message.completed")
    cancelled = client.post(
        f"/api/v1/agent/threads/{thread['id']}/actions/{old_card['metadata']['actionId']}/resolve",
        json={"decision": "reject"},
    )
    assert cancelled.status_code == 200
    from app.graph.graph import get_agent_graph_state
    cancelled_state = get_agent_graph_state(thread["id"])["state"]
    assert cancelled_state["draft_text"] == "旧稿。"
    assert cancelled_state["working_text"] == "旧稿。"

    replacement = client.post(
        f"/api/v1/agent/threads/{thread['id']}/messages/stream",
        json={
            "message": "新稿。请把这篇作为最终文案，重新生成分镜",
            "modelId": "qwen3.7-plus",
        },
    )
    new_card = next(event["message"] for event in _parse_sse(replacement.text) if event["type"] == "message.completed")
    assert new_card["type"] == "confirmation"
    assert new_card["metadata"]["previewText"] == "新稿。"


def test_rewrites_after_cancel_continue_from_latest_working_text(client, monkeypatch):
    from types import SimpleNamespace

    from app.application import agent_execution_service as agent
    from app.graph import nodes
    from app.graph.graph import get_agent_graph_state

    _stub_agent_classifications(
        monkeypatch,
        {"intent": "finalize_and_generate_storyboard", "source": "attachment"},
        {"intent": "rewrite", "source": "working_text"},
        {"intent": "rewrite", "source": "working_text"},
    )
    replies = iter(("工作稿第一版。", "工作稿第二版。"))
    prepared_calls = []
    monkeypatch.setattr(agent, "maybe_schedule_summary", lambda thread_id: None)

    def fake_prepare(thread_id, message, context_text, model_id, **kwargs):
        prepared_calls.append({"message": message, "context_text": context_text, **kwargs})
        return SimpleNamespace(
            prompt_messages=[{"role": "system", "content": ""}],
            estimated_tokens=1,
            token_budget=100,
            model_id="qwen3.7-plus",
            mode="live",
        )

    monkeypatch.setattr(nodes, "prepare_agent_rewrite", fake_prepare)
    monkeypatch.setattr(nodes, "stream_agent_rewrite", lambda prepared: iter((next(replies),)))

    workspace = client.post("/api/v1/workspaces", json={"name": "连续改写工作稿测试"}).json()
    thread = client.post("/api/v1/agent/threads", json={"workspaceId": workspace["id"]}).json()
    initial = client.post(
        f"/api/v1/agent/threads/{thread['id']}/messages/stream",
        json={
            "message": "把附件作为最终文案并生成分镜",
            "contextText": "已确认的旧稿。",
            "modelId": "qwen3.7-plus",
        },
    )
    old_card = next(event["message"] for event in _parse_sse(initial.text) if event["type"] == "message.completed")
    cancelled = client.post(
        f"/api/v1/agent/threads/{thread['id']}/actions/{old_card['metadata']['actionId']}/resolve",
        json={"decision": "reject"},
    )
    assert cancelled.status_code == 200

    first = client.post(
        f"/api/v1/agent/threads/{thread['id']}/messages/stream",
        json={"message": "在邮局前面加上夜色", "modelId": "qwen3.7-plus"},
    )
    first_reply = next(event["message"] for event in _parse_sse(first.text) if event["type"] == "message.completed")
    assert first_reply["content"] == "工作稿第一版。"

    second = client.post(
        f"/api/v1/agent/threads/{thread['id']}/messages/stream",
        json={"message": "刚才那版再写得温柔一些", "modelId": "qwen3.7-plus"},
    )
    second_reply = next(event["message"] for event in _parse_sse(second.text) if event["type"] == "message.completed")
    assert second_reply["content"] == "工作稿第二版。"
    assert prepared_calls[0]["draft_text"] == "已确认的旧稿。"
    assert prepared_calls[1]["draft_text"] == "工作稿第一版。"

    graph_state = get_agent_graph_state(thread["id"])["state"]
    assert graph_state["draft_text"] == "已确认的旧稿。"
    assert graph_state["working_text"] == "工作稿第二版。"


def test_graph_image_worker_uses_image_service_and_resumes_review(client, monkeypatch, tmp_path):
    from app.domain.models import AgentThread, Asset, Job
    from app.services.image_gen import GeneratedImage
    from app.workers import local_tasks

    monkeypatch.setattr(local_tasks, "SessionLocal", client.app.state.testing_session_factory)
    calls = []
    resumed = []

    def fake_generate(scene_number, prompt):
        calls.append((scene_number, prompt))
        return GeneratedImage(
            scene_number=scene_number,
            prompt=prompt,
            file_name=f"scene-{scene_number}.png",
            object_path=str(tmp_path / f"scene-{scene_number}.png"),
            model_id="doubao-seedream-5-0-flash-260915",
            request_id="test-request",
        )

    monkeypatch.setattr(local_tasks, "generate_scene_image", fake_generate)
    monkeypatch.setattr(local_tasks, "_resume_agent_image_flow", lambda *args: resumed.append(args))
    workspace = client.post("/api/v1/workspaces", json={"name": "后台生图服务测试"}).json()
    thread = client.post("/api/v1/agent/threads", json={"workspaceId": workspace["id"]}).json()
    with client.app.state.testing_session_factory() as db:
        job = Job(
            workspace_id=workspace["id"],
            thread_id=thread["id"],
            job_type="image_generation",
            status="queued",
            payload={
                "agentGraph": True,
                "sceneNumbers": [2],
                "storyboard": [
                    {"sceneNumber": 1, "narration": "第一镜", "imagePrompt": "第一张图"},
                    {"sceneNumber": 2, "narration": "第二镜", "imagePrompt": "第二张图"},
                ],
            },
        )
        db.add(job)
        db.commit()
        job_id = job.id

    local_tasks.run_job(job_id)
    local_tasks.run_job(job_id)

    with client.app.state.testing_session_factory() as db:
        saved_job = db.get(Job, job_id)
        assets = db.query(Asset).filter(Asset.job_id == job_id).all()
        assert saved_job.status == "completed"
        assert saved_job.result["mode"] == "live"
        assert len(assets) == 1
        assert assets[0].scene_number == 2
        assert assets[0].is_demo is False
        assert assets[0].file_name == "scene-2.png"
    assert calls == [(2, "第二张图")]
    assert resumed and resumed[0][0] == thread["id"]


def test_cancel_queued_graph_image_job_resumes_flow_and_worker_skips(client, monkeypatch):
    from app.api.v1 import jobs
    from app.domain.models import Job
    from app.workers import local_tasks

    factory = client.app.state.testing_session_factory
    resumed = []
    monkeypatch.setattr(jobs, "_resume_agent_image_flow", lambda *args: resumed.append(args))
    monkeypatch.setattr(local_tasks, "SessionLocal", factory)
    workspace = client.post("/api/v1/workspaces", json={"name": "取消排队任务测试"}).json()
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

    response = client.post(f"/api/v1/jobs/{job_id}/cancel")
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    assert resumed == [(thread["id"], job_id, [], "任务已取消")]
    local_tasks.run_job(job_id)
    with factory() as db:
        assert db.get(Job, job_id).status == "cancelled"

    with factory() as db:
        running_job = Job(
            workspace_id=workspace["id"],
            thread_id=thread["id"],
            job_type="image_generation",
            status="running",
            payload={"agentGraph": True, "storyboard": [], "sceneNumbers": []},
        )
        db.add(running_job)
        db.commit()
        running_job_id = running_job.id
    running_response = client.post(f"/api/v1/jobs/{running_job_id}/cancel")
    assert running_response.status_code == 200
    assert running_response.json()["status"] == "cancel_requested"


def test_running_image_job_stops_after_current_scene_on_cancel(client, monkeypatch, tmp_path):
    from app.domain.models import Asset, Job
    from app.services.image_gen import GeneratedImage
    from app.workers import local_tasks

    factory = client.app.state.testing_session_factory
    monkeypatch.setattr(local_tasks, "SessionLocal", factory)
    calls = []
    resumed = []
    job_ids = []

    def fake_generate(scene_number, prompt):
        calls.append(scene_number)
        if len(calls) == 1:
            with factory() as db:
                job = db.get(Job, job_ids[0])
                job.status = "cancel_requested"
                job.message = "正在停止当前任务"
                db.commit()
        return GeneratedImage(
            scene_number=scene_number,
            prompt=prompt,
            file_name=f"scene-{scene_number}.png",
            object_path=str(tmp_path / f"scene-{scene_number}.png"),
            model_id="doubao-seedream-5-0-flash-260915",
            request_id="test-request",
        )

    monkeypatch.setattr(local_tasks, "generate_scene_image", fake_generate)
    monkeypatch.setattr(local_tasks, "_resume_agent_image_flow", lambda *args: resumed.append(args))
    workspace = client.post("/api/v1/workspaces", json={"name": "运行中任务取消测试"}).json()
    thread = client.post("/api/v1/agent/threads", json={"workspaceId": workspace["id"]}).json()
    with factory() as db:
        job = Job(
            workspace_id=workspace["id"],
            thread_id=thread["id"],
            job_type="image_generation",
            status="queued",
            payload={
                "agentGraph": True,
                "sceneNumbers": [1, 2],
                "storyboard": [
                    {"sceneNumber": 1, "narration": "第一镜", "imagePrompt": "第一张图"},
                    {"sceneNumber": 2, "narration": "第二镜", "imagePrompt": "第二张图"},
                ],
            },
        )
        db.add(job)
        db.commit()
        job_ids.append(job.id)

    local_tasks.run_job(job_ids[0])

    with factory() as db:
        assert db.get(Job, job_ids[0]).status == "cancelled"
        assert db.query(Asset).filter(Asset.job_id == job_ids[0]).count() == 1
    assert calls == [1]
    assert resumed == [(thread["id"], job_ids[0], [], "任务已取消")]


def test_graph_full_confirm_image_review_srt_export_flow(client, monkeypatch, tmp_path):
    from pathlib import Path
    from types import SimpleNamespace

    from app.application import agent_execution_service as agent
    from app.domain.models import Asset, SubtitleFile
    from app.graph import nodes
    from app.services.image_gen import GeneratedImage
    from app.services.storyboard import StoryboardResult
    from app.workers import local_tasks

    _stub_agent_classifications(monkeypatch, {"intent": "finalize_and_generate_storyboard", "source": "attachment"})

    monkeypatch.setattr(nodes, "get_session_factory", lambda: client.app.state.testing_session_factory)
    monkeypatch.setattr(local_tasks, "SessionLocal", client.app.state.testing_session_factory)
    monkeypatch.setattr(nodes, "launch_job", lambda job_id: job_id)
    monkeypatch.setattr(agent, "launch_job", lambda job_id: job_id)
    monkeypatch.setattr(
        nodes,
        "prepare_agent_rewrite",
        lambda *args, **kwargs: SimpleNamespace(
            prompt_messages=[{"role": "system", "content": ""}],
            estimated_tokens=1,
            token_budget=100,
            model_id="qwen3.7-plus",
            mode="live",
        ),
    )
    monkeypatch.setattr(nodes, "stream_agent_rewrite", lambda prepared: iter(("小林走进院子。",)))
    monkeypatch.setattr(local_tasks, "EXPORT_DIR", tmp_path / "exports")
    monkeypatch.setattr(local_tasks, "ASSET_DIR", tmp_path / "assets")
    monkeypatch.setattr(local_tasks, "PREVIEW_DIR", tmp_path / "previews")
    local_tasks.ASSET_DIR.mkdir()
    local_tasks.PREVIEW_DIR.mkdir()
    monkeypatch.setattr(
        nodes,
        "generate_storyboard",
        lambda text, model_id, **kwargs: StoryboardResult(
            scenes=[{"sceneNumber": 1, "narration": text, "imagePrompt": "清晨院子，电影构图。"}],
            model_id=model_id,
            mode="live",
        ),
    )
    generated_prompts = []
    def fake_generate_scene_image(scene_number, prompt, output_dir=None):
        generated_prompts.append(prompt)
        target_dir = output_dir or local_tasks.ASSET_DIR
        target_dir.mkdir(parents=True, exist_ok=True)
        file_name = f"scene-{scene_number}-{len(list(target_dir.iterdir()))}.png"
        object_path = target_dir / file_name
        object_path.write_bytes(b"mock-image")
        return GeneratedImage(
            scene_number=scene_number,
            prompt=prompt,
            file_name=file_name,
            object_path=str(object_path),
            model_id="doubao-seedream-5-0-flash-260915",
            request_id="mock-image-request",
        )

    monkeypatch.setattr(local_tasks, "generate_scene_image", fake_generate_scene_image)

    workspace = client.post("/api/v1/workspaces", json={"name": "完整图流程测试"}).json()
    thread = client.post("/api/v1/agent/threads", json={"workspaceId": workspace["id"]}).json()
    initial = client.post(
        f"/api/v1/agent/threads/{thread['id']}/messages/stream",
        json={"message": "请把下面文案作为最终文案，然后生成分镜", "modelId": "qwen3.7-plus", "contextText": "小林走进院子。"},
    )
    final_confirmation = next(event["message"] for event in _parse_sse(initial.text) if event["type"] == "message.completed")
    storyboard_response = client.post(
        f"/api/v1/agent/threads/{thread['id']}/actions/{final_confirmation['metadata']['actionId']}/resolve",
        json={"decision": "approve"},
    )
    storyboard_confirmation = storyboard_response.json()["message"]
    image_start = client.post(
        f"/api/v1/agent/threads/{thread['id']}/actions/{storyboard_confirmation['metadata']['actionId']}/resolve",
        json={"decision": "approve"},
    )
    assert image_start.status_code == 200
    preview_job_id = image_start.json()["jobId"]
    local_tasks.run_job(preview_job_id)

    messages = client.get(f"/api/v1/agent/threads/{thread['id']}/messages").json()
    style_card = next(item for item in reversed(messages) if item["metadata"].get("flowStage") == "confirm_image_style")
    assert len(style_card["metadata"]["previewAssets"]) == 2
    assert client.get(f"/api/v1/workspaces/{workspace['id']}/assets").json() == []

    retry = client.post(
        f"/api/v1/agent/threads/{thread['id']}/actions/{style_card['metadata']['actionId']}/resolve",
        json={
            "decision": "retry_preview",
            "stylePrompt": "水彩手绘，柔和的纸面纹理",
            "imageStylePromptVersionId": "agent_image_style_default_v1",
        },
    )
    assert retry.status_code == 200
    local_tasks.run_job(retry.json()["jobId"])
    messages = client.get(f"/api/v1/agent/threads/{thread['id']}/messages").json()
    style_card = next(item for item in reversed(messages) if item["metadata"].get("flowStage") == "confirm_image_style")
    assert style_card["metadata"]["imageStyle"]["content"] == "水彩手绘，柔和的纸面纹理"
    assert client.get(f"/api/v1/workspaces/{workspace['id']}/assets").json() == []

    image_start = client.post(
        f"/api/v1/agent/threads/{thread['id']}/actions/{style_card['metadata']['actionId']}/resolve",
        json={"decision": "approve", "selectedPreviewIndex": 1},
    )
    image_job_id = image_start.json()["jobId"]
    local_tasks.run_job(image_job_id)

    messages = client.get(f"/api/v1/agent/threads/{thread['id']}/messages").json()
    image_review = next(item for item in reversed(messages) if item["metadata"].get("flowStage") == "confirm_images")
    with client.app.state.testing_session_factory() as db:
        assets = db.query(Asset).filter(Asset.job_id == image_job_id).all()
        asset = assets[0]
        assert len(assets) == 1
        assert asset.is_demo is False
        assert "水彩手绘" in asset.prompt
    assert any("水彩手绘" in prompt for prompt in generated_prompts)
    assert list(local_tasks.PREVIEW_DIR.iterdir()) == []

    srt = "1\n00:00:00,000 --> 00:00:02,000\n小林走进院子。\n"
    uploaded = client.post(
        f"/api/v1/workspaces/{workspace['id']}/subtitle-bundles",
        files=[
            ("files", ("voice.srt", srt, "text/plain")),
            ("files", ("voice.mp3", b"mock-audio", "audio/mpeg")),
        ],
    )
    assert uploaded.status_code == 200
    assert uploaded.json()["audioFileName"] == "voice.mp3"
    with client.app.state.testing_session_factory() as db:
        uploaded_bundle = db.get(SubtitleFile, uploaded.json()["id"])
        assert uploaded_bundle.audio_file_name == "voice.mp3"
        assert Path(uploaded_bundle.audio_object_path).read_bytes() == b"mock-audio"

    draft_path = tmp_path / "jianying" / "灵珠草稿-test"
    monkeypatch.setattr(
        nodes,
        "create_jianying_draft",
        lambda *args: SimpleNamespace(name="灵珠草稿-test", path=draft_path),
    )
    monkeypatch.setattr("app.core.config.JIANYING_DRAFT_DIR", str(tmp_path / "jianying"))

    image_approval = client.post(
        f"/api/v1/agent/threads/{thread['id']}/actions/{image_review['metadata']['actionId']}/resolve",
        json={"decision": "approve"},
    )
    assert image_approval.status_code == 200

    timeline_response = client.post(
        f"/api/v1/agent/threads/{thread['id']}/messages/stream",
        json={
            "message": "请生成 JSON 时间线",
            "modelId": "qwen3.7-plus",
            "subtitleFileId": uploaded.json()["id"],
        },
    )
    timeline_events = _parse_sse(timeline_response.text)
    result_message = next(event["message"] for event in timeline_events if event["type"] == "message.completed")
    assert result_message["type"] == "job_result"
    assert result_message["metadata"]["result"]["draftPath"] == str(draft_path)
    assert "剪映草稿已生成" in result_message["content"]
