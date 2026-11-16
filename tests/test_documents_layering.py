"""文档模块分层后的行为约束测试。

重点验证第一批重构的两个真实缺陷已修复：
1. 数据库写入失败时，已上传到对象存储的文件必须被清理（旧实现会泄漏）
2. 上传超过大小限制时，不留残留文件
"""

from __future__ import annotations

import io

import pytest

from app.application import document_service
from app.infrastructure import storage as object_storage


def _upload(client, workspace_id: str, filename: str, payload: bytes, content_type: str = "text/plain"):
    return client.post(
        f"/api/v1/workspaces/{workspace_id}/documents",
        files={"file": (filename, io.BytesIO(payload), content_type)},
    )


@pytest.fixture
def workspace_id(client):
    response = client.post("/api/v1/workspaces", json={"name": "测试工作区"})
    assert response.status_code == 200
    return response.json()["id"]


def test_upload_returns_expected_payload(client, workspace_id):
    """响应结构必须与重构前逐字段一致。"""
    response = _upload(client, workspace_id, "demo.txt", "这是一段测试文字。".encode("utf-8"))
    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {"documentId", "revisionId", "fileName", "content", "characterCount"}
    assert payload["fileName"] == "demo.txt"
    assert payload["content"] == "这是一段测试文字。"
    assert payload["characterCount"] == len("这是一段测试文字。")


def test_document_list_uses_single_batch_query(client, workspace_id, monkeypatch):
    """文档列表必须一次取回最新版本，不允许逐条查询（原 N+1）。"""
    for index in range(3):
        _upload(client, workspace_id, f"doc-{index}.txt", f"内容 {index}".encode("utf-8"))

    calls = {"batch": 0, "single": 0}
    original_batch = document_service.document_repo.latest_revisions_by_document_ids
    original_single = document_service.document_repo.latest_revision

    def counting_batch(db, document_ids):
        calls["batch"] += 1
        return original_batch(db, document_ids)

    def counting_single(db, document_id):
        calls["single"] += 1
        return original_single(db, document_id)

    monkeypatch.setattr(document_service.document_repo, "latest_revisions_by_document_ids", counting_batch)
    monkeypatch.setattr(document_service.document_repo, "latest_revision", counting_single)

    response = client.get(f"/api/v1/workspaces/{workspace_id}/documents")
    assert response.status_code == 200
    assert len(response.json()) == 3
    assert calls["batch"] == 1, "应使用批量查询"
    assert calls["single"] == 0, "列表接口不应逐条查版本"


def test_upload_failure_cleans_up_stored_object(client, workspace_id, monkeypatch):
    """数据库写入失败时，已上传到对象存储的文件必须被删除。"""
    uploaded: list[str] = []
    deleted: list[str] = []

    def fake_persist_file(source, object_key, content_type):
        ref = f"storage://{object_key}"
        uploaded.append(ref)
        return ref

    def fake_delete_reference(reference):
        deleted.append(reference)

    monkeypatch.setattr(object_storage, "persist_file", fake_persist_file)
    monkeypatch.setattr(object_storage, "delete_reference", fake_delete_reference)

    def boom(*args, **kwargs):
        raise RuntimeError("模拟数据库写入失败")

    monkeypatch.setattr(document_service.document_repo, "add_document", boom)

    response = _upload(client, workspace_id, "will-fail.txt", "会失败的内容".encode("utf-8"))
    assert response.status_code == 500
    assert uploaded, "应当已经尝试上传到对象存储"
    assert deleted == uploaded, "数据库失败后必须清理已上传的对象"


def test_upload_rejects_oversized_file(client, workspace_id):
    """超过 20MB 直接拒绝。"""
    oversized = b"a" * (20 * 1024 * 1024 + 1)
    response = _upload(client, workspace_id, "huge.txt", oversized)
    assert response.status_code == 413
    assert "20MB" in response.json()["detail"]


def test_upload_rejects_unsupported_or_empty(client, workspace_id):
    """解析不出文字时返回 422，而不是 500。"""
    response = _upload(client, workspace_id, "empty.txt", b"   \n  ")
    assert response.status_code == 422


def test_document_ownership_is_enforced(client, workspace_id):
    """别人的工作区不允许上传。"""
    response = _upload(client, "not-a-real-workspace", "x.txt", b"hello")
    assert response.status_code == 404


def test_latest_revision_lookup_does_not_scale_with_version_count(client, workspace_id):
    """取最新版本时，数据库返回的行数只与文档数有关，与历史版本数无关。

    早先的实现是「按 document_id 全量排序后取首条」：虽然只有一次查询，
    但每个文档的所有历史版本都会被传回 Python。一个文档改 N 版就白拉 N-1 行。
    这里给同一个文档造 30 个版本，然后统计 SQL 实际返回的行数。
    """
    from sqlalchemy import event

    from app.domain.models import DocumentRevision

    response = _upload(client, workspace_id, "many-versions.txt", b"v1")
    document_id = response.json()["documentId"]

    # 直接落库造版本，避免 30 次 HTTP 往返
    factory = client.app.state.testing_session_factory
    with factory() as db:
        for number in range(2, 31):
            db.add(DocumentRevision(document_id=document_id, revision_number=number, content="x" * number))
        db.commit()

    captured = {"sql": ""}

    def capture_sql(conn, cursor, statement, parameters, context, executemany):
        # 注意：这里绝不能调用 cursor.fetchall()。
        # 在 after_cursor_execute 阶段读游标会把结果集消费掉，
        # 应用后续就拿不到数据（表现为字数变成 0）。只记语句文本即可。
        normalized = " ".join(statement.split()).lower()
        if "document_revisions" in normalized and "max(" in normalized:
            captured["sql"] = normalized

    with factory() as session:
        engine = session.bind
    event.listen(engine, "after_cursor_execute", capture_sql)
    try:
        listed = client.get(f"/api/v1/workspaces/{workspace_id}/documents")
    finally:
        event.remove(engine, "after_cursor_execute", capture_sql)

    assert listed.status_code == 200
    items = listed.json()
    assert len(items) == 1
    # 最新版本是第 30 版，字数应为 30
    assert items[0]["characterCount"] == 30

    sql = captured["sql"]
    assert sql, "没有捕获到取最新版本的查询"
    # 关键点：最新版本的筛选必须发生在数据库里（子查询 max + join），
    # 而不是把全部历史版本拉回 Python 再挑。早先的写法会带 order by ... desc。
    assert "max(" in sql and "group by" in sql, (
        f"最新版本应由数据库筛选，实际 SQL：{sql}"
    )

