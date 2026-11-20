"""题库块级内容引用与图片/公式回退图采集——线上 AI 与离线评测共用的唯一实现。

线上 AI 讲解/判分（``app.ai.verify._bank_evidence``）和离线分层评测
（``app.eval.build_content_refs`` / ``collect_image_parts``）必须看到完全相同的
原卷图片集合：

* ``stem_blocks`` / ``options_blocks`` / ``shared_blocks`` / ``answer_blocks`` /
  ``solution_blocks`` 中 ``kind=image`` 块的 ``asset.file``；
* ``kind=math`` 块的 ``fallback_asset`` 原卷公式回退图（LaTeX 渲染失败时的兜底）；
* 旧快照的 ``question["images"]`` 与 ``group["material_images"]``（仅在块未覆盖时补位）。

本模块只依赖 :mod:`app.db`，**不** import ``verify`` / ``eval``，避免反向依赖成环。
"""
import base64
from io import BytesIO
import json
from pathlib import Path
import re

from PIL import Image

from . import db

# 与历史行为保持一致的单请求图片限制（题库只读，不做压缩）。
MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_TOTAL_IMAGE_BYTES = 20 * 1024 * 1024
MAX_IMAGE_COUNT = 40
ALLOWED_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")

FIGURE_PLACEHOLDER_RE = re.compile(r"\[图\s*\d*\]")
FORMULA_PLACEHOLDER_RE = re.compile(r"\[公式\s*\d*\]")
VISUAL_PLACEHOLDER_RE = re.compile(r"\[(?:图|公式)\s*\d*\]")


# ---------------------------------------------------------------------------
# 基础小工具（JSON / 素材路径 / 块类型）
# ---------------------------------------------------------------------------

def _json_value(value, default):
    if isinstance(value, (list, dict)):
        return value
    if not value:
        return default
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


def _json_list(value) -> list[dict]:
    data = _json_value(value, [])
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def asset_file(value) -> str | None:
    """从 asset 字段（字符串或 {file:...}）取题库相对路径。"""
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        file = value.get("file")
        return str(file).strip() if isinstance(file, str) and file.strip() else None
    return None


def asset_role(value) -> str | None:
    if isinstance(value, dict):
        role = value.get("role")
        if isinstance(role, str) and role.strip():
            return role.strip()
    return None


def image_role(image: dict) -> str | None:
    role = image.get("role") if isinstance(image, dict) else None
    return str(role).strip() if isinstance(role, str) and role.strip() else None


def block_kind(block: dict) -> str:
    """与 web/js/core/dom.js 的 blockKindOf 保持同一口径。"""
    kind = str(block.get("kind") or block.get("type") or "").lower()
    if kind in ("text", "math", "image", "table"):
        return kind
    if block.get("table"):
        return "table"
    if block.get("asset"):
        return "image"
    if block.get("latex") or block.get("fallback_asset"):
        return "math"
    return "text"


def cell_text(cell) -> str:
    if cell is None:
        return ""
    if isinstance(cell, dict):
        return str(cell.get("text") or cell.get("value") or "")
    return str(cell)


def table_text(block: dict) -> str:
    table = block.get("table") if isinstance(block.get("table"), dict) else None
    rows = table.get("rows") if table else block.get("rows")
    if isinstance(rows, list) and rows:
        lines = []
        for row in rows:
            cells = row if isinstance(row, list) else [row]
            lines.append(" | ".join(cell_text(cell) for cell in cells))
        return "\n".join(lines)
    return str(block.get("text") or "")


def blocks_readable_text(blocks) -> str:
    """把块降级成可读文本（优先 latex/text；图片块由多模态输入承载，不占位）。"""
    if not isinstance(blocks, list):
        return ""
    parts = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        kind = block_kind(block)
        if kind == "text":
            text = block.get("text")
        elif kind == "math":
            text = block.get("latex") or block.get("text")
        elif kind == "table":
            text = table_text(block)
        else:
            text = None
        if isinstance(text, str) and text.strip():
            parts.append(text.strip())
    return "\n".join(parts)


def _join_unique(parts) -> str:
    result = []
    for part in parts:
        text = str(part or "").strip()
        if text and text not in result:
            result.append(text)
    return "\n".join(result)


