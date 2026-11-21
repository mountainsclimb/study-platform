"""广场抽检门禁：签发前独立抽检校验、模式配置与审计留痕。

方案依据：

* 方案 §8 阶段 3「试点首批 100% 独立抽检」——试点期内每道正式发布题在签发前
  必须有独立编辑给出的通过（``pass``）抽检记录，严重问题停增并纠正；
* 任务 WS28——默认 ``initial_100`` 试点：``always`` 所有发布版本都要 pass；
  ``off`` 不阻断（只保留记录）；编辑可显式 override（带理由）并记录审计；
  门禁默认值要兼顾试点：不阻断历史已签发内容，只对新的签发生效。

模式解析（优先级从高到低）：

1. 管理员运行期设置（``app_settings.plaza_quality_gate``，经管理接口修改后立即生效）；
2. 环境变量 ``STUDY_PLAZA_QUALITY_GATE``（部署级固定值）；
3. 内置默认 ``initial_100``。

「试点激活（armed）」判定：显式配置了模式（管理员设置或环境变量）时从首次签发
起强制；未显式配置时，只要库中已有任意抽检记录（说明质量运营已开始），即视为
试点已激活——这样生产试点显式配置后立即生效，而尚未开始抽检的环境与历史已签发
内容不会被门禁追溯阻断。

``initial_100`` 的试点窗口按「成功签发数」（``plaza_releases`` 行数）计数，前
``pilot_limit``（默认 100，``0`` 表示不设上限）次签发要求每个版本 pass；窗口
结束后不再阻断（稳定期可切换常规抽检）。计数只用于门禁判定，不改变抽检记录。
"""
from __future__ import annotations

import os

from ..db import connection, utcnow, write_lock
from . import db

#: 门禁模式（只追加）：off=不阻断；initial_100=试点首批 100% 抽检；always=全部版本。
GATE_MODES = ("off", "initial_100", "always")
GATE_MODE_LABELS = {
    "off": "关闭（抽检只记录、不阻断签发）",
    "initial_100": "试点首批 100% 抽检（默认）",
    "always": "全部发布版本强制抽检",
}
DEFAULT_GATE_MODE = "initial_100"
DEFAULT_PILOT_LIMIT = 100
#: 门禁审计结果：blocked=阻断；override=编辑显式放行；passed=通过门禁。
GATE_OUTCOMES = ("blocked", "override", "passed")

MODE_SETTING_KEY = "plaza_quality_gate"
ARMED_SETTING_KEY = "plaza_quality_gate_armed"
LIMIT_SETTING_KEY = "plaza_quality_gate_limit"
ENV_MODE = "STUDY_PLAZA_QUALITY_GATE"
ENV_ARMED = "STUDY_PLAZA_QUALITY_GATE_ARMED"
ENV_LIMIT = "STUDY_PLAZA_QUALITY_GATE_LIMIT"

#: 门禁阻断后返回给发布端的操作指引（前端/编辑台直接展示）。
OVERRIDE_GUIDANCE = (
    "该版本尚无通过（pass）的独立抽检记录，抽检门禁已阻断本次签发。"
    "请先在编辑台完成抽检（POST /api/plaza/admin/quality/checks，decision=pass）"
    "后重新签发；如属紧急情况，可由编辑在发布请求中显式 override"
    "（quality_gate_override=true 并填写 override_reason），该操作会写入审计事件与指标。"
)


class QualityGateBlocked(Exception):
    """签发被抽检门禁阻断（发布端捕获后写 blocked 审计并返回 409）。"""

    def __init__(self, submission_id: int, version: int, info: dict,
                 actor_id: int | None = None, reason: str = ""):
        self.submission_id = int(submission_id)
        self.version = int(version)
        self.info = dict(info or {})
        self.actor_id = actor_id
        self.reason = str(reason or "")

    def detail(self) -> dict:
        """409 响应体：包含 guidance 与 override 入口（不泄露抽检员身份）。"""
        info = self.info
        return {
            "code": "quality_gate_blocked",
            "message": "该版本尚未通过独立抽检，抽检门禁阻断了本次签发",
            "submission_id": self.submission_id,
            "version": self.version,
            "mode": str(info.get("mode") or ""),
            "mode_label": str(info.get("mode_label") or ""),
            "guidance": str(info.get("guidance") or OVERRIDE_GUIDANCE),
            "override_allowed": True,
            "latest_check": info.get("latest_check"),
            "gate": {
                key: info.get(key)
                for key in (
                    "mode", "armed", "enforced", "required", "window_open",
                    "pilot_limit", "published_total",
                )
            },
        }


