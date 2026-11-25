"""试卷工坊数据层：幂等建表、常量、行序列化与归属校验。

表命名前缀 ``pl_``（契约 §3.3）。所有私有表带 ``user_id``；SQL 一律用 ``?`` 占位、
JSON 存 TEXT、时间用 ``db.utcnow()`` 的 ISO-8601 字符串，保证 SQLite / PostgreSQL 双兼容。
"""
from __future__ import annotations

import json
from typing import Any

from fastapi import HTTPException

from ..db import connection, ensure_column, utcnow

# ---------------------------------------------------------------------------
# 业务常量（上限写死并给中文提示）
# ---------------------------------------------------------------------------

SUBJECTS = ["语文", "数学", "英语", "物理", "化学", "生物", "政治", "历史", "地理"]

GRADES = [
    "小学一年级", "小学二年级", "小学三年级", "小学四年级", "小学五年级", "小学六年级",
    "初中一年级", "初中二年级", "初中三年级",
    "高中一年级", "高中二年级", "高中三年级",
]

# 题型与 xl / OMP 对齐：单选、多选、填空、主观、混合（含子题）。
Q_TYPES = {
    "SINGLE": "单选题",
    "MULTIPLE": "多选题",
    "BLANK": "填空题",
    "SUBJECTIVE": "主观题",
    "MIXED": "混合题",
}
CHOICE_TYPES = frozenset({"SINGLE", "MULTIPLE"})

# 兼容模型/人工输入的中英文别名。
Q_TYPE_ALIASES = {
    "SINGLE": "SINGLE", "single": "SINGLE", "单选": "SINGLE", "单选题": "SINGLE",
    "单项选择": "SINGLE", "单项选择题": "SINGLE", "CHOICE": "SINGLE",
    "MULTIPLE": "MULTIPLE", "multiple": "MULTIPLE", "多选": "MULTIPLE",
    "多选题": "MULTIPLE", "多项选择": "MULTIPLE", "多项选择题": "MULTIPLE",
    "BLANK": "BLANK", "blank": "BLANK", "填空": "BLANK", "填空题": "BLANK",
    "FILL": "BLANK", "FILL_BLANK": "BLANK",
    "SUBJECTIVE": "SUBJECTIVE", "subjective": "SUBJECTIVE", "主观": "SUBJECTIVE",
    "主观题": "SUBJECTIVE", "解答": "SUBJECTIVE", "解答题": "SUBJECTIVE",
    "简答": "SUBJECTIVE", "简答题": "SUBJECTIVE", "FREE": "SUBJECTIVE",
    "MIXED": "MIXED", "mixed": "MIXED", "混合": "MIXED", "混合题": "MIXED",
    "复合题": "MIXED", "COMPOSITE": "MIXED",
}

DIFFICULTY_ALIASES = {
    "简单": 0.2, "容易": 0.2, "基础": 0.2, "简单题": 0.2,
    "较易": 0.35, "中等": 0.5, "中档": 0.5, "一般": 0.5, "中等题": 0.5,
    "较难": 0.75, "困难": 0.85, "难": 0.85, "难题": 0.85,
}

QUESTION_STATUSES = ("draft", "ready", "archived")
VERIFY_STATUSES = ("pending", "verified", "rejected")
ITEM_STATUSES = ("pending", "accepted", "skipped")
IMPORT_STATUSES = ("draft", "recognizing", "ready", "committed", "failed")

