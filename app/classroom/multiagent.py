"""AI 课堂多智能体编排：角色名册、多角色讨论/插话脚本、增强白板动作（P1）。

只读参考上游 OpenMAIC（不复制代码，按语义重写）：

- 角色名册：``packages/@openmaic/dsl/src/stage.ts:117-129`` 的 ``GeneratedAgentConfig``
  （id/name/role/persona/avatar/color/priority）+ ``lib/server/agent-runtime/roster-tools.ts:165-265``
  的 set_roster 语义（恰好 1 名 teacher、至少 2 个角色、persona 写性格与教学/学习风格）。
- 讨论编排：``lib/chat/pi/prompts.ts:70-166``（导演决定谁发言、每轮一个终止动作）与
  ``:168-224``（子角色：角色守则、peer 上下文、长度硬上限、只输出 JSON）。本模块把工具循环
  精简为「一次结构化调用返回整段多角色脚本」，严格 JSON 校验 + ``call_structured`` 失败重试一次。
- 白板动作：``packages/@openmaic/dsl/src/action.ts:64-187`` 的 ``wb_draw_*`` 语义；知序播放器用
  单条 ``whiteboard`` 动作 + ``ops`` 列表承载。本模块把讨论脚本里的白板坐标统一为 **0-1 归一化
  空间**（动作上带 ``coord_space: "unit"`` 显式区分），并支持
  ``text/formula/line/rect/circle/arrow/path`` 七种 op。

与既有实现的边界（向后兼容）：

- 既有 ``schema.normalize_actions`` 生成的是 1000×562.5 画布**像素**坐标的白板动作；本模块只
  负责讨论/插话脚本里的**归一化**白板动作，不去改动既有 DSL/生成管线行为。
- 既有 ``api.py`` 的 tutor 接口、``cls_turns`` 表结构、课程/会话字段均保持原样；本模块只做增量
  （名册存 ``cls_courses.config_json.agents``，讨论发言写既有 ``cls_turns``）。
"""
from __future__ import annotations

import re

from . import db as cdb
from . import schema

#: 本模块 prompt 版本（写入名册 meta 与接口响应，便于追溯模型产物）。
PROMPT_VERSION = "classroom-multiagent-2026-11-21-v1"

# ---------------------------------------------------------------------------
# 角色名册
# ---------------------------------------------------------------------------

AGENT_ROLES = ("teacher", "assistant", "student")
ROLE_LABELS = {"teacher": "老师", "assistant": "助教", "student": "同学"}
ROLE_PRIORITY = {"teacher": 10, "assistant": 7, "student": 5}

MIN_AGENTS = 2
MAX_AGENTS = 6
MAX_CLASSMATES = MAX_AGENTS - 1
DEFAULT_CLASSMATES = 3
MAX_AGENT_NAME_CHARS = 24
MAX_AGENT_PERSONA_CHARS = 400
MIN_AGENT_PERSONA_CHARS = 2
MAX_AGENT_ID_CHARS = 40
MAX_AVATAR_CHARS = 120

#: 角色配色（沿用 OpenMAIC ``AGENT_COLOR_PALETTE`` 前 6 色，保证多角色辨识度）。
AGENT_COLOR_PALETTE = (
    "#3b82f6", "#10b981", "#f59e0b", "#ec4899", "#06b6d4", "#8b5cf6",
)

#: 课程 config_json 里存名册的键（不改表结构）。
CONFIG_KEY = "agents"
CONFIG_META_KEY = "agents_meta"

_HEX_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")
_AGENT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")


def _text(value, limit: int = 400) -> str:
    """模型/接口输入里的标量安全转字符串（bool/None 视为空）。"""
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        value = str(value)
    if not isinstance(value, str):
        return ""
    return value.replace("\x00", "").strip()[:limit]


def _number(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip().rstrip("px").strip())
        except (TypeError, ValueError):
            return None
    else:
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number
    return None


def _int(value, default: int = 0) -> int:
    number = _number(value)
    return int(number) if number is not None else default


def _color(value, default: str) -> str:
    text = _text(value, 24)
    if text.lower() in ("transparent", "none"):
        return "transparent"
    return text if _HEX_COLOR_RE.match(text) else default


def empty_roster() -> dict:
    return {"teacher": None, "classmates": []}


def _clean_agent(item, *, role: str, index: int, default_id: str, used_ids: set[str],
                 used_colors: list[str], warnings: list[str]) -> dict | None:
    """校验并规范化单个角色；结构性错误抛 ClassroomSchemaError（驱动模型重试）。"""
    if not isinstance(item, dict):
        raise schema.ClassroomSchemaError("角色名册中的角色必须是 JSON 对象")
    name = _text(item.get("name") or item.get("agent_name") or item.get("nickname"), MAX_AGENT_NAME_CHARS)
    if not name:
        raise schema.ClassroomSchemaError("角色名册第 %d 个角色缺少 name" % (index + 1))
    persona = _text(item.get("persona") or item.get("profile") or item.get("description"),
                    MAX_AGENT_PERSONA_CHARS)
    if len(persona) < MIN_AGENT_PERSONA_CHARS:
        raise schema.ClassroomSchemaError("角色「%s」的 persona 过短（至少 %d 字，写性格与教学/学习风格）"
                                          % (name, MIN_AGENT_PERSONA_CHARS))
    role_raw = _text(item.get("role"), 20).lower()
    role_value = role if role == "teacher" else ("assistant" if role_raw == "assistant" else "student")
    if role != "teacher" and role_raw == "teacher":
        raise schema.ClassroomSchemaError("角色名册只能有 1 名老师（teacher），「%s」不应是 teacher" % name)
    agent_id = _text(item.get("id") or item.get("agent_id") or item.get("agentId"), MAX_AGENT_ID_CHARS).lower()
    if not agent_id or not _AGENT_ID_RE.match(agent_id) or agent_id in used_ids:
        if agent_id:
            warnings.append("角色「%s」的 id「%s」非法或重复，已重新分配" % (name, agent_id))
        agent_id = default_id
        suffix = 2
        while agent_id in used_ids:
            agent_id = "%s_%d" % (default_id, suffix)
            suffix += 1
    used_ids.add(agent_id)
    color = _color(item.get("color"), "")
    if not color:
        color = AGENT_COLOR_PALETTE[len(used_colors) % len(AGENT_COLOR_PALETTE)]
    used_colors.append(color)
    priority = _int(item.get("priority"), 0)
    if not 1 <= priority <= 10:
        priority = ROLE_PRIORITY.get(role_value, 5)
    return {
        "id": agent_id,
        "name": name,
        "role": role_value,
        "persona": persona,
        "avatar": _text(item.get("avatar"), MAX_AVATAR_CHARS),
        "color": color,
        "priority": priority,
    }


