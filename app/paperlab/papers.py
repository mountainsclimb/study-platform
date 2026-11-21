"""试卷工坊 P1：试卷与题目快照、手工建卷、发布/排序、以及组卷/巩固卷共用的快照工具。

契约见 `INTEGRATION-CONTRACT.md` §4.2 与 recon `oh-my-paper.md` §2.3/§2.6：
- 一张试卷 = ``pl_papers``（元数据 + 版本 CAS + 发布状态）+ 有序 ``pl_paper_items``
  （``question_ref`` = ``app:ID`` 应用题库题目 / ``bank:qid`` 只读高考题库题目）。
- 建卷时把题目内容**冻结**进 ``snapshot_json``：题干/选项/答案/解析/知识点/配图引用。
  之后题库题目被修改/删除不影响历史卷（recon §2.3 paper.save）。
- 应用题库题目（``pl_questions``）走本模块的题库表；只读高考题库题目用
  ``app.practice.load_question`` 取可信后端投影（含答案，仅服务端快照使用，不回传前端）。
- 表结构在 ``ensure_tables()`` 幂等创建（P1 表；P0 的 ``db.init_tables`` 不动）。
"""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from .. import question_ref
from ..auth import current_user
from ..db import connection, utcnow, write_lock
from . import db, schema
from .db import json_dump, json_load

router = APIRouter()

# ---------------------------------------------------------------------------
# 常量与表结构（P1）
# ---------------------------------------------------------------------------

PAPER_KINDS = ("manual", "smart", "knowledge", "wrong", "layered", "replay", "consolidate", "exam")
PAPER_STATUSES = ("draft", "ready", "published")
MANUAL_CREATE_KINDS = ("manual", "exam")
MAX_PAPER_ITEMS = 60
MAX_PAPER_TITLE = 120
MAX_INSTRUCTIONS = 2000
MAX_PAPER_PAGE_SIZE = 50
MAX_SECTION_CHARS = 80

# 题目分值：题型默认分（题库本身没有分值字段时使用，前端可逐题覆盖）。
DEFAULT_SCORES = {"SINGLE": 2.0, "MULTIPLE": 4.0, "BLANK": 2.0, "SUBJECTIVE": 10.0, "MIXED": 10.0}
DEFAULT_SCORE = 5.0

# 只读高考题库题型 → 试卷工坊题型（与 paperlab.db.Q_TYPES 对齐）。
BANK_TYPE_MAP = {
    "single_choice": "SINGLE",
    "multi_choice": "MULTIPLE",
    "fill_blank": "BLANK",
    "calculation": "SUBJECTIVE",
    "short_answer": "SUBJECTIVE",
    "sub_short_answer": "SUBJECTIVE",
    "essay": "SUBJECTIVE",
    "material_group": "MIXED",
    "unknown": "SUBJECTIVE",
}
# 只读题库里不可直接选题的父题（材料题父题只作为组级上下文）。
BANK_EXCLUDED_TYPES = frozenset({"material_group"})

_TYPE_LABELS = {
    "SINGLE": "单选题", "MULTIPLE": "多选题", "BLANK": "填空题",
    "SUBJECTIVE": "主观题", "MIXED": "混合题",
}

LAYOUT_DEFAULTS = {
    "paper": "A4", "columns": 1, "font_size": 10.5, "line_height": 1.6,
    "answer_space_mm": 25, "margin_mm": 18, "page_break_before": False,
}

_PAPER_SCHEMA = """
CREATE TABLE IF NOT EXISTS pl_papers (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'manual',
    subject TEXT NOT NULL DEFAULT '',
    grade TEXT NOT NULL DEFAULT '',
    instructions TEXT NOT NULL DEFAULT '',
    layout_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'draft',
    published_at TEXT,
    source TEXT NOT NULL DEFAULT 'manual',
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS pl_papers_user ON pl_papers(user_id, id);
CREATE INDEX IF NOT EXISTS pl_papers_user_subject ON pl_papers(user_id, subject);
CREATE INDEX IF NOT EXISTS pl_papers_user_status ON pl_papers(user_id, status);

CREATE TABLE IF NOT EXISTS pl_paper_items (
    id INTEGER PRIMARY KEY,
    paper_id INTEGER NOT NULL REFERENCES pl_papers(id) ON DELETE CASCADE,
    position INTEGER NOT NULL DEFAULT 0,
    section TEXT NOT NULL DEFAULT '',
    question_ref TEXT NOT NULL,
    snapshot_json TEXT NOT NULL DEFAULT '{}',
    score REAL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS pl_paper_items_paper ON pl_paper_items(paper_id, position);
CREATE INDEX IF NOT EXISTS pl_paper_items_ref ON pl_paper_items(question_ref);
"""


def ensure_tables() -> None:
    """幂等建 P1 表（``init_paperlab()`` 每次启动调用）。"""
    with connection() as con:
        con.executescript(_PAPER_SCHEMA)


def _practice():
    """延迟导入 practice（只读题库工具；避免模块加载期的循环依赖）。"""
    from .. import practice
    return practice


# ---------------------------------------------------------------------------
# 题目引用（ref）与快照
# ---------------------------------------------------------------------------

