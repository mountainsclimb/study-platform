"""试卷工坊：上传导入（图片 / PDF / DOCX）→ 页图 → 视觉模型逐页结构化识别草稿。

流程照 Oh My Paper 的 IMPORT（recon §2.1）：上传落页图 → 任务队列逐页识别 →
草稿写入 ``pl_import_items``（人工核验前不落题库）→ 人工编辑/跳过 → 批量 commit 入库。

任务执行器契约（``register_task_handler``）：支持 SSE 阶段进度、取消、checkpoint 断点续跑；
单页识别失败只记录该页，不阻塞整批。
"""
from __future__ import annotations

import asyncio
from contextlib import closing
from io import BytesIO
import hashlib
import importlib
import inspect
from pathlib import Path
import re

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi import status as http_status
from pydantic import BaseModel, Field

from .. import storage
from ..auth import current_user
from ..content_refs import MAX_IMAGE_BYTES, image_part
from ..db import connection, utcnow, write_lock
from . import assets, db, schema
from . import prompts
from .db import (
    IMPORT_STATUSES,
    ITEM_STATUSES,
    MAX_FILE_BYTES,
    MAX_IMPORT_FILES,
    MAX_IMPORT_ITEMS,
    MAX_IMPORT_PAGES,
    MAX_OCR_LINES,
    MAX_OCR_PAGES,
    MAX_OCR_TEXT_CHARS,
    MAX_PAGE_EDGE,
    MAX_PAGE_PIXELS,
    MAX_STEM_CHARS,
    MAX_TEXT_PAGE_CHARS,
    OCR_SPLIT_MODES,
    PAGE_FOLDER,
    PDF_RENDER_SCALE,
    REVIEW_CONFIDENCE,
    VERIFY_STEM_THRESHOLD,
    json_dump,
    json_load,
)

router = APIRouter()

ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
ALLOWED_EXTENSIONS = ALLOWED_IMAGE_EXTENSIONS | {".pdf", ".docx"}
IMAGE_MEDIA_TYPES = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
}

# 双模型核验 diff.flag → 中文原因（落 review_reason / 前端提示共用）
VERIFY_FLAG_LABELS = {
    "stem_mismatch": "题干不一致",
    "options_mismatch": "选项不一致",
    "answers_mismatch": "答案不一致",
    "type_mismatch": "题型不一致",
    "missing_in_verify": "核验模型未识别出该题",
}
VERIFY_NOTICE_HINT = "机器 OCR 可能出错，以图像为准"



# ---------------------------------------------------------------------------
# 公共小工具
# ---------------------------------------------------------------------------

def _mb_text(value: int | None = None) -> str:
    size = MAX_FILE_BYTES if value is None else int(value)
    number = size / (1024 * 1024)
    return f"{number:g}MB"


def _file_extension(filename: str) -> str:
    return Path(str(filename or "")).suffix.lower()


def _safe_filename(filename: str) -> str:
    name = Path(str(filename or "")).name.strip()
    return name[:120] or "未命名文件"


def _content_name(data: bytes, suffix: str) -> str:
    digest = hashlib.sha256(data).hexdigest()[:32]
    return f"{digest}{suffix}"


def _value_error(exc: Exception) -> HTTPException:
    return HTTPException(http_status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc) or "文件处理失败")


# ---------------------------------------------------------------------------
# 双模型交叉核验（P2）：逐页独立提取 + 逐题 diff；核验失败降级不整任务失败
# ---------------------------------------------------------------------------

def _append_reason(existing: str, extra: str) -> str:
    """把新的待复核原因追加到已有原因后面（去重、限长）。"""
    existing = str(existing or "").strip()
    extra = str(extra or "").strip()
    if not extra:
        return existing
    if not existing:
        return extra[:500]
    if extra in existing:
        return existing[:500]
    return (existing + "；" + extra)[:500]


def _diff_reason(diff: dict) -> str:
    """把 diff 结构翻译成面向教师的中文待复核原因。"""
    diff = diff if isinstance(diff, dict) else {}
    flags = [str(flag) for flag in (diff.get("flags") or []) if str(flag).strip()]
    if not flags:
        return ""
    labels = [VERIFY_FLAG_LABELS.get(flag, flag) for flag in flags]
    ratio = schema.safe_float(diff.get("stem_ratio"))
    if ratio is not None and "stem_mismatch" in flags:
        labels = [
            label if label != "题干不一致"
            else f"题干不一致（相似度 {ratio * 100:.0f}%）"
            for label in labels
        ]
    return "双模型核验不一致：" + "、".join(labels)


def _verify_meta(provider, model: str, *, job_id: int | None = None) -> dict:
    provider_id = provider.get("id") if isinstance(provider, dict) else None
    return {
        "provider_id": provider_id,
        "model": str(model or ""),
        "prompt_version": prompts.PROMPT_VERSION,
        "job_id": job_id,
        "at": utcnow(),
    }