# ---------------------------------------------------------------------------
# 内容视图：题干/选项/材料的块级归一化
# ---------------------------------------------------------------------------

def content_view(question: dict) -> dict:
    """归一化题干/选项/材料，供 golden-v2 记录与 content_refs 共用。"""
    stem_plain = str(question.get("stem_plain") or "").strip()
    stem_blocks = [block for block in question.get("stem_blocks") or [] if isinstance(block, dict)]
    stem_text = stem_plain or blocks_readable_text(stem_blocks)

    options_blocks = [entry for entry in question.get("options_blocks") or [] if isinstance(entry, dict)]
    blocks_by_label = {
        str(entry.get("label")): [block for block in entry.get("blocks") or [] if isinstance(block, dict)]
        for entry in options_blocks
        if entry.get("label") is not None
    }
    options = []
    for index, option in enumerate(question.get("options") or []):
        if not isinstance(option, dict):
            continue
        label = str(option.get("label") if option.get("label") is not None else "")
        blocks = [block for block in option.get("blocks") or [] if isinstance(block, dict)]
        if not blocks:
            blocks = blocks_by_label.get(label, [])
        if not blocks and index < len(options_blocks):
            blocks = [block for block in options_blocks[index].get("blocks") or [] if isinstance(block, dict)]
        text = str(option.get("text") or "").strip() or blocks_readable_text(blocks)
        options.append({"label": label, "text": text, "blocks": blocks})
    if not options:
        for entry in options_blocks:
            label = str(entry.get("label") if entry.get("label") is not None else "")
            blocks = [block for block in entry.get("blocks") or [] if isinstance(block, dict)]
            options.append({"label": label, "text": blocks_readable_text(blocks), "blocks": blocks})

    group = question.get("group") if isinstance(question.get("group"), dict) else {}
    shared_blocks = [
        block
        for block in (question.get("shared_blocks") or group.get("shared_blocks") or [])
        if isinstance(block, dict)
    ]
    material_text = str(group.get("material_text") or question.get("material_text") or "").strip()
    shared_text = _join_unique(
        [
            group.get("parent_stem"),
            blocks_readable_text(shared_blocks),
            question.get("shared_text"),
        ]
    )
    return {
        "stem_plain": stem_plain,
        "stem_text": stem_text,
        "stem_blocks": stem_blocks,
        "options": options,
        "options_blocks": options_blocks,
        "material_text": material_text,
        "shared_text": shared_text,
        "shared_blocks": shared_blocks,
        "group": group,
    }


def iter_block_groups(question: dict, view: dict | None = None) -> list[tuple[str, str, list[dict]]]:
    """按固定顺序列出所有块组：(field, field_label, blocks)。

    顺序即模型/评测看到图片的顺序：题干 → 选项 → 共用题干 → 原卷答案 → 原卷解析。
    """
    view = view or content_view(question)
    groups: list[tuple[str, str, list[dict]]] = [("stem", "题干", view["stem_blocks"])]
    for option in view["options"]:
        label = option.get("label") or ""
        field = f"option:{label}" if label else "option"
        field_label = f"选项{label}" if label else "选项"
        groups.append((field, field_label, option.get("blocks") or []))
    groups.append(("shared", "共用题干", view["shared_blocks"]))
    groups.append(("answer", "原卷答案", _json_list(question.get("answer_blocks"))))
    groups.append(("solution", "原卷解析", _json_list(question.get("solution_blocks"))))
    return groups


# ---------------------------------------------------------------------------
# 图片采集：线上 AI 与离线评测唯一口径
# ---------------------------------------------------------------------------

def block_image_asset(block: dict) -> str | None:
    """块对应的原卷图片素材：插图取 asset.file，公式块取 fallback_asset.file。"""
    kind = block_kind(block)
    if kind == "image":
        return asset_file(block.get("asset"))
    if kind == "math":
        return asset_file(block.get("fallback_asset"))
    return None


