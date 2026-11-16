from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

SERVER_DIR = Path(__file__).resolve().parents[2]
ROOT_DIR = SERVER_DIR.parents[1]
DATA_DIR = SERVER_DIR / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
ASSET_DIR = DATA_DIR / "assets"
PREVIEW_DIR = DATA_DIR / "previews"
EXPORT_DIR = DATA_DIR / "exports"
LOG_DIR = DATA_DIR / "logs"

load_dotenv(ROOT_DIR / ".env")
# 兼容旧版配置位置；根目录 .env 中已有的变量优先级更高。
load_dotenv(SERVER_DIR / ".env", override=False)

DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{(DATA_DIR / 'lingzhu.db').as_posix()}")
REDIS_URL = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")
CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", REDIS_URL)
CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", "redis://127.0.0.1:6379/1")
JOB_MAX_ATTEMPTS = int(os.getenv("JOB_MAX_ATTEMPTS", "5"))
JOB_HEARTBEAT_SECONDS = int(os.getenv("JOB_HEARTBEAT_SECONDS", "30"))
JOB_STALE_SECONDS = int(os.getenv("JOB_STALE_SECONDS", "180"))
JOB_OUTBOX_POLL_SECONDS = int(os.getenv("JOB_OUTBOX_POLL_SECONDS", "10"))
JOB_OUTBOX_LOCK_SECONDS = int(os.getenv("JOB_OUTBOX_LOCK_SECONDS", "120"))
UPLOAD_MAX_CONCURRENCY = max(1, int(os.getenv("UPLOAD_MAX_CONCURRENCY", "4")))
AGENT_INPUT_TOKEN_BUDGET = int(os.getenv("AGENT_INPUT_TOKEN_BUDGET", "16000"))
AGENT_SUMMARY_TRIGGER_RATIO = float(os.getenv("AGENT_SUMMARY_TRIGGER_RATIO", "0.75"))
AGENT_RECENT_MESSAGE_COUNT = int(os.getenv("AGENT_RECENT_MESSAGE_COUNT", "8"))
DEMO_MODE = os.getenv("DEMO_MODE", "false").lower() in {"1", "true", "yes", "on"}
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
CORS_ORIGINS = [
    origin.strip().rstrip("/")
    for origin in os.getenv(
        "CORS_ORIGINS",
        "http://127.0.0.1:5173,http://localhost:5173,http://127.0.0.1:5174,http://localhost:5174",
    ).split(",")
    if origin.strip()
]
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "deepseek").lower()
LLM_MODEL = os.getenv("LLM_MODEL", "deepseek-v4-flash")
IMAGE_API_KEY = os.getenv("IMAGE_API_KEY", "").strip() or os.getenv("ARK_API_KEY", "").strip()
IMAGE_BASE_URL = (
    os.getenv("IMAGE_BASE_URL", "").strip()
    or os.getenv("ARK_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3").strip()
).rstrip("/")
IMAGE_MODEL = os.getenv("IMAGE_MODEL", "").strip() or "doubao-seedream-5-0-flash-260915"
S3_ENDPOINT_URL = os.getenv("S3_ENDPOINT_URL", "").strip()
S3_BUCKET = os.getenv("S3_BUCKET", "").strip()
S3_ACCESS_KEY_ID = os.getenv("S3_ACCESS_KEY_ID", "").strip()
S3_SECRET_ACCESS_KEY = os.getenv("S3_SECRET_ACCESS_KEY", "").strip()
S3_REGION = os.getenv("S3_REGION", "").strip()
S3_PUBLIC_BASE_URL = os.getenv("S3_PUBLIC_BASE_URL", "").strip().rstrip("/")
JIANYING_HELPER_URL = os.getenv(
    "JIANYING_HELPER_URL",
    "https://ts.fyshark.com/#/cozeToJianyin",
).strip()
JIANYING_EXPORT_MODE = os.getenv("JIANYING_EXPORT_MODE", "local").strip().lower()
if JIANYING_EXPORT_MODE not in {"local", "package"}:
    raise ValueError("JIANYING_EXPORT_MODE must be local or package")
JIANYING_DRAFT_DIR = os.getenv("JIANYING_DRAFT_DIR", "").strip() or str(
    Path.home()
    / "AppData"
    / "Local"
    / "JianyingPro"
    / "User Data"
    / "Projects"
    / "com.lveditor.draft"
)

for directory in (DATA_DIR, UPLOAD_DIR, ASSET_DIR, PREVIEW_DIR, EXPORT_DIR, LOG_DIR):
    directory.mkdir(parents=True, exist_ok=True)

