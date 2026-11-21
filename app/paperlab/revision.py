"""试卷工坊 P1：错题复现卷（replay）与 AI 巩固卷（consolidate / ``paper-revision`` 任务）。

照 recon `oh-my-paper.md` §2.6：
- **REPLAY**：直接按错题本里的题生成试卷（按 qid 去重、冻结快照、保留题目分值）。
  不调用模型；错题数量不足时用现有题数（不编造题目）。
- **CONSOLIDATE**：候选 = 同科、命中相同薄弱知识点、且该用户**从未作答过**的只读题库真题；
  模型只许从候选中选题并给每题理由；题数精确，候选不足时明确 422 拒绝，绝不缩水。
"""
from __future__ import annotations

import asyncio
import hashlib
import json

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ..auth import current_user
from ..db import connection, utcnow
from . import db, papers, schema

router = APIRouter()

MAX_REVISION_COUNT = 20
MAX_REVISION_CANDIDATES = 120
MIN_REVISION_COUNT = 1
REVISION_MODES = {
    "replay": {"label": "错题复现卷", "ai_required": False},
    "consolidate": {"label": "AI 巩固卷", "ai_required": True},
}


# ---------------------------------------------------------------------------
# 错题与知识点（读取 practice 的错题本/知识点关联表）
# ---------------------------------------------------------------------------

def _active_wrong_qids(user_id: int) -> list[str]:
    with connection() as con:
        rows = con.execute(
            "SELECT qid FROM wrong_questions WHERE user_id=? AND archived=0 "
            "ORDER BY next_review_at ASC, qid ASC",
            (user_id,),
        ).fetchall()
    return [str(row[0]) for row in rows]


def _qid_kp_links(qids: list[str]) -> dict[str, list[dict]]:
    """qid → [{kp_id, name}]（应用库 question_kp 关联）。"""
    result: dict[str, list[dict]] = {qid: [] for qid in qids}
    if not qids:
        return result
    with connection() as con:
        for chunk in papers._chunks(qids):
            marks = ",".join("?" for _ in chunk)
            rows = con.execute(
                "SELECT qk.qid, kp.id AS kp_id, kp.name FROM question_kp qk "
                "JOIN knowledge_points kp ON kp.id=qk.kp_id "
                f"WHERE qk.qid IN ({marks}) ORDER BY qk.weight DESC, kp.id",
                chunk,
            ).fetchall()
            for row in rows:
                result.setdefault(str(row[0]), []).append(
                    {"kp_id": int(row[1]), "name": str(row[2])}
                )
    return result


def _wrong_rows(user_id: int) -> list[dict]:
    """错题本当前有效记录 + 题库元信息（学科/题型/知识点）。"""
    qids = _active_wrong_qids(user_id)
    if not qids:
        return []
    cards = papers.bank_question_cards(qids)
    links = _qid_kp_links(qids)
    with connection() as con:
        rows = con.execute(
            "SELECT qid, reason, error_tags_json, failures, next_review_at, added_at, "
            "last_wrong_at FROM wrong_questions WHERE user_id=? AND archived=0",
            (user_id,),
        ).fetchall()
    by_qid = {str(row["qid"]): dict(row) for row in rows}
    result = []
    for qid in qids:
        card = cards.get(qid)
        row = by_qid.get(qid, {})
        if not card:
            continue
        try:
            tags = json.loads(row.get("error_tags_json") or "[]")
        except (TypeError, ValueError):
            tags = []
        result.append({
            "qid": qid,
            "subject": card.get("subject") or "",
            "type": card.get("type") or "SUBJECTIVE",
            "stem_preview": card.get("stem_preview") or "",
            "difficulty": card.get("difficulty"),
            "knowledge_tags": list(card.get("knowledge_tags") or []),
            "knowledge_points": links.get(qid, []),
            "reason": str(row.get("reason") or ""),
            "error_tags": [str(tag) for tag in tags if str(tag).strip()],
            "failures": int(row.get("failures") or 0),
            "next_review_at": row.get("next_review_at"),
            "added_at": row.get("added_at"),
            "last_wrong_at": row.get("last_wrong_at"),
            "score": card.get("score") or papers.DEFAULT_SCORE,
        })
    return result


