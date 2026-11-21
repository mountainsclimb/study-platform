"""``course`` 任务执行器：两阶段生成（大纲可人工确认 → 逐场景内容）。

流水线（对齐 recon §4.1，复用知序既有 job 机制）：

1. ``stage=outline|all``：生成结构化大纲 → 写 ``cls_courses.outline_json``（status=outline_ready）
   → 任务进入 ``awaiting_outline`` 并保存 checkpoint → 用户走既有
   ``POST /api/ai/jobs/{id}/confirm-outline`` 确认/编辑大纲后续跑；
2. 确认后（或 ``stage=scenes`` 直接开始）：逐场景生成内容，每场景一步一提交、可取消、失败隔离；
   已 ``ready`` 的场景在续跑时跳过。

所有 AI 调用走 ``app.ai.adapters.call_structured``（JSON 解析失败/结构非法自动重试一次），
不自行拼接厂商请求；SSE 通过 ``app.ai.jobs.publish_event`` 推送中文阶段名与 done/total。
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import re

from fastapi import HTTPException

from . import db as cdb
from . import images, interactive, multiagent, pbl, prompts, schema

STAGES = ("outline", "scenes", "all")
REGENERATE_HINT = "该场景内容需要后续版本生成"
#: 深度交互模式：每课 interactive 场景数量阈值（对齐上游 interactive-outlines：1-2 个）。
MIN_INTERACTIVE_SCENES = 1
MAX_INTERACTIVE_SCENES = 2
#: 深度交互模式下 slide 的「动手试一试」提示块 id 前缀。
INTERACTIVE_HINT_PREFIX = "hint_"


# ---------------------------------------------------------------------------
# 延迟引用 app.ai.jobs / app.ai（测试可 monkeypatch app.ai._call_model）
# ---------------------------------------------------------------------------

def _ai_jobs():
    from ..ai import jobs

    return jobs


def _call_model_fn():
    from .. import ai

    return ai._call_model


def _call_structured_fn():
    from ..ai.adapters import call_structured

    return call_structured


def _publish(job_id: int, event: str, payload: dict) -> None:
    try:
        _ai_jobs().publish_event(job_id, event, payload)
    except Exception:  # SSE/Redis 推送失败不影响已落库产物
        pass


def _job_status_update(job_id: int, status: str, stage: str, result: dict | None = None) -> None:
    jobs = _ai_jobs()
    updater = getattr(jobs, "_job_update", None)
    if callable(updater):
        updater(job_id, status, stage, result=result)
    else:  # pragma: no cover - jobs 模块必带 _job_update
        updater = getattr(jobs, "update_job", None)
        if callable(updater):
            updater(job_id, status, stage)


def _checkpoint_get(job_id: int) -> dict:
    value = _ai_jobs()._job_get_checkpoint(job_id)
    return value if isinstance(value, dict) else {}


def _checkpoint_set(job_id: int, checkpoint: dict) -> None:
    try:
        _ai_jobs()._job_set_checkpoint(job_id, checkpoint)
    except Exception:
        pass


def _checkpoint_force(job_id: int, checkpoint: dict) -> None:
    """取消后强制落库检查点：``_job_set_checkpoint`` 带 cancelled/dispatch_token 过滤会跳过写入。"""
    from .. import db

    with db.connection() as con:
        db.write_lock(con, "ai-job-checkpoint")
        con.execute(
            "UPDATE ai_jobs SET checkpoint_json=?, updated_at=? WHERE id=?",
            (cdb.json_dump(checkpoint), db.utcnow(), job_id),
        )


def _cancelled(job_id: int) -> bool:
    try:
        return bool(_ai_jobs().is_job_cancelled(job_id))
    except Exception:  # 任务行不存在等异常按「未取消」处理，由框架最终落定
        return False


def _call_meta(call: dict) -> dict:
    """从 ``_call_model`` 返回里提取可写入结果的调用摘要（不带密钥）。"""
    usage = call.get("usage") if isinstance(call.get("usage"), dict) else {}
    return {
        "model": call.get("model") or "",
        "provider_name": call.get("provider_name") or "",
        "usage": usage,
    }


# ---------------------------------------------------------------------------
# 课程 / 大纲读写
# ---------------------------------------------------------------------------

def _params(data) -> dict:
    params = getattr(data, "params", None)
    return dict(params) if isinstance(params, dict) else {}


def _int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _course_or_error(course_id: int, user_id: int) -> dict:
    course = cdb.find_course(course_id, user_id)
    if course is None:
        raise ValueError("课程不存在或无权访问")
    return course


def _course_outline(course: dict) -> dict:
    outline = cdb.json_load((course or {}).get("outline_json"), {})
    if not isinstance(outline, dict) or not outline.get("scenes"):
        return {}
    return outline


def _course_config(course: dict) -> dict:
    config = cdb.json_load((course or {}).get("config_json"), {})
    return config if isinstance(config, dict) else {}


def _update_course_config(course: dict, changes: dict) -> dict:
    """只更新课程 config 的部分键（读最新行再合并，避免覆盖并发写入的其它配置）。"""
    from .. import db

    with db.connection() as con:
        db.write_lock(con, "classroom-course")
        row = con.execute(
            "SELECT config_json FROM cls_courses WHERE id=? AND user_id=?",
            (course["id"], course["user_id"]),
        ).fetchone()
        if row is None:
            return {}
        config = cdb.json_load(row["config_json"], {})
        config = config if isinstance(config, dict) else {}
        config.update(changes or {})
        con.execute(
            "UPDATE cls_courses SET config_json=?, updated_at=? WHERE id=? AND user_id=?",
            (cdb.json_dump(config), db.utcnow(), course["id"], course["user_id"]),
        )
    return config


# ---------------------------------------------------------------------------
# 联网搜索（P3）：生成大纲前检索，把来源注入 prompt 并随课程落库
# ---------------------------------------------------------------------------

#: 联网资料条数上下限（与 ``app.ai.search`` 的 MAX_LIMIT=8 对齐）。
MIN_WEB_SEARCH_LIMIT = 1
MAX_WEB_SEARCH_LIMIT = 8
DEFAULT_WEB_SEARCH_LIMIT = 5
#: 场景 prompt 的来源摘要上限（每场景不超过 3000 字）。
SCENE_SOURCES_MAX_CHARS = prompts.SCENE_SOURCES_PROMPT_MAX_CHARS
#: ``course.outline.sources`` 中单条摘要的最大长度（避免课程行过大）。
SOURCE_SUMMARY_MAX_CHARS = 600


def _ai_search():
    """延迟引用 ``app.ai.search``：便于测试 monkeypatch，也避免包初始化顺序耦合。"""
    from ..ai import search

    return search


def _web_search_settings(params: dict) -> dict:
    """从任务参数解析联网搜索开关与条数（非法值按默认/边界处理，准入校验另有 422）。"""
    enabled = params.get("web_search")
    enabled = bool(enabled) if isinstance(enabled, bool) else False
    limit = _int(params.get("web_search_limit"), DEFAULT_WEB_SEARCH_LIMIT)
    return {
        "enabled": enabled,
        "limit": max(MIN_WEB_SEARCH_LIMIT, min(MAX_WEB_SEARCH_LIMIT, limit)),
    }


def _requirements_keywords(text: str, limit: int = 120) -> str:
    """把用户补充要求拆成关键词片段（检索 query 用，避免整段长文）。"""
    raw = str(text or "").strip()
    if not raw:
        return ""
    pieces = [piece.strip() for piece in re.split(r"[，。；、,.;！？!?\s\u3000：:（）()\[\]【】]+", raw) if piece.strip()]
    keywords: list[str] = []
    used = 0
    for piece in pieces:
        piece = piece[:24]
        if used + len(piece) > limit:
            break
        keywords.append(piece)
        used += len(piece) + 1
    return " ".join(keywords)


def build_web_search_query(course: dict, requirements: str = "", *, max_chars: int | None = None) -> str:
    """检索关键词 = 课程主题 + 学科/年级 + 用户补充要求关键词，拼接后 ≤300 字。"""
    limit = _int(max_chars, 0) or _ai_search().MAX_QUERY_CHARS
    parts: list[str] = []
    topic = str((course or {}).get("topic") or (course or {}).get("title") or "").strip()
    if topic:
        parts.append(topic)
    stage = "/".join(
        item for item in (
            str((course or {}).get("subject") or "").strip(),
            str((course or {}).get("grade") or "").strip(),
        ) if item
    )
    if stage:
        parts.append(stage)
    keywords = _requirements_keywords(requirements)
    if keywords:
        parts.append(keywords)
    return " ".join(parts).strip()[:limit]


def _sources_from_search(result: dict) -> list[dict]:
    """搜索响应 → ``outline.sources`` 的稳定结构（只保留可展示字段，不伪造来源）。"""
    sources: list[dict] = []
    for item in (result or {}).get("results") or []:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        title = str(item.get("title") or "").strip()
        if not url and not title:
            continue
        sources.append({
            "index": len(sources) + 1,
            "title": (title or url)[:200],
            "url": url[:600],
            "site": str(item.get("site") or "").strip()[:80],
            "date": str(item.get("date") or "").strip()[:32],
            "summary": str(item.get("summary") or "").strip()[:SOURCE_SUMMARY_MAX_CHARS],
        })
    return sources


def outline_sources(outline: dict) -> list[dict]:
    """读取/清洗 ``outline.sources``（缺失或损坏时返回空列表，不抛错）。"""
    sources = (outline or {}).get("sources") if isinstance(outline, dict) else None
    cleaned: list[dict] = []
    for index, item in enumerate(sources if isinstance(sources, list) else [], 1):
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        title = str(item.get("title") or "").strip()
        if not url and not title:
            continue
        cleaned.append({
            "index": _int(item.get("index"), index) or index,
            "title": (title or url)[:200],
            "url": url[:600],
            "site": str(item.get("site") or "").strip()[:80],
            "date": str(item.get("date") or "").strip()[:32],
            "summary": str(item.get("summary") or "").strip()[:SOURCE_SUMMARY_MAX_CHARS],
        })
    return cleaned


def format_sources_for_prompt(sources: list[dict], *, max_chars: int = SCENE_SOURCES_MAX_CHARS) -> str:
    """把落库来源格式化为场景 prompt 片段（每场景最多 3000 字，带编号与链接）。"""
    lines: list[str] = []
    used = 0
    for item in sources or []:
        block = "[%d] %s（%s%s）\n链接：%s\n摘要：%s" % (
            _int(item.get("index")), item.get("title") or "未命名",
            item.get("site") or "",
            (" · " + item["date"]) if item.get("date") else "",
            item.get("url") or "",
            str(item.get("summary") or "").strip(),
        )
        if used + len(block) > max_chars:
            break
        lines.append(block)
        used += len(block) + 2
    return "\n\n".join(lines)


def _web_search_config(payload: dict) -> dict:
    """写入 ``course.config.web_search`` 的公开配置（含未使用原因，便于前端说明）。"""
    payload = payload if isinstance(payload, dict) else {}
    return {
        "enabled": bool(payload.get("enabled")),
        "limit": _int(payload.get("limit"), DEFAULT_WEB_SEARCH_LIMIT),
        "query": str(payload.get("query") or "")[:300],
        "used": bool(payload.get("used")),
        "count": _int(payload.get("count")),
        "provider": str(payload.get("provider") or "")[:80],
        "skipped": str(payload.get("skipped") or "")[:300],
    }


async def _run_web_search(*, job_id: int, course: dict, requirements: str, settings: dict) -> dict:
    """生成大纲前联网检索。

    无搜索连接 / 检索失败 / 无结果都**降级**：记录 ``skipped`` 原因并返回空来源，不抛错，
    课程继续正常生成（前端与导出据此明确标注「本次未使用联网资料」）。
    """
    search = _ai_search()
    payload = {
        "enabled": bool((settings or {}).get("enabled")),
        "limit": _int((settings or {}).get("limit"), DEFAULT_WEB_SEARCH_LIMIT),
        "query": "",
        "used": False,
        "count": 0,
        "skipped": "",
        "provider": "",
        "prompt_text": "",
        "sources": [],
    }
    if not payload["enabled"]:
        payload["skipped"] = "未开启联网查找资料"
        return payload
    payload["query"] = build_web_search_query(course, requirements)
    if not payload["query"]:
        payload["skipped"] = "课程主题为空，无法检索"
        return payload
    _publish(job_id, "stage", {"status": "running", "stage": "检索资料", "done": 0, "total": 1})
    try:
        candidates = search.search_candidates(course["user_id"])
    except Exception as exc:  # noqa: BLE001 - 连接读取失败按无连接降级
        payload["skipped"] = "搜索连接读取失败（%s）" % type(exc).__name__
        return payload
    if not candidates:
        payload["skipped"] = "没有可用的联网搜索连接（需在「模型设置」添加 DeepSeek 连接）"
        _publish(job_id, "stage", {
            "status": "running", "stage": "未使用联网资料：没有可用的搜索连接",
            "done": 1, "total": 1, "skipped": True,
        })
        return payload
    try:
        result = await search.web_search(candidates[0], payload["query"], payload["limit"])
    except search.SearchUnavailable as exc:
        payload["skipped"] = "联网检索失败：%s" % exc
        _publish(job_id, "stage", {
            "status": "running", "stage": "联网检索失败，已按未使用资料继续生成",
            "done": 1, "total": 1, "skipped": True,
        })
        return payload
    except Exception as exc:  # noqa: BLE001 - 非预期异常同样降级
        payload["skipped"] = "联网检索异常（%s）" % type(exc).__name__
        return payload
    sources = _sources_from_search(result if isinstance(result, dict) else {})
    if not sources:
        payload["skipped"] = "联网检索没有返回可用结果"
        _publish(job_id, "stage", {
            "status": "running", "stage": "联网检索没有返回可用结果，已按未使用资料继续生成",
            "done": 1, "total": 1, "skipped": True,
        })
        return payload
    payload.update({
        "used": True,
        "count": len(sources),
        "sources": sources,
        "provider": str((result or {}).get("provider") or "")[:80] if isinstance(result, dict) else "",
        "prompt_text": search.format_results_for_prompt(result if isinstance(result, dict) else {"results": []}),
    })
    _publish(job_id, "stage", {
        "status": "running", "stage": "已获取 %d 条参考资料" % len(sources),
        "done": 1, "total": 1, "source_count": len(sources),
    })
    return payload


def _merge_outline_sources(outline: dict, *fallbacks) -> dict:
    """把来源合并回大纲（用户确认大纲时 schema 归一化会丢弃额外字段）。"""
    if not isinstance(outline, dict):
        return outline
    sources = outline_sources(outline)
    if not sources:
        for fallback in fallbacks:
            sources = outline_sources(fallback if isinstance(fallback, dict) else {})
            if sources:
                break
    if sources:
        outline["sources"] = sources
    return outline


def _resolve_web_search_config(checkpoint: dict, course: dict, outline: dict) -> dict:
    """当前课程实际生效的联网配置：checkpoint 优先，其次课程落库配置。"""
    config = (checkpoint or {}).get("web_search")
    if isinstance(config, dict) and config:
        return dict(config)
    stored = _course_config(course).get("web_search")
    if isinstance(stored, dict) and stored:
        return dict(stored)
    return _web_search_config({"enabled": False, "skipped": "本次生成未开启联网查找资料"})


def _web_search_result_fields(web_config: dict, outline: dict) -> dict:
    """任务结果里的联网字段：来源条数以实际落库来源为准（不沿用可能过期的计数）。"""
    config = dict(web_config) if isinstance(web_config, dict) else {}
    count = len(outline_sources(outline))
    config["count"] = count
    config["used"] = bool(count)
    return {
        "web_search": config,
        "source_count": count,
        "web_search_skipped": str(config.get("skipped") or ""),
    }


def _web_search_summary_suffix(web_config: dict, outline: dict) -> str:
    """任务 summary 追加来源说明：有来源写条数，启用但没拿到写原因（明确标注未使用）。"""
    count = len(outline_sources(outline))
    if count:
        return "；已使用 %d 条联网资料（来源已随课程保存）" % count
    config = web_config if isinstance(web_config, dict) else {}
    if config.get("enabled"):
        reason = str(config.get("skipped") or "").strip() or "未检索到可用资料"
        return "；未使用联网资料（%s）" % reason[:120]
    return ""


def _append_scene_sources(prompt: str, sources_text: str) -> str:
    """interactive/pbl 的 prompt 构建函数不在本任务写作用域内：在调用处追加来源块。"""
    text = str(sources_text or "").strip()
    if not text:
        return prompt
    return prompt + "\n\n## 参考来源（可选引用，禁止编造来源或链接）\n" + text + "\n"


# ---------------------------------------------------------------------------
# 深度交互模式（interactive-first）策略
# ---------------------------------------------------------------------------

def _interactive_mode(course: dict) -> bool:
    return str((course or {}).get("mode") or "") == "interactive"


def _apply_interactive_outline_policy(outline: dict, course: dict) -> dict:
    """深度交互模式的大纲约束：interactive 场景 1-2 个、必须有 widget；超出改 slide 并告警。"""
    if not _interactive_mode(course) or not isinstance(outline, dict):
        return outline
    scenes = [scene for scene in (outline.get("scenes") or []) if isinstance(scene, dict)]
    if not scenes:
        return outline
    warnings = list(outline.get("warnings") or [])

    def _warn(message: str) -> None:
        if message not in warnings:
            warnings.append(message)

    interactive_indexes = [
        index for index, scene in enumerate(scenes) if scene.get("type") == "interactive"
    ]
    for index in interactive_indexes[MAX_INTERACTIVE_SCENES:]:
        scenes[index]["type"] = "slide"
        scenes[index].pop("widget", None)
        _warn("深度交互模式每课最多 %d 个 interactive 场景，第 %d 个已改为 slide"
              % (MAX_INTERACTIVE_SCENES, index + 1))
    kept = [index for index, scene in enumerate(scenes) if scene.get("type") == "interactive"]
    for index in kept:
        if not str(scenes[index].get("widget") or "").strip():
            scenes[index]["widget"] = "simulation"
            _warn("交互场景「%s」缺少 widget，已按 simulation 处理" % scenes[index].get("title"))
    if not kept:
        _warn("深度交互模式建议至少 %d 个 interactive 场景（本次大纲没有，已按现有场景生成）"
              % MIN_INTERACTIVE_SCENES)
    outline["scenes"] = scenes
    outline["warnings"] = warnings
    outline["interactive_mode"] = True
    return outline


def _interactive_hint_text(spec: dict) -> str:
    points = [
        str(point).strip() for point in ((spec.get("outline") or {}).get("outline_points") or [])
        if str(point).strip()
    ]
    if points:
        return "结合本页内容想一想：%s——你能自己举例或验证一次吗？" % points[0][:60]
    return "先回忆本页的关键结论，再想一想它能在什么情境里用到。"


def _build_hint_element(element_id: str, text: str) -> dict | None:
    raw = {
        "id": element_id, "type": "text", "left": 60, "top": 466, "width": 880, "height": 46,
        # 用字符串拼接而不是 % 格式化：大纲要点里可能含 % 字符。
        "content": ('<p style="font-size: 16px;"><strong>动手试一试</strong>：'
                    + str(text or "") + "</p>"),
        "defaultFontName": "", "defaultColor": "#1f6feb",
    }
    try:
        content, _warnings = schema.normalize_slide_content({"elements": [raw]})
    except schema.ClassroomSchemaError:
        return None
    elements = (content.get("canvas") or {}).get("elements") or []
    return elements[0] if elements else None


def _ensure_slide_interactive_hint(content: dict, spec: dict) -> tuple[dict, list[str]]:
    """深度交互模式：确保 slide 有「动手试一试」提示文本块，并写 ``content.interactive_hint``。

    模型已按 prompt 输出 hint_ 元素时直接复用；缺失时按大纲要点确定性补入（不伪造交互结果），
    并在 meta.warnings 标注，前端可据此展示提示块。
    """
    warnings: list[str] = []
    if not isinstance(content, dict):
        return content, warnings
    canvas = content.get("canvas")
    if not isinstance(canvas, dict):
        return content, warnings
    elements = canvas.get("elements")
    if not isinstance(elements, list):
        return content, warnings
    text = _interactive_hint_text(spec)
    for element in elements:
        if not isinstance(element, dict):
            continue
        element_text = str(element.get("text") or "").strip() or schema.html_to_plain(element.get("content") or "")
        if str(element.get("id") or "").startswith(INTERACTIVE_HINT_PREFIX) or "动手试一试" in element_text:
            content["interactive_hint"] = {"element_id": element.get("id"), "text": element_text or text}
            return content, warnings
    hint_text = "动手试一试：" + text
    if len(elements) >= schema.MAX_ELEMENTS:
        content["interactive_hint"] = {"element_id": None, "text": hint_text}
        warnings.append("幻灯片元素已达上限，未追加「动手试一试」提示块")
        return content, warnings
    used = {str(element.get("id")) for element in elements if isinstance(element, dict)}
    hint_id = "%s001" % INTERACTIVE_HINT_PREFIX
    suffix = 2
    while hint_id in used:
        hint_id = "%s%03d" % (INTERACTIVE_HINT_PREFIX, suffix)
        suffix += 1
    element = _build_hint_element(hint_id, text)
    if element is None:
        return content, warnings
    elements.append(element)
    content["interactive_hint"] = {"element_id": element.get("id"), "text": hint_text}
    warnings.append("已补入「动手试一试」提示块（按大纲要点生成，未编造交互结果）")
    return content, warnings


# ---------------------------------------------------------------------------
# 可选配图（image slot）
# ---------------------------------------------------------------------------

#: 配图在幻灯片右侧的摆放位置（画布像素）：最多两张，竖排避免互相重叠。
_IMAGE_LAYOUTS = ((640, 108, 300, 192), (640, 314, 300, 192))


def _image_settings(course: dict, params: dict) -> dict | None:
    """解析本次生成任务的配图配置；关闭 / 无可用连接时返回 None（不生成、不造假图）。"""
    if params.get("images") is False:
        return None
    per_scene = _int(params.get("images_per_scene"), 1)
    if per_scene <= 0:
        return None
    per_scene = max(1, min(images.MAX_IMAGES_PER_SCENE, per_scene))
    candidate = images.resolve_candidate(
        course["user_id"],
        _int(params.get("image_provider_id")) or None,
        str(params.get("image_model") or "").strip() or None,
    )
    if candidate is None:
        return None
    return {"candidate": candidate, "per_scene": per_scene}


def _append_image_element(content: dict, asset: dict, index: int) -> dict | None:
    """把已落库配图作为 image 元素追加到 slide（复用 schema 校验，坐标固定右侧栏）。"""
    if not isinstance(content, dict):
        return None
    canvas = content.get("canvas")
    if not isinstance(canvas, dict):
        return None
    elements = canvas.get("elements")
    if not isinstance(elements, list) or len(elements) >= schema.MAX_ELEMENTS:
        return None
    left, top, width, height = _IMAGE_LAYOUTS[min(max(0, index), len(_IMAGE_LAYOUTS) - 1)]
    raw = {
        "id": "img_%03d" % (index + 1), "type": "image", "left": left, "top": top,
        "width": width, "height": height, "src": str(asset.get("url") or ""),
        "alt": str(asset.get("alt") or "教学配图")[:120], "fixedRatio": True,
    }
    try:
        normalized, _warnings = schema.normalize_slide_content({"elements": [raw]})
    except schema.ClassroomSchemaError:
        return None
    normalized_elements = (normalized.get("canvas") or {}).get("elements") or []
    if not normalized_elements:
        return None
    element = dict(normalized_elements[0])
    used = {str(item.get("id")) for item in elements if isinstance(item, dict)}
    candidate_id = str(element.get("id") or raw["id"])
    if candidate_id in used:
        suffix = 2
        while "img_%03d_%d" % (index + 1, suffix) in used:
            suffix += 1
        candidate_id = "img_%03d_%d" % (index + 1, suffix)
    element["id"] = candidate_id
    element["asset_id"] = _int(asset.get("id"))
    element["asset_sha256"] = str(asset.get("sha256") or "")
    elements.append(element)
    return element


async def _generate_scene_images(*, course: dict, spec: dict, content: dict, outline: dict,
                                 settings: dict | None) -> tuple[list[dict], list[str]]:
    """为 slide 场景生成 0-2 张配图：失败只记 warning 并跳过，绝不阻塞课程生成。"""
    if not settings or spec.get("scene_type") != "slide" or not isinstance(content, dict):
        return [], []
    assets: list[dict] = []
    warnings: list[str] = []
    for index in range(int(settings.get("per_scene") or 0)):
        prompt = prompts.build_image_prompt(
            scene_title=str(spec.get("title") or ""),
            outline_points=(spec.get("outline") or {}).get("outline_points") or [],
            course_title=str(outline.get("title") or course.get("title") or ""),
            subject=str(course.get("subject") or ""),
            grade=str(course.get("grade") or ""),
            position=_int(spec.get("order_index"), 1),
            total=len(outline.get("scenes") or []) or 1,
            index=index + 1,
            count=int(settings.get("per_scene") or 1),
        )
        try:
            asset = await images.generate_scene_image(
                user_id=course["user_id"], course_id=_int(course.get("id")),
                candidate=settings["candidate"], prompt=prompt,
                scene_id=_int(spec.get("scene_id")) or None,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # 配图属于可选增强：任何失败都跳过，不拖垮整门课
            warnings.append("配图生成失败，本页保持纯文本（%s）" % type(exc).__name__)
            continue
        element = _append_image_element(content, asset, index)
        if element is None:
            warnings.append("配图已生成但未加入幻灯片（元素数量已达上限）")
            continue
        assets.append({
            "asset_id": _int(asset.get("id")),
            "element_id": element.get("id"),
            "url": str(asset.get("url") or ""),
            "mime_type": str(asset.get("mime_type") or ""),
            "alt": str(asset.get("alt") or element.get("alt") or ""),
            "prompt": str(asset.get("prompt") or "")[:600],
            "sha256": str(asset.get("sha256") or ""),
        })
    if assets:
        content["image_assets"] = assets
    return assets, warnings


# ---------------------------------------------------------------------------
# 角色名册自动生成（course 任务内，失败不阻塞）
# ---------------------------------------------------------------------------

async def _ensure_course_roster(*, job_id: int, course: dict, provider, model: str,
                                calls: list[dict]) -> dict:
    """课程生成时自动补一次角色名册：已有名册（用户手动生成过）时跳过，失败只记 warning。"""
    # 读最新课程行：任务排队期间用户可能已手动生成过名册，不能拿旧快照覆盖。
    fresh = cdb.find_course(course["id"], course["user_id"]) or course
    config = _course_config(fresh)
    roster = multiagent.roster_from_config(config)
    if multiagent.has_roster(roster):
        return {"generated": False, "warnings": [],
                "agent_count": len(multiagent.roster_agents(roster))}
    if _cancelled(job_id):
        raise asyncio.CancelledError()
    _publish(job_id, "stage", {"status": "running", "stage": "生成课堂角色名册", "done": 0, "total": 1})
    outline = _course_outline(fresh)
    prompt = multiagent.build_roster_prompt(
        title=str(outline.get("title") or course.get("title") or ""),
        topic=str(course.get("topic") or ""),
        subject=str(course.get("subject") or ""),
        grade=str(course.get("grade") or ""),
        mode=str(course.get("mode") or "standard"),
        requirements=str(course.get("requirements") or ""),
    )

    def validator(raw):
        payload, roster_warnings = multiagent.normalize_agent_roster(raw)
        return {"roster": payload, "warnings": roster_warnings}

    try:
        result, call = await _call_structured_fn()(
            provider, model, prompt, validator, token_limit=1500,
            user_id=course["user_id"], job_id=job_id,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # 名册失败不阻塞课程生成，前端仍可手动生成
        return {
            "generated": False,
            "warnings": ["角色名册自动生成失败，已跳过（可在课程页手动生成）：%s" % type(exc).__name__],
            "agent_count": 0,
        }
    roster = result["roster"]
    updated = multiagent.with_roster_config(
        config, roster,
        model=str((call or {}).get("model") or model),
        provider_name=str((call or {}).get("provider_name") or ""),
    )
    _update_course_config(course, {
        multiagent.CONFIG_KEY: updated[multiagent.CONFIG_KEY],
        multiagent.CONFIG_META_KEY: updated[multiagent.CONFIG_META_KEY],
    })
    if isinstance(call, dict):
        calls.append(_call_meta(call))
    agent_count = len(multiagent.roster_agents(roster))
    _publish(job_id, "stage", {
        "status": "running", "stage": "课堂角色名册已生成（%d 个角色）" % agent_count,
        "done": 0, "total": 1,
    })
    return {
        "generated": True,
        "warnings": list(result.get("warnings") or []),
        "agent_count": agent_count,
    }


def _save_course_outline(course: dict, outline: dict, *, status: str = "outline_ready",
                         config_extra: dict | None = None) -> None:
    """把大纲/状态写回课程（一步一提交）。"""
    from .. import db

    title = str(outline.get("title") or "").strip()
    with db.connection() as con:
        db.write_lock(con, "classroom-course")
        row = con.execute(
            "SELECT title, topic, description, config_json FROM cls_courses WHERE id=? AND user_id=?",
            (course["id"], course["user_id"]),
        ).fetchone()
        if row is None:
            return
        fields = {
            "outline_json": cdb.json_dump(outline),
            "status": status,
            "updated_at": db.utcnow(),
        }
        # 默认标题（= 课题截断）可以被大纲标题替换；用户手动改过的标题保持不变。
        current_title = str(row["title"] or "").strip()
        if title and (not current_title or current_title == str(row["topic"] or "").strip()):
            fields["title"] = title[:60]
        outline_description = str(outline.get("description") or "").strip()
        current_description = str(row["description"] or "").strip() if "description" in row.keys() else ""
        if outline_description and not current_description:
            fields["description"] = outline_description[:600]
        if config_extra:
            config = cdb.json_load(row["config_json"], {})
            config = config if isinstance(config, dict) else {}
            config.update(config_extra)
            fields["config_json"] = cdb.json_dump(config)
        assignments = ",".join("%s=?" % key for key in fields)
        con.execute(
            "UPDATE cls_courses SET %s WHERE id=? AND user_id=?" % assignments,
            (*fields.values(), course["id"], course["user_id"]),
        )


def _set_course_status(course_id: int, user_id: int, status: str) -> None:
    from .. import db

    with db.connection() as con:
        con.execute(
            "UPDATE cls_courses SET status=?, updated_at=? WHERE id=? AND user_id=?",
            (status, db.utcnow(), course_id, user_id),
        )


def _refresh_course_counters(course_id: int, user_id: int, status: str) -> int:
    from .. import db

    with db.connection() as con:
        total = con.execute("SELECT COUNT(*) FROM cls_scenes WHERE course_id=?", (course_id,)).fetchone()[0]
        con.execute(
            "UPDATE cls_courses SET status=?, scene_count=?, updated_at=? WHERE id=? AND user_id=?",
            (status, int(total), db.utcnow(), course_id, user_id),
        )
    return int(total)


def _document_material(user_id: int, document_ids: list[int]) -> str:
    """把用户资料库中的文本拼成大纲参考材料（只读本人资料，超长截断）。"""
    from .. import db

    ids = [int(item) for item in (document_ids or []) if str(item).isdigit()][:6]
    if not ids:
        return ""
    placeholders = ",".join("?" for _ in ids)
    with db.connection() as con:
        rows = con.execute(
            "SELECT id, title, chunks_json FROM ai_documents WHERE user_id=? AND id IN (%s)" % placeholders,
            (user_id, *ids),
        ).fetchall()
    blocks = []
    for row in rows:
        chunks = cdb.json_load(row["chunks_json"], [])
        text = "\n".join(str(chunk.get("text") or "") for chunk in chunks if isinstance(chunk, dict))
        if text.strip():
            blocks.append("【资料《%s》节选】\n%s" % (row["title"] or row["id"], text.strip()[:6000]))
        if sum(len(block) for block in blocks) >= 12000:
            break
    return "\n\n".join(blocks)[:12000]


# ---------------------------------------------------------------------------
# 大纲确认（兼容 confirmed_outline JSON / Markdown / confirmed_sections）
# ---------------------------------------------------------------------------

def _guess_scene_type(title: str, allowed: list[str]) -> str:
    text = str(title or "")
    candidates = (
        ("quiz", ("测", "练习", "检验", "quiz", "test", "检查", "小测")),
        ("interactive", ("实验", "模拟", "交互", "探究", "互动", "操作", "simulation")),
        ("pbl", ("项目", "实践", "任务", "project", "项目式")),
    )
    for scene_type, markers in candidates:
        if any(marker in text.lower() for marker in markers):
            if scene_type in allowed:
                return scene_type
    return "slide" if "slide" in allowed else allowed[0]


def parse_confirmed_outline(value, *, scene_count: int, scene_types: list[str],
                            sections: list[str] | None = None,
                            fallback_title: str = "") -> dict | None:
    """解析用户在 confirm-outline 里提交的大纲。

    兼容三种写法（契约要求「可解析或直接存 JSON」都要支持）：

    1. checkpoint 里直接存 outline JSON 对象；
    2. ``confirmed_outline`` 是 JSON 字符串（前端把编辑后的大纲序列化后提交）；
    3. ``confirmed_outline`` 或 ``confirmed_sections`` 是 Markdown 标题 / 编号列表（人工编辑）。
    """
    allowed = list(scene_types or schema.SCENE_TYPES)
    raw = None
    if isinstance(value, dict):
        raw = value
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        try:
            parsed = json.loads(text)
            if isinstance(parsed, (dict, list)):
                raw = parsed
        except (TypeError, ValueError):
            raw = None
        if raw is None:
            titles = [line.lstrip("#").strip() for line in text.splitlines() if line.strip().startswith("## ")]
            if not titles:
                titles = [
                    re.sub(r"^\s*\d+[.、)]\s*", "", line).strip()
                    for line in text.splitlines() if re.match(r"^\s*\d+[.、)]", line)
                ]
            if not titles:
                titles = [line.strip() for line in text.splitlines() if line.strip()][:schema.MAX_SCENE_COUNT]
            if titles:
                raw = {
                    "title": fallback_title,
                    "scenes": [
                        {"title": title[:120], "type": _guess_scene_type(title, allowed), "outline_points": []}
                        for title in titles[:schema.MAX_SCENE_COUNT]
                        if title
                    ],
                }
    if raw is None and sections:
        titles = [str(item).strip() for item in sections if str(item).strip()]
        if titles:
            raw = {
                "title": fallback_title,
                "scenes": [
                    {"title": title[:120], "type": _guess_scene_type(title, allowed), "outline_points": []}
                    for title in titles[:schema.MAX_SCENE_COUNT]
                ],
            }
    if raw is None:
        return None
    try:
        outline, _warnings = schema.normalize_outline(
            raw, fallback_title=fallback_title or "未命名课程",
            scene_count=scene_count, scene_types=allowed,
        )
    except schema.ClassroomSchemaError:
        return None
    return outline


def _load_outline_from_checkpoint(checkpoint: dict, course: dict, *, scene_count: int,
                                  scene_types: list[str]) -> tuple[dict, bool]:
    """返回 ``(outline, confirmed)``：优先用户确认版本，其次课程已保存版本。"""
    outline = _course_outline(course)
    fallback_title = (outline.get("title") if outline else "") or course.get("title") or course.get("topic") or ""
    confirmed = parse_confirmed_outline(
        checkpoint.get("confirmed_outline"),
        scene_count=scene_count,
        scene_types=scene_types,
        sections=checkpoint.get("confirmed_sections") if isinstance(checkpoint.get("confirmed_sections"), list) else None,
        fallback_title=fallback_title,
    )
    if confirmed:
        confirmed = _apply_interactive_outline_policy(confirmed, course)
        # 用户确认时 schema 归一化会丢弃 sources：从 checkpoint/课程已存大纲里补回（不新检索）。
        return _merge_outline_sources(confirmed, checkpoint.get("outline"), _course_outline(course)), True
    generated = checkpoint.get("outline")
    if isinstance(generated, dict) and generated.get("scenes"):
        try:
            normalized, _ = schema.normalize_outline(
                generated, fallback_title=fallback_title or "未命名课程",
                scene_count=scene_count, scene_types=scene_types,
            )
            normalized = _apply_interactive_outline_policy(normalized, course)
            return _merge_outline_sources(normalized, generated, _course_outline(course)), False
        except schema.ClassroomSchemaError:
            pass
    if isinstance(outline, dict) and outline:
        outline = _apply_interactive_outline_policy(outline, course)
        outline = _merge_outline_sources(outline)
    return outline, False


# ---------------------------------------------------------------------------
# 大纲生成
# ---------------------------------------------------------------------------

async def _generate_outline(*, provider, model: str, data, course: dict, scene_count: int,
                            scene_types: list[str], depth: str, material_text: str,
                            web_search_text: str = "") -> tuple[dict, dict]:
    user_id = course["user_id"]
    job_id = _current_job_id()
    documents_material = _document_material(user_id, (getattr(data, "document_ids", None) or [])[:6]) \
        if not material_text else ""
    prompt = prompts.build_outline_prompt(
        topic=course.get("topic") or "",
        subject=course.get("subject") or (getattr(data, "subject", None) or ""),
        grade=course.get("grade") or "",
        mode=course.get("mode") or "standard",
        requirements=(getattr(data, "requirements", "") or course.get("requirements") or ""),
        scene_types=scene_types,
        scene_count=scene_count,
        depth=depth,
        material_text=material_text or documents_material,
        course_title=course.get("title") or "",
        web_search_text=web_search_text,
    )

    def validator(raw):
        outline, warnings = schema.normalize_outline(
            raw, fallback_title=course.get("title") or course.get("topic") or "未命名课程",
            scene_count=scene_count, scene_types=scene_types,
        )
        # 深度交互模式：interactive 数量约束 / widget 兜底 / 建议提示。
        outline = _apply_interactive_outline_policy(outline, course)
        return {"outline": outline, "warnings": warnings}

    result, call = await _call_structured_fn()(
        provider, model, prompt, validator, token_limit=2600, user_id=user_id, job_id=job_id,
    )
    outline = result["outline"]
    outline["warnings"] = list(outline.get("warnings") or []) + list(result.get("warnings") or [])
    return outline, _call_meta(call)


def _current_job_id() -> int | None:
    """执行器里拿不到 job_id 时用于 usage 记账；由 ``run_course_job`` 设置（协程隔离）。"""
    return _JOB_ID.get()


_JOB_ID: contextvars.ContextVar[int | None] = contextvars.ContextVar("classroom_job_id", default=None)


# ---------------------------------------------------------------------------
# 场景生成
# ---------------------------------------------------------------------------

def _outline_scene_map(outline: dict) -> dict[int, dict]:
    mapping = {}
    for index, scene in enumerate(outline.get("scenes") or [], 1):
        if isinstance(scene, dict):
            mapping[index] = scene
    return mapping


def _scene_specs(outline: dict, existing: dict[int, dict], scene_ids: list[int]) -> list[dict]:
    """把大纲 + 已落库场景合成本轮要生成的场景清单。"""
    scene_map = _outline_scene_map(outline)
    specs: list[dict] = []
    if scene_ids:
        for scene_id in scene_ids:
            row = next((item for item in existing.values() if int(item["id"]) == int(scene_id)), None)
            if row is None:
                continue
            outline_scene = scene_map.get(int(row["order_index"])) or {}
            specs.append({
                "scene_id": int(row["id"]),
                "order_index": int(row["order_index"]),
                "scene_type": row["scene_type"],
                "title": row["title"] or outline_scene.get("title") or "未命名场景",
                "outline": outline_scene,
                "force": True,
            })
        return specs
    for order_index in sorted(scene_map):
        outline_scene = scene_map[order_index]
        row = existing.get(order_index)
        specs.append({
            "scene_id": int(row["id"]) if row else None,
            "order_index": order_index,
            "scene_type": (row or {}).get("scene_type") or outline_scene.get("type") or "slide",
            "title": outline_scene.get("title") or (row or {}).get("title") or "未命名场景",
            "outline": outline_scene,
            "force": False,
        })
    return specs


def _scene_element_digest(content: dict) -> list[dict]:
    canvas = content.get("canvas") if isinstance(content, dict) else None
    if not isinstance(canvas, dict):
        return []
    digest = []
    for element in canvas.get("elements") or []:
        if not isinstance(element, dict):
            continue
        digest.append({
            "id": element.get("id"),
            "type": element.get("type"),
            "text": element.get("text") or schema.html_to_plain(element.get("content"))[:60],
            "latex": element.get("latex") or "",
        })
    return digest


def _scene_digest_text(scene: dict, content: dict) -> str:
    """给 tutor / 摘要用的当前场景内容摘要。"""
    lines = ["场景《%s》（%s）" % (scene.get("title"), schema.SCENE_TYPE_LABELS.get(scene.get("scene_type"), scene.get("scene_type")))]
    outline_scene = scene.get("outline") or {}
    points = outline_scene.get("outline_points") or []
    if points:
        lines.append("要点：" + "；".join(str(point) for point in points[:6]))
    if scene.get("scene_type") == "slide":
        for element in _scene_element_digest(content)[:12]:
            text = element.get("text") or element.get("latex")
            if text:
                lines.append("- 元素 %s（%s）：%s" % (element.get("id"), element.get("type"), text))
    elif scene.get("scene_type") == "quiz":
        for index, question in enumerate(content.get("questions") or [], 1):
            lines.append("第 %d 题：%s" % (index, question.get("question") or ""))
    elif scene.get("scene_type") == "interactive":
        lines.append("交互类型：%s；%s" % (content.get("widget") or "", schema.WIDGET_LABELS.get(content.get("widget"), "")))
    elif scene.get("scene_type") == "pbl":
        project = content.get("project") or {}
        lines.append("项目：%s；%s" % (project.get("title") or "", project.get("background") or ""))
        goals = project.get("goals") or []
        if goals:
            lines.append("目标：" + "；".join(str(goal) for goal in goals[:5]))
        for index, milestone in enumerate(project.get("milestones") or [], 1):
            if isinstance(milestone, dict):
                lines.append("里程碑 %d：%s（交付物：%s）" % (
                    index, milestone.get("title") or "", milestone.get("deliverable") or ""))
    return "\n".join(lines)


def _fallback_speech(scene_type: str, title: str, outline_scene: dict) -> str:
    points = [str(point) for point in (outline_scene.get("outline_points") or [])[:4] if str(point).strip()]
    if scene_type == "quiz":
        return "接下来我们做一组课堂小测：%s。请先独立作答，提交后我们一起看解析。" % title
    if scene_type == "interactive":
        return "这一页是一个交互场景：%s。请在页面里动手操作，观察变化后再回答下面的问题。" % title
    if scene_type == "pbl":
        return "接下来进入项目式学习环节：%s。请先阅读项目背景，再按里程碑推进任务。" % title
    speech = "我们来看这一页：%s。" % title
    if points:
        speech += "这一页的重点有：" + "；".join(points) + "。"
    return speech


def _placeholder_content(scene_type: str, title: str, outline_scene: dict) -> tuple[dict, list[str]]:
    notice = REGENERATE_HINT
    if scene_type == "interactive":
        widget = schema.normalize_widget(outline_scene.get("widget"), "simulation")
        html = schema.placeholder_interactive_html(
            title, widget,
            "该交互场景的教学内容尚未生成：P0 版本先提供可由播放器加载的占位页，"
            "后续会按场景大纲生成自包含的模拟/可视化内容，不会伪造互动结果。",
        )
        return {"type": "interactive", "widget": widget, "html": html, "height": 480}, [notice]
    notice_text = (
        "该项目式学习场景的内容尚未生成：P0 版本只提供项目结构占位（标题/背景为占位说明），"
        "后续会按大纲生成角色、里程碑、交付物与评价标准，不会伪造项目内容。"
    )
    project = {"title": title or "项目式学习", "background": notice_text, "roles": [], "milestones": [], "rubric": []}
    return {"type": "pbl", "project": project, "placeholder": True}, [notice]


async def _generate_scene_content(*, provider, model: str, spec: dict, course: dict, outline: dict,
                                  position: int, total: int, depth: str,
                                  sources_text: str = "") -> tuple[dict, list[str], dict | None]:
    scene_type = spec["scene_type"]
    title = spec["title"]
    outline_scene = spec.get("outline") or {}
    language_directive = str(outline.get("language_directive") or "")
    if scene_type == "interactive":
        # P1：真实生成自包含交互 HTML（静态安全校验失败由 call_structured 带纠错重试一次）。
        widget = schema.normalize_widget(outline_scene.get("widget"), "simulation")

        def interactive_validator(raw):
            content, warnings = interactive.normalize_generated_interactive(
                raw, title=title, widget=widget,
            )
            return {"content": content, "warnings": warnings}

        prompt = interactive.build_interactive_prompt(
            scene_title=title,
            widget=widget,
            outline_points=outline_scene.get("outline_points") or [],
            description=outline_scene.get("description") or "",
            course_title=outline.get("title") or course.get("title") or "",
            position=position, total=total, language_directive=language_directive,
            depth=depth, course_requirements=course.get("requirements") or "",
        )
        prompt = _append_scene_sources(prompt, sources_text)
        validator = interactive_validator
        token_limit = 12000
    elif scene_type == "pbl":
        # P1：真实生成结构化 PBL 项目（背景/目标/角色/里程碑/评价/导师提示）。
        pbl_config = outline_scene.get("pbl") if isinstance(outline_scene.get("pbl"), dict) else None
        prompt = pbl.build_pbl_prompt(
            scene_title=title,
            outline_points=outline_scene.get("outline_points") or [],
            description=outline_scene.get("description") or "",
            course_title=outline.get("title") or course.get("title") or "",
            position=position, total=total, language_directive=language_directive,
            depth=depth, requirements=course.get("requirements") or "",
            pbl_config=pbl_config,
        )
        prompt = _append_scene_sources(prompt, sources_text)
        validator = pbl.validator
        token_limit = 6000
    elif scene_type == "slide":
        prompt = prompts.build_slide_prompt(
            scene_title=title,
            outline_points=outline_scene.get("outline_points") or [],
            description=outline_scene.get("description") or "",
            course_title=outline.get("title") or course.get("title") or "",
            course_description=outline.get("description") or course.get("description") or "",
            position=position, total=total, language_directive=language_directive, depth=depth,
            interactive_hint=_interactive_mode(course),
            sources_text=sources_text,
        )
        validator = _slide_validator
        token_limit = 4000
    else:
        quiz_config = outline_scene.get("quiz") if isinstance(outline_scene.get("quiz"), dict) else {}
        prompt = prompts.build_quiz_prompt(
            scene_title=title,
            outline_points=outline_scene.get("outline_points") or [],
            description=outline_scene.get("description") or "",
            course_title=outline.get("title") or course.get("title") or "",
            position=position, total=total,
            question_count=max(1, min(schema.MAX_QUESTIONS, _int(quiz_config.get("question_count"), 3))),
            difficulty=str(quiz_config.get("difficulty") or "medium"),
            language_directive=language_directive,
            sources_text=sources_text,
        )
        validator = _quiz_validator
        token_limit = 3200
    result, call = await _call_structured_fn()(
        provider, model, prompt, validator, token_limit=token_limit,
        user_id=course["user_id"], job_id=_current_job_id(),
    )
    content = result["content"]
    warnings = list(result.get("warnings") or [])
    # 深度交互模式：slide 必须有「动手试一试」提示块（模型漏输出时按要点确定性补入）。
    if scene_type == "slide" and _interactive_mode(course):
        content, hint_warnings = _ensure_slide_interactive_hint(content, spec)
        warnings.extend(hint_warnings)
    return content, warnings, _call_meta(call)


def _slide_validator(raw) -> dict:
    content, warnings = schema.normalize_slide_content(raw)
    return {"content": content, "warnings": warnings}


def _quiz_validator(raw) -> dict:
    content, warnings = schema.normalize_quiz_content(raw)
    return {"content": content, "warnings": warnings}


async def _generate_scene_actions(*, provider, model: str, spec: dict, content: dict, course: dict,
                                  outline: dict, position: int, total: int) -> tuple[list[dict], list[str], dict | None]:
    """生成 speech 旁白与聚焦动作；失败时回退到按要点拼出的确定性旁白（不伪造 AI 结论）。"""
    scene_type = spec["scene_type"]
    title = spec["title"]
    outline_scene = spec.get("outline") or {}
    if scene_type != "slide":
        speech = _fallback_speech(scene_type, title, outline_scene)
        return [{"id": "act_001", "type": "speech", "text": speech}], [], None
    elements = _scene_element_digest(content)
    prompt = prompts.build_actions_prompt(
        scene_title=title,
        scene_type=scene_type,
        outline_points=outline_scene.get("outline_points") or [],
        description=outline_scene.get("description") or "",
        course_title=outline.get("title") or course.get("title") or "",
        position=position, total=total, elements=elements,
        language_directive=str(outline.get("language_directive") or ""),
    )
    element_ids = [element["id"] for element in elements if element.get("id")]

    def validator(raw):
        actions, warnings = schema.normalize_actions(raw, element_ids=element_ids, max_actions=12)
        return {"actions": actions, "warnings": warnings}

    try:
        result, call = await _call_structured_fn()(
            provider, model, prompt, validator, token_limit=1600,
            user_id=course["user_id"], job_id=_current_job_id(),
        )
    except Exception as exc:  # 旁白失败不拖垮整页内容，回退到按要点生成的确定性旁白
        speech = _fallback_speech(scene_type, title, outline_scene)
        return [{"id": "act_001", "type": "speech", "text": speech}], [
            "旁白生成失败，已使用按要点生成的确定性旁白（%s）" % type(exc).__name__
        ], None
    actions = result.get("actions") or []
    warnings = list(result.get("warnings") or [])
    if not schema.speech_text_of(actions):
        actions.append({"id": "act_speech", "type": "speech", "text": _fallback_speech(scene_type, title, outline_scene)})
        warnings.append("模型未生成 speech 旁白，已补入按要点生成的确定性旁白")
    return actions, warnings, _call_meta(call)


def _upsert_scene(*, course: dict, spec: dict, status: str, content: dict | None,
                  meta: dict | None) -> int:
    """按 (course_id, order_index) 幂等写入场景；已存在则更新，保留场景 id。"""
    from .. import db

    now = db.utcnow()
    meta_payload = meta if isinstance(meta, dict) else {}
    content_payload = content if isinstance(content, dict) else {}
    with db.connection() as con:
        db.write_lock(con, "classroom-scene")
        row = con.execute(
            "SELECT id FROM cls_scenes WHERE course_id=? AND order_index=?",
            (course["id"], spec["order_index"]),
        ).fetchone()
        if row is not None:
            con.execute(
                "UPDATE cls_scenes SET scene_type=?,title=?,content_json=?,meta_json=?,status=?,updated_at=? "
                "WHERE id=? AND user_id=?",
                (
                    spec["scene_type"], spec["title"], cdb.json_dump(content_payload), cdb.json_dump(meta_payload),
                    status, now, row["id"], course["user_id"],
                ),
            )
            return int(row["id"])
        return int(con.execute(
            "INSERT INTO cls_scenes(course_id,user_id,scene_type,order_index,title,content_json,meta_json,status,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                course["id"], course["user_id"], spec["scene_type"], spec["order_index"], spec["title"],
                cdb.json_dump(content_payload), cdb.json_dump(meta_payload), status, now, now,
            ),
        ).lastrowid)


def _mark_scene_failed(*, course: dict, spec: dict, error: str) -> None:
    message = str(error or "生成失败")[:300]
    _upsert_scene(
        course=course, spec=spec, status="failed", content=None,
        meta={"error": message, "requires_generation": True},
    )


async def _run_scene_loop(*, job_id: int, course: dict, outline: dict, specs: list[dict],
                          provider, model: str, depth: str, instruction: str,
                          checkpoint: dict, calls: list[dict],
                          image_settings: dict | None = None,
                          sources_text: str = "") -> dict:
    """逐场景生成：每场景一步一提交；失败只标记该场景并继续。"""
    total = len(specs)
    done_ids = {_int(item) for item in (checkpoint.get("done_scene_ids") or [])}
    failed_ids = {_int(item) for item in (checkpoint.get("failed_scene_ids") or [])}
    ready = 0
    failure_messages: list[dict] = []
    for position, spec in enumerate(specs, 1):
        if _cancelled(job_id):
            checkpoint.update({"done_scene_ids": sorted(done_ids), "failed_scene_ids": sorted(failed_ids),
                               "calls": calls, "prompt_version": prompts.PROMPT_VERSION})
            _checkpoint_force(job_id, checkpoint)
            raise asyncio.CancelledError()
        existing_id = spec.get("scene_id")
        if existing_id and int(existing_id) in done_ids:
            ready += 1
            continue
        stage_text = "生成第 %d/%d 个场景：%s" % (position, total, spec["title"])
        _job_status_update(job_id, "running", stage_text)
        _publish(job_id, "stage", {
            "status": "running", "stage": stage_text,
            "done": position - 1, "total": total,
            "scene_type": spec["scene_type"], "scene_title": spec["title"],
        })
        scene_id = _upsert_scene(course=course, spec=spec, status="generating", content=None, meta=None)
        spec["scene_id"] = scene_id
        warnings: list[str] = []
        call_meta = None
        try:
            if instruction:
                warnings.append("按用户要求重新生成：%s" % instruction[:120])
            content, content_warnings, call_meta = await _generate_scene_content(
                provider=provider, model=model, spec=spec, course=course, outline=outline,
                position=position, total=total, depth=depth, sources_text=sources_text,
            )
            warnings.extend(content_warnings)
            # 可选配图：失败只产生 warning，继续正常生成动作与落库。
            image_assets, image_warnings = await _generate_scene_images(
                course=course, spec=spec, content=content, outline=outline,
                settings=image_settings,
            )
            warnings.extend(image_warnings)
            actions, action_warnings, action_call = await _generate_scene_actions(
                provider=provider, model=model, spec=spec, content=content, course=course,
                outline=outline, position=position, total=total,
            )
            warnings.extend(action_warnings)
            if action_call:
                calls.append(action_call)
            if call_meta:
                calls.append(call_meta)
            is_placeholder = bool(content.get("placeholder")) if isinstance(content, dict) else True
            meta = {
                "actions": actions,
                "warnings": warnings,
                "placeholder": is_placeholder,
                "requires_generation": is_placeholder,
                "prompt_version": prompts.PROMPT_VERSION,
                "model": model,
                "position": position,
                "total": total,
                "digest": _scene_digest_text(spec, content),
            }
            if spec["scene_type"] == "interactive" and isinstance(content, dict):
                meta["widget"] = content.get("widget")
                meta["html_bytes"] = len(str(content.get("html") or "").encode("utf-8"))
                meta["interactive_checked"] = True
            if spec["scene_type"] == "pbl" and isinstance(content, dict):
                project = content.get("project") if isinstance(content.get("project"), dict) else {}
                meta["pbl"] = {
                    "title": project.get("title") or "",
                    "milestone_count": len(project.get("milestones") or []),
                    "goal_count": len(project.get("goals") or []),
                    "mentor_hint_count": len(project.get("mentor_hints") or []),
                }
            if image_assets:
                meta["images"] = image_assets
                meta["image_count"] = len(image_assets)
            if spec["scene_type"] == "slide" and _interactive_mode(course) and isinstance(content, dict):
                hint = content.get("interactive_hint")
                if isinstance(hint, dict):
                    meta["interactive_hint"] = hint
            _upsert_scene(course=course, spec=spec, status="ready", content=content, meta=meta)
            done_ids.add(scene_id)
            failed_ids.discard(scene_id)
            ready += 1
            _publish(job_id, "stage", {
                "status": "running", "stage": "已完成第 %d/%d 个场景：%s" % (position, total, spec["title"]),
                "done": position, "total": total,
                "scene_id": scene_id, "scene_title": spec["title"], "scene_type": spec["scene_type"],
            })
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # 单场景失败隔离：记录错误、继续下一个
            message = exc.detail if isinstance(exc, HTTPException) else str(exc) or type(exc).__name__
            message = str(message)[:300]
            _mark_scene_failed(course=course, spec=spec, error=message)
            failed_ids.add(scene_id)
            failure_messages.append({"scene_id": scene_id, "title": spec["title"], "error": message})
            _publish(job_id, "stage", {
                "status": "running", "stage": "第 %d/%d 个场景生成失败：%s" % (position, total, spec["title"]),
                "done": position, "total": total, "failed": True,
                "scene_id": scene_id, "scene_title": spec["title"], "error": message,
            })
        checkpoint.update({
            "course_id": course["id"],
            "done_scene_ids": sorted(done_ids),
            "failed_scene_ids": sorted(failed_ids),
            "calls": calls,
            "prompt_version": prompts.PROMPT_VERSION,
        })
        _checkpoint_set(job_id, checkpoint)
    return {"ready": ready, "failed": failure_messages, "done_ids": done_ids, "failed_ids": failed_ids}


# ---------------------------------------------------------------------------
# 任务执行器
# ---------------------------------------------------------------------------

async def run_course_job(job_id, user_id, data, provider, verifier):
    """``course`` 任务执行器：大纲（可确认）→ 逐场景生成。"""
    token = _JOB_ID.set(job_id)
    try:
        return await _run_course_job_inner(job_id, user_id, data, provider, verifier)
    finally:
        _JOB_ID.reset(token)


async def _run_course_job_inner(job_id, user_id, data, provider, verifier):
    params = _params(data)
    course_id = _int(params.get("course_id"))
    if not course_id:
        raise ValueError("缺少课程参数 course_id")
    course = _course_or_error(course_id, user_id)
    stage = str(params.get("stage") or "all")
    if stage not in STAGES:
        raise ValueError("生成阶段只能是 outline / scenes / all")
    scene_types = schema.normalize_scene_types(params.get("scene_types"))
    scene_count = schema.clamp_scene_count(params.get("scene_count"))
    depth = schema.normalize_depth(params.get("depth"))
    instruction = str(params.get("instruction") or "")[:1000]
    scene_ids = [_int(item) for item in (params.get("scene_ids") or []) if str(item).isdigit()]
    web_settings = _web_search_settings(params)

    checkpoint = _checkpoint_get(job_id)
    calls = [call for call in (checkpoint.get("calls") or []) if isinstance(call, dict)]
    outline, confirmed = _load_outline_from_checkpoint(
        checkpoint, course, scene_count=scene_count, scene_types=scene_types,
    )
    if confirmed:
        _save_course_outline(course, outline, config_extra={
            "prompt_version": prompts.PROMPT_VERSION,
            "interactive_mode": _interactive_mode(course),
        })
    elif stage in ("outline", "all") and not outline:
        # P3：生成大纲前联网检索（无搜索连接/检索失败一律降级，记录原因后继续生成）。
        web_payload = await _run_web_search(
            job_id=job_id, course=course,
            requirements=(getattr(data, "requirements", "") or course.get("requirements") or ""),
            settings=web_settings,
        )
        _publish(job_id, "stage", {"status": "running", "stage": "生成课程大纲", "done": 0, "total": 1})
        outline, call_meta = await _generate_outline(
            provider=provider, model=data.model, data=data, course=course,
            scene_count=scene_count, scene_types=scene_types, depth=depth,
            material_text=str(params.get("material_text") or "")[:12000],
            web_search_text=str(web_payload.get("prompt_text") or ""),
        )
        if web_payload.get("sources"):
            outline["sources"] = list(web_payload["sources"])
        web_config = _web_search_config(web_payload)
        calls.append(call_meta)
        _save_course_outline(
            course, outline,
            config_extra={
                "depth": depth, "scene_types": scene_types, "scene_count": scene_count,
                "prompt_version": prompts.PROMPT_VERSION,
                "interactive_mode": _interactive_mode(course),
                "web_search": web_config,
            },
        )
        checkpoint.update({
            "course_id": course_id, "stage": stage, "outline": outline,
            "calls": calls, "prompt_version": prompts.PROMPT_VERSION,
            "web_search": web_config,
        })
        _checkpoint_set(job_id, checkpoint)
    web_config = _resolve_web_search_config(checkpoint, course, outline)
    source_fields = _web_search_result_fields(web_config, outline)
    if not confirmed and stage in ("outline", "all"):
        # 两阶段生成的第一阶段：保存大纲后暂停，等用户编辑并确认（confirm-outline）。
        _job_status_update(job_id, "awaiting_outline", "awaiting_outline", result={
            "course_id": course_id,
            "course_title": outline.get("title") or course.get("title"),
            "stage": stage,
            "outline": outline,
            "notice": "大纲已生成，请核对/增删场景后点击「确认大纲并生成」。",
            "prompt_version": prompts.PROMPT_VERSION,
            "classroom_prompt_version": prompts.PROMPT_VERSION,
            **source_fields,
        })
        return {"__paused__": True, "outline": outline}
    if not outline or not outline.get("scenes"):
        raise ValueError("课程还没有可用大纲：请先生成大纲并确认，再生成场景内容")
    if stage == "outline":
        _save_course_outline(course, outline)
        return {
            "course_id": course_id,
            "course_title": outline.get("title") or course.get("title"),
            "scene_count": len(outline.get("scenes") or []),
            "summary": ("课程大纲已确认并保存"
                        + _web_search_summary_suffix(web_config, outline) + "。"),
            "prompt_version": prompts.PROMPT_VERSION,
            "classroom_prompt_version": prompts.PROMPT_VERSION,
            "calls": calls,
            **source_fields,
        }
    image_settings = _image_settings(course, params)
    return await _generate_scenes(
        job_id=job_id, course=course, outline=outline, scene_ids=scene_ids,
        provider=provider, model=data.model, depth=depth, instruction=instruction,
        checkpoint=checkpoint, calls=calls, image_settings=image_settings,
        web_config=web_config,
    )


async def _generate_scenes(*, job_id: int, course: dict, outline: dict, scene_ids: list[int],
                           provider, model: str, depth: str, instruction: str,
                           checkpoint: dict, calls: list[dict],
                           image_settings: dict | None = None,
                           web_config: dict | None = None) -> dict:
    user_id = course["user_id"]
    # P3：把本课已落库的联网来源摘要注入每个场景 prompt（每场景 ≤3000 字）。
    sources_text = format_sources_for_prompt(outline_sources(outline))
    source_fields = _web_search_result_fields(web_config or {}, outline)
    # 课程级生成（非单场景重生成）时自动补一次角色名册；已有名册直接复用，失败不阻塞。
    roster_info = {"generated": False, "warnings": [], "agent_count": 0}
    if not scene_ids:
        roster_info = await _ensure_course_roster(
            job_id=job_id, course=course, provider=provider, model=model, calls=calls,
        )
        if _cancelled(job_id):
            raise asyncio.CancelledError()
    roster_fields = {
        "roster_generated": bool(roster_info.get("generated")),
        "roster_agent_count": _int(roster_info.get("agent_count")),
        "roster_warnings": list(roster_info.get("warnings") or []),
    }
    existing_rows = cdb.course_scenes(course["id"], user_id)
    existing = {int(row["order_index"]): row for row in existing_rows}
    specs = _scene_specs(outline, existing, scene_ids)
    # 续跑：跳过已经完成、且不是本轮强制重生成的场景。
    done_ids = {_int(item) for item in (checkpoint.get("done_scene_ids") or [])}
    if not scene_ids:
        specs = [spec for spec in specs if not (spec["scene_id"] and int(spec["scene_id"]) in done_ids
                                                and existing.get(spec["order_index"], {}).get("status") == "ready")]
    if not specs:
        total_scenes = _refresh_course_counters(course["id"], user_id, "ready")
        return {
            "course_id": course["id"], "course_title": outline.get("title") or course.get("title"),
            "scene_count": total_scenes, "generated_scene_count": 0, "failed_scene_ids": [],
            "summary": ("所有场景都已生成，无需重新生成"
                        + _web_search_summary_suffix(web_config or {}, outline) + "。"),
            "prompt_version": prompts.PROMPT_VERSION,
            "classroom_prompt_version": prompts.PROMPT_VERSION,
            "calls": calls,
            **roster_fields,
            **source_fields,
        }
    _set_course_status(course["id"], user_id, "generating")
    try:
        result = await _run_scene_loop(
            job_id=job_id, course=course, outline=outline, specs=specs, provider=provider,
            model=model, depth=depth, instruction=instruction, checkpoint=checkpoint, calls=calls,
            image_settings=image_settings, sources_text=sources_text,
        )
    except asyncio.CancelledError:
        # 取消时保留已生成的场景，课程回到「大纲就绪」状态以便续跑/单场景重新生成。
        _refresh_course_counters(course["id"], user_id, "outline_ready")
        raise
    except Exception:
        _refresh_course_counters(course["id"], user_id, "failed")
        raise
    failed_ids = sorted(int(item) for item in result["failed_ids"])
    total_scenes = _refresh_course_counters(
        course["id"], user_id, "ready" if (result["ready"] or not failed_ids) else "failed",
    )
    if result["failed"]:
        _publish(job_id, "stage", {
            "status": "running",
            "stage": "已生成 %d 个场景，%d 个场景失败" % (result["ready"], len(result["failed"])),
            "done": len(specs), "total": len(specs), "failed": True,
        })
    summary = "课程生成完成：%d 个场景可用" % result["ready"]
    if result["failed"]:
        summary += "，%d 个场景失败（可单独重新生成）" % len(result["failed"])
    if roster_fields["roster_warnings"] and not roster_fields["roster_generated"]:
        summary += "；角色名册自动生成失败，可稍后手动生成"
    summary += _web_search_summary_suffix(web_config or {}, outline)
    return {
        "course_id": course["id"],
        "course_title": outline.get("title") or course.get("title"),
        "scene_count": total_scenes,
        "generated_scene_count": result["ready"],
        "failed_scene_ids": failed_ids,
        "failures": [{"scene_id": item["scene_id"], "title": item["title"], "error": item["error"]}
                     for item in result["failed"]],
        "summary": summary,
        "prompt_version": prompts.PROMPT_VERSION,
        "classroom_prompt_version": prompts.PROMPT_VERSION,
        "calls": calls,
        **roster_fields,
        **source_fields,
    }


# ---------------------------------------------------------------------------
# 任务准入校验
# ---------------------------------------------------------------------------

def validate_course_job(data, user) -> None:
    """``POST /api/ai/jobs``（task=course）与 ``POST /api/classroom/courses/{id}/generate`` 共用准入校验。"""
    params = _params(data)
    course_id = _int(params.get("course_id"))
    if not course_id:
        raise HTTPException(422, "缺少课程参数 course_id")
    course = cdb.find_course(course_id, user["id"])
    if course is None:
        raise HTTPException(404, "未找到该课程")
    stage = str(params.get("stage") or "all")
    if stage not in STAGES:
        raise HTTPException(422, "生成阶段只能是 outline / scenes / all")
    scene_count = params.get("scene_count")
    if scene_count is not None:
        value = _int(scene_count, -1)
        if value < schema.MIN_SCENE_COUNT or value > schema.MAX_SCENE_COUNT:
            raise HTTPException(422, "场景数量需在 %d-%d 之间" % (schema.MIN_SCENE_COUNT, schema.MAX_SCENE_COUNT))
    depth = params.get("depth")
    if depth is not None and str(depth) not in schema.DEPTHS:
        raise HTTPException(422, "生成深度只能是 brief / standard / deep")
    images_flag = params.get("images")
    if images_flag is not None and not isinstance(images_flag, bool):
        raise HTTPException(422, "配图开关 images 必须是布尔值")
    images_per_scene = params.get("images_per_scene")
    if images_per_scene is not None:
        value = _int(images_per_scene, -1)
        if value < 0 or value > images.MAX_IMAGES_PER_SCENE:
            raise HTTPException(422, "每场景配图数量需在 0-%d 之间" % images.MAX_IMAGES_PER_SCENE)
    image_provider_id = params.get("image_provider_id")
    image_model = str(params.get("image_model") or "").strip()
    if image_provider_id is not None or image_model:
        if not image_provider_id or not image_model:
            raise HTTPException(422, "指定配图连接时必须同时提供 image_provider_id 与 image_model")
        from ..ai.providers import _selected_provider

        _selected_provider(user["id"], _int(image_provider_id), image_model)
    elif images_flag is True and not images.image_candidates(user["id"]):
        raise HTTPException(422, "请先在「模型设置」中配置支持图片生成的连接，或关闭本页配图")
    web_search_flag = params.get("web_search")
    if web_search_flag is not None and not isinstance(web_search_flag, bool):
        raise HTTPException(422, "联网查找资料开关 web_search 必须是布尔值")
    web_search_limit = params.get("web_search_limit")
    if web_search_limit is not None:
        value = _int(web_search_limit, -1)
        if value < MIN_WEB_SEARCH_LIMIT or value > MAX_WEB_SEARCH_LIMIT:
            raise HTTPException(422, "联网资料条数需在 %d-%d 之间" % (MIN_WEB_SEARCH_LIMIT, MAX_WEB_SEARCH_LIMIT))
    scene_types = params.get("scene_types")
    if scene_types is not None and not isinstance(scene_types, (list, tuple)):
        raise HTTPException(422, "场景类型 scene_types 必须是数组")
    if isinstance(scene_types, (list, tuple)):
        unknown = [str(item) for item in scene_types if schema.normalize_scene_type(item, default="") == ""]
        if unknown:
            raise HTTPException(422, "存在不支持的场景类型：%s" % "、".join(unknown))
    scene_ids = params.get("scene_ids")
    if scene_ids is not None:
        if not isinstance(scene_ids, (list, tuple)) or not scene_ids:
            raise HTTPException(422, "重新生成场景时 scene_ids 不能为空")
        owned = {int(row["id"]) for row in cdb.course_scenes(course_id, user["id"])}
        missing = [_int(item) for item in scene_ids if _int(item) not in owned]
        if missing:
            raise HTTPException(404, "未找到要重新生成的场景")
        if stage != "scenes":
            raise HTTPException(422, "重新生成场景必须使用 stage=scenes")
    if cdb.count_running_course_jobs(course_id, user["id"]) > 0:
        raise HTTPException(409, "该课程已有生成任务在运行，请等待完成或取消后再试")
    if stage in ("scenes",) and not _course_outline(course):
        raise HTTPException(422, "请先生成并确认课程大纲，再生成场景内容")


__all__ = [
    "MAX_INTERACTIVE_SCENES", "MIN_INTERACTIVE_SCENES", "STAGES",
    "MAX_WEB_SEARCH_LIMIT", "MIN_WEB_SEARCH_LIMIT", "DEFAULT_WEB_SEARCH_LIMIT",
    "build_web_search_query", "outline_sources", "parse_confirmed_outline",
    "run_course_job", "validate_course_job",
]