async def _verify_page(
    provider, model: str, page: dict, import_row: dict, primary_result: dict, *,
    user_id: int, job_id: int, page_no: int,
) -> dict:
    """核验模型独立提取同一页，再与主模型逐题比对。

    返回值：``{status, error, entries, meta, primary_count, verify_count, count_mismatch}``；
    核验调用失败只返回 ``status='failed'`` + ``error``（由调用方降级为「未完成交叉核验」）。
    """
    meta = _verify_meta(provider, model, job_id=job_id)
    if _is_cancelled(job_id):
        return {
            "status": "failed", "error": "任务已取消，未完成交叉核验",
            "entries": [], "meta": meta, "primary_count": 0, "verify_count": 0,
            "count_mismatch": False,
        }
    try:
        verify_result = await _recognize_page(
            provider, model, page, import_row, user_id=user_id, job_id=job_id,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # 核验失败不拖垮主结果：只记录原因
        return {
            "status": "failed", "error": _error_text(exc),
            "entries": [], "meta": meta, "primary_count": 0, "verify_count": 0,
            "count_mismatch": False,
        }
    comparison = schema.compare_page_results(
        primary_result, verify_result, stem_threshold=VERIFY_STEM_THRESHOLD,
    )
    entries = list(comparison.get("entries") or [])
    any_diff = any((entry.get("diff") or {}).get("flags") for entry in entries)
    status = "diff" if (any_diff or comparison.get("count_mismatch")) else "verified"
    return {
        "status": status,
        "error": "",
        "entries": entries,
        "meta": meta,
        "primary_count": int(comparison.get("primary_count") or 0),
        "verify_count": int(comparison.get("verify_count") or 0),
        "count_mismatch": bool(comparison.get("count_mismatch")),
    }


def _verify_counts(import_id: int, user_id: int) -> dict:
    """按 verify_status 聚合当前导入的核验结果（断点续跑后也能得到准确数字）。"""
    empty = {"total": 0, "pending": 0, "verified": 0, "diff": 0, "failed": 0,
             "accepted": 0, "skipped": 0}
    with connection() as con:
        rows = con.execute(
            "SELECT verify_status, COUNT(*) AS total FROM pl_import_items "
            "WHERE import_id=? AND user_id=? GROUP BY verify_status",
            (import_id, user_id),
        ).fetchall()
    for row in rows:
        status = str(row["verify_status"] or "pending")
        if status not in empty:
            status = "pending"
        empty[status] += int(row["total"] or 0)
        empty["total"] += int(row["total"] or 0)
    return empty


def _empty_verify_summary() -> dict:
    return {"total": 0, "pending": 0, "verified": 0, "diff": 0, "failed": 0,
            "accepted": 0, "skipped": 0}


def _verify_summary_map(user_id: int, import_ids: list[int]) -> dict[int, dict]:
    """批量取多个导入的核验状态计数（列表页用，避免逐行查询）。"""
    summary: dict[int, dict] = {}
    ids = [int(value) for value in (import_ids or [])]
    if not ids:
        return summary
    placeholders = ",".join("?" for _ in ids)
    with connection() as con:
        rows = con.execute(
            f"SELECT import_id, verify_status, COUNT(*) AS total FROM pl_import_items "
            f"WHERE user_id=? AND import_id IN ({placeholders}) "
            "GROUP BY import_id, verify_status",
            (user_id, *ids),
        ).fetchall()
    for row in rows:
        import_id = int(row["import_id"])
        status = str(row["verify_status"] or "pending")
        if status not in _empty_verify_summary():
            status = "pending"
        entry = summary.setdefault(import_id, _empty_verify_summary())
        entry[status] += int(row["total"] or 0)
        entry["total"] += int(row["total"] or 0)
    return summary


# ---------------------------------------------------------------------------
# 本地 OCR（P1 的 app.ai.ocr，延迟导入 + 容错）：无视觉模型时的降级通道
# ---------------------------------------------------------------------------

def _load_ocr_module():
    """延迟导入本地 OCR 模块；未安装/导入失败返回 None（不影响应用启动）。"""
    try:
        return importlib.import_module("app.ai.ocr")
    except Exception:  # ImportError 或引擎初始化异常都不应拖垮主流程
        return None


def _ocr_unavailable(message: str = "") -> HTTPException:
    detail = str(message or "").strip()
    if detail:
        detail = f"本地 OCR 不可用：{detail}。请安装 requirements.txt 中的本地 OCR 依赖后重启服务，或改用视觉模型识别。"
    else:
        detail = (
            "本地 OCR 不可用（未安装 rapidocr 或引擎未就绪）。"
            "请安装 requirements.txt 中的本地 OCR 依赖后重启服务，"
            "或改用视觉模型识别；本地 OCR 仅作为无视觉模型时的降级方案。"
        )
    return HTTPException(http_status.HTTP_503_SERVICE_UNAVAILABLE, detail)


def _ocr_engine_status(module) -> dict:
    """安全读取 ``app.ai.ocr.engine_status()``（模块未提供时返回空结构）。"""
    status = getattr(module, "engine_status", None)
    if callable(status):
        try:
            info = status() or {}
        except Exception:
            info = {}
        if isinstance(info, dict):
            return info
    return {}


def _ocr_engine_name(module) -> str:
    name = str(_ocr_engine_status(module).get("engine") or "").strip()
    return name or "rapidocr"


def _ocr_status() -> dict:
    """本地 OCR 能力位（前端据此启用/禁用按钮并显示中文引导）。"""
    module = _load_ocr_module()
    payload = {
        "available": False,
        "engine": "",
        "version": "",
        "models": [],
        "notice": "",
        "max_pages": MAX_OCR_PAGES,
        "split_modes": list(OCR_SPLIT_MODES),
        "prompt_hint": VERIFY_NOTICE_HINT,
    }
    if module is None:
        payload["notice"] = "本地 OCR 组件未安装（app.ai.ocr 不存在），无法提取文字或生成 OCR 草稿"
        return payload
    payload["available"] = True
    checker = getattr(module, "local_ocr_available", None)
    if callable(checker):
        try:
            payload["available"] = bool(checker())
        except Exception:
            payload["available"] = False
    info = _ocr_engine_status(module)
    payload["engine"] = str(info.get("engine") or "") or "rapidocr"
    payload["version"] = str(info.get("version") or "")
    models = info.get("models")
    payload["models"] = [str(model) for model in models] if isinstance(models, list) else []
    if not payload["available"]:
        payload["notice"] = str(info.get("notice") or "").strip() or (
            "本地 OCR 引擎未就绪（可能未安装 rapidocr 或已被 STUDY_LOCAL_OCR 关闭）；"
            "可改用视觉模型识别"
        )
    return payload


def _require_ocr_module():
    payload = _ocr_status()
    if not payload.get("available"):
        raise _ocr_unavailable(payload.get("notice") or "")
    module = _load_ocr_module()
    if module is None or not callable(getattr(module, "recognize_image", None)):
        raise _ocr_unavailable("OCR 模块缺少 recognize_image 接口")
    return module


def _ocr_line_bbox(box, width: float, height: float) -> dict | None:
    """OCR 行框（四点像素坐标）→ 归一化 ``{x,y,w,h}``；无法换算时返回 None。"""
    points: list[tuple[float, float]] = []
    if isinstance(box, (list, tuple)):
        for point in box:
            if isinstance(point, dict):
                x = schema.safe_float(point.get("x"))
                y = schema.safe_float(point.get("y"))
            elif isinstance(point, (list, tuple)) and len(point) >= 2:
                x = schema.safe_float(point[0])
                y = schema.safe_float(point[1])
            else:
                continue
            if x is None or y is None:
                continue
            points.append((float(x), float(y)))
    if len(points) < 2:
        return None
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    left, right = min(xs), max(xs)
    top, bottom = min(ys), max(ys)
    largest = max(abs(left), abs(right), abs(top), abs(bottom))
    if largest > 1.5:
        if width > 0 and height > 0:
            left, right = left / width, right / width
            top, bottom = top / height, bottom / height
        else:
            return None
    left = max(0.0, min(1.0, left))
    top = max(0.0, min(1.0, top))
    right = max(left, min(1.0, right))
    bottom = max(top, min(1.0, bottom))
    width_norm = right - left
    height_norm = bottom - top
    if width_norm < 0.002 or height_norm < 0.002:
        return None
    return {"x": round(left, 4), "y": round(top, 4),
            "w": round(width_norm, 4), "h": round(height_norm, 4)}


def _normalize_ocr_result(result) -> dict:
    """规整 ``app.ai.ocr`` 的返回：文本 + 归一化行框（像素 → 0~1）。"""
    result = result if isinstance(result, dict) else {}
    text = str(result.get("text") or "").strip()[:MAX_OCR_TEXT_CHARS]
    width = schema.safe_float(result.get("width")) or 0.0
    height = schema.safe_float(result.get("height")) or 0.0
    raw_lines = result.get("lines")
    lines: list[dict] = []
    for item in raw_lines if isinstance(raw_lines, list) else []:
        if len(lines) >= MAX_OCR_LINES:
            break
        if not isinstance(item, dict):
            continue
        line_text = str(item.get("text") or "").strip()
        if not line_text:
            continue
        line: dict = {"text": line_text[:2000]}
        bbox = _ocr_line_bbox(item.get("box"), width, height)
        if bbox is not None:
            line["bbox"] = bbox
        score = schema.safe_float(item.get("score"))
        if score is not None:
            line["score"] = round(max(0.0, min(1.0, score)), 4)
        lines.append(line)
    return {
        "text": text,
        "lines": lines,
        "width": int(width),
        "height": int(height),
        "elapsed_ms": schema.safe_int(result.get("elapsed_ms")),
    }


async def _ocr_page(module, page: dict) -> dict:
    """对一页执行本地 OCR：有页面图走 recognize_image，纯文本页直接复用文字。"""
    page_no = int(page.get("page_no") or 0)
    storage_name = str(page.get("storage_name") or "")
    engine = _ocr_engine_name(module)
    if not storage_name:
        text = str(page.get("text_content") or "").strip()[:MAX_OCR_TEXT_CHARS]
        if not text:
            raise ValueError(f"第 {page_no} 页没有可 OCR 的内容")
        return {"text": text, "lines": [], "width": int(page.get("width") or 0),
                "height": int(page.get("height") or 0), "elapsed_ms": None,
                "engine": "text_page"}
    raw = await asyncio.to_thread(storage.read_file, PAGE_FOLDER, storage_name)
    recognize = getattr(module, "recognize_image", None)
    if not callable(recognize):
        raise _ocr_unavailable("OCR 模块缺少 recognize_image 接口")
    # app.ai.ocr 的接口是 async（CPU 推理在其内部丢线程池）；兼容同步实现的假模块。
    if inspect.iscoroutinefunction(recognize):
        result = await recognize(raw)
    else:
        result = await asyncio.to_thread(recognize, raw)
    normalized = _normalize_ocr_result(result)
    normalized["engine"] = engine
    return normalized


def _save_page_ocr(import_id: int, user_id: int, page_no: int, normalized: dict) -> None:
    with connection() as con:
        write_lock(con, "paperlab-ocr")
        con.execute(
            "UPDATE pl_pages SET ocr_text=?, ocr_lines_json=?, ocr_engine=?, ocr_at=?, "
            "updated_at=? WHERE import_id=? AND page_no=? AND user_id=?",
            (
                str(normalized.get("text") or ""),
                json_dump(normalized.get("lines") or []),
                str(normalized.get("engine") or ""),
                utcnow(), utcnow(),
                import_id, page_no, user_id,
            ),
        )
        db.touch_import(con, import_id, user_id)


def _resolve_selected_pages(pages, *, total: int, max_pages: int, label: str) -> list[int]:
    """解析前端指定的页码（缺省取全部页；超上限返回 422 中文提示）。"""
    if pages:
        selected: list[int] = []
        for raw in pages:
            value = schema.safe_int(raw)
            if value is None or value < 1 or value > total:
                raise HTTPException(422, f"{label} 页码不在本次导入范围内（1–{total} 页）")
            if value not in selected:
                selected.append(value)
        if len(selected) > max_pages:
            raise HTTPException(422, f"单次最多处理 {max_pages} 页 {label}，请分批操作")
        return sorted(selected)
    if total > max_pages:
        raise HTTPException(
            422, f"单次最多处理 {max_pages} 页 {label}，请指定页码分批操作"
        )
    return list(range(1, total + 1))


def _union_bbox(boxes) -> dict | None:
    """合并多个归一化 bbox（取外接矩形）；全部无效时返回 None。"""
    valid = [box for box in (boxes or []) if isinstance(box, dict)]
    lefts = [schema.safe_float(box.get("x")) for box in valid]
    tops = [schema.safe_float(box.get("y")) for box in valid]
    rights = [
        (schema.safe_float(box.get("x")) or 0) + (schema.safe_float(box.get("w")) or 0)
        for box in valid
    ]
    bottoms = [
        (schema.safe_float(box.get("y")) or 0) + (schema.safe_float(box.get("h")) or 0)
        for box in valid
    ]
    lefts = [value for value in lefts if value is not None]
    tops = [value for value in tops if value is not None]
    if not lefts or not tops:
        return None
    left = max(0.0, min(lefts))
    top = max(0.0, min(tops))
    right = min(1.0, max(rights))
    bottom = min(1.0, max(bottoms))
    width = right - left
    height = bottom - top
    if width < 0.002 or height < 0.002:
        return None
    return {"x": round(left, 4), "y": round(top, 4),
            "w": round(width, 4), "h": round(height, 4)}


_QUESTION_START_RE = re.compile(r"^\s*(?:第?\s*\d{1,3}\s*[.、．)）]|[（(]\s*\d{1,3}\s*[)）])")


def _blank_line_blocks(text: str) -> list[str]:
    blocks = [part.strip() for part in re.split(r"\n\s*\n", str(text or ""))]
    return [block for block in blocks if block]


def _lines_to_blocks(lines: list[dict], *, from_question_start: bool = False) -> list[dict]:
    """把 OCR 行按「题号开头」或「整段」合并成草稿块，并带上行框外接 bbox。"""
    if not lines:
        return []
    if from_question_start:
        starts = [index for index, line in enumerate(lines)
                  if _QUESTION_START_RE.match(str(line.get("text") or ""))]
        starts = sorted({index for index in starts if index > 0})
        if len(starts) >= 1:
            starts = [0] + starts
            groups = []
            for position, start in enumerate(starts):
                end = starts[position + 1] if position + 1 < len(starts) else len(lines)
                groups.append(lines[start:end])
            return [
                {
                    "text": "\n".join(str(line.get("text") or "").strip()
                                      for line in group if str(line.get("text") or "").strip()),
                    "bbox": _union_bbox([line.get("bbox") for line in group]),
                }
                for group in groups
            ]
    return [{
        "text": "\n".join(str(line.get("text") or "").strip()
                          for line in lines if str(line.get("text") or "").strip()),
        "bbox": _union_bbox([line.get("bbox") for line in lines]),
    }]


def _attach_block_bboxes(blocks: list[str], lines: list[dict]) -> list[dict]:
    """把纯文本块与 OCR 行按归一化字数顺序对应，给每块生成行框外接 bbox。"""
    if not lines:
        return [{"text": block, "bbox": None} for block in blocks]
    result: list[dict] = []
    cursor = 0
    for block in blocks:
        target = len(schema.normalize_compare_text(block))
        consumed = 0
        group: list[dict] = []
        while cursor < len(lines) and consumed < target:
            line = lines[cursor]
            group.append(line)
            consumed += len(schema.normalize_compare_text(line.get("text")))
            cursor += 1
        result.append({"text": block, "bbox": _union_bbox([line.get("bbox") for line in group])})
    if cursor < len(lines) and result:
        leftover = [line.get("bbox") for line in lines[cursor:]]
        result[-1]["bbox"] = _union_bbox([result[-1].get("bbox"), *leftover])
    return result


def _split_ocr_blocks(page: dict, split: str) -> list[dict]:
    """按 ``split`` 方式把一页 OCR 文本拆成题草稿块（text + bbox）。"""
    text = str(page.get("ocr_text") or "").strip()
    raw_lines = json_load(page.get("ocr_lines_json"), [])
    lines = [
        line for line in raw_lines
        if isinstance(line, dict) and str(line.get("text") or "").strip()
    ] if isinstance(raw_lines, list) else []
    if split == "lines":
        if lines:
            return [
                {"text": str(line.get("text") or "").strip(), "bbox": line.get("bbox")}
                for line in lines
            ]
        return [{"text": line.strip(), "bbox": None}
                for line in str(text or "").splitlines() if line.strip()]
    blocks = _blank_line_blocks(text)
    if len(blocks) <= 1 and lines:
        question_blocks = _lines_to_blocks(lines, from_question_start=True)
        if len(question_blocks) > 1:
            return question_blocks
    if lines and len(blocks) > 1:
        return _attach_block_bboxes(blocks, lines)
    if lines:
        return _lines_to_blocks(lines)
    return [{"text": block, "bbox": None} for block in blocks]


def _ocr_draft(page: dict, import_row: dict, block: dict, *, split: str,
               engine: str) -> dict:
    """把 OCR 文本块包装成草稿：不猜题型/选项/答案（``answers=[]``），一律待人工复核。"""
    page_no = int(page.get("page_no") or 1)
    width = int(page.get("width") or 0)
    height = int(page.get("height") or 0)
    bbox = block.get("bbox")
    if not isinstance(bbox, dict) or not bbox:
        bbox = {"x": 0.03, "y": 0.03, "w": 0.94, "h": 0.94}
    stem = str(block.get("text") or "").strip()[:MAX_STEM_CHARS]
    return {
        "page_no": page_no,
        "bbox": bbox,
        "type": "SUBJECTIVE",
        "subject": str(import_row.get("subject") or "")[:40],
        "grade": str(import_row.get("grade") or "")[:40],
        "stem": stem,
        "options": [],
        "answers": [],
        "explanation": "",
        "rubric": "",
        "knowledge_tags": [],
        "difficulty": None,
        "score": None,
        "images": [],
        "confidence": None,
        "source_page": page_no,
        "_source": "local_ocr",
        "_ocr": {
            "engine": engine,
            "split": split,
            "at": utcnow(),
            "notice": "本地 OCR 文本按行/空行合并生成，未识别题型、选项与答案，需人工核验后入库",
            "prompt_version": prompts.PROMPT_VERSION,
            "page_size": [width, height],
        },
    }



# ---------------------------------------------------------------------------
# 文件 → 页面图（同步函数，在 to_thread 中执行）
# ---------------------------------------------------------------------------

def _normalise_image(raw: bytes) -> tuple[bytes, int, int]:
    """把上传图片 / DOCX 内嵌图片统一转成 JPEG 页面图（EXIF 校正 + 最长边限制）。"""
    from PIL import Image, ImageOps

    try:
        with Image.open(BytesIO(raw)) as probe:
            probe.verify()
    except Exception:
        raise ValueError("图片无法解析，请确认文件未损坏后重新上传") from None
    with Image.open(BytesIO(raw)) as image:
        # 像素炸弹防护：Image.open 是懒加载，必须在 exif/convert/thumbnail（整图解码）之前
        # 按声明的宽高做像素预算，避免小体积但超大尺寸的图片在解码阶段打爆内存。
        width, height = image.size
        if width <= 0 or height <= 0:
            raise ValueError("图片尺寸无效，请重新导出后上传")
        if width * height > MAX_PAGE_PIXELS:
            raise ValueError(
                f"图片像素过大（{width}×{height}，上限 {MAX_PAGE_PIXELS} 像素），"
                "请先降低分辨率后再上传"
            )
        try:
            image = ImageOps.exif_transpose(image)
        except Exception:
            pass
        if image is None:
            raise ValueError("图片无法解析，请确认文件未损坏后重新上传")
        if image.mode != "RGB":
            image = image.convert("RGB")
        if max(image.size) > MAX_PAGE_EDGE:
            image.thumbnail((MAX_PAGE_EDGE, MAX_PAGE_EDGE), Image.Resampling.LANCZOS)
        buffer = BytesIO()
        image.save(buffer, format="JPEG", quality=88, optimize=True)
        data = buffer.getvalue()
        if len(data) > MAX_IMAGE_BYTES:
            raise ValueError("图片压缩后仍然过大，请先降低分辨率后再上传")
        return data, int(image.width), int(image.height)


def _pdf_page_count(raw: bytes) -> int:
    try:
        import pypdfium2 as pdfium
    except ImportError:
        raise ValueError("服务器缺少 PDF 渲染组件（pypdfium2）") from None
    try:
        with pdfium.PdfDocument(raw) as document:
            return len(document)
    except Exception:
        raise ValueError("PDF 文件无法解析，请确认文件完整或重新导出后上传") from None


def _render_pdf_pages(raw: bytes) -> list[tuple[bytes, int, int]]:
    """用 pypdfium2 渲染 PDF 每一页为 JPEG（scale=2.0 ≈ 144 DPI，落在 144~200 DPI 区间）。"""
    try:
        import pypdfium2 as pdfium
    except ImportError:
        raise ValueError("服务器缺少 PDF 渲染组件（pypdfium2）") from None
    rendered: list[tuple[bytes, int, int]] = []
    try:
        with pdfium.PdfDocument(raw) as document:
            for index in range(len(document)):
                with closing(document[index]) as page:
                    # 像素预算：按页面点尺寸 × 渲染倍率预估，超大页面在解码前直接拒绝。
                    page_width, page_height = page.get_size()
                    pixel_width = int(page_width * PDF_RENDER_SCALE)
                    pixel_height = int(page_height * PDF_RENDER_SCALE)
                    if pixel_width <= 0 or pixel_height <= 0:
                        raise ValueError("PDF 页面尺寸无效，请重新导出后上传")
                    if pixel_width * pixel_height > MAX_PAGE_PIXELS:
                        raise ValueError(
                            f"PDF 页面尺寸过大（约 {pixel_width}×{pixel_height} 像素），"
                            "请降低页面尺寸后重新导出"
                        )
                    with closing(page.render(scale=PDF_RENDER_SCALE)) as bitmap:
                        image = bitmap.to_pil().convert("RGB")
                        buffer = BytesIO()
                        image.save(buffer, format="JPEG", quality=88, optimize=True)
                        rendered.append((buffer.getvalue(), int(image.width), int(image.height)))
    except ValueError:
        raise
    except Exception:
        raise ValueError("PDF 页面渲染失败，请确认文件完整后重试") from None
    return rendered


def _extract_docx(raw: bytes) -> tuple[str, list[bytes]]:
    """提取 DOCX 的文字与图片；图片按出现顺序（related_parts）返回原始字节。"""
    try:
        from docx import Document
        from docx.parts.image import ImagePart
    except ImportError:
        raise ValueError("服务器缺少 DOCX 解析组件（python-docx）") from None
    try:
        document = Document(BytesIO(raw))
    except Exception:
        raise ValueError("DOCX 文件无法解析，请确认文件未损坏后重新上传") from None
    lines: list[str] = []
    for paragraph in document.paragraphs:
        text = (paragraph.text or "").strip()
        if text:
            lines.append(text)
    for table in document.tables:
        for row in table.rows:
            cells = [(cell.text or "").strip() for cell in row.cells]
            if any(cells):
                lines.append(" | ".join(cells))
    text = "\n".join(lines)
    images: list[bytes] = []
    seen: set[str] = set()
    try:
        related = list(document.part.related_parts.values())
    except Exception:
        related = []
    for part in related:
        if not isinstance(part, ImagePart):
            continue
        blob = getattr(part, "blob", b"") or b""
        if not blob:
            continue
        digest = hashlib.sha1(blob).hexdigest()
        if digest in seen:
            continue
        seen.add(digest)
        images.append(blob)
    return text[:MAX_TEXT_PAGE_CHARS], images


# ---------------------------------------------------------------------------
# 上传导入
# ---------------------------------------------------------------------------

async def _collect_pages(files: list[UploadFile]) -> tuple[list[dict], list[dict], str, list[str]]:
    """把上传文件展开为页图列表；返回 (页面, 来源说明, 默认标题, 提示)。

    先逐文件校验格式/大小/页数，再统一落库；任何一步超限抛 422 中文提示。
    """
    pages: list[dict] = []
    sources: list[dict] = []
    notes: list[str] = []
    default_title = ""
    for upload in files:
        filename = _safe_filename(upload.filename or "")
        extension = _file_extension(filename)
        if extension not in ALLOWED_EXTENSIONS:
            raise HTTPException(
                422,
                f"不支持的文件格式：{filename}。仅支持 JPG / PNG / WebP / PDF / DOCX",
            )
        declared_size = getattr(upload, "size", None)
        if isinstance(declared_size, int) and declared_size > MAX_FILE_BYTES:
            raise HTTPException(
                422, f"「{filename}」超过单文件 {_mb_text()} 上限，请压缩后重新上传"
            )
        raw = await upload.read(MAX_FILE_BYTES + 1)
        if len(raw) > MAX_FILE_BYTES:
            # 有界读取（不整读未知大小上传）：超出上限一律 413，避免伪造 declared size 打爆内存。
            raise HTTPException(
                413, f"「{filename}」超过单文件 {_mb_text()} 上限，请压缩后重新上传"
            )
        if not raw:
            raise HTTPException(422, f"「{filename}」是空文件，请重新选择")
        try:
            if extension in ALLOWED_IMAGE_EXTENSIONS:
                data, width, height = await asyncio.to_thread(_normalise_image, raw)
                page_items = [(data, "image/jpeg", width, height)]
            elif extension == ".pdf":
                if not raw.startswith(b"%PDF"):
                    raise ValueError("文件内容不是有效的 PDF")
                page_count = await asyncio.to_thread(_pdf_page_count, raw)
                if page_count <= 0:
                    raise ValueError("PDF 中没有可识别的页面")
                if len(pages) + page_count > MAX_IMPORT_PAGES:
                    raise HTTPException(
                        422,
                        f"单次识别最多 {MAX_IMPORT_PAGES} 页，本批文件共 {len(pages) + page_count} 页，"
                        "请拆分后重新上传",
                    )
                rendered = await asyncio.to_thread(_render_pdf_pages, raw)
                page_items = [
                    (data, "image/jpeg", width, height) for data, width, height in rendered
                ]
            else:  # docx
                text, images = await asyncio.to_thread(_extract_docx, raw)
                if images:
                    page_items = []
                    skipped_images = 0
                    for blob in images:
                        try:
                            data, width, height = await asyncio.to_thread(_normalise_image, blob)
                        except ValueError:
                            skipped_images += 1
                            continue
                        # DOCX 提取的文字作为首页图片的识别参考（视觉模型以页图为准）。
                        hint = text if not page_items else ""
                        page_items.append((data, "image/jpeg", width, height, hint))
                    if not page_items:
                        raise ValueError(
                            f"「{filename}」的内嵌图片都无法解析，请确认文件后重新上传"
                        )
                    if skipped_images:
                        notes.append(
                            f"「{filename}」有 {skipped_images} 张内嵌图片无法解析，已跳过"
                        )
                elif text.strip():
                    page_items = [("", "text/plain", 0, 0, text)]
                else:
                    raise ValueError("DOCX 中没有可识别的文字或图片")
        except HTTPException:
            raise
        except ValueError as exc:
            raise _value_error(exc) from None

        for item in page_items:
            if item[1] == "text/plain":
                data, media_type, width, height, text = item[0], item[1], item[2], item[3], item[4]
            else:
                data, media_type, width, height = item[0], item[1], item[2], item[3]
                text = item[4] if len(item) > 4 else ""
            if len(pages) >= MAX_IMPORT_PAGES:
                raise HTTPException(
                    422,
                    f"单次识别最多 {MAX_IMPORT_PAGES} 页，请拆分后重新上传",
                )
            pages.append({
                "data": data,
                "media_type": media_type,
                "width": width,
                "height": height,
                "text_content": text if text else "",
                "source_name": filename,
            })
        sources.append({
            "filename": filename,
            "extension": extension,
            "bytes": len(raw),
            "pages": len(page_items),
        })
        if not default_title:
            default_title = Path(filename).stem[:120]
    if not pages:
        raise HTTPException(422, "没有可导入的页面，请重新选择文件")
    if len(pages) > MAX_IMPORT_PAGES:
        raise HTTPException(422, f"单次识别最多 {MAX_IMPORT_PAGES} 页，请拆分后重新上传")
    return pages, sources, default_title, notes


def _store_import(pages: list[dict], sources: list[dict], *, user_id: int, title: str,
                  subject: str, grade: str, note: str = "") -> dict:
    """把页图落盘 + 写 pl_imports/pl_pages（失败清理已写文件）。"""
    saved_names: list[str] = []
    now = utcnow()
    try:
        with connection() as con:
            write_lock(con, "paperlab-import")
            import_id = con.execute(
                "INSERT INTO pl_imports(user_id,title,subject,grade,status,page_count,job_id,"
                "source_json,note,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    user_id, title, subject, grade, "draft", len(pages), None,
                    json_dump(sources), note[:500], now, now,
                ),
            ).lastrowid
            for page_no, page in enumerate(pages, start=1):
                storage_name = ""
                if page["data"]:
                    suffix = IMAGE_MEDIA_TYPES.get(page["media_type"], ".jpg")
                    storage_name = _content_name(page["data"], suffix)
                    storage.save_file(
                        PAGE_FOLDER, storage_name, page["data"], page["media_type"]
                    )
                    saved_names.append(storage_name)
                con.execute(
                    "INSERT INTO pl_pages(user_id,import_id,page_no,width,height,storage_name,"
                    "media_type,source_name,text_content,status,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        user_id, import_id, page_no, page["width"], page["height"],
                        storage_name, page["media_type"], page["source_name"],
                        page["text_content"], "pending", now, now,
                    ),
                )
            con.execute(
                "UPDATE pl_imports SET updated_at=? WHERE id=?", (utcnow(), import_id)
            )
    except Exception:
        for name in saved_names:
            storage.discard_file(PAGE_FOLDER, name)
        raise
    return db.fetch_import(import_id, user_id)


