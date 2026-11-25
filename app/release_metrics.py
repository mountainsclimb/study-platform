"""发布指标：上传完成率与复习回流率（管理员只读）。

方案 §8 的发布跟踪五项指标里，评审时长/内容质量/隐私撤回已分别在
``app/plaza/metrics.py``（广场质量）与 ``app/monitor.py``（撤回传播）中统计，
这里补齐剩余两项：

**上传完成率**
    口径：``成功校对入库数 ÷ 发起上传数``，并按通道拆分——
    ``image`` 走拍照/图片 OCR 通道（``media_type`` 以 ``image/`` 开头），
    ``text`` 是手录/粘贴文本通道（``media_type = text/plain``）。
    「入库」以 ``ai_uploads.saved_at`` 非空为准（对应 ``POST /api/ai/intake/.../save``
    真正写进我的题库）；``confirmed_at`` 非空但未入库算「校对完成未保存」，单独计数，
    因为它是最需要运营介入的一步：用户已经花时间校对却放弃了保存。

**复习回流率**
    口径：``进入待复习的可判错题 ÷ 符合条件的错题``。
    * 符合条件的错题：``wrong_questions`` 中未归档（``archived = 0``）的行；
    * 进入待复习：其中已经生成复习卡（``review_cards.card_type = 'wrong'`` 且未归档）的行。
    分子用 ``question_ref`` 对齐而不是 ``qid``，因为个人题的卡键带 ``app:`` 前缀。

两个比率在分母为 0 时返回 ``None`` 而不是 0，前端显示「—」，避免「没有数据」被读成
「完成率 0%」。窗口按 ``ai_uploads.created_at`` / ``wrong_questions.added_at`` 截断，
默认 30 天（与 ``/api/admin/ai/usage`` 一致）。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query

from . import db, question_ref
from .auth import require_admin

router = APIRouter(prefix="/api", tags=["admin"])

#: 上传窗口内「已过校对但没入库」的滞留判定：确认后超过该时长仍未保存就计入滞留。
STALE_CONFIRM_HOURS = 24


def _cutoff(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _ratio(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return round(100.0 * numerator / denominator, 1)


def _channel_of(media_type: str) -> str:
    return "image" if str(media_type or "").startswith("image/") else "text"


def upload_stats(con, days: int) -> dict:
    """上传完成率：按通道拆分的发起/校对/入库计数与比率。"""
    tables = db.table_names(con)
    if "ai_uploads" not in tables:
        return {
            "available": False,
            "note": "还没有上传记录（ai_uploads 表尚未创建）。",
            "days": days,
            "total": 0,
            "channels": {},
        }
    columns = _columns(con, "ai_uploads")
    has_confirmed = "confirmed_at" in columns
    has_saved = "saved_at" in columns
    # 用固定顺序的列清单取值：SQLite Row 与 PG dict 都能按同一顺序读到。
    selected = ["media_type", "status"]
    if has_confirmed:
        selected.append("confirmed_at")
    if has_saved:
        selected.append("saved_at")
    since = _cutoff(days)
    rows = con.execute(
        "SELECT %s FROM ai_uploads WHERE created_at>=?" % ", ".join(selected),
        (since,),
    ).fetchall()
    channels: dict[str, dict] = {}
    for row in rows:
        values = [row[name] for name in selected] if isinstance(row, dict) else list(row)
        media_type, status = values[0], values[1]
        confirmed_at = values[2] if has_confirmed else None
        saved_at = values[-1] if has_saved else None
        bucket = channels.setdefault(_channel_of(media_type), {
            "started": 0, "recognized": 0, "confirmed": 0, "saved": 0, "stalled": 0,
        })
        bucket["started"] += 1
        if str(status or "") in ("recognized", "confirmed"):
            bucket["recognized"] += 1
        if confirmed_at:
            bucket["confirmed"] += 1
        if saved_at:
            bucket["saved"] += 1
        elif confirmed_at and not saved_at:
            # 校对完成却没入库：确认时间越久越说明流程卡住了。
            try:
                confirmed_dt = datetime.fromisoformat(str(confirmed_at))
            except ValueError:
                confirmed_dt = None
            if confirmed_dt is not None:
                if confirmed_dt.tzinfo is None:
                    confirmed_dt = confirmed_dt.replace(tzinfo=timezone.utc)
                age_hours = (datetime.now(timezone.utc) - confirmed_dt).total_seconds() / 3600
                if age_hours >= STALE_CONFIRM_HOURS:
                    bucket["stalled"] += 1
    total = {"started": 0, "recognized": 0, "confirmed": 0, "saved": 0, "stalled": 0}
    for bucket in channels.values():
        for key in total:
            total[key] += bucket[key]
    for bucket in channels.values():
        bucket["completion_rate"] = _ratio(bucket["saved"], bucket["started"])
    total["completion_rate"] = _ratio(total["saved"], total["started"])
    note = "上传完成率 = 成功校对入库数 ÷ 发起上传数；按图片（拍照/OCR）与文本（手录/粘贴）两条通道分开统计。"
    if not has_saved:
        note = "当前库还没有入库留痕字段（saved_at），完成率无法统计；升级后新上传的记录会计入。"
    return {
        "available": bool(rows),
        "note": note,
        "days": days,
        "total": total,
        "channels": channels,
    }


def review_stats(con, days: int) -> dict:
    """复习回流率：符合条件的错题里，有多少已经进入待复习队列。"""
    tables = db.table_names(con)
    if "wrong_questions" not in tables:
        return {
            "available": False,
            "note": "还没有错题记录，无法统计复习回流率。",
            "days": days,
            "eligible": 0,
            "scheduled": 0,
            "rate": None,
        }
    since = _cutoff(days)
    eligible_rows = con.execute(
        "SELECT qid FROM wrong_questions WHERE archived=0 AND added_at>=?",
        (since,),
    ).fetchall()
    eligible = len(eligible_rows)
    scheduled = 0
    if "review_cards" in tables and eligible:
        # 用统一的引用规范化对齐，避免手写前缀规则跟 question_ref 模块漂移：
        # 题库裸 qid → ``bank:<qid>``，个人题/官方题保留 ``app:`` / ``official:``。
        wanted = {
            question_ref.normalize(row[0] if not isinstance(row, dict) else row["qid"])
            for row in eligible_rows
        }
        card_rows = con.execute(
            "SELECT question_ref FROM review_cards WHERE archived=0 AND card_type='wrong'",
        ).fetchall()
        have = {
            str(row[0] if not isinstance(row, dict) else row["question_ref"])
            for row in card_rows
        }
        scheduled = len(wanted & have)
    return {
        "available": eligible > 0,
        "note": (
            "复习回流率 = 已进入待复习队列的错题 ÷ 未归档错题；"
            "错题生成复习卡即计入，分母为窗口内新增的未归档错题。"
        ),
        "days": days,
        "eligible": eligible,
        "scheduled": scheduled,
        "rate": _ratio(scheduled, eligible),
    }


def _columns(con, table: str) -> set[str]:
    """表字段集合（SQLite/PG 通用；只用一次 ``LIMIT 0`` 探测，不做全表扫描）。"""
    try:
        cursor = con.execute("SELECT * FROM %s LIMIT 0" % table)
        description = getattr(cursor, "description", None) or []
        return {str(item[0]) for item in description}
    except Exception:  # noqa: BLE001 - 探测失败按「无该字段」处理，指标降级而不是 500
        return set()


@router.get("/admin/release-metrics")
def admin_release_metrics(
    days: int = Query(30, ge=1, le=365),
    user=Depends(require_admin),
):
    """发布指标（管理员）：上传完成率与复习回流率，附口径说明。"""
    with db.connection() as con:
        uploads = upload_stats(con, days)
        review = review_stats(con, days)
    return {
        "days": days,
        "generated_at": db.utcnow(),
        "upload": uploads,
        "review": review,
        "caliber": {
            "upload": uploads.get("note"),
            "review": review.get("note"),
            "stale_confirm_hours": STALE_CONFIRM_HOURS,
        },
    }


__all__ = [
    "router",
    "upload_stats",
    "review_stats",
    "STALE_CONFIRM_HOURS",
]
