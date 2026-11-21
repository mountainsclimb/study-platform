"""试卷工坊 P1：我的题库统计 / JSON·CSV 导出 / JSON·CSV 导入（无新增依赖）。

- 统计/导出/导入全部基于应用内可写题库 ``pl_questions``，严格按 ``user_id`` 隔离，
  软删除题目（``deleted_at`` 非空）不参与统计与导出。
- 导入复用 ``bank._normalize_content`` / ``schema`` 的既有内容校验：逐条错误带行号，
  支持 ``dry_run`` 预览；按内容摘要去重（重复记为 skipped，不覆盖已有题目）。
- CSV 导出使用 UTF-8 BOM + 标准 CSV 转义（Excel 直接打开不乱码）；导出上限 5000 题，
  导入上限 10MB / 2000 题。
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import Response

from ..auth import current_user
from ..db import connection, utcnow, write_lock
from . import bank, db, schema
from .db import (
    MAX_TAGS,
    QUESTION_STATUSES,
    VERIFY_STATUSES,
    json_dump,
    json_load,
)

router = APIRouter()

MAX_EXPORT_QUESTIONS = 5000
MAX_IMPORT_BYTES = 10 * 1024 * 1024       # 单文件 ≤10MB
MAX_IMPORT_QUESTIONS = 2000               # 单次 ≤2000 题
MAX_IMPORT_ERRORS = 200                   # 返回给前端的逐行错误上限
MAX_PREVIEW_CHARS = 120                   # 统计/导出中的摘要长度

CSV_COLUMNS = [
    "type", "subject", "grade", "difficulty", "knowledge_tags", "stem",
    "options", "answers", "explanation", "rubric", "parts",
]
CSV_LABELS = {
    "type": "题型", "subject": "学科", "grade": "年级", "difficulty": "难度",
    "knowledge_tags": "知识点", "stem": "题干", "options": "选项", "answers": "答案",
    "explanation": "解析", "rubric": "评分标准", "parts": "子题摘要",
}
_OPTION_RE = re.compile(r"^\s*([A-Za-z]{1,2}|[0-9]{1,2})\s*[.、:：)]\s*(.*)$", re.S)
_ANSWERS_SPLIT_RE = re.compile(r"\s*\|\s*|\s*；\s*|\s*;\s*")


# ---------------------------------------------------------------------------
# 通用：过滤条件 / 行 → 题目
# ---------------------------------------------------------------------------

def _filter_where(subject: str | None, grade: str | None, status: str | None) -> tuple[str, list]:
    where = ["user_id=?", "deleted_at IS NULL"]
    params: list = []
    if subject and subject.strip():
        where.append("subject=?")
        params.append(subject.strip())
    if grade and grade.strip():
        where.append("grade=?")
        params.append(grade.strip())
    if status:
        if status not in QUESTION_STATUSES:
            raise HTTPException(422, "未知的题目状态")
        where.append("status=?")
        params.append(status)
    return " AND ".join(where), params


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _diff_label(value) -> str:
    if value is None or value == "":
        return ""
    try:
        return f"{float(value):g}"
    except (TypeError, ValueError):
        return ""


def _content_text(content: dict, key: str) -> str:
    value = content.get(key)
    return "" if value is None else str(value)


def _options_text(options) -> str:
    if not isinstance(options, list):
        return ""
    parts = []
    for index, option in enumerate(options):
        if not isinstance(option, dict):
            continue
        label = str(option.get("label") or chr(ord("A") + index)).strip()
        text = str(option.get("text") or "").strip()
        parts.append(f"{label}. {text}")
    return " | ".join(parts)


def _answers_text(answers, parts) -> str:
    values = [str(item).strip() for item in (answers or []) if str(item).strip()]
    if values:
        return " | ".join(values)
    if isinstance(parts, list):
        collected = []
        for part in parts:
            if not isinstance(part, dict):
                continue
            part_answers = [str(item).strip() for item in (part.get("answers") or []) if str(item).strip()]
            if part_answers:
                collected.append("/".join(part_answers))
        if collected:
            return " | ".join(collected)
    return ""


def _parts_summary(parts) -> str:
    if not isinstance(parts, list) or not parts:
        return ""
    summary = []
    for index, part in enumerate(parts, start=1):
        if not isinstance(part, dict):
            continue
        stem = str(part.get("stem") or "").strip().replace("\n", " ")[:MAX_PREVIEW_CHARS]
        answers = " / ".join(str(item).strip() for item in (part.get("answers") or []) if str(item).strip())
        summary.append({"index": index, "stem": stem, "answers": answers})
    return json.dumps(summary, ensure_ascii=False)


def _digest(question: dict) -> str:
    payload = {
        "type": question.get("type") or "SUBJECTIVE",
        "subject": str(question.get("subject") or "").strip(),
        "grade": str(question.get("grade") or "").strip(),
        "difficulty": question.get("difficulty"),
        "knowledge_tags": list(question.get("knowledge_tags") or []),
        "content": question.get("content") or {},
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _existing_digests(user_id: int) -> set[str]:
    with connection() as con:
        rows = con.execute(
            "SELECT type, subject, grade, difficulty, knowledge_tags_json, content_json "
            "FROM pl_questions WHERE user_id=? AND deleted_at IS NULL",
            (user_id,),
        ).fetchall()
    digests: set[str] = set()
    for row in rows:
        digests.add(_digest({
            "type": row["type"],
            "subject": row["subject"],
            "grade": row["grade"],
            "difficulty": row["difficulty"],
            "knowledge_tags": json_load(row["knowledge_tags_json"], []),
            "content": json_load(row["content_json"], {}),
        }))
    return digests


# ---------------------------------------------------------------------------
# 1. 题库统计
# ---------------------------------------------------------------------------

@router.get("/questions/stats")
def question_stats(
    subject: str | None = Query(default=None, max_length=40),
    grade: str | None = Query(default=None, max_length=40),
    user=Depends(current_user),
):
    """题库统计（实时聚合；用户隔离；空库返回 0，不报错）。"""
    clause, params = _filter_where(subject, grade, None)
    with connection() as con:
        rows = [dict(row) for row in con.execute(
            "SELECT type, subject, status, verify_status, difficulty, knowledge_tags_json, "
            "content_json, ai_job_id, source, created_at "
            f"FROM pl_questions WHERE {clause}",
            (user["id"], *params),
        ).fetchall()]
    by_type = {value: 0 for value in db.Q_TYPES}
    by_status = {value: 0 for value in QUESTION_STATUSES}
    by_verify = {value: 0 for value in VERIFY_STATUSES}
    subjects: dict[str, int] = {}
    knowledge: dict[str, int] = {}
    missing_answers = missing_explanation = missing_tags = ai_generated = 0
    recent_cutoff = (_now() - timedelta(days=7)).isoformat()
    recent_7d = 0
    for row in rows:
        by_type[row["type"]] = by_type.get(row["type"], 0) + 1
        by_status[row["status"]] = by_status.get(row["status"], 0) + 1
        by_verify[row["verify_status"]] = by_verify.get(row["verify_status"], 0) + 1
        subjects[row["subject"] or ""] = subjects.get(row["subject"] or "", 0) + 1
        content = json_load(row["content_json"], {})
        if not isinstance(content, dict):
            content = {}
        has_answer, _preview = db._content_answer_summary(content)
        if not has_answer:
            missing_answers += 1
        if not str(content.get("explanation") or "").strip():
            missing_explanation += 1
        tags = schema.normalize_tags(json_load(row["knowledge_tags_json"], []), limit=MAX_TAGS)
        if not tags:
            missing_tags += 1
        for tag in tags:
            knowledge[tag] = knowledge.get(tag, 0) + 1
        if row.get("ai_job_id"):
            ai_generated += 1
        if str(row.get("created_at") or "") >= recent_cutoff:
            recent_7d += 1
    top_knowledge = [
        {"tag": tag, "count": count}
        for tag, count in sorted(knowledge.items(), key=lambda pair: (-pair[1], pair[0]))[:10]
    ]
    return {
        "total": len(rows),
        "by_type": by_type,
        "by_subject": [
            {"subject": name, "count": count}
            for name, count in sorted(subjects.items(), key=lambda pair: (-pair[1], pair[0]))
        ],
        "by_status": by_status,
        "by_verify": by_verify,
        "missing_answers": missing_answers,
        "missing_explanation": missing_explanation,
        "missing_tags": missing_tags,
        "knowledge": {"total": len(knowledge), "top": top_knowledge},
        "recent_7d": recent_7d,
        "ai_generated": ai_generated,
        "subject": subject or "",
        "grade": grade or "",
    }


# ---------------------------------------------------------------------------
# 2. 题库导出
# ---------------------------------------------------------------------------

def _export_rows(user_id: int, subject, grade, status) -> list[dict]:
    clause, params = _filter_where(subject, grade, status)
    with connection() as con:
        rows = con.execute(
            f"SELECT * FROM pl_questions WHERE {clause} ORDER BY id ASC",
            (user_id, *params),
        ).fetchall()
    if len(rows) > MAX_EXPORT_QUESTIONS:
        raise HTTPException(
            422,
            f"导出题目 {len(rows)} 道，超过单次上限 {MAX_EXPORT_QUESTIONS} 道；请缩小筛选条件（学科/年级/状态）",
        )
    return [dict(row) for row in rows]


def _safe_filename_part(value: str) -> str:
    return re.sub(r'[\\/:*?"<>|\s]+', "", str(value or ""))[:24]


def _download_headers(filename: str, media_type: str) -> dict:
    return {
        "Content-Disposition": f"attachment; filename=questions; filename*=UTF-8''{quote(filename)}",
        "Cache-Control": "no-store",
    }


@router.get("/questions/export")
def export_questions(
    format: str = Query(default="json", max_length=10),
    subject: str | None = Query(default=None, max_length=40),
    grade: str | None = Query(default=None, max_length=40),
    status: str | None = Query(default=None, max_length=20),
    user=Depends(current_user),
):
    """导出题库：``format=json|csv``（CSV 带 UTF-8 BOM，Excel 可直接打开）。"""
    fmt = str(format or "json").strip().lower()
    if fmt not in ("json", "csv"):
        raise HTTPException(422, "导出格式只支持 json 或 csv")
    rows = _export_rows(user["id"], subject, grade, status)
    questions = [db.public_question(row) for row in rows]
    scope = _safe_filename_part(subject) or "全部学科"
    grade_part = _safe_filename_part(grade)
    day = _now().strftime("%Y%m%d")
    if fmt == "json":
        payload = json.dumps(questions, ensure_ascii=False, indent=2)
        filename = f"题库-{scope}{('-'+grade_part) if grade_part else ''}-{day}.json"
        return Response(
            content=payload.encode("utf-8"),
            media_type="application/json; charset=utf-8",
            headers=_download_headers(filename, "application/json"),
        )
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=CSV_COLUMNS, extrasaction="ignore")
    writer.writerow({column: CSV_LABELS[column] for column in CSV_COLUMNS})
    for question in questions:
        content = question.get("content") or {}
        writer.writerow({
            "type": question.get("type") or "SUBJECTIVE",
            "subject": question.get("subject") or "",
            "grade": question.get("grade") or "",
            "difficulty": _diff_label(question.get("difficulty")),
            "knowledge_tags": "|".join(question.get("knowledge_tags") or []),
            "stem": _content_text(content, "stem"),
            "options": _options_text(content.get("options")),
            "answers": _answers_text(content.get("answers"), content.get("parts")),
            "explanation": _content_text(content, "explanation"),
            "rubric": _content_text(content, "rubric"),
            "parts": _parts_summary(content.get("parts")),
        })
    filename = f"题库-{scope}{('-'+grade_part) if grade_part else ''}-{day}.csv"
    content = ("\ufeff" + buffer.getvalue()).encode("utf-8")
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers=_download_headers(filename, "text/csv"),
    )


# ---------------------------------------------------------------------------
# 3. 题库导入
# ---------------------------------------------------------------------------

def _parse_tags(raw) -> list[str]:
    if isinstance(raw, list):
        return schema.normalize_tags(raw, limit=MAX_TAGS)
    if raw is None:
        return []
    text = str(raw).strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return schema.normalize_tags(parsed, limit=MAX_TAGS)
        except (TypeError, ValueError):
            pass
    return schema.normalize_tags(re.split(r"[|,，、;；\n]+", text), limit=MAX_TAGS)


def _parse_options(raw) -> list[dict]:
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    text = str(raw or "").strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [item for item in parsed if isinstance(item, dict)]
        except (TypeError, ValueError):
            pass
    options: list[dict] = []
    for index, piece in enumerate(re.split(r"\s*\|\s*", text)):
        piece = piece.strip()
        if not piece:
            continue
        match = _OPTION_RE.match(piece)
        if match:
            options.append({"label": match.group(1).upper(), "text": match.group(2).strip()})
        else:
            options.append({"label": chr(ord("A") + index), "text": piece})
    return options


def _parse_answers(raw) -> list[str]:
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    text = str(raw or "").strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [str(item).strip() for item in parsed if str(item).strip()]
        except (TypeError, ValueError):
            pass
    return [piece.strip() for piece in _ANSWERS_SPLIT_RE.split(text) if piece.strip()]


def _parse_parts(raw) -> list[dict]:
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    text = str(raw or "").strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        raise ValueError("parts 不是合法 JSON 数组") from None
    if not isinstance(parsed, list):
        raise ValueError("parts 必须是 JSON 数组")
    return [item for item in parsed if isinstance(item, dict)]


def _record_to_question(raw: dict, row_no: int) -> dict:
    """把一条导入记录规范化为可入库题目；非法抛 ValueError（中文原因）。"""
    if not isinstance(raw, dict):
        raise ValueError("记录必须是 JSON 对象")
    qtype = schema.normalize_qtype(str(raw.get("type") or "SUBJECTIVE"))
    if qtype is None:
        raise ValueError(f"未知的题型：{raw.get('type')}")
    subject = str(raw.get("subject") or "").strip()
    if not subject:
        raise ValueError("缺少学科（subject）")
    if len(subject) > 40:
        raise ValueError("学科名称过长（≤40 字符）")
    grade = str(raw.get("grade") or "").strip()
    if len(grade) > 40:
        raise ValueError("年级名称过长（≤40 字符）")
    difficulty_raw = raw.get("difficulty")
    difficulty = None
    if difficulty_raw not in (None, ""):
        difficulty = schema.normalize_difficulty(difficulty_raw)
        if difficulty is None:
            raise ValueError(f"难度不合法：{difficulty_raw}")
    tags = _parse_tags(raw.get("knowledge_tags"))
    content = raw.get("content")
    if isinstance(content, dict):
        normalized_content = dict(content)
    else:
        try:
            options = _parse_options(raw.get("options"))
            answers = _parse_answers(raw.get("answers"))
            parts = _parse_parts(raw.get("parts"))
        except ValueError as exc:
            raise ValueError(str(exc)) from None
        normalized_content = {
            "stem": str(raw.get("stem") or "").strip(),
            "options": options,
            "answers": answers,
            "explanation": str(raw.get("explanation") or "").strip(),
            "rubric": str(raw.get("rubric") or "").strip(),
        }
        if parts:
            normalized_content["parts"] = parts
    if not isinstance(normalized_content.get("stem"), str) or not normalized_content["stem"].strip():
        raise ValueError("缺少题干（stem）")
    try:
        content = bank._normalize_content(normalized_content, qtype)
    except HTTPException as exc:
        raise ValueError(str(exc.detail)) from None
    question = {
        "type": qtype,
        "subject": subject,
        "grade": grade,
        "difficulty": difficulty,
        "knowledge_tags": tags,
        "content": content,
    }
    return question


def _record_error_text(exc: Exception) -> str:
    if isinstance(exc, HTTPException):
        return str(exc.detail)
    message = str(exc).strip()
    return (message or "数据不合法")[:200]


def _decode_text(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise HTTPException(422, "导入文件编码无法识别：请使用 UTF-8 或 GBK 文本")


def _parse_json_import(text: str) -> list:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise HTTPException(422, f"JSON 解析失败：第 {exc.lineno} 行 {exc.msg}") from None
    if isinstance(data, dict):
        for key in ("items", "questions", "data"):
            if isinstance(data.get(key), list):
                return data[key]
        return [data]
    if not isinstance(data, list):
        raise HTTPException(422, 'JSON 导入文件必须是题目数组（或 {"items": [...]} 对象）')
    return data


def _parse_csv_import(text: str) -> list[tuple[int, dict]]:
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise HTTPException(422, "CSV 文件为空或缺少表头")
    headers = [str(name or "").strip() for name in reader.fieldnames]
    # 支持导出时的中文表头（题型/学科/...）与英文列名两种写法。
    reverse = {CSV_LABELS.get(column): column for column in CSV_COLUMNS}
    header_map = {}
    for header, original in zip(headers, reader.fieldnames):
        key = reverse.get(header, header)
        if key in CSV_COLUMNS:
            header_map[original] = key
    missing = [column for column in ("type", "subject", "stem") if column not in header_map.values()]
    if missing:
        raise HTTPException(422, "CSV 缺少必要列：" + "、".join(CSV_LABELS[column] for column in missing))
    records: list[tuple[int, dict]] = []
    for index, row in enumerate(reader, start=2):  # 第 1 行为表头
        if not any(str(value or "").strip() for value in row.values()):
            continue
        record = {}
        for original, column in header_map.items():
            record[column] = row.get(original)
        records.append((index, record))
    return records


def _parse_import(raw: bytes, filename: str) -> tuple[list[tuple[int, dict]], str]:
    if not raw.strip():
        raise HTTPException(422, "导入文件为空")
    text = _decode_text(raw)
    stripped = text.lstrip()
    lower_name = str(filename or "").lower()
    if lower_name.endswith(".csv") or (not lower_name.endswith(".json") and not stripped.startswith(("[", "{"))):
        records = _parse_csv_import(text)
        return records, "csv"
    data = _parse_json_import(text)
    return [(index + 1, item) for index, item in enumerate(data)], "json"


@router.post("/questions/import")
async def import_questions(
    file: UploadFile = File(...),
    dry_run: bool = Form(default=False),
    user=Depends(current_user),
):
    """导入题库（JSON / CSV，≤10MB、≤2000 题）。

    ``dry_run=true`` 只做校验与去重预览、不写库；正式导入逐条写入 ``pl_questions``
    （status=draft、source=import、verify_status=pending），重复内容记 skipped。
    """
    # 有界读取：只多读 1 字节探测超限；超过上限时不把整个文件读进内存，仍返回 413。
    raw = await file.read(MAX_IMPORT_BYTES + 1)
    if len(raw) > MAX_IMPORT_BYTES:
        raise HTTPException(413, f"导入文件不能超过 {MAX_IMPORT_BYTES // (1024 * 1024)}MB")
    records, fmt = _parse_import(raw, file.filename or "")
    if not records:
        raise HTTPException(422, "导入文件没有题目记录")
    if len(records) > MAX_IMPORT_QUESTIONS:
        raise HTTPException(
            422, f"单次最多导入 {MAX_IMPORT_QUESTIONS} 道题，当前文件 {len(records)} 条；请拆分后重试",
        )
    existing = _existing_digests(user["id"])
    seen: set[str] = set()
    errors: list[dict] = []
    valid: list[dict] = []
    skipped = 0
    for row_no, record in records:
        try:
            question = _record_to_question(record, row_no)
        except (ValueError, TypeError) as exc:
            if len(errors) < MAX_IMPORT_ERRORS:
                errors.append({"row": row_no, "error": _record_error_text(exc)})
            continue
        digest = _digest(question)
        if digest in existing or digest in seen:
            skipped += 1
            continue
        seen.add(digest)
        question["_digest"] = digest
        valid.append(question)
    for question in valid:
        question.pop("_digest", None)
    result = {
        "format": fmt,
        "dry_run": bool(dry_run),
        "total": len(records),
        "valid": len(valid),
        "would_create": len(valid),
        "would_skip": skipped,
        "errors": errors,
        "errors_truncated": len(errors) >= MAX_IMPORT_ERRORS,
        "created": 0,
        "skipped": skipped,
    }
    if dry_run:
        return result
    if not valid:
        return result
    created = 0
    now = utcnow()
    with connection() as con:
        write_lock(con, "paperlab-question")
        for question in valid:
            try:
                con.execute(
                    "INSERT INTO pl_questions(user_id,source,type,subject,grade,difficulty,"
                    "knowledge_tags_json,content_json,status,verify_status,version,ai_job_id,"
                    "ai_model,prompt_version,created_at,updated_at,deleted_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL,'','',?,?,NULL)",
                    (
                        user["id"], "import", question["type"], question["subject"],
                        question["grade"], question["difficulty"],
                        json_dump(question["knowledge_tags"]),
                        json_dump(question["content"]),
                        "draft", "pending", 1, now, now,
                    ),
                )
                created += 1
            except Exception as exc:  # noqa: BLE001 - 单条失败不阻断其余题目
                if len(errors) < MAX_IMPORT_ERRORS:
                    errors.append({"row": 0, "error": "写入失败：" + _record_error_text(exc)})
    result.update({"created": created, "would_create": created, "errors": errors})
    return result


__all__ = [
    "router", "question_stats", "export_questions", "import_questions",
    "MAX_EXPORT_QUESTIONS", "MAX_IMPORT_QUESTIONS", "CSV_COLUMNS",
]