def _filter_wrongs(wrongs: list[dict], subject: str | None, kp_ids: list[int] | None) -> list[dict]:
    selected = wrongs
    if subject:
        selected = [item for item in selected if item["subject"] == subject]
    if kp_ids:
        wanted = set(kp_ids)
        selected = [
            item for item in selected
            if wanted & {point["kp_id"] for point in item.get("knowledge_points") or []}
        ]
    return selected


def _normalize_kp_ids(raw) -> list[int] | None:
    if raw is None:
        return None
    if not isinstance(raw, list) or len(raw) > 20:
        raise HTTPException(422, "kp_ids 需为知识点编号数组（最多 20 个）")
    result: list[int] = []
    for value in raw:
        kp_id = schema.safe_int(value)
        if kp_id is None or kp_id < 1:
            raise HTTPException(422, "知识点编号不正确")
        if kp_id not in result:
            result.append(kp_id)
    return result


def _invalid_kp_ids(user_id: int, kp_ids: list[int] | None) -> list[int]:
    if not kp_ids:
        return []
    with connection() as con:
        marks = ",".join("?" for _ in kp_ids)
        rows = con.execute(f"SELECT id FROM knowledge_points WHERE id IN ({marks})", kp_ids).fetchall()
    valid = {int(row[0]) for row in rows}
    return [kp_id for kp_id in kp_ids if kp_id not in valid]


# ---------------------------------------------------------------------------
# HTTP：选项
# ---------------------------------------------------------------------------

@router.get("/revision/options")
def revision_options(
    subject: str | None = Query(default=None, max_length=40),
    user=Depends(current_user),
):
    """错题本可生成的题本选项：错题数、知识点选项、两种模式与上限。"""
    all_wrongs = _wrong_rows(user["id"])
    wrongs = _filter_wrongs(all_wrongs, subject, None)
    kp_counts: dict[int, dict] = {}
    for item in wrongs:
        for point in item.get("knowledge_points") or []:
            bucket = kp_counts.setdefault(point["kp_id"], {"kp_id": point["kp_id"], "name": point["name"], "wrong_count": 0})
            bucket["wrong_count"] += 1
    kp_options = sorted(kp_counts.values(), key=lambda item: (-item["wrong_count"], item["name"]))
    subjects = sorted({item["subject"] for item in all_wrongs if item["subject"]})
    return {
        "wrong_count": len(wrongs),
        "subjects": subjects,
        "kp_options": kp_options,
        "modes": [
            {
                "value": "replay",
                "label": REVISION_MODES["replay"]["label"],
                "description": "直接按错题本题目生成复现卷（原题 + 快照，不调用模型）",
                "ai_required": False,
                "count_min": MIN_REVISION_COUNT,
                "count_max": MAX_REVISION_COUNT,
            },
            {
                "value": "consolidate",
                "label": REVISION_MODES["consolidate"]["label"],
                "description": "AI 从同知识点、未作答过的真题中选题生成巩固卷（题数不足直接拒绝）",
                "ai_required": True,
                "count_min": MIN_REVISION_COUNT,
                "count_max": MAX_REVISION_COUNT,
            },
        ],
        "limits": {
            "count_min": MIN_REVISION_COUNT,
            "count_max": MAX_REVISION_COUNT,
            "replay_max": MAX_REVISION_COUNT,
        },
    }


# ---------------------------------------------------------------------------
# REPLAY：直接建卷
# ---------------------------------------------------------------------------

