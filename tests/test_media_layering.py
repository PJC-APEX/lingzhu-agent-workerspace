"""Application ownership and resource lifecycle for uploads and downloads."""
import io
import threading

import pytest

from app.application import asset_service, media_service, subtitle_service
from app.domain.exceptions import NotFoundError, TooLargeError, UpstreamError, ValidationError
from app.domain.models import AgentAction, AgentThread, Asset, ExportFile, Job, SubtitleFile, Workspace
from app.infrastructure import file_io, storage

SRT = b"1\n00:00:00,000 --> 00:00:01,000\nhello\n"


@pytest.fixture
def workspace(client, monkeypatch, tmp_path):
    from app.core import config
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    previews = tmp_path / "previews"
    previews.mkdir()
    monkeypatch.setattr(config, "UPLOAD_DIR", uploads)
    monkeypatch.setattr(config, "PREVIEW_DIR", previews)
    row = client.post("/api/v1/workspaces", json={"name": "media tests"}).json()
    factory = client.app.state.testing_session_factory
    with factory() as db:
        owner = db.get(Workspace, row["id"]).owner_user_id
    return factory, row["id"], owner, uploads, previews


def seed_asset(factory, workspace_id, path, *, scene=1, filename=None):
    with factory() as db:
        job = Job(workspace_id=workspace_id, job_type="image_generation")
        db.add(job)
        db.flush()
        asset = Asset(workspace_id=workspace_id, job_id=job.id, object_path=str(path),
                      file_name=filename or path.name, scene_number=scene)
        db.add(asset)
        db.commit()
        return asset.id



def seed_preview(factory, workspace_id, reference, filename="preview.png"):
    with factory() as db:
        job = Job(workspace_id=workspace_id, job_type="image_generation",
                  payload={"agentGraphPreview": True}, result={"assets": [{
                      "previewIndex": 0, "fileName": filename, "objectPath": reference,
                  }]})
        db.add(job)
        db.commit()
        return job.id

def test_application_denies_foreign_asset_and_export_without_http(workspace, tmp_path):
    factory, workspace_id, owner, *_ = workspace
    source = tmp_path / "image.png"
    source.write_bytes(b"private")
    asset_id = seed_asset(factory, workspace_id, source)
    with factory() as db:
        asset = db.get(Asset, asset_id)
        export = ExportFile(job_id=asset.job_id, file_name="draft.json", object_path=str(source))
        db.add(export)
        db.commit()
        export_id = export.id
        for call in (
            lambda: asset_service.list_assets(db, workspace_id=workspace_id, user_id="stranger"),
            lambda: asset_service.review_asset(db, asset_id=asset_id, decision="approve", user_id="stranger"),
            lambda: asset_service.prepare_download(db, workspace_id=workspace_id, asset_ids=[asset_id], user_id="stranger"),
            lambda: media_service.prepare_export(db, export_id=export_id, user_id="stranger"),
            lambda: media_service.prepare_asset(db, asset_id=asset_id, user_id="stranger"),
        ):
            with pytest.raises(NotFoundError):
                call()
        assert db.get(Asset, asset_id).status == "needs_review"


def test_successful_http_download_removes_staging_but_keeps_source(workspace, client, tmp_path, monkeypatch):
    factory, workspace_id, *_ = workspace
    source = tmp_path / "source.png"
    source.write_bytes(b"image")
    asset_id = seed_asset(factory, workspace_id, source)
    staging = tmp_path / "staging"
    staging.mkdir()
    monkeypatch.setattr(file_io, "temporary_directory", lambda prefix: staging)
    response = client.post(f"/api/v1/workspaces/{workspace_id}/assets/download", json={"assetIds": [asset_id]})
    assert response.status_code == 200
    assert response.content == b"image"
    assert not staging.exists()
    assert source.read_bytes() == b"image"