def normalize_agent_roster(raw) -> tuple[dict, list[str]]:
    """校验/规范化角色名册，返回 ``({teacher, classmates}, warnings)``。

    兼容三种输入：模型产出的 ``{teacher, classmates}``（只含 name/persona）、
    接口完整回写（含 id/color/priority）、以及 ``{agents:[{role,name,persona}]}`` 扁平列表。
    结构性错误抛 :class:`schema.ClassroomSchemaError`，用于驱动 ``call_structured`` 重试。
    """
    warnings: list[str] = []
    payload = schema.parse_json_payload(raw)
    if not isinstance(payload, dict):
        raise schema.ClassroomSchemaError("角色名册必须是 JSON 对象")
    teacher_raw = payload.get("teacher") or payload.get("teacher_agent") or payload.get("teacherAgent")
    classmates_raw = payload.get("classmates")
    if classmates_raw is None:
        classmates_raw = payload.get("students") or payload.get("classmates_agents") or []
    if isinstance(classmates_raw, dict):
        classmates_raw = [classmates_raw]
    if not isinstance(classmates_raw, list):
        raise schema.ClassroomSchemaError("角色名册的 classmates 必须是数组")
    if teacher_raw is None and isinstance(payload.get("agents"), list):
        # 扁平 ``agents`` 列表：按 role 拆分。
        agents = payload.get("agents") or []
        teachers = [item for item in agents if isinstance(item, dict)
                    and _text(item.get("role"), 20).lower() == "teacher"]
        if teachers:
            teacher_raw = teachers[0]
        classmates_raw = [item for item in agents if item not in teachers]
    if not isinstance(teacher_raw, dict):
        raise schema.ClassroomSchemaError("角色名册缺少 teacher（必须恰好有 1 名老师）")
    teacher_role = _text(teacher_raw.get("role"), 20).lower()
    if teacher_role and teacher_role != "teacher":
        raise schema.ClassroomSchemaError("角色名册的 teacher 字段角色必须是 teacher")

    used_ids: set[str] = set()
    used_colors: list[str] = []
    teacher = _clean_agent(teacher_raw, role="teacher", index=0, default_id="teacher_1",
                           used_ids=used_ids, used_colors=used_colors, warnings=warnings)
    if teacher is None:  # pragma: no cover - _clean_agent 只会抛错
        raise schema.ClassroomSchemaError("角色名册缺少 teacher")
    teacher["role"] = "teacher"
    teacher["priority"] = max(int(teacher.get("priority") or ROLE_PRIORITY["teacher"]), ROLE_PRIORITY["teacher"])

    classmates: list[dict] = []
    for index, item in enumerate(classmates_raw):
        agent = _clean_agent(item, role="student", index=index + 1, default_id="student_%d" % (index + 1),
                             used_ids=used_ids, used_colors=used_colors, warnings=warnings)
        if agent is not None:
            classmates.append(agent)
        if len(classmates) >= MAX_CLASSMATES:
            if len(classmates_raw) > MAX_CLASSMATES:
                warnings.append("角色名册超过上限 %d 名角色，多余角色已丢弃" % MAX_AGENTS)
            break
    if not classmates:
        raise schema.ClassroomSchemaError("角色名册至少需要 1 名同学（classmates）参与多角色讨论")
    return {"teacher": teacher, "classmates": classmates}, warnings


def roster_agents(roster) -> list[dict]:
    """扁平化名册：teacher 在前，其余按名册顺序。"""
    if not isinstance(roster, dict):
        return []
    agents: list[dict] = []
    teacher = roster.get("teacher")
    if isinstance(teacher, dict) and _text(teacher.get("name"), 80):
        agents.append(dict(teacher))
    for item in roster.get("classmates") or []:
        if isinstance(item, dict) and _text(item.get("name"), 80):
            agents.append(dict(item))
    return agents


def has_roster(roster) -> bool:
    return len(roster_agents(roster)) >= MIN_AGENTS


def find_agent(roster, agent_id: str | None) -> dict | None:
    wanted = _text(agent_id, MAX_AGENT_ID_CHARS).lower()
    if not wanted:
        return None
    for agent in roster_agents(roster):
        if _text(agent.get("id"), MAX_AGENT_ID_CHARS).lower() == wanted:
            return agent
    return None


def find_agent_by_name(roster, name: str | None) -> dict | None:
    wanted = _text(name, MAX_AGENT_NAME_CHARS)
    if not wanted:
        return None
    for agent in roster_agents(roster):
        if _text(agent.get("name"), MAX_AGENT_NAME_CHARS) == wanted:
            return agent
    return None


def roster_from_config(config) -> dict:
    """从课程 ``config_json`` 读取名册；缺失/损坏都按空名册处理（不 500）。"""
    if not isinstance(config, dict):
        return empty_roster()
    raw = config.get(CONFIG_KEY)
    if not isinstance(raw, dict):
        return empty_roster()
    try:
        roster, _warnings = normalize_agent_roster(raw)
    except (schema.ClassroomSchemaError, ValueError):
        return empty_roster()
    return roster


def roster_meta_from_config(config) -> dict:
    if not isinstance(config, dict):
        return {}
    meta = config.get(CONFIG_META_KEY)
    return meta if isinstance(meta, dict) else {}


def with_roster_config(config, roster: dict, *, model: str = "", provider_name: str = "",
                       preserve_meta: bool = False) -> dict:
    """返回写入名册后的新 config（不改其余既有配置键）。

    ``preserve_meta=True`` 用于人工编辑：保留生成时间/模型信息，只刷新 ``updated_at``。
    """
    updated = dict(config) if isinstance(config, dict) else {}
    updated[CONFIG_KEY] = roster
    meta = dict(roster_meta_from_config(config)) if preserve_meta else {}
    meta["prompt_version"] = PROMPT_VERSION
    if model:
        meta["model"] = _text(model, 160)
    if provider_name:
        meta["provider_name"] = _text(provider_name, 120)
    now = cdb.utcnow()
    if not preserve_meta or not meta.get("generated_at"):
        meta["generated_at"] = now
    meta["updated_at"] = now
    meta["agent_count"] = len(roster_agents(roster))
    updated[CONFIG_META_KEY] = meta
    return updated