def split_ref(ref) -> tuple[str, str]:
    """解析 ``app:ID`` / ``bank:qid`` / ``official:public_id``；格式非法抛 422。"""
    text = str(ref or "").strip()
    source, sep, value = text.partition(":")
    source = source.strip().lower()
    value = value.strip()
    if not sep or source not in ("app", "bank", "official") or not value:
        raise HTTPException(
            422,
            "题目引用格式应为 app:ID（应用题库）、bank:qid（高考题库）"
            "或 official:public_id（平台精选）",
        )
    if len(text) > 340:
        raise HTTPException(422, "题目引用过长")
    if source == "app":
        question_id = schema.safe_int(value)
        if question_id is None or question_id < 1:
            raise HTTPException(422, "应用题库题目编号不正确")
        return "app", str(question_id)
    if source == "official":
        # 平台精选公开题号（plz-...）：格式校验在读取适配层统一（非法 422，未发布/撤回在快照时 404）。
        from .. import official as official_module

        if not official_module.normalize_public_id(value):
            raise HTTPException(422, "平台精选公开题号不正确")
        return "official", value
    if len(value) > 300 or "\n" in value:
        raise HTTPException(422, "高考题库题目编号不正确")
    return "bank", value


def _deepcopy(value):
    try:
        return json.loads(json.dumps(value, ensure_ascii=False))
    except (TypeError, ValueError):
        return value


def _clean_text(value, limit: int | None = None) -> str:
    text = str(value or "").replace("\r\n", "\n").strip()
    if limit and len(text) > limit:
        text = text[:limit]
    return text


def _normalize_options(raw) -> list[dict]:
    options: list[dict] = []
    if not isinstance(raw, list):
        return options
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        label = _clean_text(item.get("label"), 8) or chr(ord("A") + index)
        options.append({
            "label": label,
            "text": _clean_text(item.get("text"), 4000),
            "_blocks": item.get("blocks") if isinstance(item.get("blocks"), list) else None,
        })
    return options


def _question_kp_names(con, qids: list[str]) -> dict[str, list[str]]:
    """qid → 知识点名称（应用库 question_kp 关联，按权重降序）。"""
    result: dict[str, list[str]] = {qid: [] for qid in qids}
    if not qids:
        return result
    for chunk in _chunks(qids, 900):
        marks = ",".join("?" for _ in chunk)
        rows = con.execute(
            "SELECT qk.qid, kp.name FROM question_kp qk "
            "JOIN knowledge_points kp ON kp.id=qk.kp_id "
            f"WHERE qk.qid IN ({marks}) ORDER BY qk.weight DESC, kp.id",
            chunk,
        ).fetchall()
        for row in rows:
            result.setdefault(row[0], []).append(str(row[1]))
    return {qid: list(dict.fromkeys(names)) for qid, names in result.items()}


def _chunks(values, size: int = 900):
    for start in range(0, len(values), size):
        yield values[start:start + size]


def _difficulty_of_label(label) -> float | None:
    from .db import DIFFICULTY_ALIASES
    value = str(label or "").strip()
    if not value or value in ("待标注", "unknown"):
        return None
    if value in DIFFICULTY_ALIASES:
        return float(DIFFICULTY_ALIASES[value])
    return None


def bank_difficulty_map(qids: list[str], *, with_stats: bool = True) -> dict[str, dict]:
    """只读题库题目的数值难度：管理员标注 → AI 预估 → 全站统计（≥20 次已判定作答）。

    返回 ``{qid: {"difficulty": float|None, "source": str, "accuracy": float|None}}``。
    统计口径与 ``practice._stat_difficulty`` 一致（真实数据，不编造难度）。
    """
    wanted = [qid for qid in dict.fromkeys(q for q in qids if q)]
    result: dict[str, dict] = {
        qid: {"difficulty": None, "source": "未标注", "accuracy": None} for qid in wanted
    }
    if not wanted:
        return result
    with connection() as con:
        meta_rows = []
        for chunk in _chunks(wanted):
            marks = ",".join("?" for _ in chunk)
            meta_rows.extend(con.execute(
                "SELECT qid, difficulty, ai_difficulty, ai_difficulty_score FROM question_metadata "
                f"WHERE qid IN ({marks})", chunk,
            ).fetchall())
        stats: dict[str, tuple[int, int]] = {}
        if with_stats:
            for chunk in _chunks(wanted):
                marks = ",".join("?" for _ in chunk)
                for row in con.execute(
                    "SELECT qid, COUNT(*) AS total, SUM(CASE WHEN result='correct' THEN 1 ELSE 0 END) AS ok "
                    f"FROM attempts WHERE qid IN ({marks}) AND result!='pending' GROUP BY qid",
                    chunk,
                ):
                    stats[str(row[0])] = (int(row[1] or 0), int(row[2] or 0))
    for row in meta_rows:
        qid = str(row["qid"])
        if qid not in result:
            continue
        numeric = _difficulty_of_label(row["difficulty"])
        accuracy = None
        if numeric is not None:
            result[qid].update({"difficulty": numeric, "source": "管理员标注"})
            continue
        ai_numeric = _difficulty_of_label(row["ai_difficulty"])
        if ai_numeric is None and row["ai_difficulty_score"] is not None:
            try:
                ai_numeric = max(0.0, min(1.0, float(row["ai_difficulty_score"])))
            except (TypeError, ValueError):
                ai_numeric = None
        if ai_numeric is not None:
            result[qid].update({"difficulty": ai_numeric, "source": "AI 预估"})
    for qid, (total, ok) in stats.items():
        accuracy = round(100.0 * ok / total, 1) if total else None
        if qid in result and result[qid]["difficulty"] is None:
            result[qid]["accuracy"] = accuracy
            if total >= 20:
                ratio = ok / total
                label = 0.2 if ratio >= 0.75 else 0.5 if ratio >= 0.45 else 0.85
                result[qid].update({"difficulty": label, "source": "全站统计"})
        elif qid in result:
            result[qid]["accuracy"] = accuracy
    return result


