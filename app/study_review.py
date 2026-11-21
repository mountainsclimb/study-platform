"""四档复习卡与复习排程参考适配器（总方案 §4「复习卡及日志」、§5.2「排程/迁移」、§8 工单 E）。

本模块是所有复习入口（首页今日复习、练习插题、错题本、学情、学习计划）共用的
**服务器排程真相源**：每个用户 + 卡面键只保存一份排程状态，四档人工评分写幂等
复习日志，服务端返回下一次到期时间。M2 起旧 ``wrong_questions.next_review_at`` 作为
**只读兼容字段**被同一事务镜像（:func:`_mirror_due_to_wrong`）：已有真实评分的卡是权威，
普通判分/重判不再推进排程；尚未评分的卡跟随普通判分推进首次待复习。首页/错题/练习/
学情/Anki 导出因此看到同一个到期值，``compare_due`` 仍可作为漂移对账证据。

## 算法口径（本地参考实现 + 外部引擎适配位）

* 算法标识：``fsrs-reference``，版本 ``5.0-local``；题库/练习侧旧的
  ``app/review_schedule.py``（FSRS-4.5 Lite 二档 Again/Good）保持不变。
* 权重：:data:`FSRS_WEIGHTS` 为 **FSRS-5 公开默认 19 个权重**，来源
  https://github.com/open-spaced-repetition/awesome-fsrs/wiki/The-Algorithm#fsrs-5
  （与 ts-fsrs / fsrs-rs 的 ``FSRS5_DEFAULT_WEIGHTS`` 逐位一致）。
* 公式（与上面 Wiki 的 FSRS-5 公式逐条对齐）：
  - 遗忘曲线 ``R(t,S) = (1 + 19t/(81S))^(-1/2)``；请求保留率 0.9 时间隔
    ``I = S / FACTOR * (0.9^(1/DECAY) - 1) == S``，代码按通式实现，结果向上取整到天；
  - 初始稳定度 ``S0(G) = w[G-1]``；初始难度 ``D0(G) = w4 - e^(w5(G-1)) + 1``，夹在 [1,10]；
  - 难度更新采用 FSRS-5 的线性阻尼：``ΔD = -w6(G-3)``、``D* = D + ΔD(10-D)/9``，
    再按 ``w7`` 向 ``D0(4)`` 均值回归（FSRS-5 起目标由 ``D0(3)`` 改为 ``D0(4)``）；
  - 成功回忆 ``S'r = S(1 + e^w8 (11-D) S^-w9 (e^(w10(1-R))-1) H_G E_G)``，
    Hard 乘 ``w15``、Easy 乘 ``w16``；
  - 遗忘（Again）``S'f = w11 D^-w12 ((S+1)^w13 - 1) e^(w14(1-R))``；
  - FSRS-5 新增的同日更新 ``S' = S e^(w17(G-3+w18))``：本模块按「距上次评分不足 1 天」
    判定同日，满 1 天以后一律按上面的可提取性/记忆公式计算（应用是日粒度）。
* 本模块是**确定性的本地参考实现**：同输入同输出，不引入随机模糊（fuzz）；中间量保留
  8 位小数。外部官方 ts-fsrs 引擎经 :func:`_external_schedule` 适配位预留
  （环境变量 ``STUDY_REVIEW_ENGINE=ts-fsrs|external``；本环境未部署 Node 24 常驻进程，
  调用必抛 :class:`ReviewEngineUnavailable`，随后**降级为 200** 并保留原到期时间）。
* 状态机（日粒度适配，卡片状态取值 ``new/learning/review/relearning``）：
  - ``new`` 首次评分按初始公式；Again → ``learning``，其余 → ``review``；
  - ``learning``/``relearning`` 评分成功（2/3/4）→ ``review``；Again 保持学习态；
  - ``review`` 评 Again → ``relearning`` 且 ``lapses + 1``（与 FSRS 一致，只在
    review→Again 计一次遗忘）；其余评分为 ``review``；
  - ``reps`` 每次评分 +1；间隔天 = 稳定度按保留率换算后**向上取整**，最小 1 天、
    最大 :data:`MAX_INTERVAL_DAYS`（36500 天），与应用现有日粒度一致。
* 固定时钟：:func:`now_utc` 支持 ``STUDY_REVIEW_CLOCK``（ISO-8601），所有内部默认时间
  都走它，便于测试和验收复现；函数也均可显式传入 ``now``。

## 错误口径

* ``404``：复习卡不存在或不属于当前用户（不泄露存在性）；
* ``409``：``card_version`` 与当前版本不一致（卡片已被其他评分更新）；
* ``422``：评分不在 1..4、幂等键格式非法等参数错误；
* 排程引擎不可用**不返回 503**：按设计降级为 ``200 {degraded: true, retry_allowed: true}``，
  不改变卡状态与 due、不写评分日志，保证恢复后同一幂等键只应用一次。

## 存储与并发

* 表 ``review_cards`` / ``review_ratings`` 只在 :func:`init_study_review` 用
  ``executescript`` 幂等创建；行主键已在 ``app/db.py::_ID_TABLES`` 登记，SQLite/PostgreSQL
  都能用 ``cursor.lastrowid``。
* 写路径统一 ``db.write_lock(con, "study-review")`` 串行化；SQL 占位符 ``?``，JSON 列存
  TEXT，时间统一为带 ``+00:00`` 的 UTC ISO 字符串，因此 ``due_at`` 可直接按字符串比较排序。
* 卡状态 CAS：``UPDATE ... WHERE id=? AND user_id=? AND version=?``，``rowcount=0`` → 409。

## 迁移口径（本轮只迁移错题）

* ``sync_user_cards`` 把用户全部 ``wrong_questions`` 幂等迁成 ``card_type='wrong'``、
  ``source_kind='migrated'`` 的卡；``card_key = "q:" + storage_key``（题库保持裸 qid，
  app:/official: 带前缀），``question_ref`` 存规范化引用 ``bank:...`` / ``app:...``。
* **已有卡绝不覆盖真实排程状态**：唯一同步字段是归档标记（archived=1 ↔ 0）；只有
  **首次创建**才读取旧 ``next_review_at`` 作为 due（缺失/非法时用 now+1 天），状态为
  ``new``，等待一次真实四档评分才初始化新算法状态。
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, time as dt_time, timedelta, timezone
import json
import logging
import math
import os
import re
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from . import db, question_ref
from .auth import current_user

router = APIRouter(prefix="/api/review-cards", tags=["Review cards"])
logger = logging.getLogger("zhixu.study_review")

# ---------------------------------------------------------------------------
# 常量：算法、权重、日粒度与状态
# ---------------------------------------------------------------------------

FSRS_ALGORITHM = "fsrs-reference"
FSRS_ALGORITHM_VERSION = "5.0-local"
#: 目标保留率：到期时用户记得的概率。FSRS 在 r=0.9 时「间隔 = 稳定度（天）」。
REQUEST_RETENTION = 0.9
MAX_INTERVAL_DAYS = 36500
MIN_STABILITY = 0.01

#: FSRS-5 公开默认 19 个权重（w0..w18）。
#:
#: 来源：https://github.com/open-spaced-repetition/awesome-fsrs/wiki/The-Algorithm#fsrs-5
#: 与 ts-fsrs ``FSRS5_DEFAULT_WEIGHTS``、fsrs-rs 的 FSRS-5 默认参数一致；本模块只读使用，
#: 不做在线训练/优化。外部官方引擎可用 ``STUDY_REVIEW_ENGINE`` 切换到 ts-fsrs。
FSRS_WEIGHTS = (
    0.40255,  # w0  初始稳定度：Again
    1.18385,  # w1  初始稳定度：Hard
    3.17300,  # w2  初始稳定度：Good
    15.69105,  # w3  初始稳定度：Easy
    7.19490,  # w4  初始难度基准（D0(1)）
    0.53450,  # w5  初始难度指数
    1.46040,  # w6  难度变化率
    0.00460,  # w7  难度均值回归权重
    1.54575,  # w8  成功回忆稳定度增益指数
    0.11920,  # w9  难度对稳定度增益的衰减
    1.01925,  # w10 可提取性对稳定度增益的影响
    1.93950,  # w11 遗忘后稳定度基准
    0.11000,  # w12 难度对遗忘后稳定度的影响
    0.29605,  # w13 遗忘后稳定度随旧稳定度增长
    2.26980,  # w14 可提取性对遗忘后稳定度的影响
    0.23150,  # w15 Hard 惩罚
    2.98980,  # w16 Easy 奖励
    0.51655,  # w17 同日稳定度更新系数
    0.66210,  # w18 同日稳定度更新偏移
)

#: FSRS-4.5/5 固定遗忘曲线参数：``R(t,S) = (1 + FACTOR * t / S) ** DECAY``。
DECAY = -0.5
FACTOR = 19.0 / 81.0
S_MIN = MIN_STABILITY
S_MAX = float(MAX_INTERVAL_DAYS)
D_MIN = 1.0
D_MAX = 10.0

STATE_NEW = "new"
STATE_LEARNING = "learning"
STATE_REVIEW = "review"
STATE_RELEARNING = "relearning"
STATES = (STATE_NEW, STATE_LEARNING, STATE_REVIEW, STATE_RELEARNING)

CARD_TYPES = ("wrong", "question", "note", "artifact")
SOURCE_MIGRATED = "migrated"
SOURCE_KINDS = (SOURCE_MIGRATED, "attempt", "manual")

RATING_LABELS = {1: "重来", 2: "困难", 3: "良好", 4: "轻松"}
RATING_SCALE = [1, 2, 3, 4]

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
MAX_IDEMPOTENCY_KEY_LENGTH = 120
# 与 question_ref 的客户端幂等键字符集一致；生成键使用 ``srv-`` 前缀。
_IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9._:+-]+$")
# 双读对账容差：迁移是原值拷贝，1 秒以内视为一致（避开 ISO 解析/时区归一误差）。
MATCH_TOLERANCE_SECONDS = 1.0
_ENGINE_EXTERNAL_MODES = ("ts-fsrs", "external")


class ReviewEngineUnavailable(RuntimeError):
    """外部排程引擎（ts-fsrs 常驻进程）不可用。

    ``rate_card`` 捕获后按设计降级为 200，不改变卡状态/到期时间、不写评分日志。
    """


# ---------------------------------------------------------------------------
# 时间工具：固定时钟 + 统一 UTC
# ---------------------------------------------------------------------------


def _as_utc(value) -> datetime | None:
    """把库里/调用方的时间统一成带时区的 UTC；无法解析返回 ``None``。

    旧行可能是无时区 ISO（如 ``2026-01-01T00:00:00``）：一律视为 UTC，避免与 aware
    的 now 相减时抛 ``TypeError``。
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def now_utc() -> datetime:
    """当前 UTC 时间；``STUDY_REVIEW_CLOCK`` 为 ISO-8601 时使用固定时钟。

    固定时钟只影响本模块（复习卡排程/到期查询/评分日志），用于测试与验收复现；
    未设置或无法解析时回退到 :func:`app.db.utcnow` 同源的 ``datetime.now(timezone.utc)``。
    """
    fixed = os.environ.get("STUDY_REVIEW_CLOCK", "").strip()
    if fixed:
        parsed = _as_utc(fixed)
        if parsed is not None:
            return parsed
    # 与 db.utcnow() 同一时钟来源（db.utcnow 返回 UTC ISO 字符串）。
    return datetime.fromisoformat(db.utcnow())