@router.post("/imports")
async def create_import(
    files: list[UploadFile] | None = File(default=None),
    title: str = Form(default=""),
    subject: str = Form(default=""),
    grade: str = Form(default=""),
    user=Depends(current_user),
):
    """上传图片 / PDF / DOCX，分页落盘为页面图（作为识别任务的输入）。"""
    uploads = [item for item in (files or []) if item and (item.filename or "").strip()]
    if not uploads:
        raise HTTPException(422, "请至少选择一个文件（支持 JPG / PNG / WebP / PDF / DOCX）")
    if len(uploads) > MAX_IMPORT_FILES:
        raise HTTPException(422, f"单次最多上传 {MAX_IMPORT_FILES} 个文件，请分批导入")
    subject = (subject or "").strip()[:40]
    grade = (grade or "").strip()[:40]
    pages, sources, default_title, notes = await _collect_pages(uploads)
    import_row = _store_import(
        pages, sources, user_id=user["id"],
        title=(title or "").strip()[:120] or default_title or "未命名导入",
        subject=subject, grade=grade, note="；".join(notes),
    )
    page_rows = db.list_pages(import_row["id"], user["id"])
    public_pages = [db.public_page(row, import_row["id"]) for row in page_rows]
    payload = db.public_import(import_row)
    payload["pages"] = public_pages
    return {"import": payload, "pages": public_pages}


