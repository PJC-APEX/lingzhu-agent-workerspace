import pytest

from app.services.aligner import AlignmentError, align_scenes_to_subtitles, timestamp_to_ms, validate_subtitle_cues


def _scenes():
    return [
        {"sceneNumber": 1, "narration": "小林清早走进院子，发现花开了。", "imagePrompt": "清晨院子"},
        {"sceneNumber": 2, "narration": "他拿起水壶，给花浇水。", "imagePrompt": "人物浇花"},
    ]


def _cue(index, start, end, text):
    return {"index": index, "start": start, "end": end, "text": text}


def test_aligns_consecutive_srt_cues_to_scene_by_narration_order():
    cues = [
        _cue(1, "00:00:00,000", "00:00:01,000", "小林清早走进"),
        _cue(2, "00:00:01,000", "00:00:02,000", "院子，发现花开了。"),
        _cue(3, "00:00:02,000", "00:00:03,000", "他拿起水壶，"),
        _cue(4, "00:00:03,000", "00:00:04,000", "给花浇水。"),
    ]

    result = align_scenes_to_subtitles(_scenes(), cues)

    assert result.match_ratio == 1
    assert [(scene.scene_number, scene.start_ms, scene.end_ms) for scene in result.scenes] == [
        (1, 0, 2000),
        (2, 2000, 4000),
    ]
    assert [[cue.index for cue in scene.subtitles] for scene in result.scenes] == [[1, 2], [3, 4]]
    assert [cue.scene_number for cue in result.subtitles] == [1, 1, 2, 2]


def test_cross_scene_subtitle_is_assigned_whole_to_closest_scene():
    cues = [
        _cue(1, "00:00:00,000", "00:00:01,000", "小林清早走进院子，"),
        _cue(2, "00:00:01,000", "00:00:03,000", "发现花开了他拿起水壶，"),
        _cue(3, "00:00:03,000", "00:00:04,000", "给花浇水。"),
    ]

    result = align_scenes_to_subtitles(_scenes(), cues)

    crossing_cue = result.subtitles[1]
    assert crossing_cue.scene_number in {1, 2}
    assert any(crossing_cue is cue for scene in result.scenes for cue in scene.subtitles)
    assert sum(cue.index == crossing_cue.index for scene in result.scenes for cue in scene.subtitles) == 1


def test_long_chinese_alignment_keeps_frequent_characters_in_match_score():
    narration = "小刺猬每晚都到月光邮局工作。" * 8 + "小刺猬每晚都在森林休息。" * 8
    spoken_text = "小刺猬每晚都到月光邮局工作。" * 20

    result = align_scenes_to_subtitles(
        [{"sceneNumber": 1, "narration": narration, "imagePrompt": "月光邮局"}],
        [_cue(1, "00:00:00,000", "00:00:20,000", spoken_text)],
    )

    assert result.match_ratio > 0.55
    assert [scene.scene_number for scene in result.scenes] == [1]
    assert len(result.scenes[0].subtitles) == 1


@pytest.mark.parametrize(
    "value, expected",
    [
        ("00:00:01,250", 1250),
        ("01:02:03.4", 3_723_400),
    ],
)
def test_timestamp_to_milliseconds(value, expected):
    assert timestamp_to_ms(value) == expected


def test_validate_subtitle_cues_rejects_bad_time_order_and_ranges():
    with pytest.raises(AlignmentError, match="格式无效"):
        validate_subtitle_cues([_cue(1, "bad", "00:00:01,000", "文字")])
    with pytest.raises(AlignmentError, match="结束时间"):
        validate_subtitle_cues([_cue(1, "00:00:01,000", "00:00:01,000", "文字")])
    with pytest.raises(AlignmentError, match="早于上一条"):
        validate_subtitle_cues([
            _cue(1, "00:00:02,000", "00:00:03,000", "第一条"),
            _cue(2, "00:00:01,000", "00:00:02,000", "第二条"),
        ])


def test_alignment_rejects_low_text_match_and_scenes_without_timing():
    with pytest.raises(AlignmentError, match="匹配度过低"):
        align_scenes_to_subtitles(_scenes(), [_cue(1, "00:00:00,000", "00:00:01,000", "完全不同的内容")])

    with pytest.raises(AlignmentError, match="没有匹配到字幕时间"):
        align_scenes_to_subtitles(
            _scenes(),
            [_cue(1, "00:00:00,000", "00:00:02,000", "小林清早走进院子，发现花开了。")],
        )
