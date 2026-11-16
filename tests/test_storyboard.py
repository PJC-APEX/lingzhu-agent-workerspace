import json

import pytest

from app.services import model_gateway
from app.services.storyboard import StoryboardOutputError, generate_storyboard


def test_generate_storyboard_uses_selected_model_and_returns_scenes(monkeypatch):
    source = "第一段文案。\n\n第二段文案。"
    captured = {}
    model_response = {
        "scenes": [
            {"sceneNumber": 10, "narration": "第一段文案。", "imagePrompt": "清晨的街道，人物走入画面。"},
            {"sceneNumber": 20, "narration": "第二段文案。", "imagePrompt": "室内近景，人物继续讲述。"},
        ]
    }

    def fake_complete(messages, model_id, temperature, response_format=None):
        captured["messages"] = messages
        captured["model_id"] = model_id
        captured["temperature"] = temperature
        captured["response_format"] = response_format
        return model_gateway.ModelResult(text=json.dumps(model_response, ensure_ascii=False), model_id=model_id, mode="live")

    monkeypatch.setattr("app.services.storyboard.complete_chat", fake_complete)

    result = generate_storyboard(source, "qwen3.7-plus")

    assert captured["model_id"] == "qwen3.7-plus"
    assert captured["temperature"] == 0.2
    assert captured["response_format"] == {"type": "json_object"}
    assert source in captured["messages"][1]["content"]
    assert result.mode == "live"
    assert result.model_id == "qwen3.7-plus"
    assert result.scenes == [
        {"sceneNumber": 1, "narration": "第一段文案。", "imagePrompt": "清晨的街道，人物走入画面。"},
        {"sceneNumber": 2, "narration": "第二段文案。", "imagePrompt": "室内近景，人物继续讲述。"},
    ]


def test_generate_storyboard_accepts_json_code_fence(monkeypatch):
    monkeypatch.setattr(
        "app.services.storyboard.complete_chat",
        lambda *args, **kwargs: model_gateway.ModelResult(
            text='```json\n{"scenes":[{"narration":"完整文案。","imagePrompt":"一幅画面"}]}\n```',
            model_id=kwargs["model_id"],
            mode="live",
        ),
    )

    result = generate_storyboard("完整文案。", "deepseek-v4-flash")

    assert result.scenes[0]["narration"] == "完整文案。"


@pytest.mark.parametrize(
    "model_text",
    [
        "这是一段说明，不是 JSON",
        '{"scenes":[]}',
        '{"scenes":[{"narration":"被改写的文案。","imagePrompt":"画面"}]}',
        '{"scenes":[{"narration":"完整文案。","imagePrompt":""}]}',
    ],
)
def test_generate_storyboard_rejects_invalid_or_incomplete_results(monkeypatch, model_text):
    monkeypatch.setattr(
        "app.services.storyboard.complete_chat",
        lambda *args, **kwargs: model_gateway.ModelResult(
            text=model_text,
            model_id=kwargs["model_id"],
            mode="live",
        ),
    )

    with pytest.raises(StoryboardOutputError):
        generate_storyboard("完整文案。", "deepseek-v4-flash")


def test_generate_storyboard_retries_invalid_output_once(monkeypatch):
    responses = iter((
        '{"scenes":[{"narration":"错误旁白。","imagePrompt":"一幅画面"}]}',
        '{"scenes":[{"narration":"完整文案。","imagePrompt":"月光下的邮局"}]}',
    ))
    calls = []

    def fake_complete(messages, **kwargs):
        calls.append((messages, kwargs))
        return model_gateway.ModelResult(
            text=next(responses),
            model_id=kwargs["model_id"],
            mode="live",
        )

    monkeypatch.setattr("app.services.storyboard.complete_chat", fake_complete)

    result = generate_storyboard("完整文案。", "deepseek-v4-flash")

    assert len(calls) == 2
    assert all(call[1]["response_format"] == {"type": "json_object"} for call in calls)
    assert result.scenes[0]["narration"] == "完整文案。"
    assert "未通过校验" in calls[1][0][-1]["content"]


@pytest.mark.parametrize("text, model_id", [("  ", "deepseek-v4-flash"), ("文案", "")])
def test_generate_storyboard_requires_text_and_model(text, model_id):
    with pytest.raises(ValueError):
        generate_storyboard(text, model_id)


def test_revise_storyboard_scene_requests_json_mode_and_retries_schema_failure(monkeypatch):
    from app.services.storyboard import revise_storyboard_scene

    responses = iter((
        '{"imagePrompt":""}',
        '{"imagePrompt":"夜晚的邮局，温暖灯光。"}',
    ))
    calls = []

    def fake_complete(messages, **kwargs):
        calls.append((messages, kwargs))
        return model_gateway.ModelResult(text=next(responses), model_id=kwargs["model_id"], mode="live")

    monkeypatch.setattr("app.services.storyboard.complete_chat", fake_complete)
    result = revise_storyboard_scene(
        [{"sceneNumber": 1, "narration": "完整文案。", "imagePrompt": "白天的邮局。"}],
        1,
        "改成夜晚",
        "deepseek-v4-flash",
    )

    assert len(calls) == 2
    assert all(call[1]["response_format"] == {"type": "json_object"} for call in calls)
    assert result[0]["imagePrompt"] == "夜晚的邮局，温暖灯光。"