def _build_replay(user_id: int, wrongs: list[dict], count: int) -> tuple[dict, list[dict]]:
    if not wrongs:
        raise HTTPException(422, "错题本没有符合条件的错题，无法生成错题复现卷")
    selected = wrongs[:count]
    items: list[dict] = []
    skipped: list[dict] = []
    for item in selected:
        try:
            snapshot = papers.snapshot_bank(item["qid"])
        except HTTPException as exc:
            skipped.append({"qid": item["qid"], "reason": str(exc.detail)})
            continue
        snapshot = papers.apply_score(snapshot, None)
        reason_parts = [value for value in (item.get("reason"), "/".join(item.get("error_tags") or [])) if value]
        snapshot["selection_reason"] = "错题复现" + (f"（错因：{reason_parts[0]}）" if reason_parts else "")
        items.append({
            "ref": f"bank:{item['qid']}", "snapshot": snapshot,
            "score": float(snapshot["score"]), "section": "",
        })
    if not items:
        raise HTTPException(422, "错题本里的题目暂时不可用（已隔离或已移除），无法生成错题复现卷")
    subjects = sorted({item["subject"] for item in wrongs if item["subject"]})
    paper = papers.create_paper(
        user_id,
        title="错题复现卷" + (f" · {subjects[0]}" if len(subjects) == 1 else ""),
        kind="replay",
        subject=subjects[0] if len(subjects) == 1 else "综合",
        grade="",
        instructions="按错题本原题生成的复现卷；题目内容为建卷时快照，可用于重做与打印。",
        layout=None,
        status="ready",
        source=f"replay:{user_id}:{db.utcnow()}",
        items=items,
    )
    return paper, skipped


# ---------------------------------------------------------------------------
# CONSOLIDATE：候选与任务
# ---------------------------------------------------------------------------

def _attempted_qids(user_id: int) -> set[str]:
    with connection() as con:
        rows = con.execute("SELECT DISTINCT qid FROM attempts WHERE user_id=?", (user_id,)).fetchall()
    return {str(row[0]) for row in rows}


def _consolidate_candidates(
    user_id: int, *, subject: str | None, kp_ids: list[int] | None, wrong_qids: set[str],
) -> tuple[list[dict], set[int]]:
    """候选 = 命中薄弱知识点的只读题库题，排除用户已作答/已在错题本/隔离题。"""
    wrongs = _wrong_rows(user_id)
    relevant = _filter_wrongs(wrongs, subject, kp_ids)
    if not relevant:
        raise HTTPException(422, "没有可用于巩固卷的错题（可先选择学科或知识点）")
    target_kp_ids: set[int] = set(kp_ids or [])
    if not target_kp_ids:
        for item in relevant:
            target_kp_ids.update(point["kp_id"] for point in item.get("knowledge_points") or [])
    if not target_kp_ids:
        raise HTTPException(
            422,
            "错题尚未关联知识点，无法生成知识巩固卷；可先使用「知识点批量打标」建立关联，"
            "或改用错题复现卷",
        )
    kp_names: dict[int, str] = {}
    for item in relevant:
        for point in item.get("knowledge_points") or []:
            kp_names[point["kp_id"]] = point["name"]
    with connection() as con:
        marks = ",".join("?" for _ in target_kp_ids)
        rows = con.execute(
            f"SELECT DISTINCT qid FROM question_kp WHERE kp_id IN ({marks}) LIMIT 5000",
            tuple(target_kp_ids),
        ).fetchall()
    qids = [str(row[0]) for row in rows]
    cards = papers.bank_question_cards(qids)
    attempted = _attempted_qids(user_id)
    candidates: list[dict] = []
    for qid, card in cards.items():
        if qid in wrong_qids or qid in attempted:
            continue
        if subject and card.get("subject") != subject:
            continue
        if card.get("practice_status") not in ("practice", "review"):
            continue
        candidates.append({
            "ref": f"bank:{qid}",
            "source": "bank",
            "type": card.get("type") or "SUBJECTIVE",
            "subject": card.get("subject") or "",
            "grade": "",
            "difficulty": card.get("difficulty"),
            "knowledge_tags": list(card.get("knowledge_tags") or []),
            "stem_preview": card.get("stem_preview") or "",
            "score": card.get("score") or papers.DEFAULT_SCORE,
            "_kp_overlap": len(set(card.get("knowledge_tags") or []) & set(kp_names.values())),
        })
    candidates.sort(key=lambda item: (-item.get("_kp_overlap", 0), item["ref"]))
    for candidate in candidates:
        candidate.pop("_kp_overlap", None)
    return candidates[:MAX_REVISION_CANDIDATES], target_kp_ids