@pytest.mark.parametrize("fail_at", ["download", "zip"])
def test_failed_download_preparation_removes_all_staging(workspace, tmp_path, monkeypatch, fail_at):
    factory, workspace_id, owner, *_ = workspace
    source = tmp_path / "image.png"
    source.write_bytes(b"image")
    ids = [seed_asset(factory, workspace_id, source, scene=number) for number in (1, 2)]
    staging = tmp_path / "staging"
    staging.mkdir()
    monkeypatch.setattr(file_io, "temporary_directory", lambda prefix: staging)
    def fail(*args, **kwargs):
        raise storage.ObjectStorageError("unavailable") if fail_at == "download" else OSError("zip failed")
    if fail_at == "download":
        monkeypatch.setattr(storage, "download_reference", fail)
        error = NotFoundError
    else:
        monkeypatch.setattr(file_io, "create_archive", fail)
        error = OSError
    with factory() as db, pytest.raises(error):
        asset_service.prepare_download(db, workspace_id=workspace_id, asset_ids=ids, user_id=owner)
    assert not staging.exists()
    assert source.exists()


def test_second_cloud_upload_failure_compensates_first_object(workspace, monkeypatch):
    factory, workspace_id, owner, uploads, *_ = workspace
    calls, deleted = [], []
    def persist(source, key, content_type):
        calls.append(key)
        if len(calls) == 2:
            raise storage.ObjectStorageError("audio upload failed")
        return "storage://" + key
    monkeypatch.setattr(storage, "persist_file", persist)
    monkeypatch.setattr(storage, "delete_reference", deleted.append)
    with pytest.raises(UpstreamError, match="subtitle or audio storage temporarily unavailable"):
        subtitle_service.upload_bundle(
            workspace_id=workspace_id, user_id=owner,
            subtitle=subtitle_service.UploadPart("voice.srt", io.BytesIO(SRT)),
            audio=subtitle_service.UploadPart("voice.mp3", io.BytesIO(b"audio")),
        )
    assert deleted == ["storage://" + calls[0]]
    assert list(uploads.iterdir()) == []
    with factory() as db:
        assert db.query(SubtitleFile).count() == 0


def test_subtitle_database_failure_rolls_back_and_deletes_uploaded_reference(workspace, monkeypatch):
    factory, workspace_id, owner, uploads, *_ = workspace
    deleted = []
    monkeypatch.setattr(storage, "persist_file", lambda source, key, content_type: "storage://" + key)
    monkeypatch.setattr(storage, "delete_reference", deleted.append)
    original = subtitle_service.subtitle_repo.add
    def fail_after_flush(db, **values):
        original(db, **values)
        raise RuntimeError("write failed after flush")
    monkeypatch.setattr(subtitle_service.subtitle_repo, "add", fail_after_flush)
    with pytest.raises(RuntimeError, match="after flush"):
        subtitle_service.upload_subtitle(workspace_id=workspace_id, user_id=owner,
            part=subtitle_service.UploadPart("voice.srt", io.BytesIO(SRT)))
    assert len(deleted) == 1 and deleted[0].startswith("storage://uploads/")
    assert list(uploads.iterdir()) == []
    with factory() as db:
        assert db.query(SubtitleFile).count() == 0


@pytest.mark.parametrize("payload,error,message", [
    (b"", ValidationError, "配音文件为空"),
    (b"1234", TooLargeError, "配音文件不能超过 512MB"),
])
def test_audio_validation_removes_partial_local_files(workspace, monkeypatch, payload, error, message):
    factory, workspace_id, owner, uploads, *_ = workspace
    monkeypatch.setattr(subtitle_service, "MAX_AUDIO_BYTES", 3)
    with pytest.raises(error, match=message):
        subtitle_service.upload_bundle(workspace_id=workspace_id, user_id=owner,
            subtitle=subtitle_service.UploadPart("voice.srt", io.BytesIO(SRT)),
            audio=subtitle_service.UploadPart("voice.mp3", io.BytesIO(payload)))
    assert list(uploads.iterdir()) == []
    with factory() as db:
        assert db.query(SubtitleFile).count() == 0


def test_subtitle_size_limit_has_no_files_or_rows(workspace, monkeypatch):
    factory, workspace_id, owner, uploads, *_ = workspace
    monkeypatch.setattr(subtitle_service, "MAX_SUBTITLE_BYTES", 3)
    with pytest.raises(TooLargeError, match="字幕文件不能超过 10MB"):
        subtitle_service.upload_subtitle(workspace_id=workspace_id, user_id=owner,
            part=subtitle_service.UploadPart("voice.srt", io.BytesIO(b"1234")))
    assert list(uploads.iterdir()) == []
    with factory() as db:
        assert db.query(SubtitleFile).count() == 0