def _iso(moment: datetime) -> str:
    return _as_utc(moment).isoformat()


def _normalize_now(value) -> datetime:
    """把可选的 now 参数统一成 UTC datetime；``None``/非法值走 :func:`now_utc`。"""
    parsed = _as_utc(value)
    return parsed if parsed is not None else now_utc()


def _as_float(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed):
        return None
    return parsed


def _as_int(value, default: int = 0) -> int:
    if value is None or isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _clamp(value: float, low: float, high: float) -> float:
    return min(high, max(low, value))


def _round8(value: float) -> float:
    """保留 8 位小数（与 ts-fsrs 的 ``roundTo(…, 8)`` 口径一致，便于对照）。"""
    if not math.isfinite(value):
        return value
    return round(value, 8)


# ---------------------------------------------------------------------------
# FSRS-5 纯函数公式（本地参考实现）
# ---------------------------------------------------------------------------


def _init_stability(rating: int) -> float:
    """初始稳定度 ``S0(G) = w[G-1]``（Again/Hard/Good/Easy 依次取 w0..w3）。"""
    return _clamp(FSRS_WEIGHTS[rating - 1], S_MIN, S_MAX)


def _init_difficulty(rating: int) -> float:
    """初始难度 ``D0(G) = w4 - e^(w5(G-1)) + 1``，夹在 [1, 10]。"""
    raw = FSRS_WEIGHTS[4] - math.exp(FSRS_WEIGHTS[5] * (rating - 1)) + 1.0
    return _clamp(_round8(raw), D_MIN, D_MAX)


def _mean_reversion(initial: float, current: float) -> float:
    """难度均值回归：``w7 * initial + (1 - w7) * current``。"""
    weight = FSRS_WEIGHTS[7]
    return _round8(weight * initial + (1.0 - weight) * current)


def _next_difficulty(difficulty: float, rating: int) -> float:
    """FSRS-5 难度更新：线性阻尼 + 向 ``D0(4)`` 均值回归。"""
    delta = -FSRS_WEIGHTS[6] * (rating - 3)
    damped = difficulty + delta * (D_MAX - difficulty) / 9.0
    target = _init_difficulty(4)
    return _clamp(_round8(_mean_reversion(target, damped)), D_MIN, D_MAX)


def forgetting_curve(elapsed_days: float, stability: float) -> float:
    """可提取性 ``R(t,S) = (1 + 19t/(81S))^(-1/2)``（t 以天计）。"""
    stable = max(float(stability), S_MIN)
    elapsed = max(0.0, float(elapsed_days))
    return _round8((1.0 + FACTOR * elapsed / stable) ** DECAY)


def _next_recall_stability(difficulty: float, stability: float, retrievability: float, rating: int) -> float:
    """成功回忆（Hard/Good/Easy）后的稳定度。"""
    hard_penalty = FSRS_WEIGHTS[15] if rating == 2 else 1.0
    easy_bonus = FSRS_WEIGHTS[16] if rating == 4 else 1.0
    increase = (
        math.exp(FSRS_WEIGHTS[8])
        * (11.0 - difficulty)
        * stability ** (-FSRS_WEIGHTS[9])
        * (math.exp((1.0 - retrievability) * FSRS_WEIGHTS[10]) - 1.0)
        * hard_penalty
        * easy_bonus
    )
    return max(S_MIN, stability * (1.0 + increase))


