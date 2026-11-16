from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import httpx

from app.core.config import ASSET_DIR, IMAGE_API_KEY, IMAGE_BASE_URL, IMAGE_MODEL

logger = logging.getLogger(__name__)

_IMAGE_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
_GENERATION_PATH = "/images/generations"
_DEFAULT_IMAGE_SIZE = "2K"
_DEFAULT_ASPECT_RATIO = "16:9"


@dataclass(frozen=True)
class GeneratedImage:
    scene_number: int
    prompt: str
    file_name: str
    object_path: str
    model_id: str
    request_id: str | None


class ImageGenerationError(RuntimeError):
    """图片生成接口调用或结果处理失败。"""


def _image_url(payload: object) -> str:
    if not isinstance(payload, dict):
        raise ImageGenerationError("即梦接口返回内容格式不正确")
    data = payload.get("data")
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        raise ImageGenerationError("即梦接口没有返回图片地址")
    url = data[0].get("url")
    if not isinstance(url, str) or not url.startswith(("https://", "http://")):
        raise ImageGenerationError("即梦接口返回的图片地址无效")
    return url


def _generation_endpoint(base_url: str) -> str:
    """同时兼容方舟 API 根地址和已经带生图路径的完整地址。"""
    base = base_url.rstrip("/")
    if base.endswith(_GENERATION_PATH):
        return base
    return f"{base}{_GENERATION_PATH}"


def _request_id(response: httpx.Response) -> str | None:
    return response.headers.get("x-request-id") or response.headers.get("x-tt-logid")


def _provider_error(response: httpx.Response) -> tuple[str | None, str]:
    """只提取服务商错误码和说明，不把请求头或密钥写入日志。"""
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    error = payload.get("error", payload) if isinstance(payload, dict) else {}
    if not isinstance(error, dict):
        error = {}

    code = error.get("code") or error.get("type")
    message = error.get("message") or error.get("msg") or error.get("detail")
    if not message:
        message = "服务商未提供错误说明"
    message = str(message).replace(IMAGE_API_KEY, "[已隐藏]") if IMAGE_API_KEY else str(message)
    message = " ".join(message.split())
    return (str(code)[:100] if code else None, message[:400])


def generate_scene_image(scene_number: int, prompt: str, output_dir: Path | None = None) -> GeneratedImage:
    """调用 Seedream 生图，并把返回的临时图片地址下载到素材目录。"""
    if scene_number < 1:
        raise ValueError("分镜序号必须大于 0")
    if not prompt or not prompt.strip():
        raise ValueError("生图提示词不能为空")
    if not IMAGE_API_KEY:
        raise ImageGenerationError("未配置 IMAGE_API_KEY 或 ARK_API_KEY")

    endpoint = _generation_endpoint(IMAGE_BASE_URL)
    effective_prompt = f"{prompt.strip()}\n\n画幅要求：{_DEFAULT_ASPECT_RATIO} 横向构图。"
    headers = {
        "Authorization": f"Bearer {IMAGE_API_KEY}",
        "Content-Type": "application/json",
    }
    body = {
        "model": IMAGE_MODEL,
        "prompt": effective_prompt,
        "response_format": "url",
        "size": _DEFAULT_IMAGE_SIZE,
        "stream": False,
        "watermark": True,
    }

    try:
        response = httpx.post(endpoint, headers=headers, json=body, timeout=180)
        response.raise_for_status()
        image_url = _image_url(response.json())
    except httpx.HTTPStatusError as exc:
        request_id = _request_id(exc.response)
        error_code, error_message = _provider_error(exc.response)
        logger.error(
            "event=image_generation.http_error scene_number=%s status=%s error_code=%s request_id=%s message=%s",
            scene_number,
            exc.response.status_code,
            error_code,
            request_id,
            error_message,
        )
        details = f"，错误码 {error_code}" if error_code else ""
        details += f"，请求编号 {request_id}" if request_id else ""
        raise ImageGenerationError(
            f"即梦接口返回 HTTP {exc.response.status_code}{details}：{error_message}"
        ) from exc
    except httpx.RequestError as exc:
        logger.exception("event=image_generation.request_failed scene_number=%s", scene_number)
        raise ImageGenerationError("无法连接即梦图片生成接口") from exc
    except ValueError as exc:
        logger.exception("event=image_generation.response_invalid scene_number=%s", scene_number)
        raise ImageGenerationError("即梦接口返回的不是有效 JSON") from exc

    try:
        image_response = httpx.get(image_url, timeout=60)
        image_response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        request_id = _request_id(exc.response)
        logger.error(
            "event=image_generation.download_http_error scene_number=%s status=%s request_id=%s",
            scene_number,
            exc.response.status_code,
            request_id,
        )
        raise ImageGenerationError(f"生成图片下载失败，HTTP {exc.response.status_code}") from exc
    except httpx.RequestError as exc:
        logger.exception("event=image_generation.download_failed scene_number=%s", scene_number)
        raise ImageGenerationError("无法下载即梦生成的图片") from exc

    content_type = image_response.headers.get("content-type", "").split(";", 1)[0].lower()
    extension = _IMAGE_EXTENSIONS.get(content_type)
    if not extension or not image_response.content:
        raise ImageGenerationError("下载结果不是支持的图片格式或图片内容为空")

    file_name = f"scene-{scene_number}-{uuid4().hex[:12]}{extension}"
    destination_dir = Path(output_dir or ASSET_DIR)
    destination_dir.mkdir(parents=True, exist_ok=True)
    image_path = destination_dir / file_name
    try:
        image_path.write_bytes(image_response.content)
    except OSError as exc:
        logger.exception("event=image_generation.save_failed scene_number=%s", scene_number)
        raise ImageGenerationError("生成图片保存失败") from exc

    request_id = _request_id(response)
    logger.info(
        "event=image_generation.completed scene_number=%s model_id=%s request_id=%s file_name=%s bytes=%s",
        scene_number,
        IMAGE_MODEL,
        request_id,
        file_name,
        len(image_response.content),
    )
    return GeneratedImage(
        scene_number=scene_number,
        prompt=effective_prompt,
        file_name=file_name,
        object_path=str(image_path),
        model_id=IMAGE_MODEL,
        request_id=request_id,
    )


def compose_image_prompt(scene_prompt: str, style_prompt: str = "") -> str:
    style = style_prompt.strip()
    scene = scene_prompt.strip()
    if not style:
        return scene
    return f"整体视觉风格：{style}\n\n分镜画面内容：{scene}"