def test_upload_checks_owner_before_reading_stream(workspace):
    _, workspace_id, _, uploads, *_ = workspace
    class Unreadable:
        def read(self, size):
            pytest.fail("foreign workspace upload consumed input")
    with pytest.raises(NotFoundError, match="找不到工作区"):
        subtitle_service.upload_subtitle(workspace_id=workspace_id, user_id="stranger",
            part=subtitle_service.UploadPart("voice.srt", Unreadable()))
    assert list(uploads.iterdir()) == []


def test_http_upload_creates_database_session_in_worker_thread(workspace, client, monkeypatch):
    _, workspace_id, *_ = workspace
    threads = []
    original = subtitle_service.subtitle_repo.add
    def capture(db, **values):
        threads.append(threading.get_ident())
        return original(db, **values)
    monkeypatch.setattr(subtitle_service.subtitle_repo, "add", capture)
    main_thread = threading.get_ident()
    response = client.post(f"/api/v1/workspaces/{workspace_id}/subtitle-files",
                           files={"file": ("voice.srt", SRT, "application/x-subrip")})
    assert response.status_code == 200
    assert response.json()["cueCount"] == 1
    assert threads and all(thread != main_thread for thread in threads)


def test_busy_upload_keeps_http_contract(workspace, client, monkeypatch):
    _, workspace_id, *_ = workspace
    monkeypatch.setattr(subtitle_service, "acquire_upload_slot", lambda: False)
    response = client.post(f"/api/v1/workspaces/{workspace_id}/subtitle-files",
                           files={"file": ("voice.srt", SRT, "application/x-subrip")})
    assert response.status_code == 503
    assert response.json() == {"detail": "上传服务正忙，请稍后重试"}


@pytest.mark.parametrize("prefix", ["legacy", "owned", "foreign"])
def test_private_asset_checks_cloud_prefix_and_keeps_private_headers(workspace, tmp_path, monkeypatch, prefix):
    factory, workspace_id, owner, *_ = workspace
    key = {"legacy": f"assets/{workspace_id}/image.png",
           "owned": f"assets/{owner}/{workspace_id}/image.png",
           "foreign": "assets/someone/another-workspace/image.png"}[prefix]
    asset_id = seed_asset(factory, workspace_id, "storage://" + key, filename="image.png")
    calls = []
    def download(reference, destination):
        calls.append(reference)
        destination.write_bytes(b"image")
        return destination
    monkeypatch.setattr(storage, "download_reference", download)
    with factory() as db:
        if prefix == "foreign":
            with pytest.raises(NotFoundError):
                media_service.prepare_asset(db, asset_id=asset_id, user_id=owner)
            assert not calls
        else:
            prepared = media_service.prepare_asset(db, asset_id=asset_id, user_id=owner)
            try:
                assert prepared.path.read_bytes() == b"image"
                assert prepared.headers == {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}
                assert prepared.filename is None
            finally:
                file_io.remove_directory(prepared.cleanup_directory)


def test_preview_outside_preview_root_is_rejected(workspace, tmp_path):
    factory, workspace_id, owner, *_ = workspace
    outside = tmp_path / "private.png"
    outside.write_bytes(b"private")
    with factory() as db:
        thread = AgentThread(workspace_id=workspace_id)
        db.add(thread)
        db.flush()
        db.add(AgentAction(thread_id=thread.id, action_type="graph_confirm_image_style",
                           payload={"previewAssets": [{"objectPath": str(outside)}]}))
        db.commit()
        job_id = seed_preview(factory, workspace_id, str(outside), outside.name)
        with pytest.raises(NotFoundError):
            media_service.prepare_preview(db, job_id=job_id, preview_index=0, user_id=owner)


