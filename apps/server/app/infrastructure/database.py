from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.core.config import DATABASE_URL

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def get_session_factory():
    """按当前容器装配返回会话工厂。

    容器由组合根（应用入口的 lifespan）或测试的 conftest 显式装配，
    因此这里不再反向依赖应用入口模块。
    """
    from app.core.container import get_session_factory as _from_container

    return _from_container()


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def migrate_local_schema() -> None:
    """为 SQLite 和 PostgreSQL 补齐第一版需要的新增字段。"""
    with engine.begin() as connection:
        inspector = inspect(connection)
        if "agent_threads" in inspector.get_table_names():
            columns = {column["name"] for column in inspector.get_columns("agent_threads")}
            if "agent_working_text" not in columns:
                connection.execute(text("ALTER TABLE agent_threads ADD COLUMN agent_working_text TEXT NOT NULL DEFAULT ''"))
            if "agent_working_revision_id" not in columns:
                connection.execute(text("ALTER TABLE agent_threads ADD COLUMN agent_working_revision_id VARCHAR(80)"))
            if "agent_pending_patches" not in columns:
                connection.execute(text("ALTER TABLE agent_threads ADD COLUMN agent_pending_patches JSON NOT NULL DEFAULT '[]'"))
            if "summary_text" not in columns:
                connection.execute(text("ALTER TABLE agent_threads ADD COLUMN summary_text TEXT NOT NULL DEFAULT ''"))
            if "summary_through_message_id" not in columns:
                connection.execute(text("ALTER TABLE agent_threads ADD COLUMN summary_through_message_id VARCHAR(36)"))
            if "execution_token" not in columns:
                connection.execute(text("ALTER TABLE agent_threads ADD COLUMN execution_token VARCHAR(36)"))
            if "execution_lease_until" not in columns:
                lease_type = "DATETIME" if connection.dialect.name == "sqlite" else "TIMESTAMP WITH TIME ZONE"
                connection.execute(text(f"ALTER TABLE agent_threads ADD COLUMN execution_lease_until {lease_type}"))
        if "agent_turn_requests" in inspector.get_table_names():
            columns = {column["name"] for column in inspector.get_columns("agent_turn_requests")}
            if "error_category" not in columns:
                connection.execute(text("ALTER TABLE agent_turn_requests ADD COLUMN error_category VARCHAR(40)"))
        if "jobs" in inspector.get_table_names():
            columns = {column["name"] for column in inspector.get_columns("jobs")}
            additions = {
                "attempt_count": "INTEGER NOT NULL DEFAULT 0",
                "max_attempts": "INTEGER NOT NULL DEFAULT 5",
                "heartbeat_at": "TIMESTAMP WITH TIME ZONE",
                "execution_token": "VARCHAR(36)",
                "last_enqueued_at": "TIMESTAMP WITH TIME ZONE",
                "result_message_id": "VARCHAR(36)",
                "workflow_resume_status": "VARCHAR(20) NOT NULL DEFAULT 'not_required'",
                "workflow_resume_error": "TEXT",
                "workflow_resume_started_at": "TIMESTAMP WITH TIME ZONE",
            }
            for name, column_type in additions.items():
                if name not in columns:
                    if connection.dialect.name == "sqlite" and column_type.startswith("TIMESTAMP WITH TIME ZONE"):
                        column_type = "DATETIME"
                    connection.execute(text(f"ALTER TABLE jobs ADD COLUMN {name} {column_type}"))
        if "assets" in inspector.get_table_names():
            connection.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_asset_job_scene "
                "ON assets (job_id, scene_number)"
            ))
        if "export_files" in inspector.get_table_names():
            connection.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_export_file_job "
                "ON export_files (job_id)"
            ))
        if "agent_turn_requests" in inspector.get_table_names():
            connection.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_turn_request_thread_request "
                "ON agent_turn_requests (thread_id, request_id)"
            ))
        if "rewrite_runs" in inspector.get_table_names():
            rewrite_columns = {column["name"] for column in inspector.get_columns("rewrite_runs")}
            if "style_id" not in rewrite_columns:
                connection.execute(text("ALTER TABLE rewrite_runs ADD COLUMN style_id VARCHAR(80) NOT NULL DEFAULT 'humanize'"))
            if "prompt_id" not in rewrite_columns:
                connection.execute(text("ALTER TABLE rewrite_runs ADD COLUMN prompt_id VARCHAR(120) NOT NULL DEFAULT 'normal_humanize_v1'"))
        if "rewrite_sessions" in inspector.get_table_names():
            session_columns = {column["name"] for column in inspector.get_columns("rewrite_sessions")}
            if "comparison_document_id" not in session_columns:
                connection.execute(text("ALTER TABLE rewrite_sessions ADD COLUMN comparison_document_id VARCHAR(36)"))
            if "comparison_revision_id" not in session_columns:
                connection.execute(text("ALTER TABLE rewrite_sessions ADD COLUMN comparison_revision_id VARCHAR(36)"))
        if "subtitle_files" in inspector.get_table_names():
            subtitle_columns = {column["name"] for column in inspector.get_columns("subtitle_files")}
            if "audio_file_name" not in subtitle_columns:
                connection.execute(text("ALTER TABLE subtitle_files ADD COLUMN audio_file_name VARCHAR(255)"))
            if "audio_object_path" not in subtitle_columns:
                connection.execute(text("ALTER TABLE subtitle_files ADD COLUMN audio_object_path VARCHAR(500)"))
        if "workspaces" in inspector.get_table_names():
            columns = {column["name"] for column in inspector.get_columns("workspaces")}
            if "owner_user_id" not in columns:
                connection.execute(text("ALTER TABLE workspaces ADD COLUMN owner_user_id VARCHAR(36)"))
                connection.execute(text("CREATE INDEX IF NOT EXISTS ix_workspaces_owner_user_id ON workspaces (owner_user_id)"))
        if "agent_prompt_presets" in inspector.get_table_names():
            columns = {column["name"] for column in inspector.get_columns("agent_prompt_presets")}
            if "owner_user_id" not in columns:
                connection.execute(text("ALTER TABLE agent_prompt_presets ADD COLUMN owner_user_id VARCHAR(36)"))
                connection.execute(text("CREATE INDEX IF NOT EXISTS ix_agent_prompt_presets_owner_user_id ON agent_prompt_presets (owner_user_id)"))
        connection.execute(text(
            "UPDATE rewrite_sessions "
            "SET comparison_document_id = document_id, comparison_revision_id = source_revision_id "
            "WHERE comparison_revision_id IS NULL"
        ))