def without_roster_config(config) -> dict:
    updated = dict(config) if isinstance(config, dict) else {}
    updated.pop(CONFIG_KEY, None)
    updated.pop(CONFIG_META_KEY, None)
    return updated


def roster_public(roster) -> dict:
    """接口返回用的名册视图（只暴露前端需要的字段）。"""
    agents = roster_agents(roster)
    return {
        "teacher": next((dict(agent) for agent in agents if agent.get("role") == "teacher"), None),
        "classmates": [dict(agent) for agent in agents if agent.get("role") != "teacher"],
    }


def merge_roster_patch(roster, *, teacher_patch: dict | None = None,
                       classmate_patches: list[dict] | None = None) -> tuple[dict, list[str]]:
    """按 id 局部编辑角色名/人设/颜色/优先级，返回 ``(roster, warnings)``。"""
    warnings: list[str] = []
    agents = roster_agents(roster)
    if not agents:
        raise schema.ClassroomSchemaError("请先生成角色名册，再进行局部编辑")
    by_id = {_text(agent.get("id"), MAX_AGENT_ID_CHARS): dict(agent) for agent in agents}
    patches = []
    if isinstance(teacher_patch, dict):
        patches.append(teacher_patch)
    for item in classmate_patches or []:
        if isinstance(item, dict):
            patches.append(item)
    touched = 0
    for patch in patches:
        agent_id = _text(patch.get("id"), MAX_AGENT_ID_CHARS).lower()
        if not agent_id or agent_id not in by_id:
            raise schema.ClassroomSchemaError("角色名册中没有该角色：%s" % (agent_id or "（缺少 id）"))
        target = by_id[agent_id]
        if patch.get("name") is not None:
            name = _text(patch.get("name"), MAX_AGENT_NAME_CHARS)
            if not name:
                raise schema.ClassroomSchemaError("角色名称不能为空")
            target["name"] = name
        if patch.get("persona") is not None:
            persona = _text(patch.get("persona"), MAX_AGENT_PERSONA_CHARS)
            if len(persona) < MIN_AGENT_PERSONA_CHARS:
                raise schema.ClassroomSchemaError("角色「%s」的 persona 过短" % target.get("name"))
            target["persona"] = persona
        if patch.get("color") is not None:
            color = _color(patch.get("color"), "")
            if not color:
                raise schema.ClassroomSchemaError("颜色必须是 #rgb/#rrggbb 形式的十六进制色值")
            target["color"] = color
        if patch.get("priority") is not None:
            priority = _int(patch.get("priority"), 0)
            if not 1 <= priority <= 10:
                raise schema.ClassroomSchemaError("角色优先级必须是 1-10 的整数")
            target["priority"] = priority
        touched += 1
    if not touched:
        warnings.append("没有可应用的角色编辑")
    teacher = next((item for item in by_id.values() if item.get("role") == "teacher"), None)
    classmates = [by_id[_text(agent.get("id"), MAX_AGENT_ID_CHARS)] for agent in agents
                  if agent.get("role") != "teacher"]
    result = {"teacher": dict(teacher) if teacher else None, "classmates": classmates}
    roster_normalized, warnings2 = normalize_agent_roster(result)
    warnings.extend(warnings2)
    return roster_normalized, warnings


# ---------------------------------------------------------------------------
# 增强白板动作（0-1 归一化坐标）
# ---------------------------------------------------------------------------

WHITEBOARD_KINDS = ("text", "formula", "line", "rect", "circle", "arrow", "path")
MAX_WHITEBOARD_OPS = 40
MAX_WB_TEXT_CHARS = 200
MAX_WB_FORMULA_CHARS = 300
MAX_WB_PATH_CHARS = 600
MAX_ACTION_TEXT_CHARS = 2000  # 动作内其它文字字段（speech 等）沿用 schema 上限
MAX_ACTIONS_PER_TURN = 6
DISCUSSION_ACTION_TYPES = (
    "spotlight", "laser", "highlight", "whiteboard", "next_scene", "show_slide", "play_audio",
)

_WB_KIND_ALIASES = {
    "text": "text", "label": "text", "note": "text", "文字": "text", "文本": "text",
    "formula": "formula", "latex": "formula", "math": "formula", "equation": "formula", "公式": "formula",
    "line": "line", "segment": "line", "线": "line", "直线": "line",
    "arrow": "arrow", "arrow_line": "arrow", "箭头": "arrow",
    "rect": "rect", "rectangle": "rect", "box": "rect", "square": "rect", "矩形": "rect",
    "circle": "circle", "ellipse": "circle", "oval": "circle", "圆": "circle", "椭圆": "circle",
    "path": "path", "curve": "path", "svg": "path", "路径": "path", "曲线": "path",
}

_ACTION_ALIASES = {
    "speech": "speech", "say": "speech", "narration": "speech", "text": "speech",
    "spotlight": "spotlight", "focus": "spotlight",
    "laser": "laser", "laser_pointer": "laser",
    "highlight": "highlight",
    "whiteboard": "whiteboard", "wb": "whiteboard", "white_board": "whiteboard",
    "next_scene": "next_scene", "next": "next_scene",
    "show_slide": "show_slide", "show": "show_slide",
    "play_audio": "play_audio", "audio": "play_audio",
}

_PATH_ALLOWED_RE = re.compile(r"^[MmLlHhVvCcSsQqTtAaZz0-9eE .,+\-]+$")


def _unit(value, default: float = 0.0) -> float:
    """把 0-1 归一化坐标收敛到 [0, 1]（越界/非数值都会退化为默认值或端点）。"""
    number = _number(value)
    if number is None:
        number = default
    if number != number:  # NaN 防御
        number = default
    return round(max(0.0, min(1.0, number)), 4)


def _unit_size(value, default: float) -> float:
    number = _number(value)
    if number is None:
        number = default
    if number != number:
        number = default
    return round(max(0.01, min(1.0, number)), 4)


def _wb_text_op(raw) -> tuple[dict | None, str | None]:
    content = _text(raw.get("content") or raw.get("text") or raw.get("label"), MAX_WB_TEXT_CHARS + 200)
    if not content:
        return None, "白板 text 操作缺少 content，已丢弃"
    if len(content) > MAX_WB_TEXT_CHARS:
        content = content[:MAX_WB_TEXT_CHARS]
    return {
        "kind": "text",
        "x": _unit(raw.get("x")),
        "y": _unit(raw.get("y")),
        "content": content,
        "font_size": max(10, min(72, _int(raw.get("font_size") or raw.get("fontSize"), 18))),
        "color": _color(raw.get("color"), "#333333"),
    }, None


