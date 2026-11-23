"""本地向量与重排（fastembed，可选依赖）：延迟加载、失败缓存、可重置。

按《开源组件整合实施方案》§5 与 §1.1 落地：

* ``STUDY_EMBEDDING_MODE=local`` 启用本地向量；``STUDY_RERANK=on`` 启用本地重排。
* 模型由部署方预置，不进仓库与镜像：
  - 向量 ``bge-small-zh-v1.5``（MIT，512 维）；
  - 重排 ``bge-reranker-base``（MIT）。
  目录默认 ``<data>/models/embeddings`` 与 ``<data>/models/rerank``，可用
  ``STUDY_LOCAL_EMBEDDING_DIR`` / ``STUDY_LOCAL_RERANK_DIR`` 覆盖；**目录在调用时读取**，
  以兼容测试夹具替换 ``db.DATA_DIR``。
* 同步推理由调用方经 ``asyncio.to_thread`` 执行，进程级信号量限制并发。
* 目录缺失、依赖未安装或引擎初始化失败时一律按不可用处理；调用方回退到现有
  HTTP Embedding 或本地 TF-IDF 检索，不产生占位向量。
"""
from __future__ import annotations

import importlib.metadata
import importlib.util
import os
from pathlib import Path
import threading
import time

from .. import db

__all__ = [
    "ENGINE_NAME",
    "EMBEDDING_MODEL_NAME",
    "RERANK_MODEL_NAME",
    "LocalEmbeddingError",
    "LocalEmbeddingUnavailable",
    "mode",
    "rerank_mode",
    "model_name",
    "rerank_model_name",
    "embedding_dir",
    "rerank_dir",
    "local_available",
    "rerank_available",
    "embed",
    "rerank",
    "dimension",
    "engine_status",
    "reset_engine",
    "MAX_CONCURRENCY",
]

ENGINE_NAME = "fastembed"
EMBEDDING_MODEL_NAME = "BAAI/bge-small-zh-v1.5"
RERANK_MODEL_NAME = "BAAI/bge-reranker-base"
MAX_CONCURRENCY = 2
EMBEDDING_TIMEOUT_SECONDS = 120
NOTICE = (
    "本地向量与重排由 fastembed + ONNX Runtime 在本机 CPU 上运行；"
    "模型目录由部署方预置，不进仓库与镜像。"
)

_engine = None
_engine_error: str | None = None
_rerank_engine = None
_rerank_error: str | None = None
_engine_lock = threading.Lock()
_rerank_lock = threading.Lock()
_infer_slots = threading.BoundedSemaphore(MAX_CONCURRENCY)
_dimension: int | None = None


class LocalEmbeddingError(RuntimeError):
    """本地向量/重排异常基类。"""


class LocalEmbeddingUnavailable(LocalEmbeddingError):
    """开关关闭、依赖缺失、模型目录缺失或引擎初始化失败。"""


def mode() -> str:
    return "local" if os.environ.get("STUDY_EMBEDDING_MODE", "").strip().lower() == "local" else "off"


def rerank_mode() -> str:
    return "on" if os.environ.get("STUDY_RERANK", "").strip().lower() == "on" else "off"


def model_name() -> str:
    return EMBEDDING_MODEL_NAME


def rerank_model_name() -> str:
    return RERANK_MODEL_NAME