def bank_question_cards(qids: list[str]) -> dict[str, dict]:
    """批量读取只读题库题目的候选元信息（不加载完整题目，避免 N 次读库）。"""
    wanted = [qid for qid in dict.fromkeys(q for q in qids if q)]
    if not wanted:
        return {}
    practice = _practice()
    cards: dict[str, dict] = {}
    with practice._bank() as con:
        table = practice._bank_table(con, "questions")
        for chunk in _chunks(wanted):
            marks = ",".join("?" for _ in chunk)
            rows = con.execute(
                f"SELECT qid, subject, type, stem_plain, practice_status, grading_status, bank_version "
                f"FROM {table} WHERE qid IN ({marks})", chunk,
            ).fetchall()
            for row in rows:
                raw_type = str(row["type"] or "")
                if raw_type in BANK_EXCLUDED_TYPES:
                    continue
                cards[str(row["qid"])] = {
                    "qid": str(row["qid"]),
                    "subject": str(row["subject"] or ""),
                    "bank_type": raw_type,
                    "type": BANK_TYPE_MAP.get(raw_type, "SUBJECTIVE"),
                    "stem_preview": _plain_stem(row["stem_plain"], 160),
                    "practice_status": str(row["practice_status"] or "practice"),
                    "grading_status": str(row["grading_status"] or "auto"),
                    "bank_version": str(row["bank_version"] or ""),
                }
    with connection() as con:
        kp_map = _question_kp_names(con, list(cards))
    diffs = bank_difficulty_map(list(cards))
    for qid, card in cards.items():
        card["knowledge_tags"] = kp_map.get(qid, [])
        difficulty = diffs.get(qid) or {}
        card["difficulty"] = difficulty.get("difficulty")
        card["difficulty_source"] = difficulty.get("source") or "未标注"
        card["stat_accuracy"] = difficulty.get("accuracy")
        card["score"] = DEFAULT_SCORES.get(card["type"], DEFAULT_SCORE)
    return cards


def _plain_stem(value, limit: int = 160) -> str:
    text = str(value or "")
    text = text.replace("\r", " ").replace("\n", " ")
    while "  " in text:
        text = text.replace("  ", " ")
    text = text.strip()
    return text[:limit] + ("…" if len(text) > limit else "")


def _question_kp_names_for_single(qid: str) -> list[str]:
    with connection() as con:
        return _question_kp_names(con, [qid]).get(qid, [])


def snapshot_app(user_id: int, question_id: int) -> dict:
    """冻结应用题库（``pl_questions``）题目为试卷快照。"""
    row = db.fetch_question(question_id, user_id)  # 404 归属校验
    content = json_load(row.get("content_json"), {})
    if not isinstance(content, dict):
        content = {}
    content = _deepcopy(content)
    qtype = str(row.get("type") or "SUBJECTIVE")
    content["options"] = _normalize_options(content.get("options"))
    answers = content.get("answers") if isinstance(content.get("answers"), list) else []
    parts = content.get("parts") if isinstance(content.get("parts"), list) else []
    images = []
    for index, image in enumerate(content.get("images") or []):
        if not isinstance(image, dict):
            continue
        images.append({
            "kind": "app", "index": index,
            "role": image.get("role") or "figure",
            "url": db.question_images_url(question_id, index),
            "asset": image.get("asset"),
            "media_type": image.get("media_type") or "image/png",
            "bbox": image.get("bbox"),
        })
    score = content.get("score")
    try:
        score = float(score) if score is not None else None
    except (TypeError, ValueError):
        score = None
    snapshot = {
        "ref": f"app:{int(question_id)}",
        "source": "app",
        "source_id": int(question_id),
        "subject": str(row.get("subject") or ""),
        "grade": str(row.get("grade") or ""),
        "type": qtype,
        "difficulty": row.get("difficulty"),
        "knowledge_tags": [
            str(tag) for tag in (json_load(row.get("knowledge_tags_json"), []) or []) if str(tag).strip()
        ],
        "score": score if score is not None else DEFAULT_SCORES.get(qtype, DEFAULT_SCORE),
        "verify_status": str(row.get("verify_status") or "pending"),
        "question_version": int(row.get("version") or 1),
        "content": {
            "stem": _clean_text(content.get("stem")),
            "options": [
                {key: value for key, value in option.items() if key != "_blocks"}
                for option in content["options"]
            ],
            "answers": [str(answer) for answer in answers if str(answer).strip()],
            "explanation": _clean_text(content.get("explanation")),
            "rubric": _clean_text(content.get("rubric")),
            "parts": parts,
            "images": images,
        },
        "frozen_at": utcnow(),
    }
    return snapshot


