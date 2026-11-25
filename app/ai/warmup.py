"""可选引擎启动预热：在后台线程加载模型，避免首个请求撞上模型下载/初始化超时。

只在对应开关显式打开时启动；默认零开销。每个引擎的 ``warmup()`` 只加载、不推理，
失败只记日志，不影响应用启动与其它功能。
"""
from __future__ import annotations

from datetime import datetime, timezone
import copy
import logging
import os
import threading

logger = logging.getLogger("zhixu.ai.warmup")

_state_lock = threading.Lock()
_state: dict = {"started": False, "started_at": None, "finished_at": None, "results": None}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def state() -> dict:
    """最近一次后台预热状态（设置页排障用；不触发模型加载）。"""
    with _state_lock:
        return copy.deepcopy(_state)


def reset_state() -> None:
    """清除预热记录（引擎重置/测试隔离用）。"""
    with _state_lock:
        _state.update({"started": False, "started_at": None, "finished_at": None, "results": None})


def _record(*, started: bool | None = None, finished: bool | None = None,
            results: dict | None = None) -> None:
    with _state_lock:
        if started is not None:
            _state["started"] = bool(started)
            _state["started_at"] = _now() if started else None
        if finished is not None:
            _state["finished_at"] = _now() if finished else None
        if results is not None:
            _state["results"] = copy.deepcopy(results)


def record_results(results: dict) -> None:
    """记录一次预热结果（供测试或启动线程复用）。"""
    _record(started=True, results=results, finished=True)

SWITCHES = (
    ("STUDY_FORMULA_OCR", "on"),
    ("STUDY_TABLE_OCR", "on"),
    ("STUDY_EMBEDDING_MODE", "local"),
    ("STUDY_RERANK", "on"),
    ("STUDY_LOCAL_SPEECH", "on"),
)


def any_enabled() -> bool:
    """是否有任一可选引擎通过环境变量显式开启。"""
    return any(
        os.environ.get(name, "").strip().lower() == value
        for name, value in SWITCHES
    )


def _targets():
    from . import embeddings_local, formula_ocr, speech_local, table_structure

    return (
        ("embeddings", embeddings_local.warmup),
        ("formula", formula_ocr.warmup),
        ("table", table_structure.warmup),
        ("speech", speech_local.warmup),
    )


def warmup_all() -> dict:
    """依次预热全部引擎；单个失败不影响其它引擎。"""
    results: dict[str, dict] = {}
    for name, warmup in _targets():
        try:
            results[name] = warmup()
        except Exception as error:  # noqa: BLE001 - 预热永远不能拖垮启动
            results[name] = {"error": f"{type(error).__name__}: {error}"}
    return results


def _log_results(results: dict) -> None:
    for name, result in results.items():
        if result.get("error"):
            logger.warning("可选引擎 %s 预热失败：%s", name, result["error"])
        elif result.get("loaded") or result.get("tts_loaded") or result.get("asr_loaded"):
            logger.info(
                "可选引擎 %s 预热完成（%sms）", name, result.get("elapsed_ms"),
            )
        else:
            logger.info(
                "可选引擎 %s 未预热：%s", name,
                result.get("skipped") or "不可用",
            )


def start_background_warmup() -> threading.Thread | None:
    """按开关启动守护线程；没有启用任何引擎时返回 ``None``。"""
    if not any_enabled():
        return None

    def run() -> None:
        _record(started=True, finished=False)
        try:
            results = warmup_all()
            _record(results=results, finished=True)
            _log_results(results)
        except Exception:  # pragma: no cover - 兜底日志
            _record(finished=True)
            logger.exception("可选引擎预热线程异常")

    thread = threading.Thread(target=run, name="ai-warmup", daemon=True)
    thread.start()
    return thread
