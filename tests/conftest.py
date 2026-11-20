import os

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("STUDY_ALLOWED_HOSTS", "127.0.0.1,localhost,[::1],testserver")
# 生产环境检索索引（FTS + 泄密标记）在进程启动时预热（7,659 题约 1.5s）；单测每个用例都建
# 临时应用库，逐测预热只会拖慢套件，因此默认关闭。预热行为由
# tests/test_search_perf_flags.py 的专项用例显式打开 STUDY_INDEX_PREWARM=1 覆盖。
os.environ.setdefault("STUDY_INDEX_PREWARM", "0")


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    from app import ai, auth, db, practice
    auth._failures.clear()
    auth._ip_failures.clear()
    auth._reg_attempts.clear()
    path = tmp_path / "appdata"
    monkeypatch.setattr(db, "DATA_DIR", path)
    for module in (ai, practice):
        if hasattr(module, "DATA_DIR"):
            monkeypatch.setattr(module, "DATA_DIR", path)
    return path


@pytest.fixture
def client(isolated_db):
    from app.main import app
    with TestClient(app) as value:
        response = value.post("/api/auth/register", json={"username": "测试管理员", "password": "test-password-123"})
        assert response.status_code == 200, response.text
        yield value


@pytest.fixture
def second_client(client):
    from app.main import app
    # Lifespan is owned by the first client; avoid re-initializing active AI jobs.
    value = TestClient(app)
    response = value.post("/api/auth/register", json={"username": "测试学生", "password": "test-password-456"})
    assert response.status_code == 200, response.text
    yield value
    value.close()