def snapshot_bank(qid: str) -> dict:
    """冻结只读高考题库题目为试卷快照（服务端可信投影，含答案；不直接回传前端）。"""
    practice = _practice()
    question = practice.load_question(qid)  # 不存在 → 404；隔离/不可作答 → 409
    if question.get("quarantined"):
        raise HTTPException(409, "该题已隔离待复核，不能加入试卷")
    if not question.get("practice_allowed"):
        raise HTTPException(409, "该题仅为上下文材料或暂不可作答，不能加入试卷")
    bank_type = str(question.get("type") or "")
    qtype = BANK_TYPE_MAP.get(bank_type, "SUBJECTIVE")
    options = [
        {"label": _clean_text(option.get("label"), 8), "text": _clean_text(option.get("text"), 4000)}
        for option in (question.get("options") or []) if isinstance(option, dict)
    ]
    images = []
    for image in question.get("images") or []:
        if not isinstance(image, dict):
            continue
        images.append({
            "kind": "bank",
            "subject": str(question.get("subject") or ""),
            "file": image.get("file"),
            "role": image.get("role") or "figure",
            "sha1": image.get("sha1"),
            "w": image.get("w"), "h": image.get("h"),
        })
    group = question.get("group") if isinstance(question.get("group"), dict) else None
    material = None
    if group:
        material = {
            "text": _clean_text(group.get("material_text") or group.get("parent_stem")),
            "images": [
                {
                    "kind": "bank",
                    "subject": str(question.get("subject") or ""),
                    "file": image.get("file"),
                    "role": "material",
                }
                for image in (group.get("material_images") or []) if isinstance(image, dict)
            ],
        }
    answer = question.get("answer") if isinstance(question.get("answer"), dict) else {}
    answer_text = _clean_text(answer.get("text"))
    solution = _clean_text(answer.get("solution"))
    difficulty = bank_difficulty_map([qid]).get(qid) or {}
    tags = _question_kp_names_for_single(qid)
    snapshot = {
        "ref": f"bank:{qid}",
        "source": "bank",
        "source_id": qid,
        "subject": str(question.get("subject") or ""),
        "grade": "",
        "type": qtype,
        "difficulty": difficulty.get("difficulty"),
        "difficulty_source": difficulty.get("source") or "未标注",
        "knowledge_tags": tags,
        "score": DEFAULT_SCORES.get(qtype, DEFAULT_SCORE),
        "bank_version": str(question.get("bank_version") or ""),
        "content": {
            "stem": _clean_text(question.get("stem_plain")),
            "options": options,
            "answers": [answer_text] if answer_text else [],
            "answer_text": answer_text,
            "answer_kind": answer.get("kind"),
            "explanation": solution,
            "rubric": "",
            "parts": [],
            "images": images,
            "material": material,
        },
        "origin": {
            "year": question.get("year"),
            "province": question.get("province"),
            "paper_name": question.get("paper_name"),
            "number_label": question.get("number_label"),
        },
        "frozen_at": utcnow(),
    }
    return snapshot


def _clean_official_parts(raw) -> list[dict]:
    """平台精选快照的子题（MIXED）：只保留判分/展示需要的白名单字段。"""
    parts: list[dict] = []
    if not isinstance(raw, list):
        return parts
    for index, part in enumerate(raw[:20], start=1):
        if not isinstance(part, dict):
            continue
        parts.append({
            "index": int(part.get("index") or index),
            "type": str(part.get("type") or "SUBJECTIVE").upper(),
            "stem": _clean_text(part.get("stem")),
            "options": [
                {"label": _clean_text(option.get("label"), 8), "text": _clean_text(option.get("text"), 4000)}
                for option in (part.get("options") or []) if isinstance(option, dict)
            ],
            "answers": [
                str(answer).strip() for answer in (part.get("answers") or []) if str(answer).strip()
            ],
            "explanation": _clean_text(part.get("explanation")),
            "rubric": _clean_text(part.get("rubric")),
        })
    return parts


def snapshot_official(public_id: str) -> dict:
    """冻结平台精选公开题为试卷快照（查询时可见性校验；撤回/争议后不可再建卷）。

    冻结内容 + ``public_id`` + 发布版本（``published_version``）+ 审核状态/来源声明；
    之后精选题被撤回/修订也不影响已建好的试卷与已开始的练习（工坊冻结语义不变），
    公开配图按 ``public_asset_id`` 记录，读取时仍按广场公开状态实时授权。
    """
    from .. import official as official_module

    question = official_module.load_official_question(public_id)  # 不可见 → 404
    raw_type = str(question.get("app_type") or "SUBJECTIVE").upper()
    qtype = raw_type if raw_type in ("SINGLE", "MULTIPLE", "BLANK", "SUBJECTIVE", "MIXED") \
        else "SUBJECTIVE"
    content_src = question.get("app_content") if isinstance(question.get("app_content"), dict) else {}
    options = [
        {"label": _clean_text(option.get("label"), 8), "text": _clean_text(option.get("text"), 4000)}
        for option in (question.get("options") or []) if isinstance(option, dict)
    ]
    answers = [
        str(answer).strip() for answer in (content_src.get("answers") or []) if str(answer).strip()
    ]
    answer_text = "；".join(answers)
    images = []
    for image in question.get("images") or []:
        if not isinstance(image, dict):
            continue
        images.append({
            "kind": "official",
            "index": image.get("index"),
            "role": image.get("role") or "figure",
            "alt": image.get("alt") or "",
            "url": image.get("url") or "",
            "public_asset_id": image.get("asset") or "",
            "media_type": image.get("media_type") or "",
            "accessible": bool(image.get("accessible")),
            "reason": image.get("reason") or "",
        })
    published_version = int(question.get("published_version") or 1)
    snapshot = {
        "ref": f"official:{public_id}",
        "source": "official",
        "source_id": public_id,
        "public_id": public_id,
        "published_version": published_version,
        "review_status": question.get("review_status") or "",
        "review_status_label": question.get("review_status_label") or "",
        "featured": bool(question.get("featured")),
        "community_pending": bool(question.get("community_pending")),
        "notice": question.get("notice") or "",
        "subject": str(question.get("subject") or ""),
        "grade": str(question.get("grade") or ""),
        "type": qtype,
        "difficulty": question.get("difficulty"),
        "difficulty_source": "平台精选",
        "knowledge_tags": [
            str(tag) for tag in (question.get("knowledge_tags") or []) if str(tag).strip()
        ],
        "score": DEFAULT_SCORES.get(qtype, DEFAULT_SCORE),
        "bank_version": "",
        "content": {
            "stem": _clean_text(question.get("stem_plain")),
            "options": options,
            "answers": answers,
            "answer_text": answer_text,
            "answer_kind": "reference",
            "explanation": _clean_text(question.get("solution")),
            "rubric": _clean_text(content_src.get("rubric")),
            "parts": _clean_official_parts(question.get("parts")),
            "images": images,
        },
        "source_declaration": question.get("source_declaration") or {},
        "origin": {"public_id": public_id, "published_version": published_version},
        "frozen_at": utcnow(),
    }
    return snapshot


