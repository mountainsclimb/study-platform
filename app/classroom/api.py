"""AI 课堂 HTTP API（契约 ``INTEGRATION-CONTRACT.md`` §4.1）。

路由全部挂在 ``/api/classroom`` 下（由包 ``__init__.py`` 的 router 统一前缀），全部需登录；
私有资源读写先校验 ``user_id`` 归属（他人资源 404，不泄露存在性）。错误统一中文 ``{detail}``。
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field

from ..auth import current_user
from .. import question_ref, storage, study_artifact, study_evidence
from ..ai import speech_local
from . import db as cdb
from . import generate, grading, prompts, schema, tts
from . import interactive
from . import images as image_slots
from . import multiagent

router = APIRouter()
_log = logging.getLogger("zhixu.classroom")

#: 交互场景 HTML 响应的独立 CSP（与 ``interactive.py`` 单点维护；沙箱路由据此覆盖主应用默认 CSP）。
SCENE_HTML_CSP = interactive.INTERACTIVE_CSP
MAX_PROGRESS_BYTES = 16 * 1024
MAX_HISTORY_TURNS = 20
#: 课程参考图上传上限（与 ``images.MAX_REFERENCE_BYTES`` 一致）；multipart 有界读取，超限 413。
MAX_REFERENCE_IMAGE_BYTES = image_slots.MAX_REFERENCE_BYTES


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------

def _db():
    from .. import db

    return db


async def _read_bounded_upload(file: UploadFile, limit: int, *, empty_detail: str,
                               too_large_detail: str, read_detail: str = "读取上传文件失败",
                               status_code: int = 413) -> bytes:
    """有界读取 multipart 上传：只探测 ``limit + 1`` 字节，避免客户端伪造大小导致整读。

    空内容按 422 中文引导；超过 ``limit`` 按 ``status_code``（默认 413）拒绝。错误风格与
    本模块既有上传校验一致（中文 ``{detail}``）。
    """
    try:
        payload = await file.read(int(limit) + 1)
    except Exception:
        raise HTTPException(422, read_detail) from None
    if not payload:
        raise HTTPException(422, empty_detail)
    if len(payload) > int(limit):
        raise HTTPException(status_code, too_large_detail)
    return payload


def _course_summary(row) -> dict:
    data = cdb.public_course(row)
    return {
        "id": data["id"],
        "title": data["title"],
        "topic": data["topic"],
        "subject": data["subject"],
        "grade": data["grade"],
        "mode": data["mode"],
        "interactive_mode": str(data.get("mode") or "") == "interactive",
        "status": data["status"],
        "description": data.get("description") or "",
        "scene_count": data["scene_count"],
        "cover": data.get("cover") or {},
        "source_count": len(generate.outline_sources(data.get("outline") if isinstance(data.get("outline"), dict) else {})),
        "created_at": data["created_at"],
        "updated_at": data["updated_at"],
    }


def _course_detail(row) -> dict:
    data = cdb.public_course(row)
    data["outline"] = data.get("outline") or {}
    data["config"] = data.get("config") or {}
    # P1：深度交互模式的显式能力位（与 config.interactive_mode/prompt 分支一致）。
    data["interactive_mode"] = str(data.get("mode") or "") == "interactive"
    return data


def _scene_content(row) -> dict:
    content = cdb.json_load(row.get("content_json"), {})
    return content if isinstance(content, dict) else {}


def _scene_meta(row) -> dict:
    meta = cdb.json_load(row.get("meta_json"), {})
    return meta if isinstance(meta, dict) else {}


def _scene_actions(row) -> list[dict]:
    actions = _scene_meta(row).get("actions")
    return actions if isinstance(actions, list) else []


def _scene_element_ids(row) -> list[str]:
    content = _scene_content(row)
    return schema.slide_element_ids(content) if content.get("type") == "slide" else []


def _require_provider(user_id: int, provider_id: int, model: str) -> dict:
    from ..ai.providers import _selected_provider

    return _selected_provider(user_id, provider_id, model)


def _has_any_provider(user_id: int) -> bool:
    with _db().connection() as con:
        count = con.execute("SELECT COUNT(*) FROM ai_providers WHERE user_id=?", (user_id,)).fetchone()[0]
    return int(count) > 0


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


def _session_turns(session_id: int, user_id: int, limit: int = MAX_HISTORY_TURNS) -> list[dict]:
    with _db().connection() as con:
        rows = con.execute(
            "SELECT * FROM cls_turns WHERE session_id=? AND user_id=? ORDER BY id DESC LIMIT ?",
            (session_id, user_id, limit),
        ).fetchall()
    turns = []
    for row in reversed(rows):
        data = dict(row)
        data["actions"] = cdb.json_load(data.pop("actions_json", "[]"), [])
        turns.append(data)
    return turns


def _append_turn(session_id: int, user_id: int, scene_id: int | None, role: str,
                 speaker: str, content: str, actions: list[dict] | None = None) -> int:
    now = _db().utcnow()
    with _db().connection() as con:
        turn_id = con.execute(
            "INSERT INTO cls_turns(session_id,user_id,scene_id,role,speaker,content,actions_json,created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (session_id, user_id, scene_id, role, speaker[:40], content[:4000],
             cdb.json_dump(actions or []), now),
        ).lastrowid
        con.execute("UPDATE cls_sessions SET updated_at=? WHERE id=? AND user_id=?", (now, session_id, user_id))
    return int(turn_id)


def _touch_session(session_id: int, user_id: int) -> None:
    with _db().connection() as con:
        con.execute(
            "UPDATE cls_sessions SET updated_at=? WHERE id=? AND user_id=?",
            (_db().utcnow(), session_id, user_id),
        )


def _scene_digest(row) -> str:
    meta = _scene_meta(row)
    digest = str(meta.get("digest") or "").strip()
    if digest:
        return digest
    content = _scene_content(row)
    spec = {"title": row.get("title"), "scene_type": row.get("scene_type"), "outline": {}}
    return generate._scene_digest_text(spec, content)


def _call_structured_helper():
    from ..ai.adapters import call_structured

    return call_structured


# ---------------------------------------------------------------------------
# 元信息
# ---------------------------------------------------------------------------

def _search_meta(user_id: int) -> dict:
    """P3 联网查找资料能力位：``/meta.search``（不泄露 Key；只给前端开关是否可用）。"""
    from ..ai.search import search_provider_info

    try:
        info = search_provider_info(user_id)
    except Exception:  # 配置读取失败按「不可用」处理，不影响课堂其它能力
        info = None
    if not info:
        return {
            "available": False,
            "provider": None,
            "endpoint": "/v1/tools/search",
            "hint": "联网查找资料使用 DeepSeek 官方 /v1/tools/search；请在「模型设置」添加 DeepSeek 连接后启用",
        }
    return {
        "available": True,
        "provider": info.get("provider") or "",
        "provider_id": info.get("provider_id"),
        "endpoint": info.get("endpoint") or "/v1/tools/search",
    }


@router.get("/meta")
def classroom_meta(user=Depends(current_user)):
    info = tts.tts_provider_info(user["id"])
    return {
        "enabled": True,
        "scene_types": list(schema.SCENE_TYPES),
        "scene_type_labels": dict(schema.SCENE_TYPE_LABELS),
        # M4 手工「原理演示」场景：AI 生成类型保持不变，演示按固定版本学习附件插入。
        "manual_scene_types": list(schema.MANUAL_SCENE_TYPES),
        "demo_artifact_kinds": {"demo": list(schema.DEMO_ARTIFACT_KINDS)},
        "modes": list(schema.MODES),
        "mode_labels": {"standard": "标准课堂", "interactive": "深度交互"},
        "depths": list(schema.DEPTHS),
        "widgets": [{"id": key, "label": label} for key, label in schema.WIDGET_LABELS.items()],
        "tts": {"provider": (info or {}).get("provider"), "model": (info or {}).get("model")},
        "asr": _asr_meta(user["id"]),
        # P1 多智能体课堂（增量字段，既有字段不变）。
        "agent_roles": [{"id": role, "label": multiagent.ROLE_LABELS[role]} for role in multiagent.AGENT_ROLES],
        "discussion_modes": [
            {"id": mode, "label": multiagent.DISCUSSION_MODE_LABELS[mode]}
            for mode in multiagent.DISCUSSION_MODES
        ],
        # P1 深度交互模式 + 可选配图（增量能力位，既有字段不变）。
        "interactive_mode": {
            "enabled": True,
            "mode": "interactive",
            "interactive_scenes_min": generate.MIN_INTERACTIVE_SCENES,
            "interactive_scenes_max": generate.MAX_INTERACTIVE_SCENES,
            "slide_hint": True,
        },
        "images": image_slots.image_provider_info(user["id"]),
        # P3 联网查找资料（DeepSeek /v1/tools/search）：生成大纲前检索，来源随课程落库。
        "search": _search_meta(user["id"]),
        "limits": {
            "scene_count_min": schema.MIN_SCENE_COUNT,
            "scene_count_max": schema.MAX_SCENE_COUNT,
            "scene_count_default": schema.DEFAULT_SCENE_COUNT,
            "questions_max": schema.MAX_QUESTIONS,
            "elements_max": schema.MAX_ELEMENTS,
            "actions_max": schema.MAX_ACTIONS,
            "html_max_bytes": schema.MAX_HTML_BYTES,
            "tts_text_max": tts.MAX_TTS_CHARS,
            "topic_max": 500,
            "requirements_max": 4000,
            "agents_max": multiagent.MAX_AGENTS,
            "classmates_max": multiagent.MAX_CLASSMATES,
            "agent_name_max": multiagent.MAX_AGENT_NAME_CHARS,
            "agent_persona_max": multiagent.MAX_AGENT_PERSONA_CHARS,
            "discussion_turns_max": multiagent.MAX_DISCUSSION_TURNS,
            "interject_turns_max": multiagent.MAX_INTERJECT_TURNS,
            "discussion_rounds_max": multiagent.MAX_ROUNDS,
            "discussion_text_max": multiagent.MAX_TURN_TEXT_CHARS,
            "whiteboard_ops_max": multiagent.MAX_WHITEBOARD_OPS,
            "whiteboard_text_max": multiagent.MAX_WB_TEXT_CHARS,
            "whiteboard_kinds": list(multiagent.WHITEBOARD_KINDS),
            "interactive_scenes_max": generate.MAX_INTERACTIVE_SCENES,
            "images_per_scene_max": image_slots.MAX_IMAGES_PER_SCENE,
            # M4：原理演示场景内嵌附件快照的整段 content 上限（260KB）。
            "demo_content_bytes": schema.MAX_DEMO_CONTENT_BYTES,
        },
        "prompt_version": prompts.PROMPT_VERSION,
        "multiagent_prompt_version": multiagent.PROMPT_VERSION,
    }


# ---------------------------------------------------------------------------
# 课程 CRUD
# ---------------------------------------------------------------------------

class CourseCreate(BaseModel):
    title: str | None = Field(default=None, max_length=60)
    topic: str = Field(min_length=2, max_length=500)
    subject: str | None = Field(default=None, max_length=40)
    grade: str | None = Field(default=None, max_length=40)
    mode: str = Field(default="standard")
    requirements: str = Field(default="", max_length=4000)
    document_ids: list[int] | None = Field(default=None, max_length=8)


class CoursePatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=60)
    description: str | None = Field(default=None, max_length=600)
    subject: str | None = Field(default=None, max_length=40)
    grade: str | None = Field(default=None, max_length=40)


def _validate_document_ids(user_id: int, document_ids: list[int] | None) -> list[int]:
    ids = [int(item) for item in (document_ids or []) if str(item).isdigit()]
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    with _db().connection() as con:
        owned = {
            int(row[0]) for row in con.execute(
                "SELECT id FROM ai_documents WHERE user_id=? AND id IN (%s)" % placeholders,
                (user_id, *ids),
            ).fetchall()
        }
    missing = [item for item in ids if item not in owned]
    if missing:
        raise HTTPException(404, "部分参考资料不存在或无权访问")
    return list(dict.fromkeys(ids))


@router.get("/courses")
def list_courses(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=50),
    status: str | None = None,
    user=Depends(current_user),
):
    sql = "SELECT * FROM cls_courses WHERE user_id=?"
    params: list[Any] = [user["id"]]
    if status:
        if status not in schema.COURSE_STATUSES:
            raise HTTPException(422, "未知课程状态")
        sql += " AND status=?"
        params.append(status)
    with _db().connection() as con:
        total = con.execute(
            "SELECT COUNT(*) FROM cls_courses WHERE user_id=?" + (" AND status=?" if status else ""),
            tuple(params),
        ).fetchone()[0]
        rows = con.execute(
            sql + " ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?",
            (*params, page_size, (page - 1) * page_size),
        ).fetchall()
    return {
        "items": [_course_summary(row) for row in rows],
        "total": int(total),
        "page": page,
        "page_size": page_size,
    }


@router.post("/courses")
def create_course(data: CourseCreate, user=Depends(current_user)):
    topic = data.topic.strip()
    if len(topic) < 2:
        raise HTTPException(422, "课程主题至少 2 个字")
    if data.mode not in schema.MODES:
        raise HTTPException(422, "课程模式只能是 standard 或 interactive")
    document_ids = _validate_document_ids(user["id"], data.document_ids)
    title = (data.title or "").strip() or topic[:30]
    config = {"document_ids": document_ids}
    now = _db().utcnow()
    with _db().connection() as con:
        course_id = con.execute(
            "INSERT INTO cls_courses(user_id,title,topic,subject,grade,mode,description,requirements,status,"
            "outline_json,config_json,scene_count,cover_json,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,'',?, 'draft','{}',? ,0,'{}',?,?)",
            (
                user["id"], title, topic, (data.subject or "").strip(), (data.grade or "").strip(),
                data.mode, data.requirements.strip(), cdb.json_dump(config), now, now,
            ),
        ).lastrowid
    return {"course": _course_detail(cdb.owned_course(course_id, user["id"]))}


@router.get("/courses/{course_id}")
def get_course(course_id: int, user=Depends(current_user)):
    course = _owned_course_or_404(course_id, user["id"])
    scenes = cdb.course_scenes(course_id, user["id"])
    payload: dict = {
        "course": _course_detail(course),
        "scenes": [cdb.public_scene(row) for row in scenes],
    }
    # 素材缺失降级：场景仍引用但素材行已不存在（被回收/丢失）时给出明确提示，页面显示占位而不是 500。
    missing_assets = image_slots.missing_scene_asset_ids(user["id"], scenes)
    if missing_assets:
        payload["missing_asset_ids"] = missing_assets
        payload["warnings"] = [
            "有 %d 个课堂素材已缺失或被回收，页面会显示占位内容；可重新生成配图或重新导入课程包恢复"
            % len(missing_assets)
        ]
    return payload


@router.patch("/courses/{course_id}")
def patch_course(course_id: int, data: CoursePatch, user=Depends(current_user)):
    course = _owned_course_or_404(course_id, user["id"])
    fields: dict[str, Any] = {}
    if data.title is not None:
        fields["title"] = data.title.strip() or course["title"]
    if data.description is not None:
        fields["description"] = data.description.strip()
    if data.subject is not None:
        fields["subject"] = data.subject.strip()
    if data.grade is not None:
        fields["grade"] = data.grade.strip()
    if not fields:
        return {"course": _course_detail(course)}
    fields["updated_at"] = _db().utcnow()
    assignments = ",".join("%s=?" % key for key in fields)
    with _db().connection() as con:
        con.execute(
            "UPDATE cls_courses SET %s WHERE id=? AND user_id=?" % assignments,
            (*fields.values(), course_id, user["id"]),
        )
    return {"course": _course_detail(cdb.owned_course(course_id, user["id"]))}


@router.delete("/courses/{course_id}")
def delete_course(course_id: int, user=Depends(current_user)):
    _owned_course_or_404(course_id, user["id"])
    if cdb.count_running_course_jobs(course_id, user["id"]) > 0:
        raise HTTPException(409, "该课程有生成任务正在运行，请先等待完成或取消")
    # 安全回收：只删除「无存活引用」的素材；共享素材迁移归属到仍引用它的课程并保留文件。
    audit = image_slots.reclaim_course_assets(user_id=user["id"], course_id=int(course_id))
    return {"deleted": True, "course_id": course_id, "assets": audit}


# ---------------------------------------------------------------------------
# 手工「原理演示」场景（M4）：把学习附件按固定版本插入课程
#
# 语义（方案 §4 学习附件 / §5.4 导出 / M4）：
# - 插入时读取附件「当前（或调用方指定的）revision」，把 data/preview/title 冻结成场景 content
#   快照，并记录 ``fixed_revision``；课堂播放、PPTX / 离线 HTML / 打印讲义 / ZIP 导出都只读该
#   快照，源附件后续升版（study_artifact.add_revision）不会自动改变已插入的演示场景；
# - 演示场景不由 AI 生成流程分配（``SCENE_TYPES`` 仍只有既有 4 类），只能走本组接口手工插入；
# - 错误口径：他人课程 / 他人附件 / 不存在的附件与场景 → 404（不泄露存在性）；生成任务运行中 /
#   场景数已满 / 删除非演示场景 → 409；附件类型不支持、内容超限、参数非法 → 422。
# ---------------------------------------------------------------------------

class DemoSceneCreate(BaseModel):
    """插入演示场景请求体：``revision`` 省略时读取附件当前版本并立即固定。"""

    artifact_id: str = Field(min_length=1, max_length=64)
    revision: int | None = Field(default=None, ge=1, le=schema.MAX_DEMO_REVISION)
    title: str | None = Field(default=None, max_length=120)
    source_note: str | None = Field(default=None, max_length=schema.MAX_DEMO_SOURCE_NOTE_CHARS)


def _demo_snapshot_hints(data: dict) -> tuple[list[str], int]:
    """从附件 data 提取可展示的假设与步骤数（只读取既有字段，不编造内容）。"""
    assumptions: list[str] = []
    raw_assumptions = data.get("assumptions")
    if isinstance(raw_assumptions, (list, tuple)):
        for item in raw_assumptions[: schema.MAX_DEMO_ASSUMPTIONS]:
            if isinstance(item, bool) or not isinstance(item, (str, int, float)):
                continue
            text = str(item).strip()
            if text:
                assumptions.append(text[: schema.MAX_DEMO_ASSUMPTION_CHARS])
    step_count = 0
    raw_steps = data.get("steps")
    if isinstance(raw_steps, (list, tuple)):
        step_count = min(len(raw_steps), schema.MAX_DEMO_STEP_COUNT)
    else:
        raw_count = data.get("step_count")
        if isinstance(raw_count, int) and not isinstance(raw_count, bool):
            step_count = max(0, min(raw_count, schema.MAX_DEMO_STEP_COUNT))
    return assumptions, step_count


def _demo_scene_meta(artifact: dict, content: dict, warnings: list[str]) -> dict:
    """演示场景 meta：写入固定版本摘要（tutor 摘要复用）与快照溯源信息，不暴露附件私有内容。"""
    snapshot = content.get("artifact") if isinstance(content.get("artifact"), dict) else {}
    kind = str(snapshot.get("kind") or "")
    kind_label = schema.DEMO_ARTIFACT_KIND_LABELS.get(kind, kind or "演示")
    revision = int(content.get("fixed_revision") or 0)
    lines = [
        "固定版本原理演示《%s》（%s，v%d）"
        % (snapshot.get("title") or "", kind_label, revision)
    ]
    note = str(content.get("source_note") or "").strip()
    if note:
        lines.append("说明：" + note)
    assumptions = content.get("assumptions")
    if isinstance(assumptions, list) and assumptions:
        lines.append("前提假设：" + "；".join(str(item) for item in assumptions))
    source_ref = str((artifact or {}).get("source_ref") or "").strip()
    if source_ref:
        lines.append("来源：" + source_ref[:200])
    return {
        "digest": "\n".join(lines),
        "warnings": list(warnings),
        "source": "study_artifact",
        "artifact_id": str(snapshot.get("artifact_id") or ""),
        "fixed_revision": revision,
        "kind": kind,
    }


@router.post("/courses/{course_id}/scenes/demo", status_code=201)
def add_demo_scene(course_id: int, data: DemoSceneCreate, user=Depends(current_user)):
    """把学习附件按固定版本插入课程末尾，返回 ``{scene, course:{id, scene_count}}``。

    - 课程归属先校验（他人课程 404）；生成任务运行中 409；场景数达上限 409；
    - 附件读取走 ``study_artifact.get_artifact``（他人/已删除/不存在版本统一 404）；
    - 附件类型必须是演示可用的 6 类之一（否则 422）；读取当前或指定 ``revision`` 的
      data/preview/title 组装快照并由 :func:`schema.normalize_demo_content` 校验；
    - 场景标题默认取附件标题，插入 ``scene_type='demo'``、``status='ready'``，
      ``order_index`` 追加到课程末尾，并刷新课程 ``scene_count`` / ``updated_at``。
    """
    _owned_course_or_404(course_id, user["id"])
    if cdb.count_running_course_jobs(course_id, user["id"]) > 0:
        raise HTTPException(409, "该课程有生成任务正在运行，请先等待完成或取消")
    scenes = cdb.course_scenes(course_id, user["id"])
    if len(scenes) >= schema.MAX_SCENE_COUNT:
        raise HTTPException(
            409, "场景数量已达上限（%d 个），请先删除不必要的场景" % schema.MAX_SCENE_COUNT,
        )
    bundle = study_artifact.get_artifact(user["id"], data.artifact_id, revision=data.revision)
    artifact = bundle.get("artifact") or {}
    revision_payload = bundle.get("revision") or {}
    kind = str(artifact.get("kind") or "")
    if kind not in schema.DEMO_ARTIFACT_KINDS:
        raise HTTPException(
            422,
            "该附件类型不支持课堂演示：仅支持 %s"
            % "、".join(schema.DEMO_ARTIFACT_KIND_LABELS.values()),
        )
    artifact_data = revision_payload.get("data")
    if not isinstance(artifact_data, dict):
        raise HTTPException(422, "附件内容不是可用的 JSON 对象，无法插入课堂演示")
    assumptions, step_count = _demo_snapshot_hints(artifact_data)
    raw_content = {
        "artifact": {
            "artifact_id": str(artifact.get("artifact_id") or ""),
            "revision": int(revision_payload.get("revision") or 0),
            "kind": kind,
            "title": str(artifact.get("title") or ""),
            "data": artifact_data,
            "preview": revision_payload.get("preview"),
        },
        "source_note": data.source_note or "",
        "assumptions": assumptions,
        "step_count": step_count,
        "fixed": True,
    }
    try:
        content, warnings = schema.normalize_demo_content(raw_content)
    except schema.ClassroomSchemaError as exc:
        raise HTTPException(422, str(exc)) from None
    scene_title = (data.title or "").strip() or str(artifact.get("title") or "").strip() or "原理演示"
    meta = _demo_scene_meta(artifact, content, warnings)
    now = _db().utcnow()
    with _db().connection() as con:
        cdb.write_lock(con, "classroom-demo-scene")
        row = con.execute(
            "SELECT COALESCE(MAX(order_index), 0) FROM cls_scenes WHERE course_id=?",
            (course_id,),
        ).fetchone()
        next_order = int(row[0] or 0) + 1
        scene_id = con.execute(
            "INSERT INTO cls_scenes(course_id,user_id,scene_type,order_index,title,content_json,"
            "meta_json,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,'ready',?,?)",
            (
                course_id, user["id"], "demo", next_order, scene_title,
                cdb.json_dump(content), cdb.json_dump(meta), now, now,
            ),
        ).lastrowid
        scene_count = int(
            con.execute(
                "SELECT COUNT(*) FROM cls_scenes WHERE course_id=?", (course_id,)
            ).fetchone()[0]
        )
        cdb.touch_course(con, course_id, user["id"], scene_count=scene_count)
        scene_row = con.execute("SELECT * FROM cls_scenes WHERE id=?", (scene_id,)).fetchone()
    return {
        "scene": cdb.public_scene(scene_row),
        "course": {"id": int(course_id), "scene_count": scene_count},
    }


@router.delete("/courses/{course_id}/scenes/{scene_id}")
def delete_demo_scene(course_id: int, scene_id: int, user=Depends(current_user)):
    """删除手工演示场景，并把剩余场景 ``order_index`` 重排为连续 ``0..n-1``。

    仅 ``scene_type=='demo'`` 可删除（其他类型 409「仅演示场景支持删除」，避免误删 AI 生成的
    课程内容）；他人课程 / 不存在场景 404；返回 ``{deleted:true, scene_id, course}``。
    """
    _owned_course_or_404(course_id, user["id"])
    scene = _scene_of_course(scene_id, course_id, user["id"])
    if str(scene.get("scene_type") or "") != "demo":
        raise HTTPException(409, "仅演示场景支持删除")
    now = _db().utcnow()
    with _db().connection() as con:
        cdb.write_lock(con, "classroom-demo-scene")
        con.execute(
            "DELETE FROM cls_scenes WHERE id=? AND course_id=? AND user_id=?",
            (scene_id, course_id, user["id"]),
        )
        rows = con.execute(
            "SELECT id FROM cls_scenes WHERE course_id=? AND user_id=? ORDER BY order_index, id",
            (course_id, user["id"]),
        ).fetchall()
        # 逐行按下标重排：目标下标只会被已处理行释放，不会与未处理行冲突（唯一索引安全）。
        for index, item in enumerate(rows):
            con.execute(
                "UPDATE cls_scenes SET order_index=?, updated_at=? WHERE id=?",
                (index, now, int(item["id"])),
            )
        scene_count = len(rows)
        cdb.touch_course(con, course_id, user["id"], scene_count=scene_count)
    return {
        "deleted": True,
        "scene_id": int(scene_id),
        "course": {"id": int(course_id), "scene_count": scene_count},
    }


# ---------------------------------------------------------------------------
# 生成任务
# ---------------------------------------------------------------------------

class GenerateInput(BaseModel):
    stage: str = Field(default="all")
    provider_id: int
    model: str = Field(min_length=1, max_length=160)
    scene_types: list[str] | None = Field(default=None, max_length=4)
    scene_count: int | None = Field(default=None, ge=schema.MIN_SCENE_COUNT, le=schema.MAX_SCENE_COUNT)
    depth: str | None = None
    requirements: str | None = Field(default=None, max_length=4000)
    material_text: str | None = Field(default=None, max_length=12000)
    # P2 可选配图（image slot）：不传 images 时按「有可用图片连接则自动配图」处理。
    images: bool | None = None
    images_per_scene: int | None = Field(default=None, ge=0, le=image_slots.MAX_IMAGES_PER_SCENE)
    image_provider_id: int | None = None
    image_model: str | None = Field(default=None, max_length=160)
    # P3 联网查找资料（DeepSeek /v1/tools/search）：生成大纲前检索，来源写入课程并随导出附带。
    web_search: bool = False
    web_search_limit: int | None = Field(
        default=None, ge=generate.MIN_WEB_SEARCH_LIMIT, le=generate.MAX_WEB_SEARCH_LIMIT,
    )
    # P4 平台精选题素材：source_ref=official:<公开编号>；撤回/争议/未发布题 404，格式非法 422。
    source_ref: str | None = Field(default=None, max_length=160)


class RegenerateInput(BaseModel):
    provider_id: int
    model: str = Field(min_length=1, max_length=160)
    instruction: str | None = Field(default=None, max_length=1000)


def _job_input(**kwargs):
    from ..ai.jobs import JobInput

    return JobInput(**kwargs)


def _default_scene_types(course: dict) -> list[str]:
    if str(course.get("mode") or "") == "interactive":
        return ["slide", "quiz", "interactive"]
    return ["slide", "quiz"]


def _normalize_scene_types_param(values: list[str] | None, course: dict) -> list[str]:
    if not values:
        return _default_scene_types(course)
    normalized = []
    for item in values:
        scene_type = schema.normalize_scene_type(item, default="")
        # AI 生成白名单只含既有 4 类：手工演示（demo）即使别名可识别也不能被分配生成。
        if not scene_type or scene_type not in schema.SCENE_TYPES:
            raise HTTPException(422, "存在不支持的场景类型：%s" % item)
        if scene_type not in normalized:
            normalized.append(scene_type)
    return normalized


@router.post("/courses/{course_id}/generate")
async def generate_course(course_id: int, data: GenerateInput, user=Depends(current_user)):
    course = _owned_course_or_404(course_id, user["id"])
    if data.stage not in generate.STAGES:
        raise HTTPException(422, "生成阶段只能是 outline / scenes / all")
    if not _has_any_provider(user["id"]):
        raise HTTPException(422, "请先在「模型设置」中配置并启用一个 AI 连接，再生成课程")
    _require_provider(user["id"], data.provider_id, data.model)
    if data.depth is not None and data.depth not in schema.DEPTHS:
        raise HTTPException(422, "生成深度只能是 brief / standard / deep")
    if data.stage == "scenes":
        outline = cdb.json_load(course.get("outline_json"), {})
        if not isinstance(outline, dict) or not outline.get("scenes"):
            raise HTTPException(422, "请先生成并确认课程大纲，再生成场景内容")
    scene_types = _normalize_scene_types_param(data.scene_types, course)
    if data.image_provider_id is not None or data.image_model:
        if not data.image_provider_id or not data.image_model:
            raise HTTPException(422, "指定配图连接时必须同时提供 image_provider_id 与 image_model")
        _require_provider(user["id"], data.image_provider_id, data.image_model)
    elif data.images is True and not image_slots.image_candidates(user["id"]):
        raise HTTPException(422, "请先在「模型设置」中配置支持图片生成的连接，或关闭本页配图")
    params = {
        "course_id": course_id,
        "stage": data.stage,
        "scene_types": scene_types,
        "scene_count": data.scene_count or schema.DEFAULT_SCENE_COUNT,
        "depth": data.depth or "standard",
    }
    if data.images is not None:
        params["images"] = bool(data.images)
    if data.images_per_scene is not None:
        params["images_per_scene"] = int(data.images_per_scene)
    if data.image_provider_id is not None:
        params["image_provider_id"] = int(data.image_provider_id)
    if data.image_model:
        params["image_model"] = data.image_model.strip()[:160]
    if data.material_text:
        params["material_text"] = data.material_text.strip()[:12000]
    # P4：平台精选题素材（格式非法 422；可见性由任务准入校验按查询时状态判定）。
    if data.source_ref is not None:
        seed_ref = generate.normalize_seed_source_ref(data.source_ref)
        if seed_ref:
            params["source_ref"] = seed_ref
    # P3：联网查找资料（stage=outline|all 时生成大纲前检索；scenes 阶段不重复检索）。
    params["web_search"] = bool(data.web_search)
    params["web_search_limit"] = int(
        data.web_search_limit if data.web_search_limit is not None else generate.DEFAULT_WEB_SEARCH_LIMIT
    )
    job_input = _job_input(
        task="course",
        provider_id=data.provider_id,
        model=data.model,
        requirements=(data.requirements or course.get("requirements") or "")[:4000],
        document_ids=(cdb.json_load(course.get("config_json"), {}) or {}).get("document_ids") or None,
        params=params,
    )
    from ..ai.jobs import submit_job

    job = await submit_job(user, job_input)
    return {"job": job, "course_id": course_id}


@router.post("/courses/{course_id}/scenes/{scene_id}/regenerate")
async def regenerate_scene(course_id: int, scene_id: int, data: RegenerateInput,
                           user=Depends(current_user)):
    course = _owned_course_or_404(course_id, user["id"])
    scene = _scene_of_course(scene_id, course_id, user["id"])
    if str(scene.get("scene_type") or "") == "demo":
        # 演示场景是固定版本附件快照，不参与 AI 重新生成；需要换版本请重新插入。
        raise HTTPException(
            409, "演示场景使用固定版本附件，不支持重新生成；如需更换请重新插入演示场景",
        )
    if not _has_any_provider(user["id"]):
        raise HTTPException(422, "请先在「模型设置」中配置并启用一个 AI 连接，再重新生成")
    _require_provider(user["id"], data.provider_id, data.model)
    params = {
        "course_id": course_id,
        "stage": "scenes",
        "scene_ids": [int(scene["id"])],
        "instruction": (data.instruction or "").strip()[:1000],
        "scene_types": [scene["scene_type"]],
        "scene_count": schema.DEFAULT_SCENE_COUNT,
        "depth": "standard",
    }
    job_input = _job_input(
        task="course",
        provider_id=data.provider_id,
        model=data.model,
        requirements=(course.get("requirements") or "")[:4000],
        params=params,
    )
    from ..ai.jobs import submit_job

    job = await submit_job(user, job_input)
    # 准入通过后再把场景标记为 pending（避免配额/校验失败时误改已有可用内容的状态）。
    with _db().connection() as con:
        con.execute(
            "UPDATE cls_scenes SET status='pending', updated_at=? WHERE id=? AND user_id=?",
            (_db().utcnow(), scene_id, user["id"]),
        )
    return {"job": job, "course_id": course_id, "scene_id": scene_id}


# ---------------------------------------------------------------------------
# 学习会话与进度
# ---------------------------------------------------------------------------

class SessionCreate(BaseModel):
    scene_id: int | None = None


class SessionPatch(BaseModel):
    scene_id: int | None = None
    progress: dict | None = None


@router.post("/courses/{course_id}/sessions")
def create_session(course_id: int, data: SessionCreate | None = None, user=Depends(current_user)):
    course = _owned_course_or_404(course_id, user["id"])
    scenes = cdb.course_scenes(course_id, user["id"])
    scene_id = data.scene_id if data and data.scene_id is not None else (scenes[0]["id"] if scenes else None)
    if scene_id is not None:
        _scene_of_course(scene_id, course_id, user["id"])
    now = _db().utcnow()
    with _db().connection() as con:
        session_id = con.execute(
            "INSERT INTO cls_sessions(user_id,course_id,current_scene_id,progress_json,created_at,updated_at) "
            "VALUES (?,?,?,'{}',?,?)",
            (user["id"], course["id"], scene_id, now, now),
        ).lastrowid
        row = con.execute("SELECT * FROM cls_sessions WHERE id=?", (session_id,)).fetchone()
    return {"session": cdb.public_session(row)}


@router.get("/sessions/{session_id}")
def get_session(session_id: int, user=Depends(current_user)):
    row = cdb.owned_session(session_id, user["id"])
    course = _owned_course_or_404(int(row["course_id"]), user["id"])
    scenes = cdb.course_scenes(course["id"], user["id"])
    return {
        "session": cdb.public_session(row),
        "course": _course_summary(course),
        "progress": cdb.public_session(row).get("progress") or {},
        "scenes": [
            {
                "id": scene["id"], "title": scene["title"], "type": scene["scene_type"],
                "order": scene["order_index"], "status": scene["status"],
                "has_actions": bool(_scene_actions(scene)),
                "placeholder": bool(_scene_meta(scene).get("placeholder")),
            }
            for scene in scenes
        ],
        "turns": _session_turns(session_id, user["id"], limit=20),
    }


@router.patch("/sessions/{session_id}")
def patch_session(session_id: int, data: SessionPatch, user=Depends(current_user)):
    row = cdb.owned_session(session_id, user["id"])
    scene_id = data.scene_id if data.scene_id is not None else row["current_scene_id"]
    if data.scene_id is not None:
        _scene_of_course(data.scene_id, int(row["course_id"]), user["id"])
    progress_json = row["progress_json"]
    if data.progress is not None:
        if not isinstance(data.progress, dict):
            raise HTTPException(422, "进度 progress 必须是 JSON 对象")
        payload = cdb.json_dump(data.progress)
        if len(payload.encode("utf-8")) > MAX_PROGRESS_BYTES:
            raise HTTPException(422, "学习进度数据过大")
        progress_json = payload
    with _db().connection() as con:
        con.execute(
            "UPDATE cls_sessions SET current_scene_id=?, progress_json=?, updated_at=? "
            "WHERE id=? AND user_id=?",
            (scene_id, progress_json, _db().utcnow(), session_id, user["id"]),
        )
        updated = con.execute("SELECT * FROM cls_sessions WHERE id=?", (session_id,)).fetchone()
    return {"session": cdb.public_session(updated)}


# ---------------------------------------------------------------------------
# 课堂同步对话（tutor）
# ---------------------------------------------------------------------------

class TutorInput(BaseModel):
    scene_id: int | None = None
    message: str = Field(min_length=1, max_length=2000)
    mode: str = Field(default="qa")
    history: list[dict] | None = Field(default=None, max_length=20)
    provider_id: int | None = None
    model: str | None = Field(default=None, max_length=160)


@router.post("/sessions/{session_id}/tutor")
async def tutor(session_id: int, data: TutorInput, user=Depends(current_user)):
    row = cdb.owned_session(session_id, user["id"])
    course = _owned_course_or_404(int(row["course_id"]), user["id"])
    scenes = cdb.course_scenes(course["id"], user["id"])
    if not scenes:
        raise HTTPException(422, "课程还没有场景，请先生成课程内容")
    explicit_id = data.scene_id if data.scene_id is not None else row["current_scene_id"]
    scene = None
    if explicit_id is not None:
        scene = next((item for item in scenes if int(item["id"]) == int(explicit_id)), None)
    if scene is None:
        scene = scenes[0]
    if data.mode not in ("qa", "discussion"):
        raise HTTPException(422, "对话模式只能是 qa 或 discussion")
    if not _has_any_provider(user["id"]):
        raise HTTPException(422, "请先在「模型设置」中配置并启用一个 AI 连接，再进行课堂对话")

    history = data.history if isinstance(data.history, list) else [
        {"role": turn["role"], "content": turn["content"]}
        for turn in _session_turns(session_id, user["id"], limit=8)
    ]
    digest = _scene_digest(scene)
    # 交互场景的运行时状态（由页面 postMessage → interactive-event 写入会话 progress）参与追问。
    progress = cdb.json_load(row.get("progress_json"), {})
    state_digest = interactive.tutor_state_digest(progress, scene["id"])
    if state_digest:
        digest = digest + "\n" + state_digest
    outline = cdb.json_load(course.get("outline_json"), {}) or {}
    position = int(scene["order_index"])
    prompt = prompts.build_tutor_prompt(
        course_title=outline.get("title") or course.get("title") or "",
        scene_title=scene["title"],
        scene_type=scene["scene_type"],
        scene_digest=digest,
        message=data.message,
        mode=data.mode,
        history=history,
        position=position,
        total=len(scenes),
    )
    element_ids = _scene_element_ids(scene)

    def validator(raw):
        reply, warnings = schema.normalize_tutor_reply(raw, element_ids=element_ids, scene_count=len(scenes))
        return {"reply": reply, "warnings": warnings}

    if data.provider_id is not None:
        if not data.model:
            raise HTTPException(422, "请选择课堂对话使用的模型")
        provider = _require_provider(user["id"], data.provider_id, data.model)
        model = data.model
    else:
        provider, model = _tutor_default_connection(user["id"])
    try:
        result, call = await _call_structured_helper()(
            provider, model, prompt, validator,
            token_limit=1500, user_id=user["id"],
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(503, "课堂对话暂时不可用：%s" % str(exc)[:160]) from None
    reply = result["reply"]
    _append_turn(session_id, user["id"], scene["id"], "user", "student", data.message)
    turn_id = _append_turn(
        session_id, user["id"], scene["id"], "assistant",
        reply["reply"]["speaker"], reply["reply"]["text"], reply["reply"]["actions"],
    )
    if reply.get("scene_hint"):
        hint = reply["scene_hint"]
        if "index" in hint and 0 <= int(hint["index"]) < len(scenes):
            target = scenes[int(hint["index"])]
            with _db().connection() as con:
                con.execute(
                    "UPDATE cls_sessions SET current_scene_id=?, updated_at=? WHERE id=? AND user_id=?",
                    (target["id"], _db().utcnow(), session_id, user["id"]),
                )
    usage = call.get("usage") if isinstance(call.get("usage"), dict) else {}
    return {
        "reply": reply["reply"],
        "scene_hint": reply.get("scene_hint"),
        "usage": usage,
        "model": call.get("model") or "",
        "provider_name": call.get("provider_name") or "",
        "turn_id": turn_id,
        "warnings": result.get("warnings") or [],
    }


def _tutor_default_connection(user_id: int) -> tuple[dict, str]:
    """课堂对话默认连接：用户第一条带模型的 AI 连接（P0 简化，可在请求里显式指定）。"""
    with _db().connection() as con:
        rows = con.execute(
            "SELECT * FROM ai_providers WHERE user_id=? ORDER BY id", (user_id,)
        ).fetchall()
    for row in rows:
        provider = dict(row)
        models = [str(item) for item in (cdb.json_load(provider.get("models_json"), []) or []) if str(item).strip()]
        if models:
            return provider, models[0]
    raise HTTPException(422, "请先在「模型设置」中配置并启用一个 AI 连接，再进行课堂对话")


# ---------------------------------------------------------------------------
# P1 多智能体课堂：角色名册（OpenMAIC GeneratedAgentConfig 风格）
# ---------------------------------------------------------------------------

class AgentsGenerateInput(BaseModel):
    provider_id: int | None = None
    model: str | None = Field(default=None, max_length=160)
    requirements: str | None = Field(default=None, max_length=1000)
    classmate_count: int | None = Field(default=None, ge=1, le=multiagent.MAX_CLASSMATES)


class AgentPatchItem(BaseModel):
    id: str = Field(min_length=1, max_length=multiagent.MAX_AGENT_ID_CHARS)
    name: str | None = Field(default=None, min_length=1, max_length=multiagent.MAX_AGENT_NAME_CHARS)
    persona: str | None = Field(default=None, min_length=multiagent.MIN_AGENT_PERSONA_CHARS,
                               max_length=multiagent.MAX_AGENT_PERSONA_CHARS)
    color: str | None = Field(default=None, max_length=16)
    priority: int | None = Field(default=None, ge=1, le=10)


class AgentsPatchInput(BaseModel):
    roster: dict | None = None
    teacher: AgentPatchItem | None = None
    classmates: list[AgentPatchItem] | None = Field(default=None, max_length=multiagent.MAX_CLASSMATES)
    clear: bool = False


class DiscussionInput(BaseModel):
    scene_id: int | None = None
    mode: str = Field(default="discussion")
    topic: str | None = Field(default=None, max_length=200)
    rounds: int = Field(default=multiagent.MIN_ROUNDS, ge=multiagent.MIN_ROUNDS, le=multiagent.MAX_ROUNDS)
    user_message: str | None = Field(default=None, max_length=2000)
    history: list[dict] | None = Field(default=None, max_length=12)
    provider_id: int | None = None
    model: str | None = Field(default=None, max_length=160)


class InterjectInput(BaseModel):
    scene_id: int | None = None
    message: str = Field(min_length=1, max_length=2000)
    provider_id: int | None = None
    model: str | None = Field(default=None, max_length=160)


def _course_config(course) -> dict:
    config = cdb.json_load((course or {}).get("config_json"), {})
    return config if isinstance(config, dict) else {}


def _save_course_config(course_id: int, user_id: int, config: dict) -> dict:
    with _db().connection() as con:
        con.execute(
            "UPDATE cls_courses SET config_json=?, updated_at=? WHERE id=? AND user_id=?",
            (cdb.json_dump(config), _db().utcnow(), course_id, user_id),
        )
    return cdb.owned_course(course_id, user_id)


def _agents_payload(course) -> dict:
    config = _course_config(course)
    roster = multiagent.roster_from_config(config)
    meta = multiagent.roster_meta_from_config(config)
    return {
        "roster": multiagent.roster_public(roster),
        "agents": multiagent.roster_agents(roster),
        "has_roster": multiagent.has_roster(roster),
        "prompt_version": multiagent.PROMPT_VERSION,
        "generated_at": meta.get("generated_at") or None,
        "limits": {
            "agents_max": multiagent.MAX_AGENTS,
            "classmates_max": multiagent.MAX_CLASSMATES,
            "name_max": multiagent.MAX_AGENT_NAME_CHARS,
            "persona_max": multiagent.MAX_AGENT_PERSONA_CHARS,
        },
        "roles": [{"id": role, "label": multiagent.ROLE_LABELS[role]} for role in multiagent.AGENT_ROLES],
    }


def _default_multiagent_connection(user_id: int, action: str) -> tuple[dict, str]:
    """多智能体课堂默认连接：用户第一条带模型的 AI 连接（与 tutor 同策略）。"""
    with _db().connection() as con:
        rows = con.execute(
            "SELECT * FROM ai_providers WHERE user_id=? ORDER BY id", (user_id,)
        ).fetchall()
    for row in rows:
        provider = dict(row)
        models = [str(item) for item in (cdb.json_load(provider.get("models_json"), []) or []) if str(item).strip()]
        if models:
            return provider, models[0]
    raise HTTPException(422, "请先在「模型设置」中配置并启用一个 AI 连接，再%s" % action)


def _multiagent_connection(user_id: int, provider_id: int | None, model: str | None, *,
                            action: str) -> tuple[dict, str]:
    if provider_id is not None:
        if not model:
            raise HTTPException(422, "请选择本次多智能体课堂使用的模型")
        return _require_provider(user_id, provider_id, model), model
    return _default_multiagent_connection(user_id, action)


def _session_scene(row, explicit_scene_id: int | None, scenes: list[dict]) -> dict:
    if explicit_scene_id is not None:
        scene = next((item for item in scenes if int(item["id"]) == int(explicit_scene_id)), None)
        if scene is None:
            raise HTTPException(404, "未找到该场景")
        return scene
    current = row.get("current_scene_id")
    if current is not None:
        scene = next((item for item in scenes if int(item["id"]) == int(current)), None)
        if scene is not None:
            return scene
    return scenes[0]


def _apply_scene_hint(session_id: int, user_id: int, hint, scenes: list[dict]) -> None:
    if not isinstance(hint, dict):
        return
    index = hint.get("index")
    if not isinstance(index, int) or not 0 <= index < len(scenes):
        return
    with _db().connection() as con:
        con.execute(
            "UPDATE cls_sessions SET current_scene_id=?, updated_at=? WHERE id=? AND user_id=?",
            (scenes[index]["id"], _db().utcnow(), session_id, user_id),
        )


def _call_summary(call: dict) -> dict:
    return {
        "usage": call.get("usage") if isinstance(call.get("usage"), dict) else {},
        "model": call.get("model") or "",
        "provider_name": call.get("provider_name") or "",
    }


@router.get("/courses/{course_id}/agents")
def get_course_agents(course_id: int, user=Depends(current_user)):
    course = _owned_course_or_404(course_id, user["id"])
    return _agents_payload(course)


@router.post("/courses/{course_id}/agents/generate")
async def generate_course_agents(course_id: int, data: AgentsGenerateInput, user=Depends(current_user)):
    course = _owned_course_or_404(course_id, user["id"])
    provider, model = _multiagent_connection(
        user["id"], data.provider_id, data.model, action="生成课堂角色名册",
    )
    config = _course_config(course)
    outline = cdb.json_load(course.get("outline_json"), {}) or {}
    prompt = multiagent.build_roster_prompt(
        title=(outline.get("title") or course.get("title") or ""),
        topic=course.get("topic") or "",
        subject=course.get("subject") or "",
        grade=course.get("grade") or "",
        mode=course.get("mode") or "standard",
        requirements=(data.requirements if data.requirements is not None else (course.get("requirements") or "")),
        classmate_count=data.classmate_count or multiagent.DEFAULT_CLASSMATES,
    )

    def validator(raw):
        roster, warnings = multiagent.normalize_agent_roster(raw)
        return {"roster": roster, "warnings": warnings}

    try:
        result, call = await _call_structured_helper()(
            provider, model, prompt, validator, token_limit=1500, user_id=user["id"],
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(503, "角色名册生成暂时不可用：%s" % str(exc)[:160]) from None
    roster = result["roster"]
    summary = _call_summary(call)
    warnings = list(result.get("warnings") or [])
    expected = data.classmate_count or multiagent.DEFAULT_CLASSMATES
    if len(roster.get("classmates") or []) != expected:
        warnings.append("模型返回了 %d 名同学（期望 %d 名），可手动编辑名册" % (
            len(roster.get("classmates") or []), expected))
    config = multiagent.with_roster_config(
        config, roster, model=summary["model"], provider_name=summary["provider_name"],
    )
    updated = _save_course_config(course_id, user["id"], config)
    payload = _agents_payload(updated)
    payload.update({
        "usage": summary["usage"],
        "model": summary["model"],
        "provider_name": summary["provider_name"],
        "warnings": warnings,
    })
    return payload


@router.patch("/courses/{course_id}/agents")
def patch_course_agents(course_id: int, data: AgentsPatchInput, user=Depends(current_user)):
    course = _owned_course_or_404(course_id, user["id"])
    config = _course_config(course)
    if data.clear:
        updated = _save_course_config(course_id, user["id"], multiagent.without_roster_config(config))
        return _agents_payload(updated)
    if data.roster is None and data.teacher is None and not data.classmates:
        raise HTTPException(422, "请提供完整的 roster，或要编辑的老师/同学角色")
    try:
        if data.roster is not None:
            roster, warnings = multiagent.normalize_agent_roster(data.roster)
        else:
            teacher_patch = data.teacher.model_dump(exclude_none=True) if data.teacher is not None else None
            classmate_patches = [item.model_dump(exclude_none=True) for item in (data.classmates or [])]
            roster, warnings = multiagent.merge_roster_patch(
                multiagent.roster_from_config(config),
                teacher_patch=teacher_patch,
                classmate_patches=classmate_patches,
            )
    except schema.ClassroomSchemaError as exc:
        raise HTTPException(422, str(exc)) from None
    config = multiagent.with_roster_config(config, roster, preserve_meta=True)
    updated = _save_course_config(course_id, user["id"], config)
    payload = _agents_payload(updated)
    payload["warnings"] = warnings
    return payload


# ---------------------------------------------------------------------------
# P1 多智能体课堂：多角色讨论 / 学生插话（同步，一次结构化调用）
# ---------------------------------------------------------------------------

@router.post("/sessions/{session_id}/discussion")
async def multiagent_discussion(session_id: int, data: DiscussionInput, user=Depends(current_user)):
    row = cdb.owned_session(session_id, user["id"])
    course = _owned_course_or_404(int(row["course_id"]), user["id"])
    scenes = cdb.course_scenes(course["id"], user["id"])
    if not scenes:
        raise HTTPException(422, "课程还没有场景，请先生成课程内容")
    scene = _session_scene(row, data.scene_id, scenes)
    if data.mode not in multiagent.DISCUSSION_MODES:
        raise HTTPException(422, "讨论模式只能是 discussion / qa / lecture")
    provider, model = _multiagent_connection(
        user["id"], data.provider_id, data.model, action="进行多角色课堂讨论",
    )
    roster = multiagent.roster_from_config(_course_config(course))
    if not multiagent.has_roster(roster):
        raise HTTPException(422, "请先生成课堂角色名册（1 名老师 + 同学），再进行多角色课堂讨论")
    progress = cdb.json_load(row.get("progress_json"), {})
    state_digest = interactive.tutor_state_digest(progress, scene["id"])
    history = data.history if isinstance(data.history, list) else _session_turns(session_id, user["id"], limit=8)
    outline = cdb.json_load(course.get("outline_json"), {}) or {}
    prompt = multiagent.build_discussion_prompt(
        course_title=outline.get("title") or course.get("title") or "",
        scene_title=scene["title"],
        scene_type=scene["scene_type"],
        scene_digest=_scene_digest(scene),
        roster=roster,
        mode=data.mode,
        topic=(data.topic or "").strip(),
        rounds=data.rounds,
        user_message=(data.user_message or "").strip(),
        history=history,
        position=int(scene["order_index"]),
        total=len(scenes),
        element_ids=_scene_element_ids(scene),
        state_digest=state_digest,
    )
    element_ids = set(_scene_element_ids(scene))
    scene_count = len(scenes)

    def validator(raw):
        script, warnings = multiagent.normalize_discussion_script(
            raw, roster=roster, element_ids=element_ids, scene_count=scene_count,
        )
        return {"script": script, "warnings": warnings}

    try:
        result, call = await _call_structured_helper()(
            provider, model, prompt, validator, token_limit=2500, user_id=user["id"],
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(503, "多角色课堂讨论暂时不可用：%s" % str(exc)[:160]) from None
    script = result["script"]
    user_turn_id = None
    if (data.user_message or "").strip():
        user_turn_id = _append_turn(
            session_id, user["id"], int(scene["id"]), "user", "student", data.user_message.strip(),
        )
    turn_ids = [
        _append_turn(
            session_id, user["id"], int(scene["id"]), turn["role"], turn["speaker_name"],
            turn["text"], turn["actions"],
        )
        for turn in script["turns"]
    ]
    _apply_scene_hint(session_id, user["id"], script.get("scene_hint"), scenes)
    summary = _call_summary(call)
    return {
        "turns": script["turns"],
        "scene_hint": script.get("scene_hint"),
        "mode": data.mode,
        "rounds": data.rounds,
        "usage": summary["usage"],
        "model": summary["model"],
        "provider_name": summary["provider_name"],
        "turn_ids": turn_ids,
        "user_turn_id": user_turn_id,
        "warnings": result.get("warnings") or [],
        "prompt_version": multiagent.PROMPT_VERSION,
    }


@router.post("/sessions/{session_id}/interject")
async def multiagent_interject(session_id: int, data: InterjectInput, user=Depends(current_user)):
    row = cdb.owned_session(session_id, user["id"])
    course = _owned_course_or_404(int(row["course_id"]), user["id"])
    scenes = cdb.course_scenes(course["id"], user["id"])
    if not scenes:
        raise HTTPException(422, "课程还没有场景，请先生成课程内容")
    scene = _session_scene(row, data.scene_id, scenes)
    message = data.message.strip()
    if not message:
        raise HTTPException(422, "插话内容不能为空")
    provider, model = _multiagent_connection(
        user["id"], data.provider_id, data.model, action="进行课堂插话",
    )
    roster = multiagent.roster_from_config(_course_config(course))
    if not multiagent.has_roster(roster):
        raise HTTPException(422, "请先生成课堂角色名册（1 名老师 + 同学），再进行课堂插话")
    progress = cdb.json_load(row.get("progress_json"), {})
    state_digest = interactive.tutor_state_digest(progress, scene["id"])
    history = _session_turns(session_id, user["id"], limit=8)
    outline = cdb.json_load(course.get("outline_json"), {}) or {}
    prompt = multiagent.build_interject_prompt(
        course_title=outline.get("title") or course.get("title") or "",
        scene_title=scene["title"],
        scene_type=scene["scene_type"],
        scene_digest=_scene_digest(scene),
        roster=roster,
        message=message,
        history=history,
        position=int(scene["order_index"]),
        total=len(scenes),
        element_ids=_scene_element_ids(scene),
        state_digest=state_digest,
    )
    element_ids = set(_scene_element_ids(scene))
    scene_count = len(scenes)

    def validator(raw):
        script, warnings = multiagent.normalize_discussion_script(
            raw, roster=roster, element_ids=element_ids, scene_count=scene_count,
            max_turns=multiagent.MAX_INTERJECT_TURNS,
        )
        return {"script": script, "warnings": warnings}

    try:
        result, call = await _call_structured_helper()(
            provider, model, prompt, validator, token_limit=1500, user_id=user["id"],
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(503, "课堂插话暂时不可用：%s" % str(exc)[:160]) from None
    script = result["script"]
    user_turn_id = _append_turn(
        session_id, user["id"], int(scene["id"]), "user", "student", message,
    )
    turn_ids = [
        _append_turn(
            session_id, user["id"], int(scene["id"]), turn["role"], turn["speaker_name"],
            turn["text"], turn["actions"],
        )
        for turn in script["turns"]
    ]
    _apply_scene_hint(session_id, user["id"], script.get("scene_hint"), scenes)
    summary = _call_summary(call)
    return {
        "turns": script["turns"],
        "scene_hint": script.get("scene_hint"),
        "mode": "interject",
        "usage": summary["usage"],
        "model": summary["model"],
        "provider_name": summary["provider_name"],
        "turn_ids": turn_ids,
        "user_turn_id": user_turn_id,
        "warnings": result.get("warnings") or [],
        "prompt_version": multiagent.PROMPT_VERSION,
    }


# ---------------------------------------------------------------------------
# 测验判分
# ---------------------------------------------------------------------------

class QuizAnswerInput(BaseModel):
    question_id: str = Field(min_length=1, max_length=80)
    values: list[str] | str | None = None


class QuizSubmitInput(BaseModel):
    scene_id: int
    answers: list[QuizAnswerInput] = Field(default_factory=list, max_length=schema.MAX_QUESTIONS)


# ---------------------------------------------------------------------------
# 课堂测验 → 学习证据（整合方案 §4「作答与学习证据」/ §8 工单 C）
#
# 课堂小测题目尚未正式入库，因此**不伪造** bank / app / official 引用：题目内容自带
# ``question_ref``（形如 bank:... / app:... / official:...）时原样使用，否则
# ``question_ref=None``，改用 ``record_id=quiz_attempt_id``、
# ``item_key=<course_id>:<scene_id>:<题目键>`` 标识，并在 meta 里记录
# course_id / scene_id / question_key。同一 quiz attempt 的同一题事件键固定为
# ``quiz:<quiz_attempt_id>:<item_key>``，重复写入由 study_evidence 的幂等键去重。
# 证据写入失败只记日志，绝不影响测验提交主流程与既有响应字段。
# ---------------------------------------------------------------------------

#: 学习证据表就绪缓存键：(数据目录, 是否 PostgreSQL)。换库/测试临时目录后自动重新幂等建表。
_evidence_ready_key: tuple[str, bool] | None = None


def _ensure_study_evidence_ready() -> None:
    """幂等确保学习证据表已建好。

    ``study_evidence.record_event(con=...)`` 约定由启动钩子建表；课堂测验这里再按进程
    幂等兜底一次，避免启动顺序不同导致证据静默丢失。调用方负责捕获异常（证据失败不影响提交）。
    """
    global _evidence_ready_key
    app_db = _db()
    key = (str(app_db.DATA_DIR), app_db.postgres_enabled())
    if _evidence_ready_key == key:
        return
    study_evidence.init_study_evidence()
    _evidence_ready_key = key


def _quiz_question_ref(question) -> str | None:
    """取测验题目自带的统一题目引用；没有显式 bank/app/official 前缀时返回 None（不伪造）。"""
    raw = question.get("question_ref") if isinstance(question, dict) else None
    text = str(raw or "").strip()
    if not text:
        return None
    source, separator, key = text.partition(":")
    if not separator or source.strip().lower() not in question_ref.SOURCES or not key.strip():
        return None
    try:
        return question_ref.parse(text).ref
    except question_ref.QuestionRefError:
        return None


def _quiz_verdict(detail: dict) -> tuple[str, str]:
    """课堂判分明细 → (verdict, grade_source)；主观题只标待核验，不猜对错。"""
    if detail.get("needs_review") or detail.get("correct") is None:
        return "pending", "pending"
    return ("correct", "reference") if detail.get("correct") else ("incorrect", "reference")


def _record_quiz_evidence(*, con, user_id: int, course_id: int, scene_id: int,
                          attempt_id: int, content: dict, result: dict) -> None:
    """按判分结果逐题写学习证据（同事务执行，异常只记日志）。"""
    try:
        questions = content.get("questions") if isinstance(content, dict) else []
        questions = questions if isinstance(questions, list) else []
        by_id: dict[str, dict] = {}
        for question in questions:
            if not isinstance(question, dict):
                continue
            key = str(question.get("id") or "").strip()
            if key:
                by_id.setdefault(key, question)
        detail = result.get("detail") if isinstance(result, dict) else None
        detail = detail if isinstance(detail, list) else []
        if not detail:
            # 判分只有总分、没有逐题明细时只记一条汇总证据，不编造逐题对错。
            study_evidence.record_event(
                user_id, kind="classroom_quiz", record_kind="cls_quiz_attempts",
                record_id=int(attempt_id), item_key="summary",
                event_key=f"quiz:{int(attempt_id)}:summary",
                question_ref=None, verdict="pending", grade_source="pending",
                prompt_state="unknown", reveal_state="after", con=con,
                meta={
                    "course_id": int(course_id), "scene_id": int(scene_id),
                    "score": result.get("score"), "max_score": result.get("max_score"),
                    "note": "课堂测验仅总分，无逐题核验",
                },
            )
            return
        for index, item in enumerate(detail):
            if not isinstance(item, dict):
                continue
            question_key = str(item.get("question_id") or "").strip() or "q%d" % (index + 1)
            item_key = "%d:%d:%s" % (int(course_id), int(scene_id), question_key)
            verdict, grade_source = _quiz_verdict(item)
            study_evidence.record_event(
                user_id, kind="classroom_quiz", record_kind="cls_quiz_attempts",
                record_id=int(attempt_id), item_key=item_key,
                event_key=f"quiz:{int(attempt_id)}:{item_key}",
                question_ref=_quiz_question_ref(by_id.get(question_key)),
                verdict=verdict, grade_source=grade_source,
                prompt_state="unknown", reveal_state="after", con=con,
                meta={
                    "course_id": int(course_id), "scene_id": int(scene_id),
                    "question_key": question_key,
                    "question_type": item.get("type"),
                    "score": result.get("score"), "max_score": result.get("max_score"),
                    "points": item.get("points"), "earned": item.get("earned"),
                    "needs_review": bool(item.get("needs_review")),
                },
            )
    except Exception:
        _log.exception("课堂测验学习证据写入失败：attempt=%s scene=%s", attempt_id, scene_id)


@router.post("/sessions/{session_id}/quiz")
def submit_quiz(session_id: int, data: QuizSubmitInput, user=Depends(current_user)):
    row = cdb.owned_session(session_id, user["id"])
    scene = _scene_of_course(data.scene_id, int(row["course_id"]), user["id"])
    if scene["scene_type"] != "quiz":
        raise HTTPException(422, "该场景不是测验场景")
    if not data.answers:
        raise HTTPException(422, "请先作答至少一道题")
    content = _scene_content(scene)
    answers = [item.model_dump() for item in data.answers]
    result = grading.grade_quiz(content, answers)
    evidence_ready = True
    try:
        _ensure_study_evidence_ready()
    except Exception:
        # 证据表不可用只在日志里提示，不阻断测验提交。
        evidence_ready = False
        _log.exception("学习证据表初始化失败，本次课堂测验跳过证据写入")
    now = _db().utcnow()
    with _db().connection() as con:
        attempt_id = con.execute(
            "INSERT INTO cls_quiz_attempts(user_id,course_id,scene_id,answers_json,score,max_score,detail_json,created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                user["id"], int(row["course_id"]), int(scene["id"]), cdb.json_dump(answers),
                result["score"], result["max_score"], cdb.json_dump(result), now,
            ),
        ).lastrowid
        con.execute("UPDATE cls_sessions SET updated_at=? WHERE id=? AND user_id=?", (now, session_id, user["id"]))
        if evidence_ready:
            _record_quiz_evidence(
                con=con, user_id=user["id"], course_id=int(row["course_id"]),
                scene_id=int(scene["id"]), attempt_id=int(attempt_id),
                content=content, result=result,
            )
    return {
        "attempt_id": int(attempt_id),
        "scene_id": int(scene["id"]),
        "score": result["score"],
        "max_score": result["max_score"],
        "correct_count": result["correct_count"],
        "question_count": result["question_count"],
        "needs_review_count": result["needs_review_count"],
        "detail": result["detail"],
    }


# ---------------------------------------------------------------------------
# TTS 与素材
# ---------------------------------------------------------------------------

class TtsInput(BaseModel):
    text: str = Field(min_length=1, max_length=tts.MAX_TTS_CHARS)
    scene_id: int | None = None
    voice: str | None = Field(default=None, max_length=60)
    speed: float | None = Field(default=None, ge=0.5, le=2.0)
    provider_id: int | None = None
    model: str | None = Field(default=None, max_length=160)


def _course_config_map(course) -> dict:
    config = cdb.json_load((course or {}).get("config_json"), {})
    return config if isinstance(config, dict) else {}


@router.post("/courses/{course_id}/tts")
async def synthesize_tts(course_id: int, data: TtsInput, user=Depends(current_user)):
    course = _owned_course_or_404(course_id, user["id"])
    if data.scene_id is not None:
        _scene_of_course(data.scene_id, course_id, user["id"])
    # 未显式指定音色时沿用课程配置（P4 音色列表选择会写入 config.voice）。
    voice = data.voice or str(_course_config_map(course).get("voice") or "").strip() or None
    result = await tts.synthesize(
        user_id=user["id"], course_id=course_id, text=data.text,
        voice=voice, speed=data.speed, provider_id=data.provider_id, model=data.model,
    )
    result["course_id"] = course_id
    if data.scene_id is not None:
        result["scene_id"] = data.scene_id
    return result


@router.get("/voices")
async def classroom_voices(provider_id: int | None = Query(None), user=Depends(current_user)):
    """P4 音色列表：聚合已配置 TTS 连接的可用音色（MiniMax /get_voice 缓存 10 分钟，失败回退静态）。"""
    if provider_id is not None and int(provider_id) != 0:
        candidates = tts.tts_candidates(user["id"])
        if not any(int(item["provider"]["id"]) == int(provider_id) for item in candidates):
            raise HTTPException(404, "未找到该语音合成连接")
    # provider_id=0 是本地语音的内部/展示哨兵；tts.list_voices 会按本地可用性返回。
    return await tts.list_voices(user["id"], provider_id=provider_id)


# ---------------------------------------------------------------------------
# 图片编辑（带参考图的配图生成）：MiniMax subject_reference / OpenAI 兼容 /images/edits
# ---------------------------------------------------------------------------

MAX_IMAGE_PROMPT_CHARS = 600


@router.post("/courses/{course_id}/images")
async def generate_course_image(
    course_id: int,
    prompt: str = Form(..., min_length=1, max_length=MAX_IMAGE_PROMPT_CHARS),
    scene_id: int | None = Form(None),
    provider_id: int | None = Form(None),
    model: str | None = Form(None),
    reference: UploadFile | None = File(None),
    user=Depends(current_user),
):
    """按提示词生成配图（可选参考图做图片编辑）→ 落 ``cls_assets`` 并返回素材公开信息。"""
    course = _owned_course_or_404(course_id, user["id"])
    scene = _scene_of_course(scene_id, course_id, user["id"]) if scene_id is not None else None
    if provider_id is not None or model:
        if not provider_id or not model:
            raise HTTPException(422, "指定图片连接时必须同时提供 provider_id 与 model")
    candidate = image_slots.resolve_candidate(user["id"], provider_id, model)
    if candidate is None:
        raise HTTPException(
            422,
            "还没有可用的图片生成连接：请在「模型设置」中添加支持图片生成的连接"
            "（OpenAI 兼容 /images/generations 或 MiniMax image-01），或关闭本页配图",
        )
    text = str(prompt or "").strip()
    if not text:
        raise HTTPException(422, "请填写配图描述（提示词）")
    reference_bytes = None
    if reference is not None:
        reference_bytes = await _read_bounded_upload(
            reference, MAX_REFERENCE_IMAGE_BYTES,
            empty_detail="参考图为空，请重新选择图片",
            too_large_detail="参考图不能超过 %d MB，请压缩后重试"
                             % (MAX_REFERENCE_IMAGE_BYTES // (1024 * 1024)),
            read_detail="参考图读取失败，请重新选择图片",
        )
    try:
        asset = await image_slots.generate_scene_image(
            user_id=user["id"], course_id=course_id, candidate=candidate, prompt=text,
            scene_id=int(scene["id"]) if scene is not None else None,
            reference=reference_bytes,
        )
    except ValueError as exc:
        message = str(exc)[:200]
        if message.startswith("参考图"):
            # 参考图压缩/校验失败（Pillow：最长边 1600 / JPEG 85 / ≤8MB）：原样给中文引导。
            raise HTTPException(422, message) from exc
        raise HTTPException(422, "配图生成失败：%s" % message) from exc
    except Exception as exc:  # noqa: BLE001 - 上游网络异常统一为可读失败
        raise HTTPException(502, "配图服务暂不可用（%s）" % type(exc).__name__) from exc
    image_slots.remember_generated_image(user["id"], course_id, asset)
    public_asset = {
        key: asset.get(key) for key in ("id", "kind", "mime_type", "url", "meta", "created_at")
    }
    return {
        "asset": public_asset,
        "course_id": course_id,
        "scene_id": int(scene["id"]) if scene is not None else None,
        "provider": candidate["provider"].get("name") or "",
        "model": str(candidate["model"]),
        "prompt": text,
        "size": int(asset.get("size") or 0),
        "edit": bool(asset.get("edit")),
        "warnings": list(asset.get("warnings") or []),
    }


# ---------------------------------------------------------------------------
# 服务端语音识别（ASR）兜底：浏览器不支持 SpeechRecognition 时使用
# ---------------------------------------------------------------------------

MAX_ASR_BYTES = 25 * 1024 * 1024
_ASR_MIME_FALLBACK = "audio/wav"


def _asr_candidates(user_id: int) -> list[dict]:
    """用户已配置、且模型名带 ASR 标记的连接（如 ``mimo-v2.5-asr``）。"""
    from ..ai import audio as ai_audio
    from .. import db

    with db.connection() as con:
        providers = [dict(row) for row in con.execute(
            "SELECT id,name,kind,base_url,encrypted_key,models_json,extra_headers_enc,compat_json,auth_mode "
            "FROM ai_providers WHERE user_id=? ORDER BY id", (user_id,),
        ).fetchall()]
    candidates: list[dict] = []
    for provider in providers:
        models = [str(item) for item in (cdb.json_load(provider.get("models_json"), []) or []) if str(item).strip()]
        matched = [model for model in models if ai_audio.is_asr_model(model)]
        if matched:
            candidates.append({"provider": provider, "model": matched[0]})
    return candidates


def _asr_candidate(user_id: int, provider_id: int | None, model: str | None) -> dict:
    candidates = _asr_candidates(user_id)
    if provider_id:
        for item in candidates:
            if int(item["provider"]["id"]) == int(provider_id):
                if model and model not in [str(m) for m in cdb.json_load(item["provider"].get("models_json"), []) or []]:
                    raise HTTPException(422, "该连接没有配置这个语音识别模型")
                return {"provider": item["provider"], "model": model or item["model"]}
        raise HTTPException(404, "未找到该模型连接")
    if model:
        for item in candidates:
            models = [str(m) for m in cdb.json_load(item["provider"].get("models_json"), []) or []]
            if model in models:
                return {"provider": item["provider"], "model": model}
        raise HTTPException(422, "该模型未在已配置的连接中，请先在模型设置保存该模型")
    if not candidates:
        raise HTTPException(
            422,
            "还没有可用的语音识别模型：请在「模型设置」添加支持语音识别的连接"
            "（例如小米 MiMo 的 mimo-v2.5-asr）",
        )
    return candidates[0]


@router.get("/asr/info")
def asr_info(user=Depends(current_user)):
    """前端探测：当前账号是否有服务端语音识别能力。"""
    candidates = _asr_candidates(user["id"])
    if not candidates:
        if speech_local.asr_available():
            return {
                "available": True,
                "provider": "local",
                "provider_id": None,
                "model": speech_local.asr_model_label(),
                "max_bytes": MAX_ASR_BYTES,
                "max_seconds": speech_local.max_asr_seconds(),
                "notice": "本地识别只接受 16k 单声道 PCM16 WAV；前端会用 WebAudio 自动转换。",
            }
        return {"available": False, "provider": None, "model": None, "max_bytes": MAX_ASR_BYTES}
    first = candidates[0]
    return {
        "available": True,
        "provider": first["provider"].get("name") or "",
        "provider_id": int(first["provider"]["id"]),
        "model": first["model"],
        "max_bytes": MAX_ASR_BYTES,
    }


def _asr_meta(user_id: int) -> dict:
    """``GET /meta`` 用：服务端 ASR 可用性摘要（不暴露 Key）。"""
    candidates = _asr_candidates(user_id)
    if not candidates:
        if speech_local.asr_available():
            return {
                "available": True,
                "provider": "local",
                "model": speech_local.asr_model_label(),
            }
        return {"available": False, "provider": None, "model": None}
    return {
        "available": True,
        "provider": candidates[0]["provider"].get("name") or "",
        "model": candidates[0]["model"],
    }


@router.post("/asr")
async def transcribe_audio(
    file: UploadFile = File(...),
    provider_id: int | None = Form(None),
    model: str | None = Form(None),
    user=Depends(current_user),
):
    """服务端语音转写：浏览器无 ``SpeechRecognition``（Safari/Firefox）时兜底。

    只接受音频（≤25MB），返回 ``{text, model, usage}``；未配置语音识别模型时 422 中文引导。
    """
    from ..ai import audio as ai_audio

    payload = await _read_bounded_upload(
        file, MAX_ASR_BYTES,
        empty_detail="音频文件为空",
        too_large_detail="音频文件不能超过 25MB",
        read_detail="读取音频文件失败",
    )
    mime = (file.content_type or "").split(";")[0].strip() or _ASR_MIME_FALLBACK
    if not mime.startswith("audio/") and mime not in ("video/webm", "application/octet-stream"):
        raise HTTPException(422, "请上传音频文件（webm / wav / mp3 / m4a）")
    local_selector = (
        (not provider_id or int(provider_id) == 0)
        and (not model or str(model).strip() == speech_local.asr_model_label())
    )
    if local_selector and not _asr_candidates(user["id"]) and speech_local.asr_available():
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(speech_local.transcribe, payload),
                timeout=speech_local.TRANSCRIBE_TIMEOUT_SECONDS,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - 本地引擎异常统一为可读失败
            raise HTTPException(502, "本地语音识别失败（%s）" % type(exc).__name__) from exc
        return {
            "text": result.get("text") or "",
            "model": speech_local.asr_model_label(),
            "usage": None,
            "filename": file.filename,
        }
    candidate = _asr_candidate(user["id"], provider_id, model)
    try:
        result = await ai_audio.transcribe(candidate["provider"], candidate["model"], payload, mime)
    except ValueError as exc:
        raise HTTPException(502, "语音识别失败：%s" % exc) from exc
    except Exception as exc:  # noqa: BLE001 - 上游网络异常统一为可读失败
        raise HTTPException(502, "语音识别服务暂不可用（%s）" % type(exc).__name__) from exc
    return {
        "text": result.get("text") or "",
        "model": result.get("model") or candidate["model"],
        "usage": result.get("usage") or {},
        "filename": file.filename,
    }


@router.get("/assets/{asset_id}")
def get_asset(asset_id: int, request: Request, user=Depends(current_user)):
    """读取课堂素材：按内容识别真实类型，只对白名单媒体内联返回，其余强制隔离下载。

    - 文件缺失/已被回收 → 404 中文降级（页面显示占位），绝不 500；
    - SVG/HTML 等主动内容即使被声明成图片也不会内联，统一 ``attachment`` + 二进制下载。
    """
    row = cdb.owned_asset(asset_id, user["id"])
    try:
        content = storage.read_file(tts.TTS_FOLDER, row["storage_name"])
    except HTTPException as exc:
        if exc.status_code == 404:
            raise HTTPException(404, "素材文件缺失或已被回收，请重新生成或重新导入课程") from None
        raise
    etag = str(row.get("sha256") or "").strip() or _hash_bytes(content)
    quoted = '"%s"' % etag
    if _etag_matches(request.headers.get("if-none-match"), quoted):
        return Response(status_code=304, headers={
            "ETag": quoted,
            "Cache-Control": "private, max-age=3600",
        })
    plan = image_slots.asset_serving_plan(dict(row), content)
    headers = {
        "ETag": quoted,
        "Cache-Control": "private, max-age=3600",
        "X-Content-Type-Options": "nosniff",
    }
    if not plan["inline"]:
        # 隔离下载：正确 media type + attachment，主动内容已降级为 application/octet-stream。
        headers["Content-Disposition"] = plan["disposition"]
        headers["Content-Security-Policy"] = "default-src 'none'; sandbox"
    return Response(content=content, media_type=plan["media_type"], headers=headers)


def _hash_bytes(content: bytes) -> str:
    import hashlib

    return hashlib.sha256(content).hexdigest()


def _etag_matches(header: str | None, quoted: str) -> bool:
    if not header:
        return False
    candidates = [item.strip() for item in str(header).split(",") if item.strip()]
    return any(item == "*" or item == quoted or item == quoted.strip('"') for item in candidates)


# ---------------------------------------------------------------------------
# 交互场景 HTML（独立 CSP 沙箱，供前端 iframe 加载）
# ---------------------------------------------------------------------------

@router.get("/scenes/{scene_id}/html")
def scene_html(scene_id: int, user=Depends(current_user)):
    scene = cdb.owned_scene(scene_id, user["id"])
    if scene["scene_type"] == "demo":
        # 演示场景是固定版本附件快照：只经播放器渲染，不直出 HTML（不执行任何内联脚本）。
        raise HTTPException(404, "该场景为原理演示，不提供 HTML 直出，请在课堂播放器中播放")
    if scene["scene_type"] != "interactive":
        raise HTTPException(404, "该场景不是交互场景")
    content = _scene_content(scene)
    html = str(content.get("html") or "")
    if not html.strip():
        raise HTTPException(404, "该交互场景还没有可播放内容")
    return HTMLResponse(
        content=html,
        headers={
            "Content-Security-Policy": SCENE_HTML_CSP,
            "X-Frame-Options": "SAMEORIGIN",
            "Cache-Control": "private, max-age=300",
            "X-Content-Type-Options": "nosniff",
        },
    )


__all__ = ["router"]