def collect_question_images(question: dict) -> list[str]:
    """返回该题应送给模型的全部原卷图片（去重、按块顺序）。

    覆盖五类块（stem/options/shared/answer/solution）里的插图与公式回退图；
    旧快照的 ``images`` / ``group.material_images`` 仅在块未覆盖时补位。
    线上 ``verify._bank_evidence`` 与离线 ``eval.build_content_refs`` 都只调用本函数。
    """
    view = content_view(question)
    assets: list[str] = []
    seen: set[str] = set()
    for _field, _field_label, blocks in iter_block_groups(question, view):
        for block in blocks:
            if not isinstance(block, dict):
                continue
            asset = block_image_asset(block)
            if asset and asset not in seen:
                seen.add(asset)
                assets.append(asset)
    for image in _json_list(question.get("images")):
        asset = asset_file(image)
        if asset and asset not in seen:
            seen.add(asset)
            assets.append(asset)
    for image in _json_list(view["group"].get("material_images")):
        asset = asset_file(image)
        if asset and asset not in seen:
            seen.add(asset)
            assets.append(asset)
    return assets


def resolve_bank_asset(subject: str, asset: str) -> Path | None:
    """把题库相对素材路径解析成根目录内的真实文件；越界/不存在返回 None。"""
    try:
        root = Path(db.BANK_DIR).resolve()
    except Exception:
        return None
    if not root.is_dir():
        return None
    path = Path(asset)
    candidates = [path] if path.is_absolute() else [root / path]
    if not path.is_absolute() and subject:
        candidates.append(root / subject / path)
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if root not in resolved.parents or not resolved.is_file():
            continue
        return resolved
    return None


def image_part(source) -> dict:
    """读取本地图片为多模态输入片段；超限/不可读时抛 ValueError。"""
    raw = source if isinstance(source, bytes) else Path(source).read_bytes()
    if len(raw) > MAX_IMAGE_BYTES:
        raise ValueError("图片超过大小限制")
    with Image.open(BytesIO(raw)) as image:
        image.verify()
        mime = Image.MIME.get(image.format, "image/png")
    return {"mime": mime, "data": base64.b64encode(raw).decode()}


# ---------------------------------------------------------------------------
# 证据构建：线上讲解/判分（原卷参考答案与解析也属于证据）
# ---------------------------------------------------------------------------

def build_evidence(question: dict) -> dict:
    """构建送给模型的题目证据；有块级结构时以块渲染可读文本为准。"""
    view = content_view(question)
    answer = question.get("answer") or {}
    if isinstance(answer, str):
        answer = {"text": answer}
    reference = str(answer.get("text") or question.get("answer_text") or "")
    solution = str(answer.get("solution") or question.get("solution") or "")
    answer_blocks = _json_list(question.get("answer_blocks"))
    solution_blocks = _json_list(question.get("solution_blocks"))
    # R3 有题只有块级解析：纯文本为空时用块渲染文本兜底。
    if not reference.strip():
        reference = blocks_readable_text(answer_blocks)
    if not solution.strip():
        solution = blocks_readable_text(solution_blocks)

    # [公式N]/[图N] 占位符只是旧文本字段的降级写法；有块级结构时以块渲染文本为准。
    block_stem = blocks_readable_text(view["stem_blocks"])
    stem_text = block_stem or str(question.get("stem_plain") or "").strip()
    options = []
    for option in view["options"]:
        block_text = blocks_readable_text(option.get("blocks") or [])
        options.append({
            "label": option.get("label") or "",
            "text": block_text or (option.get("text") or ""),
        })
    if not options:
        raw_options = question.get("options")
        if isinstance(raw_options, list):
            options = [
                item if isinstance(item, dict)
                else {"label": "", "text": str(item)}
                for item in raw_options
            ]

    evidence = {
        "题干": stem_text,
        "选项": options,
        "原卷参考答案": reference,
        "原卷解析": solution,
        "材料": question.get("group"),
        "学科": question.get("subject"),
        "题型": question.get("type_label") or question.get("type"),
    }
    if view["shared_text"]:
        evidence["共用题干"] = view["shared_text"]
    if view["stem_plain"] and view["stem_plain"] != stem_text:
        # 旧字段兜底：块渲染与 stem_plain 不一致时保留原文，便于核对占位符/OCR 差异。
        evidence["题干原文"] = view["stem_plain"]
    return evidence