def snapshot_for_ref(user_id: int, ref: str) -> dict:
    source, value = split_ref(ref)
    if source == "app":
        return snapshot_app(user_id, int(value))
    if source == "official":
        return snapshot_official(value)
    return snapshot_bank(value)


def apply_score(snapshot: dict, score) -> dict:
    result = dict(snapshot)
    if score is None:
        result["score"] = float(result.get("score") or DEFAULT_SCORE)
    else:
        try:
            numeric = float(score)
        except (TypeError, ValueError):
            raise HTTPException(422, "题目分值必须是数字") from None
        if numeric < 0 or numeric > 100:
            raise HTTPException(422, "题目分值应在 0–100 之间")
        result["score"] = round(numeric, 2)
    return result


# ---------------------------------------------------------------------------
# 卷面布局
# ---------------------------------------------------------------------------

def normalize_layout(raw) -> dict:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise HTTPException(422, "卷面 layout 必须是 JSON 对象")
    layout = dict(LAYOUT_DEFAULTS)
    paper = str(raw.get("paper") or layout["paper"]).upper()
    if paper not in ("A4", "A3"):
        raise HTTPException(422, "纸张只支持 A4 或 A3")
    layout["paper"] = paper
    for key, minimum, maximum in (
        ("columns", 1, 4), ("font_size", 8, 18), ("line_height", 1.0, 3.0),
        ("answer_space_mm", 0, 80), ("margin_mm", 5, 40),
    ):
        if raw.get(key) is None:
            continue
        try:
            value = float(raw[key])
        except (TypeError, ValueError):
            raise HTTPException(422, f"卷面参数 {key} 必须是数字") from None
        if value < minimum or value > maximum:
            raise HTTPException(422, f"卷面参数 {key} 超出范围（{minimum}–{maximum}）")
        layout[key] = int(value) if key == "columns" else value
    if raw.get("page_break_before") is not None:
        layout["page_break_before"] = bool(raw["page_break_before"])
    return layout


# ---------------------------------------------------------------------------
# 建卷 / 读取 / 序列化
# ---------------------------------------------------------------------------

class PaperItemInput(BaseModel):
    ref: str = Field(min_length=3, max_length=340)
    score: float | None = Field(default=None, ge=0, le=100)
    section: str = Field(default="", max_length=MAX_SECTION_CHARS)


def _normalize_items(user_id: int, raw_items, *, allow_prebuilt: bool = False) -> list[dict]:
    items: list[dict] = []
    seen: set[str] = set()
    if not isinstance(raw_items, list):
        raise HTTPException(422, "试卷题目列表格式不正确")
    if len(raw_items) > MAX_PAPER_ITEMS:
        raise HTTPException(422, f"单张试卷最多 {MAX_PAPER_ITEMS} 道题")
    for raw in raw_items:
        if not isinstance(raw, dict):
            raise HTTPException(422, "试卷题目格式不正确")
        ref = str(raw.get("ref") or "").strip()
        split_ref(ref)
        if ref in seen:
            raise HTTPException(422, "同一道题不能重复加入试卷")
        seen.add(ref)
        snapshot = raw.get("snapshot") if allow_prebuilt and isinstance(raw.get("snapshot"), dict) else None
        if snapshot is None:
            snapshot = snapshot_for_ref(user_id, ref)
        if str(snapshot.get("ref") or "") != ref:
            snapshot = dict(snapshot)
            snapshot["ref"] = ref
        snapshot = apply_score(snapshot, raw.get("score"))
        section = _clean_text(raw.get("section"), MAX_SECTION_CHARS)
        items.append({"ref": ref, "snapshot": snapshot, "score": float(snapshot["score"]), "section": section})
    return items


def _insert_paper(con, *, user_id: int, title: str, kind: str, subject: str, grade: str,
                  instructions: str, layout: dict, status: str, source: str) -> int:
    now = utcnow()
    return int(con.execute(
        "INSERT INTO pl_papers(user_id,title,kind,subject,grade,instructions,layout_json,"
        "status,published_at,source,version,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,NULL,?,1,?,?)",
        (
            user_id, title, kind, subject, grade, instructions, json_dump(layout),
            status, source, now, now,
        ),
    ).lastrowid)


def _insert_items(con, paper_id: int, items: list[dict]) -> None:
    now = utcnow()
    for position, item in enumerate(items):
        con.execute(
            "INSERT INTO pl_paper_items(paper_id,position,section,question_ref,snapshot_json,score,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                paper_id, position, item.get("section") or "", item["ref"],
                json_dump(item["snapshot"]), float(item["score"]), now,
            ),
        )


def create_paper(
    user_id: int,
    *,
    title: str,
    kind: str,
    subject: str,
    grade: str = "",
    instructions: str = "",
    layout: dict | None = None,
    status: str = "draft",
    source: str = "manual",
    items: list[dict],
) -> dict:
    """事务内创建试卷与冻结快照；``items`` 需已含 ``ref/snapshot/score/section``。"""
    title = _clean_text(title, MAX_PAPER_TITLE)
    if not title:
        raise HTTPException(422, "请填写试卷标题")
    if kind not in PAPER_KINDS:
        raise HTTPException(422, "未知的试卷类型")
    if status not in PAPER_STATUSES:
        raise HTTPException(422, "未知的试卷状态")
    subject = _clean_text(subject, 40)
    if not subject:
        raise HTTPException(422, "请选择学科")
    grade = _clean_text(grade, 40)
    instructions = _clean_text(instructions, MAX_INSTRUCTIONS)
    layout = normalize_layout(layout)
    if len(items) > MAX_PAPER_ITEMS:
        raise HTTPException(422, f"单张试卷最多 {MAX_PAPER_ITEMS} 道题")
    now = utcnow()
    with connection() as con:
        write_lock(con, "paperlab-paper")
        paper_id = _insert_paper(
            con, user_id=user_id, title=title, kind=kind, subject=subject, grade=grade,
            instructions=instructions, layout=layout, status=status, source=source,
        )
        _insert_items(con, paper_id, items)
        con.execute(
            "UPDATE pl_papers SET updated_at=? WHERE id=?", (now, paper_id)
        )
    return fetch_paper(paper_id, user_id)


