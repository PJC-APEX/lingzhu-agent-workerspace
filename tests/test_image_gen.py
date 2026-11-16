import httpx
import pytest

from app.services import image_gen
from app.services.image_gen import ImageGenerationError, generate_scene_image


def test_generate_scene_image_sends_seedream_request_and_saves_download(monkeypatch, tmp_path):
    monkeypatch.setattr(image_gen, "IMAGE_API_KEY", "test-ark-key")
    monkeypatch.setattr(image_gen, "IMAGE_BASE_URL", "https://ark.example.test/api/v3")
    monkeypatch.setattr(image_gen, "IMAGE_MODEL", "doubao-seedream-5-0-flash-260915")
    monkeypatch.setattr(image_gen, "ASSET_DIR", tmp_path)
    calls = {}

    def fake_post(url, *, headers, json, timeout):
        calls["url"] = url
        calls["headers"] = headers
        calls["body"] = json
        calls["timeout"] = timeout
        return httpx.Response(
            200,
            json={"data": [{"url": "https://images.example.test/result.png"}]},
            headers={"x-request-id": "request-123"},
            request=httpx.Request("POST", url),
        )

    def fake_get(url, *, timeout):
        calls["download_url"] = url
        return httpx.Response(
            200,
            content=b"fake-png-bytes",
            headers={"content-type": "image/png"},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(image_gen.httpx, "post", fake_post)
    monkeypatch.setattr(image_gen.httpx, "get", fake_get)

    result = generate_scene_image(3, "雨后的街道，主角撑伞走过路口。")

    assert calls["url"] == "https://ark.example.test/api/v3/images/generations"
    assert calls["headers"]["Authorization"] == "Bearer test-ark-key"
    assert calls["body"] == {
        "model": "doubao-seedream-5-0-flash-260915",
        "prompt": "雨后的街道，主角撑伞走过路口。\n\n画幅要求：16:9 横向构图。",
        "response_format": "url",
        "size": "2K",
        "stream": False,
        "watermark": True,
    }
    assert calls["timeout"] == 180
    assert calls["download_url"] == "https://images.example.test/result.png"
    assert result.scene_number == 3
    assert result.model_id == "doubao-seedream-5-0-flash-260915"
    assert result.prompt.endswith("画幅要求：16:9 横向构图。")
    assert result.request_id == "request-123"
    assert (tmp_path / result.file_name).read_bytes() == b"fake-png-bytes"


def test_generate_scene_image_requires_api_key(monkeypatch):
    monkeypatch.setattr(image_gen, "IMAGE_API_KEY", "")

    with pytest.raises(ImageGenerationError, match="未配置"):
        generate_scene_image(1, "一个场景")


@pytest.mark.parametrize(
    "status_code, error_code, error_message",
    [
        (429, "QuotaExceeded", "rate limit"),
        (500, "InternalError", "model service is temporarily unavailable"),
    ],
)
def test_generate_scene_image_reports_provider_error(monkeypatch, status_code, error_code, error_message):
    monkeypatch.setattr(image_gen, "IMAGE_API_KEY", "test-ark-key")

    def fake_post(url, **kwargs):
        return httpx.Response(
            status_code,
            json={"error": {"code": error_code, "message": error_message}},
            headers={"x-request-id": "failed-request-456"},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(image_gen.httpx, "post", fake_post)

    with pytest.raises(
        ImageGenerationError,
        match=f"HTTP {status_code}.*{error_code}.*failed-request-456.*{error_message}",
    ):
        generate_scene_image(1, "一个场景")


@pytest.mark.parametrize(
    "base_url, expected_url",
    [
        ("https://ark.example.test/api/v3", "https://ark.example.test/api/v3/images/generations"),
        ("https://ark.example.test/api/v3/images/generations", "https://ark.example.test/api/v3/images/generations"),
        ("https://ark.example.test/api/v3/images/generations/", "https://ark.example.test/api/v3/images/generations"),
    ],
)
def test_generation_endpoint_accepts_root_or_full_path(base_url, expected_url):
    assert image_gen._generation_endpoint(base_url) == expected_url


@pytest.mark.parametrize(
    "payload",
    [
        {"data": []},
        {"data": [{"url": "not-a-url"}]},
    ],
)
def test_generate_scene_image_rejects_missing_or_invalid_image_url(monkeypatch, payload):
    monkeypatch.setattr(image_gen, "IMAGE_API_KEY", "test-ark-key")

    def fake_post(url, **kwargs):
        return httpx.Response(200, json=payload, request=httpx.Request("POST", url))

    monkeypatch.setattr(image_gen.httpx, "post", fake_post)

    with pytest.raises(ImageGenerationError, match="图片地址"):
        generate_scene_image(1, "一个场景")


def test_generate_scene_image_rejects_non_image_download(monkeypatch):
    monkeypatch.setattr(image_gen, "IMAGE_API_KEY", "test-ark-key")
    monkeypatch.setattr(
        image_gen.httpx,
        "post",
        lambda url, **kwargs: httpx.Response(
            200,
            json={"data": [{"url": "https://images.example.test/result"}]},
            request=httpx.Request("POST", url),
        ),
    )
    monkeypatch.setattr(
        image_gen.httpx,
        "get",
        lambda url, **kwargs: httpx.Response(
            200,
            content=b"not-an-image",
            headers={"content-type": "text/plain"},
            request=httpx.Request("GET", url),
        ),
    )

    with pytest.raises(ImageGenerationError, match="图片格式"):
        generate_scene_image(1, "一个场景")


@pytest.mark.parametrize("scene_number, prompt", [(0, "提示词"), (1, "  ")])
def test_generate_scene_image_validates_input(scene_number, prompt):
    with pytest.raises(ValueError):
        generate_scene_image(scene_number, prompt)
