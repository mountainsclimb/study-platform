"""试卷工坊 P1：切题与答题区标注（页级 ``edit.json`` / 版本 CAS / 重切 / 发布校验）。

对齐 recon/xl-frontend.md §3.4 与 §5（切题系统深挖；上游源码缺失，按规格 + 坐标契约新建）：

- 页级编辑态存 ``pl_page_edits``（``import_id + page_no`` 唯一，``version`` 做 CAS）；
- **坐标系**：box 一律归一化到 ``[0,1]``（相对页图左上角），服务端与页图像素互换时使用
  ``pl_pages.width/height``（= 页图自然尺寸 = ``naturalWidth/naturalHeight``），
  裁图时 clamp 到页图范围并保证最小 8px；
- ``question`` 框裁切为题目图（PNG，内容寻址写 ``paperlab/assets``）并生成/更新
  ``pl_import_items`` 草稿；``answer`` 框作为答题区坐标附加到对应草稿（不裁图）；
- 已入库（``question_id`` 非空）的草稿**不覆盖**，只标记 stale（warnings + reviews，供前端提示）；
- 重切在 ≤20 页时同步执行，超过阈值走 ``paper-annotate`` 任务（复用 ``register_task_handler``）；
- 发布接口只做“状态统计 + 发布前校验”，不复制既有 commit 语义（真正入库仍走 digitize 的 commit）。
"""
from __future__ import annotations

import asyncio
import math
from io import BytesIO

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import storage
from ..auth import current_user
from ..db import connection, utcnow, write_lock
from . import assets, db, schema
from .db import MAX_IMPORT_ITEMS, PAGE_FOLDER, json_dump, json_load

router = APIRouter()