def find_paper(paper_id: int, user_id: int) -> dict | None:
    with connection() as con:
        row = con.execute(
            "SELECT * FROM pl_papers WHERE id=? AND user_id=?", (paper_id, user_id)
        ).fetchone()
    return dict(row) if row is not None else None


def fetch_paper(paper_id: int, user_id: int) -> dict:
    paper = find_paper(paper_id, user_id)
    if paper is None:
        raise HTTPException(404, "未找到该试卷")
    return paper


def find_paper_by_source(user_id: int, source: str) -> dict | None:
    with connection() as con:
        row = con.execute(
            "SELECT * FROM pl_papers WHERE user_id=? AND source=? ORDER BY id DESC LIMIT 1",
            (user_id, source),
        ).fetchone()
    return dict(row) if row is not None else None


def list_items(paper_id: int, user_id: int) -> list[dict]:
    with connection() as con:
        rows = con.execute(
            "SELECT * FROM pl_paper_items WHERE paper_id=? "
            "AND paper_id IN (SELECT id FROM pl_papers WHERE id=? AND user_id=?) "
            "ORDER BY position, id",
            (paper_id, paper_id, user_id),
        ).fetchall()
    return [dict(row) for row in rows]


def _item_has_answer(snapshot: dict) -> bool:
    content = snapshot.get("content") if isinstance(snapshot.get("content"), dict) else {}
    answers = content.get("answers")
    if isinstance(answers, list) and any(str(item).strip() for item in answers):
        return True
    if str(content.get("answer_text") or "").strip():
        return True
    parts = content.get("parts")
    if isinstance(parts, list):
        for part in parts:
            part_answers = part.get("answers") if isinstance(part, dict) else None
            if isinstance(part_answers, list) and any(str(item).strip() for item in part_answers):
                return True
    return False


def public_item(row, *, include_snapshot: bool = True) -> dict:
    row = dict(row)
    snapshot = json_load(row.get("snapshot_json"), {})
    if not isinstance(snapshot, dict):
        snapshot = {}
    payload = {
        "id": int(row["id"]),
        "paper_id": int(row["paper_id"]),
        "position": int(row.get("position") or 0),
        "section": row.get("section") or "",
        "question_ref": row.get("question_ref") or "",
        "score": row.get("score"),
        "source": snapshot.get("source"),
        "source_label": question_ref.SOURCE_LABELS.get(
            str(snapshot.get("source") or ""), str(snapshot.get("source") or ""),
        ),
        # 平台精选来源标注：公开题号/审核状态/发布版本随卷面回看（撤回后仍标注来源，不丢历史）。
        "public_id": snapshot.get("public_id") or "",
        "review_status": snapshot.get("review_status") or "",
        "review_status_label": snapshot.get("review_status_label") or "",
        "published_version": snapshot.get("published_version"),
        "type": snapshot.get("type") or "SUBJECTIVE",
        "type_label": _TYPE_LABELS.get(snapshot.get("type") or "", "题目"),
        "subject": snapshot.get("subject") or "",
        "difficulty": snapshot.get("difficulty"),
        "knowledge_tags": list(snapshot.get("knowledge_tags") or []),
        "stem_preview": _plain_stem((snapshot.get("content") or {}).get("stem"), 90),
        "has_answer": _item_has_answer(snapshot),
        "selection_reason": snapshot.get("selection_reason") or "",
        "created_at": row.get("created_at") or "",
    }
    if include_snapshot:
        payload["snapshot"] = snapshot
    return payload


def public_paper(row, *, item_count: int | None = None, total_score: float | None = None) -> dict:
    row = dict(row)
    layout = json_load(row.get("layout_json"), {})
    return {
        "id": int(row["id"]),
        "title": row.get("title") or "",
        "kind": row.get("kind") or "manual",
        "subject": row.get("subject") or "",
        "grade": row.get("grade") or "",
        "instructions": row.get("instructions") or "",
        "layout": layout if isinstance(layout, dict) else {},
        "status": row.get("status") or "draft",
        "published_at": row.get("published_at"),
        "source": row.get("source") or "manual",
        "version": int(row.get("version") or 1),
        "item_count": int(item_count if item_count is not None else row.get("item_count") or 0),
        "total_score": float(total_score if total_score is not None else row.get("total_score") or 0),
        "created_at": row.get("created_at") or "",
        "updated_at": row.get("updated_at") or "",
    }


def paper_detail(paper_id: int, user_id: int, *, include_snapshot: bool = True) -> dict:
    paper = fetch_paper(paper_id, user_id)
    items = list_items(paper_id, user_id)
    total = round(sum(float(item.get("score") or 0) for item in items), 2)
    return {
        "paper": public_paper(paper, item_count=len(items), total_score=total),
        "items": [public_item(item, include_snapshot=include_snapshot) for item in items],
    }


