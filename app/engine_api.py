"""本地可选引擎状态总览（阶段 5-1）：设置页排障与重新检测/重置。

只读聚合五个可选引擎的真实状态：

* 离线语音（sherpa-onnx TTS/ASR）
* 本地向量与重排（fastembed）
* 公式识别（rapid-latex-ocr）
* 表格结构（rapid-table）
* 分子信息（RDKit）

安全边界：绝对模型目录、模型文件路径与缺失文件清单只对管理员返回；普通用户只看到
可用性、依赖/模型数量、脱敏后的原因与加载状态。状态接口**不触发模型加载**，不伪造
「可用」，也不返回密钥。重置会重置本进程单例并把世代号写入数据库，
其它进程（arq Worker）在下个任务前跟随；仅管理员可执行。
"""
from __future__ import annotations

from datetime import datetime, timezone
import importlib.util
import re

from fastapi import APIRouter, Depends, HTTPException

from . import auth, db
from .ai import warmup

router = APIRouter(prefix="/api/engines", tags=["Engines"])

ENGINE_IDS = ("speech", "embeddings", "formula", "table", "chem")
_RESETTABLE = frozenset({"speech", "embeddings", "formula", "table"})
_SENSITIVE_KEYS = frozenset({
    "directories", "model_path", "rerank_model_path", "model_file",
    "models", "rerank_models", "missing_files", "missing_rerank_files",
})
_TEXT_KEYS = ("notice", "error", "asr_error", "rerank_error", "tts_error")
_WINDOWS_PATH_RE = re.compile(r"[A-Za-z]:[\\/][^\s，；,;）)\]]*")
_POSIX_PATH_RE = re.compile(r"(?<![\w.])/(?:[^\s，；,;）)\]]+/)+[^\s，；,;）)\]]*")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _chem_status() -> dict:
    available = importlib.util.find_spec("rdkit") is not None
    return {
        "engine": "rdkit",
        "mode": "optional",
        "available": available,
        "loaded": available,
        "version": None,
        "notice": (
            "RDKit 可用：分子式、摩尔质量与结构信息按需计算。"
            if available
            else "RDKit 未安装（requirements-chem.txt 可选依赖）；分子信息接口返回 503，不影响配平与 3D 渲染。"
        ),
    }


def _engine_statuses() -> dict[str, dict]:
    from .ai import embeddings_local, formula_ocr, speech_local, table_structure

    return {
        "speech": speech_local.engine_status(),
        "embeddings": embeddings_local.engine_status(),
        "formula": formula_ocr.engine_status(),
        "table": table_structure.engine_status(),
        "chem": _chem_status(),
    }


def _redact(value: str, paths: list[str]) -> str:
    text = str(value or "")
    for path in sorted(paths, key=len, reverse=True):
        if path:
            text = text.replace(path, "…")
    text = _WINDOWS_PATH_RE.sub("…", text)
    text = _POSIX_PATH_RE.sub("…", text)
    return text


def _known_paths(status: dict) -> list[str]:
    paths = [
        str(status.get("model_path") or ""),
        str(status.get("rerank_model_path") or ""),
        str(status.get("model_file") or ""),
    ]
    directories = status.get("directories")
    if isinstance(directories, dict):
        paths.extend(str(value) for value in directories.values() if value)
    return [path for path in paths if path]


def _public_engine(engine_id: str, status: dict, *, include_paths: bool) -> dict:
    row = {"id": engine_id, "checked_at": _now()}
    if include_paths:
        row.update(status)
        row["missing_count"] = (
            len(status.get("missing_files") or [])
            + len(status.get("missing_rerank_files") or [])
        )
        row["model_count"] = len(status.get("models") or [])
        return row
    paths = _known_paths(status)
    for key, value in status.items():
        if key in _SENSITIVE_KEYS:
            continue
        if key in _TEXT_KEYS and isinstance(value, str):
            row[key] = _redact(value, paths)
        else:
            row[key] = value
    row["missing_count"] = (
        len(status.get("missing_files") or [])
        + len(status.get("missing_rerank_files") or [])
    )
    row["model_count"] = len(status.get("models") or [])
    return row


def _compression_status(*, include_paths: bool) -> dict:
    from .ai import audio_compress

    status = audio_compress.status()
    executable = status.pop("ffmpeg", None)
    status["ffmpeg_available"] = bool(executable)
    if include_paths:
        status["ffmpeg"] = executable
    return status


def engine_overview(*, include_paths: bool) -> dict:
    """返回引擎总览；``include_paths`` 只应为管理员开启。"""
    statuses = _engine_statuses()
    engines = [
        _public_engine(engine_id, statuses[engine_id], include_paths=include_paths)
        for engine_id in ENGINE_IDS
    ]
    available = sum(1 for engine in engines if engine.get("available"))
    return {
        "checked_at": _now(),
        "window": {"count": len(engines), "available": available},
        "engines": engines,
        "warmup": warmup.state(),
        "compression": _compression_status(include_paths=include_paths),
        "can_reset": bool(include_paths),
        "note": "状态来自依赖与模型目录探测，不触发模型加载；不可用时不影响主流程。",
    }


@router.get("")
def list_engines(user=Depends(auth.current_user)):
    """本地可选引擎总览（普通用户不含绝对路径；不返回密钥）。"""
    return engine_overview(include_paths=user.get("role") == "admin")


@router.post("/{engine_id}/reset")
def reset_engine(engine_id: str, user=Depends(auth.require_admin)):
    """重置指定引擎：本进程立即生效，其它进程（arq Worker）在下个任务前应用。

    推理不只在 API 进程发生：``recognize``/``ocr`` 任务由 Worker 执行，课堂旁白也在
    Worker 内调用本地语音。若只清本进程单例，「管理员点了重置但 Worker 里失败的缓存
    还在」会让排障结论失真。因此重置会写一个跨进程世代号（``app_settings`` 里的
    ``engine.reset.<engine>``），Worker 通过 ``engine_reset.apply_pending()`` 跟随。
    """
    if engine_id not in ENGINE_IDS:
        raise HTTPException(404, "未知引擎")
    if engine_id not in _RESETTABLE:
        raise HTTPException(422, "该引擎不支持重置")
    from .ai import engine_reset

    marked = engine_reset.mark_reset(engine_id)
    warmup.reset_state()
    payload = engine_overview(include_paths=True)
    payload["reset"] = {
        "engine": engine_id,
        "at": marked["generation"],
        # 管理页据此说明作用域，避免「已重置但 Worker 仍失败」的误解。
        "scope": "api-process-immediately,worker-on-next-task",
        "note": "本进程已立即重置；其它进程（AI 任务 Worker）会在执行下一个任务前应用。",
    }
    return payload
