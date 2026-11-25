"""引擎重置的跨进程语义（P1-1）。

问题：``POST /api/engines/{id}/reset`` 原来只调用各引擎的 ``reset_engine()``，
清的是**当前 API 进程**里的模块级单例。部署形态下推理并不只发生在 API 进程：

* ``recognize`` / ``ocr`` 任务由 arq Worker 执行（``deploy/worker.py`` →
  ``app/ai/ocr.py``），Worker 进程持有自己的 ``formula_ocr`` / ``table_structure`` 单例；
* ``course-narration`` 等扩展任务同样在 Worker 内调用 ``speech_local``。

因此「管理员点了重置，但 Worker 里失败的引擎缓存还在」会让排障结论失真。

方案：用一个跨进程可见的**世代号**把重置意图传播出去，而不是投递队列任务
（投递需要 Redis 可用，而重置恰恰常用于 Redis/队列异常时的排障）：

1. ``mark_reset(engine)`` 在 ``app_settings`` 写 ``engine.reset.<engine> = <时间戳>``，
   同时重置本进程单例；
2. 其它进程（Worker）在每次执行任务前调用 ``apply_pending()``，发现 DB 里的时间戳
   比本进程记录的新，就重置自己的单例。判定用 wall-clock（``utcnow()`` 的 ISO 字符串），
   因为不同进程之间无法比较 ``monotonic()``；
3. ``applied_generation(engine)`` 记录本进程已应用到的时间戳（进程内字典）。

跨进程一致性靠 DB 写锁（``write_lock(con, "engine-reset")``）保证；读路径只有一次
主键查询，且在「世代号未变」时立即返回，不影响任务热路径。
"""
from __future__ import annotations

from . import warmup
from .. import db

#: 支持重置的引擎（chem 是纯 Python 库，无需重置）。
ENGINES = ("speech", "embeddings", "formula", "table")

_SETTING_PREFIX = "engine.reset."
#: 本进程已应用到的世代号；键为引擎名，值为 DB 中的 ISO 时间戳字符串。
_applied: dict[str, str] = {}


def _modules() -> dict[str, object]:
    """延迟导入，避免 ``app.engine_api`` ↔ Worker 启动路径的循环导入。"""
    from . import embeddings_local, formula_ocr, speech_local, table_structure

    return {
        "speech": speech_local,
        "embeddings": embeddings_local,
        "formula": formula_ocr,
        "table": table_structure,
    }


def _reset_local(engine: str) -> None:
    module = _modules().get(engine)
    reset = getattr(module, "reset_engine", None)
    if callable(reset):
        reset()


def mark_reset(engine: str) -> dict:
    """记录重置意图并重置本进程单例；返回可供响应体展示的世代信息。"""
    if engine not in ENGINES:
        raise ValueError(f"未知引擎：{engine!r}")
    stamp = db.utcnow()
    with db.connection() as con:
        db.write_lock(con, "engine-reset")
        con.execute(
            "INSERT INTO app_settings(key,value,updated_at) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (_SETTING_PREFIX + engine, stamp, stamp),
        )
    _reset_local(engine)
    warmup.reset_state()
    _applied[engine] = stamp
    return {"engine": engine, "generation": stamp}


def _stored_generations() -> dict[str, str]:
    """一次查询取全部引擎的世代号（Worker 每个任务前调用，不能跑 4 次查询）。"""
    keys = [_SETTING_PREFIX + engine for engine in ENGINES]
    placeholders = ",".join("?" for _ in keys)
    with db.connection() as con:
        rows = con.execute(
            f"SELECT key, value FROM app_settings WHERE key IN ({placeholders})", keys
        ).fetchall()
    stored: dict[str, str] = {}
    for row in rows:
        key, value = row[0], row[1]
        engine = str(key)[len(_SETTING_PREFIX):]
        if value:
            stored[engine] = str(value)
    return stored


def apply_pending() -> list[str]:
    """把其它进程发起、本进程尚未应用的重置落到本地单例；返回被重置的引擎名。"""
    try:
        stored = _stored_generations()
    except Exception:  # pragma: no cover - DB 不可用时不影响任务执行
        return []
    reset: list[str] = []
    for engine, stamp in stored.items():
        if engine not in ENGINES or stamp == _applied.get(engine):
            continue
        try:
            _reset_local(engine)
        except Exception:  # pragma: no cover - 单个引擎重置失败不影响其它引擎
            continue
        _applied[engine] = stamp
        reset.append(engine)
    if reset:
        try:
            warmup.reset_state()
        except Exception:  # pragma: no cover - 预热状态清理失败不影响任务
            pass
    return reset


def generation(engine: str) -> str:
    """本进程已应用到的世代号（便于状态接口展示与测试断言）。"""
    return _applied.get(engine, "")


def forget_local_state() -> None:
    """清空进程内记录（测试用：模拟一个新进程）。"""
    _applied.clear()
