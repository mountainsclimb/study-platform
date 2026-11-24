"""用户已配置的云端语音识别（ASR）连接发现。

通用语音 API 与课堂语音输入共用同一套判定：连接存在、模型名带 ASR 标记
（如 ``mimo-v2.5-asr``）才可作为候选；不读取/回显密钥。
"""
from __future__ import annotations

import json

from .. import db

__all__ = ["asr_candidates", "resolve_asr_candidate"]


def _models(provider: dict) -> list[str]:
    raw = provider.get("models_json")
    try:
        values = json.loads(raw) if isinstance(raw, str) else (raw or [])
    except (TypeError, ValueError):
        values = []
    return [str(item) for item in values if str(item).strip()]


def asr_candidates(user_id: int) -> list[dict]:
    """返回 ``[{provider, model}]``：用户已配置且模型名带 ASR 标记的连接（按 id 升序）。"""
    from . import audio as ai_audio

    with db.connection() as con:
        providers = [dict(row) for row in con.execute(
            "SELECT id,name,kind,base_url,encrypted_key,models_json,extra_headers_enc,"
            "compat_json,auth_mode FROM ai_providers WHERE user_id=? ORDER BY id",
            (int(user_id),),
        ).fetchall()]
    candidates: list[dict] = []
    for provider in providers:
        matched = [model for model in _models(provider) if ai_audio.is_asr_model(model)]
        if matched:
            candidates.append({"provider": provider, "model": matched[0]})
    return candidates


def resolve_asr_candidate(user_id: int, provider_id: int | None,
                          model: str | None) -> dict:
    """按连接/模型解析 ASR 候选；非法选择抛 ``ValueError``（路由转中文 4xx）。"""
    candidates = asr_candidates(user_id)
    if provider_id:
        for item in candidates:
            if int(item["provider"]["id"]) == int(provider_id):
                if model and model not in _models(item["provider"]):
                    raise ValueError("该连接没有配置这个语音识别模型")
                return {"provider": item["provider"], "model": model or item["model"]}
        raise LookupError("未找到该语音识别连接")
    if model:
        for item in candidates:
            if model in _models(item["provider"]):
                return {"provider": item["provider"], "model": model}
        raise ValueError("该模型未在已配置的连接中，请先在模型设置保存该模型")
    if not candidates:
        raise LookupError("还没有可用的语音识别模型")
    return candidates[0]
