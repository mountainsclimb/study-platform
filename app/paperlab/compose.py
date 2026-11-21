"""试卷工坊 P1：AI 组卷（``paper-compose`` 任务，照 recon §2.3 的硬校验移植）。

流程（两阶段，与 OMP ASSEMBLE 对齐）：
1. 服务端先构建**真实候选池**（应用题库 ``pl_questions`` + 只读高考题库，同科，
   同年级只在应用题库内，可选知识点/来源过滤），候选数量/题型/难度/知识点配额
   做**必要条件的可行性预检**，明显不足直接 422 并给出差额。
2. 任务执行器把候选 JSON 交给模型，模型只允许从候选里选 ref（不得编造 ID），
   输出 ``{"selections":[{"ref","score","reason"}],"summary"}``。
3. 硬校验（不满足则带差额原因重试一次，仍不满足任务失败，绝不缩水）：
   题数精确、题型数量精确、学科一致、平均难度在容差内、知识点配额用
   **Hamilton 最大余数配额 + 人数/题位二分匹配**校验（多标签题只计一次）、
   题目不重复、所有 ref 都在候选内。
4. 通过后按题目快照冻结建卷（kind=smart/knowledge/wrong/layered，status=ready）。

任务支持 checkpoint 续跑（已生成 proposal / 已建 paper 时复用），取消保留已完成
（这里"已完成"= 已落库的试卷；未落库的部分不会留下半成品）。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..auth import current_user
from ..db import connection, utcnow
from . import db, papers, schema
from .db import json_dump

router = APIRouter()

MAX_COMPOSE_COUNT = 60
MAX_CANDIDATES = 150
MAX_KP_WEIGHTS = 20
DEFAULT_TOLERANCE = 0.1
MODE_LABELS = {
    "smart": "智能组卷",
    "knowledge": "知识点组卷",
    "wrong": "错题重组",
    "layered": "分层组卷",
}
MODE_KIND = {"smart": "smart", "knowledge": "knowledge", "wrong": "wrong", "layered": "layered"}
TYPE_ORDER = ("SINGLE", "MULTIPLE", "BLANK", "SUBJECTIVE", "MIXED")
_TYPE_LABELS = {
    "SINGLE": "单选题", "MULTIPLE": "多选题", "BLANK": "填空题",
    "SUBJECTIVE": "主观题", "MIXED": "混合题",
}
_BANK_TYPE_ALIASES = {
    "single_choice": "SINGLE", "multi_choice": "MULTIPLE", "fill_blank": "BLANK",
    "calculation": "SUBJECTIVE", "short_answer": "SUBJECTIVE", "sub_short_answer": "SUBJECTIVE",
    "essay": "SUBJECTIVE", "material_group": "MIXED",
}


# ---------------------------------------------------------------------------
# 入参
# ---------------------------------------------------------------------------

class ComposeInput(BaseModel):
    mode: str = Field(default="smart", max_length=20)
    subject: str = Field(min_length=1, max_length=40)
    grade: str = Field(default="", max_length=40)
    count: int = Field(default=10, ge=1, le=MAX_COMPOSE_COUNT)
    type_counts: dict[str, int] | None = None
    difficulty_target: float | None = Field(default=None, ge=0, le=1)
    tolerance: float = Field(default=DEFAULT_TOLERANCE, ge=0, le=0.5)
    kp_weights: dict[str, float] | None = None
    filters: dict | None = None
    provider_id: int = Field(ge=1)
    model: str = Field(min_length=1, max_length=160)


def _clip_text(value, limit: int, label: str) -> str:
    text = str(value or "").strip()
    if len(text) > limit:
        raise HTTPException(422, f"{label}长度不能超过 {limit} 个字符")
    return text


def normalize_type_counts(raw, count: int) -> dict[str, int]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise HTTPException(422, "type_counts 必须是 {题型: 题数} 对象")
    if not raw:
        return {}
    result: dict[str, int] = {}
    for key, value in raw.items():
        normalized = schema.normalize_qtype(str(key)) or _BANK_TYPE_ALIASES.get(str(key).strip().lower())
        if normalized is None:
            raise HTTPException(422, f"未知的题型：{key}")
        try:
            quantity = int(value)
        except (TypeError, ValueError):
            raise HTTPException(422, f"题型 {key} 的题数必须是整数") from None
        if quantity < 0 or quantity > count:
            raise HTTPException(422, f"题型 {key} 的题数不合法")
        if normalized in result:
            raise HTTPException(422, f"题型 {key} 重复指定")
        result[normalized] = quantity
    total = sum(result.values())
    if total != count:
        raise HTTPException(422, f"题型题数之和必须等于总题数 {count}，当前为 {total}")
    return result


def normalize_kp_weights(raw) -> dict[str, float]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise HTTPException(422, "kp_weights 必须是 {知识点: 百分比} 对象")
    if not raw:
        return {}
    if len(raw) > MAX_KP_WEIGHTS:
        raise HTTPException(422, f"知识点配额最多 {MAX_KP_WEIGHTS} 项")
    result: dict[str, float] = {}
    for key, value in raw.items():
        name = _clip_text(key, 60, "知识点名称")
        if not name:
            raise HTTPException(422, "知识点名称不能为空")
        try:
            weight = float(value)
        except (TypeError, ValueError):
            raise HTTPException(422, f"知识点 {name} 的占比必须是数字") from None
        if not math.isfinite(weight) or weight < 0 or weight > 100:
            raise HTTPException(422, f"知识点 {name} 的占比应在 0–100 之间")
        if name in result:
            raise HTTPException(422, f"知识点 {name} 重复指定")
        result[name] = weight
    if sum(result.values()) <= 0:
        raise HTTPException(422, "知识点占比之和必须大于 0")
    return result


def normalize_filters(raw) -> dict:
    if raw is None:
        return {"source": "all", "knowledge_tags": [], "exclude_refs": []}
    if not isinstance(raw, dict):
        raise HTTPException(422, "filters 必须是 JSON 对象")
    source = str(raw.get("source") or "all").strip().lower()
    if source not in ("all", "app", "bank"):
        raise HTTPException(422, "filters.source 只支持 all / app / bank")
    tags_raw = raw.get("knowledge_tags") or []
    if not isinstance(tags_raw, list) or len(tags_raw) > 10:
        raise HTTPException(422, "filters.knowledge_tags 最多 10 个")
    tags = [_clip_text(tag, 40, "知识点标签") for tag in tags_raw if str(tag).strip()]
    refs_raw = raw.get("exclude_refs") or []
    if not isinstance(refs_raw, list) or len(refs_raw) > 100:
        raise HTTPException(422, "filters.exclude_refs 最多 100 个")
    refs: list[str] = []
    for ref in refs_raw:
        papers.split_ref(ref)
        refs.append(str(ref).strip())
    return {"source": source, "knowledge_tags": tags, "exclude_refs": refs}


def _parse_spec(raw) -> dict:
    """把 params 里的组卷规格规范化（HTTP 路由与任务执行器共用，失败 422）。"""
    if not isinstance(raw, dict):
        raise HTTPException(422, "组卷参数缺少 spec 对象")
    spec = raw.get("spec") if isinstance(raw.get("spec"), dict) else raw
    mode = str(spec.get("mode") or "smart").strip().lower()
    if mode not in MODE_LABELS:
        raise HTTPException(422, "组卷模式只支持 smart / knowledge / wrong / layered")
    subject = _clip_text(spec.get("subject"), 40, "学科")
    if not subject:
        raise HTTPException(422, "请选择组卷学科")
    grade = _clip_text(spec.get("grade"), 40, "年级")
    if grade and grade not in db.GRADES:
        raise HTTPException(422, "未知的年级")
    try:
        count = int(spec.get("count", 10))
    except (TypeError, ValueError):
        raise HTTPException(422, "组卷题数必须是整数") from None
    if count < 1 or count > MAX_COMPOSE_COUNT:
        raise HTTPException(422, f"单次组卷题数应在 1–{MAX_COMPOSE_COUNT} 之间")
    type_counts = normalize_type_counts(spec.get("type_counts"), count)
    difficulty_target = spec.get("difficulty_target")
    if difficulty_target is not None:
        try:
            difficulty_target = float(difficulty_target)
        except (TypeError, ValueError):
            raise HTTPException(422, "difficulty_target 必须是数字") from None
        if difficulty_target < 0 or difficulty_target > 1:
            raise HTTPException(422, "difficulty_target 应在 0–1 之间")
    try:
        tolerance = float(spec.get("tolerance", DEFAULT_TOLERANCE))
    except (TypeError, ValueError):
        raise HTTPException(422, "tolerance 必须是数字") from None
    if tolerance < 0 or tolerance > 0.5:
        raise HTTPException(422, "tolerance 应在 0–0.5 之间")
    kp_weights = normalize_kp_weights(spec.get("kp_weights"))
    filters = normalize_filters(spec.get("filters"))
    return {
        "mode": mode, "subject": subject, "grade": grade, "count": count,
        "type_counts": type_counts,
        "difficulty_target": difficulty_target,
        "tolerance": tolerance,
        "kp_weights": kp_weights,
        "kp_quotas": hamilton_quotas(count, kp_weights),
        "filters": filters,
    }


# ---------------------------------------------------------------------------
# Hamilton 配额 + 二分匹配（照 recon §2.3）
# ---------------------------------------------------------------------------

def hamilton_quotas(count: int, weights: dict[str, float]) -> dict[str, int]:
    """最大余数法（Hamilton）把 ``count × 百分比`` 分配为整数题数配额。

    ``weights`` 是知识点占比（百分比，合计 ≤100）；合计不足 100 时只约束对应比例的
    题数，其余题数不限知识点。多标签题在匹配时只计一次（见 ``match_kp_quotas``）。
    """
    if not weights:
        return {}
    total_weight = sum(max(0.0, float(value)) for value in weights.values())
    if total_weight <= 0:
        return {}
    scale = min(1.0, total_weight / 100.0) if total_weight > 100 else total_weight / 100.0
    target_slots = max(0, min(count, int(round(count * min(1.0, scale))))) if total_weight <= 100 \
        else count
    target_slots = max(0, min(count, target_slots))
    if target_slots <= 0:
        return {}
    raw = {name: target_slots * max(0.0, float(weight)) / total_weight for name, weight in weights.items()}
    quotas = {name: int(math.floor(value)) for name, value in raw.items()}
    assigned = sum(quotas.values())
    remaining = target_slots - assigned
    if remaining > 0:
        order = sorted(raw, key=lambda name: (-(raw[name] - quotas[name]), name))
        for index in range(remaining):
            quotas[order[index % len(order)]] += 1
    return {name: quantity for name, quantity in quotas.items() if quantity > 0}


def match_kp_quotas(candidates: list[dict], quotas: dict[str, int]) -> tuple[bool, dict[str, int]]:
    """把知识点配额题位分配给不同候选（Kuhn 二分匹配；多标签题只匹配一个题位）。

    返回 ``(是否全部匹配, 每个知识点实际匹配到的题数)``。
    """
    if not quotas:
        return True, {}
    slots: list[str] = []
    for kp, quantity in quotas.items():
        slots.extend([kp] * quantity)
    if len(slots) > len(candidates):
        return False, {kp: 0 for kp in quotas}
    matched_candidate: dict[int, int] = {}

    def try_assign(slot_index: int, seen: set[int]) -> bool:
        knowledge_point = slots[slot_index]
        for index, candidate in enumerate(candidates):
            if index in seen:
                continue
            if knowledge_point not in candidate.get("knowledge_tags", []):
                continue
            seen.add(index)
            previous = matched_candidate.get(index)
            if previous is None or try_assign(previous, seen):
                matched_candidate[index] = slot_index
                return True
        return False

    for slot_index in range(len(slots)):
        if not try_assign(slot_index, set()):
            break
    matched_counts = {kp: 0 for kp in quotas}
    for slot_index in matched_candidate.values():
        matched_counts[slots[slot_index]] += 1
    return len(matched_candidate) == len(slots), matched_counts


# ---------------------------------------------------------------------------
# 候选池
# ---------------------------------------------------------------------------

def _app_candidates(user_id: int, subject: str, grade: str) -> list[dict]:
    where = "WHERE user_id=? AND deleted_at IS NULL AND subject=? AND status!='archived'"
    params: list = [user_id, subject]
    if grade:
        where += " AND grade=?"
        params.append(grade)
    with connection() as con:
        rows = con.execute(
            f"SELECT id, type, subject, grade, difficulty, knowledge_tags_json, content_json "
            f"FROM pl_questions {where} ORDER BY id",
            tuple(params),
        ).fetchall()
    candidates = []
    for row in rows:
        qtype = schema.normalize_qtype(row["type"])
        if qtype is None:
            continue
        content = papers.json_load(row["content_json"], {})
        if not isinstance(content, dict) or not str(content.get("stem") or "").strip():
            continue
        tags = [str(tag) for tag in (papers.json_load(row["knowledge_tags_json"], []) or []) if str(tag).strip()]
        score = content.get("score")
        try:
            score = float(score) if score is not None else papers.DEFAULT_SCORES.get(qtype, papers.DEFAULT_SCORE)
        except (TypeError, ValueError):
            score = papers.DEFAULT_SCORES.get(qtype, papers.DEFAULT_SCORE)
        candidates.append({
            "ref": f"app:{int(row['id'])}",
            "source": "app",
            "type": qtype,
            "subject": str(row["subject"] or subject),
            "grade": str(row["grade"] or ""),
            "difficulty": float(row["difficulty"]) if row["difficulty"] is not None else None,
            "difficulty_source": "题目设置" if row["difficulty"] is not None else "未标注",
            "knowledge_tags": tags,
            "stem_preview": papers._plain_stem(content.get("stem"), 140),
            "score": round(score, 2),
        })
    return candidates


def _bank_candidates_for_subject(subject: str, limit: int = 1600) -> list[dict]:
    practice = papers._practice()
    try:
        with practice._bank() as con:
            table = practice._bank_table(con, "questions")
            rows = con.execute(
                "SELECT qid FROM " + table + " WHERE subject=? AND COALESCE(practice_status,'practice')='practice' "
                "AND COALESCE(grading_status,'auto')='auto' ORDER BY qid LIMIT ?",
                (subject, limit),
            ).fetchall()
    except HTTPException:
        return []
    qids = [str(row[0]) for row in rows]
    return _bank_candidates_from_qids(qids, subject=subject)


def _bank_candidates_from_qids(qids: list[str], *, subject: str | None = None) -> list[dict]:
    cards = papers.bank_question_cards(qids)
    candidates = []
    for qid, card in cards.items():
        if subject and card.get("subject") != subject:
            continue
        if card.get("practice_status") not in ("practice", "review"):
            continue
        if card.get("grading_status") not in ("auto", "pending"):
            continue
        candidates.append({
            "ref": f"bank:{qid}",
            "source": "bank",
            "type": card.get("type") or "SUBJECTIVE",
            "subject": card.get("subject") or "",
            "grade": "",
            "difficulty": card.get("difficulty"),
            "difficulty_source": card.get("difficulty_source") or "未标注",
            "knowledge_tags": list(card.get("knowledge_tags") or []),
            "stem_preview": card.get("stem_preview") or "",
            "score": card.get("score") or papers.DEFAULT_SCORE,
        })
    return candidates


def _wrong_qids(user_id: int, subject: str | None = None) -> list[str]:
    with connection() as con:
        rows = con.execute(
            "SELECT qid FROM wrong_questions WHERE user_id=? AND archived=0 "
            "ORDER BY next_review_at ASC, qid ASC",
            (user_id,),
        ).fetchall()
    qids = [str(row[0]) for row in rows]
    if not subject:
        return qids
    cards = papers.bank_question_cards(qids)
    return [qid for qid in qids if (cards.get(qid) or {}).get("subject") == subject]


def build_candidates(user_id: int, plan: dict) -> list[dict]:
    """构建真实候选池（同科；指定年级时只用应用题库，因为只读题库没有年级字段）。"""
    filters = plan["filters"]
    source = filters.get("source") or "all"
    subject = plan["subject"]
    grade = plan["grade"]
    candidates: list[dict] = []
    if plan["mode"] == "wrong":
        qids = _wrong_qids(user_id, subject)
        candidates.extend(_bank_candidates_from_qids(qids, subject=subject))
    else:
        if source in ("all", "app"):
            candidates.extend(_app_candidates(user_id, subject, grade))
        if source in ("all", "bank") and not grade:
            candidates.extend(_bank_candidates_for_subject(subject))
    required_tags = [tag for tag in filters.get("knowledge_tags") or [] if tag]
    excluded = set(filters.get("exclude_refs") or [])
    seen: set[str] = set()
    result: list[dict] = []
    for candidate in candidates:
        ref = candidate["ref"]
        if ref in seen or ref in excluded:
            continue
        if required_tags and not (set(required_tags) & set(candidate.get("knowledge_tags") or [])):
            continue
        seen.add(ref)
        result.append(candidate)
    # 可复现的稳定排序：应用题库在前（含分值/标注更完整），其余按题号。
    result.sort(key=lambda item: (0 if item["source"] == "app" else 1, item["type"], item["ref"]))
    return result


def limit_candidates(candidates: list[dict], plan: dict) -> list[dict]:
    """控制送模型的候选数量（≤MAX_CANDIDATES），优先保留题型/知识点配额的候选。"""
    if len(candidates) <= MAX_CANDIDATES:
        return candidates
    selected: list[dict] = []
    used: set[str] = set()

    def take(candidate: dict) -> None:
        if candidate["ref"] in used or len(selected) >= MAX_CANDIDATES:
            return
        used.add(candidate["ref"])
        selected.append(candidate)

    by_type: dict[str, list[dict]] = {}
    for candidate in candidates:
        by_type.setdefault(candidate["type"], []).append(candidate)
    for qtype, quantity in (plan.get("type_counts") or {}).items():
        for candidate in by_type.get(qtype, [])[:quantity]:
            take(candidate)
    for knowledge_point, quantity in (plan.get("kp_quotas") or {}).items():
        remaining = quantity
        for candidate in candidates:
            if remaining <= 0 or len(selected) >= MAX_CANDIDATES:
                break
            if candidate["ref"] in used:
                continue
            if knowledge_point in candidate.get("knowledge_tags", []):
                take(candidate)
                remaining -= 1
    index = 0
    while len(selected) < MAX_CANDIDATES and index < len(candidates):
        take(candidates[index])
        index += 1
    selected.sort(key=lambda item: (0 if item["source"] == "app" else 1, item["type"], item["ref"]))
    return selected


def _type_counts_from_candidates(candidates: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for candidate in candidates:
        counts[candidate["type"]] = counts.get(candidate["type"], 0) + 1
    return counts


def check_feasible(plan: dict, candidates: list[dict]) -> dict:
    """必要条件预检：明显不可行时立即 422 并说明差额；精确配额由模型输出后硬校验。"""
    count = plan["count"]
    if plan.get("grade") and plan["mode"] == "wrong":
        raise HTTPException(422, "错题重组使用错题原题（只读题库无年级字段），不支持按年级筛选")
    if not candidates:
        raise HTTPException(422, "没有符合条件的候选题目，无法组卷")
    if len(candidates) < count:
        raise HTTPException(
            422, f"候选题目不足：需要 {count} 道，符合条件的只有 {len(candidates)} 道",
        )
    available_types = _type_counts_from_candidates(candidates)
    for qtype, quantity in (plan.get("type_counts") or {}).items():
        available = available_types.get(qtype, 0)
        if available < quantity:
            raise HTTPException(
                422,
                f"题型「{_TYPE_LABELS.get(qtype, qtype)}」不足：需要 {quantity} 道，候选只有 {available} 道",
            )
    type_pool = candidates
    if plan.get("type_counts"):
        type_pool = [candidate for candidate in candidates if candidate["type"] in plan["type_counts"]]
    if plan.get("kp_quotas"):
        matched, matched_counts = match_kp_quotas(type_pool, plan["kp_quotas"])
        if not matched:
            shortages = []
            for kp, quantity in plan["kp_quotas"].items():
                got = matched_counts.get(kp, 0)
                if got < quantity:
                    shortages.append(f"「{kp}」需要 {quantity} 道，候选只能匹配 {got} 道")
            raise HTTPException(422, "知识点配额不足：" + "；".join(shortages))
    difficulty_range = None
    if plan.get("difficulty_target") is not None:
        known = [candidate["difficulty"] for candidate in type_pool if candidate["difficulty"] is not None]
        if len(known) < count:
            raise HTTPException(
                422,
                f"带难度标注的候选题目不足：需要 {count} 道，只有 {len(known)} 道有真实难度数据"
                "（管理员标注 / AI 预估 / 全站统计≥20 次作答）；请降低题数或去掉难度目标",
            )
        low, high = min(known), max(known)
        difficulty_range = [round(low, 3), round(high, 3)]
        target = plan["difficulty_target"]
        tolerance = plan["tolerance"]
        if target + tolerance < low or target - tolerance > high:
            raise HTTPException(
                422,
                f"候选题目难度范围 {low:.2f}–{high:.2f}，无法满足目标 {target:.2f}±{tolerance:.2f}；"
                "请调整难度目标或容差",
            )
    return {
        "total": len(candidates),
        "subject": plan["subject"],
        "grade": plan["grade"] or "",
        "count": count,
        "type_availability": available_types,
        "difficulty_range": difficulty_range,
        "kp_quotas": plan.get("kp_quotas") or {},
    }


def _prepare(user_id: int, spec: dict) -> tuple[dict, list[dict], dict]:
    plan = _parse_spec(spec)
    candidates = limit_candidates(build_candidates(user_id, plan), plan)
    stats = check_feasible(plan, candidates)
    candidate_map = {candidate["ref"]: candidate for candidate in candidates}
    plan["candidate_map"] = candidate_map
    return plan, candidates, stats


# ---------------------------------------------------------------------------
# 模型输出硬校验
# ---------------------------------------------------------------------------

def _proposal_entries(parsed) -> list:
    if not isinstance(parsed, dict):
        raise ValueError("模型返回的不是 JSON 对象")
    for key in ("selections", "items", "questions", "refs"):
        value = parsed.get(key)
        if isinstance(value, list):
            return value
    raise ValueError("模型未返回 selections 列表（只能从候选题中选择 ref）")


def _entry_ref(entry) -> str:
    if isinstance(entry, str):
        return entry.strip()
    if isinstance(entry, dict):
        for key in ("ref", "question_ref", "id"):
            value = entry.get(key)
            if value:
                return str(value).strip()
    return ""


def validate_proposal(parsed, plan: dict) -> dict:
    """硬校验模型选题：题数/题型/学科/难度/知识点配额/去重/候选内。不满足抛 ValueError。"""
    entries = _proposal_entries(parsed)
    count = plan["count"]
    if len(entries) != count:
        raise ValueError(f"题目数量不匹配：要求正好 {count} 道，实际 {len(entries)} 道")
    candidate_map = plan["candidate_map"]
    selected: list[dict] = []
    seen: set[str] = set()
    selection: list[dict] = []
    for index, entry in enumerate(entries):
        ref = _entry_ref(entry)
        if not ref:
            raise ValueError(f"第 {index + 1} 题缺少 ref")
        candidate = candidate_map.get(ref)
        if candidate is None:
            raise ValueError(
                f"第 {index + 1} 题引用了候选列表之外的题目 {ref}；只允许从给定候选题中选择，不得编造 ID",
            )
        if ref in seen:
            raise ValueError(f"题目重复：{ref} 被选了多次")
        seen.add(ref)
        score = None
        reason = ""
        if isinstance(entry, dict):
            raw_score = entry.get("score")
            if raw_score is not None:
                try:
                    score = float(raw_score)
                except (TypeError, ValueError):
                    raise ValueError(f"第 {index + 1} 题分值不是数字") from None
                if score < 0 or score > 100:
                    raise ValueError(f"第 {index + 1} 题分值应在 0–100 之间")
            reason = str(entry.get("reason") or "").strip()[:200]
        selected.append(candidate)
        selection.append({
            "ref": ref, "score": round(score, 2) if score is not None else None, "reason": reason,
        })
    # 学科一致（候选池已按学科构建，这里防御参数被篡改）
    bad_subject = [candidate["ref"] for candidate in selected if candidate.get("subject") != plan["subject"]]
    if bad_subject:
        raise ValueError(f"以下题目学科不一致：{'、'.join(bad_subject[:5])}")
    if plan.get("grade"):
        bad_grade = [candidate["ref"] for candidate in selected if candidate.get("grade") != plan["grade"]]
        if bad_grade:
            raise ValueError(f"以下题目年级不一致：{'、'.join(bad_grade[:5])}")
    # 题型数量精确
    expected_types = plan.get("type_counts") or {}
    actual_types: dict[str, int] = {}
    for candidate in selected:
        actual_types[candidate["type"]] = actual_types.get(candidate["type"], 0) + 1
    if expected_types:
        missing = []
        excess = []
        for qtype, quantity in expected_types.items():
            got = actual_types.get(qtype, 0)
            if got < quantity:
                missing.append(f"{_TYPE_LABELS.get(qtype, qtype)} 要求 {quantity} 道，实际 {got} 道")
            elif got > quantity:
                excess.append(f"{_TYPE_LABELS.get(qtype, qtype)} 要求 {quantity} 道，实际 {got} 道")
        for qtype, got in actual_types.items():
            expected = expected_types.get(qtype, 0)
            if qtype not in expected_types and got:
                excess.append(f"多出{_TYPE_LABELS.get(qtype, qtype)} {got} 道")
        if missing or excess:
            raise ValueError("题型数量不匹配：" + "；".join(dict.fromkeys(missing + excess)))
    # 平均难度
    if plan.get("difficulty_target") is not None:
        unknown = [candidate["ref"] for candidate in selected if candidate["difficulty"] is None]
        if unknown:
            raise ValueError(
                "以下题目没有真实难度标注，无法校验平均难度：" + "、".join(unknown[:5]),
            )
        average = sum(float(candidate["difficulty"]) for candidate in selected) / len(selected)
        target = plan["difficulty_target"]
        tolerance = plan["tolerance"]
        if abs(average - target) > tolerance + 1e-9:
            raise ValueError(
                f"平均难度不满足：目标 {target:.2f}±{tolerance:.2f}，实际 {average:.2f}；请调整选题",
            )
    # 知识点配额（Hamilton + 二分匹配，多标签题只计一次）
    if plan.get("kp_quotas"):
        matched, matched_counts = match_kp_quotas(selected, plan["kp_quotas"])
        if not matched:
            shortages = []
            for kp, quantity in plan["kp_quotas"].items():
                got = matched_counts.get(kp, 0)
                if got < quantity:
                    shortages.append(f"「{kp}」要求 {quantity} 道，只能匹配 {got} 道")
            raise ValueError(
                "知识点配额未满足：" + "；".join(shortages) + "（多标签题只计一次）",
            )
    summary = ""
    if isinstance(parsed, dict):
        summary = str(parsed.get("summary") or parsed.get("reason") or "").strip()[:500]
    return {
        "selections": selection,
        "summary": summary,
        "statistics": {
            "count": len(selected),
            "type_counts": actual_types,
            "difficulty_average": (
                round(sum(float(candidate["difficulty"]) for candidate in selected) / len(selected), 3)
                if plan.get("difficulty_target") is not None else None
            ),
            "kp_matched": matched_counts if plan.get("kp_quotas") else {},
        },
    }


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

def _compose_prompt(plan: dict, candidates: list[dict]) -> str:
    compact = [
        {
            "ref": candidate["ref"],
            "type": _TYPE_LABELS.get(candidate["type"], candidate["type"]),
            "difficulty": round(float(candidate["difficulty"]), 2) if candidate["difficulty"] is not None else None,
            "knowledge_tags": candidate["knowledge_tags"][:4],
            "stem": candidate["stem_preview"][:120],
        }
        for candidate in candidates
    ]
    requirements = [f"题数必须正好 {plan['count']} 道。"]
    if plan.get("type_counts"):
        requirements.append("题型数量必须精确：" + "；".join(
            f"{_TYPE_LABELS.get(qtype, qtype)} {quantity} 道"
            for qtype, quantity in plan["type_counts"].items()
        ) + "。")
    if plan.get("difficulty_target") is not None:
        requirements.append(
            f"平均难度（0 最简单、1 最难）必须落在 {plan['difficulty_target']:.2f}±{plan['tolerance']:.2f}；"
            "只能选 difficulty 不为 null 的题。"
        )
    if plan.get("kp_quotas"):
        requirements.append("知识点配额（多标签题只计一次）：" + "；".join(
            f"「{kp}」{quantity} 道" for kp, quantity in plan["kp_quotas"].items()
        ) + "。")
    requirements.append("只能从「候选题」中选择 ref，禁止编造 ID；同一道题不能重复；学科必须一致。")
    return (
        "你是严谨的中学组卷助手。请按硬性要求从候选题中选出一套试卷。\n\n"
        "【硬性要求】\n" + "\n".join(f"{index}. {text}" for index, text in enumerate(requirements, 1))
        + "\n\n【输出格式】只返回一个 JSON 对象，不要 Markdown 代码围栏：\n"
        '{"selections":[{"ref":"app:1","score":2,"reason":"覆盖函数单调性"}],"summary":"一句话说明选卷思路"}\n'
        "score 可省略（沿用题目默认分）；reason 用中文一句话说明为什么选这道题。\n\n"
        "【候选题（真实题库，共 %d 道）】\n%s\n" % (
            len(candidates), json.dumps(compact, ensure_ascii=False, separators=(",", ":")),
        )
    )


# ---------------------------------------------------------------------------
# HTTP：创建任务
# ---------------------------------------------------------------------------

@router.post("/papers/compose")
async def compose_paper(data: ComposeInput, user=Depends(current_user)):
    """AI 组卷：先做候选/配额可行性预检（不可行 422），再创建 ``paper-compose`` 任务。"""
    if data.mode.strip().lower() not in MODE_LABELS:
        raise HTTPException(422, "组卷模式只支持 smart / knowledge / wrong / layered")
    # 预检放在创建任务之前：明显不可行时直接 422，不消耗模型调用。
    spec = data.model_dump()
    spec["mode"] = data.mode.strip().lower()
    plan, candidates, stats = _prepare(user["id"], spec)
    from ..ai.jobs import JobInput, submit_job

    job = await submit_job(user, JobInput(
        task="paper-compose",
        provider_id=data.provider_id,
        model=data.model,
        subject=plan["subject"],
        params={"spec": {
            "mode": plan["mode"], "subject": plan["subject"], "grade": plan["grade"],
            "count": plan["count"], "type_counts": plan["type_counts"] or None,
            "difficulty_target": plan["difficulty_target"], "tolerance": plan["tolerance"],
            "kp_weights": plan["kp_weights"] or None, "filters": plan["filters"],
        }},
    ))
    return {
        "job": job,
        "id": job.get("id"),
        "task": "paper-compose",
        "status": job.get("status"),
        "mode": plan["mode"],
        "count": plan["count"],
        "candidates": stats,
    }


def validate_compose_job(data, user) -> None:
    """``paper-compose`` 准入：规格合法、候选/配额必要条件满足（provider 由框架校验）。"""
    params = data.params if isinstance(getattr(data, "params", None), dict) else {}
    spec = params.get("spec") if isinstance(params.get("spec"), dict) else params
    _prepare(user["id"], spec)


# ---------------------------------------------------------------------------
# 任务执行器
# ---------------------------------------------------------------------------

def _job_helpers():
    from .digitize import (
        _force_checkpoint, _get_checkpoint, _is_cancelled, _publish_event,
        _set_checkpoint, _update_job,
    )
    return {
        "force_checkpoint": _force_checkpoint, "get_checkpoint": _get_checkpoint,
        "is_cancelled": _is_cancelled, "publish_event": _publish_event,
        "set_checkpoint": _set_checkpoint, "update_job": _update_job,
    }


def _fingerprint(spec: dict, candidates: list[dict]) -> str:
    payload = json.dumps({"spec": spec, "candidates": [candidate["ref"] for candidate in candidates]},
                         ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _freeze_selected(user_id: int, selection: list[dict]) -> list[dict]:
    items: list[dict] = []
    for entry in selection:
        ref = entry["ref"]
        try:
            snapshot = papers.snapshot_for_ref(user_id, ref)
        except HTTPException as exc:
            raise ValueError(f"题目 {ref} 在组卷期间已不可用：{exc.detail}；请重新发起组卷") from None
        snapshot = papers.apply_score(snapshot, entry.get("score"))
        if entry.get("reason"):
            snapshot["selection_reason"] = entry["reason"]
        items.append({"ref": ref, "snapshot": snapshot, "score": float(snapshot["score"]),
                      "section": "", "selection_reason": entry.get("reason") or ""})
    return items


def _paper_result(user_id: int, paper_id: int, *, summary: str, statistics: dict,
                  reason: str | None = None) -> dict:
    detail = papers.paper_detail(paper_id, user_id, include_snapshot=False)
    return {
        "paper_id": paper_id,
        "paper": detail["paper"],
        "item_count": len(detail["items"]),
        "total_score": detail["paper"]["total_score"],
        "summary": summary,
        "statistics": statistics,
        "reason": reason,
    }


async def run_compose_job(job_id: int, user_id: int, data, provider, verifier=None):
    """``paper-compose`` 执行器：候选准备 → 模型选题 → 硬校验 → 快照建卷（可续跑）。"""
    from ..ai.adapters import call_structured
    from . import prompts

    helpers = _job_helpers()
    params = dict(getattr(data, "params", None) or {})
    spec_raw = params.get("spec") if isinstance(params.get("spec"), dict) else params
    plan, candidates, stats = _prepare(user_id, spec_raw)
    spec = {
        "mode": plan["mode"], "subject": plan["subject"], "grade": plan["grade"],
        "count": plan["count"], "type_counts": plan["type_counts"],
        "difficulty_target": plan["difficulty_target"], "tolerance": plan["tolerance"],
        "kp_weights": plan["kp_weights"], "filters": plan["filters"],
    }
    fingerprint = _fingerprint(spec, candidates)
    source = f"ai-compose:{plan['mode']}:job:{job_id}"
    existing = papers.find_paper_by_source(user_id, source)
    if existing is not None:  # 续跑/重试幂等：同一任务只生成一张卷
        return _paper_result(user_id, int(existing["id"]), summary="组卷已完成（复用同任务的试卷）",
                             statistics={"count": plan["count"], "reused": True})
    checkpoint = helpers["get_checkpoint"](job_id) or {}
    proposal = None
    if checkpoint.get("fingerprint") == fingerprint and isinstance(checkpoint.get("proposal"), dict):
        try:
            proposal = validate_proposal(checkpoint["proposal"], plan)
        except ValueError:
            proposal = None
    model = str(getattr(data, "model", "") or "")
    provider_id = provider.get("id") if isinstance(provider, dict) else getattr(provider, "id", None)
    if proposal is None:
        helpers["update_job"](job_id, "running", f"组卷候选准备：{len(candidates)} 道候选")
        helpers["publish_event"](job_id, "stage", {
            "status": "running", "stage": f"组卷候选准备：{len(candidates)} 道候选",
            "candidate_count": len(candidates),
        })
        if helpers["is_cancelled"](job_id):
            raise asyncio.CancelledError()
        prompt = _compose_prompt(plan, candidates)
        helpers["update_job"](job_id, "running", "AI 选题")
        helpers["publish_event"](job_id, "stage", {
            "status": "running", "stage": "AI 选题", "candidate_count": len(candidates),
        })
        try:
            parsed, call = await call_structured(
                provider, model, prompt,
                lambda value: validate_proposal(value, plan),
                token_limit=6000, user_id=user_id, job_id=job_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # 模型输出仍不满足硬校验：任务失败并给出差额原因
            from .digitize import _error_text
            raise ValueError(f"AI 组卷未通过硬校验：{_error_text(exc)}") from None
        proposal = parsed
        helpers["set_checkpoint"](job_id, {
            "fingerprint": fingerprint, "proposal": {
                "selections": proposal["selections"], "summary": proposal["summary"],
            },
            "updated_at": utcnow(),
        })
        helpers["publish_event"](job_id, "stage", {
            "status": "running", "stage": "硬校验通过", "statistics": proposal["statistics"],
        })
    if helpers["is_cancelled"](job_id):
        raise asyncio.CancelledError()
    helpers["update_job"](job_id, "running", "生成试卷快照")
    helpers["publish_event"](job_id, "stage", {"status": "running", "stage": "生成试卷快照"})
    items = _freeze_selected(user_id, proposal["selections"])
    if helpers["is_cancelled"](job_id):
        raise asyncio.CancelledError()
    title = f"{plan['subject']}{(' · ' + plan['grade']) if plan['grade'] else ''} · {MODE_LABELS[plan['mode']]}（{plan['count']} 题）"
    paper = papers.create_paper(
        user_id,
        title=title,
        kind=MODE_KIND[plan["mode"]],
        subject=plan["subject"],
        grade=plan["grade"],
        instructions="AI 从真实题库候选中组卷；题目内容按建卷时快照冻结。",
        layout=None,
        status="ready",
        source=source,
        items=items,
    )
    paper_id = int(paper["id"])
    helpers["set_checkpoint"](job_id, {
        "fingerprint": fingerprint, "paper_id": paper_id,
        "proposal": {"selections": proposal["selections"], "summary": proposal["summary"]},
        "updated_at": utcnow(),
    })
    helpers["publish_event"](job_id, "stage", {
        "status": "running", "stage": "组卷完成", "paper_id": paper_id,
    })
    statistics = dict(proposal["statistics"])
    statistics.update({
        "candidates": stats,
        "ai": {"model": model, "provider_id": provider_id, "prompt_version": prompts.PROMPT_VERSION},
    })
    return _paper_result(user_id, paper_id, summary=proposal["summary"] or "AI 组卷完成",
                         statistics=statistics)


__all__ = [
    "router", "run_compose_job", "validate_compose_job", "ComposeInput",
    "hamilton_quotas", "match_kp_quotas", "build_candidates", "check_feasible",
    "validate_proposal", "MAX_COMPOSE_COUNT",
]