BOX_KINDS = ("question", "answer")
MAX_BOXES_PER_PAGE = 200
MAX_BOX_ID = 80
MAX_BOX_LABEL = 80
MAX_SCORE = 1000.0
MAX_WARNINGS = 40
MIN_CROP_PX = 8
MAX_SYNC_RESEGMENT_PAGES = 20
RESEGMENT_JOB_TASK = "paper-annotate"
ANNOTATE_SOURCE = "annotation"
STALE_MESSAGE = "stale：切题框已变化，该题已入库，未覆盖草稿"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pl_page_edits (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    import_id INTEGER NOT NULL REFERENCES pl_imports(id) ON DELETE CASCADE,
    page_no INTEGER NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    data_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL,
    UNIQUE(import_id, page_no)
);
CREATE INDEX IF NOT EXISTS pl_page_edits_user ON pl_page_edits(user_id, import_id);
"""


def ensure_tables() -> None:
    """幂等建表：由 ``init_paperlab()`` 在既有表之后调用。"""
    with connection() as con:
        con.executescript(_SCHEMA)


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------

def _row_dict(row) -> dict:
    if isinstance(row, dict):
        return row
    try:
        return dict(row)
    except Exception:  # noqa: BLE001 - 行类型交由调用方决定
        return {}


def _int(value, default: int = 0) -> int:
    parsed = schema.safe_int(value)
    return int(parsed) if parsed is not None else default


def _finite(value: float | None) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return value


def _page_public(row: dict) -> dict:
    page_no = int(row.get("page_no") or 0)
    width = int(row.get("width") or 0)
    height = int(row.get("height") or 0)
    storage_name = str(row.get("storage_name") or "")
    return {
        "page_no": page_no,
        "width": width,
        "height": height,
        # 前端 fabric/canvas 需要“页图自然尺寸”做缩放基准；与 width/height 同源。
        "naturalWidth": width,
        "naturalHeight": height,
        "image_url": db.page_image_url(row["import_id"], page_no) if storage_name else None,
        "has_text": bool(str(row.get("text_content") or "").strip()),
    }


def _edit_public(row: dict | None) -> dict:
    if not row:
        return {"version": 0, "boxes": [], "updated_at": None, "edited": False}
    data = json_load(row.get("data_json"), {})
    if not isinstance(data, dict):
        data = {}
    boxes = data.get("boxes") if isinstance(data.get("boxes"), list) else []
    return {
        "version": int(row.get("version") or 0),
        "boxes": boxes,
        "updated_at": row.get("updated_at"),
        "edited": True,
    }


def _find_edit(import_id: int, page_no: int, user_id: int) -> dict | None:
    with connection() as con:
        row = con.execute(
            "SELECT * FROM pl_page_edits WHERE import_id=? AND page_no=? AND user_id=?",
            (import_id, page_no, user_id),
        ).fetchone()
    return _row_dict(row) if row is not None else None


# ---------------------------------------------------------------------------
# box 校验 / 归一化
# ---------------------------------------------------------------------------

def _normalize_box(raw, *, index: int, kind_counts: dict[str, int], used_ids: set[str],
                   label_prefix: str = "切题框") -> tuple[dict, list[str]]:
    """校验并归一化单个切题框；非法抛 422（消息含中文字段名）。"""
    warnings: list[str] = []
    if not isinstance(raw, dict):
        raise HTTPException(422, "第 %d 个%s必须是 JSON 对象" % (index + 1, label_prefix))
    kind = str(raw.get("kind") or raw.get("type") or "").strip().lower()
    if kind not in BOX_KINDS:
        raise HTTPException(422, "第 %d 个切题框 kind 只能是 question 或 answer" % (index + 1))
    coords: dict[str, float] = {}
    for name, label in (("x", "x"), ("y", "y"), ("w", "宽 w"), ("h", "高 h")):
        value = _finite(schema.safe_float(raw.get(name)))
        if value is None:
            raise HTTPException(422, "第 %d 个切题框缺少合法坐标 %s" % (index + 1, label))
        coords[name] = value
    x, y, w, h = coords["x"], coords["y"], coords["w"], coords["h"]
    # 归一化坐标：0≤x,y,w,h≤1，且框必须落在页面内；容忍 0.001 的浮点误差后 clamp。
    tolerance = 0.001
    if x < -tolerance or y < -tolerance or w <= 0 or h <= 0 or w > 1 + tolerance or h > 1 + tolerance:
        raise HTTPException(422, "第 %d 个切题框坐标需满足 0≤x,y,w,h≤1 且 w/h>0" % (index + 1))
    x = max(0.0, min(1.0, x))
    y = max(0.0, min(1.0, y))
    w = max(0.0, min(1.0, w))
    h = max(0.0, min(1.0, h))
    if x + w > 1 + tolerance or y + h > 1 + tolerance:
        raise HTTPException(422, "第 %d 个切题框超出页面范围（x+w 或 y+h 大于 1）" % (index + 1))
    w = min(w, 1.0 - x)
    h = min(h, 1.0 - y)
    if w <= 0.0 or h <= 0.0:
        raise HTTPException(422, "第 %d 个切题框坐标退化（宽或高为 0）" % (index + 1))

    raw_id = str(raw.get("id") or "").strip()
    raw_id = "".join(char for char in raw_id if char.isprintable())[:MAX_BOX_ID]
    if not raw_id:
        kind_counts[kind] = kind_counts.get(kind, 0) + 1
        raw_id = "%s_%d" % ("q" if kind == "question" else "a", kind_counts[kind])
    box_id = raw_id
    suffix = 2
    while box_id in used_ids:
        box_id = "%s_%d" % (raw_id[: MAX_BOX_ID - 8], suffix)
        suffix += 1
        if suffix > 10000:  # pragma: no cover - 防御性
            raise HTTPException(422, "切题框 id 冲突过多")
    used_ids.add(box_id)

    box: dict = {
        "id": box_id,
        "kind": kind,
        "x": round(x, 4),
        "y": round(y, 4),
        "w": round(w, 4),
        "h": round(h, 4),
    }
    label = raw.get("label")
    if isinstance(label, str) and label.strip():
        box["label"] = label.strip()[:MAX_BOX_LABEL]
    item_id = schema.safe_int(raw.get("item_id"))
    if item_id is not None and item_id > 0:
        box["item_id"] = int(item_id)
    score = _finite(schema.safe_float(raw.get("score")))
    if score is not None:
        if score < 0 or score > MAX_SCORE:
            raise HTTPException(422, "第 %d 个切题框分值需在 0-%g 之间" % (index + 1, MAX_SCORE))
        box["score"] = round(score, 2)
    return box, warnings


def _overlap_ratio(a: dict, b: dict) -> float:
    left = max(a["x"], b["x"])
    top = max(a["y"], b["y"])
    right = min(a["x"] + a["w"], b["x"] + b["w"])
    bottom = min(a["y"] + a["h"], b["y"] + b["h"])
    if right <= left or bottom <= top:
        return 0.0
    inter = (right - left) * (bottom - top)
    smaller = min(a["w"] * a["h"], b["w"] * b["h"]) or 1e-9
    return inter / smaller


def normalize_boxes(raw_boxes) -> tuple[list[dict], list[str]]:
    """校验整页 boxes 数组，返回 ``(boxes, warnings)``；非法抛 422。"""
    if not isinstance(raw_boxes, list):
        raise HTTPException(422, "boxes 必须是数组")
    if len(raw_boxes) > MAX_BOXES_PER_PAGE:
        raise HTTPException(422, "单页切题框不能超过 %d 个" % MAX_BOXES_PER_PAGE)
    boxes: list[dict] = []
    kind_counts: dict[str, int] = {}
    used_ids: set[str] = set()
    for index, raw in enumerate(raw_boxes):
        box, _warnings = _normalize_box(raw, index=index, kind_counts=kind_counts, used_ids=used_ids)
        boxes.append(box)
    warnings: list[str] = []
    for first in range(len(boxes)):
        for second in range(first + 1, len(boxes)):
            left, right = boxes[first], boxes[second]
            if left["kind"] != right["kind"]:
                continue
            ratio = _overlap_ratio(left, right)
            if ratio > 0.5:
                warnings.append(
                    "同类型切题框重叠：%s 与 %s（重叠比例 %.0f%%，已容忍，请确认）"
                    % (left["id"], right["id"], ratio * 100)
                )
    return boxes, warnings[:MAX_WARNINGS]


def normalize_single_box(raw, *, kind: str = "answer") -> dict:
    """单框校验（用于 answer-box 更新）；kind 强制为 answer。"""
    if not isinstance(raw, dict):
        raise HTTPException(422, "答题区 box 必须是 JSON 对象")
    payload = dict(raw)
    payload["kind"] = kind
    boxes, _warnings = normalize_boxes([payload])
    return boxes[0]


# ---------------------------------------------------------------------------
# 页级 annotation 读写
# ---------------------------------------------------------------------------

def _edit_data(boxes: list[dict], page: dict, *, updated_at: str) -> dict:
    return {
        "boxes": boxes,
        "page": {
            "page_no": int(page.get("page_no") or 0),
            "width": int(page.get("width") or 0),
            "height": int(page.get("height") or 0),
        },
        "updated_at": updated_at,
    }


def _import_or_404(import_id: int, user_id: int) -> dict:
    return db.fetch_import(import_id, user_id)


def _page_or_404(import_id: int, page_no: int, user_id: int) -> dict:
    return db.fetch_page(import_id, page_no, user_id)


@router.get("/imports/{import_id}/pages/{page_no}/annotation")
def get_annotation(import_id: int, page_no: int, user=Depends(current_user)):
    """读取页级切题标注；从未编辑过返回空 boxes + version=0。"""
    _import_or_404(import_id, user["id"])
    page = _page_or_404(import_id, page_no, user["id"])
    edit = _find_edit(import_id, page_no, user["id"])
    return {"page": _page_public(page), "edit": _edit_public(edit)}


class AnnotationSaveInput(BaseModel):
    version: int = Field(ge=0)
    boxes: list = Field(default_factory=list)


@router.put("/imports/{import_id}/pages/{page_no}/annotation")
def save_annotation(import_id: int, page_no: int, data: AnnotationSaveInput,
                    user=Depends(current_user)):
    """保存整页切题框（version CAS；冲突 409 返回当前 version/updated_at）。"""
    _import_or_404(import_id, user["id"])
    page = _page_or_404(import_id, page_no, user["id"])
    if not str(page.get("storage_name") or ""):
        raise HTTPException(422, "该页为纯文本页，没有页面图，无法标注切题框")
    boxes, warnings = normalize_boxes(data.boxes)
    linked_ids = sorted({int(box["item_id"]) for box in boxes if box.get("item_id")})
    if linked_ids:
        owned_ids = {int(item["id"]) for item in db.list_items(import_id, user["id"])}
        missing = [item_id for item_id in linked_ids if item_id not in owned_ids]
        if missing:
            raise HTTPException(422, "切题框关联的草稿不存在：%s" % "、".join(str(item) for item in missing[:5]))
    now = utcnow()
    payload = json_dump(_edit_data(boxes, page, updated_at=now))
    with connection() as con:
        write_lock(con, "paperlab-annotate")
        existing = con.execute(
            "SELECT * FROM pl_page_edits WHERE import_id=? AND page_no=? AND user_id=?",
            (import_id, page_no, user["id"]),
        ).fetchone()
        current = _row_dict(existing) if existing is not None else None
        if current is None:
            if int(data.version) != 0:
                _raise_version_conflict(0, None)
            con.execute(
                "INSERT INTO pl_page_edits(user_id,import_id,page_no,version,data_json,updated_at) "
                "VALUES (?,?,?,?,?,?)",
                (user["id"], import_id, page_no, 1, payload, now),
            )
            version = 1
        else:
            current_version = int(current.get("version") or 0)
            if current_version != int(data.version):
                _raise_version_conflict(current_version, current.get("updated_at"))
            changed = con.execute(
                "UPDATE pl_page_edits SET version=version+1, data_json=?, updated_at=? "
                "WHERE id=? AND user_id=? AND version=?",
                (payload, now, int(current["id"]), user["id"], current_version),
            ).rowcount
            if not changed:  # pragma: no cover - 写入锁下不应发生
                _raise_version_conflict(current_version, current.get("updated_at"))
            version = current_version + 1
        db.touch_import(con, import_id, user["id"])
    return {
        "page": _page_public(page),
        "edit": {"version": version, "boxes": boxes, "updated_at": now, "edited": True},
        "warnings": warnings,
    }


def _raise_version_conflict(version: int, updated_at) -> None:
    detail = {
        "message": "切题标注已被他人修改，请刷新后重试",
        "version": int(version),
        "updated_at": updated_at,
    }
    raise HTTPException(
        409,
        detail=detail,
        headers={
            "X-Edit-Version": str(int(version)),
            "X-Edit-Updated-At": str(updated_at or ""),
        },
    )


@router.get("/imports/{import_id}/pages")
def list_page_annotations(import_id: int, user=Depends(current_user)):
    """页列表 + 每页标注概况（box 数 / 是否编辑过 / 版本），供切题编辑器左栏使用。"""
    _import_or_404(import_id, user["id"])
    pages = db.list_pages(import_id, user["id"])
    edits: dict[int, dict] = {}
    with connection() as con:
        rows = con.execute(
            "SELECT * FROM pl_page_edits WHERE import_id=? AND user_id=?",
            (import_id, user["id"]),
        ).fetchall()
    for row in rows:
        payload = _row_dict(row)
        edits[int(payload.get("page_no") or 0)] = payload
    items = db.list_items(import_id, user["id"])
    item_counts: dict[int, dict] = {}
    for item in items:
        page_no = int(item.get("page_no") or 0)
        bucket = item_counts.setdefault(page_no, {"items": 0, "accepted": 0, "committed": 0})
        bucket["items"] += 1
        if item.get("status") == "accepted":
            bucket["accepted"] += 1
        if item.get("question_id"):
            bucket["committed"] += 1
    result = []
    for page in pages:
        page_no = int(page.get("page_no") or 0)
        edit_row = edits.get(page_no)
        boxes = []
        if edit_row:
            data = json_load(edit_row.get("data_json"), {})
            if isinstance(data, dict) and isinstance(data.get("boxes"), list):
                boxes = data["boxes"]
        result.append({
            **_page_public(page),
            "annotation": {
                "edited": bool(edit_row),
                "version": int((edit_row or {}).get("version") or 0),
                "updated_at": (edit_row or {}).get("updated_at"),
                "box_count": len(boxes),
                "question_count": sum(1 for box in boxes if isinstance(box, dict) and box.get("kind") == "question"),
                "answer_count": sum(1 for box in boxes if isinstance(box, dict) and box.get("kind") == "answer"),
                "items": item_counts.get(page_no, {"items": 0, "accepted": 0, "committed": 0}),
            },
        })
    return {"items": result, "total": len(result)}


# ---------------------------------------------------------------------------
# 裁图
# ---------------------------------------------------------------------------

def _crop_box(image, box: dict) -> tuple[bytes, int, int]:
    """按归一化 box 从页图裁切 PNG；像素坐标 clamp，宽高至少 ``MIN_CROP_PX``。"""
    width, height = image.size
    if width <= 0 or height <= 0:  # pragma: no cover - Pillow 不可能给出 0
        raise ValueError("页面尺寸无效")

    def _span(start_ratio: float, size_ratio: float, total: int) -> tuple[int, int]:
        left = int(round(start_ratio * total))
        right = int(round((start_ratio + size_ratio) * total))
        left = max(0, min(total, left))
        right = max(0, min(total, right))
        if right - left < MIN_CROP_PX:
            center = (left + right) // 2
            left = max(0, center - MIN_CROP_PX // 2)
            right = min(total, left + MIN_CROP_PX)
            left = max(0, right - MIN_CROP_PX)
        return left, right

    left, right = _span(box["x"], box["w"], width)
    top, bottom = _span(box["y"], box["h"], height)
    if right - left < MIN_CROP_PX or bottom - top < MIN_CROP_PX:  # pragma: no cover - 页面极小
        raise ValueError("切图区域太小")
    with image.crop((left, top, right, bottom)) as crop:
        output = crop.convert("RGB") if crop.mode != "RGB" else crop.copy()
        try:
            buffer = BytesIO()
            output.save(buffer, format="PNG", optimize=True)
            data = buffer.getvalue()
        finally:
            if output is not crop:
                try:
                    output.close()
                except Exception:  # noqa: BLE001
                    pass
    return data, right - left, bottom - top


def _read_page_image(page: dict):
    storage_name = str(page.get("storage_name") or "")
    if not storage_name:
        raise ValueError("该页为纯文本页，没有页面图")
    raw = storage.read_file(PAGE_FOLDER, storage_name)
    from PIL import Image

    return Image.open(BytesIO(raw))


# ---------------------------------------------------------------------------
# 重切：按页 boxes 生成/更新 import items
# ---------------------------------------------------------------------------

def _draft_bbox(item: dict) -> dict:
    draft = json_load(item.get("draft_json"), {})
    if isinstance(draft, dict) and isinstance(draft.get("bbox"), dict):
        return draft["bbox"]
    bbox = json_load(item.get("bbox_json"), {})
    return bbox if isinstance(bbox, dict) else {}


def _bbox_similar(a: dict, b: dict) -> float:
    """比较两个归一化 bbox 的近似度（IoU，退化时用中心点距离判定）。"""
    ratio = _overlap_ratio(
        {"x": a["x"], "y": a["y"], "w": max(a["w"], 1e-6), "h": max(a["h"], 1e-6)},
        {"x": b["x"], "y": b["y"], "w": max(b["w"], 1e-6), "h": max(b["h"], 1e-6)},
    )
    return ratio


def _find_matching_item(items: list[dict], box: dict, page_no: int) -> dict | None:
    candidates = [item for item in items if int(item.get("page_no") or 0) == page_no]
    best: tuple[float, dict] | None = None
    for item in candidates:
        bbox = _draft_bbox(item)
        if not isinstance(bbox.get("x"), (int, float)):
            continue
        ratio = _bbox_similar({
            "x": float(bbox.get("x") or 0), "y": float(bbox.get("y") or 0),
            "w": float(bbox.get("w") or 0), "h": float(bbox.get("h") or 0),
        }, box)
        if ratio >= 0.9 and (best is None or ratio > best[0]):
            best = (ratio, item)
    return best[1] if best else None


def _answer_boxes_for(question_box: dict, answer_boxes: list[dict]) -> list[dict]:
    selected: list[dict] = []
    cx = question_box["x"] + question_box["w"] / 2
    cy = question_box["y"] + question_box["h"] / 2
    for box in answer_boxes:
        point_x = box["x"] + box["w"] / 2
        point_y = box["y"] + box["h"] / 2
        if question_box["x"] <= point_x <= question_box["x"] + question_box["w"] and \
                question_box["y"] <= point_y <= question_box["y"] + question_box["h"]:
            selected.append(box)
            continue
        if (box["x"] + box["w"] > question_box["x"] and box["x"] < question_box["x"] + question_box["w"]
                and box["y"] + box["h"] > question_box["y"] and box["y"] < question_box["y"] + question_box["h"]):
            selected.append(box)
    if not selected:
        nearest: tuple[float, dict] | None = None
        for box in answer_boxes:
            distance = math.hypot((box["x"] + box["w"] / 2) - cx, (box["y"] + box["h"] / 2) - cy)
            if nearest is None or distance < nearest[0]:
                nearest = (distance, box)
        if nearest is not None and nearest[0] <= 0.35:
            selected.append(nearest[1])
    return selected[:8]


def _answer_public(boxes: list[dict]) -> dict | None:
    if not boxes:
        return None
    return {key: boxes[0][key] for key in ("x", "y", "w", "h")}


def _placeholder_stem(page_no: int, index: int) -> str:
    return "第 %d 页题框 %d（题干待补全）" % (page_no, index)


def _build_annotation_draft(*, existing: dict | None, page: dict, question_box: dict,
                            answer_boxes: list[dict], asset: str, pixel_width: int,
                            pixel_height: int, now: str, index: int = 1) -> dict:
    draft = dict(existing or {})
    page_no = int(page.get("page_no") or 1)
    if not str(draft.get("stem") or "").strip():
        draft["stem"] = str(question_box.get("label") or "").strip() or _placeholder_stem(page_no, index)
    draft.setdefault("type", "SUBJECTIVE")
    draft.setdefault("options", [])
    draft.setdefault("answers", [])
    draft.setdefault("explanation", "")
    draft.setdefault("rubric", "")
    draft.setdefault("knowledge_tags", [])
    draft.setdefault("difficulty", None)
    if question_box.get("score") is not None:
        draft["score"] = question_box["score"]
    draft["page_no"] = page_no
    draft["source_page"] = page_no
    draft["bbox"] = {key: question_box[key] for key in ("x", "y", "w", "h")}
    images = [image for image in (draft.get("images") or [])
              if isinstance(image, dict) and image.get("role") != "stem"]
    images.insert(0, {
        "bbox": draft["bbox"],
        "asset": asset,
        "media_type": "image/png",
        "width": int(pixel_width),
        "height": int(pixel_height),
        "role": "stem",
        "source": ANNOTATE_SOURCE,
    })
    draft["images"] = images[:20]
    answer_area = _answer_public(answer_boxes)
    if answer_area is not None:
        draft["answer_box"] = answer_area
        draft["answer_boxes"] = [
            {key: box[key] for key in ("x", "y", "w", "h")} for box in answer_boxes
        ]
    else:
        draft.pop("answer_box", None)
        draft.pop("answer_boxes", None)
    annotation = draft.get("_annotation") if isinstance(draft.get("_annotation"), dict) else {}
    annotation.update({
        "source": ANNOTATE_SOURCE,
        "kind": "question",
        "page_no": page_no,
        "box_id": question_box["id"],
        "label": question_box.get("label") or "",
        "updated_at": now,
    })
    draft["_annotation"] = annotation
    if not isinstance(draft.get("confidence"), (int, float)):
        draft["confidence"] = None
    return draft


def _stale_item(item: dict, *, box: dict | None, now: str, reason: str = STALE_MESSAGE,
                user_id: int | None = None) -> None:
    """已入库草稿：不覆盖内容，只写 stale 提示（warnings + review_reason + needs_review）。"""
    warnings = json_load(item.get("warnings_json"), [])
    if not isinstance(warnings, list):
        warnings = []
    message = reason
    if box is not None:
        message += "（建议新框 x=%.3f y=%.3f w=%.3f h=%.3f）" % (box["x"], box["y"], box["w"], box["h"])
    warnings = [str(entry) for entry in warnings if not str(entry).startswith("stale：")]
    warnings.append(message)
    owner_id = int(user_id if user_id is not None else item.get("user_id") or 0)
    with connection() as con:
        write_lock(con, "paperlab-annotate")
        con.execute(
            "UPDATE pl_import_items SET needs_review=1, review_reason=?, warnings_json=?, updated_at=? "
            "WHERE id=? AND import_id=? AND user_id=?",
            (message[:500], json_dump(warnings[-MAX_WARNINGS:]), now, int(item["id"]),
             int(item["import_id"]), owner_id),
        )
        db.touch_import(con, int(item["import_id"]), owner_id)


def _page_resegment(page_no: int, import_row: dict, user_id: int, *, keep_items: bool) -> dict:
    """对单页执行重切（同步实现，可在 to_thread / 任务执行器中调用）。"""
    import_id = int(import_row["id"])
    page = db.fetch_page(import_id, page_no, user_id)
    edit = _find_edit(import_id, page_no, user_id)
    edit_data = json_load((edit or {}).get("data_json"), {})
    boxes = edit_data.get("boxes") if isinstance(edit_data, dict) else []
    if not isinstance(boxes, list) or not boxes:
        return {"page_no": page_no, "created": 0, "updated": 0, "skipped": 0, "stale": 0,
                "warnings": ["第 %d 页没有切题框，已跳过（请先在标注接口保存 boxes）" % page_no],
                "stale_items": []}
    question_boxes = [box for box in boxes if isinstance(box, dict) and box.get("kind") == "question"]
    answer_boxes = [box for box in boxes if isinstance(box, dict) and box.get("kind") == "answer"]
    if not question_boxes:
        return {"page_no": page_no, "created": 0, "updated": 0, "skipped": 0, "stale": 0,
                "warnings": ["第 %d 页没有 question 切题框，已跳过" % page_no], "stale_items": []}
    warnings: list[str] = []
    created = updated = skipped = stale = 0
    stale_items: list[dict] = []
    now = utcnow()
    try:
        page_image = _read_page_image(page)
    except ValueError as exc:
        return {"page_no": page_no, "created": 0, "updated": 0, "skipped": len(question_boxes),
                "stale": 0, "warnings": ["第 %d 页：%s" % (page_no, exc)], "stale_items": []}
    except Exception:
        return {"page_no": page_no, "created": 0, "updated": 0, "skipped": len(question_boxes),
                "stale": 0, "warnings": ["第 %d 页页面图读取失败，未重切" % page_no], "stale_items": []}
    items = db.list_items(import_id, user_id)
    touched_ids: set[int] = set()
    stale_committed: set[int] = set()
    try:
        for index, question_box in enumerate(sorted(question_boxes, key=lambda item: (item["y"], item["x"])), 1):
            matched = None
            explicit_item_id = question_box.get("item_id")
            if explicit_item_id:
                matched = next((item for item in items if int(item["id"]) == int(explicit_item_id)), None)
                if matched is None:
                    warnings.append("第 %d 页：题框 %s 的 item_id=%s 不存在，已改为新建草稿"
                                    % (page_no, question_box["id"], explicit_item_id))
            if matched is None:
                matched = _find_matching_item(items, question_box, page_no)
            answers = _answer_boxes_for(question_box, answer_boxes)
            if matched is not None and matched.get("question_id"):
                # 已入库：绝不覆盖题目内容，只标记 stale。
                stale += 1
                stale_committed.add(int(matched["id"]))
                _stale_item(item={**matched, "user_id": user_id}, box=question_box, now=now,
                            user_id=user_id)
                stale_items.append({
                    "item_id": int(matched["id"]), "page_no": page_no,
                    "question_id": int(matched["question_id"]), "box": question_box,
                })
                continue
            try:
                data, pixel_width, pixel_height = _crop_box(page_image, question_box)
            except Exception as exc:  # noqa: BLE001 - 单框失败不影响整页
                warnings.append("第 %d 页：题框 %s 裁切失败（%s）" % (page_no, question_box["id"], exc))
                skipped += 1
                continue
            # P0：裁切图登记为服务端签发的 asset（content 只存 asset_id），不再直接暴露存储文件名。
            try:
                asset = assets.save_asset_bytes(
                    int(user_id), data, media_type="image/png", purpose="annotation_crop",
                )
            except Exception:  # noqa: BLE001 - 存储失败按跳过处理，不写半成品
                warnings.append("第 %d 页：题框 %s 裁切图保存失败" % (page_no, question_box["id"]))
                skipped += 1
                continue
            existing_draft = json_load(matched.get("draft_json"), {}) if matched else {}
            draft = _build_annotation_draft(
                existing=existing_draft if isinstance(existing_draft, dict) else {},
                page=page, question_box=question_box,
                answer_boxes=answers, asset=asset, pixel_width=pixel_width,
                pixel_height=pixel_height, now=now, index=index,
            )
            if matched is not None:
                _update_annotation_item(item=matched, user_id=user_id, draft=draft,
                                        question_box=question_box, now=now)
                touched_ids.add(int(matched["id"]))
                updated += 1
                continue
            if len(items) + created + 1 > MAX_IMPORT_ITEMS:
                warnings.append("草稿超过 %d 条上限，其余题框未生成" % MAX_IMPORT_ITEMS)
                skipped += 1
                continue
            new_id = _insert_annotation_item(import_id=import_id, user_id=user_id, page=page,
                                             draft=draft, question_box=question_box, now=now)
            touched_ids.add(new_id)
            items.append({"id": new_id, "import_id": import_id, "page_no": page_no,
                          "item_no": 0, "draft_json": json_dump(draft), "bbox_json": json_dump(draft["bbox"]),
                          "question_id": None, "status": "pending", "user_id": user_id,
                          "warnings_json": "[]", "needs_review": 1})
            created += 1
        # 已入库但当前页已没有对应题框的题目：也标 stale（不改内容）
        for item in items:
            if int(item.get("page_no") or 0) != page_no or not item.get("question_id"):
                continue
            if int(item["id"]) in stale_committed or int(item["id"]) in touched_ids:
                continue
            item_bbox = _draft_bbox(item)
            if not isinstance(item_bbox, dict) or "x" not in item_bbox:
                continue
            matched_any = False
            for question_box in question_boxes:
                try:
                    if _bbox_similar({
                        "x": float(item_bbox.get("x") or 0), "y": float(item_bbox.get("y") or 0),
                        "w": float(item_bbox.get("w") or 0), "h": float(item_bbox.get("h") or 0),
                    }, question_box) >= 0.9:
                        matched_any = True
                        break
                except (TypeError, ValueError):
                    continue
            if not matched_any:
                stale += 1
                stale_committed.add(int(item["id"]))
                _stale_item(item={**item, "user_id": user_id}, box=None, now=now,
                            reason="stale：当前页已没有与该题匹配的切题框，已入库题目未改动",
                            user_id=user_id)
                stale_items.append({"item_id": int(item["id"]), "page_no": page_no,
                                    "question_id": int(item["question_id"]), "box": None})
        if not keep_items:
            removable = [
                int(item["id"]) for item in items
                if int(item.get("page_no") or 0) == page_no
                and not item.get("question_id")
                and int(item["id"]) not in touched_ids
                and (item.get("status") or "pending") != "skipped"
            ]
            if removable:
                _delete_items(import_id, user_id, removable)
    finally:
        try:
            page_image.close()
        except Exception:  # noqa: BLE001
            pass
    return {
        "page_no": page_no,
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "stale": stale,
        "warnings": warnings[:MAX_WARNINGS],
        "stale_items": stale_items,
    }


def _insert_annotation_item(*, import_id: int, user_id: int, page: dict, draft: dict,
                            question_box: dict, now: str) -> int:
    with connection() as con:
        write_lock(con, "paperlab-annotate")
        item_no = int(con.execute(
            "SELECT COALESCE(MAX(item_no),0) FROM pl_import_items WHERE import_id=?",
            (import_id,),
        ).fetchone()[0] or 0) + 1
        item_id = con.execute(
            "INSERT INTO pl_import_items(user_id,import_id,page_no,item_no,bbox_json,draft_json,"
            "status,needs_review,review_reason,skip_reason,confidence,source,warnings_json,"
            "question_id,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?,?)",
            (
                user_id, import_id, int(page["page_no"]), item_no,
                json_dump(draft.get("bbox") or {}), json_dump(draft),
                "pending", 1, "切题草稿：请人工补全题干/题型后确认",
                "", None, "manual", json_dump(["由切题标注生成，题干为占位，提交入库前请补全"]),
                now, now,
            ),
        ).lastrowid
        db.touch_import(con, import_id, user_id)
    return int(item_id)


def _update_annotation_item(*, item: dict, user_id: int, draft: dict,
                            question_box: dict, now: str) -> None:
    review_reason = str(item.get("review_reason") or "")
    needs_review = int(item.get("needs_review") or 0)
    if (item.get("status") or "pending") != "skipped":
        needs_review = 1
        review_reason = "切题框已更新，请复核题干与裁图"
    with connection() as local_con:
        write_lock(local_con, "paperlab-annotate")
        local_con.execute(
            "UPDATE pl_import_items SET bbox_json=?, draft_json=?, needs_review=?, review_reason=?, "
            "source='manual', updated_at=? WHERE id=? AND import_id=? AND user_id=?",
            (
                json_dump(draft.get("bbox") or {}), json_dump(draft), needs_review,
                review_reason[:500], now, int(item["id"]), int(item["import_id"]), int(user_id),
            ),
        )
        db.touch_import(local_con, int(item["import_id"]), int(user_id))


def _delete_items(import_id: int, user_id: int, item_ids: list[int]) -> None:
    with connection() as con:
        write_lock(con, "paperlab-annotate")
        placeholders = ",".join("?" for _ in item_ids)
        con.execute(
            "DELETE FROM pl_import_items WHERE import_id=? AND user_id=? AND question_id IS NULL "
            "AND id IN (%s)" % placeholders,
            (import_id, user_id, *item_ids),
        )
        db.touch_import(con, import_id, user_id)


def resegment_import(import_id: int, user_id: int, pages: list[int] | None = None,
                     *, keep_items: bool = True) -> dict:
    """同步重切多页，返回聚合统计。"""
    import_row = db.fetch_import(import_id, user_id)
    page_rows = db.list_pages(import_id, user_id)
    available = [int(page["page_no"]) for page in page_rows]
    if not available:
        raise HTTPException(422, "该导入还没有页面，请重新上传")
    if pages:
        invalid = [page for page in pages if page not in available]
        if invalid:
            raise HTTPException(404, "未找到第 %s 页" % "、".join(str(page) for page in invalid))
        target_pages = sorted({int(page) for page in pages})
    else:
        target_pages = available
    if len(target_pages) > MAX_SYNC_RESEGMENT_PAGES:
        raise HTTPException(422, "单次重切最多 %d 页，请分批操作" % MAX_SYNC_RESEGMENT_PAGES)
    totals = {"created": 0, "updated": 0, "skipped": 0, "stale": 0}
    warnings: list[str] = []
    stale_items: list[dict] = []
    page_results: list[dict] = []
    for page_no in target_pages:
        result = _page_resegment(page_no, import_row, user_id, keep_items=keep_items)
        for key in totals:
            totals[key] += int(result.get(key) or 0)
        warnings.extend(result.get("warnings") or [])
        stale_items.extend(result.get("stale_items") or [])
        page_results.append({
            "page_no": result.get("page_no"),
            "created": result.get("created"),
            "updated": result.get("updated"),
            "skipped": result.get("skipped"),
            "stale": result.get("stale"),
        })
    return {
        "import_id": import_id,
        "pages": target_pages,
        "keep_items": bool(keep_items),
        "created": totals["created"],
        "updated": totals["updated"],
        "skipped": totals["skipped"],
        "stale": totals["stale"],
        "summary": "重切完成：新建 %d，更新 %d，跳过 %d，stale %d"
                   % (totals["created"], totals["updated"], totals["skipped"], totals["stale"]),
        "page_results": page_results,
        "warnings": warnings[:MAX_WARNINGS],
        "stale_items": stale_items[:MAX_BOXES_PER_PAGE],
    }


class ResegmentInput(BaseModel):
    pages: list[int] | None = Field(default=None, max_length=20)
    keep_items: bool = True
    # 仅当页数超过同步阈值（正常导入 ≤20 页不会触发）走异步任务时需要任务入参：
    provider_id: int | None = Field(default=None, ge=1)
    model: str | None = Field(default=None, max_length=160)


@router.post("/imports/{import_id}/resegment")
async def resegment(import_id: int, data: ResegmentInput, user=Depends(current_user)):
    """按页级切题框重切：裁题目图 + 生成/更新草稿；页数超阈值走 ``paper-annotate`` 任务。"""
    _import_or_404(import_id, user["id"])
    if data.pages is not None and (
            not data.pages or any(int(page) <= 0 for page in data.pages)):
        raise HTTPException(422, "重切页码 pages 必须是正整数数组")
    pages = [int(page) for page in (data.pages or []) if int(page) > 0]
    page_rows = db.list_pages(import_id, user["id"])
    if data.pages:
        available = {int(page["page_no"]) for page in page_rows}
        invalid = [page for page in pages if page not in available]
        if invalid:
            raise HTTPException(404, "未找到第 %s 页" % "、".join(str(page) for page in invalid))
    target_count = len(set(pages)) or len(page_rows)
    if target_count > MAX_SYNC_RESEGMENT_PAGES:
        if not data.provider_id or not data.model:
            raise HTTPException(422, "重切页数超过同步阈值，请先在模型设置中配置连接后重试")
        from ..ai import jobs as ai_jobs

        job_input = ai_jobs.JobInput(
            task=RESEGMENT_JOB_TASK,
            provider_id=int(data.provider_id),
            model=str(data.model),
            params={"import_id": import_id, "pages": sorted(set(pages)) or None,
                    "keep_items": bool(data.keep_items)},
        )
        job = await ai_jobs.submit_job(user, job_input)
        return {"job": job, "id": job.get("id"), "status": job.get("status"),
                "task": RESEGMENT_JOB_TASK, "import_id": import_id,
                "pages": sorted(set(pages)) or [int(page["page_no"]) for page in page_rows]}
    return resegment_import(import_id, user["id"], pages or None, keep_items=bool(data.keep_items))


# ---------------------------------------------------------------------------
# 单题答题区更新
# ---------------------------------------------------------------------------

class AnswerBoxInput(BaseModel):
    box: dict


@router.post("/imports/{import_id}/items/{item_id}/answer-box")
def update_answer_box(import_id: int, item_id: int, data: AnswerBoxInput,
                      user=Depends(current_user)):
    """更新某草稿的答题区坐标（归一化 0-1），并同步页级切题标注中的 answer 框。"""
    _import_or_404(import_id, user["id"])
    item = db.fetch_item(import_id, item_id, user["id"])
    if item.get("question_id"):
        raise HTTPException(409, "该题已入库，答题区坐标请到题库中编辑")
    box = normalize_single_box(data.box, kind="answer")
    page_no = int(item.get("page_no") or 0)
    page = _page_or_404(import_id, page_no, user["id"])
    if not str(page.get("storage_name") or ""):
        raise HTTPException(422, "该页为纯文本页，没有页面图，无法设置答题区")
    now = utcnow()
    draft = json_load(item.get("draft_json"), {})
    if not isinstance(draft, dict):
        draft = {}
    answer_area = {key: box[key] for key in ("x", "y", "w", "h")}
    draft["answer_box"] = answer_area
    draft["answer_boxes"] = [answer_area]
    annotation = draft.get("_annotation") if isinstance(draft.get("_annotation"), dict) else {}
    annotation.update({"answer_updated_at": now})
    draft["_annotation"] = annotation
    with connection() as con:
        write_lock(con, "paperlab-annotate")
        con.execute(
            "UPDATE pl_import_items SET draft_json=?, updated_at=? WHERE id=? AND import_id=? AND user_id=?",
            (json_dump(draft), now, item_id, import_id, user["id"]),
        )
        db.touch_import(con, import_id, user["id"])
    edit = _sync_answer_box_in_edit(
        import_id=import_id, page_no=page_no, user_id=user["id"], item_id=item_id,
        page=page, answer_area=answer_area, now=now,
    )
    return {
        "item": db.public_item(db.fetch_item(import_id, item_id, user["id"])),
        "edit": edit,
    }


def _sync_answer_box_in_edit(*, import_id: int, page_no: int, user_id: int, item_id: int,
                             page: dict, answer_area: dict, now: str) -> dict:
    """若该页已有 edit.json，则把该题的 answer 框同步进去（无则保持无编辑态）。"""
    edit = _find_edit(import_id, page_no, user_id)
    if edit is None:
        return _edit_public(None)
    data = json_load(edit.get("data_json"), {})
    boxes = data.get("boxes") if isinstance(data, dict) and isinstance(data.get("boxes"), list) else []
    synced = False
    for box in boxes:
        if isinstance(box, dict) and box.get("kind") == "answer" and _int(box.get("item_id")) == item_id:
            box.update(answer_area)
            synced = True
            break
    if not synced:
        boxes.append({"id": "a_item_%d" % item_id, "kind": "answer", "item_id": item_id, **answer_area})
    payload = json_dump(_edit_data(boxes, page, updated_at=now))
    with connection() as con:
        write_lock(con, "paperlab-annotate")
        row = con.execute(
            "SELECT * FROM pl_page_edits WHERE import_id=? AND page_no=? AND user_id=?",
            (import_id, page_no, user_id),
        ).fetchone()
        if row is None:  # pragma: no cover - 调用前刚查过
            return _edit_public(None)
        con.execute(
            "UPDATE pl_page_edits SET version=version+1, data_json=?, updated_at=? WHERE id=? AND user_id=?",
            (payload, now, int(row["id"]), user_id),
        )
        version = int(row["version"] or 0) + 1
    return {"version": version, "boxes": boxes, "updated_at": now, "edited": True}


# ---------------------------------------------------------------------------
# 发布前校验（只做统计 + 顺序校验，不复制 commit 语义）
# ---------------------------------------------------------------------------

def _publish_snapshot(import_id: int, user_id: int) -> dict:
    import_row = db.fetch_import(import_id, user_id)
    pages = db.list_pages(import_id, user_id)
    items = db.list_items(import_id, user_id)
    edits: dict[int, dict] = {}
    with connection() as con:
        rows = con.execute(
            "SELECT * FROM pl_page_edits WHERE import_id=? AND user_id=?",
            (import_id, user_id),
        ).fetchall()
    for row in rows:
        payload = _row_dict(row)
        data = json_load(payload.get("data_json"), {})
        boxes = data.get("boxes") if isinstance(data, dict) and isinstance(data.get("boxes"), list) else []
        edits[int(payload.get("page_no") or 0)] = {"version": int(payload.get("version") or 0), "boxes": boxes}
    errors: list[dict] = []
    warnings: list[str] = []
    candidates = [
        item for item in items
        if item.get("status") == "accepted" or item.get("question_id")
    ]
    for item in candidates:
        draft = json_load(item.get("draft_json"), {})
        if not isinstance(draft, dict):
            draft = {}
        item_errors: list[str] = []
        if not str(draft.get("stem") or "").strip():
            item_errors.append("缺少题干")
        bbox = draft.get("bbox") if isinstance(draft.get("bbox"), dict) else json_load(item.get("bbox_json"), {})
        try:
            bbox = schema.normalize_bbox(bbox, label="题目")
        except ValueError as exc:
            item_errors.append(str(exc))
        answer_box = draft.get("answer_box")
        if answer_box is not None:
            try:
                schema.normalize_bbox(answer_box, label="答题区")
            except ValueError as exc:
                item_errors.append(str(exc))
        if item.get("question_id") and item_errors:
            warnings.append("已入库草稿 %s 的字段异常（历史数据），未阻止发布：%s"
                            % (item["id"], "；".join(item_errors)))
            continue
        if item_errors:
            errors.append({
                "item_id": int(item["id"]),
                "item_no": int(item.get("item_no") or 0),
                "page_no": int(item.get("page_no") or 0),
                "status": item.get("status"),
                "errors": item_errors,
            })
    page_summaries = []
    for page in pages:
        page_no = int(page.get("page_no") or 0)
        page_items = [item for item in items if int(item.get("page_no") or 0) == page_no]
        annotation = edits.get(page_no) or {"version": 0, "boxes": []}
        boxes = annotation.get("boxes") or []
        page_summaries.append({
            "page_no": page_no,
            "items": len(page_items),
            "accepted": sum(1 for item in page_items if item.get("status") == "accepted"),
            "committed": sum(1 for item in page_items if item.get("question_id")),
            "skipped": sum(1 for item in page_items if item.get("status") == "skipped"),
            "annotation_version": int(annotation.get("version") or 0),
            "question_boxes": sum(1 for box in boxes if isinstance(box, dict) and box.get("kind") == "question"),
            "answer_boxes": sum(1 for box in boxes if isinstance(box, dict) and box.get("kind") == "answer"),
        })
    summary = {
        "import_id": import_id,
        "title": import_row.get("title") or "",
        "page_count": len(pages),
        "item_count": len(items),
        "pending": sum(1 for item in items if item.get("status") == "pending"),
        "accepted": sum(1 for item in items if item.get("status") == "accepted"),
        "skipped": sum(1 for item in items if item.get("status") == "skipped"),
        "committed": sum(1 for item in items if item.get("question_id")),
        "page_summaries": page_summaries,
        "checks": {
            "candidates": len(candidates),
            "errors": len(errors),
            "missing_annotation_pages": [
                page["page_no"] for page in page_summaries if page["annotation_version"] == 0
            ],
        },
    }
    order = [
        {
            "page_no": int(item.get("page_no") or 0),
            "item_no": int(item.get("item_no") or 0),
            "item_id": int(item["id"]),
            "question_id": item.get("question_id"),
            "status": item.get("status") or "pending",
        }
        for item in sorted(items, key=lambda item: (int(item.get("page_no") or 0), int(item.get("item_no") or 0)))
        if item.get("status") != "skipped"
    ]
    if not candidates:
        errors.append({"message": "没有已确认（accepted）或已入库的题目，请先确认题目再发布"})
    return {"summary": summary, "errors": errors, "warnings": warnings, "order": order}


@router.post("/imports/{import_id}/publish")
def publish_import(import_id: int, user=Depends(current_user)):
    """发布前校验：所有 accepted/committed 草稿需有题干与合法 bbox；返回统计与发布顺序。"""
    _import_or_404(import_id, user["id"])
    snapshot = _publish_snapshot(import_id, user["id"])
    ready = (not snapshot["errors"]) and (
        snapshot["summary"]["committed"] > 0 or snapshot["summary"]["accepted"] > 0
    )
    if not ready:
        raise HTTPException(422, detail={
            "message": "发布校验未通过，请修正后重试",
            "ready": False,
            **snapshot,
        })
    return {
        "ready": True,
        "import_id": import_id,
        **snapshot,
        "notice": "发布校验通过；实际入库请调用 POST /imports/{id}/commit（既有语义，未复制）。",
    }


# ---------------------------------------------------------------------------
# paper-annotate 任务（> MAX_SYNC_RESEGMENT_PAGES 页时使用；导入上限 20 页，正常走同步）
# ---------------------------------------------------------------------------

def _jobs():
    from ..ai import jobs

    return jobs


def _publish_event(job_id: int, event: str, payload: dict) -> None:
    try:
        _jobs().publish_event(job_id, event, payload)
    except Exception:  # noqa: BLE001 - SSE 推送失败不影响落库
        pass


def _checkpoint(job_id: int) -> dict:
    try:
        value = _jobs()._job_get_checkpoint(job_id)
    except Exception:  # noqa: BLE001
        return {}
    return value if isinstance(value, dict) else {}


def _set_checkpoint(job_id: int, checkpoint: dict) -> None:
    try:
        _jobs()._job_set_checkpoint(job_id, checkpoint)
    except Exception:  # noqa: BLE001
        pass


def _jump_to_status(job_id: int, status: str, stage: str) -> None:
    try:
        _jobs().update_job(job_id, status, stage)
    except Exception:  # noqa: BLE001
        pass


async def run_annotate_job(job_id, user_id, data, provider=None, verifier=None):
    """``paper-annotate`` 执行器：逐页重切、SSE 中文阶段、支持取消与 checkpooint 续跑。"""
    params = getattr(data, "params", None) or {}
    import_id = _int(params.get("import_id"))
    if not import_id:
        raise ValueError("缺少切题参数 import_id")
    import_row = db.fetch_import(import_id, user_id)
    page_rows = db.list_pages(import_id, user_id)
    available = [int(page["page_no"]) for page in page_rows]
    requested = [int(page) for page in (params.get("pages") or []) if _int(page) > 0]
    target_pages = [page for page in (requested or available) if page in available]
    if not target_pages:
        raise ValueError("没有可重切的页面")
    keep_items = bool(params.get("keep_items", True))
    checkpoint = _checkpoint(job_id)
    done = {_int(page) for page in (checkpoint.get("done_pages") or [])}
    totals = {"created": 0, "updated": 0, "skipped": 0, "stale": 0}
    warnings: list[str] = []
    for position, page_no in enumerate(target_pages, 1):
        if _jobs().is_job_cancelled(job_id):
            checkpoint.update({"done_pages": sorted(done)})
            _set_checkpoint(job_id, checkpoint)
            raise asyncio.CancelledError()
        if page_no in done:
            continue
        stage = "重切第 %d/%d 页" % (position, len(target_pages))
        _jump_to_status(job_id, "running", stage)
        _publish_event(job_id, "stage", {
            "status": "running", "stage": stage, "done": position - 1,
            "total": len(target_pages), "page_no": page_no,
        })
        result = await asyncio.to_thread(
            _page_resegment, page_no, import_row, user_id, keep_items=keep_items,
        )
        for key in totals:
            totals[key] += int(result.get(key) or 0)
        warnings.extend(result.get("warnings") or [])
        done.add(page_no)
        checkpoint.update({"import_id": import_id, "done_pages": sorted(done),
                           "totals": dict(totals), "warnings": warnings[-MAX_WARNINGS:]})
        _set_checkpoint(job_id, checkpoint)
        _publish_event(job_id, "stage", {
            "status": "running", "stage": "已完成第 %d/%d 页重切" % (position, len(target_pages)),
            "done": position, "total": len(target_pages), "page_no": page_no,
        })
    return {
        "import_id": import_id,
        "pages": target_pages,
        "keep_items": keep_items,
        "created": totals["created"],
        "updated": totals["updated"],
        "skipped": totals["skipped"],
        "stale": totals["stale"],
        "summary": "重切完成：新建 %d，更新 %d，跳过 %d，stale %d"
                   % (totals["created"], totals["updated"], totals["skipped"], totals["stale"]),
        "warnings": warnings[-MAX_WARNINGS:],
    }


def validate_annotate_job(data, user) -> None:
    """``paper-annotate`` 准入：导入归属、页码合法、页数上限。"""
    params = getattr(data, "params", None) or {}
    import_id = _int(params.get("import_id"))
    if not import_id:
        raise HTTPException(422, "缺少切题参数 import_id")
    import_row = db.fetch_import(import_id, user["id"])
    pages = db.list_pages(import_id, user["id"])
    available = {int(page["page_no"]) for page in pages}
    if not available:
        raise HTTPException(422, "该导入还没有页面，请重新上传")
    requested = params.get("pages")
    if requested is not None:
        if not isinstance(requested, (list, tuple)) or not requested:
            raise HTTPException(422, "重切页码 pages 必须是非空数组")
        unknown = [page for page in requested if _int(page) not in available]
        if unknown:
            raise HTTPException(404, "未找到第 %s 页" % "、".join(str(page) for page in unknown[:5]))
        if len(set(_int(page) for page in requested)) > MAX_SYNC_RESEGMENT_PAGES:
            raise HTTPException(422, "单次重切最多 %d 页" % MAX_SYNC_RESEGMENT_PAGES)
    _ = import_row


__all__ = [
    "MAX_BOXES_PER_PAGE", "RESEGMENT_JOB_TASK", "ensure_tables", "normalize_boxes",
    "resegment_import", "router", "run_annotate_job", "validate_annotate_job",
]
