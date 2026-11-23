"""Photo question pipeline: OCR-first intake (draft -> human confirm -> save to my bank) &
legacy multi-image recognize -> confirm -> AI solve & Admin batch KP tagging.

Phase 1（拍题入库主路径）:
- 上传后先跑本地 OCR（``app/ai/ocr.py``，无需视觉模型 Key）生成识别草稿；
- 视觉模型只作为「增强识别」可选动作，结果写入同一草稿；
- 人工确认与识别草稿分离：``intake_revision`` 乐观锁，异步识别在人工确认后只写
  ``pending_draft_json``（模型草稿对照），绝不覆盖 ``confirmed_json``；
- 确认后可直接保存到「我的题库」``pl_questions``（原图复制为 paperlab 资产）。
"""
import asyncio
from io import BytesIO
import json
import math
import os
from pathlib import Path
import re

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from .. import db, storage
from ..auth import current_user, require_admin
from .adapters import _strip_json_fences
from .notes_agent import _read_upload, _save_file
from .providers import _json, _owned, _selected_provider
from .verify import _upload_images

router = APIRouter(prefix="/api", tags=["Uploads & batch tagging"])

MAX_IMAGE_EDGE = 2048
MAX_TOTAL_UPLOAD_BYTES = 40 * 1024 * 1024
_EXIF_ORIENTATION = 274
_IMAGE_SUFFIXES = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}


def _valid_image(raw):
    try:
        with Image.open(BytesIO(raw)) as image:
            if image.width * image.height > 25_000_000:
                raise HTTPException(422, "图片像素过大，请先压缩到 2500 万像素以内")
            image.verify()
            if image.format not in ("PNG", "JPEG", "WEBP"):
                raise HTTPException(422, "仅支持 PNG、JPG、WebP 图片")
            return Image.MIME[image.format], _IMAGE_SUFFIXES[image.format]
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        raise HTTPException(422, "文件不是有效的图片") from None


def _normalize_question_image(raw: bytes):
    """服务端 EXIF 转正 + 最长边压缩（2048px）；无需处理时保留原始字节。

    手机拍照常见的 EXIF Orientation 会让题目倒置/横置，视觉模型识别率显著下降；
    同时 5 张大图会超出模型请求体积限制，这里统一转正并按最长边压缩。
    """
    try:
        with Image.open(BytesIO(raw)) as image:
            image_format = image.format or "PNG"
            mime = Image.MIME.get(image_format, "image/png")
            suffix = _IMAGE_SUFFIXES.get(image_format, ".png")
            try:
                orientation = image.getexif().get(_EXIF_ORIENTATION)
            except Exception:
                orientation = None
            needs_transpose = orientation not in (None, 1)
            if not needs_transpose and max(image.width, image.height) <= MAX_IMAGE_EDGE:
                return raw, mime, suffix
            image.load()
            if needs_transpose:
                image = ImageOps.exif_transpose(image)
            if max(image.width, image.height) > MAX_IMAGE_EDGE:
                image.thumbnail((MAX_IMAGE_EDGE, MAX_IMAGE_EDGE), Image.LANCZOS)
            buffer = BytesIO()
            if image_format in ("JPEG", "WEBP"):
                image.convert("RGB").save(buffer, image_format, quality=88, optimize=True)
                return buffer.getvalue(), Image.MIME[image_format], _IMAGE_SUFFIXES[image_format]
            image.save(buffer, "PNG", optimize=True)
            return buffer.getvalue(), "image/png", ".png"
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        raise HTTPException(422, "文件不是有效的图片") from None


def _check_upload_total(total_bytes: int):
    if total_bytes > MAX_TOTAL_UPLOAD_BYTES:
        raise HTTPException(422, "图片总大小超过 40 MB，请先压缩或分批上传")


def _persist_question(user_id: int, uploads: list[tuple[str, bytes, str, str]], text: str):
    stored = []
    try:
        for name, raw, mime, suffix in uploads:
            stored.append({"filename": name, "storage_name": _save_file(raw, suffix, mime), "media_type": mime})
        first, *extras = stored
        with db.connection() as con:
            upload_id = con.execute(
                "INSERT INTO ai_uploads(user_id,filename,storage_name,media_type,text_content,extra_files_json,status,created_at) "
                "VALUES (?,?,?,?,?,?,'uploaded',?)",
                (user_id, first["filename"], first["storage_name"], first["media_type"], text, _json(extras), db.utcnow()),
            ).lastrowid
        return upload_id
    except Exception:
        for item in stored:
            storage.discard_file("ai-files", item["storage_name"])
        raise


@router.post("/uploads/question")
async def upload_question(
    file: UploadFile | None = File(default=None),
    files: list[UploadFile] | None = File(default=None),
    user=Depends(current_user),
):
    incoming = [f for f in ([file] if file else []) + (files or []) if f is not None]
    if not incoming:
        raise HTTPException(422, "请上传至少一张题目图片或文本文件")
    if len(incoming) > 5:
        raise HTTPException(422, "一次最多上传 5 张题目图片")

    first = incoming[0]
    name, raw = await _read_upload(first)
    total_bytes = len(raw)
    _check_upload_total(total_bytes)
    suffix = Path(name).suffix.lower()
    text = ""
    if suffix in (".txt", ".md"):
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise HTTPException(422, "文本文件请使用 UTF-8 编码") from None
        if len(text) > 40_000 or not text.strip():
            raise HTTPException(422, "题目文本为空或超过 4 万字，请缩小范围")
        mime = "text/plain"
    else:
        mime, suffix = _valid_image(raw)
        raw, mime, suffix = await run_in_threadpool(_normalize_question_image, raw)

    uploads = [(name, raw, mime, suffix)]
    for extra in incoming[1:]:
        ename, eraw = await _read_upload(extra)
        total_bytes += len(eraw)
        _check_upload_total(total_bytes)
        emime, esuffix = _valid_image(eraw)
        eraw, emime, esuffix = await run_in_threadpool(_normalize_question_image, eraw)
        uploads.append((ename, eraw, emime, esuffix))

    upload_id = await run_in_threadpool(_persist_question, user["id"], uploads, text)

    return {
        "id": upload_id,
        "filename": name,
        "media_type": mime,
        "preview_url": f"/api/uploads/{upload_id}",
        "extra_previews": [
            {"index": i, "filename": extra[0], "preview_url": f"/api/uploads/{upload_id}/extra/{i}"}
            for i, extra in enumerate(uploads[1:])
        ],
        "extracted_text": text[:2000],
        "status": "uploaded",
    }


@router.get("/uploads/{upload_id}")
def view_upload(upload_id: int, user=Depends(current_user)):
    row = _owned("ai_uploads", upload_id, user["id"])
    return storage.file_response(
        "ai-files", row["storage_name"],
        media_type=row["media_type"],
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
    )


@router.get("/uploads/{upload_id}/extra/{index}")
def view_upload_extra(upload_id: int, index: int, user=Depends(current_user)):
    row = _owned("ai_uploads", upload_id, user["id"])
    extras = json.loads(row.get("extra_files_json") or "[]")
    if not 0 <= index < len(extras):
        raise HTTPException(404, "附加图片不存在")
    ef = extras[index]
    return storage.file_response(
        "ai-files", ef["storage_name"],
        media_type=ef["media_type"],
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
    )


# ---------------------------------------------------------------------------
# OCR-first 拍题入库（Phase 1）
#
# ai_uploads 在旧字段（recognized_json/confirmed_json/status）基础上新增：
# - intake_revision：人工确认版本；只在人工确认时 +1，异步识别回写前比较它；
# - draft_json：当前识别草稿（本地 OCR 或视觉「增强识别」），可被新草稿替换；
# - pending_draft_json：人工确认之后才到达的模型结果（模型草稿对照，需再次确认）；
# - ai_suggestion_json：AI 建议答案（与 OCR 答案分开展示，可逐字段采用）；
# - confirmed_at / saved_question_id / saved_at：确认与入库留痕。
# 迁移沿用 db.ensure_column；按数据库路径做一次性缓存，测试逐用例换库时也会重新补齐。
# ---------------------------------------------------------------------------

