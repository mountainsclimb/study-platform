"""联网搜索适配层：DeepSeek 官方 ``/v1/tools/search``（默认）+ 可选 Tavily。

DeepSeek 搜索接口（已实测）：

    POST https://api.deepseek.com/v1/tools/search
    {"query": "关键词"}
    → {"results": [{"title", "url", "site_name", "summary", "date", "authority", "rerank_score"}]}

调用方（课堂生成、AI 助手、笔记 Agent）只依赖 :func:`web_search`，不直接拼 HTTP；
没有可用搜索连接时抛 :class:`SearchUnavailable`，由调用方给出中文引导，绝不伪造搜索结果。
"""
from __future__ import annotations

import json
import logging

from ..ai import httpx as ai_httpx

log = logging.getLogger("app.ai.search")

DEFAULT_LIMIT = 5
MAX_LIMIT = 8
MAX_QUERY_CHARS = 300
MAX_SUMMARY_CHARS = 1200
SEARCH_TIMEOUT_SECONDS = 45
DEEPSEEK_HOST_MARKER = "deepseek.com"


class SearchUnavailable(RuntimeError):
    """没有可用的搜索连接（或搜索服务失败）。调用方据此给出中文引导。"""


def is_deepseek_provider(provider: dict) -> bool:
    base = str((provider or {}).get("base_url") or "").lower()
    return DEEPSEEK_HOST_MARKER in base


def search_candidates(user_id: int) -> list[dict]:
    """返回具备联网搜索能力的连接（目前为 DeepSeek 官方连接）。"""
    from .. import db

    with db.connection() as con:
        rows = con.execute(
            "SELECT id,name,kind,base_url,encrypted_key,models_json,auth_mode,"
            "extra_headers_enc,compat_json FROM ai_providers WHERE user_id=? ORDER BY id",
            (user_id,),
        ).fetchall()
    return [dict(row) for row in rows if is_deepseek_provider(dict(row))]


def search_provider_info(user_id: int) -> dict | None:
    """``GET /meta`` / 设置页用：当前账号的搜索能力摘要。"""
    candidates = search_candidates(user_id)
    if not candidates:
        return None
    provider = candidates[0]
    return {"provider_id": int(provider["id"]), "provider": provider.get("name") or "", "endpoint": "/v1/tools/search"}


def _auth_headers(provider: dict) -> dict:
    from ..ai.providers import _decrypt_key, provider_extra_headers

    headers: dict[str, str] = {"Content-Type": "application/json", "Accept": "application/json"}
    api_key = _decrypt_key(provider.get("encrypted_key") or "")
    if api_key:
        headers["Authorization"] = "Bearer %s" % api_key
    try:
        headers.update(provider_extra_headers(provider) or {})
    except Exception:  # 自定义头非法时按无自定义头处理
        pass
    return headers


def _clean_results(payload, limit: int) -> list[dict]:
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list):
        return []
    cleaned: list[dict] = []
    for item in results[:limit]:
        if not isinstance(item, dict):
            continue
        summary = str(item.get("summary") or "").strip()
        if len(summary) > MAX_SUMMARY_CHARS:
            summary = summary[:MAX_SUMMARY_CHARS] + "…"
        cleaned.append({
            "title": str(item.get("title") or "").strip()[:200],
            "url": str(item.get("url") or "").strip()[:600],
            "site": str(item.get("site_name") or "").strip()[:80],
            "date": str(item.get("date") or "").strip()[:32],
            "summary": summary,
            "authority": item.get("authority"),
        })
    return cleaned


