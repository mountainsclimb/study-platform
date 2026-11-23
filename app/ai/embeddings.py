"""Optional OpenAI-compatible embeddings for private study documents.

配置优先级：账号级 ``ai_embeddings_config``（custom 独立地址/Key、inherit 复用已有连接、
off 关闭）→ 环境变量 ``STUDY_EMBEDDING_BASE_URL`` / ``STUDY_EMBEDDING_MODEL`` /
``STUDY_EMBEDDING_API_KEY``（默认回退）→ 本地 TF-IDF 检索。

所有出站请求与模型调用走同一套 :mod:`app.ai.netguard` 策略（公网模式 IP Pinning），
并限制响应体大小防止恶意大响应拖垮进程。
"""
import asyncio
from contextlib import contextmanager
import contextvars
from dataclasses import dataclass, field
import hashlib
import json
import logging
import os
import time

from fastapi import HTTPException
import httpx

from .. import db
from ..search_vector import load_chunk_vectors, rank_chunks_hybrid, valid_embedding
from . import embeddings_local, netguard
from .providers import _cipher, provider_compat, resolve_auth_mode

_logger = logging.getLogger(__name__)
_BATCH_SIZE = 8
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
EMBEDDING_MODES = ("off", "inherit", "custom", "local")
_EMBEDDING_KINDS = ("openai", "gemini", "azure_openai")

_embedding_user_var: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "zhixu_embedding_user", default=None,
)
_last_error: dict = {}


@dataclass(frozen=True)
class EmbeddingConfig:
    endpoint: str
    model: str
    api_key: str = field(repr=False)
    auth_mode: str = "bearer"
    base_url: str = ""
    source: str = "env"
    timeout_seconds: float = 15.0

    @property
    def storage_model(self) -> str:
        # Same model labels at different endpoints need not use the same vector space.
        endpoint_id = hashlib.sha256(self.endpoint.encode()).hexdigest()[:16]
        return f"{self.model}@{endpoint_id}"

    @property
    def masked_endpoint(self) -> str:
        return netguard.mask_url(self.endpoint)


# ---------------------------------------------------------------------------
# 用户上下文（供笔记/上传链路按账号读取配置）
# ---------------------------------------------------------------------------

@contextmanager
def embedding_user(user_id: int | None):
    token = _embedding_user_var.set(user_id)
    try:
        yield
    finally:
        _embedding_user_var.reset(token)


def current_embedding_user() -> int | None:
    return _embedding_user_var.get()


# ---------------------------------------------------------------------------
# 配置表读写
# ---------------------------------------------------------------------------

def read_embedding_config(user_id: int | None) -> dict | None:
    if not user_id:
        return None
    try:
        with db.connection() as con:
            row = con.execute(
                "SELECT * FROM ai_embeddings_config WHERE user_id=?", (user_id,),
            ).fetchone()
    except Exception:
        return None
    return dict(row) if row else None


def save_embedding_config(
    user_id: int,
    *,
    mode: str,
    provider_id: int | None = None,
    base_url: str = "",
    model: str = "",
    api_key_enc: str = "",
) -> None:
    mode = mode if mode in EMBEDDING_MODES else "off"
    with db.connection() as con:
        con.execute(
            "INSERT INTO ai_embeddings_config(user_id,mode,provider_id,base_url,model,api_key_enc,updated_at) "
            "VALUES (?,?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET "
            "mode=excluded.mode,provider_id=excluded.provider_id,base_url=excluded.base_url,"
            "model=excluded.model,api_key_enc=excluded.api_key_enc,updated_at=excluded.updated_at",
            (user_id, mode, provider_id, base_url, model, api_key_enc, db.utcnow()),
        )


# ---------------------------------------------------------------------------
# 配置解析
# ---------------------------------------------------------------------------

def _embedding_policy() -> str:
    policy = netguard.url_policy()
    # 环境变量/后台服务是运维配置，不受用户白名单限制；公网模式仍强制 HTTPS + DNS 防护。
    return "public_https" if policy in netguard.PUBLIC_POLICIES else "local_open"


def _validated_base(base_url: str) -> str | None:
    try:
        return netguard.validate_base_url(base_url, resolve=False, policy=_embedding_policy())
    except HTTPException:
        return None


