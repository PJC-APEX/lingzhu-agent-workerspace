from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from difflib import SequenceMatcher

logger = logging.getLogger(__name__)

_TIMESTAMP_PATTERN = re.compile(r"^(\d+):(\d{2}):(\d{2})[,.](\d{1,3})$")
_MIN_MATCH_RATIO = 0.55


class AlignmentError(ValueError):
    """SRT 与分镜内容或时间无法可靠配对。"""


@dataclass(frozen=True)
class SubtitleCue:
    index: int
    start: str
    end: str
    start_ms: int
    end_ms: int
    text: str
    scene_number: int | None = None
    match_score: float = 0.0


@dataclass(frozen=True)
class AlignedScene:
    scene_number: int
    narration: str
    image_prompt: str
    start: str
    end: str
    start_ms: int
    end_ms: int
    subtitles: list[SubtitleCue]
    match_score: float


@dataclass(frozen=True)
class AlignmentResult:
    scenes: list[AlignedScene]
    subtitles: list[SubtitleCue]
    match_ratio: float


def timestamp_to_ms(value: str) -> int:
    match = _TIMESTAMP_PATTERN.fullmatch(value.strip())
    if not match:
        raise AlignmentError(f"SRT 时间格式无效：{value}")
    hours, minutes, seconds, milliseconds = match.groups()
    minute_value = int(minutes)
    second_value = int(seconds)
    if minute_value > 59 or second_value > 59:
        raise AlignmentError(f"SRT 时间超出范围：{value}")
    millis_value = int(milliseconds.ljust(3, "0"))
    return ((int(hours) * 60 + minute_value) * 60 + second_value) * 1000 + millis_value


def _normalise_text(value: str) -> str:
    return "".join(char.casefold() for char in value if char.isalnum())


def validate_subtitle_cues(cues: list[dict]) -> list[SubtitleCue]:
    """检查 SRT 时间格式、先后顺序和每条字幕内容。"""
    if not cues:
        raise AlignmentError("SRT 文件里没有可用字幕")

    parsed: list[SubtitleCue] = []
    previous_start = -1
    for index, cue in enumerate(cues, start=1):
        if not isinstance(cue, dict):
            raise AlignmentError(f"第 {index} 条 SRT 字幕格式无效")
        text = str(cue.get("text") or "").strip()
        start = str(cue.get("start") or "").strip()
        end = str(cue.get("end") or "").strip()
        if not text or not _normalise_text(text):
            raise AlignmentError(f"第 {index} 条 SRT 字幕没有有效文字")
        start_ms = timestamp_to_ms(start)
        end_ms = timestamp_to_ms(end)
        if end_ms <= start_ms:
            raise AlignmentError(f"第 {index} 条 SRT 字幕结束时间必须晚于开始时间")
        if start_ms < previous_start:
            raise AlignmentError(f"第 {index} 条 SRT 字幕开始时间早于上一条，请检查 SRT 顺序")
        previous_start = start_ms
        parsed.append(SubtitleCue(index, start, end, start_ms, end_ms, text))
    return parsed


def _map_subtitle_position(position: int, opcodes: list[tuple], source_length: int) -> int:
    for tag, cue_start, cue_end, source_start, source_end in opcodes:
        if cue_start <= position <= cue_end:
            if tag == "equal":
                return source_start + min(position - cue_start, source_end - source_start)
            if tag == "replace" and cue_end > cue_start:
                ratio = (position - cue_start) / (cue_end - cue_start)
                return round(source_start + ratio * (source_end - source_start))
            return source_start
    return source_length


def _matched_characters(start: int, end: int, opcodes: list[tuple]) -> int:
    matched = 0
    for tag, cue_start, cue_end, _, _ in opcodes:
        if tag == "equal":
            matched += max(0, min(end, cue_end) - max(start, cue_start))
    return matched