@router.get("/imports")
def list_imports(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=db.MAX_PAGE_SIZE),
    status: str | None = Query(default=None),
    user=Depends(current_user),
):
    where = ["user_id=?"]
    params: list = [user["id"]]
    if status:
        if status not in IMPORT_STATUSES:
            raise HTTPException(422, "未知的导入状态")
        where.append("status=?")
        params.append(status)
    clause = " AND ".join(where)
    with connection() as con:
        total = int(con.execute(
            f"SELECT COUNT(*) FROM pl_imports WHERE {clause}", tuple(params)
        ).fetchone()[0])
        rows = con.execute(
            f"SELECT * FROM pl_imports WHERE {clause} ORDER BY id DESC LIMIT ? OFFSET ?",
            (*params, page_size, (page - 1) * page_size),
        ).fetchall()
    import_ids = [int(row["id"]) for row in rows]
    verify_summaries = _verify_summary_map(user["id"], import_ids)
    items = []
    for row in rows:
        payload = db.public_import(row)
        payload["verify_summary"] = verify_summaries.get(
            int(row["id"]), _empty_verify_summary()
        )
        items.append(payload)
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/imports/{import_id}")
def import_detail(import_id: int, user=Depends(current_user)):
    import_row = db.fetch_import(import_id, user["id"])
    pages = [db.public_page(row, import_id) for row in db.list_pages(import_id, user["id"])]
    items = [db.public_item(row) for row in db.list_items(import_id, user["id"])]
    counts = {
        "total": len(items),
        "pending": sum(1 for item in items if item["status"] == "pending"),
        "accepted": sum(1 for item in items if item["status"] == "accepted"),
        "skipped": sum(1 for item in items if item["status"] == "skipped"),
        "needs_review": sum(1 for item in items if item["needs_review"]),
        "committed": sum(1 for item in items if item["question_id"]),
    }
    return {
        "import": db.public_import(import_row),
        "pages": pages,
        "items": items,
        "counts": counts,
        # P2 增量（只增字段，不改既有键）：核验状态计数 + 本地 OCR 能力位
        "verify_counts": _verify_counts(import_id, user["id"]),
        "ocr": _ocr_status(),
    }


@router.get("/imports/{import_id}/pages/{page_no}/image")
def page_image(import_id: int, page_no: int, user=Depends(current_user)):
    page = db.fetch_page(import_id, page_no, user["id"])
    if not str(page.get("storage_name") or ""):
        raise HTTPException(404, "该页为纯文本页，没有页面图")
    return storage.file_response(
        PAGE_FOLDER,
        page["storage_name"],
        media_type=page.get("media_type") or "image/jpeg",
        headers={"Cache-Control": "private, max-age=3600"},
    )


# ---------------------------------------------------------------------------
# 本地 OCR：提取页文字 / OCR-only 草稿（无视觉模型时的降级通道）
# ---------------------------------------------------------------------------

class OcrInput(BaseModel):
    pages: list[int] | None = Field(default=None, max_length=MAX_IMPORT_PAGES)


class OcrDraftInput(BaseModel):
    pages: list[int] | None = Field(default=None, max_length=MAX_IMPORT_PAGES)
    split: str = Field(default="blank_line", max_length=20)


@router.get("/ocr/info")
def ocr_info(user=Depends(current_user)):
    """本地 OCR 能力位：前端据此启用/禁用按钮并给出安装或改用视觉模型的中文引导。"""
    return _ocr_status()


@router.post("/imports/{import_id}/ocr")
async def extract_page_ocr(
    import_id: int, data: OcrInput | None = None, user=Depends(current_user),
):
    """同步对指定页（缺省前 5 页）跑本地 OCR，把文字写入页记录（``pl_pages.ocr_*``）。"""
    import_row = db.fetch_import(import_id, user["id"])
    total = int(import_row.get("page_count") or 0)
    if total <= 0:
        raise HTTPException(422, "该导入还没有页面，请重新上传")
    module = _require_ocr_module()
    engine = _ocr_engine_name(module)
    page_rows = {int(page["page_no"]): page for page in db.list_pages(import_id, user["id"])}
    selected = _resolve_selected_pages(
        (data.pages if data else None), total=total, max_pages=MAX_OCR_PAGES, label="OCR",
    )
    results: list[dict] = []
    errors: list[dict] = []
    for page_no in selected:
        page = page_rows.get(page_no)
        if page is None:
            errors.append({"page_no": page_no, "error": "页面不存在"})
            continue
        try:
            normalized = await _ocr_page(module, page)
        except HTTPException:
            raise
        except Exception as exc:  # 单页失败不阻塞其余页
            name = type(exc).__name__
            message = _error_text(exc)
            if name == "OcrUnavailable":
                raise _ocr_unavailable(message) from None
            if name == "OcrTimeout":
                raise HTTPException(
                    http_status.HTTP_503_SERVICE_UNAVAILABLE,
                    f"本地 OCR 处理超时（第 {page_no} 页），请减少页数或降低图片分辨率后重试",
                ) from None
            errors.append({"page_no": page_no, "error": message})
            continue
        text = str(normalized.get("text") or "").strip()
        lines = normalized.get("lines") or []
        if not text and not lines:
            errors.append({"page_no": page_no, "error": "未识别到文字"})
            continue
        await asyncio.to_thread(_save_page_ocr, import_id, user["id"], page_no, normalized)
        results.append({
            "page_no": page_no,
            "chars": len(text),
            "line_count": len(lines),
            "engine": normalized.get("engine") or engine,
            "elapsed_ms": normalized.get("elapsed_ms"),
            "preview": text[:300] + ("…" if len(text) > 300 else ""),
        })
    if not results:
        detail = "；".join(f"第 {item['page_no']} 页：{item['error']}" for item in errors[:3])
        raise HTTPException(
            http_status.HTTP_503_SERVICE_UNAVAILABLE,
            "本地 OCR 没有成功提取任何页面" + (f"（{detail}）" if detail else "") + "，请稍后重试或改用视觉模型识别",
        )
    return {
        "import_id": import_id,
        "pages": results,
        "errors": errors,
        "total_chars": sum(int(page["chars"]) for page in results),
        "engine": engine,
        "max_pages": MAX_OCR_PAGES,
        "notice": "本地 OCR 文字已写入页记录；AI 识别时会作为辅助提示注入（"
                  + VERIFY_NOTICE_HINT + "）。",
    }


@router.post("/imports/{import_id}/ocr-draft")
async def create_ocr_drafts(
    import_id: int, data: OcrDraftInput | None = None, user=Depends(current_user),
):
    """无视觉模型时的降级：按 OCR 行/空行合并成题草稿（不生成答案、不伪造结构）。"""
    import_row = db.fetch_import(import_id, user["id"])
    total = int(import_row.get("page_count") or 0)
    if total <= 0:
        raise HTTPException(422, "该导入还没有页面，请重新上传")
    payload = data or OcrDraftInput()
    split = str(payload.split or "blank_line").strip()
    if split not in OCR_SPLIT_MODES:
        raise HTTPException(422, "未知的 OCR 合并方式，可选 blank_line（按空行合并）或 lines（逐行生成）")
    page_rows = {int(page["page_no"]): page for page in db.list_pages(import_id, user["id"])}
    selected = _resolve_selected_pages(
        payload.pages, total=total, max_pages=MAX_IMPORT_PAGES, label="OCR 草稿",
    )
    missing = [
        page_no for page_no in selected
        if not str(page_rows.get(page_no, {}).get("ocr_text") or "").strip()
    ]
    if missing:
        if len(missing) > MAX_OCR_PAGES:
            raise HTTPException(
                422,
                f"有 {len(missing)} 页还没有 OCR 文字；单次最多自动提取 {MAX_OCR_PAGES} 页，"
                "请先分批提取文字后再生成草稿",
            )
        module = _require_ocr_module()
        for page_no in missing:
            page = page_rows.get(page_no)
            if page is None:
                raise HTTPException(404, f"未找到第 {page_no} 页")
            try:
                normalized = await _ocr_page(module, page)
            except HTTPException:
                raise
            except Exception as exc:
                name = type(exc).__name__
                if name == "OcrUnavailable":
                    raise _ocr_unavailable(_error_text(exc)) from None
                if name == "OcrTimeout":
                    raise HTTPException(
                        http_status.HTTP_503_SERVICE_UNAVAILABLE,
                        f"本地 OCR 处理超时（第 {page_no} 页），请稍后重试或减少页数",
                    ) from None
                raise HTTPException(422, f"第 {page_no} 页本地 OCR 失败：{_error_text(exc)}") from None
            await asyncio.to_thread(_save_page_ocr, import_id, user["id"], page_no, normalized)
            page_rows[page_no] = {
                **page,
                "ocr_text": normalized.get("text") or "",
                "ocr_lines_json": json_dump(normalized.get("lines") or []),
                "ocr_engine": normalized.get("engine") or "",
            }
    engines = sorted({
        str(page_rows[page_no].get("ocr_engine") or "")
        for page_no in selected if str(page_rows[page_no].get("ocr_engine") or "")
    })
    created_ids: list[int] = []
    warnings: list[str] = []
    now = utcnow()
    with connection() as con:
        write_lock(con, "paperlab-ocr-draft")
        placeholders = ",".join("?" for _ in selected)
        con.execute(
            f"DELETE FROM pl_import_items WHERE import_id=? AND user_id=? AND source='local_ocr' "
            f"AND question_id IS NULL AND page_no IN ({placeholders})",
            (import_id, user["id"], *selected),
        )
        item_no = int(con.execute(
            "SELECT COALESCE(MAX(item_no),0) FROM pl_import_items WHERE import_id=?",
            (import_id,),
        ).fetchone()[0] or 0)
        stop = False
        for page_no in selected:
            page = page_rows.get(page_no) or {}
            engine = str(page.get("ocr_engine") or "") or (engines[0] if engines else "")
            blocks = _split_ocr_blocks(page, split)
            if not blocks:
                warnings.append(f"第 {page_no} 页没有可供合并的 OCR 文字")
                continue
            for block in blocks:
                text = str(block.get("text") or "").strip()
                if not text:
                    continue
                if item_no >= MAX_IMPORT_ITEMS:
                    warnings.append(f"草稿超过 {MAX_IMPORT_ITEMS} 条上限，其余 OCR 文本未保存")
                    stop = True
                    break
                draft = _ocr_draft(page, import_row, block, split=split, engine=engine)
                item_no += 1
                item_id = con.execute(
                    "INSERT INTO pl_import_items(user_id,import_id,page_no,item_no,bbox_json,"
                    "draft_json,status,needs_review,review_reason,skip_reason,confidence,source,"
                    "verify_status,verify_json,diff_json,verify_error,warnings_json,question_id,"
                    "created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?,?)",
                    (
                        user["id"], import_id, page_no, item_no,
                        json_dump(draft.get("bbox") or {}), json_dump(draft),
                        "pending", 1,
                        "本地 OCR 草稿：仅按文字行/空行合并，题型、选项与答案需人工核验",
                        "", None, "local_ocr",
                        "pending", "{}", "{}", "",
                        json_dump(["本地 OCR 可能识别错误，请对照页图核对后再入库"]),
                        now, now,
                    ),
                ).lastrowid
                created_ids.append(int(item_id))
            if stop:
                break
        db.touch_import(con, import_id, user["id"])
    status = import_row.get("status") or "draft"
    if created_ids:
        status = await asyncio.to_thread(_settle_import, import_id, user["id"], fallback="draft")
    created_set = set(created_ids)
    items = [
        db.public_item(row) for row in db.list_items(import_id, user["id"])
        if int(row["id"]) in created_set
    ]
    return {
        "import_id": import_id,
        "pages": selected,
        "split": split,
        "created": len(created_ids),
        "item_ids": created_ids,
        "items": items,
        "ocr_engine": engines[0] if engines else "",
        "warnings": warnings,
        "import_status": status,
        "notice": "本地 OCR 草稿只包含题干文字：answers 为空、题型统一为 SUBJECTIVE、"
                  "needs_review=true；请对照页图人工补全结构并核验后再入库。",
    }