def _infer_subject(wrongs: list[dict], subject: str | None) -> str | None:
    if subject:
        return subject
    subjects = sorted({item["subject"] for item in wrongs if item["subject"]})
    if len(subjects) > 1:
        raise HTTPException(422, "错题跨多个学科，请选择要生成巩固卷的学科")
    return subjects[0] if subjects else None


def _prepare_consolidate(user_id: int, spec: dict) -> tuple[dict, list[dict], dict]:
    subject = spec.get("subject") or None
    kp_ids = spec.get("kp_ids")
    if isinstance(kp_ids, str):
        kp_ids = None
    count = int(spec.get("count") or 5)
    if count < MIN_REVISION_COUNT or count > MAX_REVISION_COUNT:
        raise HTTPException(422, f"题本题数应在 {MIN_REVISION_COUNT}–{MAX_REVISION_COUNT} 之间")
    wrongs = _wrong_rows(user_id)
    relevant = _filter_wrongs(wrongs, subject, kp_ids)
    subject = _infer_subject(relevant, subject)
    wrong_qids = {item["qid"] for item in _filter_wrongs(wrongs, subject, kp_ids)}
    candidates, target_kp_ids = _consolidate_candidates(
        user_id, subject=subject, kp_ids=kp_ids, wrong_qids=wrong_qids,
    )
    if len(candidates) < count:
        raise HTTPException(
            422,
            f"符合条件的未作答题只有 {len(candidates)} 道，少于需要的 {count} 道；"
            "为保证巩固卷质量，不会缩水或编造题目，请减少题数或更换学科/知识点",
        )
    plan = {
        "subject": subject or "",
        "kp_ids": sorted(target_kp_ids),
        "count": count,
        "wrong_qids": sorted(wrong_qids),
        "candidate_map": {candidate["ref"]: candidate for candidate in candidates},
    }
    stats = {
        "candidate_count": len(candidates),
        "count": count,
        "subject": subject or "",
        "wrong_count": len(wrong_qids),
        "kp_ids": sorted(target_kp_ids),
    }
    return plan, candidates, stats


def _consolidate_prompt(plan: dict, candidates: list[dict], wrongs: list[dict]) -> str:
    compact = [
        {
            "ref": candidate["ref"],
            "type": candidate["type"],
            "difficulty": candidate["difficulty"],
            "knowledge_tags": candidate["knowledge_tags"][:4],
            "stem": candidate["stem_preview"][:120],
        }
        for candidate in candidates
    ]
    wrong_summary = [
        {
            "知识点": [point["name"] for point in item.get("knowledge_points") or []][:4],
            "错因": "/".join(item.get("error_tags") or []) or item.get("reason") or "",
            "题干": item["stem_preview"][:80],
        }
        for item in wrongs[:15]
    ]
    return (
        "你是严谨的中学复习助手，要根据学生的错题生成一份「错题巩固卷」。\n\n"
        f"【硬性要求】\n1. 题数必须正好 {plan['count']} 道。\n"
        "2. 只能从「候选题」中选择 ref，禁止编造 ID；同一道题不能重复。\n"
        "3. 每题必须给出中文 reason，说明它针对哪个薄弱知识点/错因（结合学生错题）。\n"
        "4. 候选人都是该学生从未作答过的题，请覆盖错题暴露出的不同知识点。\n\n"
        "【输出格式】只返回 JSON：\n"
        '{"selections":[{"ref":"bank:xxx","score":5,"reason":"针对二次函数单调性易错点"}],'
        '"summary":"一句话说明巩固卷设计思路"}\n\n'
        "【学生错题摘要】\n" + json.dumps(wrong_summary, ensure_ascii=False, separators=(",", ":")) + "\n\n"
        "【候选题（共 %d 道）】\n" % len(candidates)
        + json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
    )


