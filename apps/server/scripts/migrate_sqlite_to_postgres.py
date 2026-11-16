"""把已有 SQLite 业务记录复制到当前 DATABASE_URL 指向的 PostgreSQL。"""

from __future__ import annotations

import logging
import argparse
from pathlib import Path

from sqlalchemy import create_engine, func, inspect, select

from app.core.config import DATA_DIR, DATABASE_URL
from app.domain import models  # noqa: F401 - 注册所有数据表
from app.infrastructure.database import Base, engine as target_engine, migrate_local_schema

logger = logging.getLogger("lingzhu.migration")
SOURCE_DB = DATA_DIR / "lingzhu.db"


def row_count(connection, table) -> int:
    return connection.execute(select(func.count()).select_from(table)).scalar_one()


def main() -> None:
    parser = argparse.ArgumentParser(description="检查或迁移本地 SQLite 业务数据")
    parser.add_argument("--check", action="store_true", help="只检查源库与目标库的数据量，不复制数据")
    args = parser.parse_args()
    if not DATABASE_URL.startswith("postgresql"):
        raise SystemExit("DATABASE_URL 不是 PostgreSQL 地址，已停止迁移。")
    if not SOURCE_DB.exists():
        raise SystemExit(f"找不到本地 SQLite 数据库：{SOURCE_DB}")

    source_engine = create_engine(f"sqlite:///{SOURCE_DB.as_posix()}")
    Base.metadata.create_all(bind=target_engine)
    migrate_local_schema()

    with target_engine.connect() as target:
        target_tables = set(inspect(target).get_table_names())
        target_counts = {
            table.name: row_count(target, table)
            for table in Base.metadata.sorted_tables
            if table.name in target_tables
        }
    existing = {name: count for name, count in target_counts.items() if count > 0}
    with source_engine.connect() as source:
        source_tables = set(inspect(source).get_table_names())
        source_counts = {
            table.name: row_count(source, table)
            for table in Base.metadata.sorted_tables
            if table.name in source_tables
        }

    print(f"本地 SQLite 记录数：{source_counts}")
    print(f"目标 PostgreSQL 记录数：{target_counts}")
    if args.check:
        source_engine.dispose()
        return
    if existing:
        raise SystemExit(f"目标 PostgreSQL 已有业务数据，为避免覆盖已停止迁移。非空表：{existing}")

    copied: dict[str, int] = {}
    with source_engine.connect() as source, target_engine.begin() as target:
        for table in Base.metadata.sorted_tables:
            if table.name not in source_tables:
                continue
            source_columns = {column["name"] for column in inspect(source).get_columns(table.name)}
            shared_columns = [column.name for column in table.columns if column.name in source_columns]
            if not shared_columns:
                continue
            statement = select(*(table.c[name] for name in shared_columns))
            rows = source.execute(statement).mappings()
            total = 0
            while batch := rows.fetchmany(500):
                target.execute(table.insert(), [dict(row) for row in batch])
                total += len(batch)
            if total:
                copied[table.name] = total

    source_engine.dispose()
    with target_engine.connect() as target:
        migrated_counts = {
            table.name: row_count(target, table)
            for table in Base.metadata.sorted_tables
            if table.name in target_tables
        }
    logger.info("PostgreSQL 目标记录数：%s", migrated_counts)
    logger.info("SQLite 记录复制完成，tables=%s", copied)
    print(f"迁移完成，共复制 {sum(copied.values())} 条记录；本地 SQLite 文件已保留为备份。")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    main()