def _wb_formula_op(raw) -> tuple[dict | None, str | None]:
    latex = _text(raw.get("latex") or raw.get("formula") or raw.get("content"), MAX_WB_FORMULA_CHARS + 200)
    latex = latex.strip("$").strip()
    if not latex:
        return None, "白板 formula 操作缺少 latex，已丢弃"
    if len(latex) > MAX_WB_FORMULA_CHARS:
        latex = latex[:MAX_WB_FORMULA_CHARS]
    return {
        "kind": "formula",
        "x": _unit(raw.get("x")),
        "y": _unit(raw.get("y")),
        "width": _unit_size(raw.get("width"), 0.3),
        "latex": latex,
        "color": _color(raw.get("color"), "#1f6feb"),
    }, None


def _wb_segment_op(raw, kind: str) -> tuple[dict | None, str | None]:
    x1, y1 = _unit(raw.get("x1", raw.get("start_x"))), _unit(raw.get("y1", raw.get("start_y")))
    x2, y2 = _unit(raw.get("x2", raw.get("end_x"))), _unit(raw.get("y2", raw.get("end_y")))
    if abs(x2 - x1) < 0.005 and abs(y2 - y1) < 0.005:
        return None, "白板 %s 操作起点与终点重合，已丢弃" % kind
    return {
        "kind": kind,
        "x1": x1, "y1": y1, "x2": x2, "y2": y2,
        "color": _color(raw.get("color"), "#333333"),
        "width": max(1, min(8, _int(raw.get("width"), 2))),
    }, None


def _wb_shape_op(raw, kind: str) -> tuple[dict | None, str | None]:
    width = _unit_size(raw.get("width"), 0.2)
    height = _unit_size(raw.get("height"), 0.15)
    op = {
        "kind": kind,
        "x": _unit(raw.get("x")),
        "y": _unit(raw.get("y")),
        "width": width,
        "height": height,
        "fill": _color(raw.get("fill") or raw.get("color"), "transparent"),
        "stroke": _color(raw.get("stroke"), "#5b9bd5"),
    }
    return op, None


def _wb_path_op(raw) -> tuple[dict | None, str | None]:
    d = _text(raw.get("d") or raw.get("path") or raw.get("data"), MAX_WB_PATH_CHARS + 200)
    d = re.sub(r"\s+", " ", d).strip()
    if not d:
        return None, "白板 path 操作缺少 SVG path 数据 d，已丢弃"
    if len(d) > MAX_WB_PATH_CHARS:
        return None, "白板 path 的 d 过长（>%d 字符），已丢弃" % MAX_WB_PATH_CHARS
    if not d[0] in ("M", "m"):
        return None, "白板 path 的 d 必须以 M/m 开头，已丢弃"
    if not _PATH_ALLOWED_RE.match(d):
        return None, "白板 path 含有不允许的字符，已丢弃"
    view_box_raw = raw.get("view_box") or raw.get("viewBox")
    view_box = [0.0, 0.0]
    if isinstance(view_box_raw, list) and len(view_box_raw) == 2:
        width = _number(view_box_raw[0])
        height = _number(view_box_raw[1])
        if width is not None and height is not None and 0 < width <= 1000 and 0 < height <= 1000:
            view_box = [round(float(width), 2), round(float(height), 2)]
    if view_box == [0.0, 0.0]:
        view_box = [1.0, 1.0]
    stroke = raw.get("stroke")
    stroke_color = "#333333"
    if isinstance(stroke, dict):
        stroke_color = _color(stroke.get("color"), "#333333")
    else:
        stroke_color = _color(stroke, "#333333")
    return {
        "kind": "path",
        "x": _unit(raw.get("x")),
        "y": _unit(raw.get("y")),
        "width": _unit_size(raw.get("width"), 0.2),
        "height": _unit_size(raw.get("height"), 0.2),
        "d": d,
        "view_box": view_box,
        "fill": _color(raw.get("fill") or raw.get("fillColor"), "transparent"),
        "stroke": stroke_color,
        "stroke_width": max(1, min(8, _int(raw.get("stroke_width") or raw.get("strokeWidth"), 2))),
    }, None


def _normalize_whiteboard_op(raw) -> tuple[dict | None, str | None]:
    if not isinstance(raw, dict):
        return None, "已丢弃非对象的白板操作"
    kind_raw = _text(raw.get("kind") or raw.get("type") or raw.get("op"), 16).lower()
    kind = _WB_KIND_ALIASES.get(kind_raw)
    if kind is None:
        return None, "已拒绝未知白板操作类型：%s" % (kind_raw or "空")
    if kind == "text":
        return _wb_text_op(raw)
    if kind == "formula":
        return _wb_formula_op(raw)
    if kind in ("line", "arrow"):
        return _wb_segment_op(raw, kind)
    if kind in ("rect", "circle"):
        return _wb_shape_op(raw, kind)
    return _wb_path_op(raw)


def normalize_whiteboard_ops(raw) -> tuple[list[dict], list[str]]:
    """校验白板 ops（0-1 归一化坐标），返回 ``(ops, warnings)``；未知/非法 op 一律丢弃。"""
    warnings: list[str] = []
    if raw is None:
        return [], warnings
    if isinstance(raw, dict):
        raw = raw.get("ops") or raw.get("operations") or []
    if not isinstance(raw, list):
        return [], ["whiteboard 动作的 ops 不是数组，已忽略"]
    ops: list[dict] = []
    for item in raw[:MAX_WHITEBOARD_OPS]:
        op, warning = _normalize_whiteboard_op(item)
        if warning:
            warnings.append(warning)
        if op is not None:
            ops.append(op)
    if len(raw) > MAX_WHITEBOARD_OPS:
        warnings.append("白板操作数量超过上限 %d，后续已丢弃" % MAX_WHITEBOARD_OPS)
    return ops, warnings


def _flatten_action(raw: dict) -> dict:
    """兼容上游「动作信封」：``{type:"action", name, params}`` 与 ``{type:"text", name}``。"""
    params = raw.get("params")
    if isinstance(params, dict):
        flattened = dict(params)
        flattened.update({key: value for key, value in raw.items() if key != "params"})
        return flattened
    return raw


