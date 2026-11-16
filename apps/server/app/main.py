from __future__ import annotations

import logging
from time import perf_counter
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.v1.errors import install_exception_handlers
from app.api.v1.routes import router
from app.core.container import configure_container, get_container
from app.core.config import CORS_ORIGINS, LOG_DIR, LOG_LEVEL
from app.domain import models  # noqa: F401 - 导入模型以注册数据表
from app.infrastructure.database import Base, SessionLocal, engine, migrate_local_schema
from app.graph.checkpointer import initialize_checkpointer

# 显式导入 worker 模块，触发它把 Celery 任务注册到 infrastructure/queue。
# 不依赖「routes 恰好导入了 local_tasks」这种隐式顺序——否则哪天有人
# 删掉那条导入，任务就会投不出去，而且不会报错。
from app.workers import local_tasks as _media_worker  # noqa: F401

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
file_handler = RotatingFileHandler(
    LOG_DIR / "server.log",
    maxBytes=5 * 1024 * 1024,
    backupCount=3,
    encoding="utf-8",
)
file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
file_handler.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
if not any(getattr(handler, "_lingzhu_file_handler", False) for handler in logging.getLogger().handlers):
    file_handler._lingzhu_file_handler = True
    logging.getLogger().addHandler(file_handler)
logger = logging.getLogger("lingzhu")
# 关闭 Uvicorn（应用服务器）的逐请求访问日志，避免轮询记录淹没业务事件。
logging.getLogger("uvicorn.access").disabled = True

@asynccontextmanager
async def lifespan(_: FastAPI):
    container = get_container()
    if not container.testing:
        # 生产/本地：装配真实依赖，并在启动时对齐表结构。
        configure_container(session_factory=SessionLocal, testing=False)
        Base.metadata.create_all(bind=engine)
        migrate_local_schema()
        initialize_checkpointer()
        logger.info("灵珠智能助手服务已启动，业务数据库连接已就绪，dialect=%s", engine.dialect.name)
    else:
        # 测试：容器已由 conftest 装配为独立测试库，这里不做任何建表动作。
        logger.info("测试模式启动，使用独立测试数据库")
    yield


app = FastAPI(title="灵珠智能助手", version="0.1.0", description="文案改写、智能体对话与素材生成工作台", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(router)
install_exception_handlers(app)


@app.middleware("http")
async def request_log(request: Request, call_next):
    started_at = perf_counter()
    try:
        response = await call_next(request)
        elapsed_ms = int((perf_counter() - started_at) * 1000)
        if response.status_code >= 400 or elapsed_ms >= 2000:
            logger.warning(
                "event=http.request_issue method=%s path=%s status=%s elapsed_ms=%s",
                request.method,
                request.url.path,
                response.status_code,
                elapsed_ms,
            )
        return response
    except Exception:
        logger.exception("未处理的接口异常 method=%s path=%s", request.method, request.url.path)
        return JSONResponse(status_code=500, content={"detail": "服务内部错误，请查看后端日志"})