def collect_bank_evidence(question: dict) -> tuple[dict, list[dict]]:
    """线上讲解/判分用：证据 + 真正的多模态图片输入（严格模式，缺图即报错）。"""
    evidence = build_evidence(question)
    assets = collect_question_images(question)
    if len(assets) > MAX_IMAGE_COUNT:
        raise ValueError("该题图像或公式超过 40 张，本机 AI 请求无法完整接收，请拆分题目后处理")
    subject = question.get("subject", "") or ""
    images: list[dict] = []
    total_image_size = 0
    for asset in assets:
        path = resolve_bank_asset(subject, asset)
        if path is None or path.suffix.lower() not in ALLOWED_IMAGE_SUFFIXES:
            raise ValueError("题目包含当前 AI 无法读取的图片或公式，暂不能声称已完整分析")
        total_image_size += path.stat().st_size
        if total_image_size > MAX_TOTAL_IMAGE_BYTES:
            raise ValueError("题图总量超过 20 MB，无法在当前请求中完整核验")
        images.append(image_part(path))
    return evidence, images


# ---------------------------------------------------------------------------
# 内容引用（content_refs）：golden-v2 可追溯记录与输入完整率共用
# ---------------------------------------------------------------------------

def make_ref(
    field: str,
    field_label: str,
    block_id,
    kind: str,
    *,
    role=None,
    asset=None,
    has_image: bool = False,
    required: bool = True,
    fallback_text=None,
    missing_reason=None,
) -> dict:
    return {
        "field": field,
        "field_label": field_label,
        "block_id": block_id,
        "kind": kind,
        "role": role,
        "asset": asset_file(asset),
        "has_image": bool(has_image),
        "required": bool(required),
        "fallback_text": fallback_text,
        "missing_reason": missing_reason,
    }


def _add_block_refs(add, block: dict, field: str, field_label: str, *, required: bool = True):
    kind = block_kind(block)
    block_id = block.get("block_id") or block.get("id")
    if kind == "image":
        asset = block.get("asset")
        if asset_file(asset):
            add(
                field, field_label, block_id, "image", role=asset_role(asset) or "figure",
                asset=asset, has_image=True, required=required,
            )
        else:
            add(
                field, field_label, block_id, "image", role="figure", has_image=True,
                required=required, missing_reason="题库图片块缺少素材文件",
            )
    elif kind == "math":
        fallback_asset = block.get("fallback_asset")
        fallback_text = str(block.get("latex") or block.get("text") or "").strip()
        if asset_file(fallback_asset):
            add(
                field, field_label, block_id, "image", role="formula", asset=fallback_asset,
                has_image=True, required=required, fallback_text=fallback_text or None,
            )
        elif fallback_text:
            add(
                field, field_label, block_id, "math", required=required,
                has_image=False, fallback_text=fallback_text,
            )
        else:
            add(
                field, field_label, block_id, "math", has_image=True, required=required,
                missing_reason="题库公式块缺少图片与可读文本",
            )
    elif kind == "table":
        if table_text(block).strip():
            add(field, field_label, block_id, "table", required=required)
    else:
        text = str(block.get("text") or "")
        if text.strip():
            add(field, field_label, block_id, "text", required=required)


def _append_unresolved_placeholders(add, view: dict, refs: list[dict]):
    """文本里有 [图N]/[公式N] 但题库没有对应图片时，显式记为缺项。"""
    fields = [("stem", "题干", view.get("stem_text") or "")]
    for option in view.get("options") or []:
        label = option.get("label") or ""
        field = f"option:{label}" if label else "option"
        fields.append((field, f"选项{label}" if label else "选项", option.get("text") or ""))
    if view.get("material_text"):
        fields.append(("material", "材料", view["material_text"]))
    if view.get("shared_text"):
        fields.append(("shared", "共用题干", view["shared_text"]))

    figure_placeholders = sum(len(FIGURE_PLACEHOLDER_RE.findall(text or "")) for _, _, text in fields)
    formula_placeholders = sum(len(FORMULA_PLACEHOLDER_RE.findall(text or "")) for _, _, text in fields)
    figure_images = sum(1 for ref in refs if ref.get("has_image") and ref.get("role") == "figure")
    formula_images = sum(1 for ref in refs if ref.get("has_image") and ref.get("role") == "formula")
    remaining = {
        "figure": max(0, figure_placeholders - figure_images),
        "formula": max(0, formula_placeholders - formula_images),
    }
    for role, pattern in (("figure", FIGURE_PLACEHOLDER_RE), ("formula", FORMULA_PLACEHOLDER_RE)):
        missing = remaining[role]
        if missing <= 0:
            continue
        for field, label, text in fields:
            if missing <= 0:
                break
            count = min(len(pattern.findall(text or "")), missing)
            for _ in range(count):
                add(
                    field, label, None, "image", role=role, has_image=True,
                    missing_reason="题库未提供对应的原卷图片",
                )
            missing -= count