def align_scenes_to_subtitles(scenes: list[dict], cues: list[dict]) -> AlignmentResult:
    """按文案顺序把连续 SRT 字幕分配到分镜，不拆分任何字幕条目。"""
    if not scenes:
        raise AlignmentError("没有可用于配对的分镜")
    parsed_cues = validate_subtitle_cues(cues)

    scene_ranges: list[tuple[int, int]] = []
    scene_texts: list[str] = []
    scene_numbers: list[int] = []
    scene_narrations: list[str] = []
    scene_prompts: list[str] = []
    cursor = 0
    previous_scene_number = 0
    for index, scene in enumerate(scenes, start=1):
        narration = str(scene.get("narration") or "").strip()
        normalised = _normalise_text(narration)
        if not normalised:
            raise AlignmentError(f"第 {index} 个分镜缺少对应文案")
        scene_number = int(scene.get("sceneNumber", index))
        if scene_number <= previous_scene_number:
            raise AlignmentError("分镜序号必须按递增顺序排列")
        previous_scene_number = scene_number
        scene_ranges.append((cursor, cursor + len(normalised)))
        scene_texts.append(normalised)
        scene_numbers.append(scene_number)
        scene_narrations.append(narration)
        scene_prompts.append(str(scene.get("imagePrompt") or ""))
        cursor += len(normalised)

    source_text = "".join(scene_texts)
    cue_ranges: list[tuple[int, int]] = []
    cue_texts: list[str] = []
    cursor = 0
    for cue in parsed_cues:
        normalised = _normalise_text(cue.text)
        cue_ranges.append((cursor, cursor + len(normalised)))
        cue_texts.append(normalised)
        cursor += len(normalised)
    subtitle_text = "".join(cue_texts)

    matcher = SequenceMatcher(None, subtitle_text, source_text, autojunk=False)
    opcodes = matcher.get_opcodes()
    equal_chars = sum(cue_end - cue_start for tag, cue_start, cue_end, _, _ in opcodes if tag == "equal")
    match_ratio = (2 * equal_chars / (len(subtitle_text) + len(source_text))) if subtitle_text or source_text else 0.0
    if match_ratio < _MIN_MATCH_RATIO:
        logger.warning(
            "event=subtitle_alignment.low_match ratio=%.3f srt_chars=%s narration_chars=%s",
            match_ratio,
            len(subtitle_text),
            len(source_text),
        )
        raise AlignmentError(
            f"SRT 与分镜文案匹配度过低（{match_ratio:.0%}），未生成时间线；请确认字幕对应当前最终文案"
        )

    assigned: list[list[SubtitleCue]] = [[] for _ in scenes]
    aligned_cues: list[SubtitleCue] = []
    previous_scene_index = 0
    for cue, (cue_start, cue_end) in zip(parsed_cues, cue_ranges):
        source_start = _map_subtitle_position(cue_start, opcodes, len(source_text))
        source_end = _map_subtitle_position(cue_end, opcodes, len(source_text))
        if source_end < source_start:
            source_start, source_end = source_end, source_start
        midpoint = (source_start + source_end) / 2

        overlap_scores = [
            max(0, min(source_end, scene_end) - max(source_start, scene_start))
            for scene_start, scene_end in scene_ranges
        ]
        best_overlap = max(overlap_scores, default=0)
        if best_overlap:
            candidates = [index for index, score in enumerate(overlap_scores) if score == best_overlap]
            scene_index = min(
                candidates,
                key=lambda index: abs(midpoint - (scene_ranges[index][0] + scene_ranges[index][1]) / 2),
            )
        else:
            scene_index = next(
                (index for index, (start, end) in enumerate(scene_ranges) if start <= midpoint <= end),
                len(scene_ranges) - 1,
            )
        scene_index = max(previous_scene_index, scene_index)
        previous_scene_index = scene_index

        matched = _matched_characters(cue_start, cue_end, opcodes)
        cue_score = matched / max(1, cue_end - cue_start)
        aligned_cue = SubtitleCue(
            index=cue.index,
            start=cue.start,
            end=cue.end,
            start_ms=cue.start_ms,
            end_ms=cue.end_ms,
            text=cue.text,
            scene_number=scene_numbers[scene_index],
            match_score=cue_score,
        )
        aligned_cues.append(aligned_cue)
        assigned[scene_index].append(aligned_cue)

    aligned_scenes: list[AlignedScene] = []
    missing_scenes: list[int] = []
    for index, scene_cues in enumerate(assigned):
        if not scene_cues:
            missing_scenes.append(scene_numbers[index])
            continue
        start_ms = scene_cues[0].start_ms
        end_ms = max(cue.end_ms for cue in scene_cues)
        if end_ms <= start_ms:
            raise AlignmentError(f"分镜 {scene_numbers[index]} 对应的时间段无效")
        score = sum(cue.match_score for cue in scene_cues) / len(scene_cues)
        aligned_scenes.append(AlignedScene(
            scene_number=scene_numbers[index],
            narration=scene_narrations[index],
            image_prompt=scene_prompts[index],
            start=scene_cues[0].start,
            end=max(scene_cues, key=lambda cue: cue.end_ms).end,
            start_ms=start_ms,
            end_ms=end_ms,
            subtitles=scene_cues,
            match_score=score,
        ))

    if missing_scenes:
        missing = "、".join(str(number) for number in missing_scenes)
        raise AlignmentError(f"以下分镜没有匹配到字幕时间，未生成时间线：{missing}")

    logger.info(
        "event=subtitle_alignment.completed scenes=%s cues=%s match_ratio=%.3f",
        len(aligned_scenes),
        len(aligned_cues),
        match_ratio,
    )
    return AlignmentResult(aligned_scenes, aligned_cues, match_ratio)
