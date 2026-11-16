from __future__ import annotations

import os
import time
import logging
from dataclasses import dataclass
from collections.abc import Iterator

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    OpenAI,
    RateLimitError,
)

from app.core.config import DEMO_MODE, LLM_MODEL, LLM_PROVIDER

logger = logging.getLogger(__name__)


@dataclass
class ModelResult:
    text: str
    model_id: str
    mode: str


class ModelUnavailable(Exception):
    pass


class ModelCallError(RuntimeError):
    def __init__(self, category: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.message = message
        self.category = category
        self.retryable = retryable


_MAX_ATTEMPTS = 3
_RETRY_DELAYS = (0.5, 1.0)


def _error_category(exc: Exception) -> tuple[str, bool]:
    if isinstance(exc, (APITimeoutError, APIConnectionError)):
        return "timeout" if isinstance(exc, APITimeoutError) else "connection", True
    if isinstance(exc, RateLimitError):
        body = getattr(exc, "body", None)
        error = body.get("error", body) if isinstance(body, dict) else {}
        code = str(error.get("code", "")).lower() if isinstance(error, dict) else ""
        detail = " ".join(str(error.get(key, "")) for key in ("type", "message")).lower() if isinstance(error, dict) else ""
        if any(marker in f"{code} {detail}" for marker in ("quota", "billing", "payment", "insufficient")):
            return "billing", False
        return "rate_limit", True
    if isinstance(exc, AuthenticationError):
        return "credentials", False
    if isinstance(exc, BadRequestError):
        return "request", False
    if isinstance(exc, APIStatusError):
        if exc.status_code == 402:
            return "billing", False
        if exc.status_code in {401, 403}:
            return "credentials", False
        if exc.status_code == 429:
            return "rate_limit", True
        if exc.status_code >= 500:
            return "provider", True
        return "request", False
    return "provider", False


def _user_message(category: str) -> str:
    if category == "credentials":
        return "当前模型的授权配置不可用，请切换模型后重试。"
    if category == "billing":
        return "当前模型服务暂时无法完成请求，请切换模型后重试。"
    if category == "request":
        return "当前模型无法处理这次请求，请调整请求或切换模型后重试。"
    if category == "rate_limit":
        return "当前模型请求较多，暂时无法完成。请稍后重试或切换模型。"
    return "当前模型服务暂时不可用，请稍后重试或切换模型。"


def _retryable_request(operation, *, model_id: str):
    for attempt in range(_MAX_ATTEMPTS):
        try:
            return operation()
        except Exception as exc:
            category, retryable = _error_category(exc)
            if not retryable or attempt == _MAX_ATTEMPTS - 1:
                logger.warning(
                    "event=model.request_failed model_id=%s category=%s attempt=%s retryable=%s",
                    model_id,
                    category,
                    attempt + 1,
                    retryable,
                )
                raise ModelCallError(category, _user_message(category), retryable=retryable) from exc
            logger.warning(
                "event=model.request_retry model_id=%s category=%s attempt=%s",
                model_id,
                category,
                attempt + 1,
            )
            time.sleep(_RETRY_DELAYS[attempt])


def _provider_config(model_id: str | None = None) -> tuple[str | None, str | None, str]:
    model = model_id or LLM_MODEL
    provider = LLM_PROVIDER
    if model.startswith("qwen"):
        provider = "qwen"
    elif model.startswith("deepseek"):
        provider = "deepseek"
    elif model.startswith("doubao"):
        provider = "doubao"
    if provider == "deepseek":
        return os.getenv("DEEPSEEK_API_KEY"), os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"), model
    if provider in {"qwen", "dashscope"}:
        return os.getenv("DASHSCOPE_API_KEY"), os.getenv("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"), model
    if provider in {"doubao", "ark", "volcano"}:
        endpoint = os.getenv("DOUBAO_MODEL", "").strip()
        return os.getenv("ARK_API_KEY"), os.getenv("ARK_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3"), endpoint or model
    return os.getenv("OPENAI_API_KEY"), os.getenv("OPENAI_BASE_URL"), model


def model_configured() -> bool:
    return model_available(LLM_MODEL)


def model_available(model_id: str) -> bool:
    if DEMO_MODE:
        return False
    key, _, resolved_model = _provider_config(model_id)
    if model_id.startswith("doubao"):
        return bool(key and os.getenv("DOUBAO_MODEL", "").strip())
    return bool(key and resolved_model)


def complete_chat(
    messages: list[dict],
    model_id: str | None = None,
    temperature: float = 0.7,
    response_format: dict | None = None,
) -> ModelResult:
    api_key, base_url, model = _provider_config(model_id)
    if not api_key:
        raise ModelUnavailable("尚未配置模型密钥")
    client = OpenAI(api_key=api_key, base_url=base_url, timeout=90, max_retries=0)
    request = {"model": model, "messages": messages, "temperature": temperature}
    if response_format is not None:
        request["response_format"] = response_format
    response = _retryable_request(lambda: client.chat.completions.create(**request), model_id=model)
    text = response.choices[0].message.content or ""
    return ModelResult(text=text.strip(), model_id=model, mode="live")


def stream_chat(messages: list[dict], model_id: str | None = None, temperature: float = 0.7) -> Iterator[str]:
    """通过 OpenAI 兼容的流式接口逐段返回模型文本。"""
    api_key, base_url, model = _provider_config(model_id)
    if not api_key:
        raise ModelUnavailable("尚未配置模型密钥")
    client = OpenAI(api_key=api_key, base_url=base_url, timeout=90, max_retries=0)
    emitted = False
    for attempt in range(_MAX_ATTEMPTS):
        response = None
        try:
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
                stream=True,
            )
            for chunk in response:
                if not chunk.choices:
                    continue
                content = chunk.choices[0].delta.content
                if isinstance(content, str) and content:
                    emitted = True
                    yield content
            return
        except Exception as exc:
            category, retryable = _error_category(exc)
            if emitted or not retryable or attempt == _MAX_ATTEMPTS - 1:
                logger.warning(
                    "event=model.stream_failed model_id=%s category=%s attempt=%s retryable=%s emitted=%s",
                    model,
                    category,
                    attempt + 1,
                    retryable,
                    emitted,
                )
                raise ModelCallError(category, _user_message(category), retryable=retryable) from exc
            if response is not None:
                try:
                    response.close()
                except Exception:
                    logger.debug("event=model.stream_close_failed model_id=%s", model)
            logger.warning(
                "event=model.stream_retry model_id=%s category=%s attempt=%s",
                model,
                category,
                attempt + 1,
            )
            time.sleep(_RETRY_DELAYS[attempt])

