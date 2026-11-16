from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.container import configure_container, reset_container
from app.domain import models  # noqa: F401
from app.infrastructure.database import Base, get_db
from app.main import app

# 中文用户名会让默认临时根目录 %TEMP%\pytest-of-<用户名> 在 Windows 上无法创建，
# 导致所有依赖 tmp_path 的用例在 setup 阶段报 PermissionError。
# 固定到项目内 .pytest_tmp，保证跨机器可复现。
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_LOCAL_TMP_ROOT = _PROJECT_ROOT / ".pytest_tmp" / "root"


def pytest_configure(config):
    """把 pytest 的临时根目录固定到项目内，绕开中文用户名导致的权限问题。"""
    if config.option.basetemp:
        return
    _LOCAL_TMP_ROOT.mkdir(parents=True, exist_ok=True)
    config.option.basetemp = str(_LOCAL_TMP_ROOT)


@pytest.fixture(autouse=True)
def local_file_storage(monkeypatch):
    from app.infrastructure import storage as object_storage

    monkeypatch.setattr(object_storage, "is_configured", lambda: False)


@pytest.fixture
def client():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(bind=engine)


    app.state.testing_session_factory = TestingSession
    configure_container(session_factory=TestingSession, testing=True)

    def override_get_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(app) as test_client:
            test_client.post("/api/v1/auth/register", json={"email": "fixture@example.com", "password": "fixture-password-123"})
            yield test_client
    finally:
        app.dependency_overrides.clear()
        del app.state.testing_session_factory
        reset_container()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()
