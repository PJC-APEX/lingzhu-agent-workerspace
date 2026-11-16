from __future__ import annotations

from contextlib import contextmanager
from threading import Lock

from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.postgres import PostgresSaver
from sqlalchemy.engine import make_url

from app.core.config import DATABASE_URL

_test_checkpointer = MemorySaver()
_postgres_setup_lock = Lock()
_postgres_setup_done = False
_CHECKPOINT_SETUP_LOCK_ID = 0x4C494E5A4855
_CHECKPOINT_INDEXES = {
    "checkpoints_thread_id_idx": "checkpoints",
    "checkpoint_blobs_thread_id_idx": "checkpoint_blobs",
    "checkpoint_writes_thread_id_idx": "checkpoint_writes",
}


def _postgres_dsn() -> str:
    url = make_url(DATABASE_URL)
    if url.drivername.startswith("sqlite"):
        raise RuntimeError("LangGraph 持久化检查点需要 PostgreSQL 数据库")
    url = url.set(drivername="postgresql")
    return url.render_as_string(hide_password=False)


def _schema_ready(saver) -> bool:
    with saver.conn.cursor() as cursor:
        cursor.execute("SELECT to_regclass('checkpoint_migrations') AS table_name")
        if cursor.fetchone()["table_name"] is None:
            return False
        cursor.execute("SELECT v FROM checkpoint_migrations ORDER BY v DESC LIMIT 1")
        row = cursor.fetchone()
        if row is None or row["v"] < len(saver.MIGRATIONS) - 1:
            return False
        cursor.execute(
            "SELECT cls.relname AS name, idx.indisvalid AS valid "
            "FROM pg_index idx JOIN pg_class cls ON cls.oid = idx.indexrelid "
            "WHERE cls.relname = ANY(%s)",
            (list(_CHECKPOINT_INDEXES),),
        )
        indexes = {item["name"]: item["valid"] for item in cursor.fetchall()}
    return all(indexes.get(name) is True for name in _CHECKPOINT_INDEXES)


def _setup_postgres_schema(saver) -> None:
    """Serialize first-use setup and repair invalid indexes left by interruption."""
    with saver.conn.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_lock(%s)", (_CHECKPOINT_SETUP_LOCK_ID,))
    try:
        if _schema_ready(saver):
            return
        with saver.conn.cursor() as cursor:
            for name in _CHECKPOINT_INDEXES:
                cursor.execute(
                    "SELECT idx.indisvalid AS valid FROM pg_index idx "
                    "JOIN pg_class cls ON cls.oid = idx.indexrelid WHERE cls.relname = %s",
                    (name,),
                )
                row = cursor.fetchone()
                if row is not None and not row["valid"]:
                    # Names are constants from _CHECKPOINT_INDEXES, not user input.
                    cursor.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
        saver.setup()
        # A previous setup can have recorded the latest migration while an
        # interrupted concurrent index build left that index invalid/missing.
        with saver.conn.cursor() as cursor:
            for index_name, table_name in _CHECKPOINT_INDEXES.items():
                cursor.execute(
                    f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {index_name} "
                    f"ON {table_name}(thread_id)"
                )
        if not _schema_ready(saver):
            raise RuntimeError("LangGraph PostgreSQL checkpointer schema is incomplete")
    finally:
        with saver.conn.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(%s)", (_CHECKPOINT_SETUP_LOCK_ID,))


def initialize_checkpointer() -> None:
    """Prepare persistent checkpoints before serving API or worker requests."""
    from app.core.container import get_container

    global _postgres_setup_done
    if get_container().testing or _postgres_setup_done:
        return
    with _postgres_setup_lock:
        if _postgres_setup_done:
            return
        with PostgresSaver.from_conn_string(_postgres_dsn()) as saver:
            _setup_postgres_schema(saver)
        _postgres_setup_done = True


@contextmanager
def open_checkpointer():
    """测试使用内存检查点；本地运行使用 PostgreSQL 持久化检查点。

    测试与否由依赖容器显式声明，不再反向读取 app.main 的运行状态。
    """
    from app.core.container import get_container

    testing = get_container().testing

    if testing:
        yield _test_checkpointer
        return

    with PostgresSaver.from_conn_string(_postgres_dsn()) as saver:
        global _postgres_setup_done
        if not _postgres_setup_done:
            with _postgres_setup_lock:
                if not _postgres_setup_done:
                    _setup_postgres_schema(saver)
                    _postgres_setup_done = True
        yield saver
