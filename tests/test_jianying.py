import json

import pytest

from app.services.aligner import align_scenes_to_subtitles
from app.services.jianying import TimelineExportError, build_timeline, write_timeline


def _alignment():
    scenes = [
        {"sceneNumber": 1, "narration": "小林走进院子。", "imagePrompt": "清晨院子"},
        {"sceneNumber": 2, "narration": "他给花浇水。", "imagePrompt": "人物浇花"},
    ]
    cues = [
        {"start": "00:00:00,000", "end": "00:00:01,000", "text": "小林走进"},
        {"start": "00:00:01,000", "end": "00:00:02,000", "text": "院子。"},
        {"start": "00:00:02,000", "end": "00:00:03,000", "text": "他给花浇水。"},
    ]
    return align_scenes_to_subtitles(scenes, cues)


def _assets():
    return [
        {"sceneNumber": 1, "fileName": "scene-1.png", "objectPath": "D:/assets/scene-1.png"},
        {"sceneNumber": 2, "fileName": "scene-2.png", "objectPath": "D:/assets/scene-2.png"},
    ]


def test_build_timeline_has_scene_image_spans_and_original_subtitle_timings():
    timeline = build_timeline(_alignment(), _assets())

    assert timeline["format"] == "lingzhu-timeline-v1"
    assert len(timeline["videoTrack"]) == 2
    assert timeline["videoTrack"][0]["image"] == "scene-1.png"
    assert timeline["videoTrack"][0]["startMs"] == 0
    assert timeline["videoTrack"][0]["endMs"] == 2000
    assert len(timeline["subtitleTrack"]) == 3
    assert timeline["subtitleTrack"][2]["sceneNumber"] == 2
    assert "尚未根据样例验证" in timeline["note"]


def test_write_timeline_creates_downloadable_json(tmp_path):
    path = tmp_path / "timeline.json"

    timeline = write_timeline(path, _alignment(), _assets())

    assert json.loads(path.read_text(encoding="utf-8")) == timeline


def test_timeline_rejects_missing_or_duplicate_scene_images():
    with pytest.raises(TimelineExportError, match="缺少分镜图片"):
        build_timeline(_alignment(), _assets()[:1])
    with pytest.raises(TimelineExportError, match="重复"):
        build_timeline(_alignment(), _assets() + [_assets()[0]])
    with pytest.raises(TimelineExportError, match="不属于本次分镜"):
        build_timeline(_alignment(), _assets() + [{"sceneNumber": 3, "fileName": "other.png", "path": "D:/assets/other.png"}])