_INTAKE_COLUMNS = (
    ("intake_revision", "INTEGER NOT NULL DEFAULT 1"),
    ("draft_json", "TEXT"),
    ("pending_draft_json", "TEXT"),
    ("ai_suggestion_json", "TEXT"),
    ("confirmed_at", "TEXT"),
    ("saved_question_id", "INTEGER"),
    ("saved_at", "TEXT"),
)
_INTAKE_SCHEMA_READY: set[str] = set()

_OPTION_LINE_RE = re.compile(r"^([A-La-l])\s*[.．、:：)）]\s*(.+)$")
_ANSWER_LINE_RE = re.compile(r"^(?:参考)?答案\s*[:：]\s*(.+)$")
_MAX_INTAKE_TEXT = 8000

# 第五轮「OCR 内容块」：块列表与 schema 版本随草稿/人工确认一起存进 draft_json / confirmed_json
# （沿用 JSON TEXT，不改表结构）。本地引擎不做公式识别：math 块 latex=null、needs_review=true。
INTAKE_BLOCKS_SCHEMA_VERSION = "intake-blocks-v1"
CONFIRM_BLOCKS_MAX = 200
BLOCK_KIND_VALUES = frozenset({"text", "math", "table", "image"})
_BLOCK_TEXT_MAX = 4000
_BLOCK_LATEX_MAX = 2000
_BLOCK_ORDER_MAX = 500
_BLOCK_PAGE_MAX = 200

_INTAKE_TYPE_HINTS = {
    "MULTIPLE": ("多选", "多项选择", "不定项"),
    "SINGLE": ("单选", "单项选择", "选择题"),
    "BLANK": ("填空", "默写"),
    "SUBJECTIVE": ("简答", "解答", "计算", "证明", "作文", "论述", "材料", "主观", "问答"),
}


def _intake_schema_key() -> str:
    url = (os.environ.get("DATABASE_URL") or "").strip()
    return f"pg:{url}" if url else f"sqlite:{db.DATA_DIR}"


def _ensure_intake_schema(con=None) -> None:
    """补齐 ai_uploads 的入库字段（幂等）；SQLite/PG 都走 db.ensure_column。"""
    key = _intake_schema_key()
    if key in _INTAKE_SCHEMA_READY:
        return
    if con is None:
        with db.connection() as own:
            _ensure_intake_schema(own)
        return
    try:
        for column, definition in _INTAKE_COLUMNS:
            db.ensure_column(con, "ai_uploads", column, definition)
        # 读一次确认全部列可访问，避免并发 ALTER 半途失败后永久跳过后面的列。
        con.execute(
            "SELECT " + ", ".join(column for column, _ in _INTAKE_COLUMNS) + " FROM ai_uploads LIMIT 0"
        )
    except Exception:
        raise HTTPException(503, "拍题入库数据表尚未就绪，请稍后重试") from None
    _INTAKE_SCHEMA_READY.add(key)


def _json_load(value, default=None):
    if value is None or value == "":
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _read_intake_row(upload_id: int, user_id: int) -> dict:
    _ensure_intake_schema()
    with db.connection() as con:
        row = con.execute(
            "SELECT * FROM ai_uploads WHERE id=? AND user_id=?", (upload_id, user_id),
        ).fetchone()
    if row is None:
        raise HTTPException(404, "未找到该上传题目")
    return dict(row)


def _intake_revision(row: dict) -> int:
    try:
        return max(1, int(row.get("intake_revision") or 1))
    except (TypeError, ValueError):
        return 1


def _intake_state(row: dict) -> dict:
    draft = _json_load(row.get("draft_json"))
    pending = _json_load(row.get("pending_draft_json"))
    confirmed = _json_load(row.get("confirmed_json"))
    suggestion = _json_load(row.get("ai_suggestion_json"))
    revision = _intake_revision(row)
    return {
        "upload_id": row.get("id"),
        "filename": row.get("filename") or "",
        "media_type": row.get("media_type") or "",
        "preview_url": f"/api/uploads/{int(row.get('id') or 0)}",
        "status": row.get("status") or "uploaded",
        "revision": revision,
        "confirmed": confirmed if isinstance(confirmed, dict) else None,
        "draft": draft if isinstance(draft, dict) else None,
        "pending_draft": pending if isinstance(pending, dict) else None,
        "ai_suggestion": suggestion if isinstance(suggestion, dict) else None,
        "confirmed_at": row.get("confirmed_at"),
        "saved_question_id": int(row.get("saved_question_id") or 0) or None,
        "saved_at": row.get("saved_at"),
        "extracted_text": str(row.get("text_content") or "")[:2000],
        "intake": True,
    }


def _empty_intake_draft(source: str = "local_ocr") -> dict:
    return {
        "subject": "", "type": "", "stem": "", "options": [], "answers": [],
        "conditions": [], "knowledge_tags": [], "explanation": "", "uncertain": [],
        "source": source, "ocr_text": "", "ocr_notice": "",
    }


def _local_ocr_meta(module) -> dict:
    """本地 OCR 引擎名/版本/能力（不加载模型），供 ocr-draft 等响应复用。

    模块未提供（或导入失败时传入 None）返回空结构，避免响应缺字段。
    """
    meta = {
        "engine": str(getattr(module, "ENGINE_NAME", "") or ""),
        "engine_version": None,
        "capabilities": dict(getattr(module, "CAPABILITIES", {}) or {}),
    }
    provider = getattr(module, "engine_meta", None)
    if callable(provider):
        try:
            value = provider()
        except Exception:  # 能力信息只是增强字段，读取失败不影响 OCR 主流程
            value = None
        if isinstance(value, dict):
            meta.update(value)
    return meta


async def _ocr_result_blocks(module, result, *, page: int = 1, image=None) -> list[dict]:
    """把 ``recognize_image`` 的行结果按统一口径构造成内容块，并按开关做后处理。

    块构造放在 ``app.ai.ocr`` 的 :func:`build_blocks`，这里只负责跨模块调用，
    保证「原图定位/阅读顺序/kind 判定」只有一处口径。多图拍题的 ``page`` 为图片序号；
    ``image`` 为当次上传的原始图片，传入后由 ``ocr.enrich_blocks`` 按开关补公式/表格结构。
    """
    if not isinstance(result, dict):
        return []
    builder = getattr(module, "build_blocks", None)
    if not callable(builder):
        return []
    try:
        blocks = builder(
            result.get("lines") or [],
            result.get("width") or 0,
            result.get("height") or 0,
            page=page,
        )
    except (TypeError, ValueError):
        return []
    enricher = getattr(module, "enrich_blocks", None)
    if image is not None and blocks and callable(enricher):
        try:
            outcome = await enricher(
                blocks,
                image,
                page=page,
                width=result.get("width") or 0,
                height=result.get("height") or 0,
            )
        except Exception:  # 后处理只是增强：失败时保留基础块
            return blocks
        if isinstance(outcome, dict) and isinstance(outcome.get("blocks"), list):
            return outcome["blocks"]
    return blocks


