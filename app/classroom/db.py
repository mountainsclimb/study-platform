"""AI 课堂数据层：``cls_*`` 六张表的幂等建表、JSON 序列化与归属校验。

表结构与 ``INTEGRATION-CONTRACT.md`` §3.3 / §4.1 一致：前缀 ``cls_``，所有私有表带
``user_id`` 并建 ``(user_id, ...)`` 索引；JSON 统一存 TEXT；时间用 ``db.utcnow()`` 的
ISO-8601 UTC 字符串。DDL 一律 ``con.executescript(...)``，保证 SQLite / PostgreSQL 双兼容。
"""
from __future__ import annotations

import json
from typing import Any

from fastapi import HTTPException

from ..db import connection, utcnow, write_lock

# 课程素材（TTS 音频等）落盘目录（相对 db.DATA_DIR）。
ASSET_FOLDER = "classroom/assets"

COURSE_STATUSES = ("draft", "outline_ready", "generating", "ready", "failed")
SCENE_STATUSES = ("pending", "generating", "ready", "failed")
SESSION_ROLES = ("user", "assistant", "system")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cls_courses (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    topic TEXT NOT NULL,
    subject TEXT NOT NULL DEFAULT '',
    grade TEXT NOT NULL DEFAULT '',
    mode TEXT NOT NULL DEFAULT 'standard',
    description TEXT NOT NULL DEFAULT '',
    requirements TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'draft',
    outline_json TEXT NOT NULL DEFAULT '{}',
    config_json TEXT NOT NULL DEFAULT '{}',
    scene_count INTEGER NOT NULL DEFAULT 0,
    cover_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS cls_courses_user ON cls_courses(user_id, updated_at);

CREATE TABLE IF NOT EXISTS cls_scenes (
    id INTEGER PRIMARY KEY,
    course_id INTEGER NOT NULL REFERENCES cls_courses(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    scene_type TEXT NOT NULL,
    order_index INTEGER NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    content_json TEXT NOT NULL DEFAULT '{}',
    meta_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS cls_scenes_course_order ON cls_scenes(course_id, order_index);
CREATE INDEX IF NOT EXISTS cls_scenes_user ON cls_scenes(user_id, course_id);

CREATE TABLE IF NOT EXISTS cls_assets (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    course_id INTEGER REFERENCES cls_courses(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    storage_name TEXT NOT NULL,
    mime_type TEXT NOT NULL DEFAULT 'application/octet-stream',
    sha256 TEXT NOT NULL DEFAULT '',
    meta_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS cls_assets_user ON cls_assets(user_id, kind);
CREATE INDEX IF NOT EXISTS cls_assets_sha ON cls_assets(user_id, sha256);

CREATE TABLE IF NOT EXISTS cls_sessions (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    course_id INTEGER NOT NULL REFERENCES cls_courses(id) ON DELETE CASCADE,
    current_scene_id INTEGER REFERENCES cls_scenes(id) ON DELETE SET NULL,
    progress_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS cls_sessions_user ON cls_sessions(user_id, updated_at);
CREATE INDEX IF NOT EXISTS cls_sessions_course ON cls_sessions(course_id);

CREATE TABLE IF NOT EXISTS cls_turns (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES cls_sessions(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    scene_id INTEGER,
    role TEXT NOT NULL,
    speaker TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL DEFAULT '',
    actions_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS cls_turns_session ON cls_turns(session_id, id);

CREATE TABLE IF NOT EXISTS cls_quiz_attempts (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    course_id INTEGER NOT NULL REFERENCES cls_courses(id) ON DELETE CASCADE,
    scene_id INTEGER NOT NULL REFERENCES cls_scenes(id) ON DELETE CASCADE,
    answers_json TEXT NOT NULL DEFAULT '[]',
    score REAL NOT NULL DEFAULT 0,
    max_score REAL NOT NULL DEFAULT 0,
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS cls_quiz_attempts_user ON cls_quiz_attempts(user_id, scene_id);
"""


# ---------------------------------------------------------------------------
# JSON 工具
# ---------------------------------------------------------------------------

def json_load(raw: Any, default: Any = None):
    """宽松解析数据库里的 TEXT JSON；损坏或类型不符时返回默认值。"""
    if raw is None or raw == "":
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return default
    if value is None:
        return default
    if default is not None and not isinstance(value, type(default)):
        return default
    return value


def json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


# ---------------------------------------------------------------------------
# 建表（幂等）
# ---------------------------------------------------------------------------

def init_classroom_db() -> None:
    """启动时调用一次：幂等建表（PostgreSQL 下先取咨询锁，与 practice/ai 的初始化一致）。"""
    from .. import db as _db

    with connection() as con:
        if _db.postgres_enabled():
            write_lock(con, "classroom-schema")
        con.executescript(_SCHEMA)


# ---------------------------------------------------------------------------
# 归属校验与行序列化
# ---------------------------------------------------------------------------

def owned_course(course_id: int, user_id: int) -> dict:
    with connection() as con:
        row = con.execute(
            "SELECT * FROM cls_courses WHERE id=? AND user_id=?", (course_id, user_id)
        ).fetchone()
    if row is None:
        raise HTTPException(404, "未找到该课程")
    return dict(row)


def find_course(course_id: int, user_id: int) -> dict | None:
    with connection() as con:
        row = con.execute(
            "SELECT * FROM cls_courses WHERE id=? AND user_id=?", (course_id, user_id)
        ).fetchone()
    return dict(row) if row else None


def course_scenes(course_id: int, user_id: int | None = None) -> list[dict]:
    sql = "SELECT * FROM cls_scenes WHERE course_id=?"
    params: list[Any] = [course_id]
    if user_id is not None:
        sql += " AND user_id=?"
        params.append(user_id)
    sql += " ORDER BY order_index, id"
    with connection() as con:
        rows = con.execute(sql, tuple(params)).fetchall()
    return [dict(row) for row in rows]


def owned_scene(scene_id: int, user_id: int) -> dict:
    with connection() as con:
        row = con.execute(
            "SELECT s.* FROM cls_scenes s JOIN cls_courses c ON c.id=s.course_id "
            "WHERE s.id=? AND s.user_id=? AND c.user_id=?",
            (scene_id, user_id, user_id),
        ).fetchone()
    if row is None:
        raise HTTPException(404, "未找到该场景")
    return dict(row)


def owned_session(session_id: int, user_id: int) -> dict:
    with connection() as con:
        row = con.execute(
            "SELECT s.* FROM cls_sessions s JOIN cls_courses c ON c.id=s.course_id "
            "WHERE s.id=? AND s.user_id=? AND c.user_id=?",
            (session_id, user_id, user_id),
        ).fetchone()
    if row is None:
        raise HTTPException(404, "未找到该学习会话")
    return dict(row)


def owned_asset(asset_id: int, user_id: int) -> dict:
    with connection() as con:
        row = con.execute(
            "SELECT * FROM cls_assets WHERE id=? AND user_id=?", (asset_id, user_id)
        ).fetchone()
    if row is None:
        raise HTTPException(404, "未找到该素材")
    return dict(row)


def public_course(row) -> dict:
    data = dict(row)
    data.pop("user_id", None)
    data["outline"] = json_load(data.pop("outline_json", "{}"), {})
    data["config"] = json_load(data.pop("config_json", "{}"), {})
    data["cover"] = json_load(data.pop("cover_json", "{}"), {})
    return data


def public_scene(row) -> dict:
    """场景公开视图：对齐 OpenMAIC DSL 的 Scene 形状（补充 order/type 别名）。"""
    data = dict(row)
    data.pop("user_id", None)
    content = json_load(data.pop("content_json", "{}"), {})
    meta = json_load(data.pop("meta_json", "{}"), {})
    actions = meta.get("actions") if isinstance(meta.get("actions"), list) else []
    return {
        "id": data["id"],
        "stage_id": "course_%s" % data["course_id"],
        "course_id": data["course_id"],
        "title": data["title"],
        "type": data["scene_type"],
        "scene_type": data["scene_type"],
        "order": data["order_index"],
        "order_index": data["order_index"],
        "status": data["status"],
        "content": content,
        "actions": actions,
        "meta": meta,
        "created_at": data["created_at"],
        "updated_at": data["updated_at"],
    }


def public_session(row) -> dict:
    data = dict(row)
    data.pop("user_id", None)
    data["progress"] = json_load(data.pop("progress_json", "{}"), {})
    return data


def touch_course(con, course_id: int, user_id: int, **fields) -> None:
    """更新课程公开字段并刷新 updated_at（调用方负责 commit）。"""
    if not fields:
        return
    fields["updated_at"] = utcnow()
    assignments = ",".join("%s=?" % key for key in fields)
    con.execute(
        "UPDATE cls_courses SET %s WHERE id=? AND user_id=?" % assignments,
        (*fields.values(), course_id, user_id),
    )


def count_running_course_jobs(course_id: int, user_id: int) -> int:
    """统计该课程正在排队/运行中的 course 任务数（用于 409 与生成去重）。"""
    from ..ai.jobs import TERMINAL_STATES  # 延迟导入，避免 classroom 包初始化循环

    placeholders = ",".join("?" for _ in TERMINAL_STATES)
    with connection() as con:
        rows = con.execute(
            "SELECT request_json FROM ai_jobs WHERE user_id=? AND task='course' "
            "AND status NOT IN (%s)" % placeholders,
            (user_id, *TERMINAL_STATES),
        ).fetchall()
    count = 0
    for row in rows:
        try:
            request = json.loads(row["request_json"] or "{}")
        except (TypeError, ValueError):
            continue
        params = request.get("params") or {}
        if isinstance(params, dict) and int(params.get("course_id") or 0) == course_id:
            count += 1
    return count