def _embedding_endpoint(base_url: str, compat: dict | None = None) -> str:
    params: dict[str, str] = {}
    version = str((compat or {}).get("api_version") or "")
    if version:
        params["api-version"] = version
    return netguard.join_endpoint(base_url, "/embeddings", params=params or None)


def _make_config(
    base_url: str,
    model: str,
    api_key: str,
    *,
    auth_mode: str = "bearer",
    source: str = "env",
    compat: dict | None = None,
) -> EmbeddingConfig | None:
    normalized = _validated_base(base_url)
    if not normalized or not str(model or "").strip():
        return None
    timeout_seconds = 15.0
    try:
        timeout_seconds = float((compat or {}).get("timeout_seconds") or 15.0)
    except (TypeError, ValueError):
        timeout_seconds = 15.0
    return EmbeddingConfig(
        endpoint=_embedding_endpoint(normalized, compat),
        model=str(model).strip(),
        api_key=api_key or "",
        auth_mode=auth_mode or "bearer",
        base_url=normalized,
        source=source,
        timeout_seconds=min(max(timeout_seconds, 5.0), 120.0),
    )


def env_embedding_config() -> EmbeddingConfig | None:
    """环境变量默认回退配置；未配置或格式非法时返回 None（走本地 TF-IDF）。"""
    base = os.environ.get("STUDY_EMBEDDING_BASE_URL", "").strip()
    model = os.environ.get("STUDY_EMBEDDING_MODEL", "").strip()
    if not base or not model:
        return None
    config = _make_config(
        base, model, os.environ.get("STUDY_EMBEDDING_API_KEY", "").strip(), source="env",
    )
    if config is None:
        _logger.warning("STUDY_EMBEDDING_BASE_URL 无效，使用本地检索")
    return config


def _custom_from_row(row: dict) -> EmbeddingConfig | None:
    api_key = ""
    encrypted = str(row.get("api_key_enc") or "")
    if encrypted:
        try:
            api_key = _cipher().decrypt(encrypted.encode()).decode()
        except Exception:
            api_key = ""
    return _make_config(
        str(row.get("base_url") or ""), str(row.get("model") or ""), api_key,
        auth_mode="bearer", source="custom",
    )


def _inherit_from_row(row: dict, user_id: int) -> EmbeddingConfig | None:
    provider_id = row.get("provider_id")
    if not provider_id:
        return None
    try:
        with db.connection() as con:
            provider = con.execute(
                "SELECT * FROM ai_providers WHERE id=? AND user_id=?", (provider_id, user_id),
            ).fetchone()
    except Exception:
        return None
    if provider is None:
        return None
    provider = dict(provider)
    kind = str(provider.get("kind") or "openai")
    if kind not in _EMBEDDING_KINDS:
        return None
    api_key = ""
    encrypted = str(provider.get("encrypted_key") or "")
    if encrypted:
        try:
            api_key = _cipher().decrypt(encrypted.encode()).decode()
        except Exception:
            api_key = ""
    compat = provider_compat(provider)
    return _make_config(
        str(provider.get("base_url") or ""), str(row.get("model") or ""), api_key,
        auth_mode=resolve_auth_mode(provider), source="inherit", compat=compat,
    )


def _local_config() -> EmbeddingConfig | None:
    """本地 fastembed 配置；模型目录/依赖不可用时返回 None（不伪造向量）。"""
    if not embeddings_local.local_available():
        return None
    model = embeddings_local.model_name()
    return EmbeddingConfig(
        endpoint="local://" + model,
        model=model,
        api_key="",
        base_url="",
        source="local",
    )


def _fallback_config() -> EmbeddingConfig | None:
    """账号配置无效时的回退：显式 local 模式优先，否则沿用环境变量 HTTP 配置。"""
    if os.environ.get("STUDY_EMBEDDING_MODE", "").strip().lower() == "local":
        return _local_config()
    return env_embedding_config()