# ---------------------------------------------------------------------------
# 配置解析
# ---------------------------------------------------------------------------

def _setting_text(con, key: str) -> str:
    """读取运行期设置（``app_settings`` 表可能尚未迁移完成，异常按未配置处理）。"""
    try:
        row = con.execute(
            "SELECT value FROM app_settings WHERE key=?", (str(key),),
        ).fetchone()
    except Exception:  # noqa: BLE001 - 设置表缺失时回退默认值，不阻断签发链路
        return ""
    if row is None:
        return ""
    try:
        return str(row[0] or "").strip()
    except (TypeError, ValueError):
        return ""


def _env(name: str) -> str:
    return str(os.environ.get(name, "") or "").strip()


def _parse_limit(raw) -> int | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        value = int(text)
    except (TypeError, ValueError):
        return None
    return max(0, value)


def _parse_armed(raw: str) -> bool | None:
    text = str(raw or "").strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    return None


def _count(con, sql: str, params=()) -> int:
    try:
        row = con.execute(sql, tuple(params)).fetchone()
    except Exception:  # noqa: BLE001 - 表缺失按 0 处理
        return 0
    try:
        return int(row[0] or 0)
    except (TypeError, ValueError):
        return 0


def config(con) -> dict:
    """解析当前生效的门禁配置（模式来源、激活状态、试点窗口与计数）。"""
    env_mode = _env(ENV_MODE).lower()
    db_mode = _setting_text(con, MODE_SETTING_KEY).lower()
    source = "default"
    mode = DEFAULT_GATE_MODE
    if env_mode:
        mode, source = env_mode, "env"
    if db_mode:
        mode, source = db_mode, "setting"
    mode_valid = mode in GATE_MODES
    if not mode_valid:
        source = f"{source}:invalid"
        mode = DEFAULT_GATE_MODE

    limit = None
    for raw in (_setting_text(con, LIMIT_SETTING_KEY), _env(ENV_LIMIT)):
        if raw:
            limit = _parse_limit(raw)
            if limit is not None:
                break
    if limit is None:
        limit = DEFAULT_PILOT_LIMIT

    checks_total = _count(con, "SELECT COUNT(*) FROM plaza_quality_checks")
    published_total = _count(con, "SELECT COUNT(*) FROM plaza_releases")
    raw_armed = _setting_text(con, ARMED_SETTING_KEY).lower() or _env(ENV_ARMED).lower()
    armed = _parse_armed(raw_armed)
    if armed is None:
        # 未显式指定：显式配置了模式（部署/管理设置）或已开始抽检 → 视为试点已激活。
        armed = bool(checks_total) or (source in ("env", "setting") and mode != "off")
        armed_source = "auto"
    else:
        armed_source = "setting" if _setting_text(con, ARMED_SETTING_KEY) else "env"

    window_open = limit == 0 or published_total < limit
    if mode == "off":
        mode_requires = False
    elif mode == "always":
        mode_requires = True
    else:  # initial_100
        mode_requires = window_open
    enforced = mode != "off" and armed
    #: 当前是否真的要求抽检（模式要求 + 已激活）；未激活/窗口结束均为 False。
    required = bool(mode_requires and enforced)
    return {
        "mode": mode,
        "mode_label": GATE_MODE_LABELS.get(mode, mode),
        "mode_source": source,
        "allowed_modes": list(GATE_MODES),
        "armed": bool(armed),
        "armed_source": armed_source,
        "enforced": bool(enforced),
        "required": bool(required),
        "blocking_possible": bool(enforced and required),
        "window_open": bool(window_open),
        "pilot_limit": int(limit),
        "published_total": int(published_total),
        "pilot_remaining": (max(0, int(limit) - int(published_total))
                            if limit else None),
        "checks_total": int(checks_total),
        "settings_keys": {
            "mode": MODE_SETTING_KEY,
            "armed": ARMED_SETTING_KEY,
            "pilot_limit": LIMIT_SETTING_KEY,
        },
        "env": {
            "mode": ENV_MODE,
            "armed": ENV_ARMED,
            "pilot_limit": ENV_LIMIT,
        },
    }


