"""本机演示数据种子：不配置任何 AI Key，也能看到「知序」整合模块的完整效果。

只通过 **HTTP 真实 API** 写入数据（不直接写数据库、不改 app 代码），所有示例数据标题/题干带
「示例」标记，可用 ``--reset`` 一键清理（按标题识别；不删除任何真实用户数据）。

创建：
1. AI 课堂：构造 importer 兼容 ZIP → ``POST /api/classroom/import`` → 「示例：光合作用互动课堂」
   （slide+quiz+pbl 三个场景，无需模型）；
2. 试卷工坊 / 我的题库：Pillow 生成示例试卷页 PNG → 上传 → 页级切题框 → 重切生成草稿 →
   改写为示例题并入库 → 建一份「示例：函数基础自测卷」（应用题库题 + 高考真题）→ 发布 →
   生成 A4 答题卡 → 创建并提交一次整卷练习（示例成绩，非 AI 判分）；
3. AI 助手：建一个空会话「示例：如何复习错题」（不调用模型；配置模型后即可对话）。

用法::

    python scripts/seed_demo.py --base-url http://127.0.0.1:8765 --register
    python scripts/seed_demo.py --base-url http://127.0.0.1:8765 --register --reset --yes
    python scripts/seed_demo.py --base-url http://127.0.0.1:8765 --json

依赖：httpx、Pillow（requirements.txt 已声明）；不访问外网（只访问 --base-url）。
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

try:
    import httpx
except ImportError:  # pragma: no cover - 环境缺依赖时给出可操作提示
    raise SystemExit("缺少 httpx，请先执行 pip install -r requirements.txt") from None

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # pragma: no cover
    raise SystemExit("缺少 Pillow，请先执行 pip install -r requirements.txt") from None

# ---------------------------------------------------------------------------
# 演示数据标识
# ---------------------------------------------------------------------------

MARKER = "示例"
COURSE_TITLE = "示例：光合作用互动课堂"
IMPORT_TITLE = "示例：函数基础自测卷页"
PAPER_TITLE = "示例：函数基础自测卷"
CARD_TITLE = "示例答题卡"
CONVERSATION_TITLE = "示例：如何复习错题"
EXAMPLE_TAGS = ["示例", "演示数据"]
MAX_LIST_PAGES = 20
REQUEST_TIMEOUT = 120.0
CJK_FONT_CANDIDATES = (
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
)


# ---------------------------------------------------------------------------
# HTTP 客户端
# ---------------------------------------------------------------------------

class DemoError(RuntimeError):
    """API 调用失败（消息可直接展示给用户）。"""

    def __init__(self, message: str, *, status: int | None = None, detail=None):
        super().__init__(message)
        self.status = status
        self.detail = detail


def _error_text(detail) -> str:
    if isinstance(detail, str):
        return detail
    if isinstance(detail, list):
        parts = []
        for item in detail:
            if isinstance(item, dict):
                loc = ".".join(str(part) for part in (item.get("loc") or []))
                parts.append((loc + "：" if loc else "") + str(item.get("msg") or item))
            else:
                parts.append(str(item))
        return "；".join(parts)
    if isinstance(detail, dict):
        return str(detail.get("message") or detail.get("msg") or detail)
    return str(detail or "")


class Api:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        # trust_env=False：本脚本只访问本机演示实例，不能被 Windows 系统代理/PAC 劫持
        # （否则 127.0.0.1 会被代理拦截成 502，CI/本机验收失败）。
        self.client = httpx.Client(base_url=self.base_url, timeout=REQUEST_TIMEOUT,
                                   follow_redirects=True, trust_env=False)

    def close(self) -> None:
        try:
            self.client.close()
        except Exception:  # noqa: BLE001
            pass

    def request(self, method: str, path: str, *, json_body=None, files=None, data=None) -> dict:
        response = self.client.request(method, path, json=json_body, files=files, data=data)
        payload = {}
        try:
            payload = response.json()
        except (ValueError, TypeError):
            payload = {}
        if response.status_code >= 400:
            detail = payload.get("detail") if isinstance(payload, dict) else None
            message = _error_text(detail) or f"HTTP {response.status_code} {path}"
            raise DemoError(message, status=response.status_code, detail=detail)
        return payload if isinstance(payload, dict) else {"data": payload}

    def get(self, path: str) -> dict:
        return self.request("GET", path)

    def post(self, path: str, body=None) -> dict:
        return self.request("POST", path, json_body=body if body is not None else {})

    def put(self, path: str, body=None) -> dict:
        return self.request("PUT", path, json_body=body if body is not None else {})

    def patch(self, path: str, body=None) -> dict:
        return self.request("PATCH", path, json_body=body if body is not None else {})

    def delete(self, path: str) -> dict:
        return self.request("DELETE", path)


def _auth(api: Api, username: str, password: str, register: bool) -> dict:
    status, payload = None, None
    response = api.client.post("/api/auth/login", json={"username": username, "password": password})
    if response.status_code == 200:
        return {"username": username, "created": False}
    if register:
        response = api.client.post("/api/auth/register",
                                   json={"username": username, "password": password})
        if response.status_code == 200:
            return {"username": username, "created": True}
    detail = ""
    try:
        detail = _error_text((response.json() or {}).get("detail"))
    except Exception:  # noqa: BLE001
        detail = response.text[:200]
    raise DemoError(f"登录失败（{response.status_code}）：{detail or '请检查用户名/密码，或加 --register 创建演示账号'}")


def _list_all(api: Api, path: str, key: str = "items", page_size: int = 50) -> list[dict]:
    items: list[dict] = []
    for page in range(1, MAX_LIST_PAGES + 1):
        separator = "&" if "?" in path else "?"
        payload = api.get(f"{path}{separator}page={page}&page_size={page_size}")
        batch = payload.get(key) or []
        items.extend([item for item in batch if isinstance(item, dict)])
        total = int(payload.get("total") or len(items))
        if not batch or len(items) >= total or len(batch) < page_size:
            break
    return items


def _has_marker(value) -> bool:
    return MARKER in str(value or "")


# ---------------------------------------------------------------------------
# AI 课堂：构造 importer 兼容 ZIP 并导入
# ---------------------------------------------------------------------------

def _slide_content() -> dict:
    return {
        "type": "slide",
        "schemaVersion": 1,
        "canvas": {
            "width": 1000.0,
            "height": 562.5,
            "theme": {
                "backgroundColor": "#ffffff",
                "themeColors": ["#1f6feb", "#e8590c", "#2f9e44", "#7048e8", "#c2255c"],
                "fontColor": "#222222",
                "fontName": "Microsoft YaHei",
            },
            "elements": [
                {
                    "id": "title_001", "type": "text", "left": 60.0, "top": 44.0,
                    "width": 880.0, "height": 76.0, "rotate": 0.0, "fontSize": 32,
                    "color": "#1f2937", "defaultFontName": "", "defaultColor": "#1f2937",
                    "lineHeight": 1.5,
                    "content": '<p style="font-size: 32px;"><strong>示例：光合作用互动课堂</strong></p>',
                    "text": "示例：光合作用互动课堂",
                },
                {
                    "id": "point_001", "type": "text", "left": 80.0, "top": 150.0,
                    "width": 500.0, "height": 150.0, "rotate": 0.0, "fontSize": 20,
                    "color": "#333333", "defaultFontName": "", "defaultColor": "#333333",
                    "lineHeight": 1.5,
                    "content": ('<p style="font-size: 20px;">• 示例数据：植物把光能转化为化学能</p>'
                                '<p style="font-size: 20px;">• 原料：水、二氧化碳</p>'
                                '<p style="font-size: 20px;">• 场所：叶绿体</p>'),
                    "text": "• 示例数据：植物把光能转化为化学能 • 原料：水、二氧化碳 • 场所：叶绿体",
                },
                {
                    "id": "shape_001", "type": "shape", "left": 640.0, "top": 150.0,
                    "width": 280.0, "height": 240.0, "rotate": 0.0,
                    "path": "M 0 0 L 1 0 L 1 1 L 0 1 Z", "viewBox": [1.0, 1.0],
                    "fill": "#e7f5ff", "fixedRatio": False,
                },
                {
                    "id": "latex_001", "type": "latex", "left": 660.0, "top": 250.0,
                    "width": 240.0, "height": 80.0, "rotate": 0.0,
                    "latex": "6CO_2 + 6H_2O \\rightarrow C_6H_{12}O_6 + 6O_2",
                    "color": "#1f6feb", "align": "center",
                },
            ],
        },
    }


def _quiz_content() -> dict:
    return {
        "type": "quiz",
        "questions": [
            {
                "id": "q1", "type": "single",
                "question": "示例：光合作用发生的场所是？",
                "options": [
                    {"label": "A", "text": "叶绿体"},
                    {"label": "B", "text": "线粒体"},
                    {"label": "C", "text": "细胞核"},
                    {"label": "D", "text": "液泡"},
                ],
                "answer": ["A"],
                "analysis": "示例解析：叶绿体含有叶绿素，是光合作用的场所。",
                "points": 10.0, "hasAnswer": True,
            },
            {
                "id": "q2", "type": "multiple",
                "question": "示例：光合作用的原料包括哪些？（多选）",
                "options": [
                    {"label": "A", "text": "水"},
                    {"label": "B", "text": "二氧化碳"},
                    {"label": "C", "text": "氧气"},
                    {"label": "D", "text": "有机物"},
                ],
                "answer": ["A", "B"],
                "analysis": "示例解析：水和二氧化碳是原料，氧气和有机物是产物。",
                "points": 10.0, "hasAnswer": True,
            },
        ],
    }


def _pbl_content() -> dict:
    return {
        "type": "pbl",
        "project": {
            "title": "示例：校园节能小项目",
            "background": ("示例数据：学校上个月电费上涨，请你调查校园里最耗电的设备，"
                           "并提出一份可执行的节能方案（本项目仅用于演示，不含真实数据）。"),
            "goals": [
                "示例目标：理解电功率与电能的关系",
                "示例目标：能用简单数据支撑一个节能决策",
                "示例目标：体验从调研到方案的完整过程",
            ],
            "roles": [{"name": "示例节能导师", "description": "陪你走完调研、方案与复盘，随时可以提问。"}],
            "milestones": [
                {
                    "title": "示例里程碑：调研校园用电",
                    "description": "记录三类设备的用电情况，找出最值得改进的一处。",
                    "tasks": ["列出教室/走廊/办公室的三类主要用电设备", "估算它们的日均耗电量"],
                    "deliverable": "示例用电调查表（三项数据 + 一个结论）",
                    "checkpoint": "数据来源清楚、计算可复核，结论能指出最大耗电项",
                },
                {
                    "title": "示例里程碑：提出并验证节能方案",
                    "description": "针对最大耗电项设计一个可执行方案，并说明预期节省。",
                    "tasks": ["写出方案的具体做法", "估算实施后的日均节电量"],
                    "deliverable": "示例一页节能方案（做法 + 预计节省）",
                    "checkpoint": "方案可执行、估算有依据，并能说明取舍",
                },
            ],
            "rubric": [
                {"criterion": "数据真实性", "levels": [
                    {"level": "优秀", "description": "数据有来源且计算无误"},
                    {"level": "合格", "description": "数据基本合理"},
                    {"level": "待改进", "description": "数据来源不明"},
                ]},
                {"criterion": "方案可行性", "levels": [
                    {"level": "优秀", "description": "可落地且有取舍分析"},
                    {"level": "合格", "description": "方案基本可执行"},
                ]},
            ],
            "mentor_hints": [
                "示例提示：先想清楚一天里哪些设备一直在耗电？",
                "示例提示：估算时可以用功率乘以使用时长。",
            ],
        },
    }


def build_classroom_zip() -> bytes:
    """构造 importer 兼容的课程 ZIP（manifest.json + course.json + scenes/*.json）。"""
    slide_actions = [
        {"id": "act_001", "type": "speech", "text": "示例旁白：我们先从生活现象出发，看看植物为什么离不开光。"},
        {"id": "act_002", "type": "spotlight", "element_id": "title_001", "dim_opacity": 0.5},
        {"id": "act_003", "type": "speech", "text": "示例旁白：植物把光能转化成化学能，原料是水和二氧化碳。"},
        {"id": "act_004", "type": "laser", "element_id": "latex_001", "color": "#ff0000"},
    ]
    quiz_actions = [{"id": "act_001", "type": "speech",
                     "text": "示例旁白：接下来做一组课堂小测，请先独立作答。"}]
    pbl_actions = [{"id": "act_001", "type": "speech",
                    "text": "示例旁白：进入项目式学习，请先读项目背景，再按里程碑推进。"}]
    scenes = [
        {"order_index": 1, "scene_type": "slide", "title": "示例：光合作用导入",
         "content": _slide_content(), "actions": slide_actions,
         "meta": {"prompt_version": "demo-seed", "placeholder": False}},
        {"order_index": 2, "scene_type": "quiz", "title": "示例：课堂小测",
         "content": _quiz_content(), "actions": quiz_actions,
         "meta": {"prompt_version": "demo-seed", "placeholder": False}},
        {"order_index": 3, "scene_type": "pbl", "title": "示例：校园节能项目",
         "content": _pbl_content(), "actions": pbl_actions,
         "meta": {"prompt_version": "demo-seed", "placeholder": False}},
    ]
    outline = {
        "title": COURSE_TITLE,
        "description": "示例课程：用于本机演示 AI 课堂的幻灯片/测验/项目式学习播放与导出，无需配置模型。",
        "language_directive": "使用简体中文授课；术语首次出现时给出中文解释。",
        "scenes": [
            {"id": "scene_1", "title": "示例：光合作用导入", "type": "slide",
             "description": "", "outline_points": ["示例要点：光能转化", "原料与场所"], "order": 1},
            {"id": "scene_2", "title": "示例：课堂小测", "type": "quiz",
             "description": "", "outline_points": ["示例要点：场所与原料"], "order": 2,
             "quiz": {"question_count": 2, "difficulty": "easy"}},
            {"id": "scene_3", "title": "示例：校园节能项目", "type": "pbl",
             "description": "", "outline_points": ["示例要点：调研与方案"], "order": 3},
        ],
        "warnings": [],
    }
    course = {
        "title": COURSE_TITLE,
        "topic": "光合作用",
        "subject": "生物",
        "grade": "初中二年级",
        "mode": "interactive",
        "description": "示例课程：演示 AI 课堂播放器、测验与项目式学习，全部内容明确标注为示例数据。",
        "requirements": "示例数据，用于本机演示；不含真实学生数据。",
        "outline": outline,
        "config": {"document_ids": [], "prompt_version": "demo-seed", "scene_count": len(scenes)},
        "cover": {},
    }
    manifest = {
        "format": "zhixu-classroom-zip",
        "formatVersion": 1,
        "app": "zhixu-study-platform",
        "exportedAt": datetime.now(timezone.utc).isoformat(),
        "courseId": 0,
        "course": {"title": COURSE_TITLE, "topic": "光合作用", "subject": "生物",
                   "grade": "初中二年级", "mode": "interactive"},
        "scenes": [],
        "resources": [],
        "capabilities": ["slide", "quiz", "pbl"],
        "promptVersion": "demo-seed",
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for index, scene in enumerate(scenes, 1):
            path = f"scenes/scene_{index:03d}.json"
            manifest["scenes"].append({
                "order": index, "id": index, "title": scene["title"],
                "type": scene["scene_type"], "path": path,
            })
            archive.writestr(path, json.dumps(scene, ensure_ascii=False, indent=2).encode("utf-8"))
        archive.writestr("manifest.json",
                         json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))
        archive.writestr("course.json",
                         json.dumps(course, ensure_ascii=False, indent=2).encode("utf-8"))
    return buffer.getvalue()


def seed_classroom(api: Api, state: dict) -> dict:
    courses = _list_all(api, "/api/classroom/courses")
    existing = next((course for course in courses if course.get("title") == COURSE_TITLE), None)
    if existing:
        state["courses"][COURSE_TITLE] = existing
        return {"status": "skipped", "course_id": existing["id"],
                "summary": f"已存在课程「{COURSE_TITLE}」，跳过导入"}
    payload = build_classroom_zip()
    result = api.request(
        "POST", "/api/classroom/import",
        files={"file": (f"{COURSE_TITLE}.zip", payload, "application/zip")},
    )
    course = result.get("course") or {}
    if not course.get("id"):
        raise DemoError("课程导入未返回 course.id")
    state["courses"][COURSE_TITLE] = course
    return {"status": "created", "course_id": course["id"],
            "summary": f"已导入课程（{result.get('scene_count') or 3} 个场景）"}


# ---------------------------------------------------------------------------
# 试卷工坊：示例页图 → 切题 → 重切 → 示例题 → 入库 → 试卷/答题卡/练习
# ---------------------------------------------------------------------------

def _load_font(size: int):
    for candidate in CJK_FONT_CANDIDATES:
        try:
            if Path(candidate).is_file():
                return ImageFont.truetype(candidate, size=size)
        except Exception:  # noqa: BLE001
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def build_example_page_png() -> bytes:
    width, height = 900, 1200
    image = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    title_font = _load_font(34)
    body_font = _load_font(25)
    small_font = _load_font(21)
    draw.rectangle([30, 30, width - 30, height - 30], outline=(210, 218, 224), width=2)
    draw.text((60, 55), "示例：函数基础自测卷页（演示数据）", font=title_font, fill=(31, 41, 55))
    draw.line([60, 110, width - 60, 110], fill=(200, 208, 214), width=2)

    draw.text((60, 150), "1. 示例：已知 f(x)=x+1，则 f(2)=______。", font=body_font, fill=(31, 41, 55))
    draw.text((90, 205), "（示例填空题，仅用于演示导入/切题/入库流程）", font=small_font, fill=(120, 130, 140))
    draw.rectangle([60, 260, width - 60, 420], outline=(220, 226, 232), width=1)

    draw.text((60, 470), "2. 示例：下列函数中，在实数集 R 上单调递增的是（  ）", font=body_font,
              fill=(31, 41, 55))
    for index, text in enumerate(("A. y = -x", "B. y = x", "C. y = 1/x", "D. y = -x²")):
        draw.text((90, 530 + index * 42), text, font=body_font, fill=(55, 65, 81))
    draw.rectangle([60, 730, width - 60, 860], outline=(220, 226, 232), width=1)

    draw.text((60, 920), "示例提示：本题为演示数据，答错也会记录到本次示例练习。", font=small_font,
              fill=(120, 130, 140))

    # 半透明水印
    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    watermark_font = _load_font(90)
    watermark = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    ImageDraw.Draw(watermark).text((120, 500), "示例数据", font=watermark_font, fill=(220, 60, 60, 30))
    watermark = watermark.rotate(-28, expand=False, center=(width / 2, height / 2))
    image = Image.alpha_composite(image.convert("RGBA"), watermark).convert("RGB")
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def _example_questions() -> list[dict]:
    return [
        {
            "type": "BLANK",
            "stem": "示例：已知 f(x)=x+1，则 f(2)=____。",
            "options": [],
            "answers": ["3"],
            "explanation": "示例解析：把 x=2 代入 f(x)=x+1，得 f(2)=3。",
            "rubric": "",
            "knowledge_tags": EXAMPLE_TAGS + ["函数"],
            "difficulty": 0.2,
            "score": 5.0,
        },
        {
            "type": "SINGLE",
            "stem": "示例：下列函数中，在实数集 R 上单调递增的是（  ）",
            "options": [
                {"label": "A", "text": "y = -x"},
                {"label": "B", "text": "y = x"},
                {"label": "C", "text": "y = 1/x"},
                {"label": "D", "text": "y = -x²"},
            ],
            "answers": ["B"],
            "explanation": "示例解析：y=x 在 R 上单调递增。",
            "rubric": "",
            "knowledge_tags": EXAMPLE_TAGS + ["函数"],
            "difficulty": 0.3,
            "score": 5.0,
        },
    ]


def _find_example_import(api: Api) -> dict | None:
    imports = _list_all(api, "/api/paperlab/imports")
    return next((row for row in imports if row.get("title") == IMPORT_TITLE), None)


def _ensure_annotation(api: Api, import_id: int) -> dict:
    annotation = api.get(f"/api/paperlab/imports/{import_id}/pages/1/annotation")
    edit = annotation.get("edit") or {}
    if int(edit.get("version") or 0) > 0:
        return {"status": "skipped", "version": edit.get("version"),
                "summary": "切题框已存在，跳过标注"}
    boxes = [
        {"id": "q1", "kind": "question", "x": 0.06, "y": 0.11, "w": 0.88, "h": 0.22,
         "label": "示例第1题"},
        {"id": "q2", "kind": "question", "x": 0.06, "y": 0.38, "w": 0.88, "h": 0.25,
         "label": "示例第2题"},
        {"id": "a1", "kind": "answer", "x": 0.12, "y": 0.44, "w": 0.76, "h": 0.08,
         "label": "示例答题区"},
    ]
    saved = api.put(f"/api/paperlab/imports/{import_id}/pages/1/annotation",
                    {"version": int(edit.get("version") or 0), "boxes": boxes})
    return {"status": "created", "version": (saved.get("edit") or {}).get("version"),
            "summary": "已写入 2 个题目框 + 1 个答题区"}


def _ensure_import_items(api: Api, import_id: int) -> dict:
    detail = api.get(f"/api/paperlab/imports/{import_id}")
    items = detail.get("items") or []
    page_items = [item for item in items if int(item.get("page_no") or 0) == 1]
    resegmented = False
    if len(page_items) < 2:
        api.post(f"/api/paperlab/imports/{import_id}/resegment", {"pages": [1]})
        resegmented = True
        detail = api.get(f"/api/paperlab/imports/{import_id}")
        items = detail.get("items") or []
        page_items = [item for item in items if int(item.get("page_no") or 0) == 1]
    return {"items_count": len(page_items), "resegmented": resegmented,
            "summary": f"重切完成：{len(page_items)} 条草稿" if resegmented
                       else "已存在重切草稿，跳过"}


def _ensure_committed_questions(api: Api, import_id: int) -> tuple[list[int], dict]:
    detail = api.get(f"/api/paperlab/imports/{import_id}")
    items = [item for item in (detail.get("items") or []) if int(item.get("page_no") or 0) == 1]
    questions = _example_questions()
    patched = 0
    for index, item in enumerate(sorted(items, key=lambda row: int(row.get("item_no") or 0))):
        if item.get("question_id"):
            continue
        if index >= len(questions):
            break
        draft = dict(item.get("draft") or {})
        sample = questions[index]
        draft.update({
            "type": sample["type"],
            "stem": sample["stem"],
            "options": sample["options"],
            "answers": sample["answers"],
            "explanation": sample["explanation"],
            "rubric": sample["rubric"],
            "knowledge_tags": sample["knowledge_tags"],
            "difficulty": sample["difficulty"],
            "score": sample["score"],
            "subject": "数学",
            "grade": "高中一年级",
        })
        api.patch(f"/api/paperlab/imports/{import_id}/items/{item['id']}",
                  {"draft": draft, "status": "accepted"})
        patched += 1
    item_ids = [int(item["id"]) for item in items
                if not item.get("question_id") and (item.get("status") in ("accepted", "pending"))]
    created_ids: list[int] = []
    if item_ids:
        result = api.post(f"/api/paperlab/imports/{import_id}/commit",
                          {"item_ids": item_ids, "target": "app_bank"})
        created_ids = [int(qid) for qid in (result.get("created_question_ids") or [])]
        detail = api.get(f"/api/paperlab/imports/{import_id}")
        items = [item for item in (detail.get("items") or []) if int(item.get("page_no") or 0) == 1]
    valid_ids: list[int] = []
    for item in items:
        question_id = item.get("question_id")
        if not question_id:
            continue
        try:
            api.get(f"/api/paperlab/questions/{int(question_id)}")
            valid_ids.append(int(question_id))
        except DemoError:
            continue
    return valid_ids, {"patched": patched, "created": created_ids,
                       "summary": f"示例题已入库（本次新建 {len(created_ids)} 题）"}


def _ensure_fallback_questions(api: Api, valid_ids: list[int]) -> tuple[list[int], dict]:
    """入库题在 reset 后可能已被软删除：优先复用可见的示例题，不足时用同一份示例内容补齐。"""
    reused = 0
    try:
        payload = api.get("/api/paperlab/questions?keyword=%E7%A4%BA%E4%BE%8B&page_size=50")
        for item in payload.get("items") or []:
            content = item.get("content") or {}
            stem = str(content.get("stem") or "")
            tags = content.get("knowledge_tags") or []
            if "示例" not in stem and not any(
                    "示例" in str(tag) or "演示数据" in str(tag) for tag in tags):
                continue
            question_id = int(item.get("id") or 0)
            if question_id and question_id not in valid_ids and len(valid_ids) < 2:
                valid_ids.append(question_id)
                reused += 1
    except DemoError:
        pass
    created = 0
    for sample in _example_questions():
        if len(valid_ids) >= 2:
            break
        result = api.post("/api/paperlab/questions", {
            "type": sample["type"],
            "subject": "数学",
            "grade": "高中一年级",
            "difficulty": sample["difficulty"],
            "knowledge_tags": sample["knowledge_tags"],
            "content": {
                "stem": sample["stem"],
                "options": sample["options"],
                "answers": sample["answers"],
                "explanation": sample["explanation"],
                "rubric": sample["rubric"],
                "parts": [],
                "images": [],
                "score": sample["score"],
            },
            "status": "ready",
        })
        question = result.get("question") or {}
        if question.get("id"):
            valid_ids.append(int(question["id"]))
            created += 1
    if created:
        summary = f"补充示例题 {created} 道"
    elif reused:
        summary = f"复用已有示例题 {reused} 道"
    else:
        summary = "示例题已存在"
    return valid_ids, {"created": created, "reused": reused, "summary": summary}


def _bank_refs(api: Api) -> tuple[list[str], str]:
    try:
        payload = api.get(
            "/api/questions?subject=%E6%95%B0%E5%AD%A6&type=single_choice"
            "&has_answer=true&page_size=2&sort=year_desc")
        refs: list[str] = []
        for item in payload.get("items") or []:
            qid = str(item.get("qid") or "").strip()
            if qid:
                refs.append(f"bank:{qid}")
        return refs[:2], "从只读高考题库取到 %d 道真题" % len(refs[:2])
    except DemoError as exc:
        return [], f"高考题库不可用（{exc}），本示例卷只使用应用题库题"


def _ensure_paper(api: Api, app_ids: list[int], bank_refs: list[str]) -> dict:
    papers = _list_all(api, "/api/paperlab/papers")
    existing = next((paper for paper in papers if paper.get("title") == PAPER_TITLE), None)
    if existing:
        paper_id = int(existing["id"])
        publish_result = {"status": "skipped"}
        if existing.get("status") != "published":
            api.post(f"/api/paperlab/papers/{paper_id}/publish", {"published": True})
            publish_result = {"status": "published"}
        return {"id": paper_id, "status": "skipped", "publish": publish_result,
                "summary": "示例试卷已存在，跳过建卷"}
    items = []
    for question_id in app_ids[:2]:
        items.append({"ref": f"app:{question_id}", "score": 5.0, "section": "一、示例题（应用题库）"})
    for ref in bank_refs:
        items.append({"ref": ref, "score": 5.0, "section": "二、高考真题（示例卷）"})
    result = api.post("/api/paperlab/papers", {
        "title": PAPER_TITLE,
        "kind": "manual",
        "subject": "数学",
        "grade": "高中一年级",
        "instructions": "示例数据：本卷用于演示试卷工坊的建卷/发布/打印/答题卡/整卷练习，不影响真实题库。",
        "items": items,
    })
    paper = result.get("paper") or {}
    if not paper.get("id"):
        raise DemoError("建卷未返回 paper.id")
    paper_id = int(paper["id"])
    api.post(f"/api/paperlab/papers/{paper_id}/publish", {"published": True})
    return {"id": paper_id, "status": "created", "publish": {"status": "published"},
            "summary": f"已建卷并发布（{len(items)} 题）"}


def _ensure_card(api: Api, paper_id: int) -> dict:
    cards = api.get(f"/api/paperlab/papers/{paper_id}/cards").get("items") or []
    if cards:
        return {"status": "skipped", "card_id": cards[0].get("id"),
                "summary": "答题卡已存在，跳过生成"}
    result = api.post(f"/api/paperlab/papers/{paper_id}/cards", {
        "preset": "A4", "columns": 1, "include_subjective": True,
        "title": CARD_TITLE,
    })
    card = result.get("card") or {}
    if not card.get("id"):
        raise DemoError("答题卡生成未返回 card.id")
    return {"status": "created", "card_id": card["id"], "summary": "已生成 A4 答题卡"}


def _demo_answers(paper_detail: dict) -> list[dict]:
    answers = []
    for index, item in enumerate(paper_detail.get("items") or [], 1):
        snapshot = item.get("snapshot") or {}
        qtype = str(snapshot.get("type") or "").upper()
        content = snapshot.get("content") or {}
        ref = str(item.get("question_ref") or "")
        if qtype in ("SINGLE", "MULTIPLE"):
            if ref.startswith("bank:"):
                answer = "A"  # 示例作答：真题不展示答案，随便选一个，演示判分/错题联动
            else:
                answer = content.get("answers") or ["A"]
        elif qtype == "BLANK":
            answer = content.get("answers") or ["示例答案"]
        else:
            answer = "示例作答：这是演示数据，未调用 AI 判分。"
        answers.append({"item_position": index, "answer": answer})
    return answers


def _ensure_attempt(api: Api, paper_id: int, paper_detail: dict) -> dict:
    attempts = api.get(f"/api/paperlab/papers/{paper_id}/attempts").get("items") or []
    submitted = [item for item in attempts if item.get("status") == "submitted"]
    if submitted:
        return {"status": "skipped", "attempt_id": submitted[0].get("id"),
                "summary": "整卷练习已提交，跳过"}
    created = api.post(f"/api/paperlab/papers/{paper_id}/attempts")
    attempt = created.get("attempt") or {}
    if not attempt.get("id"):
        raise DemoError("创建整卷练习未返回 attempt.id")
    attempt_id = int(attempt["id"])
    answers = _demo_answers(paper_detail)
    result = api.post(f"/api/paperlab/attempts/{attempt_id}/submit", {"answers": answers})
    summary = (result.get("result") or {}).get("summary") or {}
    return {"status": "created", "attempt_id": attempt_id,
            "score": summary.get("score"), "max_score": summary.get("max_score"),
            "summary": f"已提交示例练习（{summary.get('score', 0)}/{summary.get('max_score', 0)} 分）"}


def seed_paperlab(api: Api, state: dict) -> dict:
    import_row = _find_example_import(api)
    import_created = False
    if import_row is None:
        png = build_example_page_png()
        upload = api.request(
            "POST", "/api/paperlab/imports",
            files={"files": (f"{IMPORT_TITLE}.png", png, "image/png")},
            data={"title": IMPORT_TITLE, "subject": "数学", "grade": "高中一年级"},
        )
        import_row = upload.get("import") or {}
        if not import_row.get("id"):
            raise DemoError("上传示例试卷页未返回 import.id")
        import_created = True
    import_id = int(import_row["id"])
    state["paperlab"]["import_id"] = import_id

    annotation = _ensure_annotation(api, import_id)
    resegment = _ensure_import_items(api, import_id)
    app_ids, commit_info = _ensure_committed_questions(api, import_id)
    app_ids, fallback_info = _ensure_fallback_questions(api, app_ids)
    if len(app_ids) < 2:
        raise DemoError("示例题数量不足（需要至少 2 道应用题库题）")
    state["paperlab"]["app_question_ids"] = app_ids[:2]
    bank_refs, bank_note = _bank_refs(api)
    paper = _ensure_paper(api, app_ids[:2], bank_refs)
    paper_detail = api.get(f"/api/paperlab/papers/{paper['id']}")
    card = _ensure_card(api, paper["id"])
    attempt = _ensure_attempt(api, paper["id"], paper_detail)
    state["paperlab"].update({
        "paper_id": paper["id"],
        "card_id": card.get("card_id"),
        "attempt_id": attempt.get("attempt_id"),
        "bank_refs": bank_refs,
    })
    created = any(step.get("status") == "created" for step in (annotation, resegment))
    overall = "created" if (import_created or created or paper["status"] == "created") else "skipped"
    return {
        "status": overall,
        "import_id": import_id,
        "paper_id": paper["id"],
        "card_id": card.get("card_id"),
        "attempt_id": attempt.get("attempt_id"),
        "steps": {
            "import": "created" if import_created else "skipped",
            "annotation": annotation,
            "resegment": resegment,
            "commit": commit_info,
            "fallback_questions": fallback_info,
            "bank": bank_note,
            "paper": paper,
            "card": card,
            "attempt": attempt,
        },
        "summary": "示例试卷/答题卡/练习已就绪",
    }


# ---------------------------------------------------------------------------
# AI 助手：仅建空会话
# ---------------------------------------------------------------------------

def seed_assistant(api: Api, state: dict) -> dict:
    conversations = _list_all(api, "/api/assistant/conversations")
    for conversation in conversations:
        if conversation.get("title") == CONVERSATION_TITLE:
            state["assistant"]["conversation_id"] = conversation.get("id")
            return {"status": "skipped", "conversation_id": conversation.get("id"),
                    "summary": "示例会话已存在，跳过"}
    result = api.post("/api/assistant/conversations", {"title": CONVERSATION_TITLE})
    conversation = result.get("conversation") or {}
    if not conversation.get("id"):
        raise DemoError("创建助手会话未返回 conversation.id")
    state["assistant"]["conversation_id"] = conversation["id"]
    return {"status": "created", "conversation_id": conversation["id"],
            "summary": "已创建示例会话（空会话，未调用 AI）"}


# ---------------------------------------------------------------------------
# --reset 清理（只按标题识别本脚本的示例数据）
# ---------------------------------------------------------------------------

def _is_example_question(item: dict) -> bool:
    content = item.get("content") or {}
    tags = content.get("knowledge_tags") or item.get("knowledge_tags") or []
    return _has_marker(content.get("stem")) or _has_marker(item.get("stem_preview")) or \
        any(_has_marker(tag) for tag in tags)


def cleanup(api: Api) -> dict:
    deleted = {"courses": 0, "papers": 0, "questions": 0, "cards": 0, "conversations": 0}
    skipped: list[str] = []
    warnings: list[str] = []

    for course in _list_all(api, "/api/classroom/courses"):
        if _has_marker(course.get("title")):
            try:
                api.delete(f"/api/classroom/courses/{int(course['id'])}")
                deleted["courses"] += 1
            except DemoError as exc:
                warnings.append(f"删除课程 {course.get('id')} 失败：{exc}")

    paper_entries = [paper for paper in _list_all(api, "/api/paperlab/papers")
                     if _has_marker(paper.get("title"))]
    for paper in paper_entries:
        try:
            cards = api.get(f"/api/paperlab/papers/{int(paper['id'])}/cards").get("items") or []
            for card in cards:
                try:
                    api.delete(f"/api/paperlab/cards/{int(card['id'])}")
                    deleted["cards"] += 1
                except DemoError:
                    pass
            api.delete(f"/api/paperlab/papers/{int(paper['id'])}")
            deleted["papers"] += 1
        except DemoError as exc:
            warnings.append(f"删除试卷 {paper.get('id')} 失败：{exc}")

    for question in _list_all(api, "/api/paperlab/questions"):
        if not _is_example_question(question):
            continue
        try:
            api.delete(f"/api/paperlab/questions/{int(question['id'])}")
            deleted["questions"] += 1
        except DemoError as exc:
            warnings.append(f"删除题目 {question.get('id')} 失败：{exc}")

    for conversation in _list_all(api, "/api/assistant/conversations"):
        if _has_marker(conversation.get("title")):
            try:
                api.delete(f"/api/assistant/conversations/{int(conversation['id'])}")
                deleted["conversations"] += 1
            except DemoError as exc:
                warnings.append(f"删除会话 {conversation.get('id')} 失败：{exc}")

    # 导入记录没有 DELETE 接口：保留并复用；在摘要里明确说明。
    remaining_imports = [row for row in _list_all(api, "/api/paperlab/imports")
                         if _has_marker(row.get("title"))]
    if remaining_imports:
        skipped.append(
            "试卷识别导入记录（paperlab imports）目前没有删除 API，"
            f"保留 {len(remaining_imports)} 条示例导入并在重建时复用（不重复上传）："
            + "、".join(str(row.get("id")) for row in remaining_imports[:5])
        )
    return {"deleted": deleted, "warnings": warnings, "skipped": skipped}


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def _entry_urls(base_url: str, state: dict) -> dict:
    classroom_id = (state["courses"].get(COURSE_TITLE) or {}).get("id")
    paper_id = state["paperlab"].get("paper_id")
    card_id = state["paperlab"].get("card_id")
    urls = {
        "home": f"{base_url.rstrip('/')}/#/",
        "classroom": f"{base_url.rstrip('/')}/#/classroom",
        "classroom_player": (f"{base_url.rstrip('/')}/#/classroom?course={classroom_id}&view=play"
                             if classroom_id else None),
        "mybank": f"{base_url.rstrip('/')}/#/mybank",
        "paperlab": f"{base_url.rstrip('/')}/#/paperlab",
        "paper_detail": (f"{base_url.rstrip('/')}/#/paperlab?tab=papers&paper={paper_id}"
                         if paper_id else None),
        "card_svg": (f"{base_url.rstrip('/')}/api/paperlab/cards/{card_id}/svg"
                     if card_id else None),
        "insight": f"{base_url.rstrip('/')}/#/insight",
        "assistant": f"{base_url.rstrip('/')}/#/assistant",
        "tasks": f"{base_url.rstrip('/')}/#/tasks",
    }
    return {key: value for key, value in urls.items() if value}


def _print_human(summary: dict) -> None:
    print("=" * 66)
    print("知序 · 本机演示数据种子")
    print("=" * 66)
    print(f"服务地址：{summary['base_url']}")
    print(f"账号：{summary['user']['username']}" +
          ("（新创建）" if summary["user"].get("created") else "（已存在）"))
    if summary.get("cleanup"):
        deleted = summary["cleanup"].get("deleted") or {}
        print("--reset 清理：" + "，".join(f"{key} {value}" for key, value in deleted.items()))
        for warning in summary["cleanup"].get("warnings") or []:
            print(f"  ! {warning}")
        for note in summary["cleanup"].get("skipped") or []:
            print(f"  - {note}")
    for module_title, module in summary["modules"].items():
        if not module:
            continue
        print(f"\n[{module_title}] {module.get('summary') or ''}")
        for note in module.get("warnings") or []:
            print(f"  ! {note}")
    print("\n入口：")
    for name, url in (summary.get("urls") or {}).items():
        print(f"  - {name}: {url}")
    print("\n说明：所有示例数据标题/题干都带「示例」标记，不会影响真实数据；"
          "清理见 scripts/README-demo.md 或重新执行 --reset。")


def _ensure_utf8_stdio() -> None:
    """Windows 控制台/重定向默认 GBK，示例标题含中文与特殊符号时保证输出不崩溃。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:  # noqa: BLE001 - 旧环境不支持 reconfigure 时保持原样
                pass


def main(argv: list[str] | None = None) -> int:
    _ensure_utf8_stdio()
    parser = argparse.ArgumentParser(description="知序本机演示数据种子（无需 AI Key）")
    parser.add_argument("--base-url", default="http://127.0.0.1:8765",
                        help="已启动的知序服务地址（默认 http://127.0.0.1:8765）")
    parser.add_argument("--username", default="demo", help="登录账号（默认 demo）")
    parser.add_argument("--password", default="demo-pass-12345", help="登录密码（默认 demo-pass-12345）")
    parser.add_argument("--register", action="store_true",
                        help="账号不存在时自动注册演示账号（--username/--password）")
    parser.add_argument("--reset", action="store_true",
                        help="先清理本脚本创建的示例数据（按标题识别，不删真实数据）再重建")
    parser.add_argument("--yes", action="store_true", help="--reset 时跳过交互确认")
    parser.add_argument("--json", action="store_true", help="只输出 JSON 摘要（便于脚本调用）")
    args = parser.parse_args(argv)

    base_url = args.base_url.rstrip("/")
    summary: dict = {
        "base_url": base_url,
        "user": {"username": args.username, "created": False},
        "modules": {},
        "warnings": [],
    }
    api: Api | None = None
    state = {"courses": {}, "paperlab": {}, "assistant": {}}
    exit_code = 0
    try:
        api = Api(base_url)
        try:
            health = api.get("/api/health")
        except DemoError as exc:
            raise DemoError(f"无法连接 {base_url}（{exc}）。请先启动服务，例如："
                            "python -m uvicorn app.main:app --host 127.0.0.1 --port 8765") from None
        summary["health"] = {"status": health.get("status"), "bank_version": health.get("bank_version")}
        summary["user"] = _auth(api, args.username, args.password, register=args.register)

        if args.reset:
            preview = cleanup(api) if args.yes else None
            if not args.yes:
                # 先预览将要删除的数据，再确认（避免误删）。
                courses = [c for c in _list_all(api, "/api/classroom/courses") if _has_marker(c.get("title"))]
                papers = [p for p in _list_all(api, "/api/paperlab/papers") if _has_marker(p.get("title"))]
                questions = [q for q in _list_all(api, "/api/paperlab/questions") if _is_example_question(q)]
                conversations = [c for c in _list_all(api, "/api/assistant/conversations")
                                 if _has_marker(c.get("title"))]
                print("将清理以下示例数据：")
                print(f"  课程 {len(courses)} 门、试卷 {len(papers)} 张、题目 {len(questions)} 道、"
                      f"助手会话 {len(conversations)} 个（试卷导入记录无删除 API，会保留复用）")
                try:
                    answer = input("确认删除这些示例数据？输入 yes 继续：").strip().lower()
                except (EOFError, OSError):
                    answer = ""
                if answer not in ("yes", "y"):
                    print("已取消，未修改任何数据。")
                    return 0
            if preview is None:
                preview = cleanup(api)
            summary["cleanup"] = preview

        for label, key, runner in (
            ("AI 课堂", "classroom", seed_classroom),
            ("试卷工坊 / 我的题库", "paperlab", seed_paperlab),
            ("AI 助手", "assistant", seed_assistant),
        ):
            try:
                summary["modules"][label] = runner(api, state)
            except DemoError as exc:
                summary["modules"][label] = {"status": "failed", "error": str(exc),
                                             "summary": f"失败：{exc}"}
                summary["warnings"].append(f"{label} 失败：{exc}")
                exit_code = 2
            except Exception as exc:  # noqa: BLE001 - 单模块失败不影响其它模块
                summary["modules"][label] = {"status": "failed", "error": repr(exc),
                                             "summary": f"失败：{exc}"}
                summary["warnings"].append(f"{label} 失败：{exc!r}")
                exit_code = 2

        summary["urls"] = _entry_urls(base_url, state)
        summary["cleanup_command"] = (
            f"python scripts/seed_demo.py --base-url {base_url} "
            f"--username {args.username} --reset --yes"
        )
        summary["ok"] = exit_code == 0
        if args.json:
            print(json.dumps(summary, ensure_ascii=False, indent=2))
        else:
            _print_human(summary)
            if summary["warnings"]:
                print("\n警告：")
                for warning in summary["warnings"]:
                    print(f"  ! {warning}")
    except DemoError as exc:
        failure = {**summary, "ok": False, "error": str(exc)}
        if args.json:
            print(json.dumps(failure, ensure_ascii=False, indent=2))
        else:
            print(f"错误：{exc}", file=sys.stderr)
        exit_code = 2
    finally:
        if api is not None:
            api.close()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