# ---------------------------------------------------------------------------
# 触发识别任务
# ---------------------------------------------------------------------------

class RecognizeInput(BaseModel):
    page_from: int | None = Field(default=None, ge=1)
    page_to: int | None = Field(default=None, ge=1)
    provider_id: int = Field(ge=1)
    model: str = Field(min_length=1, max_length=160)
    # P2：双模型交叉核验（核验连接/模型是独立的一次视觉提取，仅比对差异，不覆盖主结果）
    cross_verify: bool = False
    verify_provider_id: int | None = Field(default=None, ge=1)
    verify_model: str | None = Field(default=None, min_length=1, max_length=160)


def _resolve_page_range(page_from: int | None, page_to: int | None, total: int) -> tuple[int, int]:
    if total <= 0:
        raise HTTPException(422, "该导入还没有页面，请重新上传")
    start = int(page_from or 1)
    end = int(page_to or total)
    if start < 1 or start > total:
        raise HTTPException(422, "识别起始页超出导入页数")
    if end < start:
        raise HTTPException(422, "页码范围不正确：结束页不能早于起始页")
    if end > total:
        raise HTTPException(422, "识别结束页超出导入页数")
    if end - start + 1 > MAX_IMPORT_PAGES:
        raise HTTPException(422, f"单次最多识别 {MAX_IMPORT_PAGES} 页，请分批识别")
    return start, end


@router.post("/imports/{import_id}/recognize")
async def recognize_import(
    import_id: int, data: RecognizeInput, user=Depends(current_user),
):
    """创建 ``paper-import`` 任务：逐页调用视觉模型识别，结果写入题目草稿。

    ``cross_verify=true`` 时同时逐页调用核验模型独立提取并逐题比对（只标注差异、
    不覆盖主结果；核验失败降级为「未完成交叉核验」）。主/核验同一连接+模型会 422。
    """
    import_row = db.fetch_import(import_id, user["id"])
    total = int(import_row.get("page_count") or 0)
    start, end = _resolve_page_range(data.page_from, data.page_to, total)
    from ..ai import jobs as ai_jobs

    job_input = ai_jobs.JobInput(
        task="paper-import",
        provider_id=data.provider_id,
        model=data.model,
        cross_verify=bool(data.cross_verify),
        verify_provider_id=data.verify_provider_id if data.cross_verify else None,
        verify_model=data.verify_model if data.cross_verify else None,
        params={"import_id": import_id, "page_from": start, "page_to": end},
    )
    job = await ai_jobs.submit_job(user, job_input)
    with connection() as con:
        write_lock(con, "paperlab-import-job")
        db.touch_import(con, import_id, user["id"], status="recognizing", job_id=job["id"])
    return {
        "job": job,
        "id": job.get("id"),
        "status": job.get("status"),
        "task": job.get("task"),
        "import_id": import_id,
        "page_from": start,
        "page_to": end,
        "cross_verify": bool(data.cross_verify),
        "verify_provider_id": data.verify_provider_id if data.cross_verify else None,
        "verify_model": data.verify_model if data.cross_verify else None,
    }


# ---------------------------------------------------------------------------
# 人工编辑草稿 / 批量入库
# ---------------------------------------------------------------------------

class ItemPatchInput(BaseModel):
    draft: dict | None = None
    bbox: dict | None = None
    status: str | None = None
    skip_reason: str | None = Field(default=None, max_length=500)


@router.patch("/imports/{import_id}/items/{item_id}")
def patch_item(
    import_id: int, item_id: int, data: ItemPatchInput, user=Depends(current_user),
):
    """人工修改识别草稿：题干/答案/知识点/裁图框，跳过与恢复。"""
    import_row = db.fetch_import(import_id, user["id"])
    item = db.fetch_item(import_id, item_id, user["id"])
    if item.get("question_id"):
        raise HTTPException(409, "该题已入库，请到题库中编辑")
    target_status = data.status
    if target_status is not None and target_status not in ITEM_STATUSES:
        raise HTTPException(422, "未知的草稿状态")
    draft = json_load(item.get("draft_json"), {})
    if not isinstance(draft, dict):
        draft = {}
    patch: dict | None = None
    if data.bbox is not None:
        patch = dict(data.draft or {})
        patch["bbox"] = data.bbox
    elif data.draft is not None:
        patch = dict(data.draft)
    if patch is not None:
        page = db.fetch_page(import_id, int(item["page_no"]), user["id"])
        # 编辑后保留模型/OCR 溯源标记（normalize_draft 只返回契约字段）
        meta_keys = {
            key: draft.get(key) for key in ("_ai", "_ocr", "_source") if draft.get(key)
        }
        try:
            draft = schema.apply_draft_edit(
                draft, patch,
                page_no=int(item["page_no"]),
                width=int(page.get("width") or 0),
                height=int(page.get("height") or 0),
                default_subject=import_row.get("subject") or "",
                default_grade=import_row.get("grade") or "",
            )
            draft.update(meta_keys)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
    new_status = target_status or item.get("status") or "pending"
    skip_reason = item.get("skip_reason") or ""
    review_reason = item.get("review_reason") or ""
    needs_review = int(item.get("needs_review") or 0)
    source = item.get("source") or "ai"
    if patch is not None:
        source = "manual"
        review_reason = ""
        needs_review = 0
    if new_status == "skipped":
        skip_reason = (data.skip_reason or skip_reason or "人工跳过").strip()[:500]
        review_reason = "人工跳过"
        needs_review = 1
    else:
        skip_reason = ""
        if target_status is not None:
            review_reason = ""
            needs_review = 0
    with connection() as con:
        write_lock(con, "paperlab-item")
        con.execute(
            "UPDATE pl_import_items SET bbox_json=?, draft_json=?, status=?, needs_review=?, "
            "review_reason=?, skip_reason=?, source=?, updated_at=? "
            "WHERE id=? AND import_id=? AND user_id=?",
            (
                json_dump(draft.get("bbox") or {}), json_dump(draft), new_status,
                needs_review, review_reason, skip_reason, source, utcnow(),
                item_id, import_id, user["id"],
            ),
        )
        db.touch_import(con, import_id, user["id"])
    return {"item": db.public_item(db.fetch_item(import_id, item_id, user["id"]))}


class AcceptVerifyInput(BaseModel):
    source: str = Field(default="primary", max_length=16)


@router.post("/imports/{import_id}/items/{item_id}/accept-verify")
def accept_item_verify(
    import_id: int, item_id: int, data: AcceptVerifyInput | None = None,
    user=Depends(current_user),
):
    """人工确认双模型核验结果：以主模型（primary）或核验模型（verify）的草稿为准。

    - ``verify``：用核验模型独立提取的草稿替换当前草稿（bbox/页码沿用已有人工结果），
      并把 ``_ai`` 标记改为核验模型，保证入库来源可追溯；
    - ``primary``：保留主模型草稿，仅清除待复核标记。
    两种确认都会把草稿 ``source`` 置为 ``manual``（避免重跑识别覆盖人工决定）。
    """
    import_row = db.fetch_import(import_id, user["id"])
    item = db.fetch_item(import_id, item_id, user["id"])
    if item.get("question_id"):
        raise HTTPException(409, "该题已入库，请到题库中编辑")
    if item.get("status") == "skipped":
        raise HTTPException(409, "跳过的草稿不能确认核验结果，请先恢复为待确认")
    source = str((data.source if data else "primary") or "primary").strip().lower()
    if source not in {"primary", "verify"}:
        raise HTTPException(422, "未知的确认来源（可选 primary 以主模型为准 / verify 以核验模型为准）")
    verify_json = json_load(item.get("verify_json"), {})
    if not isinstance(verify_json, dict):
        verify_json = {}
    verify_status = str(item.get("verify_status") or "pending")
    verify_draft = verify_json.get("draft")
    verify_draft = verify_draft if isinstance(verify_draft, dict) and verify_draft else None
    if verify_status in {"pending", "skipped"} and verify_draft is None:
        raise HTTPException(422, "该草稿还没有双模型核验结果，请先开启交叉核验重新识别")
    if source == "verify" and verify_draft is None:
        raise HTTPException(422, "该草稿没有核验模型结果（未完成交叉核验），只能以主模型为准")
    draft = json_load(item.get("draft_json"), {})
    if not isinstance(draft, dict):
        draft = {}
    accepted_source = "primary"
    if source == "verify" and verify_draft is not None:
        merged = dict(verify_draft)
        page_no = int(item.get("page_no") or draft.get("page_no") or 1)
        merged["page_no"] = page_no
        merged["source_page"] = schema.safe_int(draft.get("source_page")) or page_no
        merged["bbox"] = draft.get("bbox") or merged.get("bbox") or {}
        page = db.fetch_page(import_id, page_no, user["id"])
        try:
            merged = schema.normalize_draft(
                merged, page_no=page_no,
                width=int(page.get("width") or 0), height=int(page.get("height") or 0),
                default_subject=import_row.get("subject") or "",
                default_grade=import_row.get("grade") or "",
            )
        except ValueError as exc:
            raise HTTPException(422, f"核验模型结果不完整，无法直接采用：{exc}") from None
        merged["_ai"] = {
            "provider_id": verify_json.get("provider_id"),
            "model": verify_json.get("model") or "",
            "prompt_version": verify_json.get("prompt_version") or prompts.PROMPT_VERSION,
            "job_id": verify_json.get("job_id"),
            "at": verify_json.get("at") or utcnow(),
            "accepted_from": "verify",
        }
        if draft.get("_ocr"):
            merged["_ocr"] = draft.get("_ocr")
        if draft.get("_source"):
            merged["_source"] = draft.get("_source")
        draft = merged
        accepted_source = "verify"
    accepted = dict(verify_json)
    accepted["accepted"] = accepted_source
    accepted["accepted_at"] = utcnow()
    review_reason = "人工确认核验结果" + ("（以核验模型为准）" if accepted_source == "verify" else "（以主模型为准）")
    with connection() as con:
        write_lock(con, "paperlab-item")
        con.execute(
            "UPDATE pl_import_items SET bbox_json=?, draft_json=?, source='manual', needs_review=0, "
            "review_reason=?, verify_status='accepted', verify_json=?, updated_at=? "
            "WHERE id=? AND import_id=? AND user_id=?",
            (
                json_dump(draft.get("bbox") or {}), json_dump(draft), review_reason,
                json_dump(accepted), utcnow(), item_id, import_id, user["id"],
            ),
        )
        db.touch_import(con, import_id, user["id"])
    return {
        "item": db.public_item(db.fetch_item(import_id, item_id, user["id"])),
        "accepted": accepted_source,
    }