def embedding_config(user_id: int | None = None) -> EmbeddingConfig | None:
    """解析当前生效的 Embedding 配置（账号配置优先，环境变量回退）。"""
    uid = user_id if user_id is not None else current_embedding_user()
    row = read_embedding_config(uid) if uid else None
    if row:
        mode = str(row.get("mode") or "off")
        if mode == "local":
            return _local_config()
        if mode == "custom":
            config = _custom_from_row(row)
            if config is not None:
                return config
            _logger.warning("账号自定义 Embedding 配置无效，回退到环境变量/本地检索")
            return _fallback_config()
        if mode == "inherit":
            config = _inherit_from_row(row, uid or 0)
            if config is not None:
                return config
            _logger.warning("复用的 Embedding 连接不可用，回退到环境变量/本地检索")
            return _fallback_config()
        return None
    return _fallback_config()


def _transient_config(
    user_id: int,
    *,
    mode: str | None = None,
    provider_id: int | None = None,
    base_url: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
) -> EmbeddingConfig | None:
    """构造「测试向量接口」用的临时配置（不落库）。"""
    row = read_embedding_config(user_id) or {}
    resolved_mode = mode or str(row.get("mode") or "")
    if resolved_mode == "off":
        return None
    if resolved_mode == "local":
        return _local_config()
    if resolved_mode == "inherit" or (not resolved_mode and row.get("mode") == "inherit"):
        effective_row = dict(row)
        effective_row["provider_id"] = provider_id or row.get("provider_id")
        effective_row["model"] = str(model or row.get("model") or "")
        return _inherit_from_row(effective_row, user_id)
    if resolved_mode == "custom" or base_url or model:
        key = ""
        if api_key is not None:
            key = str(api_key).strip()
        elif row.get("mode") == "custom" and row.get("api_key_enc"):
            try:
                key = _cipher().decrypt(str(row["api_key_enc"]).encode()).decode()
            except Exception:
                key = ""
        return _make_config(
            str(base_url or row.get("base_url") or ""),
            str(model or row.get("model") or ""),
            key, auth_mode="bearer", source="custom",
        )
    return embedding_config(user_id)


# ---------------------------------------------------------------------------
# 出站请求
# ---------------------------------------------------------------------------

def _auth_request(config: EmbeddingConfig) -> tuple[dict, str]:
    headers = {"Content-Type": "application/json"}
    url = config.endpoint
    mode = config.auth_mode or "bearer"
    if not config.api_key:
        return headers, url
    if mode == "bearer":
        headers["Authorization"] = "Bearer " + config.api_key
    elif mode == "x-api-key":
        headers["x-api-key"] = config.api_key
    elif mode == "api-key":
        headers["api-key"] = config.api_key
    elif mode == "query_key":
        url = netguard.join_endpoint(url, "", params={"key": config.api_key})
    return headers, url


def _set_last_error(message: str) -> None:
    _last_error.clear()
    _last_error.update({
        "message": netguard.mask_secrets(str(message or ""))[:300],
        "at": db.utcnow(),
    })


def _clear_last_error() -> None:
    _last_error.clear()


async def _request_embeddings(config: EmbeddingConfig, texts: list[str]) -> list[list[float]]:
    headers, url = _auth_request(config)
    vectors: list[list[float]] = []
    dimension = None
    kwargs = netguard.client_kwargs(
        url, timeout_seconds=config.timeout_seconds, connect_timeout=10.0,
    )
    async with httpx.AsyncClient(**kwargs) as client:
        for offset in range(0, len(texts), _BATCH_SIZE):
            batch = texts[offset:offset + _BATCH_SIZE]
            async with client.stream("POST", url, headers=headers, json={
                "model": config.model, "input": batch, "encoding_format": "float",
            }) as response:
                status = response.status_code
                buf = bytearray()
                async for piece in response.aiter_bytes():
                    buf.extend(piece)
                    if len(buf) > MAX_RESPONSE_BYTES:
                        raise ValueError("embedding response exceeds size limit")
                if not 200 <= status < 300:
                    detail = netguard.mask_secrets(buf.decode("utf-8", "ignore"))[:300]
                    raise ValueError(f"embedding request failed with HTTP {status} {detail}".strip())
            try:
                payload = json.loads(buf)
            except json.JSONDecodeError:
                raise ValueError("embedding response is not valid JSON") from None
            rows = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(rows, list) or len(rows) != len(batch):
                raise ValueError("embedding response does not match batch size")
            by_index = {}
            for row in rows:
                idx = row.get("index") if isinstance(row, dict) else None
                if type(idx) is not int or not 0 <= idx < len(batch) or idx in by_index:
                    raise ValueError("invalid embedding response index")
                vec = row.get("embedding")
                if not valid_embedding(vec) or (dimension is not None and len(vec) != dimension):
                    raise ValueError("invalid embedding response vector")
                dimension = len(vec)
                by_index[idx] = vec
            vectors.extend(by_index[i] for i in range(len(batch)))
    return vectors


