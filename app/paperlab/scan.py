"""试卷工坊 P9：答题卡扫描识别（OMR，个人自测维度）。

能力（契约 §4.2；只依赖已装的 opencv / numpy / Pillow，不新增运行依赖、不访问外网）：
- **定位点与透视矫正**：四角 12×5mm 黑色实心矩形 → 连通域候选（长宽比/实心度/面积）→
  四元组合几何校验（凸四边形/角点角度/对边比例/四角尺寸一致）→ 小尺寸试矫正校验四角暗块都对得上
  → ``cv2.getPerspectiveTransform`` 矫正到答题卡模板像素尺寸（96 DPI，与 ``card_svg`` 一致）；
- **页面码**：按 layout 页脚位置裁出 46 位二进制文本 → 周期网格 + 局部投影谷底细化单元边界 →
  用固定位 ``101``/``110`` 标定 0/1 阈值（不依赖具体字体）→ 与模板签名/页码比对，不符标 ``code_ok=false``；
- **气泡 OMR**：按 layout 气泡圆心取内圈暗像素比例（阈值可配，默认 0.45）判断填涂；单选多泡冲突、
  填涂不清晰等一律标 ``unreadable``（绝不猜答案）；多选题支持任意组合；
- **学号**：学号填涂区逐位解码（6/8 位、每位 10 泡）；手写线学号不识别；
- **判分**：复用 ``practice`` 的客观判分口径（单选/多选精确匹配），主观题不计分；不伪造读数；
- **人工纠错确认**：``POST /scans/{id}/confirm`` 按纠错后的答案重算分数，并在同一事务内生成一条
  ``pl_paper_attempts``（detail.source='scan'）复用既有错题联动（``bank:`` 题写入错题本）。

表 ``pl_card_scans`` 只存识别结果与分数；原始上传图片不落盘（不扩大存储面），前端在本次会话内
用本地文件做 canvas 叠加；从历史打开时展示结构化结果（无原图）。
"""
from __future__ import annotations

import asyncio
from io import BytesIO
import logging
import math
import os
from typing import Any

import cv2
import numpy as np
from PIL import Image
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from .. import question_ref, study_evidence
from ..auth import current_user
from ..db import connection, utcnow, write_lock
from . import card_svg, cards, papers
from . import practice as paper_practice
from .db import MAX_PAGE_PIXELS, json_dump, json_load

router = APIRouter()
_log = logging.getLogger("zhixu.paperlab.scan")

# ---------------------------------------------------------------------------
# 常量与表（幂等建表）
# ---------------------------------------------------------------------------

MAX_SCAN_PAGES = 10                       # 单次扫描最多页数
MAX_PAGE_BYTES = 30 * 1024 * 1024         # 单页图片 ≤ 30MB
# 解码前像素预算：30MB 压缩包可解出数亿像素（像素炸弹）。先读文件头再决定是否解码，
# 正常手机照片 12–48MP 不受影响，异常大图在 cv2.imdecode 之前就拒绝。
# 复用 db.MAX_PAGE_PIXELS（40MP），与工坊导入的图像预算保持同一口径。
DEFAULT_BUBBLE_THRESHOLD = 0.45           # 气泡填涂暗度比例阈值（可配）
MIN_PAGE_SHORT_EDGE = 320                 # 图片短边下限（低于视为分辨率过低）
MIN_PAGE_WIDTH_PX = 500                   # 由定位点估算的答题卡宽度下限
DETECT_MAX_EDGE = 2400                    # 定位点检测时图片最长边（性能）
AMBIGUITY_MARGIN = 0.12                   # 暗度落在阈值附近视为「填涂不清晰」
BUBBLE_INNER_RATIO = 0.62                 # 采样圆半径 = 气泡半径 × 该系数（避开印刷描边）
ANCHOR_ASPECT = 2.4                       # 12×5mm 定位点长宽比
ANCHOR_MEAN_AREA_MM2 = 60.0               # 12×5mm
SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
SCAN_STATUSES = ("recognized", "unreadable", "confirmed")
MAX_CONFIRM_ANSWERS = 60

try:  # 阈值可用环境变量覆盖（运维调参），非法值回退默认。
    DEFAULT_BUBBLE_THRESHOLD = float(str(os.environ.get("STUDY_OMR_BUBBLE_THRESHOLD", "")).strip() or DEFAULT_BUBBLE_THRESHOLD)
    DEFAULT_BUBBLE_THRESHOLD = max(0.1, min(0.9, DEFAULT_BUBBLE_THRESHOLD))
