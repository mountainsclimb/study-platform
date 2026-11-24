"""知识点本地向量打标：题干与知识点描述向量化后写入 ``question_kp``。

边界：

* 只在已配置 Embedding（本地或远程）时运行；向量不可用直接返回 ``available:false``，
  不写任何占位关联。
* 只处理没有人工确认（``source='admin'`` 或 ``reviewed_by`` 非空）的题目；人工关联
  绝不被覆盖。自动关联的 source 为 ``local_ai``（高置信）或
  ``local_ai_pending_review``（待复核），复核沿用既有 ``/admin/kp-review-queue``。
* 每次运行先删除该题旧的 ``local_ai*`` 关联再写入，保证可重复执行；已有其它模型的
  ``ai*`` 关联保留，不降级。
* 默认只取三级知识点（最细粒度）参与匹配；没有三级节点时退回全部节点。
"""
from __future__ import annotations

import math

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from . import db
from .ai import embeddings
from .auth import require_admin

router = APIRouter()

__all__ = ["router", "assign_tags", "run_local_tagging"]

LOCAL_SOURCES = ("local_ai", "local_ai_pending_review")
DEFAULT_THRESHOLD = 0.45
DEFAULT_HIGH_CONFIDENCE = 0.62
MAX_KPS_PER_QUESTION = 2
MAX_QUESTION_TEXT = 800


class LocalTagInput(BaseModel):
    subject: str | None = Field(default=None, max_length=20)
    limit: int = Field(default=200, ge=1, le=2000)
    threshold: float = Field(default=DEFAULT_THRESHOLD, ge=0.0, le=1.0)
    high_confidence: float = Field(default=DEFAULT_HIGH_CONFIDENCE, ge=0.0, le=1.0)
    dry_run: bool = False


