"""试卷工坊：AI 补全（ENRICH）答案 / 解析 / 评分标准 / 知识点。

按 recon §2.4：模型只允许补全答案、解析、评分标准与知识点；题干/选项/配图/子题结构
保持不变。硬校验（答案非空、解析非空、主观题有 rubric、选择题答案落在选项 label、
MIXED 子题完整、填空题每空一个答案）。已人工核验的题目 AI 不覆盖。
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..auth import current_user
from ..db import connection, utcnow, write_lock
from . import db, schema
from . import prompts
from .db import MAX_ENRICH_QUESTIONS, json_dump, json_load
from .digitize import (
    _error_text,
    _force_checkpoint,
    _get_checkpoint,
    _is_cancelled,
    _paperlab_jobs,
    _publish_event,
    _set_checkpoint,
    _update_job,
)

router = APIRouter()

ENRICH_TOKEN_LIMIT = 6000


# ---------------------------------------------------------------------------
# 创建任务
# ---------------------------------------------------------------------------

class EnrichInput(BaseModel):
    question_ids: list[int] = Field(min_length=1, max_length=MAX_ENRICH_QUESTIONS)
    provider_id: int = Field(ge=1)
    model: str = Field(min_length=1, max_length=160)


@router.post("/questions/enrich")
async def enrich_questions(data: EnrichInput, user=Depends(current_user)):
    """创建 ``paper-enrich`` 任务：逐题补全答案/解析/知识点，结果回到题库草稿。"""
    question_ids = list(dict.fromkeys(int(qid) for qid in data.question_ids))
    if len(question_ids) > MAX_ENRICH_QUESTIONS:
        raise HTTPException(422, f"单次最多补全 {MAX_ENRICH_QUESTIONS} 道题")
    from ..ai import jobs as ai_jobs

    job_input = ai_jobs.JobInput(
        task="paper-enrich",
        provider_id=data.provider_id,
        model=data.model,
        params={"question_ids": question_ids},
    )
    job = await ai_jobs.submit_job(user, job_input)
    return {
        "job": job,
        "id": job.get("id"),
        "status": job.get("status"),
        "task": job.get("task"),
        "question_ids": question_ids,
    }


def validate_enrich_job(data, user) -> None:
    """``paper-enrich`` 准入校验：题目归属、数量上限（provider 由 jobs 框架校验）。"""
    params = data.params if isinstance(getattr(data, "params", None), dict) else {}
    raw_ids = params.get("question_ids")
    if not isinstance(raw_ids, list) or not raw_ids:
        raise HTTPException(422, "请选择要补全的题目")
    question_ids: list[int] = []
    for value in raw_ids:
        question_id = schema.safe_int(value)
        if question_id is None or question_id < 1:
            raise HTTPException(422, "题目编号不正确")
        if question_id not in question_ids:
            question_ids.append(question_id)
    if len(question_ids) > MAX_ENRICH_QUESTIONS:
        raise HTTPException(422, f"单次最多补全 {MAX_ENRICH_QUESTIONS} 道题")
    for question_id in question_ids:
        if db.find_question(question_id, user["id"]) is None:
            raise HTTPException(404, f"未找到题目 {question_id}")


# ---------------------------------------------------------------------------
# 任务执行器
# ---------------------------------------------------------------------------

def _apply_enrichment(
    question_id: int, user_id: int, enrichment: dict, *, job_id: int, model: str,
    provider_id: int | None,
) -> str:
    """把补全结果写回题目（版本 CAS + 核验状态守卫）；返回 applied / verified / missing / conflict。"""
    now = utcnow()
    with connection() as con:
        write_lock(con, "paperlab-enrich")
        row = con.execute(
            "SELECT * FROM pl_questions WHERE id=? AND user_id=? AND deleted_at IS NULL",
            (question_id, user_id),
        ).fetchone()
        if row is None:
            return "missing"
        if (row["verify_status"] or "pending") == "verified":
            return "verified"
        content = json_load(row["content_json"], {})
        if not isinstance(content, dict):
            content = {}
        if enrichment.get("parts"):
            original_parts = content.get("parts") if isinstance(content.get("parts"), list) else []
            enrich_parts = {
                int(part["index"]): part
                for part in enrichment["parts"]
                if isinstance(part, dict) and schema.safe_int(part.get("index")) is not None
            }
            merged_parts: list = []
            for position, original in enumerate(original_parts):
                if not isinstance(original, dict):
                    merged_parts.append(original)
                    continue
                part_index = schema.safe_int(original.get("index"), position + 1) or (position + 1)
                merged = dict(original)
                update = enrich_parts.get(part_index)
                if update:
                    merged["answers"] = update["answers"]
                    if update.get("explanation"):
                        merged["explanation"] = update["explanation"]
                    if update.get("rubric"):
                        merged["rubric"] = update["rubric"]
                    if update.get("knowledge_tags"):
                        merged["knowledge_tags"] = schema.normalize_tags(
                            list(original.get("knowledge_tags") or []) + update["knowledge_tags"]
                        )
                merged_parts.append(merged)
            content["parts"] = merged_parts
        if enrichment.get("answers"):
            content["answers"] = enrichment["answers"]
        if enrichment.get("explanation"):
            content["explanation"] = enrichment["explanation"]
        if enrichment.get("rubric"):
            content["rubric"] = enrichment["rubric"]
        content["ai"] = {
            "kind": "enrich",
            "provider_id": provider_id,
            "model": model,
            "prompt_version": prompts.PROMPT_VERSION,
            "job_id": job_id,
            "at": now,
        }
        existing_tags = json_load(row["knowledge_tags_json"], [])
        if not isinstance(existing_tags, list):
            existing_tags = []
        tags = schema.normalize_tags(list(existing_tags) + list(enrichment.get("knowledge_tags") or []))
        current_version = int(row["version"] or 1)
        changed = con.execute(
            "UPDATE pl_questions SET content_json=?, knowledge_tags_json=?, verify_status='pending', "
            "version=?, ai_job_id=?, ai_model=?, prompt_version=?, updated_at=? "
            "WHERE id=? AND user_id=? AND version=? AND deleted_at IS NULL",
            (
                json_dump(content), json_dump(tags), current_version + 1, job_id, model,
                prompts.PROMPT_VERSION, now, question_id, user_id, current_version,
            ),
        ).rowcount
        return "applied" if changed else "conflict"


async def run_enrich_job(job_id: int, user_id: int, data, provider, verifier=None):
    """``paper-enrich`` 执行器：逐题调用模型补全，单题失败隔离，支持取消与断点续跑。"""
    from ..ai.adapters import call_structured

    params = dict(getattr(data, "params", None) or {})
    raw_ids = params.get("question_ids") if isinstance(params.get("question_ids"), list) else []
    question_ids: list[int] = []
    for value in raw_ids:
        question_id = schema.safe_int(value)
        if question_id and question_id >= 1 and question_id not in question_ids:
            question_ids.append(question_id)
    if not question_ids:
        raise ValueError("任务参数缺少 question_ids（要补全的题目）")
    if len(question_ids) > MAX_ENRICH_QUESTIONS:
        raise ValueError(f"单次最多补全 {MAX_ENRICH_QUESTIONS} 道题")

    model = str(getattr(data, "model", "") or "")
    provider_id = provider.get("id") if isinstance(provider, dict) else None
    total = len(question_ids)
    checkpoint = _get_checkpoint(job_id)
    done_ids = {int(value) for value in (checkpoint.get("done_ids") or []) if schema.safe_int(value)}
    done_ids &= set(question_ids)
    failed_ids = {int(value) for value in (checkpoint.get("failed_ids") or []) if schema.safe_int(value)}
    failed_ids &= set(question_ids) - done_ids
    skipped = list(checkpoint.get("skipped") or []) if isinstance(checkpoint.get("skipped"), list) else []
    failed = list(checkpoint.get("failed") or []) if isinstance(checkpoint.get("failed"), list) else []
    enriched_count = int(checkpoint.get("enriched_count") or 0)

    def checkpoint_payload() -> dict:
        return {
            "question_ids": question_ids,
            "done_ids": sorted(done_ids),
            "failed_ids": sorted(failed_ids),
            "skipped": skipped[-200:],
            "failed": failed[-200:],
            "enriched_count": enriched_count,
            "total": total,
            "updated_at": utcnow(),
        }

    _update_job(job_id, "running", f"补全 0/{total} 题")
    _publish_event(job_id, "stage", {
        "status": "running", "stage": f"补全 0/{total} 题", "done": 0, "total": total,
    })

    for position, question_id in enumerate(question_ids, start=1):
        if _is_cancelled(job_id):
            _force_checkpoint(job_id, checkpoint_payload())
            _update_job(job_id, "cancelled", "cancelled")
            _publish_event(job_id, "stage", {
                "status": "cancelled", "stage": "cancelled",
                "done": len(done_ids), "total": total,
            })
            return {
                "__cancelled__": True,
                "cancelled": True,
                "question_ids": question_ids,
                "enriched_count": enriched_count,
                "done_ids": sorted(done_ids),
                "skipped": skipped,
                "failed": failed,
                "total": total,
                "notice": f"任务已取消：已补全 {len(done_ids)}/{total} 道题，已完成内容已保存。",
            }
        if question_id in done_ids:
            continue
        question = db.find_question(question_id, user_id)
        if question is None:
            failed_ids.add(question_id)
            failed.append({"question_id": question_id, "error": "题目不存在或已删除"})
            _publish_event(job_id, "stage", {
                "status": "running", "stage": f"题目 {question_id} 不存在，已跳过",
                "question_id": question_id, "state": "failed", "done": len(done_ids), "total": total,
            })
            await asyncio.to_thread(_set_checkpoint, job_id, checkpoint_payload())
            continue
        if (question.get("verify_status") or "pending") == "verified":
            done_ids.add(question_id)
            skipped.append({"question_id": question_id, "reason": "已人工核验，AI 不覆盖"})
            _publish_event(job_id, "stage", {
                "status": "running", "stage": f"第 {position}/{total} 题已核验，跳过",
                "question_id": question_id, "state": "skipped", "done": len(done_ids), "total": total,
            })
            await asyncio.to_thread(_set_checkpoint, job_id, checkpoint_payload())
            _update_job(job_id, "running", f"补全 {len(done_ids)}/{total} 题")
            continue
        if (question.get("status") or "") == "archived":
            done_ids.add(question_id)
            skipped.append({"question_id": question_id, "reason": "题目已归档，跳过"})
            await asyncio.to_thread(_set_checkpoint, job_id, checkpoint_payload())
            continue

        _update_job(job_id, "running", f"补全第 {position}/{total} 题（第 {question_id} 题）")
        _publish_event(job_id, "stage", {
            "status": "running", "stage": f"补全第 {position}/{total} 题",
            "question_id": question_id, "done": len(done_ids), "total": total,
        })
        prompt = prompts.build_enrich_prompt(question)

        def validator(parsed, _question=question):
            return schema.validate_enrichment(parsed, _question)

        try:
            result, _call = await call_structured(
                provider, model, prompt, validator, images=None,
                token_limit=ENRICH_TOKEN_LIMIT, user_id=user_id, job_id=job_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # 单题失败隔离，继续补全其它题
            failed_ids.add(question_id)
            failed.append({"question_id": question_id, "error": _error_text(exc)})
            _publish_event(job_id, "stage", {
                "status": "running", "stage": f"第 {question_id} 题补全失败，已跳过",
                "question_id": question_id, "state": "failed",
                "error": _error_text(exc), "done": len(done_ids), "total": total,
            })
            await asyncio.to_thread(_set_checkpoint, job_id, checkpoint_payload())
            continue
        outcome = await asyncio.to_thread(
            _apply_enrichment, question_id, user_id, result,
            job_id=job_id, model=model, provider_id=provider_id,
        )
        if outcome == "applied":
            enriched_count += 1
            done_ids.add(question_id)
            failed_ids.discard(question_id)
        elif outcome == "verified":
            done_ids.add(question_id)
            skipped.append({"question_id": question_id, "reason": "补全期间题目已被人工核验，已放弃写入"})
        elif outcome == "missing":
            failed_ids.add(question_id)
            failed.append({"question_id": question_id, "error": "题目在补全期间被删除"})
        else:  # conflict：并发修改，交给用户重试
            failed_ids.add(question_id)
            failed.append({"question_id": question_id, "error": "题目在补全期间被修改，本次结果未写入"})
        _publish_event(job_id, "stage", {
            "status": "running", "stage": f"补全 {len(done_ids)}/{total} 题",
            "question_id": question_id, "state": "enriched" if outcome == "applied" else outcome,
            "done": len(done_ids), "total": total,
        })
        await asyncio.to_thread(_set_checkpoint, job_id, checkpoint_payload())
        _update_job(job_id, "running", f"补全 {len(done_ids)}/{total} 题")

    await asyncio.to_thread(_set_checkpoint, job_id, checkpoint_payload())
    notice_parts = [f"AI 补全完成：成功 {enriched_count} 题，跳过 {len(skipped)} 题，失败 {len(failed)} 题。"]
    if skipped:
        notice_parts.append("已核验题目不会被 AI 覆盖。")
    if failed:
        notice_parts.append("失败题目可单独重试。")
    return {
        "question_ids": question_ids,
        "enriched_count": enriched_count,
        "done_ids": sorted(done_ids),
        "failed_ids": sorted(failed_ids),
        "skipped": skipped,
        "failed": failed,
        "total": total,
        "notice": " ".join(notice_parts),
        "summary": " ".join(notice_parts),
    }
