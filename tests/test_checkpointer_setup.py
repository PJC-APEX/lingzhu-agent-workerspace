"""Checkpoint schema setup runs before graph requests and repairs interrupted indexes."""
from contextlib import contextmanager

from app.graph import checkpointer


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection
        self.result = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, query, params=None):
        normalized = " ".join(query.split())
        self.connection.statements.append(normalized)
        if "to_regclass" in normalized:
            self.result = {"table_name": "checkpoint_migrations"}
        elif normalized.startswith("SELECT v FROM checkpoint_migrations"):
            self.result = {"v": self.connection.version}
        elif "FROM pg_index" in normalized and "ANY" in normalized:
            self.result = [{"name": name, "valid": valid} for name, valid in self.connection.indexes.items()]
        elif "FROM pg_index" in normalized and "relname = %s" in normalized:
            name = params[0]
            self.result = {"valid": self.connection.indexes[name]} if name in self.connection.indexes else None
        elif normalized.startswith("DROP INDEX CONCURRENTLY"):
            name = normalized.split()[5]
            self.connection.indexes.pop(name, None)
        elif normalized.startswith("CREATE INDEX CONCURRENTLY"):
            name = normalized.split()[5]
            self.connection.indexes[name] = True
        return self

    def fetchone(self):
        return self.result

    def fetchall(self):
        return self.result


class FakeConnection:
    def __init__(self, version, indexes):
        self.version = version
        self.indexes = dict(indexes)
        self.statements = []

    def cursor(self):
        return FakeCursor(self)


class FakeSaver:
    MIGRATIONS = tuple(range(10))

    def __init__(self, conn):
        self.conn = conn
        self.setup_calls = 0

    def setup(self):
        self.setup_calls += 1
        self.conn.version = len(self.MIGRATIONS) - 1
        for name in checkpointer._CHECKPOINT_INDEXES:
            self.conn.indexes[name] = True


def test_ready_schema_skips_setup_ddl():
    conn = FakeConnection(9, {name: True for name in checkpointer._CHECKPOINT_INDEXES})
    saver = FakeSaver(conn)

    checkpointer._setup_postgres_schema(saver)

    assert saver.setup_calls == 0
    assert not any("CREATE INDEX" in sql or "DROP INDEX" in sql for sql in conn.statements)


def test_interrupted_invalid_index_is_dropped_then_migrations_resume():
    names = list(checkpointer._CHECKPOINT_INDEXES)
    conn = FakeConnection(5, {names[0]: False})
    saver = FakeSaver(conn)

    checkpointer._setup_postgres_schema(saver)

    assert saver.setup_calls == 1
    assert conn.version == 9
    assert all(conn.indexes.get(name) is True for name in names)
    assert any("DROP INDEX CONCURRENTLY IF EXISTS checkpoints_thread_id_idx" in sql for sql in conn.statements)
    assert conn.statements[0].startswith("SELECT pg_advisory_lock")
    assert conn.statements[-1].startswith("SELECT pg_advisory_unlock")