def _action_type_name(raw: dict) -> str:
    type_raw = _text(raw.get("type") or raw.get("kind"), 24).lower()
    if type_raw in ("action", "tool", ""):
        type_raw = _text(raw.get("name"), 24).lower()
    elif type_raw == "text" and raw.get("name"):
        type_raw = _text(raw.get("name"), 24).lower()
    return _ACTION_ALIASES.get(type_raw, "")


def normalize_multiagent_actions(raw, *, element_ids=None,
                                 max_actions: int = MAX_ACTIONS_PER_TURN) -> tuple[list[dict], list[str]]:
    """校验讨论/插话脚本里的动作：白名单 + 增强白板（0-1 坐标）+ 元素 id 归属。"""
    warnings: list[str] = []
    if raw is None:
        return [], warnings
    if isinstance(raw, dict):
        raw = raw.get("actions") or raw.get("items") or []
    if not isinstance(raw, list):
        return [], ["课堂动作不是数组，已忽略"]
    actions: list[dict] = []
    for item in raw[:max_actions]:
        if not isinstance(item, dict):
            warnings.append("已丢弃非对象的课堂动作")
            continue
        flattened = _flatten_action(item)
        action_type = _action_type_name(flattened)
        if not action_type:
            warnings.append("已拒绝未知课堂动作：%s" % (_text(flattened.get("type") or flattened.get("name"), 24) or "空"))
            continue
        if action_type == "whiteboard":
            ops, op_warnings = normalize_whiteboard_ops(flattened.get("ops") or flattened.get("operations"))
            warnings.extend(op_warnings)
            if not ops:
                warnings.append("whiteboard 动作没有有效操作，已丢弃")
                continue
            actions.append({"type": "whiteboard", "coord_space": "unit", "ops": ops})
            continue
        normalized, item_warnings = schema.normalize_actions(
            [flattened], element_ids=element_ids, max_actions=1,
        )
        warnings.extend(item_warnings)
        if not normalized:
            continue
        action = normalized[0]
        if action.get("type") == "speech":
            warnings.append("讨论脚本的发言请写在 text 字段，已忽略 speech 动作")
            continue
        if action.get("type") not in DISCUSSION_ACTION_TYPES:
            warnings.append("讨论脚本不支持该动作：%s" % action.get("type"))
            continue
        actions.append(action)
    if len(raw) > max_actions:
        warnings.append("单段发言的动作数量超过上限 %d，后续已丢弃" % max_actions)
    for index, action in enumerate(actions, 1):
        action["id"] = "act_%03d" % index
    return actions, warnings


def _normalize_scene_hint(payload: dict, scene_count: int = 0) -> dict | None:
    hint_raw = payload.get("scene_hint") or payload.get("sceneHint")
    if not isinstance(hint_raw, dict):
        return None
    hint: dict = {}
    index = _number(hint_raw.get("index", hint_raw.get("scene_index")))
    if index is not None:
        value = int(index)
        if scene_count and 0 <= value < scene_count:
            hint["index"] = value
        elif not scene_count:
            hint["index"] = max(0, value)
    scene_id = _text(hint_raw.get("scene_id") or hint_raw.get("sceneId"), 40)
    if scene_id:
        hint["scene_id"] = scene_id
    reason = _text(hint_raw.get("reason"), 200)
    if reason and hint:
        hint["reason"] = reason
    # 只有 reason 没有可用跳转目标（index/scene_id）的 hint 没有 playback 价值，直接丢弃。
    return hint or None


# ---------------------------------------------------------------------------
# 讨论 / 插话脚本校验
# ---------------------------------------------------------------------------

DISCUSSION_MODES = ("discussion", "qa", "lecture")
DISCUSSION_MODE_LABELS = {"discussion": "多角色讨论", "qa": "自由问答", "lecture": "多角色讲授"}
MIN_ROUNDS = 1
MAX_ROUNDS = 3
MAX_DISCUSSION_TURNS = 6
MAX_INTERJECT_TURNS = 2
MAX_TURN_TEXT_CHARS = 500


def normalize_discussion_script(raw, *, roster, element_ids=None, scene_count: int = 0,
                                max_turns: int = MAX_DISCUSSION_TURNS) -> tuple[dict, list[str]]:
    """校验多角色脚本 ``{turns:[{speaker_id,text,actions}], scene_hint?}``。

    严格语义：speaker 必须是名册里的角色（未知角色/空台词会抛 ClassroomSchemaError 触发重试）；
    超出段数上限截断并告警；动作过白名单与增强白板校验。
    """
    warnings: list[str] = []
    payload = schema.parse_json_payload(raw)
    if not isinstance(payload, dict):
        raise schema.ClassroomSchemaError("多角色脚本必须是 JSON 对象")
    turns_raw = payload.get("turns")
    if turns_raw is None:
        turns_raw = payload.get("segments") or payload.get("script")
    if not isinstance(turns_raw, list) or not turns_raw:
        raise schema.ClassroomSchemaError("多角色脚本缺少非空的 turns 数组")
    agents = roster_agents(roster)
    if not agents:
        raise schema.ClassroomSchemaError("多角色脚本需要先有课堂角色名册")
    allowed_ids = set(element_ids) if element_ids is not None else None
    turns: list[dict] = []
    for index, item in enumerate(turns_raw[:max_turns]):
        if not isinstance(item, dict):
            raise schema.ClassroomSchemaError("第 %d 段发言不是 JSON 对象" % (index + 1))
        agent = find_agent(roster, item.get("speaker_id") or item.get("speakerId") or item.get("agent_id"))
        if agent is None:
            agent = find_agent_by_name(roster, item.get("speaker_name") or item.get("speaker") or item.get("name"))
        if agent is None:
            raise schema.ClassroomSchemaError(
                "第 %d 段发言的角色不在名册中：%s" % (
                    index + 1,
                    _text(item.get("speaker_id") or item.get("speaker_name") or item.get("speaker"), 40) or "空",
                )
            )
        text = _text(item.get("text") or item.get("content") or item.get("speech"), MAX_TURN_TEXT_CHARS + 100)
        if not text:
            raise schema.ClassroomSchemaError("第 %d 段发言（%s）缺少 text" % (index + 1, agent.get("name")))
        if len(text) > MAX_TURN_TEXT_CHARS:
            text = text[:MAX_TURN_TEXT_CHARS]
            warnings.append("第 %d 段发言过长，已截断到 %d 字" % (index + 1, MAX_TURN_TEXT_CHARS))
        actions, action_warnings = normalize_multiagent_actions(
            item.get("actions"), element_ids=allowed_ids,
        )
        warnings.extend(action_warnings)
        turns.append({
            "speaker_id": _text(agent.get("id"), MAX_AGENT_ID_CHARS),
            "speaker_name": _text(agent.get("name"), MAX_AGENT_NAME_CHARS),
            "role": _text(agent.get("role"), 20) or "student",
            "color": _text(agent.get("color"), 24),
            "text": text,
            "actions": actions,
        })
    if len(turns_raw) > max_turns:
        warnings.append("多角色脚本超过 %d 段上限，后续发言已丢弃" % max_turns)
    result = {"turns": turns, "scene_hint": _normalize_scene_hint(payload, scene_count)}
    return result, warnings


