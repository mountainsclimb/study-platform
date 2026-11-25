"""首页统计口径（方案 §1 三个用户动作 / §7「学情追踪」）。

首页与学情页共用同一套真实数据口径，不在前端复制或估算任何数字：

题库全局（当前只读发布包，例如 r5）：
* **可练**：``practice_status='practice'``，并扣除运行期门禁——公开字段混入答案解析、
  硬隔离/题组隔离（``practice`` 的 ``bank_leak_flags``）与管理员临时隔离
  （``practice._active_isolations``）。直接复用 practice 的 ``_answer_leak_sql`` /
  ``_excluded_sql`` 查询构件，不在首页重复实现门禁。
* **可自动判分**：可练且 ``grading_status='auto'``，再扣除题组材料缺失、参考答案
  缺失/占位以及数据侧 ``quality_flags_json`` 标记的缺图/答案缺失/多选答案待定
  （``missing_figure`` / ``missing_image`` / ``answer_missing`` /
  ``answer_and_solution_missing`` / ``multi_choice_answer``）。
* **待核验**：题库可见题中不能自动判分的部分（practice/review 状态与 pending grading）。
  因此 ``可自动判分 + 待核验 = 题库可见题数``，被门禁排除的题单独列在 ``excluded``。

个人（当前登录账号）：
* **待复习**：``wrong_questions`` 未归档且 ``next_review_at <= 现在``（与错题本/学情一致）。
* **今日练习**：今日（UTC 自然日，与 ``db.utcnow()`` 一致）的题库/个人题作答次数 +
  课堂小测次数 + 已提交整卷练习次数，按作答事件计数。
* **首次正确率**：每题第一次**已判定**作答（result != 'pending'）的正确占比；同一道题
  之后的重复作答不再计入样本，避免「刷题次数」抬高正确率。题库题与个人题（``app:`` 引用）
  都按题目身份去重。逐题指标以统一作答记录 ``attempts`` 为唯一来源（整卷练习只计入今日
  活动次数），避免整卷逐题入账后重复计数。
* **重复练习**：有 ≥2 次作答的题目数，以及第 2 次起的重复作答次数。
* **待判分**：最近一次作答仍为 pending（参考答案待核验/主观题）的题目数。
* **薄弱知识点**：复用 ``app.insight`` 的知识点口径（``question_kp`` 逐题关联、正确率 < 70%），
  返回真实样本数；已判定样本 < 3 时标记 ``sample_limited``，不做个性化推断。

平台精选（独立 ``official`` 块，与 ``bank``/``personal`` 平级）：
* **口径**：只统计当前公开可见的精选题（社区待核验/已核验/精选；撤回/争议在查询时即时剔除），
  由 ``app/official.official_caliber`` 统一计算；本人已练精选数/精选错题数/到期复习同样按题目
  当前可见性过滤，撤回题的历史记录保留但不计入。``bank.official_practice_pool`` 供首页题库卡片
  展示「平台精选可练数量」。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from fastapi import HTTPException

from . import db, practice, vocab

DEFAULT_DAYS = 90
# 知识点「样本不足」阈值：与学情页优势知识点门槛一致，少于 3 次已判定作答只展示样本数。
#: 薄弱判定的样本门槛：与 ``app/insight.py`` 的 ``MIN_WEAK_SAMPLE`` 同值（两处独立常量，
#: 改动需同步；insight 的 ``weak`` 布尔已内置该门槛，这里用于「仅展示真实样本数」的降级标记）。
MIN_WEAK_SAMPLE = 3

#: 首页薄弱清单的入选条件比 insight 的 ``weak`` 徽标更宽：样本不足（已判定 < 3 次）但正确率
#: 偏低的知识点也列出来，并在 ``sample_limited`` 上标注「只报真实样本数」。这样学生能看到
#: 「练了 2 次都错」这种早期信号，又不会被当成能力结论去生成掌握路径/推送推荐。
WEAK_LIST_ACCURACY = 70.0
# 数据侧质量标记：这些代码意味着答案/题面不完整，不能保证自动判分。
BLOCKING_QUALITY_CODES = (
    "missing_figure",
    "missing_image",
    "answer_missing",
    "answer_and_solution_missing",
    "multi_choice_answer",
)
_MAX_WEAK = 3
_BANK_CACHE: dict = {"key": None, "value": None}

CALIBER = {
    "bank": (
        "题库全局口径来自当前只读发布包：可练 = practice 状态扣除答案泄露/硬隔离/临时隔离；"
        "可自动判分 = 可练且 grading_status=auto，再扣除题组材料缺失、参考答案缺失/占位与"
        "数据侧缺图/答案缺失标记；待核验 = 题库可见题中不能自动判分的部分。"
        "首页与练习/检索复用同一套 practice 门禁，不写死数字。"
    ),
    "today": (
        "今日练习次数 = 今日（UTC 自然日）的题库/个人题作答次数 + 课堂小测次数 + "
        "已提交整卷练习次数，按作答事件计，不做本地估算。"
    ),
    "first_attempt": (
        "首次正确率 = 每题第一次已判定作答（result != 'pending'）中判对的占比；"
        "同一道题之后的重复作答不再计入样本。逐题指标以统一作答记录 attempts 为唯一来源"
        "（题库题与 app: 个人题同表；整卷练习只计入今日活动次数），避免整卷逐题入账后重复计数。"
    ),
    "repeat": "重复练习 = 有 ≥2 次作答的题目数；repeat_attempts 为第 2 次起的重复作答次数。",
    "pending": "待判分 = 最近一次作答仍为 pending（参考答案待核验/主观题）的题目数。",
    "weak": (
        "薄弱知识点复用学情口径：按 question_kp 题目关联逐题统计，正确率 < 70% 视为薄弱；"
        "返回真实样本数，已判定样本 < " + str(MIN_WEAK_SAMPLE) + " 次时标记 sample_limited，"
        "不做个性化推断。"
    ),
    "due": "待复习 = 错题本未归档且已到复习时间（next_review_at）的题目数，与错题本一致。",
    "review_due": (
        "『今日复习』以服务端四档复习卡（/api/review-cards）为准：due_now=已到期未归档卡，"
        "due_today=本 UTC 自然日结束前到期（含已逾期）。卡到期时间在同一事务镜像到旧错题字段，"
        "因此首页、错题本、复习队列、学情与学习计划看到同一个日期。"
    ),
    "official": (
        "平台精选口径来自 app/official.py：只统计当前公开可见的精选题（社区待核验/已核验/精选；"
        "撤回/争议在查询时即时剔除，不缓存）；可自动判分=单选/多选（有选项且有答案）或填空题"
        "（有答案）；已练精选数、精选错题数与到期复习同样按题目当前可见性过滤，撤回题的历史记录"
        "保留但不计入本口径。首页与 /api/stats、/api/questions?source=official 同一口径。"
    ),
}


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _today_start_iso() -> str:
    now = _now()
    return now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()


def _safe_scalar(con, sql: str, params=(), default=0):
    try:
        row = con.execute(sql, params).fetchone()
    except Exception:  # noqa: BLE001 - 单个模块表缺失不应让首页统计整体失败
        return default
    if row is None or row[0] is None:
        return default
    return row[0]


def _quality_flag_codes(raw) -> set[str]:
    """数据侧 ``quality_flags_json`` 的 code 集合（与 practice._data_quality_flags 同源）。"""
    try:
        items = json.loads(raw) if raw else []
    except (TypeError, ValueError):
        return set()
    codes: set[str] = set()
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict):
            code = str(item.get("code") or "").strip()
            if code:
                codes.add(code)
    return codes


# ---------------------------------------------------------------------------
# 题库全局口径（只读题库 + practice 运行期门禁）
# ---------------------------------------------------------------------------


def _bank_cache_key() -> tuple:
    path = practice._bank_dir() / "题库.db"
    try:
        stat = path.stat()
        signature = (str(path.resolve()), stat.st_mtime_ns, stat.st_size)
    except OSError:
        signature = (str(path), 0, 0)
    try:
        data_dir = str(db.DATA_DIR)
    except Exception:  # noqa: BLE001
        data_dir = ""
    try:
        isolated = tuple(sorted(practice._active_isolations().keys()))
    except Exception:  # noqa: BLE001
        isolated = ()
    return (signature, data_dir, isolated)


def _compute_bank_caliber() -> dict:
    """按 practice 同一套门禁统计题库可练 / 可自动判分 / 待核验数量（只读）。"""
    with practice._app_with_bank() as con:
        practice.ensure_leak_flags(con)
        questions = practice._bank_table(con, "questions")
        leak_clauses, _ = practice._answer_leak_sql(con, prefix="q")
        # _excluded_sql(alias) 返回 (子句字符串, 参数)，内部生成 "q.qid" / "q.group_id" 前缀，
        # 含硬隔离常量与管理员临时隔离；与检索/练习使用同一套门禁。
        excluded_clause, excluded_params = practice._excluded_sql("q")
        excluded_params = list(excluded_params or [])
        excluded_clauses = [excluded_clause] if excluded_clause else []
        isolation_qids = sorted(practice._active_isolations().keys())
        visible = " AND ".join([*leak_clauses, *excluded_clauses]) or "1=1"
        total = int(con.execute(f"SELECT COUNT(*) FROM {questions} q").fetchone()[0])
        # 被门禁排除的题：泄露/硬隔离与临时隔离分开统计（两套条件可能重叠，仅作口径提示）。
        leak_only = int(con.execute(
            f"SELECT COUNT(*) FROM {questions} q WHERE NOT ({' AND '.join(leak_clauses)})"
        ).fetchone()[0]) if leak_clauses else 0
        isolation_only = int(con.execute(
            f"SELECT COUNT(*) FROM {questions} q WHERE q.qid IN ({','.join('?' for _ in isolation_qids)})",
            isolation_qids,
        ).fetchone()[0]) if isolation_qids else 0

        grouped = con.execute(
            f"SELECT COALESCE(q.practice_status,'practice') AS ps, "
            f"COALESCE(q.grading_status,'auto') AS gs, COUNT(*) AS n "
            f"FROM {questions} q WHERE {visible} GROUP BY 1, 2",
            excluded_params,
        ).fetchall()
        status_counts = {"practice": 0, "review": 0, "context": 0, "quarantined": 0}
        grading_counts = {"auto": 0, "pending": 0, "disabled": 0}
        visible_total = 0
        practice_pool = 0
        auto_db = 0
        for row in grouped:
            status, grading, count = str(row[0]), str(row[1]), int(row[2])
            status_counts[status] = status_counts.get(status, 0) + count
            grading_counts[grading] = grading_counts.get(grading, 0) + count
            visible_total += count
            if status == "practice":
                practice_pool += count
                if grading == "auto":
                    auto_db += count

        # 可自动判分：在 practice+auto 基础上扣除题组材料缺失、答案缺失与数据侧质量标记。
        group_join = practice._bank_group_join(con, "g")
        candidate_rows = con.execute(
            f"SELECT q.qid, COALESCE(q.answer_text,''), COALESCE(q.quality_flags_json,''), "
            f"CASE WHEN q.group_id IS NOT NULL AND g.group_id IS NULL THEN 1 ELSE 0 END "
            f"FROM {questions} q {group_join} "
            f"WHERE {visible} AND COALESCE(q.practice_status,'practice')='practice' "
            f"AND COALESCE(q.grading_status,'auto')='auto'",
            excluded_params,
        ).fetchall()
        auto_gradable = 0
        gates = {
            "leak_or_quarantine": leak_only,
            "isolated": isolation_only,
            "missing_group": 0,
            "missing_answer": 0,
            **{code: 0 for code in BLOCKING_QUALITY_CODES},
        }
        for row in candidate_rows:
            if row[3]:
                gates["missing_group"] += 1
                continue
            if not practice._has_reference(row[1]):
                gates["missing_answer"] += 1
                continue
            codes = _quality_flag_codes(row[2])
            hit = next((code for code in BLOCKING_QUALITY_CODES if code in codes), None)
            if hit:
                gates[hit] += 1
                continue
            auto_gradable += 1

        pending_verification = visible_total - auto_gradable
        try:
            bank_version = practice._current_bank_version()
        except sqlite3.Error:  # noqa: PERF203 - 旧库可能没有 bank_version 列
            bank_version = ""
    return {
        "available": True,
        "bank_version": bank_version,
        "bank_source": db.BANK_SOURCE,
        "total_questions": total,
        "visible_questions": visible_total,
        "practice_pool": practice_pool,
        "auto_gradable": auto_gradable,
        "pending_verification": pending_verification,
        "excluded_by_gates": total - visible_total,
        "status_counts": status_counts,
        "grading_counts": grading_counts,
        "auto_status_count": auto_db,
        "gates": gates,
        "caliber": CALIBER["bank"],
    }


def _bank_caliber() -> dict:
    key = _bank_cache_key()
    if _BANK_CACHE["key"] == key and _BANK_CACHE["value"] is not None:
        return _BANK_CACHE["value"]
    try:
        value = _compute_bank_caliber()
    except HTTPException as exc:
        return {"available": False, "note": str(exc.detail or "题库暂不可用")}
    except FileNotFoundError:
        return {"available": False, "note": "未找到题库，请检查 STUDY_BANK_DIR 配置"}
    except Exception:  # noqa: BLE001 - 题库异常时首页仍展示个人统计
        return {"available": False, "note": "题库统计暂不可用"}
    _BANK_CACHE["key"] = key
    _BANK_CACHE["value"] = value
    return value


# ---------------------------------------------------------------------------
# 个人口径
# ---------------------------------------------------------------------------


def _today_payload(user_id: int) -> dict:
    """今日练习次数：题库/个人题作答 + 课堂小测 + 已提交整卷（UTC 自然日）。"""
    start = _today_start_iso()
    with db.connection() as con:
        practice_count = int(_safe_scalar(
            con, "SELECT COUNT(*) FROM attempts WHERE user_id=? AND created_at>=?",
            (user_id, start),
        ))
        classroom_count = int(_safe_scalar(
            con, "SELECT COUNT(*) FROM cls_quiz_attempts WHERE user_id=? AND created_at>=?",
            (user_id, start),
        ))
        paper_count = int(_safe_scalar(
            con,
            "SELECT COUNT(*) FROM pl_paper_attempts WHERE user_id=? AND status='submitted' "
            "AND COALESCE(submitted_at, created_at)>=?",
            (user_id, start),
        ))
    return {
        "date": start[:10],
        "total": practice_count + classroom_count + paper_count,
        "practice": practice_count,
        "classroom": classroom_count,
        "paper": paper_count,
    }


def _attempt_quality_payload(user_id: int) -> dict:
    """首次正确率 / 重复练习 / 待判分（按题目身份去重，题库题与个人题同口径）。"""
    with db.connection() as con:
        rows = con.execute(
            "SELECT qid, result FROM attempts WHERE user_id=? ORDER BY id", (user_id,)
        ).fetchall()
    counts: dict[str, int] = {}
    last_result: dict[str, str] = {}
    first_graded: dict[str, str] = {}
    pending_attempts = 0
    for row in rows:
        qid, result = str(row[0]), str(row[1])
        counts[qid] = counts.get(qid, 0) + 1
        last_result[qid] = result
        if result == "pending":
            pending_attempts += 1
        elif qid not in first_graded:
            first_graded[qid] = result
    first_correct = sum(1 for result in first_graded.values() if result == "correct")
    first_samples = len(first_graded)
    repeat_questions = sum(1 for count in counts.values() if count >= 2)
    return {
        "first_attempt": {
            "samples": first_samples,
            "correct": first_correct,
            "accuracy": round(100.0 * first_correct / first_samples, 1) if first_samples else 0.0,
        },
        "repeat_practice": {
            "questions": repeat_questions,
            "attempts": sum(count - 1 for count in counts.values() if count >= 2),
        },
        "pending_grading": {
            "questions": sum(1 for result in last_result.values() if result == "pending"),
            "attempts": pending_attempts,
        },
    }


def _library_payload(user_id: int) -> dict:
    """我的题库当前数量（个人题与待核验数，真实计数）。"""
    with db.connection() as con:
        questions = int(_safe_scalar(
            con, "SELECT COUNT(*) FROM pl_questions WHERE user_id=? AND deleted_at IS NULL",
            (user_id,),
        ))
        pending = int(_safe_scalar(
            con, "SELECT COUNT(*) FROM pl_questions WHERE user_id=? AND deleted_at IS NULL "
                 "AND verify_status='pending'",
            (user_id,),
        ))
        drafts = int(_safe_scalar(
            con, "SELECT COUNT(*) FROM pl_questions WHERE user_id=? AND deleted_at IS NULL "
                 "AND status='draft'",
            (user_id,),
        ))
    return {"questions": questions, "pending_review": pending, "drafts": drafts}


def _continue_payload(user_id: int) -> dict:
    """「继续上次练习」入口：最近一次作答与最近一次已提交整卷练习。"""
    with db.connection() as con:
        attempt = con.execute(
            "SELECT qid, result, created_at FROM attempts WHERE user_id=? "
            "ORDER BY id DESC LIMIT 1", (user_id,),
        ).fetchone()
        paper = con.execute(
            "SELECT a.paper_id, a.score, a.max_score, "
            "COALESCE(a.submitted_at, a.created_at) AS finished_at, p.title "
            "FROM pl_paper_attempts a LEFT JOIN pl_papers p ON p.id=a.paper_id "
            "WHERE a.user_id=? AND a.status='submitted' "
            "ORDER BY COALESCE(a.submitted_at, a.created_at) DESC, a.id DESC LIMIT 1",
            (user_id,),
        ).fetchone()
    last_attempt = None
    if attempt is not None:
        last_attempt = {
            "qid": attempt[0],
            "result": attempt[1],
            "created_at": attempt[2],
        }
    last_paper = None
    if paper is not None:
        last_paper = {
            "paper_id": paper[0],
            "score": round(float(paper[1] or 0), 2),
            "max_score": round(float(paper[2] or 0), 2),
            "finished_at": paper[3],
            "title": str(paper[4] or ""),
        }
    return {"last_attempt": last_attempt, "last_paper": last_paper}


def _weak_knowledge(knowledge: dict) -> tuple[list[dict], str | None]:
    """薄弱知识点（复用 insight._knowledge_payload 的 items），带真实样本数。"""
    items = [
        item for item in (knowledge.get("items") or [])
        if item.get("weak")
        or (
            int(item.get("graded_attempts") or 0) > 0
            and float(item.get("accuracy") or 0) < WEAK_LIST_ACCURACY
        )
    ]
    items.sort(key=lambda item: (item.get("accuracy", 0), -item.get("attempts", 0), item.get("title") or ""))
    weak = []
    for item in items[:_MAX_WEAK]:
        graded = int(item.get("graded_attempts") or 0)
        weak.append({
            "kp_id": item.get("kp_id"),
            "title": item.get("title"),
            "path": item.get("path"),
            "subject": item.get("subject"),
            "attempts": int(item.get("attempts") or 0),
            "graded_attempts": graded,
            "accuracy": float(item.get("accuracy") or 0),
            "sample_limited": graded < MIN_WEAK_SAMPLE,
        })
    note = knowledge.get("note")
    limited = [item for item in weak if item["sample_limited"]]
    if limited:
        note = (
            "部分薄弱知识点的已判定样本少于 %s 次（如「%s」仅 %s 次），只展示真实样本数，"
            "样本补足前不做个性化推断。" % (
                MIN_WEAK_SAMPLE, limited[0]["title"], limited[0]["graded_attempts"])
        )
    return weak, note


def _review_payload(user_id: int) -> dict:
    """今日复习统一口径（M2）：读服务端四档复习卡，失败降级为旧错题口径。

    返回 ``{available, due_now, due_today, total, algorithm, algorithm_version,
    due_source, caliber, note}``；前端只在 available 时显示复习卡入口，不用本地估算。
    """
    try:
        from . import study_review

        stats = study_review.stats(user_id)
        return {
            "available": True,
            "due_now": int(stats.get("due_now") or 0),
            "due_today": int(stats.get("due_today") or 0),
            "total": int(stats.get("total") or 0),
            "algorithm": stats.get("algorithm"),
            "algorithm_version": stats.get("algorithm_version"),
            "due_source": "review-cards",
            "caliber": CALIBER["review_due"],
            "note": "",
        }
    except Exception as error:  # noqa: BLE001 - 复习排程不可用不能拖垮首页其他真实统计
        return {
            "available": False,
            "due_now": None,
            "due_today": None,
            "total": None,
            "algorithm": None,
            "algorithm_version": None,
            "due_source": "legacy",
            "caliber": CALIBER["due"],
            "note": (
                "复习排程暂不可用，首页沿用错题本到期时间；"
                f"（{type(error).__name__}）"
            ),
        }


def _personal_payload(user_id: int, days: int) -> dict:
    from . import insight  # 延迟导入：insight 在模块底部注册本模块的路由

    days = insight._clamp_days(days)
    data = insight._collect(user_id, days)
    summary = insight._attempt_summary(data["attempts"])
    now = insight._now_iso()
    active_wrongs = [item for item in data["wrongs"] if not item.get("archived")]
    due_reviews = sum(
        1 for item in active_wrongs if str(item.get("next_review_at") or "") <= now
    )
    try:
        knowledge = insight._knowledge_payload(data)
    except Exception:  # noqa: BLE001 - 知识点关联表异常时首页仍返回其他真实统计
        knowledge = {"items": [], "note": "知识点统计暂不可用", "coverage": {}}
    weak, weak_note = _weak_knowledge(knowledge)
    return {
        "window_days": days,
        "window_start": data["start"],
        "due_reviews": due_reviews,
        "wrong_count": len(active_wrongs),
        "attempts": summary["attempts"],
        "graded_attempts": summary["graded_attempts"],
        "correct_attempts": summary["correct_attempts"],
        "today": _today_payload(user_id),
        **_attempt_quality_payload(user_id),
        "library": _library_payload(user_id),
        "continue": _continue_payload(user_id),
        "weak_knowledge": weak,
        "weak_note": weak_note,
        "knowledge_coverage": knowledge.get("coverage") or {},
        "caliber": {
            "today": CALIBER["today"],
            "first_attempt": CALIBER["first_attempt"],
            "repeat": CALIBER["repeat"],
            "pending": CALIBER["pending"],
            "weak": CALIBER["weak"],
            "due": CALIBER["due"],
            "review_due": CALIBER["review_due"],
        },
    }


# ---------------------------------------------------------------------------
# 对外快照（路由注册在 app/insight.py，避免改 app/main.py）
# ---------------------------------------------------------------------------


def _official_caliber(user_id: int) -> dict:
    """平台精选口径（可见性查询时判定，撤回/争议不计入；与 practice.stats 同源）。"""
    try:
        from . import official as official_module

        return official_module.official_caliber(user_id)
    except Exception:  # noqa: BLE001 - 精选口径失败不能拖垮首页其他真实统计
        return {
            "available": False,
            "total_questions": 0,
            "practice_pool": 0,
            "auto_gradable": 0,
            "attempts": 0,
            "practiced": 0,
            "wrong_count": 0,
            "due_reviews": 0,
            "caliber": CALIBER["official"],
            "note": "平台精选统计暂不可用",
        }


def build_home(user_id: int, days: int = DEFAULT_DAYS) -> dict:
    """首页统计快照：题库全局口径 + 平台精选口径 + 当前账号个人口径 + 复习排程口径。"""
    personal = _personal_payload(user_id, days)
    official = _official_caliber(user_id)
    review = _review_payload(user_id)
    bank = dict(_bank_caliber())
    # 平台精选可练数量同时挂到 bank 块：首页题库卡片按这个字段展示（旧后端无该字段时
    # 前端能力探测降级，不伪造数字）。
    if official.get("available"):
        bank["official_practice_pool"] = official.get("practice_pool", 0)
        bank["official_auto_gradable"] = official.get("auto_gradable", 0)
    return {
        "generated_at": db.utcnow(),
        "range": {"days": personal["window_days"], "today": _today_start_iso()[:10], "timezone": "UTC"},
        "bank": bank,
        "official": official,
        "personal": personal,
        # M2：今日复习以服务端四档复习卡为准（与错题/练习/学情同一到期值）。
        "review": review,
        # 批次六（§7）：词汇本加法键；模块不可用时 available=false，旧字段不变。
        "vocab": vocab.home_summary(user_id),
        "caliber": CALIBER,
    }