async def web_search(provider: dict, query: str, limit: int = DEFAULT_LIMIT, *,
                     client=None) -> dict:
    """调用 DeepSeek 官方搜索接口，返回 ``{query, results, provider}``。

    失败（网络/鉴权/结构异常/无结果）统一抛 :class:`SearchUnavailable`，不返回编造结果。
    """
    from ..ai import netguard
    from ..ai.providers import _validate_url

    text = str(query or "").strip()
    if not text:
        raise SearchUnavailable("搜索关键词为空")
    if len(text) > MAX_QUERY_CHARS:
        text = text[:MAX_QUERY_CHARS]
    safe_limit = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))

    base_url = _validate_url((provider or {}).get("base_url") or "", resolve=True)
    endpoint = netguard.join_endpoint(base_url, "/tools/search")
    headers = _auth_headers(provider)
    client_kwargs = netguard.client_kwargs(endpoint, timeout_seconds=SEARCH_TIMEOUT_SECONDS)
    own_client = client is None
    http = ai_httpx.AsyncClient(**client_kwargs) if own_client else client
    try:
        if own_client:
            await http.__aenter__()
        response = await http.post(endpoint, headers=headers, json={"query": text})
        if not 200 <= response.status_code < 300:
            raise SearchUnavailable("搜索服务返回 HTTP %s" % response.status_code)
        try:
            payload = response.json()
        except Exception as exc:  # noqa: BLE001
            raise SearchUnavailable("搜索服务返回结构无法解析") from exc
        results = _clean_results(payload, safe_limit)
        if not results:
            raise SearchUnavailable("搜索没有返回结果，请换一个更具体的关键词")
        return {
            "query": text,
            "results": results,
            "provider": (provider or {}).get("name") or "DeepSeek",
            "endpoint": "/v1/tools/search",
        }
    except SearchUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 - 网络异常统一转成可读错误
        log.warning("web search failed: %s", type(exc).__name__)
        raise SearchUnavailable("搜索服务暂不可用（%s）" % type(exc).__name__) from exc
    finally:
        if own_client:
            try:
                await http.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass


def format_results_for_prompt(result: dict, *, max_chars: int = 6000) -> str:
    """把搜索结果格式化为可注入提示词的中文片段（带来源编号与 URL，便于引用）。"""
    lines = ["【联网搜索资料（外部来源，引用时请保留标题与链接；不确定处标注待核实）】"]
    used = 0
    for index, item in enumerate(result.get("results") or [], start=1):
        block = "[%d] %s（%s%s）\n链接：%s\n摘要：%s" % (
            index, item.get("title") or "未命名",
            item.get("site") or "",
            (" · " + item.get("date")) if item.get("date") else "",
            item.get("url") or "",
            (item.get("summary") or "").strip(),
        )
        if used + len(block) > max_chars:
            break
        lines.append(block)
        used += len(block)
    return "\n\n".join(lines)


def public_result(result: dict) -> dict:
    """对外返回：去掉内部字段，保留可展示的结果与来源。"""
    return {
        "query": result.get("query") or "",
        "provider": result.get("provider") or "",
        "results": [
            {key: item.get(key) for key in ("title", "url", "site", "date", "summary", "authority")}
            for item in result.get("results") or []
        ],
    }


# ---------------------------------------------------------------------------
# HTTP 路由：/api/ai/web-search（供前端与外部 Agent 使用）
# ---------------------------------------------------------------------------

from fastapi import APIRouter, Depends, HTTPException, Query  # noqa: E402

from ..auth import current_user  # noqa: E402

router = APIRouter(prefix="/api", tags=["AI web search"])


@router.get("/ai/search/info")
def search_info(user=Depends(current_user)):
    """当前账号是否具备联网搜索能力（不泄露 Key）。"""
    info = search_provider_info(user["id"])
    if not info:
        return {
            "available": False,
            "provider": None,
            "hint": "联网搜索使用 DeepSeek 官方 /v1/tools/search；请在「模型设置」添加 DeepSeek 连接后启用",
        }
    return {"available": True, **info}


@router.get("/ai/web-search")
async def web_search_api(
    q: str = Query(min_length=2, max_length=MAX_QUERY_CHARS, description="搜索关键词"),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    user=Depends(current_user),
):
    """联网搜索（只读）：返回带来源摘要的真实网页结果；无连接时 422 中文引导。"""
    candidates = search_candidates(user["id"])
    if not candidates:
        raise HTTPException(
            422,
            "还没有可用的联网搜索连接：请在「模型设置」添加 DeepSeek 连接（使用官方 /v1/tools/search）",
        )
    try:
        result = await web_search(candidates[0], q, limit)
    except SearchUnavailable as exc:
        raise HTTPException(502, "联网搜索失败：%s" % exc) from exc
    return public_result(result)


__all__ = [
    "DEFAULT_LIMIT", "MAX_LIMIT", "SearchUnavailable",
    "is_deepseek_provider", "search_candidates", "search_provider_info",
    "web_search", "format_results_for_prompt", "public_result", "router",
]