# ---------------------------------------------------------------------------
# prompt（中文，结构性移植 OpenMAIC Director / Child prompt 语义）
# ---------------------------------------------------------------------------

_JSON_RULES = """## 输出格式要求（必须严格遵守）
1. 只输出纯 JSON，不要解释、不要 Markdown 代码块（```）、不要在 JSON 前后加任何文字。
2. JSON 必须能被 json.loads 直接解析；字符串内不要出现未转义换行。
3. 所有面向学生的文字使用简体中文。
4. 不要反问，不要输出“稍等/我无法完成”之类的说明。"""

_WHITEBOARD_RULES = """## 白板动作（whiteboard）
- 形态：{"type":"whiteboard","ops":[...]}，一次最多 %d 个 op。
- 坐标一律 **0-1 归一化**：左上角 (0,0)，右下角 (1,1)，不要用像素；x/y/width/height 都取 0-1。
- op 类型：
  - text：{"kind":"text","x":0.1,"y":0.2,"content":"关键词（≤200字）","font_size":22,"color":"#333333"}
  - formula：{"kind":"formula","x":0.1,"y":0.35,"width":0.4,"latex":"C = 2\\pi r"}
  - line：{"kind":"line","x1":0.1,"y1":0.5,"x2":0.5,"y2":0.5,"color":"#333333","width":2}
  - arrow：同上但 kind 为 arrow（表示带箭头连线）
  - rect：{"kind":"rect","x":0.1,"y":0.1,"width":0.25,"height":0.15,"fill":"transparent","stroke":"#5b9bd5"}
  - circle：同 rect，kind 为 circle（x/y 为外接框左上角，width/height 为 0-1 尺寸）
  - path：{"kind":"path","x":0.1,"y":0.6,"width":0.2,"height":0.1,"d":"M0 0 L1 0 L1 1 Z","view_box":[1,1],"fill":"transparent","stroke":"#333333"}
- 白板只放关键词/公式/流程，不要写长篇台词；只在讲机制、流程、对比、推导时使用。""" % MAX_WHITEBOARD_OPS


def _roster_text(roster) -> str:
    lines = []
    for agent in roster_agents(roster):
        lines.append("- id: \"%s\"｜姓名：%s｜角色：%s｜优先级：%d｜人设：%s" % (
            agent.get("id"), agent.get("name"),
            ROLE_LABELS.get(str(agent.get("role") or ""), agent.get("role")),
            int(agent.get("priority") or 5), _text(agent.get("persona"), MAX_AGENT_PERSONA_CHARS),
        ))
    return "\n".join(lines) or "（名册为空）"


def _history_text(history) -> str:
    lines = []
    for turn in (history or [])[-8:]:
        if not isinstance(turn, dict):
            continue
        role = _text(turn.get("role"), 20)
        speaker = _text(turn.get("speaker"), 40)
        if role == "user":
            lines.append("学生（插话/提问）：%s" % _text(turn.get("content"), 300))
        else:
            label = ROLE_LABELS.get(role, "课堂角色")
            lines.append("%s%s：%s" % (speaker or label, "（%s）" % label if speaker else "", _text(turn.get("content"), 300)))
    return "\n".join(lines) or "（暂无历史对话）"


def build_roster_prompt(*, title: str, topic: str, subject: str = "", grade: str = "",
                        mode: str = "standard", requirements: str = "",
                        classmate_count: int = DEFAULT_CLASSMATES) -> str:
    """课程角色名册生成 prompt：一次结构化调用产出 teacher + classmates。"""
    count = max(1, min(MAX_CLASSMATES, int(classmate_count or DEFAULT_CLASSMATES)))
    mode_label = {"standard": "标准课堂", "interactive": "深度交互"}.get(mode, mode)
    return f"""# 课堂角色名册生成

你是一名课堂角色设计师，为「知序」的 AI 互动课堂设计多智能体角色名册。

## 课程信息
- 课程标题：{_text(title, 60) or _text(topic, 60)}
- 课程主题：{_text(topic, 500)}
- 学科 / 学段：{_text(subject, 40) or "未指定"} / {_text(grade, 40) or "未指定"}
- 课程模式：{mode_label}
- 教师要求：{_text(requirements, 1000) or "（无）"}

## 任务
设计 1 名老师（teacher）和 {count} 名同学（classmates），构成能支撑多角色讨论的课堂名册。

## 设计规则
1. 姓名：2-8 个简体中文字符，符合该学科与学段的气质；同学之间要有明显差异（性格、学习风格、常见疑问不同）。
2. persona：2-3 句具体描述，写清性格 + 教学/学习风格，不要只写“老师/学生”这类角色标签。
3. 老师要能引导讨论、点名提问、总结；同学要能提出真实疑问、暴露常见误区或补充例子。
4. 角色之间不要重名；不要出现真实人名、学校名或任何隐私信息。
5. 只设计 teacher 与 classmates，不要设计助教角色。

{_JSON_RULES}

## 输出结构（严格）
{{
  "teacher": {{"name": "李老师", "persona": "……"}},
  "classmates": [
    {{"name": "小明", "persona": "……"}},
    {{"name": "小雅", "persona": "……"}}
  ]
}}
classmates 数组必须恰好 {count} 项；除 name/persona 外不要输出其它字段。"""


