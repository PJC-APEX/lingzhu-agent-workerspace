"""业务异常。

application 层只抛这些异常，不依赖 FastAPI；
由 API 层的异常处理器统一转换为 HTTP 状态码。
"""

from __future__ import annotations


class AppError(Exception):
    """所有业务异常的基类。"""

    status_code = 400
    default_message = "请求无法处理"

    def __init__(self, message: str | None = None) -> None:
        self.message = message or self.default_message
        super().__init__(self.message)


class NotFoundError(AppError):
    status_code = 404
    default_message = "找不到请求的资源"


class ConflictError(AppError):
    """并发冲突、状态已变化、版本不匹配。"""

    status_code = 409
    default_message = "资源状态已变化，请刷新后重试"


class UnauthorizedError(AppError):
    status_code = 401
    default_message = "请先登录"


class ValidationError(AppError):
    status_code = 422
    default_message = "请求参数无效"


class TooLargeError(AppError):
    status_code = 413
    default_message = "文件超出大小限制"


class UnsupportedFormatError(AppError):
    status_code = 415
    default_message = "不支持的文件格式"


class ServiceBusyError(AppError):
    status_code = 503
    default_message = "服务正忙，请稍后重试"


class UpstreamError(AppError):
    status_code = 502
    default_message = "上游服务暂时不可用"


class InternalError(AppError):
    """主体操作已生效，但收尾步骤失败。

    典型场景：任务取消成功，但唤醒对话流程时报错。
    必须和 409 区分开——409 会让调用方以为整个操作没生效、从而重试，
    而这里的动作其实已经落库了。
    """

    status_code = 500
    default_message = "操作已生效，但后续步骤失败"