def _next_forget_stability(difficulty: float, stability: float, retrievability: float) -> float:
    """遗忘（Again）后的稳定度。"""
    forgot = (
        FSRS_WEIGHTS[11]
        * difficulty ** (-FSRS_WEIGHTS[12])
        * ((stability + 1.0) ** FSRS_WEIGHTS[13] - 1.0)
        * math.exp((1.0 - retrievability) * FSRS_WEIGHTS[14])
    )
    return max(S_MIN, forgot)


def _next_short_term_stability(stability: float, rating: int) -> float:
    """FSRS-5 同日更新：``S' = S * e^(w17(G-3+w18))``。"""
    raw = stability * math.exp(FSRS_WEIGHTS[17] * (rating - 3 + FSRS_WEIGHTS[18]))
    return _clamp(raw, S_MIN, S_MAX)


def _next_interval_days(stability: float) -> int:
    """把稳定度换算成天粒度间隔：按请求保留率求 t，向上取整，限制在 [1, 36500]。"""
    # I(r, S) = S / FACTOR * (r^(1/DECAY) - 1)；r=0.9 时恰好等于 S。
    raw = stability / FACTOR * (REQUEST_RETENTION ** (1.0 / DECAY) - 1.0)
    # 先按 9 位小数消除浮点尾差再加向上取整，避免 S=3 被算成 4 天。
    days = math.ceil(round(raw, 9))
    return max(1, min(MAX_INTERVAL_DAYS, int(days)))


def _validate_rating(rating) -> int:
    """评分必须是 1..4 的整数；非法时抛 422（中文口径）。"""
    if isinstance(rating, bool) or not isinstance(rating, int):
        raise HTTPException(422, "评分必须是 1 到 4 的整数（1=重来，2=困难，3=良好，4=轻松）")
    if rating < 1 or rating > 4:
        raise HTTPException(422, "评分必须是 1 到 4 的整数（1=重来，2=困难，3=良好，4=轻松）")
    return rating


# ---------------------------------------------------------------------------
# 纯函数排程：schedule()
# ---------------------------------------------------------------------------


def schedule(card_state: dict, rating: int, now: datetime) -> dict:
    """对一张卡应用一次四档评分，返回新的 FSRS-5 排程状态（纯函数，不碰数据库）。

    :param card_state: 卡状态，字段
        ``state``（new/learning/review/relearning）、``stability``、``difficulty``、
        ``reps``、``lapses``、``last_rating``、``last_review``（ISO）、``due``（ISO）。
    :param rating: 1=Again 2=Hard 3=Good 4=Easy。
    :param now: 评分时刻（服务端时钟，必须带时区或按 UTC 解释）。
    :return: ``state/stability/difficulty/reps/lapses/state/last_rating/last_review/due/
        interval_days``。间隔按天向上取整，最小 1 天、最大 36500 天。
    :raises HTTPException: 422，评分不在 1..4 或 now 非法。
    """
    rating_value = _validate_rating(rating)
    moment = _as_utc(now)
    if moment is None:
        raise HTTPException(422, "评分时刻 now 必须是合法的 ISO-8601 时间")

    state = str((card_state or {}).get("state") or STATE_NEW).strip().lower()
    if state not in STATES:
        state = STATE_NEW
    stability = _as_float((card_state or {}).get("stability"))
    difficulty = _as_float((card_state or {}).get("difficulty"))
    reps = max(0, _as_int((card_state or {}).get("reps"), 0)) + 1
    lapses = max(0, _as_int((card_state or {}).get("lapses"), 0))
    last_review = _as_utc((card_state or {}).get("last_review"))

    if state == STATE_NEW or stability is None or difficulty is None:
        # 首次真实评分：初始化 FSRS-5 记忆状态；Earlier 的旧到期只是「首次待复习」。
        stability = _init_stability(rating_value)
        difficulty = _init_difficulty(rating_value)
        next_state = STATE_LEARNING if rating_value == 1 else STATE_REVIEW
    else:
        elapsed_days = 0.0
        if last_review is not None:
            elapsed_days = max(0.0, (moment - last_review).total_seconds() / 86400.0)
        if elapsed_days < 1.0:
            # 同日（不足 1 天）重复评分：FSRS-5 的同日稳定度更新。
            stability = _next_short_term_stability(stability, rating_value)
        else:
            retrievability = forgetting_curve(elapsed_days, stability)
            if rating_value == 1:
                stability = _next_forget_stability(difficulty, stability, retrievability)
            else:
                stability = _next_recall_stability(
                    difficulty, stability, retrievability, rating_value
                )
        difficulty = _next_difficulty(difficulty, rating_value)
        if rating_value == 1:
            if state == STATE_REVIEW:
                # 与 FSRS 一致：只有 review → Again 计一次遗忘。
                lapses += 1
                next_state = STATE_RELEARNING
            elif state == STATE_LEARNING:
                next_state = STATE_LEARNING
            else:
                next_state = STATE_RELEARNING
        else:
            next_state = STATE_REVIEW

    stability = _clamp(_round8(stability), S_MIN, S_MAX)
    difficulty = _clamp(_round8(difficulty), D_MIN, D_MAX)
    interval_days = _next_interval_days(stability)
    due_at = _iso(moment + timedelta(days=interval_days))
    return {
        "state": next_state,
        "stability": stability,
        "difficulty": difficulty,
        "reps": reps,
        "lapses": lapses,
        "last_rating": rating_value,
        "last_review": _iso(moment),
        "due": due_at,
        "interval_days": interval_days,
    }


# ---------------------------------------------------------------------------
# 表结构与初始化
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS review_cards (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    card_key TEXT NOT NULL,
    card_type TEXT NOT NULL,
    question_ref TEXT NOT NULL,
    state_json TEXT NOT NULL,
    due_at TEXT NOT NULL,
    last_rating INTEGER,
    reps INTEGER NOT NULL DEFAULT 0,
    lapses INTEGER NOT NULL DEFAULT 0,
    source_kind TEXT NOT NULL,
    algorithm TEXT NOT NULL,
    algorithm_version TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    archived INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(user_id, card_key)
);
CREATE INDEX IF NOT EXISTS review_cards_due ON review_cards(user_id, archived, due_at);
CREATE INDEX IF NOT EXISTS review_cards_ref ON review_cards(user_id, question_ref);