def build_discussion_prompt(*, course_title: str, scene_title: str, scene_type: str, scene_digest: str,
                            roster, mode: str = "discussion", topic: str = "", rounds: int = 1,
                            user_message: str = "", history=None, position: int = 1, total: int = 1,
                            element_ids=(), state_digest: str = "",
                            max_turns: int = MAX_DISCUSSION_TURNS) -> str:
    """多角色讨论/自由问答/讲授的整段脚本 prompt（一次调用返回全部 turns）。"""
    mode_text = {
        "discussion": (
            "多角色讨论（可含圆桌辩论）：老师先点题或抛出问题，同学依次表达观点/追问/互相补充，"
            "必要时形成 2 个不同立场的简短辩论，最后由老师归纳。"
        ),
        "qa": "自由问答：老师先直接回答学生的问题，再由助教或同学补充一个角度或追问，共 2-4 段。",
        "lecture": "多角色讲授：老师分 2-3 段循序讲解，中途让 1 名同学复述或提出一个真实疑问，老师再收束。",
    }.get(mode, "多角色讨论：老师引导，同学提问或补充。")
    rounds_value = max(MIN_ROUNDS, min(MAX_ROUNDS, int(rounds or MIN_ROUNDS)))
    suggested = min(max_turns - 1, 1 + rounds_value * 2) if mode == "discussion" else min(4, max_turns)
    element_text = "、".join(str(item) for item in list(element_ids or [])[:24]) or "（本页没有可指向的元素，不要使用 spotlight/laser/highlight）"
    return f"""# 多智能体课堂脚本生成

你是 AI 课堂的**导演（Director）**：不亲自讲课，而是安排名册里的角色按顺序发言，并给每段发言配上课堂动作。
所有课堂内容只依据下面提供的「当前场景摘要」与「角色名册」，不要编造页面没有的内容。

## 当前课堂
- 课程：{_text(course_title, 60)}
- 当前场景：第 {position}/{total} 页《{_text(scene_title, 120)}》（类型：{_text(scene_type, 20)}）
- 本页内容摘要：
{_text(scene_digest, 2500) or "（本页内容摘要缺失，请只做与主题相关的通用引导）"}

## 角色名册（只能让这些角色发言）
{_roster_text(roster)}

## 本次任务
- 模式：{mode_text}
- 讨论轮次：{rounds_value} 轮（建议输出约 {suggested} 段发言，最多 {max_turns} 段）
- 讨论主题：{_text(topic, 200) or "（沿用当前场景主题）"}
- 学生本次发言：{_text(user_message, 800) or "（学生没有发言，由老师发起讨论）"}
- 可指向的幻灯片元素 id：{element_text}
- 学生交互状态：{_text(state_digest, 800) or "（无）"}

## 最近对话（供衔接，不要重复）
{_history_text(history)}

## 编排规则（硬性）
1. speaker_id 必须来自上面名册的 id，不得使用名册外的角色；不要让同一角色连续发言超过 2 段。
2. 讨论模式必须至少出现 1 名同学；老师负责点题、追问和归纳，同学负责提问、补充或个人观点。
3. 每段 text 用口语化简体中文：老师 40-120 字，助教/同学 20-60 字；不要 Markdown、列表符号或角色括号前缀。
4. 不要复述 prompt、不要暴露系统设定、不要把 JSON 或动作写进 text。
5. actions 只能使用白名单：spotlight、laser、highlight、whiteboard、next_scene、show_slide、play_audio。
   - spotlight/laser/highlight 的 element_id 必须来自上面的元素 id；不确定就省略。
   - next_scene/show_slide 只在确实需要换页时使用（index 从 0 开始）。
   - play_audio 无音频素材可引用，不要使用。
6. 说话要像真实课堂：老师可以提问，同学可以答错后再被引导，但不要伪造学生的正确答案。
7. scene_hint 可选：仅当讨论结束必须切到另一页时才给出，index 从 0 开始；否则省略。

{_WHITEBOARD_RULES}

{_JSON_RULES}

## 输出结构（严格，只输出一个 JSON 对象）
{{
  "turns": [
    {{"speaker_id": "teacher_1", "text": "……", "actions": [
      {{"type": "spotlight", "element_id": "text_001"}}
    ]}},
    {{"speaker_id": "student_1", "text": "……", "actions": [
      {{"type": "whiteboard", "ops": [
        {{"kind":"text","x":0.08,"y":0.12,"content":"先找已知条件","font_size":20}},
        {{"kind":"arrow","x1":0.08,"y1":0.32,"x2":0.32,"y2":0.32}}
      ]}}
    ]}}
  ],
  "scene_hint": {{"index": 1, "reason": "下一段需要看下一页的图"}}
}}
actions 可以为空数组 []；scene_hint 不需要时直接省略。"""


def build_interject_prompt(*, course_title: str, scene_title: str, scene_type: str, scene_digest: str,
                           roster, message: str, history=None, position: int = 1, total: int = 1,
                           element_ids=(), state_digest: str = "",
                           max_turns: int = MAX_INTERJECT_TURNS) -> str:
    """学生插话后的多角色回应 prompt（1-2 段）。"""
    element_text = "、".join(str(item) for item in list(element_ids or [])[:24]) or "（本页没有可指向的元素，不要使用 spotlight/laser/highlight）"
    return f"""# 多智能体课堂 · 学生插话回应

你是 AI 课堂的**导演（Director）**：学生刚刚插话，请安排 1-2 段角色回应（最多 {max_turns} 段）。
所有内容只依据「当前场景摘要」「角色名册」和「最近对话」，不要编造页面没有的内容。

## 当前课堂
- 课程：{_text(course_title, 60)}
- 当前场景：第 {position}/{total} 页《{_text(scene_title, 120)}》（类型：{_text(scene_type, 20)}）
- 本页内容摘要：
{_text(scene_digest, 2500) or "（本页内容摘要缺失，请只做与主题相关的通用回应）"}

## 角色名册（只能让这些角色发言）
{_roster_text(roster)}

## 最近对话
{_history_text(history)}

## 学生插话
{_text(message, 800)}

## 规则（硬性）
1. 第一段必须由老师（或助教）回应，先正面接住学生的话；需要时第 2 段由 1 名同学补充或追问。
2. speaker_id 必须来自名册；不要重复最近对话里已有的内容。
3. text 口语化简体中文：老师 40-120 字，同学 20-60 字；不要 Markdown、列表符号或角色括号前缀。
4. actions 白名单：spotlight、laser、highlight、whiteboard、next_scene、show_slide、play_audio；
   spotlight/laser/highlight 的 element_id 只能来自：{element_text}；play_audio 不要使用。
5. 学生问与本页无关的问题时，先简短回应，再给 1 个与本页相关的引导。
6. 不需要 scene_hint 时省略。

{_WHITEBOARD_RULES}

{_JSON_RULES}

## 输出结构（严格）
{{
  "turns": [
    {{"speaker_id": "teacher_1", "text": "……", "actions": []}},
    {{"speaker_id": "student_1", "text": "……", "actions": []}}
  ]
}}
只输出 1-{max_turns} 段；actions 可以为空数组 []。"""