async def _embed(config: EmbeddingConfig, texts: list[str]) -> list[list[float]]:
    if config.source == "local":
        try:
            vectors = await asyncio.wait_for(
                asyncio.to_thread(embeddings_local.embed, texts),
                timeout=embeddings_local.EMBEDDING_TIMEOUT_SECONDS,
            )
            if len(vectors) != len(texts):
                raise ValueError("本地向量返回数量与输入不一致")
            _clear_last_error()
            return vectors
        except (embeddings_local.LocalEmbeddingError, ValueError, TimeoutError, OSError) as exc:
            _set_last_error(f"{type(exc).__name__}: {exc}")
            _logger.warning("本地向量计算失败（%s），使用本地检索", type(exc).__name__)
            return []
    try:
        vectors = await asyncio.wait_for(_request_embeddings(config, texts), timeout=30)
        _clear_last_error()
        return vectors
    except (httpx.HTTPError, httpx.InvalidURL, HTTPException, ValueError, TimeoutError, OSError) as exc:
        _set_last_error(f"{type(exc).__name__}: {exc}")
        _logger.warning("资料向量请求失败（%s），使用本地检索", type(exc).__name__)
        return []


# ---------------------------------------------------------------------------
# 对外能力
# ---------------------------------------------------------------------------

async def document_vectors(chunks: list[dict], *, user_id: int | None = None) -> list[tuple[str, str, str]]:
    config = embedding_config(user_id)
    if not config or not chunks:
        return []
    texts = ["\n".join(filter(None, [c.get("heading_path"), c["text"]])) for c in chunks]
    vectors = await _embed(config, texts)
    storage_model = config.storage_model
    return [(c["chunk_id"], storage_model, json.dumps(v)) for c, v in zip(chunks, vectors)]


def store_document_vectors(con, document_id: int, vectors: list[tuple[str, str, str]]) -> None:
    if vectors:
        created_at = db.utcnow()
        for chunk_id, model, vector in vectors:
            con.execute(
                "INSERT INTO chunk_embeddings(document_id,chunk_id,model,vector_json,created_at) VALUES (?,?,?,?,?) "
                "ON CONFLICT(document_id,chunk_id,model) DO UPDATE "
                "SET vector_json=excluded.vector_json,created_at=excluded.created_at",
                (document_id, chunk_id, model, vector, created_at),
            )


def _rerank_callable():
    """返回启用的重排可调用对象；未启用/初始化失败返回 None。"""
    try:
        from . import rerank as rerank_module

        return rerank_module.score_texts if rerank_module.available() else None
    except Exception:
        return None


async def retrieve_document_chunks(
    chunks: list[dict], query_text: str, top_k: int, *, user_id: int | None = None,
) -> list[dict]:
    config = embedding_config(user_id)
    rerank = _rerank_callable()
    if config and len(chunks) > top_k and query_text.strip():
        dense_vectors = load_chunk_vectors(chunks, config.storage_model)
        if dense_vectors:
            query_vectors = await _embed(config, [query_text])
            if query_vectors:
                # 重排是 CPU 推理：连同 TF-IDF 打分一起放线程池，避免阻塞事件循环。
                return await asyncio.to_thread(
                    rank_chunks_hybrid,
                    chunks, query_text,
                    top_k=top_k,
                    query_vector=query_vectors[0],
                    dense_vectors=dense_vectors,
                    rerank=rerank,
                )
    return await asyncio.to_thread(
        rank_chunks_hybrid, chunks, query_text, top_k=top_k, rerank=rerank,
    )


def _display_endpoint(config: EmbeddingConfig) -> str:
    if config.source == "local":
        return "local://" + config.model
    return config.masked_endpoint