# 上传与任务上限：契约 §4.2 limits。
MAX_FILE_BYTES = 30 * 1024 * 1024          # 单文件 ≤ 30MB
MAX_IMPORT_PAGES = 20                      # 单次导入 ≤ 20 页
MAX_IMPORT_FILES = 20                      # 单次导入 ≤ 20 个文件
MAX_IMPORT_ITEMS = 400                     # 单次导入 ≤ 400 道题草稿
MAX_ENRICH_QUESTIONS = 50                  # 单次 AI 补全 ≤ 50 题
MAX_PAGE_SIZE = 100                        # 题库列表单页上限
REVIEW_CONFIDENCE = 0.65                   # 低于该置信度的识别结果标记待复核
MAX_PAGE_EDGE = 2400                       # 页面图最长边（像素）
MAX_PAGE_PIXELS = 40_000_000               # 单页图/上传图像素预算（防解码前像素炸弹）
PDF_RENDER_SCALE = 2.0                     # pypdfium2 渲染倍率 ≈ 144 DPI（144~200 DPI 区间）
MAX_TEXT_PAGE_CHARS = 20000                # DOCX 纯文本页上限
MAX_STEM_CHARS = 20000
MAX_OPTIONS = 12
MAX_PARTS = 20
MAX_TAGS = 30

# P2：双模型识别核验与本地 OCR
VERIFY_STATUSES = ("pending", "verified", "diff", "failed", "accepted", "skipped")
VERIFY_STEM_THRESHOLD = 0.92               # 题干归一化相似度低于该值视为不一致
MAX_OCR_PAGES = 5                          # 同步 OCR / OCR 草稿单次最多页数
OCR_SPLIT_MODES = ("blank_line", "lines")  # OCR 草稿合并方式：按空行分块 / 逐行
MAX_OCR_TEXT_CHARS = 40000                 # 单页 OCR 文本上限（防异常大文本落库）
MAX_OCR_LINES = 800                        # 单页 OCR 行数上限

PAGE_FOLDER = "paperlab/pages"
ASSET_FOLDER = "paperlab/assets"

# 页面图 / 裁切图 MIME
IMAGE_MIME = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}
ASSET_MIME = {"png": "image/png", "jpg": "image/jpeg"}


# ---------------------------------------------------------------------------
# JSON 工具
# ---------------------------------------------------------------------------