def _bbox_pixels(bbox: dict, width: int, height: int) -> tuple[int, int, int, int] | None:
    try:
        x = float(bbox.get("x"))
        y = float(bbox.get("y"))
        w = float(bbox.get("w"))
        h = float(bbox.get("h"))
    except (TypeError, ValueError, AttributeError):
        return None
    if width <= 0 or height <= 0:
        return None
    left = max(0, min(width, int(round(x * width))))
    top = max(0, min(height, int(round(y * height))))
    right = max(0, min(width, int(round((x + w) * width))))
    bottom = max(0, min(height, int(round((y + h) * height))))
    if right - left < 4 or bottom - top < 4:
        return None
    return left, top, right, bottom


def _crop_draft_images(draft: dict, page: dict, user_id: int,
                       con=None) -> tuple[dict, list[str]]:
    """按图片 bbox 从页图裁切配图存 ``paperlab/assets`` 并登记资产；失败只记 warning。

    P0：新裁切的配图登记为服务端签发的 ``asset_id``（content 只存 asset_id）；
    草稿里已有的引用只接受本人在用资产（旧文件名需先经迁移确认归属），无法归属的
    引用会被移除并给出中文提示，避免把他人资产写进自己的题目。
    ``con`` 传入时复用调用方事务（SQLite 下避免嵌套写锁）。
    """
    warnings: list[str] = []
    images = draft.get("images")
    if not isinstance(images, list) or not images:
        return draft, warnings
    storage_name = str(page.get("storage_name") or "")
    if not storage_name:
        return draft, ["纯文本页无法自动裁剪配图，请人工处理"]
    try:
        raw = storage.read_file(PAGE_FOLDER, storage_name)
    except Exception:
        return draft, ["页面图读取失败，配图未裁剪"]
    from PIL import Image

    try:
        with Image.open(BytesIO(raw)) as page_image:
            page_image = page_image.convert("RGB")
            width, height = page_image.size
            cropped_images: list[dict] = []
            for index, image in enumerate(images):
                if not isinstance(image, dict):
                    continue
                updated = dict(image)
                if updated.get("asset"):
                    # 只保留当前用户已有的资产；旧文件名引用需先迁移确认归属。
                    asset_id = assets.adopt_reference(
                        int(user_id), updated.get("asset"),
                        purpose="import_crop", allow_claim=False, con=con,
                    )
                    if asset_id:
                        updated["asset"] = asset_id
                    else:
                        updated.pop("asset", None)
                        warnings.append(
                            f"第 {index + 1} 张配图的旧引用无法归属到当前用户，已移除"
                        )
                    cropped_images.append(updated)
                    continue
                box = _bbox_pixels(updated.get("bbox") or {}, width, height)
                if box is None:
                    warnings.append(f"第 {index + 1} 张配图 bbox 无效，未裁剪")
                    cropped_images.append(updated)
                    continue
                try:
                    with page_image.crop(box) as crop:
                        buffer = BytesIO()
                        crop.save(buffer, format="PNG", optimize=True)
                        data = buffer.getvalue()
                        asset_id = assets.save_asset_bytes(
                            int(user_id), data, media_type="image/png",
                            purpose="import_crop", con=con,
                        )
                        updated["asset"] = asset_id
                        updated["media_type"] = "image/png"
                        updated["width"] = int(crop.width)
                        updated["height"] = int(crop.height)
                except Exception as exc:
                    warnings.append(f"第 {index + 1} 张配图裁切失败：{exc}")
                cropped_images.append(updated)
            draft = dict(draft)
            draft["images"] = cropped_images
    except Exception:
        warnings.append("页面图打开失败，配图未裁剪")
    return draft, warnings


def _item_verify_payload(verify: dict | None, index: int) -> dict:
    """把页级核验结果拆成单题落库字段（核验失败/缺项都降级为「未完成交叉核验」）。"""
    payload = {
        "verify_status": "pending", "verify_json": {}, "diff_json": {},
        "verify_error": "", "needs_review": False, "reason": "",
    }
    if verify is None:
        return payload
    status = str(verify.get("status") or "")
    meta = verify.get("meta") if isinstance(verify.get("meta"), dict) else {}
    if status == "failed":
        payload["verify_status"] = "failed"
        payload["verify_error"] = str(verify.get("error") or "核验模型调用失败")
        payload["needs_review"] = True
        payload["reason"] = "未完成交叉核验（结果仅来自主模型），请人工复核"
        return payload
    entries = verify.get("entries") if isinstance(verify.get("entries"), list) else []
    entry = entries[index] if index < len(entries) and isinstance(entries[index], dict) else None
    if entry is None:
        payload["verify_status"] = "failed"
        payload["verify_error"] = "核验模型未返回该题的结果"
        payload["needs_review"] = True
        payload["reason"] = "未完成交叉核验（该题没有核验结果），请人工复核"
        return payload
    diff = entry.get("diff") if isinstance(entry.get("diff"), dict) else {}
    flags = [str(flag) for flag in (diff.get("flags") or []) if str(flag).strip()]
    payload["verify_json"] = {**meta, "draft": entry.get("draft") or {}, "index": index}
    payload["diff_json"] = diff
    if flags:
        payload["verify_status"] = "diff"
        payload["needs_review"] = True
        payload["reason"] = _diff_reason(diff)
    else:
        payload["verify_status"] = "verified"
    return payload


def _save_page_items(
    import_id: int, user_id: int, page: dict, result: dict, *, model: str,
    provider_id: int | None, job_id: int, verify: dict | None = None,
) -> dict:
    """把一页识别结果写入 pl_import_items（同页 AI 旧草稿整体替换，人工稿保留）。

    ``verify`` 为页级双模型核验结果（``_verify_page`` 返回值）：逐题写
    ``verify_status/verify_json/diff_json/verify_error``，diff 不一致或核验失败时
    强制 ``needs_review=1`` 并追加中文待复核原因；核验失败只降级标注，不影响主结果。
    """
    page_no = int(page["page_no"])
    ai_meta = {
        "provider_id": provider_id,
        "model": model,
        "prompt_version": prompts.PROMPT_VERSION,
        "job_id": job_id,
        "at": utcnow(),
    }
    warnings: list[str] = []
    created = 0
    skipped = 0
    now = utcnow()
    with connection() as con:
        write_lock(con, "paperlab-items")
        con.execute(
            "DELETE FROM pl_import_items WHERE import_id=? AND user_id=? AND page_no=? "
            "AND source='ai' AND status='pending' AND question_id IS NULL",
            (import_id, user_id, page_no),
        )
        item_no = int(con.execute(
            "SELECT COALESCE(MAX(item_no),0) FROM pl_import_items WHERE import_id=?",
            (import_id,),
        ).fetchone()[0] or 0)
        questions = list(result.get("questions") or [])
        for index, draft in enumerate(questions):
            if item_no >= MAX_IMPORT_ITEMS:
                warnings.append(f"草稿超过 {MAX_IMPORT_ITEMS} 条上限，其余题目未保存")
                break
            draft = dict(draft)
            draft["_ai"] = ai_meta
            draft, crop_warnings = _crop_draft_images(draft, page, user_id, con)
            warnings.extend(f"第 {page_no} 页：" + message for message in crop_warnings)
            confidence = draft.get("confidence")
            needs_review = confidence is None or float(confidence) < REVIEW_CONFIDENCE
            review_reason = ""
            if needs_review:
                if confidence is None:
                    review_reason = "模型未给出置信度"
                else:
                    review_reason = f"识别置信度较低（{float(confidence):.2f}），请人工复核"
            verify_payload = _item_verify_payload(verify, index)
            if verify_payload["needs_review"]:
                needs_review = True
            review_reason = _append_reason(review_reason, verify_payload["reason"])
            item_no += 1
            con.execute(
                "INSERT INTO pl_import_items(user_id,import_id,page_no,item_no,bbox_json,"
                "draft_json,status,needs_review,review_reason,skip_reason,confidence,source,"
                "verify_status,verify_json,diff_json,verify_error,warnings_json,question_id,"
                "created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?,?)",
                (
                    user_id, import_id, page_no, item_no,
                    json_dump(draft.get("bbox") or {}), json_dump(draft),
                    "pending", 1 if needs_review else 0, review_reason, "",
                    float(confidence) if confidence is not None else None,
                    "ai",
                    verify_payload["verify_status"],
                    json_dump(verify_payload["verify_json"]),
                    json_dump(verify_payload["diff_json"]),
                    verify_payload["verify_error"],
                    json_dump(crop_warnings), now, now,
                ),
            )
            created += 1
        for skip in result.get("skipped") or []:
            if item_no >= MAX_IMPORT_ITEMS:
                warnings.append(f"草稿超过 {MAX_IMPORT_ITEMS} 条上限，其余跳过项未保存")
                break
            item_no += 1
            reason = str(skip.get("reason") or "无法辨认").strip()[:500]
            draft = {
                "page_no": page_no,
                "bbox": skip.get("bbox") or {},
                "source_page": skip.get("source_page") or page_no,
                "skip_reason": reason,
            }
            con.execute(
                "INSERT INTO pl_import_items(user_id,import_id,page_no,item_no,bbox_json,"
                "draft_json,status,needs_review,review_reason,skip_reason,confidence,source,"
                "verify_status,verify_json,diff_json,verify_error,warnings_json,question_id,"
                "created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?,?)",
                (
                    user_id, import_id, page_no, item_no,
                    json_dump(draft.get("bbox") or {}), json_dump(draft),
                    "skipped", 1, "识别跳过", reason, None, "ai",
                    "skipped", "{}", "{}", "", json_dump([]), now, now,
                ),
            )
            skipped += 1
        con.execute(
            "UPDATE pl_pages SET status='recognized', updated_at=? WHERE import_id=? AND page_no=?",
            (now, import_id, page_no),
        )
        db.touch_import(con, import_id, user_id)
    return {"created": created, "skipped": skipped, "warnings": warnings}


class CommitInput(BaseModel):
    item_ids: list[int] | None = Field(default=None, max_length=MAX_IMPORT_ITEMS)
    target: str = Field(default="app_bank", max_length=20)


def _question_fields_from_draft(draft: dict, import_row: dict, *, job_id: int | None,
                                model: str, provider_id: int | None) -> dict:
    qtype = draft.get("type") or "SUBJECTIVE"
    source_rows = json_load(import_row.get("source"), [])
    filename = ""
    if isinstance(source_rows, list) and source_rows and isinstance(source_rows[0], dict):
        filename = str(source_rows[0].get("filename") or "")
    ai_meta = draft.get("_ai") if isinstance(draft.get("_ai"), dict) else {}
    ocr_meta = draft.get("_ocr") if isinstance(draft.get("_ocr"), dict) else {}
    content = {
        "stem": draft.get("stem") or "",
        "options": draft.get("options") or [],
        "answers": draft.get("answers") or [],
        "explanation": draft.get("explanation") or "",
        "rubric": draft.get("rubric") or "",
        "parts": draft.get("parts") or [],
        "images": draft.get("images") or [],
        "bbox": draft.get("bbox") or {},
        "source_page": int(draft.get("source_page") or draft.get("page_no") or 1),
        "score": draft.get("score"),
        "confidence": draft.get("confidence"),
        "source": {
            "kind": "import",
            "import_id": int(import_row["id"]),
            "page_no": int(draft.get("page_no") or 1),
            "filename": filename,
        },
    }
    if draft.get("_source") == "local_ocr":
        content["source"]["origin"] = "local_ocr"
    if ocr_meta:
        content["ocr"] = ocr_meta
    if ai_meta:
        content["ai"] = ai_meta
    return {
        "type": qtype,
        "subject": (draft.get("subject") or import_row.get("subject") or "")[:40],
        "grade": (draft.get("grade") or import_row.get("grade") or "")[:40],
        "difficulty": draft.get("difficulty"),
        "knowledge_tags": draft.get("knowledge_tags") or [],
        "content": content,
        "ai_job_id": job_id,
        "ai_model": model or str(ai_meta.get("model") or ""),
        "prompt_version": prompts.PROMPT_VERSION,
    }