@pytest.mark.parametrize("display_name", [None, "scene-1-generated.png"])
@pytest.mark.parametrize("prefix", ["legacy", "owned", "foreign"])
def test_preview_cloud_prefix_authorizes_only_own_workspace(workspace, monkeypatch, prefix, display_name):
    factory, workspace_id, owner, *_ = workspace
    reference = "storage://" + {
        "legacy": f"previews/{workspace_id}/preview.png",
        "owned": f"previews/{owner}/{workspace_id}/preview.png",
        "foreign": "previews/other-owner/other-workspace/preview.png",
    }[prefix]
    with factory() as db:
        thread = AgentThread(workspace_id=workspace_id)
        db.add(thread)
        db.flush()
        db.add(AgentAction(thread_id=thread.id, action_type="graph_confirm_image_style",
                           payload={"previewAssets": [{"objectPath": reference, "fileName": display_name}]}))
        db.commit()
    job_id = seed_preview(factory, workspace_id, reference, display_name or "preview.png")
    calls = []
    def download(ref, destination):
        calls.append(ref)
        destination.write_bytes(b"preview")
        return destination
    monkeypatch.setattr(storage, "download_reference", download)
    with factory() as db:
        if prefix == "foreign":
            with pytest.raises(NotFoundError):
                media_service.prepare_preview(db, job_id=job_id, preview_index=0, user_id=owner)
            assert calls == []
        else:
            prepared = media_service.prepare_preview(db, job_id=job_id, preview_index=0, user_id=owner)
            try:
                assert prepared.path.read_bytes() == b"preview"
                assert calls == [reference]
            finally:
                file_io.remove_directory(prepared.cleanup_directory)


def test_preview_display_filename_resolves_oss_object_over_http(workspace, client, monkeypatch):
    factory, workspace_id, owner, *_ = workspace
    reference = f"storage://previews/{owner}/{workspace_id}/jobs/job-1/preview-0.jpg"
    filename = "scene-1-generated.jpg"
    with factory() as db:
        thread = AgentThread(workspace_id=workspace_id)
        db.add(thread)
        db.flush()
        db.add(AgentAction(thread_id=thread.id, action_type="graph_confirm_image_style", payload={
            "previewAssets": [{"fileName": filename, "objectPath": reference}],
        }))
        db.commit()
    job_id = seed_preview(factory, workspace_id, reference, filename)
    downloaded = []
    staged = []
    def download(ref, destination):
        downloaded.append(ref)
        staged.append(destination)
        destination.write_bytes(b"preview-jpeg")
        return destination
    monkeypatch.setattr(storage, "download_reference", download)
    response = client.get(f"/api/v1/jobs/{job_id}/previews/0/content")
    assert response.status_code == 200
    assert response.content == b"preview-jpeg"
    assert response.headers["content-type"] == "image/jpeg"
    assert response.headers["cache-control"] == "private, no-store"
    assert downloaded == [reference]
    assert not staged[0].parent.exists()
    client.post("/api/v1/auth/logout")
    client.post("/api/v1/auth/register", json={
        "email": "preview-stranger@example.com", "password": "stranger-password-123",
    })
    assert client.get(f"/api/v1/jobs/{job_id}/previews/0/content").status_code == 404
    assert downloaded == [reference]


def test_cloud_export_http_cleanup_and_unavailable_contract(workspace, client, tmp_path, monkeypatch):
    factory, workspace_id, *_ = workspace
    with factory() as db:
        job = Job(workspace_id=workspace_id, job_type="video_generation")
        db.add(job)
        db.flush()
        export = ExportFile(job_id=job.id, file_name="draft.json", object_path="storage://exports/draft.json")
        db.add(export)
        db.commit()
        export_id = export.id
    staging = tmp_path / "staging"
    staging.mkdir()
    monkeypatch.setattr(file_io, "temporary_directory", lambda prefix: staging)
    def download(reference, destination):
        destination.write_bytes(b'{"ok":true}')
        return destination
    monkeypatch.setattr(storage, "download_reference", download)
    response = client.get(f"/api/v1/exports/{export_id}/download")
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert response.headers["content-type"] == "application/json"
    assert not staging.exists()
    staging.mkdir()
    def unavailable(*args):
        raise storage.ObjectStorageError("offline")
    monkeypatch.setattr(storage, "download_reference", unavailable)
    response = client.get(f"/api/v1/exports/{export_id}/download")
    assert response.status_code == 404
    assert response.json() == {"detail": "导出文件暂时不可用"}
    assert not staging.exists()
