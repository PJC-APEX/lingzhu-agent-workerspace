from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from app.services.aligner import align_scenes_to_subtitles
from app.services.jianying_draft import JianyingDraftExportError, create_jianying_draft


def test_create_jianying_draft_places_images_and_imports_srt(monkeypatch, tmp_path):
    image1 = tmp_path / "scene-1.png"
    image2 = tmp_path / "scene-2.png"
    image1.write_bytes(b"image-1")
    image2.write_bytes(b"image-2")
    subtitle = tmp_path / "voice.srt"
    subtitle.write_text("sample", encoding="utf-8")
    audio = tmp_path / "voice.mp3"
    audio.write_bytes(b"audio")
    alignment = align_scenes_to_subtitles(
        [
            {"sceneNumber": 1, "narration": "小林走进院子。", "imagePrompt": "院子"},
            {"sceneNumber": 2, "narration": "月光落在石阶上。", "imagePrompt": "月光"},
        ],
        [
            {"start": "00:00:00,000", "end": "00:00:02,000", "text": "小林走进院子。"},
            {"start": "00:00:02,000", "end": "00:00:04,500", "text": "月光落在石阶上。"},
        ],
    )
    calls = {"segments": [], "audio": None, "tracks": [], "srt": None, "saved": False}

    class FakeScript:
        def append_tracks(self, tracks):
            calls["tracks"].extend(tracks)

        def add_segment(self, segment, track):
            if track == "配音":
                calls["audio"] = (segment, track)
            else:
                calls["segments"].append((segment, track))

        def import_srt(self, path, track_name):
            calls["srt"] = (path, track_name)

        def save(self):
            calls["saved"] = True

    class FakeDraftFolder:
        def __init__(self, path):
            calls["folder"] = path

        def create_draft(self, name, width, height):
            calls["draft"] = (name, width, height)
            return FakeScript()

    fake_draft = ModuleType("pyJianYingDraft")
    fake_draft.DraftFolder = FakeDraftFolder
    fake_draft.TrackSpec = lambda kind, name: (kind, name)
    fake_draft.TrackType = SimpleNamespace(audio="audio", video="video")
    fake_draft.AudioMaterial = lambda path: SimpleNamespace(path=path, duration=4_500_000)
    fake_draft.AudioSegment = lambda material, timerange: (material.path, timerange)
    fake_draft.trange = lambda start, end: (start, end)
    fake_draft.VideoSegment = lambda path, timerange: (path, timerange)
    fake_draft.trange_seconds = lambda start, duration: (start, duration)
    monkeypatch.setitem(sys.modules, "pyJianYingDraft", fake_draft)

    result = create_jianying_draft(
        alignment,
        [
            {"sceneNumber": 2, "fileName": image2.name, "objectPath": str(image2)},
            {"sceneNumber": 1, "fileName": image1.name, "objectPath": str(image1)},
        ],
        subtitle,
        audio,
        tmp_path / "drafts",
    )

    assert calls["tracks"] == [("audio", "配音"), ("video", "分镜图片")]
    materials = result.path / "materials"
    assert calls["audio"] == ((str(materials / "voiceover.mp3"), (0, 4_500_000)), "配音")
    assert calls["segments"] == [
        ((str(materials / "scene-0001.png"), (0.0, 2.0)), "分镜图片"),
        ((str(materials / "scene-0002.png"), (2.0, 2.5)), "分镜图片"),
    ]
    assert calls["srt"] == (str(materials / "subtitles.srt"), "字幕")
    assert calls["saved"] is True
    assert result.path == tmp_path / "drafts" / result.name
    # Simulate the caller cleaning its downloaded input directory. Every
    # path handed to the draft SDK must still exist and retain its content.
    for original in (image1, image2, subtitle, audio):
        original.unlink()
    assert Path(calls["audio"][0][0]).read_bytes() == b"audio"
    assert Path(calls["segments"][0][0][0]).read_bytes() == b"image-1"
    assert Path(calls["segments"][1][0][0]).read_bytes() == b"image-2"
    assert Path(calls["srt"][0]).read_text(encoding="utf-8") == "sample"


def test_create_jianying_draft_rejects_missing_scene_image(tmp_path):
    subtitle = tmp_path / "voice.srt"
    subtitle.write_text("sample", encoding="utf-8")
    alignment = align_scenes_to_subtitles(
        [{"sceneNumber": 1, "narration": "小林走进院子。", "imagePrompt": "院子"}],
        [{"start": "00:00:00,000", "end": "00:00:02,000", "text": "小林走进院子。"}],
    )

    with pytest.raises(JianyingDraftExportError, match="缺少分镜图片"):
        create_jianying_draft(alignment, [], subtitle, tmp_path / "voice.mp3", tmp_path / "drafts")
