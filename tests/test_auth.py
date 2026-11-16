from app.domain.models import Asset, Job, Workspace


def test_business_routes_require_authentication(client):
    client.post("/api/v1/auth/logout")

    assert client.get("/api/v1/workspaces").status_code == 401
    assert client.get("/api/v1/health").status_code == 200


def test_auth_accepts_the_local_vite_fallback_origin_and_rejects_foreign_origins(client):
    accepted = client.post(
        "/api/v1/auth/login",
        headers={"Origin": "http://127.0.0.1:5174"},
        json={"email": "fixture@example.com", "password": "fixture-password-123"},
    )
    rejected = client.post(
        "/api/v1/auth/login",
        headers={"Origin": "https://attacker.example"},
        json={"email": "fixture@example.com", "password": "fixture-password-123"},
    )

    assert accepted.status_code == 200
    assert rejected.status_code == 403


def test_thread_execution_lease_allows_only_one_holder(client):
    from app.application.thread_execution import ThreadExecutionLease

    workspace = client.post("/api/v1/workspaces", json={"name": "lease test"}).json()
    thread = client.post("/api/v1/agent/threads", json={"workspaceId": workspace["id"]}).json()
    first = ThreadExecutionLease(thread["id"])
    second = ThreadExecutionLease(thread["id"])
    factory = client.app.state.testing_session_factory

    with factory() as db:
        assert first.acquire(db) is True
    try:
        with factory() as db:
            assert second.acquire(db) is False
    finally:
        first.release()
    try:
        with factory() as db:
            assert second.acquire(db) is True
    finally:
        second.release()


def test_stream_request_returns_conflict_when_thread_is_busy(client):
    from app.application.thread_execution import ThreadExecutionLease

    workspace = client.post("/api/v1/workspaces", json={"name": "busy thread"}).json()
    thread = client.post("/api/v1/agent/threads", json={"workspaceId": workspace["id"]}).json()
    lease = ThreadExecutionLease(thread["id"])
    factory = client.app.state.testing_session_factory
    with factory() as db:
        assert lease.acquire(db) is True
    try:
        response = client.post(
            f"/api/v1/agent/threads/{thread['id']}/messages/stream",
            json={"message": "你好", "requestId": "busy-request-001"},
        )
    finally:
        lease.release()

    assert response.status_code == 409
    assert response.json()["detail"] == "该对话正在处理中，请稍后重试"
    assert client.get(f"/api/v1/agent/threads/{thread['id']}/messages").json() == []


def test_users_cannot_access_each_others_workspace_or_media(client, tmp_path):
    workspace = client.post("/api/v1/workspaces", json={"name": "private"}).json()
    thread = client.post("/api/v1/agent/threads", json={"workspaceId": workspace["id"]}).json()
    private_prompt = client.post("/api/v1/agent/prompts", json={
        "kind": "rewrite",
        "name": "private prompt",
        "content": "only for account one",
    }).json()
    asset_name = "private-test.png"
    own_file = tmp_path / asset_name
    own_file.write_bytes(b"private")
    factory = client.app.state.testing_session_factory
    with factory() as db:
        job = Job(workspace_id=workspace["id"], job_type="image_generation")
        db.add(job)
        db.flush()
        asset = Asset(workspace_id=workspace["id"], job_id=job.id, file_name=asset_name, object_path=str(own_file))
        db.add(asset)
        db.flush()
        asset_id = asset.id
        db.commit()
    own_media = client.get(f"/api/v1/assets/{asset_id}/content")
    assert own_media.status_code == 200
    assert own_media.content == b"private"

    client.post("/api/v1/auth/logout")
    second = client.post("/api/v1/auth/register", json={"email": "second@example.com", "password": "second-password-123"})
    assert second.status_code == 201
    assert client.get("/api/v1/workspaces").json() == []
    assert client.get(f"/api/v1/workspaces/{workspace['id']}/assets").status_code == 404
    assert client.get(f"/api/v1/agent/threads/{thread['id']}/messages").status_code == 404
    prompt_catalog = client.get("/api/v1/agent/prompts").json()
    assert all(item["promptId"] != private_prompt["promptId"] for item in prompt_catalog["rewrite"])
    assert client.post(
        f"/api/v1/agent/prompts/{private_prompt['promptId']}/versions",
        json={"content": "unauthorized edit"},
    ).status_code == 404
    assert client.get(f"/api/v1/assets/{asset_id}/content").status_code == 404
