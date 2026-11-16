from app.services.normal_rewrite import service as rewrite_model
from app.services import model_gateway
from openai import APIConnectionError, AuthenticationError
import httpx


def test_demo_rewrite_does_not_require_provider_credentials(monkeypatch):
    monkeypatch.setattr(rewrite_model, "model_available", lambda model_id: False)
    text, mode = rewrite_model.rewrite_text("我认为这件事非常重要。", "normal_humanize_v1", "", "deepseek-v4-flash", 55)
    assert text == "在我看来这件事十分重要。"
    assert mode == "demo"


def test_prompt_id_builds_selected_strategy_and_user_prompt(monkeypatch):
    captured = {}
    monkeypatch.setattr(rewrite_model, "model_available", lambda model_id: True)

    def fake_complete(messages, model_id, temperature):
        captured["messages"] = messages
        return model_gateway.ModelResult(text="改写结果", model_id=model_id, mode="live")

    monkeypatch.setattr(rewrite_model, "complete_chat", fake_complete)
    text, mode = rewrite_model.rewrite_text("原文", "normal_deduplicate_v1", "保留口语感", "qwen3.7-plus", 60)

    assert text == "改写结果"
    assert mode == "live"
    assert "当前改写策略：降重改写" in captured["messages"][0]["content"]
    assert "保留口语感" in captured["messages"][1]["content"]


def test_doubao_uses_ark_endpoint_id(monkeypatch):
    monkeypatch.setattr(model_gateway, "DEMO_MODE", False)
    monkeypatch.setenv("ARK_API_KEY", "test-key")
    monkeypatch.setenv("ARK_BASE_URL", "https://ark.example.test/api/v3")
    monkeypatch.setenv("DOUBAO_MODEL", "ep-test-123")

    key, base_url, model = model_gateway._provider_config("doubao-2.1-pro")

    assert key == "test-key"
    assert base_url == "https://ark.example.test/api/v3"
    assert model == "ep-test-123"
    assert model_gateway.model_available("doubao-2.1-pro") is True


def test_doubao_is_unavailable_without_endpoint(monkeypatch):
    monkeypatch.setattr(model_gateway, "DEMO_MODE", False)
    monkeypatch.setenv("ARK_API_KEY", "test-key")
    monkeypatch.delenv("DOUBAO_MODEL", raising=False)

    assert model_gateway.model_available("doubao-2.1-pro") is False


def test_selected_deepseek_and_qwen_ids_are_sent_to_their_providers(monkeypatch):
    monkeypatch.setattr(model_gateway, "DEMO_MODE", False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-test-key")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "qwen-test-key")

    deepseek = model_gateway._provider_config("deepseek-v4-flash")
    qwen = model_gateway._provider_config("qwen3.7-plus")

    assert deepseek[0] == "deepseek-test-key"
    assert deepseek[2] == "deepseek-v4-flash"
    assert qwen[0] == "qwen-test-key"
    assert qwen[2] == "qwen3.7-plus"


def test_model_gateway_retries_transient_failures_three_total_attempts(monkeypatch):
    monkeypatch.setattr(model_gateway.time, "sleep", lambda _: None)
    calls = 0

    def flaky_request():
        nonlocal calls
        calls += 1
        raise APIConnectionError(request=httpx.Request("POST", "https://model.test"))

    try:
        model_gateway._retryable_request(flaky_request, model_id="deepseek-v4-flash")
    except model_gateway.ModelCallError as exc:
        assert exc.category == "connection"
        assert exc.retryable is True
    else:
        raise AssertionError("expected a model failure")

    assert calls == 3


def test_model_gateway_does_not_retry_authentication_failures(monkeypatch):
    monkeypatch.setattr(model_gateway.time, "sleep", lambda _: None)
    calls = 0

    def unauthorized_request():
        nonlocal calls
        calls += 1
        response = httpx.Response(401, request=httpx.Request("POST", "https://model.test"))
        raise AuthenticationError("invalid api key", response=response, body={"error": {"message": "invalid api key"}})

    try:
        model_gateway._retryable_request(unauthorized_request, model_id="deepseek-v4-flash")
    except model_gateway.ModelCallError as exc:
        assert exc.category == "credentials"
        assert exc.retryable is False
    else:
        raise AssertionError("expected a model failure")

    assert calls == 1


