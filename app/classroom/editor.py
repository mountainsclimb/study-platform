"""P6a 幻灯片结构化编辑器后端：slide 内容保存（version CAS）与场景复制。

路由（均挂在 ``/api/classroom`` 前缀下，全部需登录；先校验归属，他人/不存在统一 404）：

- ``GET   /courses/{course_id}/scenes/{scene_id}/content`` → ``{scene:{id,course_id,title,scene_type,status,version,content,updated_at}}``
  供编辑器读取当前 ``version`` 与完整 DSL（``GET /courses/{id}`` 的公开场景不含 version）。
- ``PATCH /courses/{course_id}/scenes/{scene_id}/content`` body ``{version:int, content:object}``
  → ``{scene:{id,version,content}, warnings:[...]}``

  * 仅 ``scene_type=='slide'`` 可编辑，其他类型 422「只有幻灯片场景支持编辑」；
  * 用 :func:`schema.normalize_slide_content` 归一化 + 校验（未知元素类型 / 元素数量超限 /
    结构不可用 → 422 中文原因；越界坐标按 DSL 规则钳制到画布内）；
  * ``version`` 做 CAS：与 ``cls_scenes.version`` 不一致 → ``409 {detail:{message,version}}``
    （``detail.version`` 是服务端当前版本，前端据此「重新加载」或「覆盖保存」）；
  * 成功写 ``content_json``、``version=version+1``、``updated_at``，并刷新课程 ``updated_at``
    （播放器/导出读同一 ``content_json``，保存后立即生效）。

- ``POST /courses/{course_id}/scenes/{scene_id}/duplicate`` → ``{scene:{...,version:1}, course_id}``
  复制该场景（内容/动作元数据原样复制，标题加「（副本）」），``order_index`` 追加到课程末尾，
  课程 ``scene_count`` 同步 +1。

``cls_scenes.version`` 通过 :func:`ensure_columns` 幂等补齐（SQLite/PostgreSQL 双兼容：
``app.db.ensure_column``）。``init_classroom()`` 启动时经 ``register()`` 调用一次；各入口再按
「数据目录 + 引擎」缓存复查，测试里反复切换隔离数据目录也会重新补列。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..auth import current_user
from . import db as cdb
from . import schema

router = APIRouter()

#: 「当前进程已确认补过 version 列」的 (数据目录, 引擎) 集合；测试换 STUDY_DATA_DIR 后仍会重查。
_schema_ready: set[str] = set()

TITLE_MAX = 120


# ---------------------------------------------------------------------------
# 迁移 / 归属 / 行序列化
# ---------------------------------------------------------------------------

def _schema_key() -> str:
    from .. import db as app_db

    engine = "postgres" if app_db.postgres_enabled() else "sqlite"
    return "%s|%s" % (engine, getattr(app_db, "DATA_DIR", ""))


def ensure_columns() -> None:
    """幂等给 ``cls_scenes`` 补 ``version INTEGER NOT NULL DEFAULT 1``（CAS 依赖）。"""
    key = _schema_key()
    if key in _schema_ready:
        return
    from .. import db as app_db
    from ..db import ensure_column

    with app_db.connection() as con:
        ensure_column(con, "cls_scenes", "version", "INTEGER NOT NULL DEFAULT 1")
    _schema_ready.add(key)


def register() -> None:
    """应用启动时由 ``init_classroom() → register_jobs()`` 调用：幂等补列，不新增任务类型。"""
    ensure_columns()


def _owned_course_or_404(course_id: int, user_id: int) -> dict:
    course = cdb.find_course(course_id, user_id)
    if course is None:
        raise HTTPException(404, "未找到该课程")
    return course


def _scene_of_course(scene_id: int, course_id: int, user_id: int) -> dict:
    scene = cdb.owned_scene(scene_id, user_id)
    if int(scene["course_id"]) != int(course_id):
        raise HTTPException(404, "未找到该场景")
    return scene


def _scene_version(scene: dict) -> int:
    try:
        return max(1, int(scene.get("version") or 1))
    except (TypeError, ValueError):
        return 1


def _scene_payload(scene: dict) -> dict:
    """场景编辑视图：在公开场景字段之外附带 ``version`` 与可编辑 ``content``。"""
    content = cdb.json_load(scene.get("content_json"), {})
    if not isinstance(content, dict):
        content = {}
    return {
        "id": int(scene["id"]),
        "course_id": int(scene["course_id"]),
        "title": str(scene.get("title") or ""),
        "scene_type": str(scene.get("scene_type") or ""),
        "status": str(scene.get("status") or ""),
        "version": _scene_version(scene),
        "content": content,
        "updated_at": scene.get("updated_at"),
    }


def _raise_version_conflict(version: int) -> None:
    raise HTTPException(
        409,
        detail={
            "message": "幻灯片内容已被修改（版本冲突），请重新加载或覆盖保存。",
            "version": int(version),
        },
        headers={"X-Edit-Version": str(int(version))},
    )


# ---------------------------------------------------------------------------
# 内容校验
# ---------------------------------------------------------------------------

def _element_alias(etype: str) -> str | None:
    """复用 schema 的元素类型别名表（text/textbox/title→text 等）。"""
    aliases = getattr(schema, "_ELEMENT_ALIASES", None)
    if isinstance(aliases, dict):
        return aliases.get(etype)
    return etype if etype in schema.ELEMENT_TYPES else None


def _validate_slide_content(content: Any) -> tuple[dict, list[str]]:
    """校验编辑器提交的 slide 内容并返回 ``(归一化内容, warnings)``；非法抛 422 中文原因。"""
    if not isinstance(content, dict):
        raise HTTPException(422, "幻灯片内容必须是 JSON 对象")
    canvas = content.get("canvas") if isinstance(content.get("canvas"), dict) else content
    elements = canvas.get("elements") if isinstance(canvas, dict) else None
    if not isinstance(elements, list) or not elements:
        raise HTTPException(422, "幻灯片内容缺少 elements 数组，至少需要 1 个元素")
    if len(elements) > schema.MAX_ELEMENTS:
        raise HTTPException(
            422,
            "幻灯片元素最多 %d 个（当前 %d 个），请先删除多余元素再保存"
            % (schema.MAX_ELEMENTS, len(elements)),
        )
    for index, item in enumerate(elements, 1):
        if not isinstance(item, dict):
            raise HTTPException(422, "第 %d 个幻灯片元素必须是 JSON 对象" % index)
        raw_type = str(item.get("type") or item.get("elementType") or "").strip().lower()
        if not raw_type:
            raise HTTPException(422, "第 %d 个幻灯片元素缺少 type（可用：%s）"
                                % (index, "、".join(schema.ELEMENT_TYPES)))
        if _element_alias(raw_type) is None:
            raise HTTPException(
                422,
                "第 %d 个幻灯片元素类型不支持：%s（可用：%s）"
                % (index, raw_type, "、".join(schema.ELEMENT_TYPES)),
            )
    try:
        normalized, warnings = schema.normalize_slide_content(content)
    except schema.ClassroomSchemaError as exc:
        raise HTTPException(422, str(exc)) from None
    return normalized, warnings


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------

@router.get("/courses/{course_id}/scenes/{scene_id}/content")
def get_scene_content(course_id: int, scene_id: int, user=Depends(current_user)):
    """读取场景可编辑内容 + 当前 version（编辑器加载 / 冲突后重新加载）。"""
    ensure_columns()
    _owned_course_or_404(course_id, user["id"])
    scene = _scene_of_course(scene_id, course_id, user["id"])
    return {"scene": _scene_payload(scene)}


class SceneContentInput(BaseModel):
    version: int = Field(ge=1)
    content: Any = None


@router.patch("/courses/{course_id}/scenes/{scene_id}/content")
def save_scene_content(course_id: int, scene_id: int, data: SceneContentInput,
                       user=Depends(current_user)):
    """保存幻灯片内容（version CAS，仅 slide 场景）。"""
    ensure_columns()
    _owned_course_or_404(course_id, user["id"])
    scene = _scene_of_course(scene_id, course_id, user["id"])
    if str(scene.get("scene_type") or "") != "slide":
        raise HTTPException(422, "只有幻灯片场景支持编辑")
    content, warnings = _validate_slide_content(data.content)
    payload = cdb.json_dump(content)
    from .. import db as app_db
    from ..db import write_lock

    now = app_db.utcnow()
    with app_db.connection() as con:
        # 同一课程的编辑写入串行化（SQLite BEGIN IMMEDIATE / PG advisory lock）。
        write_lock(con, "classroom-scene-content")
        row = con.execute(
            "SELECT version FROM cls_scenes WHERE id=? AND course_id=? AND user_id=?",
            (scene_id, course_id, user["id"]),
        ).fetchone()
        if row is None:
            raise HTTPException(404, "未找到该场景")
        current_version = max(1, int(row["version"] or 1))
        if current_version != int(data.version):
            _raise_version_conflict(current_version)
        changed = con.execute(
            "UPDATE cls_scenes SET content_json=?, version=version+1, updated_at=? "
            "WHERE id=? AND course_id=? AND user_id=? AND version=?",
            (payload, now, scene_id, course_id, user["id"], current_version),
        ).rowcount
        if not changed:  # pragma: no cover - 写入锁下不应发生，兜底仍给出可解释冲突
            _raise_version_conflict(current_version)
        con.execute(
            "UPDATE cls_courses SET updated_at=? WHERE id=? AND user_id=?",
            (now, course_id, user["id"]),
        )
    return {
        "scene": {"id": int(scene_id), "version": current_version + 1, "content": content},
        "warnings": warnings,
    }


@router.post("/courses/{course_id}/scenes/{scene_id}/duplicate")
def duplicate_scene(course_id: int, scene_id: int, user=Depends(current_user)):
    """复制场景并追加到课程末尾（order_index 顺延，scene_count +1）。"""
    ensure_columns()
    course = _owned_course_or_404(course_id, user["id"])
    scene = _scene_of_course(scene_id, course_id, user["id"])
    from .. import db as app_db
    from ..db import write_lock

    now = app_db.utcnow()
    title = str(scene.get("title") or "").strip() or "未命名场景"
    suffix = "（副本）"
    if not title.endswith(suffix):
        title = (title[: TITLE_MAX - len(suffix)] + suffix)
    with app_db.connection() as con:
        write_lock(con, "classroom-scene-duplicate")
        row = con.execute(
            "SELECT COALESCE(MAX(order_index), 0) FROM cls_scenes WHERE course_id=?",
            (course_id,),
        ).fetchone()
        next_order = int(row[0] or 0) + 1
        new_id = con.execute(
            "INSERT INTO cls_scenes(course_id,user_id,scene_type,order_index,title,content_json,"
            "meta_json,status,version,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,1,?,?)",
            (
                course_id, user["id"], scene.get("scene_type") or "slide", next_order, title,
                scene.get("content_json") or "{}", scene.get("meta_json") or "{}",
                scene.get("status") or "ready", now, now,
            ),
        ).lastrowid
        con.execute(
            "UPDATE cls_courses SET scene_count=("
            "SELECT COUNT(*) FROM cls_scenes WHERE course_id=?), updated_at=? "
            "WHERE id=? AND user_id=?",
            (course_id, now, course_id, user["id"]),
        )
        created = con.execute("SELECT * FROM cls_scenes WHERE id=?", (new_id,)).fetchone()
    new_scene = dict(created) if created is not None else {}
    payload = cdb.public_scene(new_scene) if new_scene else {}
    payload["version"] = _scene_version(new_scene)
    payload["content"] = cdb.json_load(new_scene.get("content_json"), {}) if new_scene else {}
    return {"scene": payload, "course_id": int(course_id), "course_title": course.get("title") or ""}


__all__ = ["router", "register", "ensure_columns"]