def embedding_status(user_id: int | None = None) -> dict:
    """就绪页用的 Embedding 状态：来源 / 模型 / 端点（脱敏）/ 最近失败原因。"""
    config = embedding_config(user_id)
    row = read_embedding_config(user_id) if user_id else None
    env_config = env_embedding_config()
    local_available = embeddings_local.local_available()
    if config is not None:
        return {
            "configured": True,
            "ready": True,
            "source": config.source,
            "model": config.model,
            "endpoint": _display_endpoint(config),
            "env_configured": env_config is not None,
            "local_available": local_available,
            "rerank": embeddings_local.rerank_available(),
            "detail": "本地语义检索已配置" if config.source == "local" else "语义检索已配置",
            "recent_error": dict(_last_error) if _last_error else None,
        }
    mode = str((row or {}).get("mode") or "")
    if mode == "off":
        detail = "已关闭语义检索，使用本地全文检索"
        source = "off"
    elif mode == "local" or os.environ.get("STUDY_EMBEDDING_MODE", "").strip().lower() == "local":
        detail = (
            "本地向量模型可用，但当前配置未生效"
            if local_available else "本地向量模型不可用（依赖未安装或模型目录缺失），使用本地全文检索"
        )
        source = "local"
    elif env_config is not None:
        detail = "环境变量配置不可用，使用本地全文检索"
        source = "env"
    else:
        detail = "未配置 Embedding 服务，使用本地全文检索"
        source = "none"
    return {
        "configured": False,
        "ready": False,
        "source": source,
        "model": "",
        "endpoint": "",
        "env_configured": env_config is not None,
        "local_available": local_available,
        "rerank": embeddings_local.rerank_available(),
        "detail": detail,
        "recent_error": dict(_last_error) if _last_error else None,
    }


async def test_embedding_config(
    user_id: int,
    *,
    mode: str | None = None,
    provider_id: int | None = None,
    base_url: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    text: str | None = None,
) -> dict:
    """发送 1 条短文本验证向量接口，返回维度/耗时/脱敏错误。"""
    config = _transient_config(
        user_id, mode=mode, provider_id=provider_id, base_url=base_url,
        model=model, api_key=api_key,
    )
    if config is None:
        return {
            "ok": False, "dimension": None, "latency_ms": None, "model": "",
            "endpoint": "", "source": "off",
            "diagnosis": "当前语义检索未配置或已关闭，使用本地全文检索",
        }
    sample = (str(text or "").strip() or "知序语义检索连接测试")
    started = time.monotonic()
    if config.source == "local":
        vectors = await _embed(config, [sample])
        dimension = len(vectors[0]) if vectors else None
        error = dict(_last_error).get("message") if _last_error else ""
        return {
            "ok": bool(vectors),
            "dimension": dimension,
            "latency_ms": max(0, int((time.monotonic() - started) * 1000)),
            "model": config.model,
            "endpoint": _display_endpoint(config),
            "source": "local",
            "diagnosis": (
                "本地向量模型正常" if vectors
                else ("本地向量模型不可用：" + (error or "未返回向量"))
            ),
        }
    try:
        vectors = await asyncio.wait_for(_request_embeddings(config, [sample]), timeout=30)
    except (httpx.HTTPError, httpx.InvalidURL, HTTPException, ValueError, TimeoutError, OSError) as exc:
        message = netguard.mask_secrets(f"{type(exc).__name__}: {exc}")[:300]
        _set_last_error(message)
        return {
            "ok": False, "dimension": None,
            "latency_ms": max(0, int((time.monotonic() - started) * 1000)),
            "model": config.model, "endpoint": _display_endpoint(config),
            "source": config.source, "diagnosis": f"向量接口不可用：{message}",
        }
    _clear_last_error()
    dimension = len(vectors[0]) if vectors else None
    return {
        "ok": bool(vectors),
        "dimension": dimension,
        "latency_ms": max(0, int((time.monotonic() - started) * 1000)),
        "model": config.model,
        "endpoint": _display_endpoint(config),
        "source": config.source,
        "diagnosis": "向量接口正常" if vectors else "向量接口返回空结果",
    }


__all__ = [
    "EMBEDDING_MODES", "EmbeddingConfig", "MAX_RESPONSE_BYTES", "current_embedding_user",
    "document_vectors", "embedding_config", "embedding_status", "embedding_user",
    "env_embedding_config", "read_embedding_config", "retrieve_document_chunks",
    "save_embedding_config", "store_document_vectors", "test_embedding_config",
]