# ---------------------------------------------------------------------------
# 独立抽检判定
# ---------------------------------------------------------------------------

def _check_summary(row: dict | None) -> dict | None:
    """抽检记录摘要（不输出抽检员内部 id；公开/编辑端统一脱敏）。"""
    if not isinstance(row, dict):
        return None
    return {
        "id": int(row.get("id") or 0),
        "decision": str(row.get("decision") or ""),
        "severity": str(row.get("severity") or ""),
        "version": int(row.get("version") or 0),
        "public_id": str(row.get("public_id") or "") or None,
        "note": str(row.get("note") or "")[:500],
        "auto_withdrawn": bool(row.get("auto_withdrawn")),
        "checker_display": db.masked_display("抽检员", row.get("checker_id")),
        "created_at": str(row.get("created_at") or ""),
    }


def latest_check(con, submission_id: int, version: int) -> dict | None:
    row = con.execute(
        "SELECT * FROM plaza_quality_checks WHERE submission_id=? AND version=? "
        "ORDER BY id DESC LIMIT 1",
        (int(submission_id), int(version)),
    ).fetchone()
    return db._row_dict(row)


def _independent_pass(row: dict | None, author_id: int) -> bool:
    if not isinstance(row, dict):
        return False
    if str(row.get("decision") or "") != "pass":
        return False
    checker = int(row.get("checker_id") or 0)
    return bool(checker) and checker != int(author_id or 0)


def evaluate(con, submission: dict, version: int) -> dict:
    """评估某投稿版本的签发门禁：是否需要抽检、是否阻断、通过记录与指引。"""
    info = config(con)
    info.update({
        "version": int(version),
        "blocking": False,
        "satisfied": False,
        "passed": False,
        "pass_check": None,
        "latest_check": None,
        "guidance": "",
        "message": "",
    })
    if info["mode"] == "off":
        info["message"] = "抽检门禁已关闭：抽检记录保留，但不阻断签发"
        return info
    if not info["armed"]:
        info["message"] = "抽检门禁尚未激活：本次签发不阻断（开始抽检或显式开启后生效）"
        return info
    if not info["required"]:
        info["message"] = "试点首批 100% 抽检窗口已结束：本次签发不再强制抽检"
        return info
    row = latest_check(con, int(submission["id"]), int(version))
    info["latest_check"] = _check_summary(row)
    ok = _independent_pass(row, int(submission.get("author_id") or 0))
    info["satisfied"] = ok
    info["passed"] = ok
    if ok:
        info["pass_check"] = info["latest_check"]
        info["message"] = "该版本已通过独立抽检，允许签发"
    else:
        info["blocking"] = True
        info["guidance"] = OVERRIDE_GUIDANCE
        info["message"] = "该版本尚未通过独立抽检，抽检门禁阻断签发"
    return info


# ---------------------------------------------------------------------------
# 审计与配置写入
# ---------------------------------------------------------------------------

def record_outcome(con, submission_id, version: int, mode: str, outcome: str,
                   actor_id=None, reason: str = "") -> int:
    """在同一写事务内记录门禁判定结果（调用方持有 ``plaza-write`` 锁）。"""
    return db.insert_row(con, "plaza_quality_gate_log", {
        "submission_id": int(submission_id) if submission_id is not None else None,
        "version": int(version or 0),
        "mode": str(mode or "")[:40],
        "outcome": str(outcome or "")[:20],
        "actor_id": int(actor_id) if actor_id is not None else None,
        "reason": str(reason or "")[:2000],
        "created_at": utcnow(),
    })


def record_blocked(blocked: "QualityGateBlocked") -> None:
    """记录被阻断的签发尝试（独立事务；审计失败不得掩盖 409 响应）。"""
    try:
        with connection() as con:
            write_lock(con, "plaza-write")
            record_outcome(
                con, blocked.submission_id, blocked.version,
                str(blocked.info.get("mode") or ""), "blocked",
                actor_id=blocked.actor_id,
                reason=blocked.reason or str(blocked.info.get("message") or ""),
            )
    except Exception:  # noqa: BLE001 - 审计写失败不影响门禁结论
        return