except ValueError:  # pragma: no cover - 环境变量非法时保持默认
    DEFAULT_BUBBLE_THRESHOLD = 0.45

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pl_card_scans (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    paper_id INTEGER NOT NULL REFERENCES pl_papers(id) ON DELETE CASCADE,
    card_id INTEGER NOT NULL REFERENCES pl_cards(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'recognized',
    page_count INTEGER NOT NULL DEFAULT 0,
    result_json TEXT NOT NULL DEFAULT '{}',
    score REAL NOT NULL DEFAULT 0,
    max_score REAL NOT NULL DEFAULT 0,
    attempt_id INTEGER REFERENCES pl_paper_attempts(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS pl_card_scans_user ON pl_card_scans(user_id, paper_id, id);
CREATE INDEX IF NOT EXISTS pl_card_scans_card ON pl_card_scans(card_id, id);
CREATE INDEX IF NOT EXISTS pl_card_scans_status ON pl_card_scans(user_id, status);
"""


def ensure_tables() -> None:
    """幂等建表（``pl_card_scans``）；由 ``init_paperlab()`` 启动时调用。"""
    with connection() as con:
        con.executescript(_SCHEMA)


def register() -> None:
    """扫描是同步 API（无需任务执行器）；保留 ``register()`` 与 ``__init__`` 接线一致。"""
    return None


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------

def _row_dict(row) -> dict:
    if isinstance(row, dict):
        return row
    try:
        return dict(row)
    except Exception:  # noqa: BLE001
        return {}


def _mm_px(value: float) -> float:
    return float(value) * card_svg.MM_TO_PX


def _template_size(layout: dict) -> tuple[int, int]:
    """模板页像素尺寸（96 DPI，与 ``render_card_svg`` 的 width/height 一致）。"""
    paper = layout.get("paper") if isinstance(layout.get("paper"), dict) else {}
    width = int(round(float(paper.get("width_mm") or 210) * card_svg.MM_TO_PX))
    height = int(round(float(paper.get("height_mm") or 297) * card_svg.MM_TO_PX))
    return max(1, width), max(1, height)


def _page_by_no(layout: dict, page_no: int) -> dict | None:
    for page in layout.get("pages") or []:
        if int(page.get("page_no") or 0) == int(page_no):
            return page
    return None


def _page_count(layout: dict) -> int:
    pages = layout.get("pages")
    return len(pages) if isinstance(pages, list) else 0


def _anchor_centers_px(layout: dict, page: dict, scale: float = 1.0) -> tuple[np.ndarray, dict]:
    """模板四角定位点中心像素坐标（顺序 top_left/top_right/bottom_right/bottom_left）。"""
    boxes = {str(item.get("corner")): item for item in page.get("anchors") or [] if isinstance(item, dict)}
    points = []
    for corner in ("top_left", "top_right", "bottom_right", "bottom_left"):
        box = boxes.get(corner) or {}
        x = (float(box.get("x") or 0) + float(box.get("w") or 0) / 2.0) * card_svg.MM_TO_PX * scale
        y = (float(box.get("y") or 0) + float(box.get("h") or 0) / 2.0) * card_svg.MM_TO_PX * scale
        points.append([x, y])
    return np.array(points, dtype=np.float32), boxes


def _paper_threshold(gray: np.ndarray) -> float:
    """暗色阈值：以页面主亮度（较亮分位）估计，兼容偏亮/偏暗拍照。"""
    paper_level = float(np.percentile(gray, 75))
    return max(35.0, min(185.0, paper_level * 0.58))


def _dark_level(gray: np.ndarray) -> float:
    """Otsu 暗色阈值（用于气泡暗度比例），限制在合理区间。"""
    value = float(cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)[0])
    return max(60.0, min(205.0, value))


# ---------------------------------------------------------------------------
# 图片解码 / 定位点检测 / 四角选择 / 透视矫正
# ---------------------------------------------------------------------------

class ImageBudgetExceeded(ValueError):
    """图片像素预算超限：在读文件头阶段拒绝，绝不进入整图解码。"""


def _probe_image_size(data: bytes) -> tuple[int, int] | None:
    """只读图片文件头拿宽高（PIL 懒加载，不解码像素）；无法识别返回 None。

    Pillow 自带的 ``DecompressionBombError``（超过 2×MAX_IMAGE_PIXELS 才触发）说明声明
    尺寸已远大于任何合理页面，直接按超预算拒绝。
    """
    if not data:
        return None
    try:
        with Image.open(BytesIO(data)) as probe:
            width, height = probe.size
            return int(width), int(height)
    except Image.DecompressionBombError:
        raise ImageBudgetExceeded("图片像素数过大（疑似像素炸弹），拒绝解码") from None
    except Exception:  # noqa: BLE001 - 非图片/损坏图片交给解码器给原有中文原因
        return None


def _pixel_budget_problem(size: tuple[int, int] | None) -> str:
    """像素预算提示（空串表示未超限）；``None`` 表示文件头不可识别、交由解码器判断。"""
    if not size:
        return ""
    width, height = size
    if width * height <= MAX_PAGE_PIXELS:
        return ""
    return (
        f"图片像素 {width}×{height} 超过单页 {MAX_PAGE_PIXELS // 1_000_000}MP 上限，"
        "请压缩或降分辨率后重试"
    )


def _decode_image(data: bytes, filename: str = "") -> np.ndarray:
    if not data:
        raise ValueError("空文件")
    # 防御性复核：即使调用方绕过 HTTP 预检，也不在像素炸弹上分配解码内存。
    try:
        problem = _pixel_budget_problem(_probe_image_size(data))
    except ImageBudgetExceeded as exc:
        raise ValueError(str(exc)) from None
    if problem:
        raise ValueError(problem)
    buffer = np.frombuffer(data, dtype=np.uint8)
    image = cv2.imdecode(buffer, cv2.IMREAD_COLOR)
    if image is None or image.size == 0:
        raise ValueError("图片无法解码（文件损坏或不是支持的图片格式）")
    if image.shape[0] < 40 or image.shape[1] < 40:
        raise ValueError("图片尺寸过小")
    return image


def _detect_scale(gray: np.ndarray) -> tuple[np.ndarray, float]:
    """检测用最大边长限制；返回 (缩放图, 坐标回映系数=原图/缩放图)。"""
    height, width = gray.shape[:2]
    longest = max(height, width)
    if longest <= DETECT_MAX_EDGE:
        return gray, 1.0
    scale = DETECT_MAX_EDGE / float(longest)
    resized = cv2.resize(gray, (max(1, int(round(width * scale))), max(1, int(round(height * scale)))),
                         interpolation=cv2.INTER_AREA)
    return resized, 1.0 / scale


def _detect_anchor_candidates(gray: np.ndarray) -> tuple[list[dict], float]:
    """检测黑色实心矩形候选（定位点）；返回 (候选列表, 坐标回映系数)。"""
    small, factor = _detect_scale(gray)
    height, width = small.shape[:2]
    image_area = float(width * height)
    threshold = _paper_threshold(small)
    mask = np.where(small < threshold, 255, 0).astype(np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    count, _labels, stats, centroids = cv2.connectedComponentsWithStats(mask, 8)
    expected_area = image_area * ANCHOR_MEAN_AREA_MM2 / (210.0 * 297.0)
    candidates: list[dict] = []
    for index in range(1, count):
        x, y, box_w, box_h, area = (int(value) for value in stats[index])
        if box_w < 6 or box_h < 3:
            continue
        fill = area / float(box_w * box_h)
        aspect = box_w / float(box_h)
        if fill < 0.58 or not (1.15 <= aspect <= 10.0):
            continue
        if area < expected_area * 0.12 or area > expected_area * 8.0:
            continue
        aspect_weight = math.exp(-((aspect - ANCHOR_ASPECT) / 1.1) ** 2)
        candidates.append({
            "x": float(centroids[index][0]) * factor,
            "y": float(centroids[index][1]) * factor,
            "area": float(area) * factor * factor,
            "w": float(box_w) * factor,
            "h": float(box_h) * factor,
            "fill": fill,
            "aspect": aspect,
            "score": float(area) * (0.5 + 0.5 * aspect_weight),
        })
    candidates.sort(key=lambda item: item["score"], reverse=True)
    return candidates[:12], factor


def _order_quad(points: list[dict]) -> list[dict]:
    """把四个点排成 top_left → top_right → bottom_right → bottom_left（逆时针为正）。"""
    array = np.array([[point["x"], point["y"]] for point in points], dtype=np.float64)
    center = array.mean(axis=0)
    angles = np.arctan2(array[:, 1] - center[1], array[:, 0] - center[0])
    order = np.argsort(angles)
    ordered = [points[int(index)] for index in order]
    # 统一为图像坐标系下的顺时针（屏幕坐标 y 向下）：先按角度排序已稳定，再选左上角起点。
    start = min(range(4), key=lambda idx: (ordered[idx]["x"] + ordered[idx]["y"]))
    ordered = ordered[start:] + ordered[:start]
    return ordered


def _quad_geometry_error(ordered: list[dict], layout: dict, image_shape: tuple[int, int]) -> str:
    """四角几何校验；返回中文原因（空串表示通过）。"""
    height, width = image_shape
    points = np.array([[point["x"], point["y"]] for point in ordered], dtype=np.float64)
    area = abs(float(cv2.contourArea(points.astype(np.float32))))
    if area < 0.10 * width * height:
        return "定位点围成的区域过小"
    for index in range(4):
        previous, current, following = points[(index - 1) % 4], points[index], points[(index + 1) % 4]
        first, second = previous - current, following - current
        norm = float(np.linalg.norm(first) * np.linalg.norm(second))
        if norm <= 1e-9:
            return "定位点位置重叠"
        cosine = float(np.dot(first, second) / norm)
        angle = math.degrees(math.acos(max(-1.0, min(1.0, cosine))))
        if angle < 52.0 or angle > 128.0:
            return "拍摄角度过大"
    top = float(np.linalg.norm(points[1] - points[0]))
    bottom = float(np.linalg.norm(points[3] - points[2]))
    left = float(np.linalg.norm(points[3] - points[0]))
    right = float(np.linalg.norm(points[2] - points[1]))
    if min(top, bottom) / max(top, bottom) < 0.55 or min(left, right) / max(left, right) < 0.55:
        return "拍摄角度过大"
    paper = layout.get("paper") if isinstance(layout.get("paper"), dict) else {}
    expected_width_mm = max(50.0, float(paper.get("width_mm") or 210) - 36.0)
    expected_height_mm = max(50.0, float(paper.get("height_mm") or 297) - 29.0)
    expected_ratio = expected_width_mm / expected_height_mm
    observed_ratio = ((top + bottom) / 2.0) / max(1e-6, (left + right) / 2.0)
    if observed_ratio < expected_ratio * 0.55 or observed_ratio > expected_ratio * 1.75:
        return "四角定位点比例异常"
    areas = [float(point["area"]) for point in ordered]
    if max(areas) / max(1.0, min(areas)) > 4.5:
        return "四角定位点尺寸差异过大"
    widths = [float(point["w"]) for point in ordered]
    heights = [float(point["h"]) for point in ordered]
    if max(widths) / max(1.0, min(widths)) > 2.5 or max(heights) / max(1.0, min(heights)) > 2.5:
        return "四角定位点尺寸差异过大"
    return ""


def _homography_from_points(anchors: list[dict], template_points: np.ndarray) -> np.ndarray:
    source = np.array([[anchor["x"], anchor["y"]] for anchor in anchors], dtype=np.float32)
    return cv2.getPerspectiveTransform(source, template_points.astype(np.float32))


def _quad_validation(gray: np.ndarray, ordered: list[dict], layout: dict, page: dict) -> bool:
    """用 1/4 尺寸试矫正：四角定位点区域必须都足够暗，防止把文字/阴影当成定位点。"""
    small_scale = 0.25
    template_points, _boxes = _anchor_centers_px(layout, page, scale=small_scale)
    homography = _homography_from_points(ordered, template_points)
    width, height = _template_size(layout)
    small_width = max(32, int(round(width * small_scale)))
    small_height = max(32, int(round(height * small_scale)))
    warped = cv2.warpPerspective(gray, homography, (small_width, small_height),
                                 flags=cv2.INTER_LINEAR, borderValue=255)
    threshold = _paper_threshold(warped)
    paper = layout.get("paper") if isinstance(layout.get("paper"), dict) else {}
    margin = float(paper.get("margin_mm") or 12.0) * card_svg.MM_TO_PX * small_scale
    checks = 0
    for box in page.get("anchors") or []:
        if not isinstance(box, dict):
            continue
        x0 = float(box.get("x") or 0) * card_svg.MM_TO_PX * small_scale
        y0 = float(box.get("y") or 0) * card_svg.MM_TO_PX * small_scale
        box_w = float(box.get("w") or 0) * card_svg.MM_TO_PX * small_scale
        box_h = float(box.get("h") or 0) * card_svg.MM_TO_PX * small_scale
        inner_x0 = int(round(x0 + box_w * 0.15))
        inner_x1 = int(round(x0 + box_w * 0.85))
        inner_y0 = int(round(y0 + box_h * 0.2))
        inner_y1 = int(round(y0 + box_h * 0.8))
        inner_x0, inner_y0 = max(0, inner_x0), max(0, inner_y0)
        inner_x1 = min(small_width, max(inner_x0 + 1, inner_x1))
        inner_y1 = min(small_height, max(inner_y0 + 1, inner_y1))
        patch = warped[inner_y0:inner_y1, inner_x0:inner_x1]
        if patch.size == 0:
            return False
        dark_ratio = float((patch < threshold).mean())
        checks += 1
        if dark_ratio < 0.5:
            return False
    return checks == 4


def _candidate_quads(gray: np.ndarray, layout: dict, page: dict) -> tuple[list[list[dict]], str, list[dict]]:
    """从候选里枚举四角定位点组合；返回 (几何合理且四角都暗的四边形列表, 失败原因, 全部候选)。"""
    candidates, _factor = _detect_anchor_candidates(gray)
    if len(candidates) < 4:
        return [], "缺少定位点（四角定位标记未完整检测到）", candidates
    combinations: list[tuple[float, list[dict]]] = []
    count = len(candidates)
    for first in range(count):
        for second in range(first + 1, count):
            for third in range(second + 1, count):
                for fourth in range(third + 1, count):
                    quad = _order_quad([candidates[first], candidates[second], candidates[third], candidates[fourth]])
                    if _quad_geometry_error(quad, layout, gray.shape[:2]):
                        continue
                    points = np.array([[point["x"], point["y"]] for point in quad], dtype=np.float32)
                    area = abs(float(cv2.contourArea(points)))
                    combinations.append((area, quad))
    if not combinations:
        # 有候选但组不成合理四边形：优先提示角度问题（角度校验是常见原因）。
        return [], "拍摄角度过大或四角定位点不完整，请正对答题卡、拍全四角后重拍", candidates
    combinations.sort(key=lambda item: item[0], reverse=True)
    quads: list[list[dict]] = []
    for _area, quad in combinations[:24]:
        if _quad_validation(gray, quad, layout, page):
            quads.append(quad)
        if len(quads) >= 6:
            break
    if not quads:
        return [], "缺少定位点（四角定位标记未完整检测到，可能被遮挡或裁切）", candidates
    return quads, "", candidates


def _warp_page(gray: np.ndarray, homography: np.ndarray, layout: dict) -> np.ndarray:
    width, height = _template_size(layout)
    return cv2.warpPerspective(gray, homography, (width, height),
                               flags=cv2.INTER_LINEAR, borderValue=255)


def _inverse_homography(homography: np.ndarray) -> list[float]:
    inverse = np.linalg.inv(homography)
    inverse = inverse / inverse[2, 2]
    return [float(value) for value in inverse.flatten()[:8]]


def _map_point(homography: list[float], x: float, y: float) -> tuple[float, float]:
    a, b, c, d, e, f, g, h = homography
    denominator = g * x + h * y + 1.0
    if abs(denominator) < 1e-9:
        return float(x), float(y)
    return (a * x + b * y + c) / denominator, (d * x + e * y + f) / denominator


# ---------------------------------------------------------------------------
# 页面码解码（46 位：101 + FNV32 签名 + 8bit 页码 + 110）
# ---------------------------------------------------------------------------

def _code_crop_region(layout: dict, page: dict) -> tuple[int, int, int, int]:
    footer = page.get("footer") if isinstance(page.get("footer"), dict) else {}
    cell = footer.get("page_code") if isinstance(footer.get("page_code"), dict) else {}
    center_x = float(cell.get("x") or (layout.get("paper") or {}).get("width_mm", 210) / 2.0)
    baseline = float(cell.get("y") or 278.0)
    font = float(cell.get("size") or 3.4)
    x0 = int(round((center_x - 50.0) * card_svg.MM_TO_PX))
    x1 = int(round((center_x + 50.0) * card_svg.MM_TO_PX))
    y0 = int(round((baseline - font * 1.5) * card_svg.MM_TO_PX))
    y1 = int(round((baseline + font * 0.5) * card_svg.MM_TO_PX))
    return x0, y0, x1, y1


def _page_code_bits_from_crop(crop: np.ndarray) -> tuple[str | None, float]:
    """从页面码条带解 46 位；返回 (bits 或 None, 0/1 分离度)。"""
    if crop.size == 0:
        return None, 0.0
    paper_level = float(np.percentile(crop, 70))
    threshold = max(30.0, min(185.0, paper_level * 0.6))
    mask = np.where(crop < threshold, 1, 0).astype(np.uint8)
    profile = mask.sum(axis=0).astype(np.float64)
    ink_columns = np.nonzero(profile)[0]
    if ink_columns.size < 12:
        return None, 0.0
    left = int(ink_columns.min())
    right = int(ink_columns.max())
    span = right - left + 1
    cell_width = span / 46.0
    # 1) 用「等分网格相位搜索」最小化网格线上的墨迹，锁定字符单元边界。
    best_score = None
    best_phase = float(left)
    for phase in np.arange(left - 0.7 * cell_width, left + 0.3 * cell_width, 0.1):
        indexes = np.round(phase + np.arange(46) * cell_width).astype(int)
        indexes = np.clip(indexes, 0, profile.size - 1)
        score = float(profile[indexes].sum())
        if best_score is None or score < best_score:
            best_score = score
            best_phase = float(phase)
    # 2) 每个边界在期望位置 ±1.8px 内做局部投影谷底细化（抗字距/亚像素漂移）。
    boundaries: list[int] = []
    for index in range(47):
        center = best_phase + index * cell_width
        low = max(0, int(math.floor(center - 1.8)))
        high = min(profile.size, int(math.ceil(center + 1.8)))
        if high <= low:
            boundaries.append(int(round(center)))
            continue
        boundaries.append(low + int(np.argmin(profile[low:high])))
    boundaries[0] = left
    boundaries[-1] = right + 1
    for index in range(1, len(boundaries)):
        if boundaries[index] <= boundaries[index - 1]:
            boundaries[index] = boundaries[index - 1] + 1
    counts = []
    for index in range(46):
        start = boundaries[index]
        end = min(mask.shape[1], max(start + 1, boundaries[index + 1]))
        counts.append(float(mask[:, start:end].sum()))
    # 3) 固定位 101（前 3 位）/ 110（后 3 位）标定 0/1 极性；字体不同也能稳定区分。
    one_counts = [counts[index] for index in (0, 2, 43, 44)]
    zero_counts = [counts[index] for index in (1, 45)]
    mean_one = float(np.mean(one_counts))
    mean_zero = float(np.mean(zero_counts))
    quality = abs(mean_one - mean_zero)
    if quality < 2.5:
        return None, quality
    threshold_count = (mean_one + mean_zero) / 2.0
    if mean_one < mean_zero:
        bits = "".join("1" if value < threshold_count else "0" for value in counts)
    else:
        bits = "".join("1" if value > threshold_count else "0" for value in counts)
    return bits, quality


def _decode_page_code(warped: np.ndarray, layout: dict, page: dict) -> dict:
    """解码页面码并与模板签名/页码比对；返回 {ok, code, page_no, quality, reason}。"""
    x0, y0, x1, y1 = _code_crop_region(layout, page)
    height, width = warped.shape[:2]
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(width, x1), min(height, y1)
    crop = warped[y0:y1, x0:x1]
    bits, quality = _page_code_bits_from_crop(crop)
    template = layout.get("template") if isinstance(layout.get("template"), dict) else {}
    signature = int(template.get("signature") or 0)
    total_pages = _page_count(layout)
    result = {"ok": False, "code": None, "page_no": None, "quality": round(float(quality), 2), "reason": ""}
    if not bits or len(bits) != 46 or bits[:3] != "101" or bits[-3:] != "110" or set(bits) - {"0", "1"}:
        result["reason"] = "页面码无法解码（打印模糊/污损/裁切），请保持页脚完整后重拍"
        return result
    page_no = int(bits[35:43], 2)
    expected = card_svg.page_code(signature, page_no) if 1 <= page_no <= total_pages else None
    result["code"] = bits
    result["page_no"] = page_no if 1 <= page_no <= total_pages else None
    if expected is None:
        result["reason"] = f"页面码页码超出答题卡范围（解码页码 {page_no}，本卡共 {total_pages} 页），可能扫错了答题卡"
        return result
    if bits != expected:
        result["reason"] = "页面码与答题卡模板不匹配（可能扫错答题卡或页面顺序不对），请核对后重拍"
        return result
    result["ok"] = True
    return result


# ---------------------------------------------------------------------------
# 气泡 / 学号采样
# ---------------------------------------------------------------------------

def _bubble_fill(gray: np.ndarray, dark_level: float, center_x: float, center_y: float,
                 radius_px: float) -> float:
    """气泡内圈暗像素比例（0~1）；半径按 ``BUBBLE_INNER_RATIO`` 收缩以避开印刷描边。"""
    sample_radius = max(1.2, float(radius_px) * BUBBLE_INNER_RATIO)
    x0 = max(0, int(math.floor(center_x - sample_radius)))
    x1 = min(gray.shape[1], int(math.ceil(center_x + sample_radius)) + 1)
    y0 = max(0, int(math.floor(center_y - sample_radius)))
    y1 = min(gray.shape[0], int(math.ceil(center_y + sample_radius)) + 1)
    if x1 <= x0 or y1 <= y0:
        return 0.0
    patch = gray[y0:y1, x0:x1]
    yy, xx = np.mgrid[y0:y1, x0:x1]
    disk = (xx - center_x) ** 2 + (yy - center_y) ** 2 <= sample_radius * sample_radius
    total = int(disk.sum())
    if total <= 0:
        return 0.0
    dark = int(((patch < dark_level) & disk).sum())
    return dark / float(total)


def _sample_student_no(warped: np.ndarray, page: dict, homography: list[float],
                       threshold: float = DEFAULT_BUBBLE_THRESHOLD) -> dict:
    """学号填涂区逐位解码（6/8 位，每位 10 泡）；不识别手写线。"""
    header = page.get("header") if isinstance(page.get("header"), dict) else {}
    area = header.get("student_no") if isinstance(header.get("student_no"), dict) else {}
    digits = int(area.get("digits") or 0)
    bubble = bool(area.get("bubble"))
    result: dict[str, Any] = {
        "digits": digits or None,
        "bubble": bubble,
        "value": None,
        "ok": False,
        "rows": [],
        "missing_positions": [],
        "ambiguous_positions": [],
        "marks": [],
    }
    if not area or not bubble:
        if area and not bubble:
            result["reason"] = "学号区为手写线，不做自动识别"
        return result
    dark_level = _dark_level(warped)
    decoded: list[str] = []
    for row in area.get("digit_rows") or []:
        if not isinstance(row, dict):
            continue
        position = int(row.get("position") or 0)
        chosen: list[int] = []
        row_marks: list[dict] = []
        fills = []
        for bubble_item in row.get("bubbles") or []:
            if not isinstance(bubble_item, dict):
                continue
            value = int(bubble_item.get("value") or 0)
            center_x = float(bubble_item.get("x") or 0) * card_svg.MM_TO_PX
            center_y = float(bubble_item.get("y") or 0) * card_svg.MM_TO_PX
            radius_px = float(bubble_item.get("r") or 1.15) * card_svg.MM_TO_PX
            fill = _bubble_fill(warped, dark_level, center_x, center_y, radius_px)
            fills.append(round(fill, 3))
            if fill >= threshold:
                chosen.append(value)
            if fill >= max(threshold * 0.55, 0.2):
                img_x, img_y = _map_point(homography, center_x, center_y)
                row_marks.append({
                    "position": position, "value": value, "x": round(center_x, 2), "y": round(center_y, 2),
                    "img_x": round(img_x, 2), "img_y": round(img_y, 2), "fill": round(fill, 3),
                    "selected": fill >= threshold,
                })
        if len(chosen) == 1:
            decoded.append(str(chosen[0]))
        elif len(chosen) > 1:
            result["ambiguous_positions"].append(position)
        else:
            result["missing_positions"].append(position)
        result["rows"].append({"position": position, "chosen": chosen, "fills": fills})
        result["marks"].extend(row_marks)
    if not result["missing_positions"] and not result["ambiguous_positions"] and decoded:
        result["value"] = "".join(decoded)
        result["ok"] = True
    elif result["missing_positions"] or result["ambiguous_positions"]:
        result["reason"] = "学号填涂不完整或存在歧义，未自动识别"
    return result


def _sample_page_questions(warped: np.ndarray, layout_page: dict, item_map: dict[int, dict],
                           homography: list[float], threshold: float) -> tuple[dict, list[dict], list[dict]]:
    """采样一页的客观题气泡；返回 (answers, bubbles, unreadable)。"""
    answers: dict[int, list[str]] = {}
    bubbles: list[dict] = []
    unreadable: list[dict] = []
    dark_level = _dark_level(warped)
    for group in layout_page.get("groups") or []:
        if not isinstance(group, dict):
            continue
        for question in group.get("questions") or []:
            if not isinstance(question, dict):
                continue
            layout_no = int(question.get("no") or 0)
            meta = item_map.get(layout_no)
            bubble_defs = [item for item in question.get("bubbles") or [] if isinstance(item, dict)]
            if meta is None or not bubble_defs or not meta.get("has_bubbles"):
                continue
            position = int(meta["position"])
            fills: list[tuple[dict, float]] = []
            for bubble_item in bubble_defs:
                center_x = float(bubble_item.get("x") or 0) * card_svg.MM_TO_PX
                center_y = float(bubble_item.get("y") or 0) * card_svg.MM_TO_PX
                radius_px = float(bubble_item.get("r") or card_svg.BUBBLE_R_MM) * card_svg.MM_TO_PX
                fill = _bubble_fill(warped, dark_level, center_x, center_y, radius_px)
                fills.append((bubble_item, fill))
            selected = [str(item.get("label") or "") for item, fill in fills if fill >= threshold]
            ambiguous = [
                (item, fill) for item, fill in fills
                if fill < threshold and fill >= max(threshold - AMBIGUITY_MARGIN, 0.12)
            ]
            reason = ""
            item_unreadable = False
            if meta["type"] == "SINGLE" and len(selected) > 1:
                item_unreadable = True
                labels = "、".join(selected)
                reason = f"单选题检测到多个填涂（{labels}），无法确定答案"
            elif ambiguous:
                labels = "、".join(str(item.get("label") or "") for item, _fill in ambiguous)
                item_unreadable = True
                fills_text = "、".join(f"{item.get('label') or ''}={fill:.2f}" for item, fill in ambiguous)
                reason = f"填涂不清晰（{labels} 暗度 {fills_text} 接近阈值 {threshold:g}），请人工确认"
            if item_unreadable:
                unreadable.append({
                    "item_position": position, "layout_no": layout_no,
                    "reason": reason, "selected": selected,
                })
                answers[position] = None  # type: ignore[assignment]
            else:
                answers[position] = selected
            # 前端 canvas 叠加：只有被选中/接近阈值/冲突的气泡需要画出来。
            for bubble_item, fill in fills:
                label = str(bubble_item.get("label") or "")
                is_selected = fill >= threshold
                is_ambiguous = (bubble_item, fill) in ambiguous
                if not (is_selected or is_ambiguous):
                    continue
                center_x = float(bubble_item.get("x") or 0) * card_svg.MM_TO_PX
                center_y = float(bubble_item.get("y") or 0) * card_svg.MM_TO_PX
                img_x, img_y = _map_point(homography, center_x, center_y)
                bubbles.append({
                    "item_position": position, "layout_no": layout_no, "label": label,
                    "x": round(center_x, 2), "y": round(center_y, 2),
                    "img_x": round(img_x, 2), "img_y": round(img_y, 2),
                    "fill": round(fill, 3), "selected": is_selected,
                    "ambiguous": is_ambiguous, "unreadable": item_unreadable,
                })
    return answers, bubbles, unreadable


# ---------------------------------------------------------------------------
# 试卷题目映射与判分（复用 practice 的客观判分口径）
# ---------------------------------------------------------------------------

def _layout_item_map(items_raw: list[dict], layout: dict) -> dict[int, dict]:
    """layout 题号 no（答题卡上的连续编号）→ 试卷题目 position/快照/分值。"""
    include_subjective = bool(layout.get("include_subjective", True))
    mapping: dict[int, dict] = {}
    layout_no = 0
    for position, row in enumerate(items_raw, start=1):
        snapshot = json_load(row.get("snapshot_json"), {})
        if not isinstance(snapshot, dict):
            snapshot = {}
        qtype = str(snapshot.get("type") or "SUBJECTIVE").upper()
        if qtype not in ("SINGLE", "MULTIPLE") and not include_subjective:
            continue
        layout_no += 1
        mapping[layout_no] = {
            "layout_no": layout_no,
            "position": position,
            "item_id": int(row.get("id") or 0),
            "question_ref": str(row.get("question_ref") or ""),
            "score": round(float(row.get("score") or 0), 2),
            "type": qtype,
            "snapshot": snapshot,
            "has_bubbles": False,
            "page_no": None,
            "options": _choice_options(snapshot),
        }
    for page in layout.get("pages") or []:
        page_no = int(page.get("page_no") or 0)
        for group in page.get("groups") or []:
            for question in (group or {}).get("questions") or []:
                if not isinstance(question, dict):
                    continue
                meta = mapping.get(int(question.get("no") or 0))
                if meta is None:
                    continue
                meta["page_no"] = page_no
                meta["has_bubbles"] = bool(question.get("bubbles"))
    return mapping


def _choice_options(snapshot: dict) -> list[dict]:
    content = snapshot.get("content") if isinstance(snapshot.get("content"), dict) else {}
    options = []
    for index, option in enumerate(content.get("options") or []):
        if not isinstance(option, dict):
            continue
        options.append({
            "label": str(option.get("label") or chr(ord("A") + index))[:2],
            "text": str(option.get("text") or "")[:80],
        })
    return options


def _judge_item_for_meta(meta: dict, answer) -> dict:
    item = {
        "score": meta.get("score") or 0,
        "question_ref": meta.get("question_ref") or "",
        "snapshot_json": json_dump(meta.get("snapshot") or {}),
    }
    return paper_practice._judge_item(item, answer)


def _objective_metas(item_map: dict[int, dict]) -> list[dict]:
    return [
        meta for _no, meta in sorted(item_map.items())
        if meta.get("has_bubbles") and meta.get("type") in ("SINGLE", "MULTIPLE")
    ]


def _grade_scan(item_map: dict[int, dict], answers: dict[int, list[str] | None],
                unreadable_reasons: dict[int, str] | None = None) -> dict:
    """识别/确认共用的客观题判分；返回 questions/answer_map/summary/score/max_score。

    ``answers[position] is None`` 表示该题识别不确定（pending，不判分、不得猜答案）；
    空列表 ``[]`` 表示明确未作答（判错但不计入「不确定」）。
    """
    questions = []
    answer_map: dict[str, list[str]] = {}
    reasons = unreadable_reasons or {}
    score = 0.0
    max_score = 0.0
    correct_count = incorrect_count = pending_count = unanswered_count = 0
    for meta in _objective_metas(item_map):
        position = int(meta["position"])
        selected = answers.get(position)
        max_score += float(meta.get("score") or 0)
        if selected is None:
            judgement = "pending"
            got = 0.0
            reason = reasons.get(position) or "识别不确定，待人工确认"
            pending_count += 1
        else:
            answer_map[str(position)] = list(selected)
            judged = _judge_item_for_meta(meta, selected)
            judgement = judged.get("judgement") or "pending"
            got = round(float(judged.get("score") or 0), 2)
            reason = judged.get("reason") or ""
            if judgement == "correct":
                score += got
                correct_count += 1
            elif judgement == "incorrect":
                incorrect_count += 1
                if not selected:
                    unanswered_count += 1
            else:
                pending_count += 1
        questions.append({
            "item_position": position,
            "item_id": meta.get("item_id"),
            "layout_no": int(meta.get("layout_no") or 0),
            "page_no": meta.get("page_no"),
            "question_ref": meta.get("question_ref") or "",
            "type": meta.get("type"),
            "score": round(float(meta.get("score") or 0), 2),
            "options": meta.get("options") or [],
            "selected": list(selected) if selected is not None else [],
            "unreadable": selected is None,
            "reason": reason,
            "judgement": judgement,
            "score_got": got,
            "reference_answers": _reference_labels(meta.get("snapshot") or {}),
        })
    graded = correct_count + incorrect_count
    summary = {
        "item_count": len(questions),
        "correct_count": correct_count,
        "incorrect_count": incorrect_count,
        "pending_count": pending_count,
        "unanswered_count": unanswered_count,
        "graded_count": graded,
        "score": round(score, 2),
        "max_score": round(max_score, 2),
        "accuracy": round(correct_count / graded, 4) if graded else None,
    }
    return {
        "questions": questions,
        "answers": answer_map,
        "summary": summary,
        "score": round(score, 2),
        "max_score": round(max_score, 2),
    }


def _reference_labels(snapshot: dict) -> list[str]:
    try:
        return list(paper_practice._reference_choice_labels(snapshot))
    except Exception:  # noqa: BLE001 - 快照异常不阻塞识别
        return []


# ---------------------------------------------------------------------------
# 单页识别
# ---------------------------------------------------------------------------

def _page_base(index: int, filename: str, image_shape: tuple[int, int] | None = None) -> dict:
    height, width = image_shape if image_shape else (0, 0)
    return {
        "index": index + 1,
        "file_name": filename,
        "status": "unreadable",
        "reason": "",
        "page_no": None,
        "code_ok": False,
        "code": None,
        "expected_code": None,
        "code_quality": 0.0,
        "image_width": int(width),
        "image_height": int(height),
        "page_width": 0,
        "page_height": 0,
        "anchors": [],
        "homography": [],
        "markers": {},
        "student_no": {"digits": None, "bubble": None, "value": None, "ok": False},
        "bubbles": [],
        "unreadable": [],
        "unreadable_items": [],
    }


def _recognize_page(index: int, filename: str, raw: bytes, layout: dict,
                    item_map: dict[int, dict], threshold: float) -> dict:
    """识别一页：定位点→透视→页面码→气泡/学号；失败页给中文原因且不猜读数。"""
    page_result = _page_base(index, filename)
    try:
        image = _decode_image(raw, filename)
    except ValueError as exc:
        page_result["reason"] = f"「{filename}」无法读取：{exc}"
        return page_result
    height, width = image.shape[:2]
    page_result["image_width"] = int(width)
    page_result["image_height"] = int(height)
    if min(height, width) < MIN_PAGE_SHORT_EDGE:
        page_result["reason"] = (
            f"图片分辨率过低（短边仅 {min(height, width)} 像素，需 ≥ {MIN_PAGE_SHORT_EDGE}），"
            "请用手机后置摄像头在充足光线下重拍"
        )
        return page_result
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    # 四角定位点在所有页相同：任意一页模板即可做透视矫正；页码由页面码决定具体版面。
    first_page = (layout.get("pages") or [{}])[0]
    quads, error, candidates = _candidate_quads(gray, layout, first_page)
    if not quads:
        page_result["reason"] = error or "缺少定位点（四角定位标记未完整检测到）"
        page_result["markers"] = {
            "candidates": [
                {"x": round(float(item["x"]), 2), "y": round(float(item["y"]), 2),
                 "area": round(float(item["area"]), 1)}
                for item in candidates
            ],
        }
        return page_result
    template_points, _boxes = _anchor_centers_px(layout, first_page)
    width_px, height_px = _template_size(layout)
    page_result["page_width"] = width_px
    page_result["page_height"] = height_px
    accepted: tuple[list[dict], np.ndarray, np.ndarray, dict] | None = None
    # 几何校验后仍可能把黑色文字块误当第四个定位点（三点即可把错位点映射到模板角）：
    # 再用「页面码能否解出合法 46 位」确认透视矩阵确实对齐了整页，避免伪造读数。
    for quad in quads:
        homography = _homography_from_points(quad, template_points)
        warped = _warp_page(gray, homography, layout)
        decoded = _decode_page_code(warped, layout, first_page)
        if decoded.get("code"):
            accepted = (quad, homography, warped, decoded)
            break
    if accepted is None:
        page_result["reason"] = (
            "未能校验页面码（四角定位点可能不完整、被遮挡，或页脚页面码区域不清晰）；"
            "请拍全四角、保持页脚完整后重拍"
        )
        page_result["markers"] = {"candidates": [
            {"x": round(float(item["x"]), 2), "y": round(float(item["y"]), 2),
             "area": round(float(item["area"]), 1)} for item in candidates
        ]}
        return page_result
    anchors, homography, warped, decoded = accepted
    inverse = _inverse_homography(homography)
    page_result["homography"] = [round(value, 8) for value in inverse]
    anchor_marks = []
    for corner, anchor in zip(("top_left", "top_right", "bottom_right", "bottom_left"), anchors):
        anchor_marks.append({"corner": corner, "x": round(float(anchor["x"]), 2),
                             "y": round(float(anchor["y"]), 2)})
    page_result["anchors"] = anchor_marks
    # 分辨率（由定位点跨度反推答题卡在原始图中的宽度）
    top_span = math.hypot(anchors[1]["x"] - anchors[0]["x"], anchors[1]["y"] - anchors[0]["y"])
    paper = layout.get("paper") if isinstance(layout.get("paper"), dict) else {}
    width_mm = float(paper.get("width_mm") or 210)
    estimated_page_width = top_span * width_mm / max(1.0, width_mm - 36.0)
    if estimated_page_width < MIN_PAGE_WIDTH_PX:
        page_result["reason"] = (
            f"图片分辨率过低（推算答题卡宽度约 {int(round(estimated_page_width))} 像素，需 ≥ {MIN_PAGE_WIDTH_PX}），"
            "请靠近拍摄或使用更高分辨率"
        )
        return page_result
    code_box = _page_code_crop_on_original(layout, first_page, inverse, width, height)
    page_result["markers"] = {
        "anchors": anchor_marks,
        "page_code": {
            "x": round(float(code_box["x"]), 2), "y": round(float(code_box["y"]), 2),
            "width": round(float(code_box["width"]), 2), "height": round(float(code_box["height"]), 2),
        },
    }
    signature = int(((layout.get("template") or {}).get("signature") or 0))
    if decoded.get("page_no"):
        page_result["page_no"] = int(decoded["page_no"])
        page_result["expected_code"] = card_svg.page_code(signature, int(decoded["page_no"]))
    page_result["code"] = decoded.get("code")
    page_result["code_ok"] = bool(decoded.get("ok"))
    page_result["code_quality"] = decoded.get("quality") or 0.0
    if not decoded.get("ok"):
        page_result["reason"] = decoded.get("reason") or "页面码校验失败，请核对答题卡与页面后重拍"
        return page_result
    layout_page = _page_by_no(layout, int(decoded["page_no"]))
    if layout_page is None:
        page_result["reason"] = "页面码页码不在答题卡范围内，请重拍"
        return page_result
    page_result["expected_code"] = card_svg.page_code(signature, int(decoded["page_no"]))
    answers, bubbles, unreadable = _sample_page_questions(warped, layout_page, item_map, inverse, threshold)
    page_result["bubbles"] = bubbles
    page_result["unreadable"] = [entry["layout_no"] for entry in unreadable]
    page_result["unreadable_items"] = unreadable
    page_result["answers"] = {str(position): selected for position, selected in answers.items()}
    page_result["student_no"] = _sample_student_no(warped, layout_page, inverse, threshold)
    page_result["status"] = "ok"
    page_result["reason"] = ""
    return page_result


def _page_code_crop_on_original(layout: dict, page: dict, inverse: list[float],
                                image_width: int, image_height: int) -> dict:
    x0, y0, x1, y1 = _code_crop_region(layout, page)
    corners = ((x0, y0), (x1, y0), (x0, y1), (x1, y1))
    mapped = [_map_point(inverse, x, y) for x, y in corners]
    xs = [min(image_width, max(0.0, point[0])) for point in mapped]
    ys = [min(image_height, max(0.0, point[1])) for point in mapped]
    return {"x": min(xs), "y": min(ys), "width": max(1.0, max(xs) - min(xs)),
            "height": max(1.0, max(ys) - min(ys))}


# ---------------------------------------------------------------------------
# 整卷识别结果
# ---------------------------------------------------------------------------

def _recognize_scan(page_results: list[dict], item_map: dict[int, dict],
                    layout: dict) -> dict:
    """合并各页识别结果 → result_json（答案/不可读项/分数/汇总）。"""
    total_pages = _page_count(layout)
    readable_pages = [page for page in page_results if page.get("status") == "ok"]
    decoded_numbers = [int(page["page_no"]) for page in readable_pages if page.get("page_no")]
    missing_pages = [number for number in range(1, total_pages + 1) if number not in decoded_numbers]
    duplicate_pages = sorted({number for number in decoded_numbers if decoded_numbers.count(number) > 1})
    answers: dict[int, list[str] | None] = {}
    unreadable_items: list[dict] = []
    # 先按页收集识别出的答案；未识别页的题目统一标不可读。
    for page in readable_pages:
        for key, selected in (page.get("answers") or {}).items():
            answers[int(key)] = selected
        for entry in page.get("unreadable_items") or []:
            unreadable_items.append({
                "item_position": int(entry["item_position"]),
                "layout_no": int(entry.get("layout_no") or 0),
                "page_no": int(page.get("page_no") or 0),
                "reason": str(entry.get("reason") or ""),
            })
    for number, meta in sorted(item_map.items()):
        if not meta.get("has_bubbles") or meta.get("type") not in ("SINGLE", "MULTIPLE"):
            continue
        position = int(meta["position"])
        if position in answers:
            continue
        if meta.get("page_no") and int(meta["page_no"]) not in decoded_numbers:
            answers[position] = None
            unreadable_items.append({
                "item_position": position,
                "layout_no": int(number),
                "page_no": meta.get("page_no"),
                "reason": (
                    "该题所在页未识别（缺失/重复/页面码不符），请重拍该页"
                    if int(meta.get("page_no") or 0) in missing_pages or duplicate_pages
                    else "该题所在页未能识别，请重拍该页"
                ),
            })
    graded = _grade_scan(
        item_map, answers,
        {entry["item_position"]: entry["reason"] for entry in unreadable_items},
    )
    # 学号：取各页一致的值；不一致时显式标记冲突。
    student_values = sorted({
        str(page["student_no"].get("value")) for page in readable_pages
        if (page.get("student_no") or {}).get("value")
    })
    student_no = {
        "value": student_values[0] if len(student_values) == 1 else None,
        "conflict": len(student_values) > 1,
        "values": student_values,
        "digits": (readable_pages[0]["student_no"].get("digits") if readable_pages else None),
    }
    summary = dict(graded["summary"])
    summary["page_count"] = total_pages
    summary["readable_page_count"] = len(readable_pages)
    result = {
        "schema": "zhixu-card-scan-v1",
        "page_count": total_pages,
        "pages": page_results,
        "answers": graded["answers"],
        "questions": graded["questions"],
        "unreadable_items": _dedupe_unreadable(unreadable_items),
        "student_no": student_no,
        "missing_pages": missing_pages,
        "duplicate_pages": duplicate_pages,
        "score": graded["score"],
        "max_score": graded["max_score"],
        "summary": summary,
        "confirmed": False,
        "confirmed_at": None,
        "attempt_id": None,
    }
    return result


def _dedupe_unreadable(entries: list[dict]) -> list[dict]:
    seen = set()
    result = []
    for entry in entries:
        key = int(entry.get("item_position") or 0)
        if key in seen:
            continue
        seen.add(key)
        result.append(entry)
    result.sort(key=lambda item: int(item.get("item_position") or 0))
    return result


def _result_status(result: dict) -> str:
    pages = result.get("pages") or []
    return "recognized" if any(page.get("status") == "ok" for page in pages) else "unreadable"


# ---------------------------------------------------------------------------
# 序列化 / 读取
# ---------------------------------------------------------------------------

def _scan_public(row: dict) -> dict:
    return {
        "id": int(row["id"]),
        "paper_id": int(row["paper_id"]),
        "card_id": int(row["card_id"]),
        "status": row.get("status") or "recognized",
        "page_count": int(row.get("page_count") or 0),
        "score": round(float(row.get("score") or 0), 2),
        "max_score": round(float(row.get("max_score") or 0), 2),
        "attempt_id": row.get("attempt_id"),
        "created_at": row.get("created_at") or "",
        "updated_at": row.get("updated_at") or "",
    }


def _owned_scan(scan_id: int, user_id: int) -> dict:
    with connection() as con:
        row = con.execute(
            "SELECT * FROM pl_card_scans WHERE id=? AND user_id=?", (scan_id, user_id)
        ).fetchone()
    if row is None:
        raise HTTPException(404, "未找到该扫描记录")
    return _row_dict(row)


def _scan_payload(row: dict) -> dict:
    result = json_load(row.get("result_json"), {})
    if not isinstance(result, dict):
        result = {}
    payload = {"scan": _scan_public(row), "result": result}
    try:
        paper = papers.fetch_paper(int(row["paper_id"]), int(row["user_id"]))
        items = papers.list_items(int(row["paper_id"]), int(row["user_id"]))
        payload["paper"] = papers.public_paper(
            paper, item_count=len(items),
            total_score=round(sum(float(item.get("score") or 0) for item in items), 2),
        )
    except HTTPException:
        payload["paper"] = None
    try:
        card = cards.fetch_card(int(row["card_id"]), int(row["user_id"]))
        payload["card"] = cards.public_card(card)
    except HTTPException:
        payload["card"] = None
    if row.get("attempt_id"):
        payload["attempt"] = _attempt_summary(int(row["attempt_id"]), int(row["user_id"]))
    return payload


def _attempt_summary(attempt_id: int, user_id: int) -> dict | None:
    with connection() as con:
        row = con.execute(
            "SELECT * FROM pl_paper_attempts WHERE id=? AND user_id=?", (attempt_id, user_id)
        ).fetchone()
    if row is None:
        return None
    attempt = _row_dict(row)
    detail = json_load(attempt.get("detail_json"), {})
    summary = (detail or {}).get("summary") if isinstance(detail, dict) else {}
    summary = summary if isinstance(summary, dict) else {}
    return {
        "id": int(attempt["id"]),
        "status": attempt.get("status") or "",
        "score": round(float(attempt.get("score") or 0), 2),
        "max_score": round(float(attempt.get("max_score") or 0), 2),
        "correct_count": int(summary.get("correct_count") or 0),
        "incorrect_count": int(summary.get("incorrect_count") or 0),
        "pending_count": int(summary.get("pending_count") or 0),
        "submitted_at": attempt.get("submitted_at"),
    }


def _card_and_layout(paper_id: int, user_id: int, card_id: int | None) -> tuple[dict, dict, list[dict], dict]:
    """取本次扫描使用的答题卡与试卷题目；默认选最近一张与当前试卷指纹一致的卡。"""
    paper = papers.fetch_paper(paper_id, user_id)
    items_raw = papers.list_items(paper_id, user_id)
    if not items_raw:
        raise HTTPException(422, "试卷还没有题目，无法扫描答题卡")
    item_snapshots = []
    for row in items_raw:
        snapshot = json_load(row.get("snapshot_json"), {})
        item_snapshots.append({
            "position": row.get("position"),
            "question_ref": row.get("question_ref") or "",
            "score": row.get("score"),
            "snapshot": snapshot if isinstance(snapshot, dict) else {},
        })
    fingerprint = card_svg._paper_fingerprint(paper, item_snapshots)
    with connection() as con:
        rows = con.execute(
            "SELECT * FROM pl_cards WHERE paper_id=? AND user_id=? ORDER BY id DESC",
            (paper_id, user_id),
        ).fetchall()
    card_rows = [_row_dict(row) for row in rows]
    chosen = None
    chosen_layout: dict = {}
    if card_id is not None:
        card = cards.fetch_card(int(card_id), user_id)
        if int(card["paper_id"]) != int(paper_id):
            raise HTTPException(404, "未找到该答题卡")
        layout = cards._layout_of(card)
        template = layout.get("template") if isinstance(layout.get("template"), dict) else {}
        if str(template.get("paper_fingerprint") or "") != fingerprint:
            raise HTTPException(422, "该答题卡已失效（试卷结构已变化），请重新生成答题卡后再扫描")
        if card.get("status") == "stale":
            raise HTTPException(422, "该答题卡已失效（试卷结构已变化），请重新生成答题卡后再扫描")
        chosen, chosen_layout = card, layout
    else:
        for card in card_rows:
            layout = cards._layout_of(card)
            template = layout.get("template") if isinstance(layout.get("template"), dict) else {}
            if str(template.get("paper_fingerprint") or "") == fingerprint:
                chosen, chosen_layout = card, layout
                break
        if chosen is None:
            if card_rows:
                raise HTTPException(422, "现有答题卡已失效（试卷结构已变化），请重新生成答题卡后再扫描")
            raise HTTPException(422, "该试卷还没有答题卡：请先在「答题卡」区块生成答题卡，打印填写后再扫描")
    if not chosen_layout.get("pages"):
        raise HTTPException(409, "答题卡版面数据缺失，请重新生成答题卡")
    return chosen, chosen_layout, items_raw, paper


# ---------------------------------------------------------------------------
# 人工纠错确认：写扫描结果 + 生成 pl_paper_attempts（source=scan）
# ---------------------------------------------------------------------------

def _validate_confirm_answers(layout: dict, item_map: dict[int, dict], result: dict,
                              submitted: dict) -> dict[int, list[str]]:
    """校验纠错后的答案；返回 {item_position: [labels]}。未处理的不可读项报 422。"""
    objective = {int(meta["position"]): meta for meta in _objective_metas(item_map)}
    cleaned: dict[int, list[str]] = {}
    for raw_key, raw_value in (submitted or {}).items():
        try:
            position = int(raw_key)
        except (TypeError, ValueError):
            raise HTTPException(422, "答案键必须是题目序号") from None
        meta = objective.get(position)
        if meta is None:
            raise HTTPException(422, f"第 {position} 题不是本答题卡上的客观题，不能提交答案")
        if not isinstance(raw_value, list):
            raise HTTPException(422, f"第 {position} 题答案必须是选项 label 数组")
        options = {str(option["label"]).upper() for option in meta.get("options") or []}
        labels: list[str] = []
        for value in raw_value:
            label = str(value or "").strip().upper()
            if not label:
                continue
            if label not in options:
                raise HTTPException(422, f"第 {position} 题的选项「{label}」不在本题选项内")
            if label in labels:
                raise HTTPException(422, f"第 {position} 题的选项「{label}」重复提交")
            labels.append(label)
        if meta["type"] == "SINGLE" and len(labels) > 1:
            raise HTTPException(422, f"第 {position} 题是单选题，只能保留一个答案")
        # 按选项顺序排序，保证判分与展示稳定。
        order = [str(option["label"]).upper() for option in meta.get("options") or []]
        labels.sort(key=lambda label: order.index(label))
        cleaned[position] = labels
    unreadable_positions = [
        int(entry["item_position"]) for entry in result.get("unreadable_items") or []
        if int(entry.get("item_position") or 0) in objective
    ]
    missing = [position for position in unreadable_positions if position not in cleaned]
    if missing:
        numbers = "、".join(str(position) for position in sorted(missing))
        raise HTTPException(422, f"第 {numbers} 题为识别不确定项，请先人工确认选项或明确标记未作答")
    # 未提交的其它客观题沿用识别结果（已确定的读数）；识别不确定项必须由用户显式提交。
    recognized = result.get("answers") if isinstance(result.get("answers"), dict) else {}
    for position, meta in objective.items():
        if position in cleaned:
            continue
        value = recognized.get(str(position))
        if isinstance(value, list):
            cleaned[position] = [str(label) for label in value]
        else:
            cleaned[position] = []
    return cleaned


# ---------------------------------------------------------------------------
# 学习证据投影（方案 §4「作答与学习证据」）：答题卡确认后的逐题判分同事务写入统一
# 证据事件，同一题可按 question_ref 在练习/工坊/课堂/错题中定位；证据只是判分结果的
# 投影，写入失败只记日志，绝不回滚/阻断确认与判分主流程。
# 共享证据模块的 record_kind 白名单中答题卡记录对应 "paper_scan"（业务表 pl_card_scans
# 由 record_id 承载扫描记录 ID），与整卷练习的 paper_attempt 事件互不干扰。
# ---------------------------------------------------------------------------

def _scan_item_evidence_key(scan_id: int, position: int) -> str:
    """答题卡逐题学习证据的幂等基础键：同一扫描记录的同一题位只有一条有效证据。"""
    return f"paper:scan:{int(scan_id)}:{int(position)}"


def _scan_pending_note(judged: dict) -> str:
    """待核验证据的 meta.note：如实说明缺参考答案/主观题不自动判分，不伪造对错。"""
    reference = judged.get("reference") if isinstance(judged.get("reference"), dict) else {}
    parts = reference.get("parts") if isinstance(reference.get("parts"), list) else []
    has_reference = bool(
        reference.get("answers")
        or str(reference.get("answer_text") or "").strip()
        or any(isinstance(part, dict) and part.get("answers") for part in parts)
    )
    if not has_reference:
        return "答题卡无可核对参考答案"
    return "答题卡只自动判客观题，主观题按待核验记录"


def _record_scan_item_evidence(con, user_id: int, *, scan_id: int, position: int,
                               ref: str, judged: dict, max_score: float) -> None:
    """答题卡逐题证据：确认后的判分结果 → 共享证据事件（同一事务内、幂等）。

    ``question_ref`` 为空或格式非法时跳过并记日志（不因证据问题让确认 500）；判分未明确
    对错时 verdict 如实记 ``pending``，并在 ``meta.note`` 说明原因（缺参考答案/主观题）。
    """
    if not ref:
        _log.warning("答题卡学习证据跳过：scan=%s 第 %s 题缺少题目引用", scan_id, position)
        return
    try:
        normalized_ref = question_ref.normalize(ref)
    except question_ref.QuestionRefError as error:
        _log.warning(
            "答题卡学习证据跳过：scan=%s 第 %s 题题目引用非法（%s）",
            scan_id, position, type(error).__name__,
        )
        return
    verdict, grade_source = paper_practice._evidence_verdict_source(judged)
    meta = {
        "source": "scan",
        "score": round(float(judged.get("score") or 0), 2),
        "max_score": round(float(max_score or 0), 2),
    }
    if verdict == "pending":
        meta["note"] = _scan_pending_note(judged)
    try:
        study_evidence.record_event(
            user_id,
            kind="paper_scan",
            record_kind="paper_scan",
            record_id=int(scan_id),
            item_key=str(position),
            question_ref=normalized_ref,
            verdict=verdict,
            grade_source=grade_source,
            # 答题卡是作答后的纸质提交：判分时未看提示、答案未揭晓，属于独立作答。
            prompt_state="none",
            reveal_state="before",
            event_key=_scan_item_evidence_key(scan_id, position),
            con=con,
            meta=meta,
        )
    except Exception as error:  # noqa: BLE001 - 证据是投影，失败不阻断确认与判分
        _log.warning("答题卡学习证据写入失败（判分已保存）：%s", type(error).__name__)


def _create_scan_attempt(con, user_id: int, paper: dict, items_raw: list[dict],
                         answers: dict[int, list[str] | None], scan_id: int) -> dict:
    """按 practice 口径生成一次已提交练习（detail.source='scan'），复用错题联动。"""
    now = utcnow()
    max_score = round(sum(float(item.get("score") or 0) for item in items_raw), 2)
    con.execute(
        "INSERT INTO pl_paper_attempts(user_id,paper_id,paper_version,status,answers_json,"
        "score,max_score,detail_json,started_at,submitted_at,created_at) "
        "VALUES(?,?,?,'submitted','{}',0,?,'{}',?,?,?)",
        (user_id, int(paper["id"]), int(paper.get("version") or 1), max_score, now, now, now),
    )
    attempt_id = int(con.execute(
        "SELECT id FROM pl_paper_attempts WHERE user_id=? AND paper_id=? ORDER BY id DESC LIMIT 1",
        (user_id, int(paper["id"])),
    ).fetchone()[0])
    score = 0.0
    correct_count = incorrect_count = pending_count = 0
    detail_items: list[dict] = []
    wrong_added: list[dict] = []
    app_unlinked: list[dict] = []
    answers_json: dict[str, Any] = {}
    for position, item in enumerate(items_raw, start=1):
        answer = answers.get(position)
        judged = paper_practice._judge_item(item, answer)
        snapshot = paper_practice._snapshot(item)
        ref = str(item.get("question_ref") or "")
        item_max = round(float(item.get("score") or 0), 2)
        # 方案 §4：确认判分结果在同一事务内投影为共享学习证据（失败只记日志，不影响确认）。
        _record_scan_item_evidence(
            con, user_id, scan_id=scan_id, position=position, ref=ref,
            judged=judged, max_score=item_max,
        )
        if answer is not None:
            answers_json[str(position)] = answer
        if judged["judgement"] == "correct":
            score += float(judged.get("score") or 0)
            correct_count += 1
        elif judged["judgement"] == "incorrect":
            incorrect_count += 1
        else:
            pending_count += 1
        entry = {
            "item_position": position,
            "question_ref": ref,
            "type": str(snapshot.get("type") or "SUBJECTIVE").upper(),
            "judgement": judged["judgement"],
            "score": round(float(judged.get("score") or 0), 2),
            "max_score": item_max,
            "submitted": answer,
            "reason": judged.get("reason") or "",
            "reference": judged.get("reference") or {},
        }
        if judged.get("parts"):
            entry["parts"] = judged["parts"]
        detail_items.append(entry)
        con.execute(
            "INSERT INTO pl_paper_answers(attempt_id,user_id,item_position,question_ref,answer_json,"
            "result,score,max_score,detail_json,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(attempt_id,item_position) DO UPDATE SET "
            "answer_json=excluded.answer_json,result=excluded.result,score=excluded.score,"
            "max_score=excluded.max_score,detail_json=excluded.detail_json",
            (
                attempt_id, user_id, position, ref, json_dump(answer),
                judged["judgement"], float(judged.get("score") or 0), item_max,
                json_dump({**judged, "submitted": answer}), now,
            ),
        )
        if judged["judgement"] in ("incorrect", "pending"):
            if ref.startswith("bank:"):
                qid = ref.split(":", 1)[1].strip()
                if qid and paper_practice._record_wrong(con, user_id, qid, judged["judgement"]):
                    wrong_added.append({
                        "item_position": position, "qid": qid,
                        "judgement": judged["judgement"],
                        "reason": paper_practice._wrong_reason(judged),
                    })
            elif ref.startswith("app:"):
                app_unlinked.append({
                    "item_position": position, "question_ref": ref,
                    "judgement": judged["judgement"],
                    "reason": "应用题库题目已记录在本次扫描结果中；错题本按只读题库 qid 索引，暂不写入",
                })
    summary = {
        "item_count": len(items_raw),
        "correct_count": correct_count,
        "incorrect_count": incorrect_count,
        "pending_count": pending_count,
        "score": round(score, 2),
        "max_score": max_score,
        "auto_graded": correct_count + incorrect_count,
    }
    detail = {
        "source": "scan",
        "scan_id": int(scan_id),
        "summary": summary,
        "items": detail_items,
        "wrong_added": wrong_added,
        "app_bank_unlinked": app_unlinked,
        "notice": (
            f"答题卡扫描确认：正确 {correct_count} 题、错误 {incorrect_count} 题；"
            f"主观题不自动判分 {pending_count} 题。"
            + (f"错题已加入错题本 {len(wrong_added)} 题。" if wrong_added else "")
        ),
    }
    con.execute(
        "UPDATE pl_paper_attempts SET answers_json=?, score=?, max_score=?, detail_json=?, "
        "submitted_at=? WHERE id=?",
        (json_dump(answers_json), round(score, 2), max_score, json_dump(detail), now, attempt_id),
    )
    return {
        "id": attempt_id,
        "status": "submitted",
        "score": round(score, 2),
        "max_score": max_score,
        "correct_count": correct_count,
        "incorrect_count": incorrect_count,
        "pending_count": pending_count,
        "wrong_added": wrong_added,
        "app_bank_unlinked": app_unlinked,
        "notice": detail["notice"],
        "submitted_at": now,
    }


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class ConfirmInput(BaseModel):
    answers: dict[str, list[str]] = Field(default_factory=dict)


def _clean_threshold(value) -> float:
    try:
        threshold = DEFAULT_BUBBLE_THRESHOLD if value is None else float(value)
    except (TypeError, ValueError):
        raise HTTPException(422, "填涂阈值必须是 0.1–0.9 之间的数字（默认 0.45）") from None
    if not math.isfinite(threshold) or threshold < 0.1 or threshold > 0.9:
        raise HTTPException(422, "填涂阈值应在 0.1–0.9 之间（默认 0.45）")
    return round(threshold, 3)


@router.get("/scan/meta")
def scan_meta(user=Depends(current_user)):
    """扫描能力与上限（前端上传前提示用；阈值/页数上限与后端常量一致）。"""
    return {
        "enabled": True,
        "max_pages": MAX_SCAN_PAGES,
        "max_page_mb": MAX_PAGE_BYTES // (1024 * 1024),
        "max_page_pixels": MAX_PAGE_PIXELS,
        "threshold": DEFAULT_BUBBLE_THRESHOLD,
        "formats": sorted(extension.lstrip(".").upper() for extension in SUPPORTED_EXTENSIONS),
        "student_digits": [6, 8],
        "note": "识别不确定的题目标记为「需人工确认」；确认前不判分、不伪造读数。",
    }


@router.post("/papers/{paper_id}/scan")
async def create_scan(
    paper_id: int,
    files: list[UploadFile] | None = File(default=None),
    card_id: int | None = Form(default=None),
    threshold: float | None = Form(default=None),
    user=Depends(current_user),
):
    """上传多页答题卡图片 → 定位点/页面码/气泡/学号识别 → 返回逐页结果与分数。

    识别失败页返回中文原因（缺定位点/角度过大/页面码不符/分辨率过低），不猜答案。
    """
    uploads = [item for item in (files or []) if item and (item.filename or "").strip()]
    if not uploads:
        raise HTTPException(422, "请至少选择一张答题卡图片（JPG / PNG / WebP）")
    if len(uploads) > MAX_SCAN_PAGES:
        raise HTTPException(422, f"单次最多扫描 {MAX_SCAN_PAGES} 页，请分多次上传")
    bubble_threshold = _clean_threshold(threshold)
    card, layout, items_raw, paper = _card_and_layout(paper_id, user["id"], card_id)
    total_pages = _page_count(layout)
    if len(uploads) > total_pages:
        raise HTTPException(
            422,
            f"该答题卡共 {total_pages} 页，本次上传了 {len(uploads)} 张图片；请按答题卡页数上传",
        )
    item_map = _layout_item_map(items_raw, layout)
    payloads: list[tuple[str, bytes]] = []
    for upload in uploads:
        filename = str(upload.filename or "答题卡")
        extension = os.path.splitext(filename)[1].lower()
        if extension and extension not in SUPPORTED_EXTENSIONS:
            raise HTTPException(422, f"「{filename}」不是支持的图片格式（仅支持 JPG / PNG / WebP）")
        # 有界读取：只多读 1 字节探测是否超限，绝不把整个超大文件先读进内存再判断。
        raw = await upload.read(MAX_PAGE_BYTES + 1)
        if not raw:
            raise HTTPException(422, f"「{filename}」是空文件，请重新选择")
        if len(raw) > MAX_PAGE_BYTES:
            raise HTTPException(
                422,
                f"「{filename}」超过单页 {MAX_PAGE_BYTES // (1024 * 1024)}MB 上限，请压缩后重试",
            )
        # 解码前校验像素预算：像素炸弹在读文件头阶段就以 422 拒绝，不进 cv2 解码。
        try:
            size = _probe_image_size(raw)
        except ImageBudgetExceeded:
            raise HTTPException(
                422,
                f"「{filename}」图片像素数过大（超过单页 {MAX_PAGE_PIXELS // 1_000_000}MP 上限），"
                "请压缩后重试",
            ) from None
        problem = _pixel_budget_problem(size)
        if problem:
            raise HTTPException(422, f"「{filename}」{problem}")
        payloads.append((filename, raw))
    page_results = await asyncio.to_thread(
        lambda: [
            _recognize_page(index, filename, raw, layout, item_map, bubble_threshold)
            for index, (filename, raw) in enumerate(payloads)
        ]
    )
    result = _recognize_scan(page_results, item_map, layout)
    result["threshold"] = bubble_threshold
    template = layout.get("template") if isinstance(layout.get("template"), dict) else {}
    # 确认时用于核对「扫描后答题卡/试卷是否被改过」，避免拿旧扫描结果套新卷面判分。
    result["card_signature"] = int(template.get("signature") or 0)
    result["paper_fingerprint"] = str(template.get("paper_fingerprint") or "")
    now = utcnow()
    with connection() as con:
        write_lock(con, "paperlab-scan")
        con.execute(
            "INSERT INTO pl_card_scans(user_id,paper_id,card_id,status,page_count,result_json,"
            "score,max_score,attempt_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,NULL,?,?)",
            (
                user["id"], paper_id, int(card["id"]), _result_status(result),
                len(payloads), json_dump(result), result["score"], result["max_score"], now, now,
            ),
        )
        scan_id = int(con.execute(
            "SELECT id FROM pl_card_scans WHERE user_id=? AND paper_id=? ORDER BY id DESC LIMIT 1",
            (user["id"], paper_id),
        ).fetchone()[0])
    row = _owned_scan(scan_id, user["id"])
    response = _scan_payload(row)
    response["card"] = cards.public_card(card, layout)
    return response


@router.get("/scans/{scan_id}")
def get_scan(scan_id: int, user=Depends(current_user)):
    """本人扫描记录（含逐页识别结果、题目纠错信息与确认后的判分）。"""
    row = _owned_scan(scan_id, user["id"])
    return _scan_payload(row)


@router.post("/scans/{scan_id}/confirm")
def confirm_scan(scan_id: int, data: ConfirmInput | None = None, user=Depends(current_user)):
    """人工纠错后确认成绩：重算客观题分数并生成一条 ``pl_paper_attempts``（source=scan）。"""
    row = _owned_scan(scan_id, user["id"])
    if row.get("status") == "confirmed":
        raise HTTPException(409, "该扫描结果已确认，不能重复确认；如需更正请重新扫描")
    result = json_load(row.get("result_json"), {})
    if not isinstance(result, dict):
        raise HTTPException(409, "扫描结果数据缺失，请重新扫描")
    paper = papers.fetch_paper(int(row["paper_id"]), user["id"])
    items_raw = papers.list_items(int(row["paper_id"]), user["id"])
    if not items_raw:
        raise HTTPException(422, "试卷题目已不存在，无法确认成绩")
    card = cards.fetch_card(int(row["card_id"]), user["id"])
    layout = cards._layout_of(card)
    item_snapshots = []
    for item in items_raw:
        snapshot = json_load(item.get("snapshot_json"), {})
        item_snapshots.append({
            "position": item.get("position"),
            "question_ref": item.get("question_ref") or "",
            "score": item.get("score"),
            "snapshot": snapshot if isinstance(snapshot, dict) else {},
        })
    template = layout.get("template") if isinstance(layout.get("template"), dict) else {}
    if result.get("card_signature") and int(template.get("signature") or 0) != int(result["card_signature"]):
        raise HTTPException(409, "答题卡版面已变更，该扫描结果基于旧版面；请重新扫描后再确认")
    expected_fingerprint = str(result.get("paper_fingerprint") or "")
    if expected_fingerprint and card_svg._paper_fingerprint(paper, item_snapshots) != expected_fingerprint:
        raise HTTPException(422, "试卷结构已变化，该扫描结果基于旧版试卷；请重新扫描后再确认")
    pages = result.get("pages") or []
    if len(pages) != _page_count(layout):
        raise HTTPException(
            422,
            f"答题卡共 {_page_count(layout)} 页，本次只识别了 {len(pages)} 页；"
            "请重新拍摄全部页面后再确认成绩",
        )
    bad_pages = [page for page in pages if page.get("status") != "ok"]
    if bad_pages:
        reasons = "；".join(
            f"第 {page.get('index')} 张图片：{page.get('reason') or '未能识别'}" for page in bad_pages
        )
        raise HTTPException(422, f"仍有页面未能识别，请重拍后再确认成绩（{reasons}）")
    if result.get("missing_pages") or result.get("duplicate_pages"):
        raise HTTPException(422, "页面不完整或存在重复页，请按答题卡页序重拍全部页面后再确认")
    item_map = _layout_item_map(items_raw, layout)
    submitted = (data.answers if data else {}) or {}
    if len(submitted) > MAX_CONFIRM_ANSWERS:
        raise HTTPException(422, f"单次最多确认 {MAX_CONFIRM_ANSWERS} 题答案")
    answers = _validate_confirm_answers(layout, item_map, result, submitted)
    graded = _grade_scan(item_map, answers)
    # 兜底建表放在写事务之外（进程内幂等一次），避免启动顺序不同导致证据静默丢失。
    try:
        paper_practice._ensure_study_evidence_ready()
    except Exception as error:  # noqa: BLE001 - 建表失败只记日志，确认判分照常
        _log.warning("学习证据表初始化失败（答题卡确认继续）：%s", type(error).__name__)
    now = utcnow()
    with connection() as con:
        write_lock(con, "paperlab-scan")
        fresh = con.execute(
            "SELECT status FROM pl_card_scans WHERE id=? AND user_id=?", (scan_id, user["id"])
        ).fetchone()
        if fresh is None:
            raise HTTPException(404, "未找到该扫描记录")
        if fresh["status"] == "confirmed":
            raise HTTPException(409, "该扫描结果已确认，不能重复确认；如需更正请重新扫描")
        attempt = _create_scan_attempt(con, user["id"], paper, items_raw, answers, scan_id)
        result["answers"] = graded["answers"]
        result["questions"] = graded["questions"]
        confirmed_summary = dict(graded["summary"])
        confirmed_summary["page_count"] = int(result.get("page_count") or len(pages))
        confirmed_summary["readable_page_count"] = len(pages)
        result["summary"] = confirmed_summary
        result["score"] = graded["score"]
        result["max_score"] = graded["max_score"]
        result["unreadable_items"] = []
        result["confirmed"] = True
        result["confirmed_at"] = now
        result["attempt_id"] = attempt["id"]
        result["confirm_notice"] = attempt["notice"]
        con.execute(
            "UPDATE pl_card_scans SET status='confirmed', result_json=?, score=?, max_score=?, "
            "attempt_id=?, updated_at=? WHERE id=? AND user_id=? AND status!='confirmed'",
            (json_dump(result), graded["score"], graded["max_score"], attempt["id"], now, scan_id, user["id"]),
        )
    row = _owned_scan(scan_id, user["id"])
    payload = _scan_payload(row)
    payload["attempt"] = attempt
    return payload


@router.get("/papers/{paper_id}/scans")
def list_scans(paper_id: int, user=Depends(current_user)):
    """某张试卷的答题卡扫描历史（本人；按创建时间倒序，最多 50 条）。"""
    papers.fetch_paper(paper_id, user["id"])
    with connection() as con:
        rows = con.execute(
            "SELECT * FROM pl_card_scans WHERE paper_id=? AND user_id=? ORDER BY id DESC LIMIT 50",
            (paper_id, user["id"]),
        ).fetchall()
    items = []
    for row in rows:
        row_dict = _row_dict(row)
        payload = _scan_public(row_dict)
        result = json_load(row_dict.get("result_json"), {})
        if isinstance(result, dict):
            summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
            payload["summary"] = {
                "page_count": summary.get("page_count"),
                "readable_page_count": summary.get("readable_page_count"),
                "correct_count": summary.get("correct_count"),
                "incorrect_count": summary.get("incorrect_count"),
                "pending_count": summary.get("pending_count"),
                "accuracy": summary.get("accuracy"),
            }
            payload["student_no"] = (result.get("student_no") or {}).get("value") \
                if isinstance(result.get("student_no"), dict) else None
        items.append(payload)
    return {"items": items, "total": len(items), "paper_id": paper_id}


__all__ = [
    "router", "ensure_tables", "register",
    "MAX_SCAN_PAGES", "MAX_PAGE_BYTES", "MAX_PAGE_PIXELS", "DEFAULT_BUBBLE_THRESHOLD",
]