def embedding_dir() -> Path:
    """向量模型目录（调用时读取，测试可替换 db.DATA_DIR）。"""
    configured = os.environ.get("STUDY_LOCAL_EMBEDDING_DIR", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(db.DATA_DIR) / "models" / "embeddings"


def rerank_dir() -> Path:
    configured = os.environ.get("STUDY_LOCAL_RERANK_DIR", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(db.DATA_DIR) / "models" / "rerank"


def _module_available() -> bool:
    for name in ("fastembed", "onnxruntime"):
        try:
            if importlib.util.find_spec(name) is None:
                return False
        except (ImportError, ValueError):
            return False
    return True


def _engine_version() -> str | None:
    try:
        return importlib.metadata.version(ENGINE_NAME)
    except importlib.metadata.PackageNotFoundError:
        return None
    except Exception:
        return None


def _model_files(directory: Path) -> list[str]:
    try:
        return sorted(path.name for path in directory.glob("*.onnx"))
    except OSError:
        return []


def _dir_ready(directory: Path) -> bool:
    """模型目录可用：存在且包含 ONNX 文件（或 fastembed 的 HF 缓存子目录）。"""
    try:
        if not directory.is_dir():
            return False
        if _model_files(directory):
            return True
        # 允许部署方按 huggingface_hub 缓存结构放置 models--... 目录。
        return any(path.is_dir() and path.name.startswith("models--") for path in directory.iterdir())
    except OSError:
        return False


def _get_engine():
    """返回 TextEmbedding 单例；首次调用加载模型，失败缓存（可 reset_engine 重试）。"""
    global _engine, _engine_error
    with _engine_lock:
        if _engine is not None:
            return _engine
        if _engine_error is not None:
            raise LocalEmbeddingUnavailable(_engine_error)
        directory = embedding_dir()
        if not _dir_ready(directory):
            _engine_error = (
                f"本地向量模型目录不可用：{directory}（需预置 {EMBEDDING_MODEL_NAME}）"
            )
            raise LocalEmbeddingUnavailable(_engine_error)
        try:
            from fastembed import TextEmbedding
        except Exception as error:
            _engine_error = (
                f"本地向量依赖不可用（{type(error).__name__}），将使用本地全文检索"
            )
            raise LocalEmbeddingUnavailable(_engine_error) from None
        try:
            engine = TextEmbedding(
                model_name=EMBEDDING_MODEL_NAME,
                cache_dir=str(directory),
                local_files_only=True,
                threads=1,
            )
        except Exception as error:
            _engine_error = (
                f"本地向量引擎初始化失败（{type(error).__name__}），"
                "将使用本地全文检索"
            )
            raise LocalEmbeddingUnavailable(_engine_error) from None
        _engine = engine
        return _engine


def _get_rerank_engine():
    global _rerank_engine, _rerank_error
    with _rerank_lock:
        if _rerank_engine is not None:
            return _rerank_engine
        if _rerank_error is not None:
            raise LocalEmbeddingUnavailable(_rerank_error)
        directory = rerank_dir()
        if not _dir_ready(directory):
            _rerank_error = (
                f"本地重排模型目录不可用：{directory}（需预置 {RERANK_MODEL_NAME}）"
            )
            raise LocalEmbeddingUnavailable(_rerank_error)
        try:
            from fastembed.rerank.cross_encoder import TextCrossEncoder
        except Exception as error:
            _rerank_error = (
                f"本地重排依赖不可用（{type(error).__name__}），将沿用原排序"
            )
            raise LocalEmbeddingUnavailable(_rerank_error) from None
        try:
            engine = TextCrossEncoder(
                model_name=RERANK_MODEL_NAME,
                cache_dir=str(directory),
                local_files_only=True,
                threads=1,
            )
        except Exception as error:
            _rerank_error = (
                f"本地重排引擎初始化失败（{type(error).__name__}），将沿用原排序"
            )
            raise LocalEmbeddingUnavailable(_rerank_error) from None
        _rerank_engine = engine
        return _rerank_engine


def reset_engine() -> None:
    global _engine, _engine_error, _rerank_engine, _rerank_error, _dimension
    with _engine_lock:
        _engine = None
        _engine_error = None
        _dimension = None
    with _rerank_lock:
        _rerank_engine = None
        _rerank_error = None


def local_available() -> bool:
    """本地向量引擎是否可用（依赖 + 模型目录）。

    注意：这里**不**检查 ``STUDY_EMBEDDING_MODE``——账号级配置也可以选择 ``local``，
    是否启用由 ``embeddings.embedding_config()`` 决定；环境变量开关只影响默认回退。
    """
    if _engine is not None:
        return True
    if _engine_error is not None:
        return False
    return _module_available() and _dir_ready(embedding_dir())


def rerank_available() -> bool:
    if rerank_mode() != "on":
        return False
    if _rerank_engine is not None:
        return True
    if _rerank_error is not None:
        return False
    return _module_available() and _dir_ready(rerank_dir())


def _unavailable_reason() -> str:
    if _engine_error is not None:
        return _engine_error
    return (
        f"本地向量不可用：未安装 fastembed 或模型目录缺失（{embedding_dir()}）"
    )


def dimension() -> int | None:
    """已加载模型的向量维度；未加载过时返回 None（不触发加载）。"""
    return _dimension


def engine_status() -> dict:
    available = local_available()
    rerank_ready = rerank_available()
    if available:
        notice = NOTICE
        if mode() != "local":
            notice += "当前未通过 STUDY_EMBEDDING_MODE 默认启用，可在设置页选择「本地模型」。"
    else:
        notice = _unavailable_reason() + "；不影响其它功能。"
    return {
        "engine": ENGINE_NAME,
        "mode": mode(),
        "rerank_mode": rerank_mode(),
        "available": available,
        "rerank_available": rerank_ready,
        "loaded": _engine is not None,
        "rerank_loaded": _rerank_engine is not None,
        "version": _engine_version() if available else None,
        "model": EMBEDDING_MODEL_NAME,
        "rerank_model": RERANK_MODEL_NAME,
        "dimension": _dimension,
        "models": _model_files(embedding_dir()) if available else [],
        "rerank_models": _model_files(rerank_dir()) if rerank_ready else [],
        "limits": {"concurrency": MAX_CONCURRENCY, "timeout_seconds": EMBEDDING_TIMEOUT_SECONDS},
        "directories": {
            "embeddings": str(embedding_dir()),
            "rerank": str(rerank_dir()),
        },
        "notice": notice,
        "error": _engine_error,
        "rerank_error": _rerank_error,
    }


def _as_float_list(vector) -> list[float]:
    try:
        values = [float(item) for item in vector]
    except (TypeError, ValueError):
        raise LocalEmbeddingError("本地向量引擎返回了非数值向量") from None
    if not values:
        raise LocalEmbeddingError("本地向量引擎返回空向量")
    return values


def embed(texts: list[str]) -> list[list[float]]:
    """同步计算文本向量；依赖/目录不可用或引擎失败时抛异常。"""
    if not local_available():
        raise LocalEmbeddingUnavailable(_unavailable_reason())
    cleaned = [str(text or "") for text in texts]
    if not cleaned:
        return []
    started = time.monotonic()
    with _infer_slots:
        engine = _get_engine()
        try:
            vectors = [_as_float_list(vector) for vector in engine.embed(cleaned)]
        except LocalEmbeddingError:
            raise
        except Exception as error:
            raise LocalEmbeddingError(
                f"本地向量计算失败（{type(error).__name__}）"
            ) from None
    if len(vectors) != len(cleaned):
        raise LocalEmbeddingError("本地向量引擎返回数量与输入不一致")
    global _dimension
    _dimension = len(vectors[0])
    if any(len(vector) != _dimension for vector in vectors):
        raise LocalEmbeddingError("本地向量维度不一致")
    # 只用于调试，不进入契约。
    embed.last_elapsed_ms = int((time.monotonic() - started) * 1000)
    return vectors


embed.last_elapsed_ms = 0


def rerank(query: str, texts: list[str]) -> list[float]:
    """同步计算重排得分；依赖/目录不可用或引擎失败时抛异常。"""
    if not rerank_available():
        raise LocalEmbeddingUnavailable("本地重排不可用（STUDY_RERANK 未开启或模型目录缺失）")
    documents = [str(text or "") for text in texts]
    if not documents:
        return []
    with _infer_slots:
        engine = _get_rerank_engine()
        try:
            scores = [float(score) for score in engine.rerank(str(query or ""), documents)]
        except Exception as error:
            raise LocalEmbeddingError(
                f"本地重排计算失败（{type(error).__name__}）"
            ) from None
    if len(scores) != len(documents):
        raise LocalEmbeddingError("本地重排返回数量与输入不一致")
    return scores