def _parse_question_text(text: str, *, source: str = "local_ocr", blocks=None) -> dict:
    """把 OCR/文本初稿拆成题干/选项/答案的机器草稿（启发式，可能出错，仅预填）。

    ``blocks`` 为本地 OCR 产出的内容块（含原图位置/阅读顺序/置信度）；只有调用方确实处理了
    块（传入列表，含空列表 ``[]``）时才在草稿里写 ``schema_version`` 与 ``blocks``，
    避免给旧草稿凭空加键。题干/选项/答案仍走原有行启发式，块文本不改变旧拆分口径。
    """
    draft = _empty_intake_draft(source)
    stem_lines: list[str] = []
    option_lines: list[tuple[str, str]] = []
    answers: list[str] = []
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        answer_match = _ANSWER_LINE_RE.match(line)
        if answer_match:
            value = answer_match.group(1).strip()
            if value:
                answers.append(value[:4000])
            continue
        option_match = _OPTION_LINE_RE.match(line)
        if option_match:
            option_lines.append((option_match.group(1).upper(), option_match.group(2).strip()[:4000]))
            continue
        stem_lines.append(line)

    labels = [label for label, _ in option_lines]
    expected = list("ABCDEFGHIJKLMNOPQRSTUVWXYZ"[:len(option_lines)])
    if len(option_lines) >= 2 and labels == expected and all(text_value for _, text_value in option_lines):
        draft["options"] = [{"label": label, "text": value} for label, value in option_lines]
    else:
        # 不是连续 A/B/C… 选项：按普通题干行还原，避免误拆。
        stem_lines.extend(f"{label}. {value}" for label, value in option_lines)
    draft["stem"] = "\n".join(stem_lines).strip()[:20000]
    draft["answers"] = answers[:8]
    if not draft["stem"] and not draft["options"] and not draft["answers"]:
        draft["uncertain"] = ["没有识别出可用文字，请重拍、裁剪或手动录入"]
    if blocks is not None:
        draft["schema_version"] = INTAKE_BLOCKS_SCHEMA_VERSION
        draft["blocks"] = [dict(block) for block in blocks if isinstance(block, dict)]
    return draft


def _load_intake_image_bytes(row: dict) -> list[bytes]:
    references = []
    for extra in _json_load(row.get("extra_files_json"), []) or []:
        if isinstance(extra, dict) and extra.get("storage_name"):
            references.append(str(extra["storage_name"]))
    references.insert(0, str(row.get("storage_name") or ""))
    images: list[bytes] = []
    for reference in references:
        if not reference:
            continue
        try:
            raw = storage.read_file("ai-files", reference)
        except HTTPException:
            continue
        if raw:
            images.append(raw)
    return images


@router.get("/uploads/{upload_id}/intake")
def get_upload_intake(upload_id: int, user=Depends(current_user)):
    """拍题草稿状态（任务中心深链/刷新恢复用）：草稿、人工确认、待确认模型草稿与入库结果。"""
    row = _read_intake_row(upload_id, user["id"])
    return _intake_state(row)


@router.post("/uploads/{upload_id}/ocr-draft")
async def create_ocr_draft(upload_id: int, user=Depends(current_user)):
    """OCR-first：本地 OCR 生成/刷新识别草稿（不需要任何视觉模型 Key）。

    识别不可用/超时返回 200 + ``ocr_available=false``（保留原图，前端提示重试/手录）；
    图片本身损坏返回 422。已人工确认的题目只更新草稿，不改写确认字段。

    响应新增（旧字段全部保留）：
    * ``blocks``：当前草稿的内容块（含原图位置/阅读顺序/置信度；无块时为 ``[]``）；
    * ``engine`` / ``engine_version`` / ``capabilities``：本地引擎与能力声明（不加载模型）；
    * 草稿 ``draft.schema_version = "intake-blocks-v1"`` + ``draft.blocks``：新页面用于
      原图定位与公式校对；文本上传等没有块的旧路径不凭空追加这些键。
    """
    from . import ocr as local_ocr

    engine_meta = _local_ocr_meta(local_ocr)
    row = _read_intake_row(upload_id, user["id"])
    status_before = str(row.get("status") or "uploaded")
    media_type = str(row.get("media_type") or "")
    text = str(row.get("text_content") or "").strip()
    notice = ""
    blocks: list[dict] = []

    if text and not media_type.startswith("image/"):
        draft = _parse_question_text(text, source="text")
        draft["ocr_text"] = text[:_MAX_INTAKE_TEXT]
        draft["ocr_notice"] = ""
        ocr_available = True
    else:
        images = await run_in_threadpool(_load_intake_image_bytes, row)
        if not images:
            raise HTTPException(422, "该上传没有可识别的图片，请重新拍照或手动录入")
        parts: list[str] = []
        ocr_available = True
        for index, raw in enumerate(images, 1):
            try:
                result = await local_ocr.recognize_image(data=raw)
            except local_ocr.OcrUnavailable as error:
                ocr_available = False
                notice = str(error)
                break
            except local_ocr.OcrTimeout as error:
                ocr_available = False
                notice = str(error)
                break
            except ValueError as error:
                raise HTTPException(422, str(error)) from None
            except local_ocr.OcrError as error:
                ocr_available = False
                notice = str(error)
                break
            line_text = str((result or {}).get("text") or "").strip()
            if line_text:
                prefix = f"【第 {index} 张图】\n" if len(images) > 1 else ""
                parts.append(prefix + line_text)
            # 多图拍题用图片序号作为 page（1 基），单图固定 1；块构造口径只在 ocr.build_blocks。
            blocks.extend(await _ocr_result_blocks(
                local_ocr, result, page=index if len(images) > 1 else 1, image=raw,
            ))
        if not ocr_available:
            return {
                "upload_id": upload_id,
                "revision": _intake_revision(row),
                "ocr_available": False,
                "ocr_notice": notice,
                "blocks": [],
                "block_notice": str(getattr(local_ocr, "BLOCKS_NOTICE", "") or ""),
                "draft": _json_load(row.get("draft_json")),
                "status": status_before,
                "intake": _intake_state(row),
                "notice": notice + "；原图已保留，可重试或直接手动录入。",
                **engine_meta,
            }
        ocr_text = "\n\n".join(parts)[:_MAX_INTAKE_TEXT]
        draft = _parse_question_text(ocr_text, source="local_ocr", blocks=blocks)
        draft["ocr_text"] = ocr_text
        draft["ocr_notice"] = getattr(local_ocr, "OCR_NOTICE", "")
        if not ocr_text and not draft["uncertain"]:
            draft["uncertain"] = ["本地 OCR 未识别出文字，请重拍/裁剪后重试或手动录入"]
        if not ocr_text:
            notice = "本地 OCR 未识别出文字，请重拍、裁剪后重试或手动录入"

    draft["created_at"] = db.utcnow()
    # 本地 OCR 草稿不递增 intake_revision：版本只在人工确认时前进。
    new_status = "confirmed" if status_before == "confirmed" else "draft"
    with db.connection() as con:
        _ensure_intake_schema(con)
        con.execute(
            "UPDATE ai_uploads SET draft_json=?, status=? WHERE id=? AND user_id=?",
            (_json(draft), new_status, upload_id, user["id"]),
        )
    fresh = _read_intake_row(upload_id, user["id"])
    return {
        "upload_id": upload_id,
        "revision": _intake_revision(fresh),
        "ocr_available": True,
        "ocr_notice": draft.get("ocr_notice") or "",
        "blocks": [block for block in draft.get("blocks") or [] if isinstance(block, dict)],
        "block_notice": str(getattr(local_ocr, "BLOCKS_NOTICE", "") or ""),
        "draft": draft,
        "status": new_status,
        "intake": _intake_state(fresh),
        "notice": notice or "本地 OCR 草稿已预填；机器识别可能有错字、漏字或公式偏差，请逐字核对后再确认。",
        **engine_meta,
    }


class AiAnswerInput(BaseModel):
    provider_id: int
    model: str = Field(min_length=1, max_length=160)
    expected_version: int | None = Field(default=None, ge=1)


