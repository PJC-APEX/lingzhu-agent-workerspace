"""把 application 层抛出的业务异常转换为 HTTP 响应。

这样 application 层不需要 import fastapi，也不需要知道状态码。
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.domain.exceptions import AppError

logger = logging.getLogger(__name__)

__all__ = ["install_exception_handlers"]


def install_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        if exc.status_code >= 500:
            logger.error(
                "event=business.error path=%s status=%s message=%s",
                request.url.path,
                exc.status_code,
                exc.message,
            )
        else:
            logger.info(
                "event=business.rejected path=%s status=%s message=%s",
                request.url.path,
                exc.status_code,
                exc.message,
            )
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})
