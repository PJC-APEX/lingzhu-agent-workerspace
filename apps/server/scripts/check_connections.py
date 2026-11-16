"""检查 PostgreSQL 和 Redis 是否可由本机项目连接，不输出连接凭据。"""

from redis import Redis
from sqlalchemy import inspect, text
from fastapi.testclient import TestClient

from app.core.config import REDIS_URL
from app.infrastructure.database import engine
from app.main import app
from app.workers.celery_app import celery_app


def main() -> None:
    with engine.connect() as connection:
        database, user = connection.execute(text("SELECT current_database(), current_user")).one()
        tables = inspect(connection).get_table_names()
        print(f"PostgreSQL: connected database={database} user={user} tables={len(tables)}")

    redis = Redis.from_url(REDIS_URL, socket_connect_timeout=3, socket_timeout=3)
    if not redis.ping():
        raise SystemExit("Redis: PING failed")
    print("Redis: connected PING=ok")
    redis.close()

    with celery_app.connection_for_write() as broker:
        broker.ensure_connection(max_retries=1)
    print("Celery: Redis broker connection=ok")

    # 通过只读 API 请求验证 FastAPI 实际使用的也是 PostgreSQL。
    with TestClient(app) as client:
        response = client.get("/api/v1/workspaces")
        response.raise_for_status()
        print(f"API: connected workspaces={len(response.json())}")


if __name__ == "__main__":
    main()