@router.post("/uploads/{upload_id}/ai-answer")
async def suggest_answer(upload_id: int, data: AiAnswerInput, user=Depends(current_user)):
    """AI 建议答案（与 OCR 答案分开展示，逐字段采用）；需要已配置模型连接。"""
    from . import _call_model as call_model

    row = _read_intake_row(upload_id, user["id"])
    revision = _intake_revision(row)
    if data.expected_version is not None and int(data.expected_version) != revision:
        raise HTTPException(409, "题目已更新（版本冲突），请刷新页面后重试")
    confirmed = _json_load(row.get("confirmed_json"), {}) or {}
    draft = _json_load(row.get("draft_json"), {}) or {}
    base = {**draft, **{key: value for key, value in confirmed.items() if value not in (None, "", [], {})}}
    stem = str(base.get("stem") or "").strip()
    if not stem:
        raise HTTPException(422, "请先识别或填写题干，再请求 AI 建议答案")
    options_text = "\n".join(
        f"{option.get('label')}. {option.get('text')}"
        for option in (base.get("options") or [])
        if isinstance(option, dict) and option.get("text")
    )
    prompt = (
        "你是高中题目教研员。请解答下面这道题，并严格返回 JSON 对象：\n"
        '{"answers":["最终答案（选择题用选项标号，填空按空给）"],'
        '"explanation":"完整的解题解析",'
        '"rubric":"主观题评分标准，客观题可为空字符串",'
        '"knowledge_tags":["1-3 个知识点"],'
        '"uncertain":["信息不足或有歧义的地方"]}\n'
        "不要编造题目没有的条件；信息不足时在 uncertain 说明并给出最可能的答案。\n\n"
        f"题目：\n{stem}"
        + (f"\n选项：\n{options_text}" if options_text else "")
        + (
            "\n补充条件：" + "；".join(str(item) for item in (base.get("conditions") or []) if str(item).strip())
            if base.get("conditions") else ""
        )
    )
    provider = _selected_provider(user["id"], data.provider_id, data.model)
    try:
        call = await call_model(
            provider, data.model, prompt, json_output=True, token_limit=2400, user_id=user["id"],
        )
    except HTTPException:
        raise
    except Exception as exc:  # 模型调用失败：明确提示，不静默降级
        raise HTTPException(502, f"AI 建议答案生成失败，请检查模型配置后重试：{exc}") from None
    parsed = {}
    try:
        candidate = json.loads(_strip_json_fences(str((call or {}).get("content") or "")))
        if isinstance(candidate, dict):
            parsed = candidate
    except (TypeError, ValueError):
        parsed = {}
    if not parsed:
        raise HTTPException(502, "AI 建议答案未按结构化格式返回，请重试")
    suggestion = {
        "answers": [str(item).strip() for item in (parsed.get("answers") or []) if str(item).strip()][:8],
        "explanation": str(parsed.get("explanation") or "").strip()[:20000],
        "rubric": str(parsed.get("rubric") or "").strip()[:20000],
        "knowledge_tags": [str(item).strip()[:80] for item in (parsed.get("knowledge_tags") or []) if str(item).strip()][:10],
        "uncertain": [str(item).strip()[:200] for item in (parsed.get("uncertain") or []) if str(item).strip()][:10],
        "model": str((call or {}).get("model") or data.model),
        "provider_name": str((call or {}).get("provider_name") or ""),
        "created_at": db.utcnow(),
        "source": "ai",
    }
    with db.connection() as con:
        _ensure_intake_schema(con)
        con.execute(
            "UPDATE ai_uploads SET ai_suggestion_json=? WHERE id=? AND user_id=?",
            (_json(suggestion), upload_id, user["id"]),
        )
    fresh = _read_intake_row(upload_id, user["id"])
    return {
        "upload_id": upload_id,
        "revision": _intake_revision(fresh),
        "ai_suggestion": suggestion,
        "notice": "这是「AI 建议答案」，仅供参考；请对照原题与 OCR 结果后再采用。",
        "intake": _intake_state(fresh),
    }


class SaveToBankInput(BaseModel):
    expected_version: int | None = Field(default=None, ge=1)
    subject: str = Field(default="", max_length=40)
    type: str = Field(default="", max_length=40)
    grade: str = Field(default="", max_length=40)
    difficulty: float | None = Field(default=None, ge=0, le=1)
    stem: str = Field(default="", max_length=20000)
    options: list[dict] | None = Field(default=None, max_length=12)
    answers: list[str] | None = Field(default=None, max_length=8)
    explanation: str = Field(default="", max_length=20000)
    conditions: list[str] | None = Field(default=None, max_length=20)
    knowledge_tags: list[str] | None = Field(default=None, max_length=30)
    include_original_image: bool = True


def _normalize_intake_options(raw) -> list[dict]:
    options: list[dict] = []
    for index, item in enumerate(raw or []):
        if isinstance(item, dict):
            label = str(item.get("label") or "").strip().upper()
            text_value = str(item.get("text") or "").strip()
        else:
            label, text_value = "", str(item or "").strip()
        if not text_value:
            continue
        if not label:
            label = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"[index % 26]
        options.append({"label": label[:4], "text": text_value[:4000]})
    return options[:12]


def _normalize_intake_answers(raw) -> list[str]:
    answers: list[str] = []
    for item in raw or []:
        value = str(item or "").strip()
        if value:
            answers.append(value[:4000])
    return answers[:8]


def _intake_qtype(raw_type, options: list[dict], answers: list[str]) -> str:
    """把模型/OCR 给出的题型文本映射到 paperlab 的 SINGLE/MULTIPLE/BLANK/SUBJECTIVE。"""
    from ..paperlab import schema as paperlab_schema

    normalized = paperlab_schema.normalize_qtype(str(raw_type or "")) if str(raw_type or "").strip() else None
    if normalized in ("SINGLE", "MULTIPLE") and not options:
        normalized = None
    if normalized == "MIXED":
        normalized = None
    if normalized:
        return normalized
    text = str(raw_type or "").strip()
    if options:
        return "MULTIPLE" if len(answers) > 1 else "SINGLE"
    for candidate, hints in _INTAKE_TYPE_HINTS.items():
        if any(hint in text for hint in hints):
            return candidate
    return "SUBJECTIVE"