def validate_revision_job(data, user) -> None:
    params = data.params if isinstance(getattr(data, "params", None), dict) else {}
    spec = params.get("spec") if isinstance(params.get("spec"), dict) else params
    mode = str(spec.get("mode") or "consolidate").strip().lower()
    if mode != "consolidate":
        raise HTTPException(422, "paper-revision 任务只支持 consolidate 模式；错题复现卷请直接调用 POST /revision")
    _prepare_consolidate(user["id"], spec)


def validate_consolidate_proposal(parsed, plan: dict) -> dict:
    if not isinstance(parsed, dict):
        raise ValueError("模型返回的不是 JSON 对象")
    entries = None
    for key in ("selections", "items", "questions"):
        if isinstance(parsed.get(key), list):
            entries = parsed[key]
            break
    if entries is None:
        raise ValueError("模型未返回 selections 列表（只能从候选题中选择 ref）")
    count = plan["count"]
    if len(entries) != count:
        raise ValueError(f"题目数量不匹配：要求正好 {count} 道，实际 {len(entries)} 道")
    candidate_map = plan["candidate_map"]
    selection: list[dict] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        ref = entry.strip() if isinstance(entry, str) else str((entry or {}).get("ref") or "").strip()
        reason = "" if isinstance(entry, str) else str(entry.get("reason") or "").strip()
        if not ref:
            raise ValueError(f"第 {index + 1} 题缺少 ref")
        if ref not in candidate_map:
            raise ValueError(f"第 {index + 1} 题引用了候选之外的题 {ref}（禁止编造 ID）")
        if ref in seen:
            raise ValueError(f"题目重复：{ref}")
        seen.add(ref)
        if not reason:
            raise ValueError(f"第 {index + 1} 题缺少 reason（需要说明针对的薄弱知识点）")
        selection.append({"ref": ref, "reason": reason[:200]})
    summary = str(parsed.get("summary") or parsed.get("reason") or "").strip()[:500]
    return {"selections": selection, "summary": summary,
            "statistics": {"count": len(selection), "kp_ids": plan["kp_ids"]}}