def json_load(raw: Any, default: Any = None):
    """宽松解析数据库里的 TEXT JSON；损坏/类型不符时返回默认值。"""
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

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pl_imports (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL DEFAULT '',
    grade TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'draft',
    page_count INTEGER NOT NULL DEFAULT 0,
    job_id INTEGER,
    source_json TEXT NOT NULL DEFAULT '[]',
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS pl_imports_user ON pl_imports(user_id, id);
CREATE INDEX IF NOT EXISTS pl_imports_user_status ON pl_imports(user_id, status);

CREATE TABLE IF NOT EXISTS pl_questions (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    source TEXT NOT NULL DEFAULT 'manual',
    type TEXT NOT NULL DEFAULT 'SUBJECTIVE',
    subject TEXT NOT NULL DEFAULT '',
    grade TEXT NOT NULL DEFAULT '',
    difficulty REAL,
    knowledge_tags_json TEXT NOT NULL DEFAULT '[]',
    content_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'draft',
    verify_status TEXT NOT NULL DEFAULT 'pending',
    version INTEGER NOT NULL DEFAULT 1,
    ai_job_id INTEGER,
    ai_model TEXT NOT NULL DEFAULT '',
    prompt_version TEXT NOT NULL DEFAULT '',
    content_sha256 TEXT NOT NULL DEFAULT '',
    source_ref TEXT NOT NULL DEFAULT '',
    source_kind TEXT NOT NULL DEFAULT '',
    source_question_id TEXT NOT NULL DEFAULT '',
    source_bank_version TEXT NOT NULL DEFAULT '',
    source_content_revision TEXT NOT NULL DEFAULT '',
    source_answer_revision TEXT NOT NULL DEFAULT '',
    source_meta_json TEXT NOT NULL DEFAULT '{}',
    collected_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT
);
CREATE INDEX IF NOT EXISTS pl_questions_user ON pl_questions(user_id, id);
CREATE INDEX IF NOT EXISTS pl_questions_user_subject ON pl_questions(user_id, subject);
CREATE INDEX IF NOT EXISTS pl_questions_user_grade ON pl_questions(user_id, grade);
CREATE INDEX IF NOT EXISTS pl_questions_user_verify ON pl_questions(user_id, verify_status);

CREATE TABLE IF NOT EXISTS pl_pages (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    import_id INTEGER NOT NULL REFERENCES pl_imports(id) ON DELETE CASCADE,
    page_no INTEGER NOT NULL,
    width INTEGER NOT NULL DEFAULT 0,
    height INTEGER NOT NULL DEFAULT 0,
    storage_name TEXT NOT NULL DEFAULT '',
    media_type TEXT NOT NULL DEFAULT 'image/jpeg',
    source_name TEXT NOT NULL DEFAULT '',
    text_content TEXT NOT NULL DEFAULT '',
    ocr_text TEXT NOT NULL DEFAULT '',
    ocr_lines_json TEXT NOT NULL DEFAULT '[]',
    ocr_blocks_json TEXT NOT NULL DEFAULT '[]',
    ocr_blocks_version INTEGER NOT NULL DEFAULT 0,
    ocr_engine TEXT NOT NULL DEFAULT '',
    ocr_at TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(import_id, page_no)
);
CREATE INDEX IF NOT EXISTS pl_pages_import ON pl_pages(import_id, page_no);
CREATE INDEX IF NOT EXISTS pl_pages_user ON pl_pages(user_id);

CREATE TABLE IF NOT EXISTS pl_import_items (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    import_id INTEGER NOT NULL REFERENCES pl_imports(id) ON DELETE CASCADE,
    page_no INTEGER NOT NULL,
    item_no INTEGER NOT NULL,
    bbox_json TEXT NOT NULL DEFAULT '{}',
    draft_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending',
    needs_review INTEGER NOT NULL DEFAULT 0,
    review_reason TEXT NOT NULL DEFAULT '',
    skip_reason TEXT NOT NULL DEFAULT '',
    confidence REAL,
    source TEXT NOT NULL DEFAULT 'ai',
    verify_status TEXT NOT NULL DEFAULT 'pending',
    verify_json TEXT NOT NULL DEFAULT '{}',
    diff_json TEXT NOT NULL DEFAULT '{}',
    verify_error TEXT NOT NULL DEFAULT '',
    warnings_json TEXT NOT NULL DEFAULT '[]',
    question_id INTEGER REFERENCES pl_questions(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(import_id, item_no)
);
CREATE INDEX IF NOT EXISTS pl_import_items_import ON pl_import_items(import_id, page_no, item_no);
CREATE INDEX IF NOT EXISTS pl_import_items_user ON pl_import_items(user_id, status);
CREATE INDEX IF NOT EXISTS pl_import_items_question ON pl_import_items(question_id);

-- P0：个人题图资产登记（服务端签发 asset_id；content.images[i].asset 只存 asset_id）。
-- status: active/published/disabled；public_derivative 预留给后续「平台精选」，默认 0。
CREATE TABLE IF NOT EXISTS pl_assets (
    id INTEGER PRIMARY KEY,
    asset_id TEXT NOT NULL UNIQUE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    folder TEXT NOT NULL DEFAULT 'paperlab/assets',
    storage_ref TEXT NOT NULL DEFAULT '',
    media_type TEXT NOT NULL DEFAULT 'image/png',
    sha256 TEXT NOT NULL DEFAULT '',
    bytes INTEGER NOT NULL DEFAULT 0,
    purpose TEXT NOT NULL DEFAULT 'question_image',
    status TEXT NOT NULL DEFAULT 'active',
    public_derivative INTEGER NOT NULL DEFAULT 0,
    question_id INTEGER,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS pl_assets_user ON pl_assets(user_id, id);
CREATE INDEX IF NOT EXISTS pl_assets_ref ON pl_assets(folder, storage_ref);
CREATE INDEX IF NOT EXISTS pl_assets_question ON pl_assets(question_id);

-- Phase 1 剩余（§6.2/§6.3）：个人题不可变内容版本历史。
-- 收录题库题、创建/编辑个人题、题库更新各生成一行；行内容只增不改（无修改入口）。
-- source_ref 形如 ``bank:<qid>``；content_sha256 为归一化 content JSON 的摘要。
CREATE TABLE IF NOT EXISTS pl_question_versions (
    id INTEGER PRIMARY KEY,
    question_id INTEGER NOT NULL REFERENCES pl_questions(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    origin TEXT NOT NULL DEFAULT 'manual',
    type TEXT NOT NULL DEFAULT 'SUBJECTIVE',
    subject TEXT NOT NULL DEFAULT '',
    grade TEXT NOT NULL DEFAULT '',
    difficulty REAL,
    knowledge_tags_json TEXT NOT NULL DEFAULT '[]',
    content_json TEXT NOT NULL DEFAULT '{}',
    content_sha256 TEXT NOT NULL DEFAULT '',
    asset_refs_json TEXT NOT NULL DEFAULT '[]',
    source_ref TEXT NOT NULL DEFAULT '',
    source_question_id TEXT NOT NULL DEFAULT '',
    source_bank_version TEXT NOT NULL DEFAULT '',
    source_content_revision TEXT NOT NULL DEFAULT '',
    source_answer_revision TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(question_id, version)
);
CREATE INDEX IF NOT EXISTS pl_question_versions_question ON pl_question_versions(question_id, version);
CREATE INDEX IF NOT EXISTS pl_question_versions_user ON pl_question_versions(user_id, question_id);
CREATE INDEX IF NOT EXISTS pl_question_versions_source ON pl_question_versions(source_ref);
"""


# P2 增量列：老库通过 ensure_column 幂等补齐（SQLite / PostgreSQL 双兼容）。
_PAGE_EXTRA_COLUMNS = (
    ("ocr_text", "TEXT NOT NULL DEFAULT ''"),
    ("ocr_lines_json", "TEXT NOT NULL DEFAULT '[]'"),
    ("ocr_blocks_json", "TEXT NOT NULL DEFAULT '[]'"),
    ("ocr_blocks_version", "INTEGER NOT NULL DEFAULT 0"),
    ("ocr_engine", "TEXT NOT NULL DEFAULT ''"),
    ("ocr_at", "TEXT"),
)
_ITEM_EXTRA_COLUMNS = (
    ("verify_status", "TEXT NOT NULL DEFAULT 'pending'"),
    ("verify_json", "TEXT NOT NULL DEFAULT '{}'"),
    ("diff_json", "TEXT NOT NULL DEFAULT '{}'"),
    ("verify_error", "TEXT NOT NULL DEFAULT ''"),
)

# Phase 1 剩余（§6.2/§6.3）：个人收录来源元数据。老库经 ensure_column 增量补齐，
# 不 drop / 重建 pl_questions；新库同时在 CREATE TABLE 里直接带上（见 _SCHEMA）。
_QUESTION_EXTRA_COLUMNS = (
    ("content_sha256", "TEXT NOT NULL DEFAULT ''"),
    ("source_ref", "TEXT NOT NULL DEFAULT ''"),
    ("source_kind", "TEXT NOT NULL DEFAULT ''"),
    ("source_question_id", "TEXT NOT NULL DEFAULT ''"),
    ("source_bank_version", "TEXT NOT NULL DEFAULT ''"),
    ("source_content_revision", "TEXT NOT NULL DEFAULT ''"),
    ("source_answer_revision", "TEXT NOT NULL DEFAULT ''"),
    ("source_meta_json", "TEXT NOT NULL DEFAULT '{}'"),
    ("collected_at", "TEXT"),
)


def init_tables() -> None:
    """幂等建表：应用启动时由 ``init_paperlab()`` 调用一次（每次启动可重复执行）。

    P2 增量列（``pl_pages.ocr_*`` / ``pl_import_items.verify_*``）与 Phase 1 的来源列
    （``pl_questions.source_*`` / ``content_sha256`` / ``collected_at``）同时写进 CREATE TABLE
    （新库直接带上）并用 ``ensure_column`` 给老库补列，重复执行无副作用。
    """
    with connection() as con:
        con.executescript(_SCHEMA)
        for table, columns in (("pl_pages", _PAGE_EXTRA_COLUMNS),
                               ("pl_import_items", _ITEM_EXTRA_COLUMNS),
                               ("pl_questions", _QUESTION_EXTRA_COLUMNS)):
            for column, definition in columns:
                ensure_column(con, table, column, definition)
        # 来源列补完后才能建索引（老库在 executescript 阶段还没有 source_ref 列）。
        con.execute(
            "CREATE INDEX IF NOT EXISTS pl_questions_user_source "
            "ON pl_questions(user_id, source_ref)"
        )


# ---------------------------------------------------------------------------
# 行读取（带归属校验，失败抛中文 HTTPException）
# ---------------------------------------------------------------------------

def _row_dict(row) -> dict:
    if isinstance(row, dict):
        return row
    try:
        return dict(row)
    except Exception:
        return {}


def fetch_import(import_id: int, user_id: int) -> dict:
    with connection() as con:
        row = con.execute(
            "SELECT * FROM pl_imports WHERE id=? AND user_id=?", (import_id, user_id)
        ).fetchone()
    if row is None:
        raise HTTPException(404, "未找到该导入记录")
    return _row_dict(row)


def find_import(import_id: int, user_id: int) -> dict | None:
    with connection() as con:
        row = con.execute(
            "SELECT * FROM pl_imports WHERE id=? AND user_id=?", (import_id, user_id)
        ).fetchone()
    return _row_dict(row) if row is not None else None


def list_pages(import_id: int, user_id: int) -> list[dict]:
    with connection() as con:
        rows = con.execute(
            "SELECT * FROM pl_pages WHERE import_id=? AND user_id=? ORDER BY page_no",
            (import_id, user_id),
        ).fetchall()
    return [_row_dict(row) for row in rows]


def fetch_page(import_id: int, page_no: int, user_id: int) -> dict:
    with connection() as con:
        row = con.execute(
            "SELECT * FROM pl_pages WHERE import_id=? AND page_no=? AND user_id=?",
            (import_id, page_no, user_id),
        ).fetchone()
    if row is None:
        raise HTTPException(404, "未找到该页")
    return _row_dict(row)


def list_items(import_id: int, user_id: int) -> list[dict]:
    with connection() as con:
        rows = con.execute(
            "SELECT * FROM pl_import_items WHERE import_id=? AND user_id=? ORDER BY item_no",
            (import_id, user_id),
        ).fetchall()
    return [_row_dict(row) for row in rows]


def fetch_item(import_id: int, item_id: int, user_id: int) -> dict:
    with connection() as con:
        row = con.execute(
            "SELECT * FROM pl_import_items WHERE id=? AND import_id=? AND user_id=?",
            (item_id, import_id, user_id),
        ).fetchone()
    if row is None:
        raise HTTPException(404, "未找到该题目草稿")
    return _row_dict(row)


def fetch_question(question_id: int, user_id: int) -> dict:
    with connection() as con:
        row = con.execute(
            "SELECT * FROM pl_questions WHERE id=? AND user_id=? AND deleted_at IS NULL",
            (question_id, user_id),
        ).fetchone()
    if row is None:
        raise HTTPException(404, "未找到该题目")
    return _row_dict(row)


def find_question(question_id: int, user_id: int) -> dict | None:
    with connection() as con:
        row = con.execute(
            "SELECT * FROM pl_questions WHERE id=? AND user_id=? AND deleted_at IS NULL",
            (question_id, user_id),
        ).fetchone()
    return _row_dict(row) if row is not None else None


# ---------------------------------------------------------------------------
# 序列化（对外 JSON 形状；契约 §4.2，只增不改）
# ---------------------------------------------------------------------------

def page_image_url(import_id: int, page_no: int) -> str:
    return f"/api/paperlab/imports/{int(import_id)}/pages/{int(page_no)}/image"


def question_images_url(question_id: int, index: int) -> str:
    return f"/api/paperlab/questions/{int(question_id)}/images/{int(index)}"


def _content_answer_summary(content: dict) -> tuple[bool, str]:
    answers = content.get("answers")
    has_answer = isinstance(answers, list) and any(str(item).strip() for item in answers)
    parts = content.get("parts")
    if not has_answer and isinstance(parts, list):
        for part in parts:
            part_answers = part.get("answers") if isinstance(part, dict) else None
            if isinstance(part_answers, list) and any(str(item).strip() for item in part_answers):
                has_answer = True
                break
    stem = str(content.get("stem") or "").strip()
    preview = stem[:80] + ("…" if len(stem) > 80 else "")
    return has_answer, preview


def public_question(row) -> dict:
    row = _row_dict(row)
    content = json_load(row.get("content_json"), {})
    if not isinstance(content, dict):
        content = {}
    question_id = int(row["id"])
    images = content.get("images")
    if isinstance(images, list):
        for index, image in enumerate(images):
            if isinstance(image, dict) and image.get("asset") and not image.get("url"):
                image["url"] = question_images_url(question_id, index)
    has_answer, preview = _content_answer_summary(content)
    tags = json_load(row.get("knowledge_tags_json"), [])
    if not isinstance(tags, list):
        tags = []
    source_meta = json_load(row.get("source_meta_json"), {})
    if not isinstance(source_meta, dict):
        source_meta = {}
    warnings = source_meta.get("warnings")
    if not isinstance(warnings, list):
        warnings = []
    source = row.get("source") or "manual"
    source_ref = row.get("source_ref") or ""
    source_kind = row.get("source_kind") or source
    if not row.get("source_kind") and source_ref.startswith("bank:"):
        source_kind = "bank"
    return {
        "id": question_id,
        "type": row.get("type") or "SUBJECTIVE",
        "subject": row.get("subject") or "",
        "grade": row.get("grade") or "",
        "difficulty": row.get("difficulty"),
        "knowledge_tags": [str(tag) for tag in tags if str(tag).strip()],
        "content": content,
        "status": row.get("status") or "draft",
        "verify_status": row.get("verify_status") or "pending",
        "version": int(row.get("version") or 1),
        "ai_job_id": row.get("ai_job_id"),
        "ai_model": row.get("ai_model") or "",
        "prompt_version": row.get("prompt_version") or "",
        "source": source,
        "has_answer": has_answer,
        "stem_preview": preview,
        # 个人收录来源与版本元数据（只增不改，供「有新版」徽标/版本弹层使用）。
        "source_kind": source_kind,
        "source_ref": source_ref,
        "source_question_id": row.get("source_question_id") or "",
        "source_bank_version": row.get("source_bank_version") or "",
        "source_content_revision": row.get("source_content_revision") or "",
        "source_answer_revision": row.get("source_answer_revision") or "",
        "collected_at": row.get("collected_at") or None,
        "content_sha256": row.get("content_sha256") or "",
        "collect_warnings": [str(item) for item in warnings if str(item).strip()],
        # 实际值由 versions.annotate_source_flags 按题库实时状态批量填充；None=未知。
        "has_new_version": None,
        "source_available": None,
        "created_at": row.get("created_at") or "",
        "updated_at": row.get("updated_at") or "",
    }


def public_item(row) -> dict:
    row = _row_dict(row)
    draft = json_load(row.get("draft_json"), {})
    if not isinstance(draft, dict):
        draft = {}
    warnings = json_load(row.get("warnings_json"), [])
    if not isinstance(warnings, list):
        warnings = []
    bbox = json_load(row.get("bbox_json"), {})
    if not isinstance(bbox, dict):
        bbox = {}
    verify = json_load(row.get("verify_json"), {})
    if not isinstance(verify, dict):
        verify = {}
    diff = json_load(row.get("diff_json"), {})
    if not isinstance(diff, dict):
        diff = {}
    verify_status = str(row.get("verify_status") or "pending")
    return {
        "id": int(row["id"]),
        "import_id": int(row["import_id"]),
        "page_no": int(row.get("page_no") or 0),
        "item_no": int(row.get("item_no") or 0),
        "bbox": bbox,
        "draft": draft,
        "status": row.get("status") or "pending",
        "needs_review": bool(row.get("needs_review")),
        "review_reason": row.get("review_reason") or "",
        "skip_reason": row.get("skip_reason") or "",
        "confidence": row.get("confidence"),
        "source": row.get("source") or "ai",
        "verify_status": verify_status if verify_status in VERIFY_STATUSES else "pending",
        "verify": verify,
        "diff": diff,
        "verify_error": row.get("verify_error") or "",
        "warnings": [str(w) for w in warnings],
        "question_id": row.get("question_id"),
        "updated_at": row.get("updated_at") or "",
    }


def public_page(row, import_id: int) -> dict:
    row = _row_dict(row)
    page_no = int(row.get("page_no") or 0)
    storage_name = str(row.get("storage_name") or "")
    ocr_text = str(row.get("ocr_text") or "").strip()
    ocr_lines = json_load(row.get("ocr_lines_json"), [])
    if not isinstance(ocr_lines, list):
        ocr_lines = []
    ocr_blocks = json_load(row.get("ocr_blocks_json"), [])
    if not isinstance(ocr_blocks, list):
        ocr_blocks = []
    return {
        "page_no": page_no,
        "width": int(row.get("width") or 0),
        "height": int(row.get("height") or 0),
        "image_url": page_image_url(import_id, page_no) if storage_name else None,
        "media_type": row.get("media_type") or "",
        "has_text": bool(str(row.get("text_content") or "").strip()),
        "status": row.get("status") or "pending",
        "has_ocr": bool(ocr_text),
        "ocr_engine": row.get("ocr_engine") or "",
        "ocr_at": row.get("ocr_at") or "",
        "ocr_chars": len(ocr_text),
        "ocr_line_count": len(ocr_lines),
        "ocr_preview": (ocr_text[:120] + "…") if len(ocr_text) > 120 else ocr_text,
        # 公式/表格后处理需要的块；没有块时不出现该键（旧页面 JSON 保持不变）。
        **({
            "has_blocks": True,
            "ocr_block_count": len(ocr_blocks),
            "ocr_blocks_version": int(row.get("ocr_blocks_version") or 0),
        } if ocr_blocks else {}),
    }


def public_import(row) -> dict:
    row = _row_dict(row)
    source = json_load(row.get("source_json"), [])
    if not isinstance(source, list):
        source = []
    return {
        "id": int(row["id"]),
        "title": row.get("title") or "",
        "subject": row.get("subject") or "",
        "grade": row.get("grade") or "",
        "status": row.get("status") or "draft",
        "page_count": int(row.get("page_count") or 0),
        "job_id": row.get("job_id"),
        "source": source,
        "note": row.get("note") or "",
        "created_at": row.get("created_at") or "",
        "updated_at": row.get("updated_at") or "",
    }


def touch_import(con, import_id: int, user_id: int, *, status: str | None = None,
                 job_id: int | None = None) -> None:
    """更新导入记录的更新时间（可选状态 / 任务号）；调用方负责事务。"""
    sets = ["updated_at=?"]
    params: list[Any] = [utcnow()]
    if status is not None:
        sets.append("status=?")
        params.append(status)
    if job_id is not None:
        sets.append("job_id=?")
        params.append(int(job_id))
    params.extend([import_id, user_id])
    con.execute(
        f"UPDATE pl_imports SET {', '.join(sets)} WHERE id=? AND user_id=?", tuple(params)
    )