@router.post("/imports/{import_id}/commit")
async def commit_import(
    import_id: int, data: CommitInput, user=Depends(current_user),
):
    """把人工确认的草稿批量写入 ``pl_questions``（幂等：重复 commit 不重复建题）。"""
    import_row = db.fetch_import(import_id, user["id"])
    target = (data.target or "app_bank").strip()
    if target not in {"app_bank", "paper"}:
        raise HTTPException(422, "未知的入库目标")
    if target == "paper":
        raise HTTPException(422, "组卷功能尚未开放，当前仅支持入库到应用题库（app_bank）")
    items = db.list_items(import_id, user["id"])
    by_id = {int(item["id"]): item for item in items}
    selected_ids: list[int] | None = None
    if data.item_ids:
        selected_ids = []
        for raw_id in data.item_ids:
            item = by_id.get(int(raw_id))
            if item is None:
                raise HTTPException(404, f"未找到草稿 {raw_id}")
            if int(item["id"]) not in selected_ids:
                selected_ids.append(int(item["id"]))
    candidates: list[dict] = []
    existing_ids: list[int] = []
    warnings: list[dict] = []
    if selected_ids is not None:
        pool = [by_id[item_id] for item_id in selected_ids]
        explicit = True
    else:
        pool = items
        explicit = False
    for item in pool:
        if item.get("question_id"):
            existing_ids.append(int(item["question_id"]))
            continue
        if item.get("status") == "skipped":
            if selected_ids is not None:
                warnings.append({"item_id": item["id"], "message": "已跳过的草稿不会入库"})
            continue
        if not explicit and item.get("status") != "accepted":
            continue
        candidates.append(item)
    if not candidates:
        if existing_ids:
            return {
                "created_question_ids": [],
                "existing_question_ids": existing_ids,
                "committed": 0,
                "warnings": warnings,
                "import_status": import_row.get("status") or "ready",
            }
        raise HTTPException(422, "没有可入库的题目，请先确认（accept）题目后再入库")
    pages = {int(page["page_no"]): page for page in db.list_pages(import_id, user["id"])}
    prepared: list[tuple[dict, dict, list[str]]] = []
    for item in candidates:
        draft = json_load(item.get("draft_json"), {})
        if not isinstance(draft, dict) or not draft.get("stem"):
            warnings.append({"item_id": item["id"], "message": "草稿缺少题干，已跳过"})
            continue
        try:
            meta = {
                key: draft.get(key) for key in ("_ai", "_ocr", "_source") if draft.get(key)
            }
            draft = schema.normalize_draft(
                draft, page_no=int(item["page_no"]),
                width=int((pages.get(int(item["page_no"])) or {}).get("width") or 0),
                height=int((pages.get(int(item["page_no"])) or {}).get("height") or 0),
                default_subject=import_row.get("subject") or "",
                default_grade=import_row.get("grade") or "",
            )
            draft.update(meta)
        except ValueError as exc:
            raise HTTPException(422, f"第 {item['item_no']} 条草稿不合格：{exc}") from None
        page = pages.get(int(item["page_no"]))
        item_warnings: list[str] = []
        if page is not None:
            draft, item_warnings = await asyncio.to_thread(
                _crop_draft_images, draft, page, user["id"],
            )
        else:
            # P0：无页面图也过滤掉无法归属的旧配图引用，避免把他人资产写进题目。
            draft, item_warnings = assets.sanitize_draft_images(user["id"], draft)
        prepared.append((item, draft, item_warnings))
    if not prepared:
        raise HTTPException(422, "没有可入库的题目，请先确认（accept）题目后再入库")
    created_ids: list[int] = []
    now = utcnow()
    try:
        with connection() as con:
            write_lock(con, "paperlab-commit")
            for item, draft, item_warnings in prepared:
                fresh = con.execute(
                    "SELECT id, question_id, status FROM pl_import_items WHERE id=? AND import_id=? AND user_id=?",
                    (item["id"], import_id, user["id"]),
                ).fetchone()
                if fresh is None:
                    continue
                if fresh["question_id"]:
                    existing_ids.append(int(fresh["question_id"]))
                    continue
                fields = _question_fields_from_draft(
                    draft, import_row, job_id=import_row.get("job_id"),
                    model=str((draft.get("_ai") or {}).get("model") or ""),
                    provider_id=(draft.get("_ai") or {}).get("provider_id"),
                )
                question_id = con.execute(
                    "INSERT INTO pl_questions(user_id,source,type,subject,grade,difficulty,"
                    "knowledge_tags_json,content_json,status,verify_status,version,ai_job_id,"
                    "ai_model,prompt_version,created_at,updated_at,deleted_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)",
                    (
                        user["id"], "import", fields["type"], fields["subject"],
                        fields["grade"], fields["difficulty"],
                        json_dump(fields["knowledge_tags"]),
                        json_dump(fields["content"]), "draft", "pending", 1,
                        fields["ai_job_id"], fields["ai_model"], fields["prompt_version"],
                        now, now,
                    ),
                ).lastrowid
                con.execute(
                    "UPDATE pl_import_items SET question_id=?, status='accepted', needs_review=0, "
                    "review_reason='', warnings_json=?, updated_at=? WHERE id=?",
                    (
                        question_id,
                        json_dump(item_warnings + (json_load(item.get("warnings_json"), []) or [])),
                        now, item["id"],
                    ),
                )
                created_ids.append(int(question_id))
            remaining = con.execute(
                "SELECT COUNT(*) FROM pl_import_items WHERE import_id=? AND user_id=? "
                "AND status<>'skipped' AND question_id IS NULL",
                (import_id, user["id"]),
            ).fetchone()[0]
            import_status = "committed" if int(remaining) == 0 else "ready"
            db.touch_import(con, import_id, user["id"], status=import_status)
    finally:
        pass
    return {
        "created_question_ids": created_ids,
        "existing_question_ids": existing_ids,
        "committed": len(created_ids),
        "warnings": warnings,
        "import_status": import_status,
        "import_id": import_id,
    }


# ---------------------------------------------------------------------------
# paper-import 任务执行器
# ---------------------------------------------------------------------------

def _paperlab_jobs():
    from ..ai import jobs
    return jobs


def _update_job(job_id: int, status: str, stage: str = "") -> None:
    _paperlab_jobs().update_job(job_id, status, stage)


def _publish_event(job_id: int, event: str, payload: dict) -> None:
    try:
        _paperlab_jobs().publish_event(job_id, event, payload)
    except Exception:  # 事件推送失败不影响任务本身
        pass


def _is_cancelled(job_id: int) -> bool:
    try:
        return bool(_paperlab_jobs().is_job_cancelled(job_id))
    except Exception:
        return False


def _get_checkpoint(job_id: int) -> dict:
    try:
        checkpoint = _paperlab_jobs()._job_get_checkpoint(job_id) or {}
    except Exception:
        return {}
    return checkpoint if isinstance(checkpoint, dict) else {}


def _set_checkpoint(job_id: int, checkpoint: dict) -> None:
    try:
        _paperlab_jobs()._job_set_checkpoint(job_id, checkpoint)
    except Exception:
        pass


def _force_checkpoint(job_id: int, checkpoint: dict) -> None:
    """取消后 dispatch_token 过滤会让常规 checkpoint 写入失败，这里强制落盘。"""
    try:
        with connection() as con:
            con.execute(
                "UPDATE ai_jobs SET checkpoint_json=?, updated_at=? WHERE id=?",
                (json_dump(checkpoint), utcnow(), job_id),
            )
    except Exception:
        pass


def _error_text(exc: Exception) -> str:
    if isinstance(exc, HTTPException):
        return str(exc.detail)
    message = str(exc or "").strip()
    return (message or "识别失败")[:300]


async def _recognize_page(
    provider, model: str, page: dict, import_row: dict, *, user_id: int, job_id: int,
) -> dict:
    """单页识别：有页面图走视觉模型，纯文本页（DOCX）走文本模型；严格 JSON 校验。"""
    from ..ai.adapters import call_structured

    page_no = int(page["page_no"])
    total_pages = int(import_row.get("page_count") or 0)
    storage_name = str(page.get("storage_name") or "")
    text_content = str(page.get("text_content") or "")
    width = int(page.get("width") or 0)
    height = int(page.get("height") or 0)
    if not storage_name:
        if not text_content.strip():
            raise ValueError(f"第 {page_no} 页没有可识别的内容")
        prompt = prompts.build_import_text_page_prompt(
            page_no=page_no, total_pages=total_pages, text=text_content,
            subject=str(import_row.get("subject") or ""),
            grade=str(import_row.get("grade") or ""),
        )
        images = None
    else:
        raw = await asyncio.to_thread(storage.read_file, PAGE_FOLDER, storage_name)
        images = [image_part(raw)]
        prompt = prompts.build_import_page_prompt(
            page_no=page_no, total_pages=total_pages, width=width, height=height,
            subject=str(import_row.get("subject") or ""),
            grade=str(import_row.get("grade") or ""),
            text_hint=str(page.get("text_content") or ""),
            # P2：本地 OCR 文字作为辅助提示（prompt 内已明确「机器 OCR 可能出错，以图像为准」）
            ocr_hint=str(page.get("ocr_text") or ""),
        )

    def validator(parsed):
        return schema.normalize_page_result(
            parsed, page_no=page_no, width=width, height=height,
            subject=str(import_row.get("subject") or ""),
            grade=str(import_row.get("grade") or ""),
        )

    validated, _call = await call_structured(
        provider, model, prompt, validator, images=images,
        token_limit=8000, user_id=user_id, job_id=job_id,
    )
    return validated


def _settle_import(import_id: int, user_id: int, *, fallback: str = "failed") -> str:
    """识别结束后按草稿数量落定导入状态：有草稿→ready，否则 fallback。"""
    with connection() as con:
        write_lock(con, "paperlab-import-status")
        row = con.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN status='skipped' THEN 1 ELSE 0 END) AS skipped "
            "FROM pl_import_items WHERE import_id=? AND user_id=?",
            (import_id, user_id),
        ).fetchone()
        total = int(row["total"] or 0) if row is not None else 0
        skipped = int(row["skipped"] or 0) if row is not None else 0
        status = "ready" if total - skipped > 0 else fallback
        db.touch_import(con, import_id, user_id, status=status)
    return status


