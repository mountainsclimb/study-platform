"""PostgreSQL 连接池离线回归（伪 psycopg_pool 模块，不连真实 PG）。

覆盖优化方案 §3.1 P1：
  * 进程级 ``psycopg_pool.ConnectionPool(min_size=2, max_size=20, open=True)`` 懒创建、复用；
  * ``connection()`` 从池借还，异常时回滚并归还（连接不泄漏）；
  * 缺少 ``psycopg_pool`` 时回退为每次新建连接（可用性优先），只告警一次；
  * ``close_pool()`` 可关闭连接池（atexit / 后续 lifespan 接入）。
"""
import sys
import types
from pathlib import Path

import pytest

from app import db

ROOT = Path(__file__).resolve().parents[1]


class FakeCursor:
    lastrowid = None

    def fetchone(self):
        return None

    def fetchall(self):
        return []


class FakePgConnection:
    def __init__(self):
        self.statements = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def execute(self, statement, params=None):
        self.statements.append(statement)
        return FakeCursor()

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


class FakePool:
    """psycopg_pool.ConnectionPool 的伪实现：记录构造参数、借还与关闭。"""

    instances: list["FakePool"] = []

    def __init__(self, conninfo, **kwargs):
        self.conninfo = conninfo
        self.kwargs = kwargs
        self.borrowed = 0
        self.returned = []
        self.closed = False
        FakePool.instances.append(self)

    def connection(self, timeout=None):
        return _FakePoolConnection(self)

    def close(self):
        self.closed = True


class _FakePoolConnection:
    def __init__(self, pool):
        self.pool = pool
        self.conn = None

    def __enter__(self):
        self.conn = FakePgConnection()
        self.pool.borrowed += 1
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        # 与 psycopg_pool 语义一致：正常结束提交，异常回滚，最后归还连接。
        if exc_type:
            try:
                self.conn.rollback()
            except Exception:
                pass
        else:
            self.conn.commit()
        self.pool.returned.append(self.conn)
        return False


@pytest.fixture(autouse=True)
def _pool_env(monkeypatch):
    FakePool.instances.clear()
    monkeypatch.setattr(db, "_pg_pool", None)
    monkeypatch.setattr(db, "_pg_pool_warned", False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake/db")
    # 连接参数依赖真实 psycopg 游标工厂；离线测试用空参数，伪池无需驱动细节。
    monkeypatch.setattr(db, "_pool_kwargs", lambda: {})
    yield
    monkeypatch.setattr(db, "_pg_pool", None)


def _install_fake_pool_module(monkeypatch, pool_cls=FakePool):
    module = types.ModuleType("psycopg_pool")
    module.ConnectionPool = pool_cls
    monkeypatch.setitem(sys.modules, "psycopg_pool", module)


def test_pool_created_lazily_on_first_connection_and_reused(monkeypatch):
    _install_fake_pool_module(monkeypatch)
    assert db._pg_pool is None
    assert FakePool.instances == []

    with db.connection() as con:
        assert con.execute("SELECT 1") is not None
    assert len(FakePool.instances) == 1
    pool = FakePool.instances[0]
    assert pool.conninfo == "postgresql://fake/db"
    assert pool.kwargs["min_size"] == 2
    assert pool.kwargs["max_size"] == 20
    assert pool.kwargs["open"] is True

    with db.connection() as con:
        con.execute("SELECT 2")
    assert len(FakePool.instances) == 1, "后续 connection() 必须复用同一进程级连接池"
    assert pool.borrowed == 2
    assert len(pool.returned) == 2, "每次退出 connection() 都必须归还连接"
    assert all(not conn.closed for conn in pool.returned), "池拥有的连接不能被业务层关闭"


def test_pool_returns_connection_after_exception(monkeypatch):
    _install_fake_pool_module(monkeypatch)
    with pytest.raises(RuntimeError):
        with db.connection() as con:
            con.execute("SELECT 1")
            raise RuntimeError("boom")
    pool = FakePool.instances[0]
    assert pool.borrowed == len(pool.returned) == 1, "异常路径也不能泄漏连接"
    assert pool.returned[0].rollbacks >= 1
    assert not pool.returned[0].closed


def test_attach_bank_with_pool_commits_and_returns_connection(monkeypatch):
    _install_fake_pool_module(monkeypatch)
    mirror_calls = []
    monkeypatch.setattr(db, "_ensure_postgres_bank", lambda con: mirror_calls.append(con))
    with db.connection(attach_bank=True) as con:
        con.execute("SELECT 1")
    pool = FakePool.instances[0]
    assert len(mirror_calls) == 1
    assert mirror_calls[0].raw is pool.returned[0]
    assert pool.returned[0].commits >= 1
    assert len(pool.returned) == 1


def test_missing_psycopg_pool_falls_back_to_direct_connections(monkeypatch):
    # sys.modules 里的 None 会让 `import psycopg_pool` 立即 ImportError。
    monkeypatch.setitem(sys.modules, "psycopg_pool", None)
    created = []

    def fake_open():
        raw = FakePgConnection()
        created.append(raw)
        return raw

    monkeypatch.setattr(db, "_open_postgres", fake_open)
    with db.connection() as con:
        con.execute("SELECT 1")
    with db.connection() as con:
        con.execute("SELECT 2")
    assert len(created) == 2, "缺包回退路径保持旧行为：每次新建连接"
    assert all(raw.closed for raw in created), "回退路径的连接必须显式关闭"
    assert db._pg_pool is False
    assert db._pg_pool_warned is True


def test_close_pool_closes_and_resets(monkeypatch):
    _install_fake_pool_module(monkeypatch)
    pool = db._get_pool()
    assert pool is FakePool.instances[0]
    db.close_pool()
    assert pool.closed is True
    assert db._pg_pool is None
    # 二次关闭是安全的空操作。
    db.close_pool()


def test_deploy_requirements_pin_psycopg_pool():
    requirements = (ROOT / "deploy" / "requirements.txt").read_text(encoding="utf-8")
    assert "psycopg_pool>=3.2,<4" in requirements