CREATE TABLE IF NOT EXISTS review_ratings (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    card_id INTEGER NOT NULL,
    rating INTEGER NOT NULL,
    idempotency_key TEXT NOT NULL,
    card_version INTEGER NOT NULL,
    rated_at TEXT NOT NULL,
    due_at TEXT NOT NULL,
    state_before_json TEXT,
    state_after_json TEXT,
    algorithm TEXT NOT NULL,
    algorithm_version TEXT NOT NULL,
    degraded INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE(user_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS review_ratings_card ON review_ratings(user_id, card_id, id);
"""

_CARD_COLUMNS = (
    "id, user_id, card_key, card_type, question_ref, state_json, due_at, last_rating, "
    "reps, lapses, source_kind, algorithm, algorithm_version, version, archived, "
    "created_at, updated_at"
)


def init_study_review() -> None:
    """幂等建表：由 ``app.main.lifespan`` 在启动时调用（与其它 init_* 一致）。

    只建本模块自己的两张表，不碰旧 ``wrong_questions``；SQLite 与 PostgreSQL 共用同一段
    ``executescript``（``_Postgres.executescript`` 会把 ``id INTEGER PRIMARY KEY`` 转成 identity）。
    """
    with db.connection() as con:
        if db.postgres_enabled():
            db.write_lock(con, "study-review")
        con.executescript(_SCHEMA)


# ---------------------------------------------------------------------------
# 连接、状态序列化与卡投影
# ---------------------------------------------------------------------------


@contextmanager
def _use(con):
    """读取用连接：未传入时自建（db.connection），传入时复用调用方事务。"""
    if con is not None:
        yield con
        return
    with db.connection() as owned:
        yield owned


@contextmanager
def _write(con):
    """写入用连接：未传入时自建并加 ``study-review`` 写锁；传入时由调用方管理事务。"""
    if con is not None:
        yield con
        return
    with db.connection() as owned:
        db.write_lock(owned, "study-review")
        yield owned


def _load_state(raw) -> dict:
    if isinstance(raw, dict):
        return dict(raw)
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _dump_state(state: dict) -> str:
    return json.dumps(state or {}, ensure_ascii=False, sort_keys=True)


def _state_from_row(row) -> dict:
    """由卡行构造 ``schedule()`` 可用的状态字典；列字段优先于 state_json。"""
    raw = _load_state(row["state_json"])
    state = str(raw.get("state") or STATE_NEW).strip().lower()
    if state not in STATES:
        state = STATE_NEW
    return {
        "state": state,
        "stability": _as_float(raw.get("stability")),
        "difficulty": _as_float(raw.get("difficulty")),
        "reps": max(0, _as_int(row["reps"], 0)),
        "lapses": max(0, _as_int(row["lapses"], 0)),
        "last_rating": _as_int(row["last_rating"], 0) if row["last_rating"] is not None else None,
        "last_review": raw.get("last_review") or None,
        "due": row["due_at"],
    }


def _card_public(row) -> dict:
    """卡的 API 投影（due_cards / get_card / 评分响应共用同一结构）。"""
    state = _state_from_row(row)
    version = _as_int(row["version"], 1) or 1
    card_key = str(row["card_key"] or "")
    storage_qid = card_key[2:] if card_key.startswith("q:") else card_key
    return {
        "card_id": row["id"],
        "card_key": card_key,
        "card_type": row["card_type"],
        "question_ref": row["question_ref"],
        "qid": storage_qid,
        "state": state["state"],
        "stability": state["stability"],
        "difficulty": state["difficulty"],
        "reps": state["reps"],
        "lapses": state["lapses"],
        "last_rating": state["last_rating"],
        "last_review": state["last_review"],
        "due_at": row["due_at"],
        "source_kind": row["source_kind"],
        "algorithm": row["algorithm"],
        "algorithm_version": row["algorithm_version"],
        "version": version,
        "card_version": version,
        "archived": bool(row["archived"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "rating_scale": list(RATING_SCALE),
        "rating_labels": {str(key): label for key, label in RATING_LABELS.items()},
    }


# ---------------------------------------------------------------------------
# 迁移：wrong_questions → review_cards（只读旧表，绝不覆盖已有真实排程）
# ---------------------------------------------------------------------------


def _card_key_for(ref) -> str:
    """卡面键：``q:<storage_key>``（题库裸 qid，app:/official: 带前缀）。"""
    return "q:" + question_ref.storage_key(ref)


def _canonical_ref(ref) -> str:
    return question_ref.normalize(ref)


def _legacy_due(value, moment: datetime) -> str:
    """旧 ``next_review_at`` → 新 due_at；缺失/非法时按 now+1 天（与应用错题默认一致）。"""
    parsed = _as_utc(value)
    if parsed is None:
        return _iso(moment + timedelta(days=1))
    return _iso(parsed)


def _due_interval_days(due_at: str, moment: datetime) -> int:
    """到期时间 → 旧表 ``interval_days``（至少 1 天，四舍五入到天）。"""
    parsed = _as_utc(due_at)
    if parsed is None:
        return 1
    delta = (parsed - moment).total_seconds() / 86400.0
    return max(1, int(round(delta)))


def _mirror_due_to_wrong(con, user_id: int, ref_value, *, due_at: str, moment: datetime) -> bool:
    """把复习卡的权威到期时间镜像到旧 ``wrong_questions.next_review_at``。

    M2「统一到期值」：错题本/复习队列/首页/学情/Anki 导出等旧读取方继续读
    ``wrong_questions``，因此服务端在同一事务内把卡到期写回旧表，保证所有入口看到
    同一个日期；返回是否命中旧行（卡对应的旧错题已删除时返回 False）。
    """
    try:
        storage_qid = question_ref.storage_key(ref_value)
    except question_ref.QuestionRefError:
        return False
    if not storage_qid:
        return False
    changed = con.execute(
        "UPDATE wrong_questions SET next_review_at=?, interval_days=? "
        "WHERE user_id=? AND qid=?",
        (due_at, _due_interval_days(due_at, moment), user_id, storage_qid),
    ).rowcount
    return bool(changed)


def _insert_card(
    con,
    *,
    user_id: int,
    card_key: str,
    card_type: str,
    ref: str,
    due_at: str,
    source_kind: str,
    archived: int,
    moment: datetime,
) -> dict:
    state = {
        "state": STATE_NEW,
        "stability": None,
        "difficulty": None,
        "reps": 0,
        "lapses": 0,
        "last_rating": None,
        "last_review": None,
        "due": due_at,
    }
    now_iso = _iso(moment)
    cursor = con.execute(
        "INSERT INTO review_cards(user_id, card_key, card_type, question_ref, state_json, "
        "due_at, last_rating, reps, lapses, source_kind, algorithm, algorithm_version, "
        "version, archived, created_at, updated_at) "
        "VALUES(?,?,?,?,?,?,NULL,0,0,?,?,?,1,?,?,?)",
        (
            user_id, card_key, card_type, ref, _dump_state(state), due_at,
            source_kind, FSRS_ALGORITHM, FSRS_ALGORITHM_VERSION, int(archived),
            now_iso, now_iso,
        ),
    )
    card_id = cursor.lastrowid
    row = con.execute(
        f"SELECT {_CARD_COLUMNS} FROM review_cards WHERE id=? AND user_id=?",
        (card_id, user_id),
    ).fetchone()
    return _card_public(row)


def _sync_one(con, user_id: int, wrong_row, moment: datetime) -> tuple[str, dict | None]:
    """同步一条错题记录，返回 (动作, 卡投影)；动作为 created/updated/archived/unchanged/skipped。

    到期值权威方向（M2「服务端一个到期值」）：

    * 卡尚未被真实四档评分（``reps=0``）：普通判分仍推进首次待复习 → **卡跟随旧表**到期；
    * 卡已有真实评分（``reps>0``）：卡是权威，普通判分/重判不再推进排程 → **旧表跟随卡**
      （同一事务镜像，错题本/首页/学情/Anki 导出读到同一天）；
    * 归档标记始终从旧表同步。
    """
    try:
        card_key = _card_key_for(wrong_row["qid"])
        ref = _canonical_ref(wrong_row["qid"])
    except question_ref.QuestionRefError:
        logger.warning("错题 qid 无法规范化，跳过迁移：%r", wrong_row["qid"])
        return "skipped", None
    archived = 1 if wrong_row["archived"] else 0
    existing = con.execute(
        f"SELECT {_CARD_COLUMNS} FROM review_cards WHERE user_id=? AND card_key=?",
        (user_id, card_key),
    ).fetchone()
    if existing is not None:
        reps = max(0, _as_int(existing["reps"], 0) or 0)
        archive_changed = (_as_int(existing["archived"], 0) or 0) != archived
        if archive_changed:
            con.execute(
                "UPDATE review_cards SET archived=?, updated_at=? WHERE id=? AND user_id=?",
                (archived, _iso(moment), existing["id"], user_id),
            )
        if archived:
            # 归档卡只同步归档标记，不参与到期对齐（恢复后按既有状态继续）。
            fresh = con.execute(
                f"SELECT {_CARD_COLUMNS} FROM review_cards WHERE id=? AND user_id=?",
                (existing["id"], user_id),
            ).fetchone()
            return ("archived" if archive_changed else "unchanged"), _card_public(fresh)
        changed = archive_changed
        if reps > 0:
            # 真实评分后的卡是权威：把旧表到期拉齐（普通判分不再覆盖排程）。
            _mirror_due_to_wrong(
                con, user_id, existing["question_ref"],
                due_at=existing["due_at"], moment=moment,
            )
        else:
            # 尚未真实评分的卡：首次待复习由普通判分推进，卡跟随旧表到期。
            legacy_due = _legacy_due(wrong_row["next_review_at"], moment)
            if legacy_due != existing["due_at"]:
                state = _load_state(existing["state_json"])
                state["due"] = legacy_due
                con.execute(
                    "UPDATE review_cards SET due_at=?, state_json=?, updated_at=? "
                    "WHERE id=? AND user_id=?",
                    (legacy_due, _dump_state(state), _iso(moment), existing["id"], user_id),
                )
                changed = True
        fresh = con.execute(
            f"SELECT {_CARD_COLUMNS} FROM review_cards WHERE id=? AND user_id=?",
            (existing["id"], user_id),
        ).fetchone()
        return ("updated" if changed else "unchanged"), _card_public(fresh)
    # 首次创建：迁移旧到期时间作为「首次待复习」，状态 new，等真实评分才初始化算法状态。
    card = _insert_card(
        con,
        user_id=user_id,
        card_key=card_key,
        card_type="wrong",
        ref=ref,
        due_at=_legacy_due(wrong_row["next_review_at"], moment),
        source_kind=SOURCE_MIGRATED,
        archived=archived,
        moment=moment,
    )
    return "created", card


def sync_wrong_question(user_id: int, qid: str, *, con=None) -> dict | None:
    """把一道错题同步成复习卡，返回卡的 API 投影；无对应错题且无卡时返回 ``None``。

    口径：

    * ``wrong_questions.archived=1`` → 对应卡 ``archived=1``（恢复时回到 0）；
    * 已有卡只同步归档标记，**不覆盖**已有真实排程状态与到期时间；
    * 首次创建才读取旧 ``next_review_at`` 作为 due（缺失/非法 → now+1 天），状态 ``new``；
    * ``qid`` 接受裸 qid / ``bank:`` / ``app:`` / ``official:`` 任一引用写法。
    """
    try:
        storage_qid = question_ref.storage_key(qid)
    except question_ref.QuestionRefError:
        return None
    moment = now_utc()
    with _write(con) as dbcon:
        wrong_row = dbcon.execute(
            "SELECT * FROM wrong_questions WHERE user_id=? AND qid=?",
            (user_id, storage_qid),
        ).fetchone()
        if wrong_row is None:
            existing = dbcon.execute(
                f"SELECT {_CARD_COLUMNS} FROM review_cards WHERE user_id=? AND card_key=?",
                (user_id, "q:" + storage_qid),
            ).fetchone()
            return _card_public(existing) if existing is not None else None
        _action, card = _sync_one(dbcon, user_id, wrong_row, moment)
        return card


def sync_user_cards(user_id: int, *, con=None) -> dict:
    """把该用户全部错题迁成复习卡（幂等，可重复调用）。

    :return: ``{created, updated, archived, total, unchanged, skipped, algorithm,
        algorithm_version, note}``。``updated`` 只统计归档标记发生变化（含恢复）的卡；
        ``archived`` 是本次处理中处于归档状态的错题数；``total`` 是处理行数。重复调用
        第二次 ``created=0``（已有卡状态保持不变）。
    """
    moment = now_utc()
    created = updated = unchanged = skipped = archived = 0
    with _write(con) as dbcon:
        rows = dbcon.execute(
            "SELECT * FROM wrong_questions WHERE user_id=? ORDER BY qid",
            (user_id,),
        ).fetchall()
        for wrong_row in rows:
            action, _card = _sync_one(dbcon, user_id, wrong_row, moment)
            if action == "created":
                created += 1
            elif action in ("updated", "archived"):
                updated += 1
            elif action == "unchanged":
                unchanged += 1
            else:
                skipped += 1
            if wrong_row["archived"]:
                archived += 1
    return {
        "created": created,
        "updated": updated,
        "archived": archived,
        "total": len(rows),
        "unchanged": unchanged,
        "skipped": skipped,
        "algorithm": FSRS_ALGORITHM,
        "algorithm_version": FSRS_ALGORITHM_VERSION,
        "note": (
            "错题迁移幂等：首次创建读取旧到期时间作为待复习，状态为 new；"
            "已有复习卡的排程状态与到期时间不会被覆盖，只同步归档标记。"
        ),
    }


def ensure_user_cards(user_id: int, *, con=None) -> dict:
    """惰性迁移：该用户还有错题没有对应复习卡时，自动迁移一次（幂等，逐条判定）。

    读取入口（到期队列/统计/对账/错题本覆盖、首页与学情）调用：保证旧用户、以及升级后
    新产生的错题第一次读取就有到期卡；卡与错题已一致时只做两次小查询，不写库。
    """
    with _use(con) as dbcon:
        wrong_qids = [
            str(row[0]) for row in dbcon.execute(
                "SELECT qid FROM wrong_questions WHERE user_id=? ORDER BY qid", (user_id,),
            )
        ]
        if not wrong_qids:
            return {"migrated": False, "created": 0, "total": 0,
                    "note": "没有错题，无需迁移"}
        card_keys = {
            str(row[0]) for row in dbcon.execute(
                "SELECT card_key FROM review_cards WHERE user_id=?", (user_id,),
            )
        }
    missing = 0
    for qid in wrong_qids:
        try:
            key = "q:" + question_ref.storage_key(qid)
        except question_ref.QuestionRefError:
            continue
        if key not in card_keys:
            missing += 1
    if not missing:
        return {"migrated": False, "created": 0, "total": len(wrong_qids),
                "note": "复习卡与错题已一致，无需迁移"}
    result = sync_user_cards(user_id, con=con)
    return {"migrated": bool(result.get("created")), **result}


def due_map(user_id: int, *, con=None) -> dict[str, dict]:
    """按写库 qid 批量返回复习卡到期投影（供旧读取方覆盖为同一个到期值）。

    返回 ``{qid: {due_at, card_id, state, version, rated, archived}}``；``qid`` 是
    ``wrong_questions.qid`` 的写库键（题库裸 qid、`app:<id>`、`official:<id>`），
    键与旧错题卡一一对应。读取前会做一次惰性迁移，保证旧用户也有一致的到期值。
    """
    ensure_user_cards(user_id, con=con)
    with _use(con) as dbcon:
        rows = dbcon.execute(
            f"SELECT {_CARD_COLUMNS} FROM review_cards WHERE user_id=?",
            (user_id,),
        ).fetchall()
    result: dict[str, dict] = {}
    for row in rows:
        try:
            storage_qid = question_ref.storage_key(row["question_ref"])
        except question_ref.QuestionRefError:
            continue
        card = _card_public(row)
        result[storage_qid] = {
            "due_at": card["due_at"],
            "card_id": card["card_id"],
            "state": card["state"],
            "version": card["version"],
            "rated": int(card.get("reps") or 0) > 0,
            "archived": bool(card.get("archived")),
        }
    return result


# ---------------------------------------------------------------------------
# 读取：卡、到期队列、统计、双读对账
# ---------------------------------------------------------------------------


def _card_id_value(card_id) -> int:
    """卡 id 必须是整数（路由层已由 FastAPI 转换，函数级调用同样给出 422 而不是 500）。"""
    if isinstance(card_id, bool) or not isinstance(card_id, int):
        raise HTTPException(422, "复习卡编号必须是整数")
    return card_id


def get_card(user_id: int, card_id: int, con=None) -> dict:
    """按 id 读取当前用户的复习卡（含归档卡）；不存在或无权 → 404。"""
    card_id_value = _card_id_value(card_id)
    with _use(con) as dbcon:
        row = dbcon.execute(
            f"SELECT {_CARD_COLUMNS} FROM review_cards WHERE id=? AND user_id=?",
            (card_id_value, user_id),
        ).fetchone()
    if row is None:
        raise HTTPException(404, "复习卡不存在")
    return _card_public(row)


def card_by_ref(user_id: int, ref, con=None) -> dict | None:
    """按题目引用读取当前用户的复习卡；引用非法或无卡返回 ``None``（不抛 404）。"""
    try:
        card_key = _card_key_for(ref)
    except question_ref.QuestionRefError:
        return None
    with _use(con) as dbcon:
        row = dbcon.execute(
            f"SELECT {_CARD_COLUMNS} FROM review_cards WHERE user_id=? AND card_key=?",
            (user_id, card_key),
        ).fetchone()
    return _card_public(row) if row is not None else None


def _overdue_days(due_at, moment: datetime) -> int:
    due = _as_utc(due_at)
    if due is None:
        return 0
    seconds = (moment - due).total_seconds()
    if seconds <= 0:
        return 0
    return int(seconds // 86400)


def _limit_value(limit) -> int:
    value = _as_int(limit, DEFAULT_LIMIT)
    return max(1, min(MAX_LIMIT, value))


def due_cards(
    user_id: int,
    *,
    now=None,
    limit: int = DEFAULT_LIMIT,
    include_future: bool = False,
    con=None,
) -> dict:
    """到期复习队列（按 due_at 升序、id 升序稳定排序）。

    :param now: 服务端时钟（ISO 字符串或 datetime）；默认走固定时钟。
    :param limit: 返回条数 1..200（默认 50）。
    :param include_future: ``True`` 时返回全部未归档卡（含未到期），仍按到期时间排序。
    :return: ``{items, total, now, message, limit, include_future}``；``total`` 是满足条件的
        总数（不受 ``limit`` 截断）；每项含
        ``card_id/card_key/card_type/question_ref/due_at/state/reps/lapses/
        rating_scale/overdue_days`` 及卡的完整投影。
    """
    moment = _normalize_now(now)
    moment_iso = _iso(moment)
    limit_value = _limit_value(limit)
    ensure_user_cards(user_id, con=con)
    clauses = ["user_id=?", "archived=0"]
    params: list = [user_id]
    if not include_future:
        clauses.append("due_at<=?")
        params.append(moment_iso)
    where = " AND ".join(clauses)
    with _use(con) as dbcon:
        total = dbcon.execute(
            f"SELECT COUNT(*) FROM review_cards WHERE {where}", tuple(params)
        ).fetchone()[0]
        rows = dbcon.execute(
            f"SELECT {_CARD_COLUMNS} FROM review_cards WHERE {where} "
            "ORDER BY due_at ASC, id ASC LIMIT ?",
            (*params, limit_value),
        ).fetchall()
    items = []
    for row in rows:
        card = _card_public(row)
        card["overdue_days"] = _overdue_days(row["due_at"], moment)
        items.append(card)
    if include_future:
        message = "返回全部未归档复习卡（包含未到期），按到期时间升序；到期时间以服务端 due_at 为准。"
    elif not items:
        message = "当前没有到期的复习卡。"
    else:
        message = (
            f"共 {int(total)} 张到期复习卡，按到期时间升序；评分后以服务端返回的 "
            "due_at 为唯一下一次到期时间。"
        )
    return {
        "items": items,
        "total": int(total),
        "now": moment_iso,
        "message": message,
        "limit": limit_value,
        "include_future": bool(include_future),
        "due_source": "review-cards",
        "note": "到期时间由服务端复习卡排程写入，并镜像到旧错题到期字段；首页/错题/练习/学情同一日期。",
    }


def stats(user_id: int, *, now=None) -> dict:
    """复习卡统计：到期数 + 状态分布 + 归档数。

    口径：``due_now`` = 未归档且 ``due_at <= now``；``due_today`` = 未归档且在本 UTC 自然日
    结束前到期（含已逾期）；``new/learning/review/relearning`` 按未归档卡的当前状态计数；
    ``total`` 含归档卡，``archived`` 为归档数。
    """
    moment = _normalize_now(now)
    moment_iso = _iso(moment)
    tomorrow_start = datetime.combine(
        moment.date() + timedelta(days=1), dt_time.min, tzinfo=timezone.utc
    ).isoformat()
    ensure_user_cards(user_id)
    counts = {state: 0 for state in STATES}
    total = archived = due_now = due_today = 0
    with db.connection() as dbcon:
        rows = dbcon.execute(
            "SELECT state_json, due_at, archived FROM review_cards WHERE user_id=?",
            (user_id,),
        ).fetchall()
    for row in rows:
        total += 1
        if _as_int(row["archived"], 0):
            archived += 1
            continue
        state = str(_load_state(row["state_json"]).get("state") or STATE_NEW).lower()
        if state not in counts:
            state = STATE_NEW
        counts[state] += 1
        due_at = str(row["due_at"] or "")
        if due_at and due_at <= moment_iso:
            due_now += 1
        if due_at and due_at < tomorrow_start:
            due_today += 1
    return {
        "due_today": due_today,
        "due_now": due_now,
        "new": counts[STATE_NEW],
        "learning": counts[STATE_LEARNING],
        "review": counts[STATE_REVIEW],
        "relearning": counts[STATE_RELEARNING],
        "total": total,
        "archived": archived,
        "algorithm": FSRS_ALGORITHM,
        "algorithm_version": FSRS_ALGORITHM_VERSION,
        "due_source": "review-cards",
        "legacy_mirror": True,
    }


def compare_due(user_id: int, *, now=None, con=None) -> dict:
    """旧错题排程与新复习卡的双读对账（不改变已有排程，必要时先做幂等惰性迁移）。

    逐条比较「未归档错题的 ``next_review_at``」与「对应未归档卡的 ``due_at``」：

    * ``matched``：两者可解析且相差不超过 :data:`MATCH_TOLERANCE_SECONDS` 秒；
    * ``mismatched``：可解析但差异超容差（``diff_hours = 新 due − 旧 due``，小时保留 2 位），
      或任一侧无法解析（``diff_hours=None``）；
    * ``missing_cards``：活跃错题没有对应复习卡（尚未迁移）；
    * ``extra_cards``：没有对应活跃错题的未归档卡（迁移后新增/错题已删除）。

    :return: ``{checked, matched, mismatched, missing_cards, extra_cards, note}``。
    """
    _normalize_now(now)  # 保持与其它读接口一致的时钟校验/固定时钟语义（本函数不依赖 now）
    ensure_user_cards(user_id, con=con)
    with _use(con) as dbcon:
        wrong_rows = dbcon.execute(
            "SELECT qid, next_review_at FROM wrong_questions "
            "WHERE user_id=? AND archived=0 ORDER BY qid",
            (user_id,),
        ).fetchall()
        card_rows = dbcon.execute(
            "SELECT card_key, due_at FROM review_cards WHERE user_id=? AND archived=0",
            (user_id,),
        ).fetchall()
    cards = {str(row["card_key"]): str(row["due_at"] or "") for row in card_rows}
    matched = 0
    mismatched: list[dict] = []
    missing: list[str] = []
    matched_keys: set[str] = set()
    for wrong_row in wrong_rows:
        qid = str(wrong_row["qid"] or "")
        try:
            card_key = _card_key_for(qid)
        except question_ref.QuestionRefError:
            missing.append(qid)
            continue
        new_due = cards.get(card_key)
        if new_due is None:
            missing.append(qid)
            continue
        matched_keys.add(card_key)
        old_due_raw = str(wrong_row["next_review_at"] or "")
        old_due = _as_utc(old_due_raw)
        new_due_dt = _as_utc(new_due)
        if old_due is None or new_due_dt is None:
            mismatched.append(
                {"qid": qid, "old_due": old_due_raw, "new_due": new_due, "diff_hours": None}
            )
            continue
        diff_seconds = (new_due_dt - old_due).total_seconds()
        if abs(diff_seconds) <= MATCH_TOLERANCE_SECONDS:
            matched += 1
        else:
            mismatched.append(
                {
                    "qid": qid,
                    "old_due": old_due_raw,
                    "new_due": new_due,
                    "diff_hours": round(diff_seconds / 3600.0, 2),
                }
            )
    extra = sorted(key for key in cards if key not in matched_keys)
    return {
        "checked": len(wrong_rows),
        "matched": matched,
        "mismatched": mismatched,
        "missing_cards": missing,
        "extra_cards": extra,
        "note": (
            "双读对账：旧 wrong_questions.next_review_at 与新 review_cards.due_at "
            "按未归档错题逐条比较，容差 1 秒；diff_hours = 新 due − 旧 due。"
            "对账前会做一次幂等惰性迁移（旧用户首次读取自动建卡），"
            "但不会改变已有卡的真实排程或到期值；差异仅作为切换/漂移证据。"
        ),
    }


# ---------------------------------------------------------------------------
# 评分：幂等键 + 版本 CAS + 外部引擎降级
# ---------------------------------------------------------------------------


def _engine_mode() -> str:
    return os.environ.get("STUDY_REVIEW_ENGINE", "").strip().lower()


def _external_schedule(card_state: dict, rating: int, now: datetime) -> dict:
    """外部 ts-fsrs 常驻进程适配位（方案 §5.2：固定版本、事务外计算）。

    正式部署应由 Node.js 24 常驻进程接收「服务端时钟 + 卡状态 + 评分」，返回 FSRS-5
    新状态；Python 在事务外调用，最后按卡版本原子提交卡状态与日志。**本环境未部署该
    进程**，因此函数固定抛 :class:`ReviewEngineUnavailable`，由 :func:`rate_card` 降级。

    接入时保持签名不变：返回 ``schedule()`` 同结构 dict（至少包含 ``due``）。
    """
    raise ReviewEngineUnavailable("ts-fsrs 排程引擎未部署，无法计算下一次到期时间")


def _degraded_result(card_row, *, rating: int, idempotency_key: str, engine: str) -> dict:
    """引擎不可用时的降级响应：200、原 due 不变、不写评分日志。"""
    card = _card_public(card_row)
    return {
        "card": card,
        "rating": rating,
        "due_at": card["due_at"],
        "replay": False,
        "replayed": False,
        "degraded": True,
        "retry_allowed": True,
        "engine": engine,
        "algorithm": FSRS_ALGORITHM,
        "algorithm_version": FSRS_ALGORITHM_VERSION,
        "idempotency_key": idempotency_key,
        "note": "排程引擎暂不可用，已保留最近一次有效到期时间，请稍后重试",
    }


def _idempotency_key(value) -> str:
    """校验或生成幂等键：≤120 字符、``[A-Za-z0-9._:+-]``；缺省由服务端生成。"""
    if value is None:
        return "srv-" + uuid.uuid4().hex
    text = str(value).strip()
    if not text:
        return "srv-" + uuid.uuid4().hex
    if len(text) > MAX_IDEMPOTENCY_KEY_LENGTH or not _IDEMPOTENCY_KEY_RE.match(text):
        raise HTTPException(
            422,
            "幂等键只能包含字母、数字与 . _ : + -，且长度不超过 120 个字符",
        )
    return text


def _replayed_result(con, *, user_id: int, key: str, card_id: int) -> dict | None:
    """同一 ``(user_id, idempotency_key)`` 已记录时返回原结果（不写第二行）。"""
    recorded = con.execute(
        "SELECT * FROM review_ratings WHERE user_id=? AND idempotency_key=?",
        (user_id, key),
    ).fetchone()
    if recorded is None:
        return None
    recorded_card_id = _as_int(recorded["card_id"], 0) or card_id
    current = con.execute(
        f"SELECT {_CARD_COLUMNS} FROM review_cards WHERE id=? AND user_id=?",
        (recorded_card_id, user_id),
    ).fetchone()
    return {
        "card": _card_public(current) if current is not None else None,
        "card_id": recorded_card_id,
        "rating": int(recorded["rating"]),
        "due_at": recorded["due_at"],
        "replay": True,
        "replayed": True,
        "degraded": bool(recorded["degraded"]),
        "retry_allowed": False,
        "algorithm": recorded["algorithm"],
        "algorithm_version": recorded["algorithm_version"],
        "idempotency_key": key,
        "note": "该评分已应用（幂等重放），未重复写入复习日志。",
    }


def _external_after_state(result, before_state: dict, rating: int, moment: datetime) -> dict:
    """把外部引擎返回值规整成 :func:`schedule` 输出结构（对接时保持协议稳定）。"""
    if not isinstance(result, dict) or not result.get("due"):
        raise ReviewEngineUnavailable("外部引擎返回格式非法")
    after = dict(before_state)
    for field in ("state", "stability", "difficulty", "reps", "lapses", "last_rating", "last_review", "due"):
        if field in result:
            after[field] = result[field]
    after["state"] = str(after.get("state") or STATE_REVIEW).lower()
    if after["state"] not in STATES:
        after["state"] = STATE_REVIEW
    after["reps"] = max(0, _as_int(after.get("reps"), 0))
    after["lapses"] = max(0, _as_int(after.get("lapses"), 0))
    after["last_rating"] = rating
    after["last_review"] = _iso(moment)
    if _as_utc(after.get("due")) is None:
        raise ReviewEngineUnavailable("外部引擎返回的到期时间非法")
    due_moment = _as_utc(after["due"])
    after["due"] = _iso(due_moment)
    interval = max(0.0, (due_moment - moment).total_seconds() / 86400.0)
    after["interval_days"] = max(1, min(MAX_INTERVAL_DAYS, int(math.ceil(interval))))
    return after


def _apply_rating(con, *, user_id: int, card_id: int, rating: int, card_version, key: str, moment: datetime) -> dict:
    """在已加写锁的事务内执行评分：重放 → 版本校验 → 引擎/本地计算 → CAS → 日志。"""
    card_row = con.execute(
        f"SELECT {_CARD_COLUMNS} FROM review_cards WHERE id=? AND user_id=?",
        (card_id, user_id),
    ).fetchone()
    if card_row is None:
        raise HTTPException(404, "复习卡不存在")

    replayed = _replayed_result(con, user_id=user_id, key=key, card_id=card_id)
    if replayed is not None:
        return replayed

    current_version = _as_int(card_row["version"], 1) or 1
    if card_version is not None:
        if isinstance(card_version, bool) or not isinstance(card_version, int):
            raise HTTPException(422, "卡片版本必须是整数")
        if int(card_version) != current_version:
            raise HTTPException(409, "复习卡已被更新，请刷新后重试")

    before_state = _state_from_row(card_row)
    engine = _engine_mode()
    if engine in _ENGINE_EXTERNAL_MODES:
        try:
            external = _external_schedule(before_state, rating, moment)
            after_state = _external_after_state(external, before_state, rating, moment)
        except Exception as exc:  # noqa: BLE001 - 引擎超时/协议错误一律降级，不能 500
            logger.warning("复习排程引擎不可用，降级保留原到期时间：%s", exc)
            return _degraded_result(
                card_row, rating=rating, idempotency_key=key, engine=engine
            )
    else:
        after_state = schedule(before_state, rating, moment)

    due_at = str(after_state["due"])
    version_after = current_version + 1
    now_iso = _iso(moment)
    cursor = con.execute(
        "UPDATE review_cards SET state_json=?, due_at=?, last_rating=?, reps=?, lapses=?, "
        "algorithm=?, algorithm_version=?, version=?, updated_at=? "
        "WHERE id=? AND user_id=? AND version=?",
        (
            _dump_state(after_state),
            due_at,
            rating,
            max(0, _as_int(after_state.get("reps"), 0)),
            max(0, _as_int(after_state.get("lapses"), 0)),
            FSRS_ALGORITHM,
            FSRS_ALGORITHM_VERSION,
            version_after,
            now_iso,
            card_id,
            user_id,
            current_version,
        ),
    )
    if not getattr(cursor, "rowcount", 1):
        raise HTTPException(409, "复习卡已被更新，请刷新后重试")
    # M2：同一事务把权威到期镜像到旧错题表，首页/错题/练习/学情/Anki 导出看到同一天。
    _mirror_due_to_wrong(
        con, user_id, card_row["question_ref"], due_at=due_at, moment=moment,
    )
    con.execute(
        "INSERT INTO review_ratings(user_id, card_id, rating, idempotency_key, card_version, "
        "rated_at, due_at, state_before_json, state_after_json, algorithm, algorithm_version, "
        "degraded, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,0,?)",
        (
            user_id,
            card_id,
            rating,
            key,
            current_version,
            now_iso,
            due_at,
            _dump_state(before_state),
            _dump_state(after_state),
            FSRS_ALGORITHM,
            FSRS_ALGORITHM_VERSION,
            now_iso,
        ),
    )
    updated = con.execute(
        f"SELECT {_CARD_COLUMNS} FROM review_cards WHERE id=? AND user_id=?",
        (card_id, user_id),
    ).fetchone()
    return {
        "card": _card_public(updated),
        "rating": rating,
        "due_at": due_at,
        "replay": False,
        "replayed": False,
        "degraded": False,
        "retry_allowed": False,
        "algorithm": FSRS_ALGORITHM,
        "algorithm_version": FSRS_ALGORITHM_VERSION,
        "idempotency_key": key,
        "note": "评分已应用，下一次到期时间以服务端返回的 due_at 为准。",
    }


def rate_card(
    user_id: int,
    card_id: int,
    *,
    rating: int,
    card_version: int | None = None,
    idempotency_key: str | None = None,
    now=None,
    con=None,
) -> dict:
    """对一张复习卡应用四档评分，返回新卡状态与服务端到期时间。

    顺序与口径：

    1. 参数校验：评分必须 1..4（422）、幂等键格式（422）；
    2. 卡不存在/不属于当前用户 → 404；
    3. 同一 ``(user_id, idempotency_key)`` 已记录 → 直接返回原结果
       （``replayed=true``，不改状态、不再写第二行；重放优先于版本校验）；
    4. 传入 ``card_version`` 且与当前版本不一致 → 409「复习卡已被更新，请刷新后重试」；
    5. ``STUDY_REVIEW_ENGINE`` 为 ``ts-fsrs``/``external`` 时调用外部引擎；不可用则
       **降级 200**：``{degraded: true, retry_allowed: true, due_at: 原 due, card: 原卡}``，
       不写评分日志（恢复后同幂等键重试只应用一次）；
    6. 正常路径：``schedule()`` 计算 → CAS 更新卡（version+1）→ 写 ``review_ratings``。

    ``idempotency_key`` 建议客户端必传（≤120，字符集 ``[A-Za-z0-9._:+-]``）；缺省时
    服务端生成 ``srv-<uuid>`` 并写入唯一行，弱网重试请复用同一 key。
    """
    rating_value = _validate_rating(rating)
    key = _idempotency_key(idempotency_key)
    moment = _normalize_now(now)
    card_id_value = _card_id_value(card_id)
    with _write(con) as dbcon:
        return _apply_rating(
            dbcon,
            user_id=user_id,
            card_id=card_id_value,
            rating=rating_value,
            card_version=card_version,
            key=key,
            moment=moment,
        )


# ---------------------------------------------------------------------------
# HTTP 路由：静态路径必须在 /{card_id} 之前注册
# ---------------------------------------------------------------------------


class RatingIn(BaseModel):
    """评分请求体：评分 + 可选卡片版本 + 可选幂等键。"""

    rating: int
    card_version: int | None = None
    idempotency_key: str | None = Field(default=None, max_length=MAX_IDEMPOTENCY_KEY_LENGTH)


@router.get("/due")
def api_due_cards(
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    include_future: bool = Query(False),
    user=Depends(current_user),
):
    """到期复习卡（服务端唯一到期时间；按 due_at 升序稳定排序）。"""
    return due_cards(user["id"], limit=limit, include_future=include_future)


@router.get("/compare")
def api_compare_due(user=Depends(current_user)):
    """新旧排程双读对账（只读证据，切换既有入口前使用）。"""
    return compare_due(user["id"])


@router.get("/stats")
def api_stats(user=Depends(current_user)):
    """复习卡统计：到期数、状态分布、总数与归档数。"""
    return stats(user["id"])


@router.post("/sync")
def api_sync(user=Depends(current_user)):
    """把当前用户的错题幂等迁移成复习卡；重复调用 created=0。"""
    return sync_user_cards(user["id"])


@router.get("/{card_id}")
def api_get_card(card_id: int, user=Depends(current_user)):
    """读取单张复习卡（含归档卡；他人卡 404）。"""
    return {"card": get_card(user["id"], card_id)}


@router.post("/{card_id}/ratings")
def api_rate_card(card_id: int, payload: RatingIn, user=Depends(current_user)):
    """四档评分：1=重来 2=困难 3=良好 4=轻松；返回服务端提交的唯一下次到期时间。"""
    return rate_card(
        user["id"],
        card_id,
        rating=payload.rating,
        card_version=payload.card_version,
        idempotency_key=payload.idempotency_key,
    )


__all__ = [
    "CARD_TYPES",
    "DEFAULT_LIMIT",
    "FSRS_ALGORITHM",
    "FSRS_ALGORITHM_VERSION",
    "FSRS_WEIGHTS",
    "MAX_INTERVAL_DAYS",
    "MAX_LIMIT",
    "RATING_LABELS",
    "RATING_SCALE",
    "REQUEST_RETENTION",
    "ReviewEngineUnavailable",
    "SOURCE_KINDS",
    "STATES",
    "card_by_ref",
    "compare_due",
    "due_cards",
    "forgetting_curve",
    "get_card",
    "init_study_review",
    "now_utc",
    "rate_card",
    "router",
    "schedule",
    "stats",
    "sync_user_cards",
    "sync_wrong_question",
]