def _choice_answer_labels(answers: list[str], options: list[dict]) -> tuple[list[str], list[str]]:
    """选择题答案归一到选项标号：返回 (可写入的标号, 无法解析的原文)。"""
    labels = {str(option.get("label") or "").strip().upper() for option in options}
    resolved: list[str] = []
    unresolved: list[str] = []
    for answer in answers:
        text = str(answer or "").strip().upper().replace(" ", "")
        letters = [char for char in text if char in labels]
        if letters and len(letters) == len([char for char in text if char.isalnum() and char in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"]):
            for letter in letters:
                if letter not in resolved:
                    resolved.append(letter)
        elif text in labels:
            resolved.append(text)
        else:
            unresolved.append(str(answer))
    return resolved, unresolved


@router.post("/uploads/{upload_id}/save-to-bank")
def save_upload_to_bank(upload_id: int, data: SaveToBankInput, user=Depends(current_user)):
    """把人工确认后的拍题写入「我的题库」（pl_questions），保留原图引用。

    - 必须已人工确认；``saved_question_id`` 存在时幂等返回同一题；
    - 保存失败抛带中文提示的错误（前端显式提示并允许重试），不静默吞错。
    """
    row = _read_intake_row(upload_id, user["id"])
    revision = _intake_revision(row)
    if data.expected_version is not None and int(data.expected_version) != revision:
        raise HTTPException(409, "题目已更新（版本冲突），请刷新页面后重试")
    confirmed = _json_load(row.get("confirmed_json"), None)
    draft = _json_load(row.get("draft_json"), {}) or {}
    if not isinstance(confirmed, dict) or not confirmed:
        raise HTTPException(409, "请先人工确认题干后再保存到我的题库")
    base = {**draft, **confirmed}

    existing_id = int(row.get("saved_question_id") or 0)
    if existing_id:
        from ..paperlab import db as paperlab_db

        try:
            existing = paperlab_db.fetch_question(existing_id, user["id"])
        except HTTPException:
            existing = None
        if existing:
            return {
                "saved": True,
                "already_saved": True,
                "saved_question_id": existing_id,
                "question": paperlab_db.public_question(existing),
                "revision": revision,
                "intake": _intake_state(row),
                "notice": "该题已保存到我的题库，返回的是同一条记录。",
            }

    stem = str(data.stem or base.get("stem") or "").strip()
    if not stem:
        raise HTTPException(422, "题干为空，请先确认或填写题干后再保存")
    subject = str(data.subject or base.get("subject") or "").strip() or "其他"
    raw_type = str(data.type if data.type is not None else base.get("type") or "")
    options = _normalize_intake_options(data.options if data.options is not None else base.get("options"))
    answers = _normalize_intake_answers(data.answers if data.answers is not None else base.get("answers"))
    explanation = str(data.explanation if data.explanation else base.get("explanation") or "").strip()
    knowledge_tags = [
        str(tag).strip()[:80]
        for tag in (data.knowledge_tags if data.knowledge_tags is not None else base.get("knowledge_tags") or [])
        if str(tag).strip()
    ][:30]
    qtype = _intake_qtype(raw_type, options, answers)
    if qtype in ("SINGLE", "MULTIPLE"):
        answers, unresolved = _choice_answer_labels(answers, options)
        if unresolved and not answers:
            # 选项标号无法解析：保留原文到解析里，避免因 OCR 噪声导致整题保存失败。
            note = "识别答案：" + "；".join(unresolved)
            explanation = (note + ("\n" + explanation if explanation else "")).strip()
        if qtype == "SINGLE" and len(answers) > 1:
            qtype = "MULTIPLE"
        if qtype == "MULTIPLE" and len(answers) == 1:
            pass
    conditions = [
        str(item).strip() for item in (
            data.conditions if data.conditions is not None else base.get("conditions") or []
        ) if str(item).strip()
    ][:20]
    content: dict = {
        "stem": stem,
        "options": options,
        "answers": answers,
        "explanation": explanation,
        "rubric": str(confirmed.get("rubric") or "").strip(),
        "parts": [],
        "images": [],
    }
    if conditions:
        content["conditions"] = conditions

    images: list[dict] = []
    if data.include_original_image and str(row.get("media_type") or "").startswith("image/"):
        from ..paperlab import assets as paperlab_assets

        try:
            raw_image = storage.read_file("ai-files", str(row.get("storage_name") or ""))
        except HTTPException:
            raw_image = b""
        if raw_image:
            try:
                asset_id = paperlab_assets.save_asset_bytes(
                    user["id"], raw_image, media_type=str(row.get("media_type") or "image/png"),
                    purpose="question_image",
                )
            except HTTPException:
                raise
            except Exception as exc:
                raise HTTPException(502, f"原图写入我的题库失败，请重试：{exc}") from None
            images.append({"asset": asset_id, "role": "original", "source": "upload"})
    content["images"] = images

    from ..paperlab import bank as paperlab_bank

    payload = paperlab_bank.QuestionCreateInput(
        type=qtype,
        subject=subject,
        grade=str(data.grade or base.get("grade") or "").strip(),
        difficulty=data.difficulty,
        knowledge_tags=knowledge_tags,
        content=content,
        status="ready" if answers else "draft",
    )
    try:
        created = paperlab_bank.create_question(payload, user)
    except HTTPException:
        raise
    except Exception as exc:  # 保存失败必须显式反馈，前端可重试
        raise HTTPException(502, f"保存到我的题库失败，请重试：{exc}") from None
    question = (created or {}).get("question") or {}
    saved_id = int(question.get("id") or 0)
    now = db.utcnow()
    with db.connection() as con:
        _ensure_intake_schema(con)
        con.execute(
            "UPDATE ai_uploads SET saved_question_id=?, saved_at=? WHERE id=? AND user_id=?",
            (saved_id, now, upload_id, user["id"]),
        )
    fresh = _read_intake_row(upload_id, user["id"])
    return {
        "saved": True,
        "already_saved": False,
        "saved_question_id": saved_id,
        "question": question,
        "revision": _intake_revision(fresh),
        "intake": _intake_state(fresh),
        "notice": "已保存到我的题库；可在「我的题库」编辑、核验或组卷。",
    }


class ConfirmUploadInput(BaseModel):
    # 兼容旧前端：不带 expected_version 时按旧行为直接确认；新前端带 intake_revision 做乐观锁。
    expected_version: int | None = Field(default=None, ge=1)
    subject: str = Field(default="", max_length=40)
    type: str = Field(default="", max_length=40)
    stem: str = Field(min_length=1, max_length=20000)
    options: list[dict] = Field(default_factory=list, max_length=12)
    conditions: list[str] = Field(default_factory=list, max_length=20)
    answers: list[str] | None = Field(default=None, max_length=8)
    explanation: str = Field(default="", max_length=20000)
    knowledge_tags: list[str] | None = Field(default=None, max_length=30)
    # 第五轮：人工确认的 OCR 内容块（含原图位置/阅读顺序/置信度）。可选：
    # 旧客户端不传时沿用 draft/confirmed 里已有 blocks，不丢数据。
    blocks: list[dict] | None = Field(default=None, max_length=CONFIRM_BLOCKS_MAX)


def _validate_confirm_blocks(raw, notices: list[str] | None = None) -> list[dict]:
    """校验人工确认请求里的识别块（只校验已知字段，未知字段原样保留）。

    规则（非法一律 422 + 中文提示）：
    * 每块必须是非空对象，最多 ``CONFIRM_BLOCKS_MAX`` 块；
    * ``kind`` 若提供必须属于 ``BLOCK_KIND_VALUES``（text/math/table/image）；
    * ``text`` ≤4000 字、``latex`` ≤2000 字（可为 null）、``order`` 0–500、``page`` 1–200；
    * ``bbox``/``norm`` 若提供必须是对象，x/y/w/h 为有限数值或缺失；
    * ``table_html`` 必须能通过服务端白名单清洗且 ≤20000 字；不通过时**丢弃该字段**
      并在 ``notices`` 里说明（不整单 422，文字仍可确认）；
    * ``row_count``/``col_count`` 若提供必须是 1–100 的整数，否则丢弃。
    """
    if not isinstance(raw, list):
        raise HTTPException(422, "blocks 必须是识别块列表")
    if len(raw) > CONFIRM_BLOCKS_MAX:
        raise HTTPException(422, f"识别块最多 {CONFIRM_BLOCKS_MAX} 个，请精简后再确认")
    blocks: list[dict] = []
    for position, block in enumerate(raw, 1):
        if not isinstance(block, dict) or not block:
            raise HTTPException(422, f"第 {position} 个识别块为空或格式无效")
        kind = block.get("kind")
        if kind is not None and (
            not isinstance(kind, str) or kind.strip().lower() not in BLOCK_KIND_VALUES
        ):
            raise HTTPException(
                422, f"第 {position} 个识别块 kind 无效（仅支持 text、math、table、image）"
            )
        text_value = block.get("text")
        if text_value is not None:
            if not isinstance(text_value, str):
                raise HTTPException(422, f"第 {position} 个识别块 text 必须是字符串")
            if len(text_value) > _BLOCK_TEXT_MAX:
                raise HTTPException(422, f"第 {position} 个识别块 text 不能超过 {_BLOCK_TEXT_MAX} 字")
        latex = block.get("latex")
        if latex is not None:
            if not isinstance(latex, str):
                raise HTTPException(422, f"第 {position} 个识别块 latex 必须是字符串或 null")
            if len(latex) > _BLOCK_LATEX_MAX:
                raise HTTPException(422, f"第 {position} 个识别块 latex 不能超过 {_BLOCK_LATEX_MAX} 字")
        order = block.get("order")
        if order is not None:
            if isinstance(order, bool) or not isinstance(order, int) or not 0 <= order <= _BLOCK_ORDER_MAX:
                raise HTTPException(422, f"第 {position} 个识别块 order 必须是 0–{_BLOCK_ORDER_MAX} 的整数")
        page = block.get("page")
        if page is not None:
            if isinstance(page, bool) or not isinstance(page, int) or not 1 <= page <= _BLOCK_PAGE_MAX:
                raise HTTPException(422, f"第 {position} 个识别块 page 必须是 1–{_BLOCK_PAGE_MAX} 的整数")
        for field in ("bbox", "norm"):
            value = block.get(field)
            if value is None:
                continue
            if not isinstance(value, dict):
                raise HTTPException(422, f"第 {position} 个识别块 {field} 必须是对象")
            for axis in ("x", "y", "w", "h"):
                if axis not in value:
                    continue
                number = value[axis]
                if (
                    isinstance(number, bool)
                    or not isinstance(number, (int, float))
                    or not math.isfinite(float(number))
                ):
                    raise HTTPException(422, f"第 {position} 个识别块 {field}.{axis} 必须是数值")
        table_html = block.get("table_html")
        if table_html is not None:
            cleaned = None
            if isinstance(table_html, str):
                try:
                    from . import table_structure
                    cleaned = table_structure.sanitize_table_html(table_html)
                except (ValueError, TypeError):
                    cleaned = None
            if cleaned:
                block["table_html"] = cleaned
            else:
                block.pop("table_html", None)
                if notices is not None:
                    notices.append(
                        f"第 {position} 个识别块的表格结构无效或超长（上限 20000 字），已丢弃，文字内容仍保留"
                    )
        for field in ("row_count", "col_count"):
            value = block.get(field)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 100:
                block.pop(field, None)
                if notices is not None:
                    notices.append(f"第 {position} 个识别块的 {field} 无效，已丢弃")
        blocks.append(dict(block))
    return blocks


@router.patch("/uploads/{upload_id}/confirm")
def confirm_upload(upload_id: int, data: ConfirmUploadInput, user=Depends(current_user)):
    """人工确认题干/答案（识别草稿 -> 人工确认）。

    - ``expected_version`` 对应 ai_uploads.intake_revision；不一致返回 409，避免覆盖他人/他端修改；
    - ``blocks`` 为可选内容块：传了就校验并覆盖写入 ``confirmed_json.blocks``；不传则沿用
      draft/confirmed 里已有块（旧客户端/旧草稿没有 blocks 时不追加空键，保持旧 JSON 原样）；
    - 确认会递增 intake_revision，并清空 pending_draft_json（模型草稿已被用户看过并处理）；
    - 异步识别任务在确认后只把模型结果写入 pending_draft_json，不改 confirmed_json。
    """
    from .. import practice

    row = _read_intake_row(upload_id, user["id"])
    current_revision = _intake_revision(row)
    if data.expected_version is not None and int(data.expected_version) != current_revision:
        raise HTTPException(409, "题目已被其他操作更新（版本冲突），请刷新页面后重试")
    block_notices: list[str] = []
    blocks = (
        _validate_confirm_blocks(data.blocks, block_notices)
        if data.blocks is not None else None
    )
    confirmed = _json_load(row.get("confirmed_json"), {}) or {}
    draft = _json_load(row.get("draft_json"), {}) or {}
    base = {**draft, **confirmed}
    if blocks is None:
        inherited = base.get("blocks")
        blocks = inherited if isinstance(inherited, list) else None
    answers = list(data.answers) if data.answers is not None else list(base.get("answers") or [])
    knowledge_tags = (
        list(data.knowledge_tags) if data.knowledge_tags is not None
        else list(base.get("knowledge_tags") or [])
    )
    payload = {
        "subject": data.subject,
        "type": data.type,
        "stem": data.stem.strip(),
        "options": data.options,
        "conditions": data.conditions,
        "answers": [str(item).strip() for item in answers if str(item).strip()][:8],
        "explanation": data.explanation if data.explanation else str(base.get("explanation") or "").strip(),
        "knowledge_tags": [str(item).strip()[:80] for item in knowledge_tags if str(item).strip()][:30],
    }
    if blocks is not None:
        payload["blocks"] = blocks
        payload["schema_version"] = str(
            base.get("schema_version") or INTAKE_BLOCKS_SCHEMA_VERSION
        )
    now = db.utcnow()
    with db.connection() as con:
        _ensure_intake_schema(con)
        changed = con.execute(
            "UPDATE ai_uploads SET confirmed_json=?, text_content=?, status='confirmed', "
            "intake_revision=?, confirmed_at=?, pending_draft_json=NULL "
            "WHERE id=? AND user_id=? AND intake_revision=?",
            (_json(payload), data.stem.strip(), current_revision + 1, now,
             upload_id, user["id"], current_revision),
        ).rowcount
        if not changed:
            raise HTTPException(409, "题目已被其他操作更新（版本冲突），请刷新页面后重试")
    revision = current_revision + 1
    similar = practice.search_similar_questions(data.stem, subject=data.subject or None, limit=5)
    fresh = _read_intake_row(upload_id, user["id"])
    response = {
        "id": upload_id,
        "status": "confirmed",
        "confirmed": payload,
        "similar_questions": similar,
        "revision": revision,
        "intake": _intake_state(fresh),
    }
    if block_notices:
        response["block_notices"] = block_notices
    return response


@router.post("/uploads/{upload_id}/similar")
def find_similar_for_upload(upload_id: int, user=Depends(current_user)):
    from .. import practice
    row = _owned("ai_uploads", upload_id, user["id"])
    confirmed = json.loads(row["confirmed_json"]) if row.get("confirmed_json") else None
    recognized = json.loads(row["recognized_json"]) if row.get("recognized_json") else None
    text = (confirmed or {}).get("stem") or (recognized or {}).get("stem") or row.get("text_content") or ""
    subject = (confirmed or {}).get("subject") or (recognized or {}).get("subject") or None
    similar = practice.search_similar_questions(text, subject=subject, limit=5)
    return {"items": similar, "has_exact_match": any(s.get("exact_match") for s in similar)}


LOCAL_OCR_TEXT_LIMIT = 8000  # 本地 OCR 文本注入提示/返回给用户校对的最大字符数


async def _local_ocr_question_text(images) -> tuple[str, str | None, list[dict]]:
    """拍题识别前先跑本地 OCR：文本用于视觉提示校对，同时带回可供原图定位的内容块。

    返回 ``(文本, 提示, blocks)``；本地 OCR 不可用/单张失败不阻断视觉识别
    （返回空文本/空块与提示），绝不返回占位文本。多图拍题的块 ``page`` 为图片序号（1 基）。
    """
    if not images:
        return "", None, []
    try:
        from . import ocr as local_ocr
    except Exception:  # 本地 OCR 模块导入失败按未启用处理
        return "", None, []
    parts: list[str] = []
    blocks: list[dict] = []
    notice = None
    multi = len(images) > 1
    for index, image in enumerate(images, 1):
        data = image.get("data") if isinstance(image, dict) else None
        if not isinstance(data, str) or not data:
            continue
        try:
            result = await local_ocr.recognize_image(image)
        except local_ocr.OcrUnavailable as error:
            notice = str(error)
            break
        except local_ocr.OcrTimeout as error:
            notice = str(error)
            break
        except Exception:  # 单张图片本地识别失败：继续尝试下一张，不影响视觉识别
            continue
        text = str(result.get("text") or "").strip() if isinstance(result, dict) else ""
        if text:
            parts.append(f"【第 {index} 张图】\n{text}" if multi else text)
        blocks.extend(await _ocr_result_blocks(
            local_ocr, result, page=index if multi else 1, image=image,
        ))
    return "\n\n".join(parts)[:LOCAL_OCR_TEXT_LIMIT], notice, blocks


async def _recognize_job(user_id: int, data, provider, verifier, *, job_id=None):
    """Step 1 of photo pipeline: structured vision recognition (optionally dual-model diff).

    与 OCR-first 草稿分离：识别期间记录 intake_revision；回写前重新比较——
    若期间发生人工确认（或开始时该题已确认），模型结果只写入 pending_draft_json
    作为「模型草稿对照」，绝不覆盖 confirmed_json，需要用户再次确认才生效。
    """
    from . import _call_model as call_model
    from .. import practice

    _ensure_intake_schema()
    row = _owned("ai_uploads", data.upload_id, user_id)
    base_revision = _intake_revision(row)
    images = await asyncio.to_thread(_upload_images, row)

    if not images and not row["text_content"].strip():
        raise ValueError("上传内容为空，无法识别")

    # 视觉识别前先跑本地 OCR：作为提示词里的校对参考，并在结果中带回 ocr_text 供用户核对；
    # 同时拿本地 OCR 行结果构造内容块（原图定位/阅读顺序），随识别结果一起回写。
    ocr_text, ocr_error, ocr_blocks = await _local_ocr_question_text(images)
    ocr_hint = (
        "\n\n本地 OCR 初步识别（机器初稿，可能有错字、漏字或公式偏差；仅供校对参考，"
        "请以图片内容为准逐字核对后输出题干与选项，不要照抄错误）：\n" + ocr_text
        if ocr_text else ""
    )
    prompt = (
        "请准确识别这道题目（数学与科学公式使用标准 LaTeX $...$），严禁自行补造缺失文字或条件。"
        "严格返回 JSON 对象：\n"
        '{"subject":"数学|物理|化学|生物|语文|英语|历史|地理|政治|其他",'
        '"type":"单选题|多选题|填空题|计算题|简答题|材料题|其他",'
        '"stem":"完整题干文本",'
        '"options":[{"label":"A","text":"选项内容"}],'
        '"answers":["图中若可见参考答案/答案则给出；无法确定时返回空数组"],'
        '"conditions":["图中可见关键几何/物理条件"],'
        '"uncertain":["模糊或无法确认的部分"]}\n'
        + (f"\n附加文本：\n{row['text_content']}" if row["text_content"].strip() else "")
        + ocr_hint
    )

    calls_raw = []
    if verifier:
        res = await asyncio.gather(
            call_model(provider, data.model, prompt, images, True, token_limit=2500,
                       user_id=user_id, job_id=job_id),
            call_model(verifier, data.verify_model, prompt, images, True, token_limit=2500,
                       user_id=user_id, job_id=job_id),
            return_exceptions=True,
        )
        calls_raw = [r for r in res if isinstance(r, dict)]
        if not calls_raw:
            raise ValueError("两个视觉识别模型均请求失败，请检查模型是否支持视觉输入")
    else:
        calls_raw = [await call_model(
            provider, data.model, prompt, images, True, token_limit=2500,
            user_id=user_id, job_id=job_id,
        )]

    def parse_rec(txt):
        try:
            obj = json.loads(_strip_json_fences(txt))
            if isinstance(obj, dict) and obj.get("stem"):
                if not isinstance(obj.get("answers"), list):
                    obj["answers"] = []
                return obj
        except Exception:
            pass
        return {
            "subject": "", "type": "", "stem": txt.strip(), "options": [], "answers": [],
            "conditions": [], "uncertain": ["模型未按结构化格式返回，请人工核对"],
        }

    primary_rec = parse_rec(calls_raw[0]["content"])
    # 内容块随识别结果回写：recognized_json / draft_json / pending_draft_json 必须带 blocks，
    # 供前端做原图定位与公式校对（math 块 latex=null、needs_review=true，绝不伪造公式识别）。
    recognized_payload = {
        **primary_rec,
        "blocks": list(ocr_blocks),
        "schema_version": INTAKE_BLOCKS_SCHEMA_VERSION,
    }
    diffs = []
    secondary_rec = None
    if len(calls_raw) == 2:
        secondary_rec = parse_rec(calls_raw[1]["content"])
        if primary_rec.get("stem", "").strip() != secondary_rec.get("stem", "").strip():
            diffs.append("题干识别存在差异，请逐字核对")
        if len(primary_rec.get("options", [])) != len(secondary_rec.get("options", [])):
            diffs.append("选项数量识别不一致，请核对")
        if primary_rec.get("subject") != secondary_rec.get("subject"):
            diffs.append(f"学科判断不同：{primary_rec.get('subject')} vs {secondary_rec.get('subject')}")

    now = db.utcnow()
    model_meta = {
        "model": str(calls_raw[0].get("model") or data.model),
        "provider_name": str(calls_raw[0].get("provider_name") or ""),
    }
    vision_draft = {
        **recognized_payload,
        "source": "vision",
        "ocr_text": ocr_text,
        "ocr_notice": ocr_error or "",
        "created_at": now,
        **model_meta,
    }
    # 回写前重新比较版本：人工确认优先，模型结果只能作为待确认草稿。
    # 用带条件的原子 UPDATE 消除「读取版本→确认提交→回写」之间的竞态：
    # 只有未人工确认（confirmed_json 为空）且版本未变时才允许覆盖识别草稿。
    pending = {
        **vision_draft,
        "reason": "识别完成时该题已人工确认，模型草稿需再次确认才会采用",
    }
    with db.connection() as con:
        _ensure_intake_schema(con)
        changed = con.execute(
            "UPDATE ai_uploads SET recognized_json=?, draft_json=?, status='recognized' "
            "WHERE id=? AND user_id=? AND intake_revision=? "
            "AND (confirmed_json IS NULL OR confirmed_json='')",
            (_json(recognized_payload), _json(vision_draft), data.upload_id, user_id, base_revision),
        ).rowcount
        if changed:
            draft_applied = True
            current_revision = base_revision
        else:
            draft_applied = False
            row_after = con.execute(
                "SELECT intake_revision FROM ai_uploads WHERE id=? AND user_id=?",
                (data.upload_id, user_id),
            ).fetchone()
            if row_after is None:
                raise ValueError("上传题目已被删除，无法写回识别结果")
            current_revision = int(row_after["intake_revision"] or 1)
            # 已人工确认：保留 confirmed_json 原文，模型结果作为对照草稿等待再次确认。
            con.execute(
                "UPDATE ai_uploads SET pending_draft_json=? WHERE id=? AND user_id=?",
                (_json(pending), data.upload_id, user_id),
            )

    similar = await asyncio.to_thread(
        practice.search_similar_questions,
        primary_rec.get("stem", ""),
        primary_rec.get("subject") or None,
        limit=5,
    )
    notice = (
        "该题已人工确认，本次识别结果已保存为「模型草稿」，需你再次确认才会生效。"
        if not draft_applied else ""
    )
    return {
        "upload_id": data.upload_id,
        "recognized": recognized_payload,
        "secondary_recognized": secondary_rec,
        "differences": diffs,
        "similar_questions": similar,
        "has_exact_match": any(s.get("exact_match") for s in similar),
        "content": primary_rec.get("stem", ""),
        # 本地 OCR 初稿：供用户在确认题干前逐字核对（不可用时为空串，并给出提示）。
        "ocr_text": ocr_text,
        "ocr_notice": ocr_error or "",
        # 内容块（本地 OCR 行结果构造，带原图位置与阅读顺序；见 recognized.blocks）。
        "blocks": list(ocr_blocks),
        "calls": [{"model": c["model"], "provider_name": c["provider_name"], "usage": c["usage"]} for c in calls_raw],
        # 草稿回写结果：False 表示识别期间发生人工确认，结果进入待确认模型草稿。
        "draft_applied": draft_applied,
        "draft_conflict": not draft_applied,
        "revision": current_revision,
        "notice": notice,
    }


# ---------------------------------------------------------------------------
# Admin Batch KP + AI Difficulty Tagging (§2.3, §2.4)
# ---------------------------------------------------------------------------

class TaggingEstimateInput(BaseModel):
    subject: str
    limit: int = Field(default=50, ge=1, le=2000)
    only_untagged: bool = True


@router.post("/admin/tagging/estimate")
def estimate_tagging(data: TaggingEstimateInput, user=Depends(require_admin)):
    from .. import practice
    with practice._app_with_bank() as con:
        excl, params = practice._excluded_sql("q")
        sql = f"SELECT COUNT(*) FROM bank.questions q WHERE q.subject=? AND {excl}"
        all_params = [data.subject, *params]
        if data.only_untagged:
            sql += " AND NOT EXISTS (SELECT 1 FROM question_kp qk WHERE qk.qid=q.qid)"
        total_candidates = con.execute(sql, all_params).fetchone()[0]
        kp_count = con.execute("SELECT COUNT(*) FROM knowledge_points WHERE subject=?", (data.subject,)).fetchone()[0]
    target = min(total_candidates, data.limit)
    # 实际执行按每 5 题一次模型调用（micro-batch），预估必须与真实调用次数一致。
    calls = (target + 4) // 5 if target else 0
    batches_of_50 = (target + 49) // 50 if target else 0
    return {
        "subject": data.subject,
        "candidate_questions": total_candidates,
        "will_process": target,
        "batches_of_50": batches_of_50,
        "batches_of_5": calls,
        "questions_per_call": 5,
        "estimated_calls": calls,
        "kp_count": kp_count,
        "notice": (
            f"将按每 5 题一次模型调用处理最多 {target} 道{data.subject}题"
            f"（共约 {calls} 次调用，可分 {batches_of_50} 批执行，可随时取消或续跑）。"
        ),
    }


async def _tagging_job(job_id: int, user_id: int, data, provider):
    """Batch tag questions with KP IDs (restricted to official KP tree) + AI difficulty (1-5)."""
    from . import _call_model as call_model
    from .jobs import _job_update, _job_set_checkpoint, _job_get_checkpoint, is_job_cancelled
    from .. import practice

    subject = data.subject
    if not subject:
        raise ValueError("批量打标必须指定学科")
    limit = min(max(1, int(getattr(data, "tagging_limit", 50) or 50)), 1000)

    with practice._app_with_bank() as con:
        kp_rows = con.execute(
            "SELECT id, parent_id, name, level FROM knowledge_points WHERE subject=? ORDER BY level, sort, id",
            (subject,),
        ).fetchall()
        if not kp_rows:
            raise ValueError(f"学科「{subject}」暂无知识点树，请先导入或创建知识点")
        kp_catalog = [{"id": r["id"], "name": r["name"], "level": r["level"]} for r in kp_rows]
        valid_kp_ids = {r["id"] for r in kp_rows}

        excl, params = practice._excluded_sql("q")
        q_rows = con.execute(
            f"SELECT q.qid, q.type, q.stem_plain FROM bank.questions q "
            f"WHERE q.subject=? AND {excl} AND NOT EXISTS (SELECT 1 FROM question_kp qk WHERE qk.qid=q.qid) "
            f"ORDER BY q.year DESC, q.qid LIMIT ?",
            [subject, *params, limit],
        ).fetchall()

    checkpoint = _job_get_checkpoint(job_id) or {}
    done_qids = set(checkpoint.get("done_qids") or [])
    auto_count = int(checkpoint.get("auto_count") or 0)
    review_count = int(checkpoint.get("review_count") or 0)
    errors = list(checkpoint.get("errors") or [])

    # Process in micro-batches of 5 questions per API call (10 calls per 50-question batch)
    pending_rows = [dict(r) for r in q_rows if r["qid"] not in done_qids]
    total = len(q_rows)
    calls_meta = []
    kp_json_str = _json(kp_catalog)

    for offset in range(0, len(pending_rows), 5):
        if is_job_cancelled(job_id):
            break
        group = pending_rows[offset:offset + 5]
        _job_update(job_id, "running", f"tagging-{len(done_qids)}/{total}")
        batch_input = [{"qid": r["qid"], "type": r["type"], "stem": (r["stem_plain"] or "")[:500]} for r in group]
        prompt = (
            f"你是高考{subject}教研员。请为下面每道题从给定的知识点列表中选择 1–2 个最准确的 kp_id，"
            "绝不能编造列表之外的 kp_id；提炼 1 个简短的题型/解法标签 method（如「导数-含参讨论」「完形-逻辑复现」）；"
            "并预估题目难度 1–5 分（1-2简单，3中等，4-5困难）。\n"
            "严格返回 JSON 对象：{\"items\":[{\"qid\":\"...\",\"kps\":[{\"kp_id\":123,\"confidence\":0.92}],\"method\":\"...\",\"difficulty_score\":3}]}\n"
            f"可选知识点列表：\n{kp_json_str}\n\n待标注题目：\n{_json(batch_input)}"
        )
        try:
            call = await call_model(
                provider, data.model, prompt, json_output=True, token_limit=2400,
                user_id=user_id, job_id=job_id,
            )
            calls_meta.append({"model": call["model"], "provider_name": call["provider_name"], "usage": call["usage"]})
            parsed = json.loads(_strip_json_fences(call["content"]))
            items = parsed.get("items") if isinstance(parsed, dict) else []
            now = db.utcnow()
            with db.connection() as con:
                for it in items or []:
                    qid = it.get("qid")
                    if not qid or qid not in {r["qid"] for r in group}:
                        continue
                    for kp_item in (it.get("kps") or [])[:3]:
                        kp_id = kp_item.get("kp_id")
                        conf = float(kp_item.get("confidence") or 0.0)
                        if kp_id not in valid_kp_ids:
                            continue
                        src = "ai" if conf >= 0.8 else "ai_pending_review"
                        if src == "ai":
                            auto_count += 1
                        else:
                            review_count += 1
                        con.execute(
                            "INSERT INTO question_kp(qid,kp_id,weight,source,model,confidence,created_at) VALUES (?,?,1.0,?,?,?,?) "
                            "ON CONFLICT(qid,kp_id) DO UPDATE SET source=excluded.source,model=excluded.model,confidence=excluded.confidence",
                            (qid, kp_id, src, data.model, round(conf, 3), now),
                        )
                    method_name = str(it.get("method") or "").strip()[:80]
                    if method_name:
                        con.execute(
                            "INSERT INTO methods(subject,name) VALUES (?,?) ON CONFLICT(subject,name) DO NOTHING",
                            (subject, method_name),
                        )
                        mid = con.execute("SELECT id FROM methods WHERE subject=? AND name=?", (subject, method_name)).fetchone()[0]
                        con.execute(
                            "INSERT INTO question_method(qid,method_id,source) VALUES (?,?,'ai') "
                            "ON CONFLICT(qid,method_id) DO NOTHING",
                            (qid, mid),
                        )
                    score = it.get("difficulty_score")
                    if isinstance(score, (int, float)) and 1 <= score <= 5:
                        ai_diff = "简单" if score <= 2 else ("中等" if score <= 3.5 else "困难")
                        con.execute(
                            "INSERT INTO question_metadata(qid,difficulty,ai_difficulty,ai_difficulty_score,ai_model,ai_updated_at,updated_at) "
                            "VALUES (?,'待标注',?,?,?,?,?) "
                            "ON CONFLICT(qid) DO UPDATE SET ai_difficulty=excluded.ai_difficulty,"
                            "ai_difficulty_score=excluded.ai_difficulty_score,ai_model=excluded.ai_model,ai_updated_at=excluded.ai_updated_at",
                            (qid, ai_diff, float(score), data.model, now, now),
                        )
                    done_qids.add(qid)
        except Exception as exc:
            errors.append(f"批次 {offset // 5 + 1} 失败：{exc}")
        checkpoint.update({"done_qids": sorted(done_qids), "auto_count": auto_count, "review_count": review_count, "errors": errors[:20]})
        await run_in_threadpool(_job_set_checkpoint, job_id, checkpoint)

    return {
        "subject": subject,
        "processed": len(done_qids),
        "total": total,
        "auto_tagged": auto_count,
        "needs_review": review_count,
        "errors": errors,
        "content": f"已完成 {subject} 批量打标：处理 {len(done_qids)}/{total} 题，高置信度自动入库 {auto_count} 条，进入复核队列 {review_count} 条。",
        "calls": calls_meta,
    }
