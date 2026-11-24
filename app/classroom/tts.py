"""AI 课堂 TTS 分层：默认浏览器 ``speechSynthesis``，可选 OpenAI 兼容 ``/audio/speech``。

契约（总方案 §3.1-4）：没有 TTS 厂商配置时返回 ``{"fallback": "browser"}``；配置了且具备
``audio/speech`` 能力时直接 httpx 调 OpenAI 兼容端点（走 ``netguard`` 校验、60s 超时），
音频按 sha256 内容寻址写入 ``cls_assets``；**任何失败都回退浏览器语音而不是报 500**。

P4 音色列表：:func:`list_voices` 按已配置的 TTS 连接聚合可用音色——
MiniMax 走 ``/get_voice``（``{voice_type:"all"}``，模块级缓存 10 分钟，失败回退内置列表并标注
``source:'static'``），小米 MiMo 用 9 个固定音色，其余 OpenAI 兼容连接返回默认音色标注。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time

from .. import storage
from ..ai import netguard, speech_local
from . import db as cdb

log = logging.getLogger("app.classroom.tts")

TTS_FOLDER = cdb.ASSET_FOLDER
TTS_KIND = "tts"
DEFAULT_VOICE = "alloy"
DEFAULT_SPEED = 1.0
MAX_TTS_CHARS = 2000
TTS_TIMEOUT_SECONDS = 60
#: 音色列表缓存时长（秒）：MiniMax ``/get_voice`` 的结果在模块级 dict 中缓存 10 分钟。
VOICES_CACHE_TTL_SECONDS = 600
#: MiniMax 音色查询超时（秒）：失败只回退静态列表，不拖慢页面。
VOICE_QUERY_TIMEOUT_SECONDS = 20
_TTS_MODEL_MARKERS = ("tts", "audio", "speech", "voice")
_OPENAI_COMPATIBLE_KINDS = ("openai", "azure_openai")
_MIME_BY_SUFFIX = {
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
    "opus": "audio/ogg",
    "aac": "audio/aac",
    "flac": "audio/flac",
}

#: 模块级音色缓存：``key -> {"expires": monotonic, "items": [...], "source": "api"|"static"}``。
_VOICE_CACHE: dict[str, dict] = {}

#: 小米 MiMo 固定音色（``app.ai.audio.MIMO_TTS_VOICES``）与中文显示名。
MIMO_VOICE_LABELS = {
    "mimo_default": "默认音色",
    "冰糖": "冰糖（清甜）",
    "茉莉": "茉莉（温柔）",
    "苏打": "苏打（活泼）",
    "白桦": "白桦（沉稳）",
    "Mia": "Mia（英文女声）",
    "Chloe": "Chloe（英文女声）",
    "Milo": "Milo（英文男声）",
    "Dean": "Dean（英文男声）",
}

#: MiniMax 内置音色（``/get_voice`` 不可用时回退；ID 与官方 system voice 一致）。
MINIMAX_STATIC_VOICES = (
    ("female-shaonv", "少女音色", "zh-CN"),
    ("male-qn-qingse", "青涩青年音色", "zh-CN"),
    ("male-qn-jingying", "精英青年音色", "zh-CN"),
    ("male-qn-badao", "霸道青年音色", "zh-CN"),
    ("male-qn-daxuesheng", "青年大学生音色", "zh-CN"),
    ("female-yujie", "御姐音色", "zh-CN"),
    ("female-chengshu", "成熟女性音色", "zh-CN"),
    ("female-tianmei", "甜美女性音色", "zh-CN"),
    ("presenter_male", "男性主持人", "zh-CN"),
    ("presenter_female", "女性主持人", "zh-CN"),
    ("audiobook_male_1", "男性有声书 1", "zh-CN"),
    ("audiobook_female_1", "女性有声书 1", "zh-CN"),
)

#: OpenAI 兼容 ``/audio/speech`` 的默认音色（官方常用 6 个；默认 alloy）。
OPENAI_STATIC_VOICES = (
    "alloy", "echo", "fable", "onyx", "nova", "shimmer",
)



def _has_audio_capability(model_row) -> bool:
    if not isinstance(model_row, dict):
        return False
    caps = cdb.json_load(model_row.get("caps_json"), {})
    if not isinstance(caps, dict):
        return False
    if caps.get("audio_speech") or caps.get("tts") or caps.get("audio"):
        return True
    # 未打能力标签时按模型名兜底：`mimo-v2.5-tts`、`speech-02-turbo` 等语音合成分支。
    from ..ai.audio import is_tts_model

    return is_tts_model(model_row.get("model_id") or "")


def tts_candidates(user_id: int) -> list[dict]:
    """返回 ``[{provider, model}]``：用户已配置、且具备语音合成能力的连接（按优先级）。"""
    from .. import db

    with db.connection() as con:
        providers = [dict(row) for row in con.execute(
            "SELECT * FROM ai_providers WHERE user_id=? ORDER BY id", (user_id,)
        ).fetchall()]
        model_rows = [dict(row) for row in con.execute(
            "SELECT * FROM ai_models WHERE user_id=? AND enabled=1", (user_id,)
        ).fetchall()]
    by_provider: dict[int, dict[str, dict]] = {}
    for row in model_rows:
        by_provider.setdefault(int(row["provider_id"]), {})[row["model_id"]] = row
    candidates: list[dict] = []
    for provider in providers:
        models = [str(item) for item in (cdb.json_load(provider.get("models_json"), []) or []) if str(item).strip()]
        meta = by_provider.get(int(provider["id"]), {})
        flagged = [model for model in models if _has_audio_capability(meta.get(model))]
        if flagged:
            candidates.append({"provider": provider, "model": flagged[0], "explicit": True})
            continue
        if str(provider.get("kind") or "").lower() not in _OPENAI_COMPATIBLE_KINDS:
            continue
        marked = [model for model in models if any(marker in model.lower() for marker in _TTS_MODEL_MARKERS)]
        if marked:
            candidates.append({"provider": provider, "model": marked[0], "explicit": False})
    return candidates


def select_candidate(user_id: int, provider_id: int | None = None) -> dict | None:
    """通用朗读/课堂共用的连接选择：指定连接必须存在，否则取第一个。"""
    candidates = tts_candidates(user_id)
    if provider_id:
        for item in candidates:
            if int(item["provider"]["id"]) == int(provider_id):
                return item
        return None
    return candidates[0] if candidates else None


def tts_provider_info(user_id: int) -> dict | None:
    """``GET /meta`` 用：当前用户可用的 TTS 连接摘要；没有则 None。"""
    candidates = tts_candidates(user_id)
    if not candidates:
        if speech_local.tts_available():
            return {
                "provider_id": None,
                "provider": "local",
                "model": speech_local.model_label(),
            }
        return None
    candidate = candidates[0]
    provider = candidate["provider"]
    return {
        "provider_id": int(provider["id"]),
        "provider": provider.get("name") or "",
        "model": candidate["model"],
    }


# ---------------------------------------------------------------------------
# P4 音色列表：MiniMax /get_voice（缓存 10 分钟）+ 静态回退 / 小米固定 9 音色 / OpenAI 默认
# ---------------------------------------------------------------------------

def _audio_adapter():
    from ..ai import audio as ai_audio

    return ai_audio


def default_voice_for(provider: dict | None) -> str:
    """连接对应的默认音色（用于未显式选择音色时的兜底与列表标注）。"""
    audio = _audio_adapter()
    if provider:
        if audio.is_mimo_provider(provider):
            return audio.DEFAULT_MIMO_VOICE
        if audio.is_minimax_provider(provider):
            return audio.MINIMAX_DEFAULT_VOICE
    return DEFAULT_VOICE


def normalize_voice_for(provider: dict | None, voice: str | None) -> str:
    """按厂商校验/回退音色：本地用音色序号，MiMo/MiniMax 用各自规则，其余透传。"""
    if provider and str(provider.get("kind") or "") == "local":
        return speech_local.normalize_voice(voice)
    audio = _audio_adapter()
    mimo = bool(provider and audio.is_mimo_provider(provider))
    minimax = bool(provider and audio.is_minimax_provider(provider))
    return audio._normalize_voice(voice, mimo=mimo, minimax=minimax)


def clear_voice_cache() -> None:
    """清空模块级音色缓存（测试或用户在设置页刷新音色时使用）。"""
    _VOICE_CACHE.clear()


def _cache_get(key: str) -> dict | None:
    entry = _VOICE_CACHE.get(key)
    if not entry:
        return None
    if float(entry.get("expires") or 0) <= time.monotonic():
        _VOICE_CACHE.pop(key, None)
        return None
    return entry


def _cache_put(key: str, items: list[dict], source: str) -> None:
    _VOICE_CACHE[key] = {
        "expires": time.monotonic() + VOICES_CACHE_TTL_SECONDS,
        "items": [dict(item) for item in items],
        "source": source,
    }


def _voice_item(*, provider: dict, model: str, voice_id: str, name: str,
                lang: str = "zh-CN", source: str = "static", default: bool = False) -> dict:
    provider_name = str(provider.get("name") or "")
    return {
        "provider_id": int(provider["id"]),
        "provider": provider_name,
        "model": str(model or ""),
        "voice_id": str(voice_id),
        "name": str(name or voice_id),
        "lang": str(lang or "zh-CN"),
        "source": source,
        "default": bool(default),
    }


def _static_voices_for(provider: dict, model: str) -> tuple[list[dict], str]:
    """连接的内置音色列表（MiniMax / MiMo / OpenAI 兼容），返回 ``(items, provider_kind)``。"""
    audio = _audio_adapter()
    default_voice = default_voice_for(provider)
    if audio.is_minimax_provider(provider):
        items = [
            _voice_item(provider=provider, model=model, voice_id=voice_id, name=name, lang=lang,
                        source="static", default=voice_id == default_voice)
            for voice_id, name, lang in MINIMAX_STATIC_VOICES
        ]
        return items, "minimax"
    if audio.is_mimo_provider(provider):
        items = [
            _voice_item(provider=provider, model=model, voice_id=voice_id,
                        name=MIMO_VOICE_LABELS.get(voice_id, voice_id),
                        source="static", default=voice_id == default_voice)
            for voice_id in audio.MIMO_TTS_VOICES
        ]
        return items, "mimo"
    items = [
        _voice_item(provider=provider, model=model, voice_id=voice_id,
                    name=voice_id, lang="", source="static", default=voice_id == default_voice)
        for voice_id in OPENAI_STATIC_VOICES
    ]
    return items, "openai"


def _parse_minimax_voice_list(payload) -> list[dict]:
    """把 MiniMax ``/get_voice`` 响应解析成音色条目（未知结构返回空列表）。"""
    if not isinstance(payload, dict):
        return []
    base_resp = payload.get("base_resp") or {}
    if isinstance(base_resp, dict) and base_resp and int(base_resp.get("status_code") or 0) != 0:
        raise ValueError("音色服务错误：%s" % (base_resp.get("status_msg") or "未知错误"))
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    raw_list = payload.get("voice_list") or data.get("voice_list") or []
    if not isinstance(raw_list, list):
        return []
    items: list[dict] = []
    seen: set[str] = set()
    for raw in raw_list:
        if not isinstance(raw, dict):
            continue
        voice_id = str(raw.get("voice_id") or raw.get("voiceId") or "").strip()
        if not voice_id or voice_id in seen:
            continue
        seen.add(voice_id)
        name = str(raw.get("voice_name") or raw.get("voiceName") or voice_id).strip()
        lang = str(raw.get("language") or raw.get("lang") or "zh-CN").strip() or "zh-CN"
        items.append({"voice_id": voice_id, "name": name or voice_id, "lang": lang})
    return items


async def _fetch_minimax_voices(provider: dict, model: str) -> list[dict]:
    """调用 MiniMax ``/get_voice`` 拉取账号可用音色；失败抛 ValueError 由调用方回退静态列表。"""
    from ..ai import httpx as ai_httpx
    from ..ai.providers import _decrypt_key, _validate_url, provider_extra_headers

    base_url = _validate_url(provider.get("base_url") or "", resolve=True)
    endpoint = netguard.join_endpoint(base_url, "/get_voice")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    api_key = _decrypt_key(provider.get("encrypted_key") or "")
    if api_key:
        headers["Authorization"] = "Bearer %s" % api_key
    try:
        headers.update(provider_extra_headers(provider) or {})
    except Exception:  # 自定义头非法时按无自定义头处理，继续尝试请求
        pass
    client_kwargs = netguard.client_kwargs(endpoint, timeout_seconds=VOICE_QUERY_TIMEOUT_SECONDS)
    async with ai_httpx.AsyncClient(**client_kwargs) as client:
        response = await client.post(endpoint, headers=headers, json={"voice_type": "all"})
    if not 200 <= response.status_code < 300:
        raise ValueError("音色服务返回 HTTP %s" % response.status_code)
    try:
        payload = response.json()
    except Exception as exc:  # noqa: BLE001
        raise ValueError("音色服务返回结构无法解析") from exc
    raw_items = _parse_minimax_voice_list(payload)
    default_voice = default_voice_for(provider)
    return [
        _voice_item(provider=provider, model=model, voice_id=item["voice_id"], name=item["name"],
                    lang=item["lang"], source="api", default=item["voice_id"] == default_voice)
        for item in raw_items
    ]


async def list_voices(user_id: int, *, provider_id: int | None = None) -> dict:
    """聚合当前账号所有 TTS 连接的可用音色（MiniMax 走 API + 10 分钟缓存，失败回退静态）。

    返回 ``{items, total, providers, tts, cached, warnings, ttl_seconds}``；没有任何 TTS 连接时
    ``items`` 为空并给出中文警告（前端据此禁用旁白合成并引导去模型设置）。
    """
    candidates = tts_candidates(user_id)
    local_filter = provider_id is None or int(provider_id) == 0
    if provider_id is not None and int(provider_id) != 0:
        wanted = int(provider_id)
        candidates = [item for item in candidates if int(item["provider"]["id"]) == wanted]
    if not candidates and local_filter and speech_local.tts_available():
        try:
            # 音色列表需要加载模型读取 num_speakers：放线程池，避免阻塞事件循环。
            local_items = await asyncio.to_thread(speech_local.list_voices)
        except Exception as exc:  # 本地模型加载失败：不 500，回退空列表 + 中文警告
            return {
                "items": [],
                "total": 0,
                "providers": [],
                "tts": tts_provider_info(user_id),
                "cached": False,
                "warnings": [f"本地语音模型不可用：{str(exc)[:120]}"],
                "ttl_seconds": VOICES_CACHE_TTL_SECONDS,
            }
        items = [
            {
                "provider_id": None,
                "provider": "本地语音",
                "model": speech_local.model_label(),
                "voice_id": str(item["voice_id"]),
                "name": str(item.get("name") or item["voice_id"]),
                "lang": str(item.get("lang") or "zh-CN"),
                "source": "local",
                "default": bool(item.get("default")),
            }
            for item in local_items
        ]
        return {
            "items": items,
            "total": len(items),
            "providers": [{
                "provider_id": None,
                "provider": "本地语音",
                "model": speech_local.model_label(),
                "source": "local",
                "count": len(items),
            }],
            "tts": tts_provider_info(user_id),
            "cached": False,
            "warnings": [],
            "ttl_seconds": VOICES_CACHE_TTL_SECONDS,
        }
    items: list[dict] = []
    providers: list[dict] = []
    warnings: list[str] = []
    cached_any = False
    seen: set[tuple[int, str]] = set()
    for candidate in candidates:
        provider = candidate["provider"]
        model = str(candidate["model"])
        key = "voices:%s:%s:%s" % (provider.get("kind") or "", provider["id"], model)
        entry = _cache_get(key)
        source = "static"
        provider_items: list[dict]
        if entry is not None:
            provider_items = [dict(item) for item in entry.get("items") or []]
            source = str(entry.get("source") or "static")
            cached_any = True
        elif _audio_adapter().is_minimax_provider(provider):
            try:
                provider_items = await _fetch_minimax_voices(provider, model)
                source = "api"
                if not provider_items:
                    raise ValueError("音色服务未返回可用音色")
            except Exception as exc:  # noqa: BLE001 - 任何失败都回退静态列表，不影响其它连接
                provider_items, _kind = _static_voices_for(provider, model)
                source = "static"
                warnings.append("「%s」音色列表读取失败，已使用内置音色（%s）"
                                % (provider.get("name") or "MiniMax", str(exc)[:120]))
            _cache_put(key, provider_items, source)
        else:
            provider_items, _kind = _static_voices_for(provider, model)
            _cache_put(key, provider_items, "static")
        providers.append({
            "provider_id": int(provider["id"]),
            "provider": provider.get("name") or "",
            "model": model,
            "source": source,
            "count": len(provider_items),
        })
        for item in provider_items:
            dedupe = (int(item["provider_id"]), str(item["voice_id"]))
            if dedupe in seen:
                continue
            seen.add(dedupe)
            items.append(item)
    if not candidates:
        warnings.append(
            "还没有可用的语音合成连接：请在「模型设置」添加支持 TTS 的连接"
            "（小米 MiMo / MiniMax / OpenAI 兼容语音），或继续使用浏览器朗读。"
        )
    return {
        "items": items,
        "total": len(items),
        "providers": providers,
        "tts": tts_provider_info(user_id),
        "cached": cached_any,
        "warnings": warnings,
        "ttl_seconds": VOICES_CACHE_TTL_SECONDS,
    }



async def synthesize_provider(provider: dict, model: str, text: str, *,
                              voice: str | None = None,
                              speed: float | None = None) -> tuple[bytes, str]:
    """公开的厂商合成入口（通用朗读复用；课堂内部仍走 :func:`_request_speech`）。"""
    return await _request_speech(provider, model, text, voice=voice, speed=speed)


async def _request_speech(provider: dict, model: str, text: str, *, voice: str | None = None,
                          speed: float | None = None) -> tuple[bytes, str]:
    """合成语音；小米 MiMo 走 chat 复合模态，其余厂商走 OpenAI 兼容 ``/audio/speech``。"""
    from ..ai import audio as ai_audio

    if ai_audio.is_mimo_provider(provider) or ai_audio.is_minimax_provider(provider):
        # 小米 MiMo：``chat/completions`` + assistant 角色 + modalities=["audio"]；
        # MiniMax：``/t2a_v2``（hex 音频）。音色由 ``_normalize_voice`` 校验并回退厂商默认值。
        return await ai_audio.synthesize(
            provider, model, text, voice=voice, fmt="mp3", timeout=TTS_TIMEOUT_SECONDS,
        )
    from ..ai import httpx as ai_httpx
    from ..ai.providers import _decrypt_key, _validate_url, provider_extra_headers

    base_url = _validate_url(provider.get("base_url") or "", resolve=True)
    endpoint = netguard.join_endpoint(base_url, "/audio/speech")
    headers = {"Content-Type": "application/json", "Accept": "audio/*"}
    api_key = _decrypt_key(provider.get("encrypted_key") or "")
    if api_key:
        headers["Authorization"] = "Bearer %s" % api_key
    try:
        headers.update(provider_extra_headers(provider) or {})
    except Exception:  # 自定义头非法时按无自定义头处理，失败最终回退浏览器
        pass
    body = {
        "model": model,
        "input": text,
        "voice": voice or DEFAULT_VOICE,
        "response_format": "mp3",
    }
    if speed:
        body["speed"] = speed
    client_kwargs = netguard.client_kwargs(endpoint, timeout_seconds=TTS_TIMEOUT_SECONDS)
    async with ai_httpx.AsyncClient(**client_kwargs) as client:
        response = await client.post(endpoint, headers=headers, json=body)
        if not 200 <= response.status_code < 300:
            raise ValueError("语音服务返回 HTTP %s" % response.status_code)
        content = response.content
        if not content:
            raise ValueError("语音服务返回空音频")
        mime = (response.headers.get("content-type") or "audio/mpeg").split(";")[0].strip()
        return content, mime or "audio/mpeg"


def _save_audio_asset(user_id: int, course_id: int, audio: bytes, mime: str,
                      *, voice: str | None = None) -> dict:
    """内容寻址落库：同用户同 sha256 的音频只存一份。"""
    from .. import db

    digest = hashlib.sha256(audio).hexdigest()
    with db.connection() as con:
        existing = con.execute(
            "SELECT * FROM cls_assets WHERE user_id=? AND kind=? AND sha256=? ORDER BY id LIMIT 1",
            (user_id, TTS_KIND, digest),
        ).fetchone()
    if existing is not None:
        return dict(existing)
    suffix = "mp3"
    for candidate, candidate_mime in _MIME_BY_SUFFIX.items():
        if candidate_mime == mime:
            suffix = candidate
            break
    storage_name = "tts-%s.%s" % (digest[:32], suffix)
    storage.save_file(TTS_FOLDER, storage_name, audio, mime)
    now = db.utcnow()
    with db.connection() as con:
        asset_id = con.execute(
            "INSERT INTO cls_assets(user_id,course_id,kind,storage_name,mime_type,sha256,meta_json,created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (user_id, course_id, TTS_KIND, storage_name, mime, digest,
             cdb.json_dump({"voice": voice or DEFAULT_VOICE, "size": len(audio)}), now),
        ).lastrowid
        row = con.execute("SELECT * FROM cls_assets WHERE id=?", (asset_id,)).fetchone()
    return dict(row)


def asset_url(asset_id: int) -> str:
    return "/api/classroom/assets/%d" % int(asset_id)


def asset_public(row) -> dict:
    return {
        "id": int(row["id"]),
        "kind": row["kind"],
        "mime_type": row["mime_type"],
        "url": asset_url(row["id"]),
        "meta": cdb.json_load(row.get("meta_json"), {}),
        "created_at": row["created_at"],
    }


async def _local_synthesize(*, user_id: int, course_id: int, text: str,
                            voice: str | None, speed: float | None) -> dict:
    """零厂商配置时的本地合成；失败落回浏览器朗读并保留中文原因。

    合成成功先经过阶段 4-4 的压缩（MP3/Opus，失败回退 WAV），再按内容寻址落
    ``cls_assets``；缓存键由 sha256 天然包含格式与音色。
    """
    from ..ai import audio_compress

    effective_voice = speech_local.normalize_voice(voice)
    try:
        audio, mime = await asyncio.wait_for(
            asyncio.to_thread(speech_local.synthesize, text, effective_voice, speed),
            timeout=speech_local.SYNTH_TIMEOUT_SECONDS,
        )
        audio, mime, audio_format = audio_compress.compress_speech(audio, mime)
        asset = _save_audio_asset(user_id, course_id, audio, mime, voice=effective_voice)
    except Exception as exc:  # 本地失败与厂商失败同口径：回退浏览器，不 500
        log.warning("Local classroom TTS failed, falling back to browser: %s", type(exc).__name__)
        return {"fallback": "browser", "error": "本地语音合成失败：%s" % str(exc)[:160]}
    return {
        "asset": asset_public(asset),
        "provider": "local",
        "model": speech_local.model_label(),
        "voice": effective_voice,
        "format": audio_format,
    }


async def synthesize(*, user_id: int, course_id: int, text: str, voice: str | None = None,
                     speed: float | None = None, provider_id: int | None = None,
                     model: str | None = None) -> dict:
    """合成语音；返回 ``{"asset": {...}, "provider": "..."}`` 或 ``{"fallback": "browser"}``。"""
    normalized = str(text or "").strip()
    if not normalized:
        return {"fallback": "browser", "error": "旁白文本为空"}
    if len(normalized) > MAX_TTS_CHARS:
        return {"fallback": "browser", "error": "单次语音合成文本不能超过 %d 字" % MAX_TTS_CHARS}
    candidates = tts_candidates(user_id)
    candidate = None
    if provider_id:
        for item in candidates:
            if int(item["provider"]["id"]) == int(provider_id):
                candidate = item
                break
        if candidate is None:
            return {"fallback": "browser", "error": "所选连接未配置语音合成能力"}
    else:
        candidate = candidates[0] if candidates else None
    if candidate is None:
        # 只允许「未指定模型」或「本地模型标签」走本地；任意厂商模型在无连接时保持失败。
        if model and str(model).strip() != speech_local.model_label():
            return {"fallback": "browser", "error": "所选语音模型未配置可用的合成连接"}
        if speech_local.tts_available():
            return await _local_synthesize(
                user_id=user_id, course_id=course_id, text=normalized,
                voice=voice, speed=speed,
            )
        return {"fallback": "browser"}
    provider = candidate["provider"]
    target_model = str(model or candidate["model"])
    speed_value = max(0.5, min(2.0, float(speed))) if speed is not None else DEFAULT_SPEED
    # 音色按厂商规范化：小米只接受内置音色、MiniMax 接受官方 voice_id；未指定时用连接默认音色。
    effective_voice = normalize_voice_for(provider, str(voice).strip() if voice else None)
    try:
        audio, mime = await _request_speech(
            provider, target_model, normalized,
            voice=effective_voice, speed=speed_value,
        )
        asset = _save_audio_asset(
            user_id, course_id, audio, mime, voice=effective_voice,
        )
    except Exception as exc:  # 任何失败都回退浏览器语音，绝不 500
        log.warning("Classroom TTS failed, falling back to browser: %s", type(exc).__name__)
        return {"fallback": "browser", "error": "语音合成失败：%s" % str(exc)[:160]}
    return {
        "asset": asset_public(asset),
        "provider": provider.get("name") or "openai-compatible",
        "model": target_model,
        "voice": effective_voice,
    }


__all__ = [
    "DEFAULT_SPEED", "DEFAULT_VOICE", "MAX_TTS_CHARS", "MINIMAX_STATIC_VOICES",
    "MIMO_VOICE_LABELS", "OPENAI_STATIC_VOICES", "TTS_FOLDER", "TTS_KIND",
    "VOICES_CACHE_TTL_SECONDS", "asset_public", "asset_url", "clear_voice_cache",
    "default_voice_for", "list_voices", "normalize_voice_for", "select_candidate",
    "synthesize", "synthesize_provider", "tts_candidates", "tts_provider_info",
]