def _cosine(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm <= 0 or right_norm <= 0:
        return 0.0
    return max(-1.0, min(1.0, dot / (left_norm * right_norm)))


def assign_tags(
    question_vectors: list[list[float]],
    kp_vectors: list[list[float]],
    *,
    threshold: float = DEFAULT_THRESHOLD,
    high_confidence: float = DEFAULT_HIGH_CONFIDENCE,
    max_kps: int = MAX_KPS_PER_QUESTION,
) -> list[list[dict]]:
    """纯函数：按余弦相似度给每道题返回 ``[{kp_index, score, source}]``。"""
    assignments: list[list[dict]] = []
    for vector in question_vectors:
        scored = []
        for index, kp_vector in enumerate(kp_vectors):
            score = _cosine(vector, kp_vector)
            if score >= threshold:
                scored.append((score, index))
        scored.sort(reverse=True)
        assignments.append([
            {
                "kp_index": index,
                "score": round(score, 4),
                "source": "local_ai" if score >= high_confidence else "local_ai_pending_review",
            }
            for score, index in scored[:max_kps]
        ])
    return assignments


def _kp_text(row: dict, path: dict[int, str]) -> str:
    aliases = row.get("aliases") or []
    if isinstance(aliases, str):
        aliases = [part for part in aliases.replace("，", ",").split(",") if part.strip()]
    parent = path.get(row.get("parent_id")) or ""
    parts = [str(row.get("name") or "").strip()]
    if parent:
        parts.append(parent)
    if aliases:
        parts.append("别名：" + "、".join(str(item) for item in aliases[:8]))
    description = str(row.get("description") or "").strip()
    if description:
        parts.append(description[:200])
    return "；".join(part for part in parts if part)


def _load_kps(con, subject: str | None) -> list[dict]:
    sql = "SELECT id, subject, parent_id, name, level, aliases_json, description FROM knowledge_points"
    params: list = []
    if subject:
        sql += " WHERE subject=?"
        params.append(subject)
    sql += " ORDER BY subject, level, sort, id"
    rows = [dict(row) for row in con.execute(sql, params).fetchall()]
    path: dict[int, str] = {}
    by_id = {row["id"]: row for row in rows}
    for row in rows:
        names = []
        cursor = row
        guard = 0
        while cursor is not None and guard < 8:
            names.append(str(cursor.get("name") or ""))
            cursor = by_id.get(cursor.get("parent_id"))
            guard += 1
        path[row["id"]] = " / ".join(reversed([name for name in names if name]))
    finest = [row for row in rows if int(row.get("level") or 1) >= 3]
    selected = finest or rows
    return [
        {
            "kp_id": int(row["id"]),
            "subject": row.get("subject") or "",
            "name": row.get("name") or "",
            "aliases": _json_list(row.get("aliases_json")),
            "text": _kp_text(row, path),
        }
        for row in selected
    ]


def _json_list(raw) -> list:
    import json

    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


def _load_questions(con, subject: str | None, limit: int) -> list[dict]:
    sql = (
        "SELECT q.qid, q.subject, q.stem_plain FROM bank.questions q "
        "WHERE LENGTH(TRIM(q.stem_plain)) >= 8 "
        "AND NOT EXISTS ("
        "SELECT 1 FROM question_kp qk WHERE qk.qid=q.qid "
        "AND (qk.source='admin' OR qk.reviewed_by IS NOT NULL)"
        ")"
    )
    params: list = []
    if subject:
        sql += " AND q.subject=?"
        params.append(subject)
    sql += " ORDER BY q.qid LIMIT ?"
    params.append(limit)
    return [dict(row) for row in con.execute(sql, params).fetchall()]


async def run_local_tagging(
    *, user_id: int, subject: str | None = None, limit: int = 200,
    threshold: float = DEFAULT_THRESHOLD,
    high_confidence: float = DEFAULT_HIGH_CONFIDENCE,
    dry_run: bool = False,
) -> dict:
    config = embeddings.embedding_config(user_id)
    if config is None:
        return {
            "available": False, "questions": 0, "tagged": 0, "queued": 0,
            "skipped": 0, "notice": "未配置 Embedding 服务，未写入任何知识点关联。",
        }
    with db.connection(attach_bank=True) as con:
        kps = _load_kps(con, subject)
        questions = _load_questions(con, subject, limit)
    if not kps:
        return {
            "available": True, "questions": 0, "tagged": 0, "queued": 0,
            "skipped": 0, "notice": "没有可用的知识点（请先导入知识点树）。",
        }
    if not questions:
        return {
            "available": True, "questions": 0, "tagged": 0, "queued": 0,
            "skipped": 0, "notice": "没有需要打标的题目（可能都已有确认关联）。",
        }
    texts = [kp["text"] for kp in kps] + [
        str(item.get("stem_plain") or "")[:MAX_QUESTION_TEXT] for item in questions
    ]
    vectors = await embeddings.embed_texts(texts, user_id=user_id)
    if len(vectors) != len(texts):
        return {
            "available": False, "questions": len(questions), "tagged": 0,
            "queued": 0, "skipped": len(questions),
            "notice": "Embedding 调用失败，未写入任何知识点关联。",
        }
    assignments = assign_tags(
        vectors[len(kps):], vectors[:len(kps)],
        threshold=threshold, high_confidence=high_confidence,
    )
    tagged = sum(1 for items in assignments if any(item["source"] == "local_ai" for item in items))
    queued = sum(1 for items in assignments if any(item["source"] != "local_ai" for item in items))
    if dry_run:
        return {
            "available": True, "questions": len(questions), "tagged": tagged,
            "queued": queued, "skipped": 0, "dry_run": True,
            "notice": "预演模式：未写入数据库。",
            "sample": [
                {
                    "qid": questions[index].get("qid"),
                    "tags": [
                        {**item, "kp_id": kps[item["kp_index"]]["kp_id"]}
                        for item in items
                    ],
                }
                for index, items in enumerate(assignments[:10])
            ],
        }
    model = f"vector:{config.storage_model}"[:120]
    now = db.utcnow()
    written = 0
    with db.connection() as con:
        db.write_lock(con, "kp-local-tagging")
        for question, items in zip(questions, assignments):
            qid = str(question.get("qid") or "")
            if not qid:
                continue
            existing = {
                int(row["kp_id"])
                for row in con.execute(
                    "SELECT kp_id FROM question_kp WHERE qid=?", (qid,),
                ).fetchall()
            }
            if items:
                con.execute(
                    "DELETE FROM question_kp WHERE qid=? AND source IN (?,?)",
                    (qid, *LOCAL_SOURCES),
                )
            for item in items:
                kp_id = kps[item["kp_index"]]["kp_id"]
                if kp_id in existing:
                    continue
                con.execute(
                    "INSERT INTO question_kp(qid,kp_id,weight,source,model,confidence,created_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (qid, kp_id, 1.0, item["source"], model, item["score"], now),
                )
                written += 1
    return {
        "available": True, "questions": len(questions), "tagged": tagged,
        "queued": queued, "skipped": 0, "written": written,
        "notice": f"已为 {len(questions)} 道题生成 {written} 条自动关联；低置信关联待复核。",
    }


@router.post("/admin/kp-tag-local")
async def admin_local_tag(data: LocalTagInput | None = None, user=Depends(require_admin)):
    """管理员触发本地/远程向量知识点打标；人工确认的关联不会被覆盖。"""
    payload = data or LocalTagInput()
    return await run_local_tagging(
        user_id=user["id"], subject=payload.subject, limit=payload.limit,
        threshold=payload.threshold, high_confidence=payload.high_confidence,
        dry_run=payload.dry_run,
    )