def _write_setting(con, key: str, value: str) -> None:
    con.execute(
        "INSERT INTO app_settings(key,value,updated_at) VALUES (?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (str(key), str(value), utcnow()),
    )


def update_settings(*, mode: str | None = None, armed: bool | None = None,
                    pilot_limit: int | None = None, reset: bool = False) -> dict:
    """管理员修改门禁设置（写入 ``app_settings``，立即生效）；返回最新状态。"""
    with connection() as con:
        write_lock(con, "app-settings")
        if reset:
            con.execute(
                "DELETE FROM app_settings WHERE key IN (?,?,?)",
                (MODE_SETTING_KEY, ARMED_SETTING_KEY, LIMIT_SETTING_KEY),
            )
        else:
            if mode is not None:
                if mode not in GATE_MODES:
                    raise ValueError("未知的抽检门禁模式")
                _write_setting(con, MODE_SETTING_KEY, mode)
            if armed is not None:
                _write_setting(con, ARMED_SETTING_KEY, "1" if armed else "0")
            if pilot_limit is not None:
                limit = int(pilot_limit)
                if limit < 0:
                    raise ValueError("试点窗口计数不能为负数")
                _write_setting(con, LIMIT_SETTING_KEY, str(limit))
    with connection() as con:
        return state(con)


def state(con) -> dict:
    """门禁状态摘要：配置 + 审计计数 + 最近判定记录（编辑/管理员可见）。"""
    info = config(con)
    counts = {outcome: 0 for outcome in GATE_OUTCOMES}
    mode_counts: dict[str, int] = {}
    try:
        rows = con.execute(
            "SELECT outcome, mode, COUNT(*) FROM plaza_quality_gate_log "
            "GROUP BY outcome, mode",
        ).fetchall()
    except Exception:  # noqa: BLE001 - 日志表缺失按无记录处理
        rows = []
    for row in rows:
        outcome = str(row[0] or "")
        mode = str(row[1] or "")
        total = int(row[2] or 0)
        if outcome in counts:
            counts[outcome] += total
        mode_counts[mode] = mode_counts.get(mode, 0) + total
    recent: list[dict] = []
    try:
        recent_rows = con.execute(
            "SELECT * FROM plaza_quality_gate_log ORDER BY id DESC LIMIT 10",
        ).fetchall()
    except Exception:  # noqa: BLE001
        recent_rows = []
    for raw in recent_rows:
        item = db._row_dict(raw) or {}
        recent.append({
            "id": int(item.get("id") or 0),
            "submission_id": item.get("submission_id"),
            "version": int(item.get("version") or 0),
            "mode": str(item.get("mode") or ""),
            "outcome": str(item.get("outcome") or ""),
            "actor_id": item.get("actor_id"),
            "reason": str(item.get("reason") or ""),
            "created_at": str(item.get("created_at") or ""),
        })
    info.update({
        "counts": counts,
        "override_total": counts.get("override", 0),
        "blocked_total": counts.get("blocked", 0),
        "passed_total": counts.get("passed", 0),
        "evaluated_total": sum(counts.values()),
        "by_mode": mode_counts,
        "recent": recent,
        "notice": (
            "试点首批 100% 抽检门禁：未抽检的签发会被阻断并提示 override；"
            "override 与阻断均写入审计与指标。"
        ),
    })
    return info


__all__ = [
    "DEFAULT_GATE_MODE",
    "DEFAULT_PILOT_LIMIT",
    "ENV_ARMED",
    "ENV_LIMIT",
    "ENV_MODE",
    "GATE_MODES",
    "GATE_MODE_LABELS",
    "GATE_OUTCOMES",
    "LIMIT_SETTING_KEY",
    "MODE_SETTING_KEY",
    "ARMED_SETTING_KEY",
    "OVERRIDE_GUIDANCE",
    "QualityGateBlocked",
    "config",
    "evaluate",
    "latest_check",
    "record_blocked",
    "record_outcome",
    "state",
    "update_settings",
]