# ---------------------------------------------------------------------------
# 示例动作序列（供 prompt 少样本、文档与前端联调）
# ---------------------------------------------------------------------------

#: 做题推导：已知条件 → 公式 → 结论（0-1 归一化坐标）。
EXAMPLE_DERIVATION_OPS = [
    {"kind": "text", "x": 0.06, "y": 0.08, "content": "已知：半径 r = 3", "font_size": 22, "color": "#333333"},
    {"kind": "formula", "x": 0.06, "y": 0.22, "width": 0.32, "latex": "C = 2\\pi r", "color": "#1f6feb"},
    {"kind": "arrow", "x1": 0.06, "y1": 0.36, "x2": 0.06, "y2": 0.46, "color": "#333333", "width": 2},
    {"kind": "text", "x": 0.06, "y": 0.5, "content": "代入：C = 2 × 3.14 × 3 = 18.84", "font_size": 20, "color": "#2f9e44"},
    {"kind": "line", "x1": 0.06, "y1": 0.66, "x2": 0.4, "y2": 0.66, "color": "#e8590c", "width": 2},
    {"kind": "text", "x": 0.06, "y": 0.7, "content": "答：周长约 18.84", "font_size": 24, "color": "#c2255c"},
]

#: 流程图：三步流程 + 箭头（0-1 归一化坐标）。
EXAMPLE_FLOW_OPS = [
    {"kind": "rect", "x": 0.06, "y": 0.18, "width": 0.22, "height": 0.16, "fill": "transparent", "stroke": "#5b9bd5"},
    {"kind": "text", "x": 0.09, "y": 0.24, "content": "光反应", "font_size": 20, "color": "#333333"},
    {"kind": "arrow", "x1": 0.28, "y1": 0.26, "x2": 0.38, "y2": 0.26, "color": "#333333", "width": 2},
    {"kind": "rect", "x": 0.38, "y": 0.18, "width": 0.22, "height": 0.16, "fill": "transparent", "stroke": "#5b9bd5"},
    {"kind": "text", "x": 0.41, "y": 0.24, "content": "ATP / NADPH", "font_size": 18, "color": "#333333"},
    {"kind": "arrow", "x1": 0.6, "y1": 0.26, "x2": 0.7, "y2": 0.26, "color": "#333333", "width": 2},
    {"kind": "rect", "x": 0.7, "y": 0.18, "width": 0.22, "height": 0.16, "fill": "transparent", "stroke": "#5b9bd5"},
    {"kind": "text", "x": 0.73, "y": 0.24, "content": "暗反应", "font_size": 20, "color": "#333333"},
]

#: 公式推导：多行公式 + 结论（0-1 归一化坐标）。
EXAMPLE_FORMULA_OPS = [
    {"kind": "text", "x": 0.06, "y": 0.08, "content": "由勾股定理：", "font_size": 20, "color": "#333333"},
    {"kind": "formula", "x": 0.06, "y": 0.2, "width": 0.5, "latex": "a^2 + b^2 = c^2", "color": "#1f6feb"},
    {"kind": "formula", "x": 0.06, "y": 0.36, "width": 0.6, "latex": "c = \\sqrt{a^2 + b^2}", "color": "#1f6feb"},
    {"kind": "path", "x": 0.72, "y": 0.2, "width": 0.18, "height": 0.18,
     "d": "M0 1 L1 0 M1 0 L0.8 0 M1 0 L1 0.2", "view_box": [1, 1],
     "fill": "transparent", "stroke": "#e8590c", "stroke_width": 2},
]

WHITEBOARD_EXAMPLES = {
    "derivation": EXAMPLE_DERIVATION_OPS,  # 做题推导
    "flow": EXAMPLE_FLOW_OPS,              # 流程图
    "formula": EXAMPLE_FORMULA_OPS,        # 公式推导
}


def sample_discussion_actions() -> dict:
    """给文档/前端联调用的样例动作组合（直接可下发播放器）。"""
    whiteboard, _ = normalize_whiteboard_ops(EXAMPLE_FLOW_OPS)
    return {
        "spotlight": {"id": "act_001", "type": "spotlight", "element_id": "text_001", "dim_opacity": 0.5},
        "whiteboard": {"id": "act_002", "type": "whiteboard", "coord_space": "unit", "ops": whiteboard},
    }


__all__ = [
    "AGENT_COLOR_PALETTE", "AGENT_ROLES", "CONFIG_KEY", "CONFIG_META_KEY",
    "DEFAULT_CLASSMATES", "DISCUSSION_ACTION_TYPES", "DISCUSSION_MODES", "DISCUSSION_MODE_LABELS",
    "EXAMPLE_DERIVATION_OPS", "EXAMPLE_FLOW_OPS", "EXAMPLE_FORMULA_OPS",
    "MAX_ACTIONS_PER_TURN", "MAX_AGENTS", "MAX_CLASSMATES", "MAX_DISCUSSION_TURNS",
    "MAX_INTERJECT_TURNS", "MAX_ROUNDS", "MAX_TURN_TEXT_CHARS", "MAX_WB_FORMULA_CHARS",
    "MAX_WB_PATH_CHARS", "MAX_WB_TEXT_CHARS", "MAX_WHITEBOARD_OPS", "MIN_AGENTS",
    "MIN_ROUNDS", "PROMPT_VERSION", "ROLE_LABELS", "ROLE_PRIORITY", "WHITEBOARD_EXAMPLES",
    "WHITEBOARD_KINDS", "build_discussion_prompt", "build_interject_prompt",
    "build_roster_prompt", "empty_roster", "find_agent", "find_agent_by_name", "has_roster",
    "merge_roster_patch", "normalize_agent_roster", "normalize_discussion_script",
    "normalize_multiagent_actions", "normalize_whiteboard_ops", "roster_agents",
    "roster_from_config", "roster_meta_from_config", "roster_public", "sample_discussion_actions",
    "with_roster_config", "without_roster_config",
]
