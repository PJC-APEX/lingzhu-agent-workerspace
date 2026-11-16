from __future__ import annotations

import importlib
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from app.services.aligner import AlignmentResult

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class JianyingDraftExport:
    name: str
    path: Path


class JianyingDraftExportError(ValueError):
    """The aligned media cannot be assembled into a local Jianying draft."""


def create_jianying_draft(
    alignment: AlignmentResult,
    assets: list[dict],
    subtitle_path: str | Path,
    audio_path: str | Path,
    draft_folder_path: str | Path,
) -> JianyingDraftExport:
    assets_by_scene: dict[int, dict] = {}
    for asset in assets:
        scene_number = int(asset.get("sceneNumber", 0))
        if scene_number < 1 or scene_number in assets_by_scene:
            raise JianyingDraftExportError("图片素材分镜序号无效或重复")
        assets_by_scene[scene_number] = asset

    expected = {scene.scene_number for scene in alignment.scenes}
    missing = sorted(expected - assets_by_scene.keys())
    if missing:
        raise JianyingDraftExportError(f"缺少分镜图片：{'、'.join(map(str, missing))}")
    unexpected = sorted(assets_by_scene.keys() - expected)
    if unexpected:
        raise JianyingDraftExportError(f"发现不属于本次分镜的图片：{'、'.join(map(str, unexpected))}")

    subtitle_file = Path(subtitle_path)
    if not subtitle_file.is_file():
        raise JianyingDraftExportError(f"找不到已上传的 SRT 文件：{subtitle_file}")
    audio_file = Path(audio_path)
    if not audio_file.is_file():
        raise JianyingDraftExportError(f"找不到已上传的配音音频：{audio_file}")

    image_paths: dict[int, Path] = {}
    for scene_number, asset in assets_by_scene.items():
        image_path = Path(str(asset.get("objectPath") or ""))
        if not image_path.is_file():
            raise JianyingDraftExportError(f"分镜 {scene_number} 的图片文件不存在：{image_path}")
        image_paths[scene_number] = image_path

    try:
        draft = importlib.import_module("pyJianYingDraft")
    except ImportError as exc:
        raise JianyingDraftExportError(
            "尚未安装 pyJianYingDraft，请按项目依赖安装后重启后端"
        ) from exc

    draft_folder = Path(draft_folder_path).expanduser().resolve()
    draft_folder.mkdir(parents=True, exist_ok=True)
    draft_name = f"灵珠草稿-{uuid4().hex[:8]}"
    folder = draft.DraftFolder(str(draft_folder))
    script = folder.create_draft(draft_name, 1080, 1920)
    path = draft_folder / draft_name
    materials = path / "materials"
    materials.mkdir(parents=True, exist_ok=False)
    # Draft materials reference absolute paths; OSS inputs may come from a
    # TemporaryDirectory that disappears immediately after this function.
    # Use separate deterministic names to avoid collisions between sources.
    saved_audio = materials / f"voiceover{audio_file.suffix}"
    saved_subtitle = materials / "subtitles.srt"
    shutil.copyfile(audio_file, saved_audio)
    shutil.copyfile(subtitle_file, saved_subtitle)
    for scene_number, image_path in image_paths.items():
        saved_image = materials / f"scene-{scene_number:04d}{image_path.suffix}"
        shutil.copyfile(image_path, saved_image)
        image_paths[scene_number] = saved_image
    script.append_tracks([
        draft.TrackSpec(draft.TrackType.audio, "配音"),
        draft.TrackSpec(draft.TrackType.video, "分镜图片"),
    ])

    audio_material = draft.AudioMaterial(str(saved_audio))
    audio_segment = draft.AudioSegment(
        audio_material,
        draft.trange(0, audio_material.duration),
    )
    script.add_segment(audio_segment, track="配音")

    for scene in alignment.scenes:
        start = scene.start_ms / 1000
        duration = (scene.end_ms - scene.start_ms) / 1000
        segment = draft.VideoSegment(
            str(image_paths[scene.scene_number]),
            draft.trange_seconds(start, duration=duration),
        )
        script.add_segment(segment, track="分镜图片")

    script.import_srt(str(saved_subtitle), track_name="字幕")
    script.save()

    logger.info(
        "event=jianying_draft.created path=%s scenes=%s subtitles=%s match_ratio=%.3f",
        path,
        len(alignment.scenes),
        len(alignment.subtitles),
        alignment.match_ratio,
    )
    return JianyingDraftExport(name=draft_name, path=path)