def build_content_refs(question: dict, view: dict | None = None) -> list[dict]:
    """构建「计划送给模型」的内容引用：字段名、block_id、asset 文件名、是否含图。

    执行评测时会再补上 delivered 标记；题库缺图/缺资产在构建阶段就落成
    ``missing_reason``，便于单独定位题库缺项。原卷答案解析（answer/solution）
    的图片是核验证据的一部分，记为可选单元（required=False）：线上讲解/判分
    会送，离线独立求解不会把参考答案图送去泄题。
    """
    view = view or content_view(question)
    refs: list[dict] = []
    seen = set()

    def add(field, field_label, block_id, kind, *, role=None, asset=None,
            has_image=False, required=True, fallback_text=None, missing_reason=None):
        ref = make_ref(
            field, field_label, block_id, kind, role=role, asset=asset,
            has_image=has_image, required=required, fallback_text=fallback_text,
            missing_reason=missing_reason,
        )
        key = (
            ref["field"], ref["block_id"], ref["kind"], ref["asset"],
            ref["has_image"], ref["required"], ref["missing_reason"],
        )
        if key in seen:
            return
        seen.add(key)
        refs.append(ref)

    for block in view["stem_blocks"]:
        _add_block_refs(add, block, "stem", "题干")
    if not view["stem_blocks"] and view["stem_text"]:
        add("stem", "题干", None, "text")

    for option in view["options"]:
        label = option.get("label") or ""
        field = f"option:{label}" if label else "option"
        field_label = f"选项{label}" if label else "选项"
        blocks = option.get("blocks") or []
        if blocks:
            for block in blocks:
                _add_block_refs(add, block, field, field_label)
        elif option.get("text"):
            add(field, field_label, None, "text")

    for block in view["shared_blocks"]:
        _add_block_refs(add, block, "shared", "共用题干")
    if view["material_text"]:
        add("material", "材料", None, "text")
    if view["shared_text"] and not any(ref.get("field") == "shared" for ref in refs):
        add("shared", "共用题干", None, "text")

    # 原卷答案解析块：线上核验证据会送到模型，离线独立求解不送（可选单元）。
    for block in _json_list(question.get("answer_blocks")):
        _add_block_refs(add, block, "answer", "原卷答案", required=False)
    for block in _json_list(question.get("solution_blocks")):
        _add_block_refs(add, block, "solution", "原卷解析", required=False)

    # blocks 没覆盖到的题库图片（旧快照没有 blocks，只有 images_json）。
    existing_assets = {ref["asset"] for ref in refs if ref.get("asset")}
    for image in _json_list(question.get("images")):
        file = asset_file(image)
        if file and file not in existing_assets:
            add(
                "stem", "题干", None, "image", role=image_role(image) or "figure",
                asset=file, has_image=True,
            )
            existing_assets.add(file)
    for image in _json_list(view["group"].get("material_images")):
        file = asset_file(image)
        if file and file not in existing_assets:
            add(
                "material", "材料", None, "image", role=image_role(image) or "figure",
                asset=file, has_image=True,
            )
            existing_assets.add(file)

    _append_unresolved_placeholders(add, view, refs)
    return refs


__all__ = [
    "MAX_IMAGE_BYTES", "MAX_TOTAL_IMAGE_BYTES", "MAX_IMAGE_COUNT",
    "FIGURE_PLACEHOLDER_RE", "FORMULA_PLACEHOLDER_RE", "VISUAL_PLACEHOLDER_RE",
    "ALLOWED_IMAGE_SUFFIXES",
    "asset_file", "asset_role", "image_role", "block_kind", "cell_text", "table_text",
    "blocks_readable_text", "content_view", "iter_block_groups",
    "block_image_asset", "collect_question_images", "resolve_bank_asset", "image_part",
    "build_evidence", "collect_bank_evidence",
    "make_ref", "build_content_refs",
]