async def run_import_job(job_id: int, user_id: int, data, provider, verifier=None):
    """``paper-import`` 执行器：逐页识别、逐页落草稿、checkpoint 续跑、支持取消。"""
    params = dict(getattr(data, "params", None) or {})
    import_id = schema.safe_int(params.get("import_id"))
    if not import_id:
        raise ValueError("任务参数缺少 import_id（导入记录）")
    import_row = db.find_import(import_id, user_id)
    if import_row is None:
        raise ValueError("导入记录不存在或没有权限")
    total_pages = int(import_row.get("page_count") or 0)
    if total_pages <= 0:
        raise ValueError("该导入还没有页面，请重新上传")
    page_from = schema.safe_int(params.get("page_from"))
    page_to = schema.safe_int(params.get("page_to"))
    if page_from is None:
        page_from = int(getattr(data, "page_from", 0) or 0) or 1
    if page_to is None:
        page_to = int(getattr(data, "page_to", 0) or 0) or total_pages
    page_from = max(1, int(page_from))
    page_to = min(total_pages, int(page_to))
    if page_from > total_pages:
        raise ValueError("识别起始页超出导入页数")
    if page_to < page_from:
        raise ValueError("页码范围不正确：结束页不能早于起始页")
    if page_to - page_from + 1 > MAX_IMPORT_PAGES:
        raise ValueError(f"单次最多识别 {MAX_IMPORT_PAGES} 页")

    pages = {int(page["page_no"]): page for page in db.list_pages(import_id, user_id)}
    missing_pages = [no for no in range(page_from, page_to + 1) if no not in pages]
    if missing_pages:
        raise ValueError("识别页码范围超出导入页数，请刷新后重试")
    scope = list(range(page_from, page_to + 1))
    total = len(scope)

    checkpoint = _get_checkpoint(job_id)
    if schema.safe_int(checkpoint.get("import_id")) != import_id:
        checkpoint = {}
    done_pages = {int(no) for no in (checkpoint.get("done_pages") or []) if schema.safe_int(no)}
    done_pages &= set(scope)
    failed_pages = {int(no) for no in (checkpoint.get("failed_pages") or []) if schema.safe_int(no)}
    failed_pages &= set(scope) - done_pages
    errors = {
        str(key): str(value)[:300]
        for key, value in (checkpoint.get("errors") or {}).items()
        if schema.safe_int(key) in set(scope)
    }
    warnings: list[str] = []

    # P2 双模型交叉核验：主/核验模型必须不同；核验失败只降级标注，不整任务失败。
    cross_verify_requested = bool(getattr(data, "cross_verify", False))
    cross_verify_enabled = cross_verify_requested
    verify_model = str(getattr(data, "verify_model", "") or "")
    verify_provider_id = schema.safe_int(getattr(data, "verify_provider_id", None))
    if cross_verify_requested:
        if verifier is None or not verify_model:
            cross_verify_enabled = False
            warnings.append(
                "双模型核验未启用：没有可用的核验模型（请重新选择核验连接与模型），"
                "本次结果仅来自主模型"
            )
        else:
            try:
                from ..ai.providers import _identity
                if _identity(verifier, verify_model) == _identity(
                    provider, str(getattr(data, "model", "") or "")
                ):
                    cross_verify_enabled = False
                    warnings.append(
                        "双模型核验未启用：主模型与核验模型相同，请重新选择不同模型；"
                        "本次结果仅来自主模型"
                    )
            except Exception:  # 身份判断失败不影响识别本身
                pass
    if not cross_verify_enabled:
        verify_model = ""
        verify_provider_id = None

    def verify_result_block() -> dict:
        counts = _verify_counts(import_id, user_id)
        notice = ""
        if cross_verify_enabled:
            notice = (
                f"双模型核验完成：一致 {counts['verified']} 题、不一致 {counts['diff']} 题、"
                f"未完成 {counts['failed']} 题"
                + ("；未完成项已降级为主模型结果并标注待复核" if counts["failed"] else "")
            )
        return {
            "enabled": cross_verify_enabled,
            "requested": cross_verify_requested,
            "provider_id": verify_provider_id,
            "model": verify_model,
            "counts": counts,
            "notice": notice,
        }

    def checkpoint_payload() -> dict:
        return {
            "import_id": import_id,
            "page_from": page_from,
            "page_to": page_to,
            "done_pages": sorted(done_pages),
            "failed_pages": sorted(failed_pages),
            "errors": errors,
            "total": total,
            "updated_at": utcnow(),
        }

    done_count = len(done_pages)
    stage_suffix = "（双模型核验）" if cross_verify_enabled else ""
    _update_job(job_id, "running", f"识别 {done_count}/{total} 页{stage_suffix}")
    _publish_event(job_id, "stage", {
        "status": "running", "stage": f"识别 {done_count}/{total} 页{stage_suffix}",
        "done": done_count, "total": total, "cross_verify": cross_verify_enabled,
    })

    for page_no in scope:
        if _is_cancelled(job_id):
            _force_checkpoint(job_id, checkpoint_payload())
            _update_job(job_id, "cancelled", "cancelled")
            _publish_event(job_id, "stage", {
                "status": "cancelled", "stage": "cancelled",
                "done": len(done_pages), "total": total,
            })
            status = await asyncio.to_thread(_settle_import, import_id, user_id, fallback="draft")
            verify_block = await asyncio.to_thread(verify_result_block)
            cancel_warnings = warnings + [
                f"任务已取消：已保存 {len(done_pages)}/{total} 页的识别草稿，可稍后续跑"
            ]
            if cross_verify_enabled and verify_block["counts"]["failed"]:
                cancel_warnings.append("部分页面未完成交叉核验，已标注待复核")
            return {
                "__cancelled__": True,
                "cancelled": True,
                "import_id": import_id,
                "page_count": total,
                "done_pages": sorted(done_pages),
                "failed_pages": sorted(failed_pages),
                "warnings": cancel_warnings,
                "status": status,
                "verify": verify_block,
            }
        if page_no in done_pages:
            continue
        _update_job(job_id, "running", f"识别第 {page_no} 页（{len(done_pages)}/{total}）")
        _publish_event(job_id, "stage", {
            "status": "running", "stage": f"识别第 {page_no} 页（{len(done_pages)}/{total}）",
            "page": page_no, "done": len(done_pages), "total": total,
        })
        try:
            result = await _recognize_page(
                provider, str(getattr(data, "model", "") or ""), pages[page_no],
                import_row, user_id=user_id, job_id=job_id,
            )
            page_verify = None
            if cross_verify_enabled:
                _update_job(job_id, "running", f"核验第 {page_no} 页（{len(done_pages)}/{total}）")
                _publish_event(job_id, "stage", {
                    "status": "running", "stage": f"第 {page_no} 页双模型核验中",
                    "page": page_no, "done": len(done_pages), "total": total,
                })
                page_verify = await _verify_page(
                    verifier, verify_model, pages[page_no], import_row, result,
                    user_id=user_id, job_id=job_id, page_no=page_no,
                )
                if page_verify["status"] == "failed":
                    warnings.append(
                        f"第 {page_no} 页未完成交叉核验：{page_verify['error']}"
                        "（该页结果仅来自主模型，已标注待复核）"
                    )
                elif page_verify.get("count_mismatch"):
                    warnings.append(
                        f"第 {page_no} 页：核验模型识别出 {page_verify['verify_count']} 题，"
                        f"主模型 {page_verify['primary_count']} 题，数量不一致，已标记待复核"
                    )
            saved = await asyncio.to_thread(
                _save_page_items, import_id, user_id, pages[page_no], result,
                model=str(getattr(data, "model", "") or ""),
                provider_id=(provider or {}).get("id") if isinstance(provider, dict) else None,
                job_id=job_id, verify=page_verify,
            )
            done_pages.add(page_no)
            failed_pages.discard(page_no)
            errors.pop(str(page_no), None)
            warnings.extend(saved.get("warnings") or [])
            diff_count = sum(
                1 for entry in (page_verify or {}).get("entries") or []
                if isinstance(entry, dict) and (entry.get("diff") or {}).get("flags")
            )
            _publish_event(job_id, "stage", {
                "status": "running",
                "stage": f"识别 {len(done_pages)}/{total} 页",
                "page": page_no, "state": "recognized",
                "question_count": saved.get("created", 0),
                "skip_count": saved.get("skipped", 0),
                "verify_status": (page_verify or {}).get("status") or "",
                "verify_diff_count": diff_count,
                "done": len(done_pages), "total": total,
            })
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # 单页失败只记录该页，整批继续
            failed_pages.add(page_no)
            errors[str(page_no)] = _error_text(exc)
            _publish_event(job_id, "stage", {
                "status": "running", "stage": f"第 {page_no} 页识别失败，已跳过",
                "page": page_no, "state": "failed", "error": errors[str(page_no)],
                "done": len(done_pages), "total": total,
            })
        done_count = len(done_pages)
        await asyncio.to_thread(_set_checkpoint, job_id, checkpoint_payload())
        _update_job(job_id, "running", f"识别 {done_count}/{total} 页")

    await asyncio.to_thread(_set_checkpoint, job_id, checkpoint_payload())
    status = await asyncio.to_thread(_settle_import, import_id, user_id, fallback="failed")
    if failed_pages:
        detail = "；".join(
            f"第 {page} 页 {errors.get(str(page), '识别失败')}" for page in sorted(failed_pages)
        )
        warnings.append(f"以下页面识别失败：{detail}。其余页面草稿已保存，可只重试失败页。")
    verify_block = await asyncio.to_thread(verify_result_block)
    if cross_verify_enabled:
        if verify_block["counts"]["failed"]:
            warnings.append(verify_block["notice"])
        elif verify_block["counts"]["diff"]:
            warnings.append(
                f"双模型核验发现 {verify_block['counts']['diff']} 题不一致，已标记待复核"
            )
    updated = db.find_import(import_id, user_id) or import_row
    summary = (
        f"导入 {import_row.get('title') or ''} 识别完成：{len(done_pages)}/{total} 页成功，"
        f"共 {len([page for page in done_pages])} 页已生成草稿，状态 {status}。"
    )
    if cross_verify_enabled:
        summary += (
            f"双模型核验：一致 {verify_block['counts']['verified']} 题、"
            f"不一致 {verify_block['counts']['diff']} 题、"
            f"未完成 {verify_block['counts']['failed']} 题。"
        )
    return {
        "import_id": import_id,
        "import": db.public_import(updated),
        "page_count": total,
        "done_pages": sorted(done_pages),
        "failed_pages": sorted(failed_pages),
        "errors": errors,
        "warnings": warnings,
        "notice": "\n".join(warnings),
        "status": status,
        "summary": summary,
        "verify": verify_block,
        "cross_verify": cross_verify_enabled,
    }


def validate_import_job(data, user) -> None:
    """``paper-import`` 准入校验：导入归属、页码范围、页数上限、核验连接独立性。

    ``cross_verify=true`` 时要求主/核验连接不是同一 ``_identity(连接, 模型)``；核验连接
    必须属于本人且模型在该连接内（由 ``_selected_provider`` 校验，失败 404/422）。
    """
    params = data.params if isinstance(getattr(data, "params", None), dict) else {}
    import_id = schema.safe_int(params.get("import_id"))
    if not import_id:
        raise HTTPException(422, "任务参数缺少 import_id（导入记录）")
    import_row = db.find_import(int(import_id), user["id"])
    if import_row is None:
        raise HTTPException(404, "未找到该导入记录")
    total = int(import_row.get("page_count") or 0)
    if total <= 0:
        raise HTTPException(422, "该导入还没有页面，请重新上传")
    start = schema.safe_int(params.get("page_from")) or 1
    end = schema.safe_int(params.get("page_to")) or total
    if start < 1 or start > total:
        raise HTTPException(422, "识别起始页超出导入页数")
    if end < start:
        raise HTTPException(422, "页码范围不正确：结束页不能早于起始页")
    if end > total:
        raise HTTPException(422, "识别结束页超出导入页数")
    if end - start + 1 > MAX_IMPORT_PAGES:
        raise HTTPException(422, f"单次最多识别 {MAX_IMPORT_PAGES} 页，请分批识别")
    if getattr(data, "cross_verify", False):
        verify_provider_id = schema.safe_int(getattr(data, "verify_provider_id", None))
        verify_model = str(getattr(data, "verify_model", "") or "").strip()
        if not verify_provider_id or not verify_model:
            raise HTTPException(422, "请选择独立的核验连接和模型")
        from ..ai.providers import _identity, _selected_provider

        verifier = _selected_provider(user["id"], verify_provider_id, verify_model)
        provider = _selected_provider(user["id"], data.provider_id, data.model)
        if _identity(provider, data.model) == _identity(verifier, verify_model):
            raise HTTPException(
                422, "相同连接和模型重复调用不构成交叉核验，请选择不同模型"
            )
