"""Photo question 2-step pipeline (multi-image recognize -> confirm -> similar bank match) & Admin batch KP tagging."""
import asyncio
from io import BytesIO
import json
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from .. import db, storage
from ..auth import current_user, require_admin
from .adapters import _strip_json_fences
from .notes_agent import _read_upload, _save_file
from .providers import _json, _owned
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


class ConfirmUploadInput(BaseModel):
    subject: str = Field(default="", max_length=40)
    type: str = Field(default="", max_length=40)
    stem: str = Field(min_length=1, max_length=20000)
    options: list[dict] = Field(default_factory=list, max_length=12)
    conditions: list[str] = Field(default_factory=list, max_length=20)


@router.patch("/uploads/{upload_id}/confirm")
def confirm_upload(upload_id: int, data: ConfirmUploadInput, user=Depends(current_user)):
    from .. import practice
    _owned("ai_uploads", upload_id, user["id"])
    payload = data.model_dump()
    with db.connection() as con:
        con.execute(
            "UPDATE ai_uploads SET confirmed_json=?, text_content=?, status='confirmed' WHERE id=? AND user_id=?",
            (_json(payload), data.stem.strip(), upload_id, user["id"]),
        )
    similar = practice.search_similar_questions(data.stem, subject=data.subject or None, limit=5)
    return {"id": upload_id, "status": "confirmed", "confirmed": payload, "similar_questions": similar}


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


async def _recognize_job(user_id: int, data, provider, verifier, *, job_id=None):
    """Step 1 of photo pipeline: structured OCR recognition (optionally dual-model diff)."""
    from . import _call_model as call_model
    from .. import practice

    row = _owned("ai_uploads", data.upload_id, user_id)
    images = await asyncio.to_thread(_upload_images, row)

    if not images and not row["text_content"].strip():
        raise ValueError("上传内容为空，无法识别")

    prompt = (
        "请准确识别这道题目（数学与科学公式使用标准 LaTeX $...$），严禁自行补造缺失文字或条件。"
        "严格返回 JSON 对象：\n"
        '{"subject":"数学|物理|化学|生物|语文|英语|历史|地理|政治|其他",'
        '"type":"单选题|多选题|填空题|计算题|简答题|材料题|其他",'
        '"stem":"完整题干文本",'
        '"options":[{"label":"A","text":"选项内容"}],'
        '"conditions":["图中可见关键几何/物理条件"],'
        '"uncertain":["模糊或无法确认的部分"]}\n'
        + (f"\n附加文本：\n{row['text_content']}" if row["text_content"].strip() else "")
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
                return obj
        except Exception:
            pass
        return {"subject": "", "type": "", "stem": txt.strip(), "options": [], "conditions": [], "uncertain": ["模型未按结构化格式返回，请人工核对"]}

    primary_rec = parse_rec(calls_raw[0]["content"])
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

    with db.connection() as con:
        con.execute(
            "UPDATE ai_uploads SET recognized_json=?, status='recognized' WHERE id=? AND user_id=?",
            (_json(primary_rec), data.upload_id, user_id),
        )

    similar = practice.search_similar_questions(primary_rec.get("stem", ""), subject=primary_rec.get("subject") or None, limit=5)
    return {
        "upload_id": data.upload_id,
        "recognized": primary_rec,
        "secondary_recognized": secondary_rec,
        "differences": diffs,
        "similar_questions": similar,
        "has_exact_match": any(s.get("exact_match") for s in similar),
        "content": primary_rec.get("stem", ""),
        "calls": [{"model": c["model"], "provider_name": c["provider_name"], "usage": c["usage"]} for c in calls_raw],
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
