import json
from urllib.parse import quote

from app.core import config
from app.services import jianying_export
from app.infrastructure import storage as object_storage
from app.services.aligner import align_scenes_to_subtitles


def test_export_uploads_scene_assets_and_returns_helper_link(monkeypatch, tmp_path):
    image_path = tmp_path / "scene-1.png"
    image_path.write_bytes(b"image")
    alignment = align_scenes_to_subtitles(
        [{"sceneNumber": 1, "narration": "小林走进院子。", "imagePrompt": "清晨院子"}],
        [{"start": "00:00:00,000", "end": "00:00:02,000", "text": "小林走进院子。"}],
    )
    uploads = []

    def fake_upload(path, object_key, content_type):
        uploads.append((path, object_key, content_type))
        return f"https://cdn.example/{object_key}"

    monkeypatch.setattr(jianying_export, "EXPORT_DIR", tmp_path / "exports")
    monkeypatch.setattr(object_storage, "is_configured", lambda: True)
    monkeypatch.setattr(object_storage, "upload_file", fake_upload)
    monkeypatch.setattr(config, "JIANYING_HELPER_URL", "https://ts.fyshark.com/#/cozeToJianyin")

    exported = jianying_export.export_timeline(
        alignment,
        [{"sceneNumber": 1, "fileName": image_path.name, "objectPath": str(image_path)}],
        "workspace-1",
    )

    image_url = "https://cdn.example/assets/workspace-1/scene-1.png"
    draft_url = f"https://cdn.example/drafts/workspace-1/{exported.file_name}"
    assert [upload[1:] for upload in uploads] == [
        ("assets/workspace-1/scene-1.png", "image/png"),
        (f"drafts/workspace-1/{exported.file_name}", "application/json"),
    ]
    assert exported.timeline["videoTrack"][0]["imagePath"] == image_url
    assert json.loads(exported.path.read_text(encoding="utf-8"))["videoTrack"][0]["imagePath"] == image_url
    assert exported.draft_url == draft_url
    assert exported.helper_url == f"https://ts.fyshark.com/#/cozeToJianyin?drafId={quote(draft_url, safe='')}"