# ---------------------------------------------------------------------------
# HTTP：试卷 CRUD / 发布 / 排序
# ---------------------------------------------------------------------------

class PaperCreateInput(BaseModel):
    title: str = Field(min_length=1, max_length=MAX_PAPER_TITLE)
    kind: str = Field(default="manual", max_length=20)
    subject: str = Field(min_length=1, max_length=40)
    grade: str = Field(default="", max_length=40)
    instructions: str = Field(default="", max_length=MAX_INSTRUCTIONS)
    layout: dict | None = None
    items: list[PaperItemInput] = Field(default_factory=list, max_length=MAX_PAPER_ITEMS)


class PaperPatchInput(BaseModel):
    version: int = Field(ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=MAX_PAPER_TITLE)
    grade: str | None = Field(default=None, max_length=40)
    instructions: str | None = Field(default=None, max_length=MAX_INSTRUCTIONS)
    layout: dict | None = None
    items: list[PaperItemInput] | None = Field(default=None, max_length=MAX_PAPER_ITEMS)


class PaperReorderInput(BaseModel):
    version: int = Field(ge=1)
    item_ids: list[int] = Field(min_length=1, max_length=MAX_PAPER_ITEMS)


class PaperPublishInput(BaseModel):
    published: bool = True


def _summary_rows(con, where: str, params: tuple, page: int, page_size: int) -> tuple[int, list]:
    total = int(con.execute(f"SELECT COUNT(*) FROM pl_papers p {where}", params).fetchone()[0])
    rows = con.execute(
        f"SELECT p.*, "
        "(SELECT COUNT(*) FROM pl_paper_items i WHERE i.paper_id=p.id) AS item_count, "
        "(SELECT COALESCE(SUM(i.score),0) FROM pl_paper_items i WHERE i.paper_id=p.id) AS total_score "
        f"FROM pl_papers p {where} ORDER BY p.updated_at DESC, p.id DESC LIMIT ? OFFSET ?",
        (*params, page_size, (page - 1) * page_size),
    ).fetchall()
    return total, rows


@router.get("/papers")
def list_papers(
    subject: str | None = Query(default=None, max_length=40),
    kind: str | None = Query(default=None, max_length=20),
    status: str | None = Query(default=None, max_length=20),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=MAX_PAPER_PAGE_SIZE),
    user=Depends(current_user),
):
    """试卷列表（不含题目快照，便于前端列表页）。"""
    where = "WHERE p.user_id=?"
    params: list[Any] = [user["id"]]
    if subject:
        where += " AND p.subject=?"
        params.append(subject.strip())
    if kind:
        if kind not in PAPER_KINDS:
            raise HTTPException(422, "未知的试卷类型")
        where += " AND p.kind=?"
        params.append(kind)
    if status:
        if status not in PAPER_STATUSES:
            raise HTTPException(422, "未知的试卷状态")
        where += " AND p.status=?"
        params.append(status)
    with connection() as con:
        total, rows = _summary_rows(con, where, tuple(params), page, page_size)
    return {
        "items": [public_paper(row, item_count=row["item_count"], total_score=row["total_score"]) for row in rows],
        "total": total, "page": page, "page_size": page_size,
    }


@router.post("/papers")
def create_manual_paper(data: PaperCreateInput, user=Depends(current_user)):
    """手工建卷：逐题快照冻结；items 为空时创建草稿卷。"""
    if data.kind not in MANUAL_CREATE_KINDS:
        raise HTTPException(422, "手工建卷类型只支持 manual 或 exam")
    items = _normalize_items(user["id"], [item.model_dump() for item in data.items])
    paper = create_paper(
        user["id"], title=data.title, kind=data.kind, subject=data.subject,
        grade=data.grade, instructions=data.instructions, layout=data.layout,
        source="manual", status="draft", items=items,
    )
    return paper_detail(paper["id"], user["id"])


@router.get("/papers/{paper_id}")
def get_paper(paper_id: int, include_snapshot: bool = True, user=Depends(current_user)):
    return paper_detail(paper_id, user["id"], include_snapshot=include_snapshot)


def _replace_items(con, paper_id: int, user_id: int, items: list[dict]) -> None:
    con.execute("DELETE FROM pl_paper_items WHERE paper_id=?", (paper_id,))
    _insert_items(con, paper_id, items)


@router.patch("/papers/{paper_id}")
def patch_paper(paper_id: int, data: PaperPatchInput, user=Depends(current_user)):
    """修改试卷（标题/年级/说明/版式/题目）；``version`` CAS，已发布试卷只读。"""
    with connection() as con:
        write_lock(con, "paperlab-paper")
        row = con.execute(
            "SELECT * FROM pl_papers WHERE id=? AND user_id=?", (paper_id, user["id"])
        ).fetchone()
        if row is None:
            raise HTTPException(404, "未找到该试卷")
        if (row["status"] or "") == "published":
            raise HTTPException(409, "试卷已发布，不能修改结构与内容；请先取消发布")
        current_version = int(row["version"] or 1)
        if int(data.version) != current_version:
            raise HTTPException(409, "试卷已被修改（版本冲突），请刷新后重试")
        sets: list[str] = []
        params: list[Any] = []
        if data.title is not None:
            title = _clean_text(data.title, MAX_PAPER_TITLE)
            if not title:
                raise HTTPException(422, "请填写试卷标题")
            sets.append("title=?")
            params.append(title)
        if data.grade is not None:
            sets.append("grade=?")
            params.append(_clean_text(data.grade, 40))
        if data.instructions is not None:
            sets.append("instructions=?")
            params.append(_clean_text(data.instructions, MAX_INSTRUCTIONS))
        if data.layout is not None:
            existing = json_load(row["layout_json"], {})
            merged = dict(existing) if isinstance(existing, dict) else {}
            merged.update(data.layout if isinstance(data.layout, dict) else {})
            sets.append("layout_json=?")
            params.append(json_dump(normalize_layout(merged)))
        if data.items is not None:
            items = _normalize_items(user["id"], [item.model_dump() for item in data.items])
            _replace_items(con, paper_id, user["id"], items)
        if not sets and data.items is None:
            raise HTTPException(422, "没有需要修改的字段")
        sets.append("version=?")
        params.append(current_version + 1)
        sets.append("updated_at=?")
        params.append(utcnow())
        params.extend([paper_id, user["id"], current_version])
        changed = con.execute(
            "UPDATE pl_papers SET " + ", ".join(sets) +
            " WHERE id=? AND user_id=? AND version=? AND status!='published'",
            tuple(params),
        ).rowcount
        if not changed:
            raise HTTPException(409, "试卷已被修改（版本冲突），请刷新后重试")
    return paper_detail(paper_id, user["id"])


