from __future__ import annotations

import json
import logging
from pathlib import Path

from app.services.aligner import AlignmentResult

logger = logging.getLogger(__name__)


class TimelineExportError(ValueError):
    """导出时间线时图片和分镜无法完整对应。"""


def build_timeline(alignment: AlignmentResult, assets: list[dict]) -> dict:
    """生成灵珠时间线 JSON；字段结构尚未按剪映小助手样例验证。"""
    assets_by_scene: dict[int, dict] = {}
    for asset in assets:
        scene_number = int(asset.get("sceneNumber", 0))
        if scene_number < 1 or scene_number in assets_by_scene:
            raise TimelineExportError("图片素材分镜序号无效或重复")
        assets_by_scene[scene_number] = asset

    expected = {scene.scene_number for scene in alignment.scenes}
    missing = sorted(expected - assets_by_scene.keys())
    if missing:
        raise TimelineExportError(f"缺少分镜图片：{'、'.join(map(str, missing))}")
    unexpected = sorted(assets_by_scene.keys() - expected)
    if unexpected:
        raise TimelineExportError(f"发现不属于本次分镜的图片：{'、'.join(map(str, unexpected))}")

    video_track = []
    for scene in alignment.scenes:
        asset = assets_by_scene[scene.scene_number]
        image_path = str(asset.get("objectPath") or asset.get("path") or "")
        file_name = str(asset.get("fileName") or "")
        if not image_path or not file_name:
            raise TimelineExportError(f"分镜 {scene.scene_number} 的图片路径信息不完整")
        video_track.append({
            "sceneNumber": scene.scene_number,
            "image": file_name,
            "imagePath": image_path,
            "start": scene.start,
            "end": scene.end,
            "startMs": scene.start_ms,
            "endMs": scene.end_ms,
            "durationMs": scene.end_ms - scene.start_ms,
            "narration": scene.narration,
        })

    subtitle_track = [
        {
            "index": cue.index,
            "sceneNumber": cue.scene_number,
            "text": cue.text,
            "start": cue.start,
            "end": cue.end,
            "startMs": cue.start_ms,
            "endMs": cue.end_ms,
        }
        for cue in alignment.subtitles
    ]
    duration_ms = max(cue.end_ms for cue in alignment.subtitles)
    return {
        "format": "lingzhu-timeline-v1",
        "note": "灵珠时间线数据；剪映小助手导入格式尚未根据样例验证。",
        "durationMs": duration_ms,
        "alignment": {"matchRatio": round(alignment.match_ratio, 4)},
        "videoTrack": video_track,
        "subtitleTrack": subtitle_track,
    }


def write_timeline(path: Path, alignment: AlignmentResult, assets: list[dict]) -> dict:
    timeline = build_timeline(alignment, assets)
    path.write_text(json.dumps(timeline, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(
        "event=timeline.exported path=%s video_clips=%s subtitles=%s duration_ms=%s",
        path.name,
        len(timeline["videoTrack"]),
        len(timeline["subtitleTrack"]),
        timeline["durationMs"],
    )
    return timeline
