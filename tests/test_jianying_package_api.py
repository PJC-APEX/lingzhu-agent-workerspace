"""Authorized ZIP export for local Jianying draft creation."""
import io
import zipfile

import pytest
from fastapi.testclient import TestClient

from app.domain.models import AgentThread, Asset, Job, SubtitleFile, Workspace
from app.main import app


@pytest.fixture
def video_export(client, tmp_path):
    workspace = client.post("/api/v1/workspaces", json={"name": "导出包测试"}).json()
    factory = client.app.state.testing_session_factory
    source = tmp_path / "materials"
    source.mkdir()
    image = source / "original-scene.jpg"
    image.write_bytes(b"scene-image-bytes")
    audio = source / "original-voice.mp3"
    audio.write_bytes(b"voice-audio-bytes")
    subtitle_path = source / "original.srt"
    subtitle_path.write_bytes(b"subtitle-source-bytes")
    cues = [{"index": 1, "start": "00:00:00,000", "end": "00:00:02,000",
             "start_ms": 0, "end_ms": 2000, "text": "Hello"}]
    with factory() as db:
        owner = db.get(Workspace, workspace["id"]).owner_user_id
        thread = AgentThread(workspace_id=workspace["id"], title="package test")
        db.add(thread)
        db.flush()
        subtitle = SubtitleFile(workspace_id=workspace["id"], file_name="source.srt",
                                object_path=str(subtitle_path), audio_file_name="voice.mp3",
                                audio_object_path=str(audio), cues=cues)
        db.add(subtitle)
        image_job = Job(workspace_id=workspace["id"], thread_id=thread.id,
                        job_type="image_generation", status="completed",
                        result={"storyboard": [{"sceneNumber": 1, "narration": "Hello",
                                                 "imagePrompt": "a scene"}]})
        db.add(image_job)
        db.flush()
        asset = Asset(workspace_id=workspace["id"], job_id=image_job.id, file_name="scene.jpg",
                      object_path=str(image), scene_number=1, status="approved")
        db.add(asset)
        db.flush()
        video = Job(workspace_id=workspace["id"], thread_id=thread.id,
                    job_type="video_generation", status="completed",
                    payload={"subtitleFileId": subtitle.id, "imageJobId": image_job.id})
        db.add(video)
        db.commit()
        video_id = video.id
    return factory, workspace["id"], owner, video_id, source


def test_video_package_contains_manifest_timeline_and_media(video_export, client):
    _, _, _, job_id, source = video_export
    response = client.get(f"/api/v1/jobs/{job_id}/jianying-package")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/zip")
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert set(archive.namelist()) == {
            "manifest.json", "timeline.json", "materials/subtitles/source.srt",
            "materials/audio/voice.mp3", "materials/images/scene-0001-scene.jpg",
        }
        import json
        manifest = json.loads(archive.read("manifest.json"))
        timeline = json.loads(archive.read("timeline.json"))
        assert manifest["format"] == "lingzhu-jianying-package-v1"
        assert manifest["materials"]["audio"]["path"] == "materials/audio/voice.mp3"
        assert timeline["videoTrack"][0]["imagePath"] == "materials/images/scene-0001-scene.jpg"
        assert archive.read("materials/audio/voice.mp3") == b"voice-audio-bytes"
        assert archive.read("materials/subtitles/source.srt") == b"subtitle-source-bytes"
        assert archive.read("materials/images/scene-0001-scene.jpg") == b"scene-image-bytes"
    assert list(source.iterdir())


def test_video_package_is_private_and_requires_completed_video_job(video_export, client):
    factory, _, _, job_id, _ = video_export
    stranger = TestClient(app)
    registered = stranger.post("/api/v1/auth/register", json={
        "email": "package-stranger@example.com", "password": "stranger-password-123",
    })
    assert registered.status_code == 201
    assert stranger.get(f"/api/v1/jobs/{job_id}/jianying-package").status_code == 404
    with factory() as db:
        db.get(Job, job_id).status = "running"
        db.commit()
    response = client.get(f"/api/v1/jobs/{job_id}/jianying-package")
    assert response.status_code == 409
    assert response.json() == {"detail": "视频任务尚未完成"}


def test_video_package_rejects_unreviewed_or_incomplete_assets(video_export, client):
    factory, workspace_id, owner, job_id, _ = video_export
    with factory() as db:
        asset = db.query(Asset).filter(Asset.workspace_id == workspace_id).one()
        asset.status = "needs_review"
        db.commit()
    response = client.get(f"/api/v1/jobs/{job_id}/jianying-package")
    assert response.status_code == 409
    assert response.json() == {"detail": "图片素材不完整或尚未全部审核通过"}


def test_video_package_cleans_staging_on_storage_failure(video_export, client, monkeypatch, tmp_path):
    from app.application import jianying_package_service
    from app.infrastructure import storage
    from app.infrastructure import file_io

    _, _, _, job_id, _ = video_export
    staging = tmp_path / "package-stage"
    def make_staging(prefix):
        staging.mkdir()
        return staging
    monkeypatch.setattr(file_io, "temporary_directory", make_staging)
    monkeypatch.setattr(storage, "download_reference", lambda *args: (_ for _ in ()).throw(storage.ObjectStorageError("offline")))
    response = client.get(f"/api/v1/jobs/{job_id}/jianying-package")
    assert response.status_code == 502
    assert response.json() == {"detail": "导出素材暂时无法读取，请稍后重试"}
    assert not staging.exists()


def test_graph_package_mode_queues_export_without_creating_server_draft(video_export, monkeypatch):
    from app.core import config
    from app.application import jianying_export_service as service
    from app.graph.nodes import assemble_jianying_draft_node
    from app.domain.models import AgentThread, JobOutboxEvent
    factory, workspace_id, owner, video_id, _ = video_export
    with factory() as db:
        video = db.get(Job, video_id)
        image_id = video.payload["imageJobId"]
        subtitle_id = video.payload["subtitleFileId"]
        thread_id = video.thread_id
        scenes = db.get(Job, image_id).result["storyboard"]
        db.delete(video)
        db.commit()
    monkeypatch.setattr(config, "JIANYING_EXPORT_MODE", "package")
    sent = []
    monkeypatch.setattr(service, "publish_job", sent.append)
    result = assemble_jianying_draft_node({
        "workspace_id": workspace_id, "thread_id": thread_id, "user_id": owner,
        "image_job_id": image_id, "subtitle_file_id": subtitle_id, "storyboard": scenes,
        "image_assets": [],
    })
    assert result["stage"] == "completed"
    assert result["draft_path"] == ""
    with factory() as db:
        job = db.get(Job, result["package_job_id"])
        assert job.job_type == "video_generation" and job.status == "queued"
        assert db.query(JobOutboxEvent).filter(JobOutboxEvent.job_id == job.id).count() == 1
    second = service.submit_export(thread_id=thread_id, workspace_id=workspace_id, user_id=owner,
                                   image_job_id=image_id, subtitle_file_id=subtitle_id)
    assert second == result["package_job_id"]
    with factory() as db:
        assert db.query(Job).filter(Job.job_type == "video_generation").count() == 1