def _revision_fingerprint(plan: dict, candidates: list[dict]) -> str:
    payload = json.dumps({
        "plan": {k: plan[k] for k in ("subject", "kp_ids", "count", "wrong_qids")},
        "candidates": [candidate["ref"] for candidate in candidates],
    }, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _job_helpers():
    from .digitize import _get_checkpoint, _is_cancelled, _publish_event, _set_checkpoint, _update_job
    return {
        "get_checkpoint": _get_checkpoint, "is_cancelled": _is_cancelled,
        "publish_event": _publish_event, "set_checkpoint": _set_checkpoint,
        "update_job": _update_job,
    }


async def run_revision_job(job_id: int, user_id: int, data, provider, verifier=None):
    """``paper-revision``（consolidate）执行器：候选准备 → 模型选题+理由 → 硬校验 → 建卷。"""
    from ..ai.adapters import call_structured
    from . import prompts

    helpers = _job_helpers()
    params = dict(getattr(data, "params", None) or {})
    spec_raw = params.get("spec") if isinstance(params.get("spec"), dict) else params
    plan, candidates, stats = _prepare_consolidate(user_id, spec_raw)
    spec = {key: plan[key] for key in ("subject", "kp_ids", "count", "wrong_qids")}
    fingerprint = _revision_fingerprint(plan, candidates)
    source = f"ai-revision:consolidate:job:{job_id}"
    existing = papers.find_paper_by_source(user_id, source)
    if existing is not None:
        detail = papers.paper_detail(int(existing["id"]), user_id, include_snapshot=False)
        return {
            "paper_id": int(existing["id"]), "paper": detail["paper"],
            "item_count": len(detail["items"]), "total_score": detail["paper"]["total_score"],
            "summary": "巩固卷已完成（复用同任务的试卷）", "statistics": stats,
        }
    checkpoint = helpers["get_checkpoint"](job_id) or {}
    proposal = None
    if checkpoint.get("fingerprint") == fingerprint and isinstance(checkpoint.get("proposal"), dict):
        try:
            proposal = validate_consolidate_proposal(checkpoint["proposal"], plan)
        except ValueError:
            proposal = None
    model = str(getattr(data, "model", "") or "")
    provider_id = provider.get("id") if isinstance(provider, dict) else getattr(provider, "id", None)
    if proposal is None:
        helpers["update_job"](job_id, "running", f"巩固卷候选准备：{len(candidates)} 道候选")
        helpers["publish_event"](job_id, "stage", {
            "status": "running", "stage": f"巩固卷候选准备：{len(candidates)} 道候选",
            "candidate_count": len(candidates),
        })
        if helpers["is_cancelled"](job_id):
            raise asyncio.CancelledError()
        wrongs = _filter_wrongs(_wrong_rows(user_id), plan["subject"] or None, plan["kp_ids"] or None)
        prompt = _consolidate_prompt(plan, candidates, wrongs)
        helpers["update_job"](job_id, "running", "AI 选题与理由")
        helpers["publish_event"](job_id, "stage", {"status": "running", "stage": "AI 选题与理由"})
        try:
            parsed, _call = await call_structured(
                provider, model, prompt,
                lambda value: validate_consolidate_proposal(value, plan),
                token_limit=4000, user_id=user_id, job_id=job_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            from .digitize import _error_text
            raise ValueError(f"AI 巩固卷未通过硬校验：{_error_text(exc)}") from None
        proposal = parsed
        helpers["set_checkpoint"](job_id, {
            "fingerprint": fingerprint,
            "proposal": {"selections": proposal["selections"], "summary": proposal["summary"]},
            "updated_at": utcnow(),
        })
    if helpers["is_cancelled"](job_id):
        raise asyncio.CancelledError()
    helpers["update_job"](job_id, "running", "生成巩固卷快照")
    helpers["publish_event"](job_id, "stage", {"status": "running", "stage": "生成巩固卷快照"})
    items: list[dict] = []
    for entry in proposal["selections"]:
        try:
            snapshot = papers.snapshot_bank(entry["ref"].split(":", 1)[1])
        except HTTPException as exc:
            raise ValueError(f"题目 {entry['ref']} 在生成期间已不可用：{exc.detail}") from None
        snapshot = papers.apply_score(snapshot, None)
        snapshot["selection_reason"] = entry["reason"]
        items.append({"ref": entry["ref"], "snapshot": snapshot,
                      "score": float(snapshot["score"]), "section": ""})
    if helpers["is_cancelled"](job_id):
        raise asyncio.CancelledError()
    subject = plan["subject"] or "综合"
    paper = papers.create_paper(
        user_id,
        title=f"错题巩固卷 · {subject}（{plan['count']} 题）",
        kind="consolidate",
        subject=subject,
        grade="",
        instructions="AI 根据错题暴露的薄弱知识点，从未作答过的真题中选题生成的巩固卷；题目为建卷时快照。",
        layout=None,
        status="ready",
        source=source,
        items=items,
    )
    paper_id = int(paper["id"])
    helpers["set_checkpoint"](job_id, {
        "fingerprint": fingerprint,
        "proposal": {"selections": proposal["selections"], "summary": proposal["summary"]},
        "paper_id": paper_id, "updated_at": utcnow(),
    })
    helpers["publish_event"](job_id, "stage", {"status": "running", "stage": "巩固卷完成", "paper_id": paper_id})
    detail = papers.paper_detail(paper_id, user_id, include_snapshot=False)
    return {
        "paper_id": paper_id,
        "paper": detail["paper"],
        "item_count": len(detail["items"]),
        "total_score": detail["paper"]["total_score"],
        "summary": proposal["summary"] or "错题巩固卷已生成",
        "reasons": [
            {"ref": entry["ref"], "reason": entry["reason"]} for entry in proposal["selections"]
        ],
        "statistics": {**stats, "ai": {
            "model": model, "provider_id": provider_id, "prompt_version": prompts.PROMPT_VERSION,
        }},
    }


# ---------------------------------------------------------------------------
# HTTP：生成题本
# ---------------------------------------------------------------------------

class RevisionInput(BaseModel):
    mode: str = Field(default="replay", max_length=20)
    subject: str | None = Field(default=None, max_length=40)
    kp_ids: list[int] | None = None
    count: int = Field(default=5, ge=MIN_REVISION_COUNT, le=MAX_REVISION_COUNT)
    provider_id: int | None = Field(default=None, ge=1)
    model: str | None = Field(default=None, max_length=160)


@router.post("/revision")
async def create_revision(data: RevisionInput, user=Depends(current_user)):
    """生成错题题本：replay 直接建卷；consolidate 创建 ``paper-revision`` AI 任务。"""
    mode = str(data.mode or "").strip().lower()
    if mode not in REVISION_MODES:
        raise HTTPException(422, "题本模式只支持 replay（错题复现卷）或 consolidate（AI 巩固卷）")
    kp_ids = _normalize_kp_ids(data.kp_ids)
    invalid = _invalid_kp_ids(user["id"], kp_ids)
    if invalid:
        raise HTTPException(404, f"知识点不存在：{', '.join(str(kp_id) for kp_id in invalid)}")
    subject = (data.subject or "").strip() or None
    if subject and subject not in db.SUBJECTS:
        raise HTTPException(422, "未知的学科")
    if mode == "replay":
        wrongs = _filter_wrongs(_wrong_rows(user["id"]), subject, kp_ids)
        paper, skipped = _build_replay(user["id"], wrongs, data.count)
        detail = papers.paper_detail(paper["id"], user["id"], include_snapshot=False)
        return {
            "mode": "replay",
            "paper": detail["paper"],
            "items": detail["items"],
            "item_count": len(detail["items"]),
            "skipped": skipped,
            "message": f"已生成错题复现卷（{len(detail['items'])} 题）"
                       + (f"，{len(skipped)} 道题暂时不可用已跳过" if skipped else ""),
        }
    if not data.provider_id or not data.model:
        raise HTTPException(422, "AI 巩固卷需要选择模型连接（provider_id 与 model）")
    spec = {"mode": "consolidate", "subject": subject, "kp_ids": kp_ids, "count": data.count}
    plan, candidates, stats = _prepare_consolidate(user["id"], spec)
    normalized_spec = {
        "mode": "consolidate", "subject": plan["subject"], "kp_ids": plan["kp_ids"],
        "count": plan["count"], "wrong_qids": plan["wrong_qids"],
    }
    from ..ai.jobs import JobInput, submit_job
    job = await submit_job(user, JobInput(
        task="paper-revision",
        provider_id=data.provider_id,
        model=data.model,
        subject=plan["subject"] or None,
        params={"spec": normalized_spec},
    ))
    return {
        "mode": "consolidate",
        "job": job,
        "id": job.get("id"),
        "task": "paper-revision",
        "status": job.get("status"),
        "count": plan["count"],
        "candidates": stats,
    }


__all__ = [
    "router", "run_revision_job", "validate_revision_job", "RevisionInput",
    "MAX_REVISION_COUNT", "REVISION_MODES",
]