@router.delete("/papers/{paper_id}")
def delete_paper(paper_id: int, user=Depends(current_user)):
    with connection() as con:
        write_lock(con, "paperlab-paper")
        row = con.execute(
            "SELECT id FROM pl_papers WHERE id=? AND user_id=?", (paper_id, user["id"])
        ).fetchone()
        if row is None:
            raise HTTPException(404, "未找到该试卷")
        # 练习记录保留（pl_paper_attempts.paper_id 无级联外键）：删卷后仍可按冻结快照回看。
        attempts_kept = int(con.execute(
            "SELECT COUNT(*) FROM pl_paper_attempts WHERE paper_id=? AND user_id=?",
            (paper_id, user["id"]),
        ).fetchone()[0])
        con.execute("DELETE FROM pl_paper_items WHERE paper_id=?", (paper_id,))
        con.execute("DELETE FROM pl_papers WHERE id=? AND user_id=?", (paper_id, user["id"]))
    return {"deleted": True, "paper_id": paper_id, "attempts_kept": attempts_kept}


@router.post("/papers/{paper_id}/items/reorder")
def reorder_items(paper_id: int, data: PaperReorderInput, user=Depends(current_user)):
    """按 item_ids 顺序重排题目（version CAS；已发布 409）。"""
    item_ids = list(dict.fromkeys(int(item_id) for item_id in data.item_ids))
    with connection() as con:
        write_lock(con, "paperlab-paper")
        row = con.execute(
            "SELECT * FROM pl_papers WHERE id=? AND user_id=?", (paper_id, user["id"])
        ).fetchone()
        if row is None:
            raise HTTPException(404, "未找到该试卷")
        if (row["status"] or "") == "published":
            raise HTTPException(409, "试卷已发布，不能调整题目顺序；请先取消发布")
        current_version = int(row["version"] or 1)
        if int(data.version) != current_version:
            raise HTTPException(409, "试卷已被修改（版本冲突），请刷新后重试")
        existing = [int(item[0]) for item in con.execute(
            "SELECT id FROM pl_paper_items WHERE paper_id=?", (paper_id,)
        ).fetchall()]
        if set(item_ids) != set(existing):
            raise HTTPException(422, "排序列表必须包含试卷中的全部题目，且不能包含其它题目")
        now = utcnow()
        for position, item_id in enumerate(item_ids):
            con.execute(
                "UPDATE pl_paper_items SET position=? WHERE id=? AND paper_id=?",
                (position, item_id, paper_id),
            )
        con.execute(
            "UPDATE pl_papers SET version=?, updated_at=? WHERE id=? AND user_id=? AND version=?",
            (current_version + 1, now, paper_id, user["id"], current_version),
        )
    return paper_detail(paper_id, user["id"])


@router.post("/papers/{paper_id}/publish")
def publish_paper(paper_id: int, data: PaperPublishInput, user=Depends(current_user)):
    """发布/取消发布：发布后试卷结构与快照锁定（PATCH/reorder 409）。"""
    with connection() as con:
        write_lock(con, "paperlab-paper")
        row = con.execute(
            "SELECT * FROM pl_papers WHERE id=? AND user_id=?", (paper_id, user["id"])
        ).fetchone()
        if row is None:
            raise HTTPException(404, "未找到该试卷")
        current_version = int(row["version"] or 1)
        now = utcnow()
        if data.published:
            count = int(con.execute(
                "SELECT COUNT(*) FROM pl_paper_items WHERE paper_id=?", (paper_id,)
            ).fetchone()[0])
            if count == 0:
                raise HTTPException(422, "试卷还没有题目，不能发布")
            con.execute(
                "UPDATE pl_papers SET status='published', published_at=?, version=?, updated_at=? "
                "WHERE id=? AND user_id=?",
                (now, current_version + 1, now, paper_id, user["id"]),
            )
        else:
            con.execute(
                "UPDATE pl_papers SET status='ready', published_at=NULL, version=?, updated_at=? "
                "WHERE id=? AND user_id=?",
                (current_version + 1, now, paper_id, user["id"]),
            )
    return paper_detail(paper_id, user["id"])


__all__ = [
    "router", "ensure_tables", "split_ref", "snapshot_for_ref", "snapshot_app", "snapshot_bank",
    "bank_question_cards", "bank_difficulty_map", "create_paper", "fetch_paper", "find_paper",
    "find_paper_by_source", "paper_detail", "public_paper", "public_item", "list_items",
    "normalize_layout", "apply_score", "DEFAULT_SCORES", "DEFAULT_SCORE", "BANK_TYPE_MAP",
    "PAPER_KINDS", "PAPER_STATUSES", "MAX_PAPER_ITEMS",
]
