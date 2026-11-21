"""AI 课堂可选配图：OpenAI 兼容 ``/images/generations``（P2 image slot）。

分层策略（与 :mod:`app.classroom.tts` 对齐，复用既有 provider 加密配置与 netguard）：

- 没有可用图片连接 / 用户关闭 → **不生成**，课程保持纯文本（绝不造假图）；
- 有连接时为 slide 场景生成 0-2 张教学配图，图片字节按 sha256 内容寻址写入
  ``cls_assets``（同用户同图只存一份），slide image 元素引用 ``/api/classroom/assets/{id}``；
- **任何失败都只记 warning 并跳过**，不阻塞课程生成任务、不 500。

本模块同时提供课堂素材的两条 P1 加固路径：

- **类型控制**：按字节内容识别真实类型（:func:`sniff_media_type` / :func:`asset_serving_plan` /
  :func:`classify_import_media`），只允许白名单媒体内联返回，SVG/HTML/PDF 等附件强制
  ``Content-Disposition: attachment`` 隔离下载，主动内容降级为二进制；
- **引用与回收**：共享素材（图片/TTS/视频）按内容寻址复用，删课时现查其它课程/场景的存活
  引用（:func:`asset_references`），只回收无引用素材（:func:`reclaim_course_assets`），
  并为页面缺失素材提供降级信息（:func:`missing_scene_asset_ids`）。

能力探测：优先 ``ai_models.caps_json`` 的 ``image``/``image_generation``/``images`` 标记，
其次对 OpenAI 兼容连接按模型名启发式（dall-e / gpt-image / flux / stable-diffusion / imagen / …）。
"""
from __future__ import annotations

import base64
import hashlib
import io
import logging
import re

from .. import storage
from ..ai import netguard
from . import db as cdb
from . import tts

log = logging.getLogger("app.classroom.images")

IMAGE_KIND = "image"
IMAGE_FOLDER = cdb.ASSET_FOLDER
DEFAULT_SIZE = "1024x1024"
MAX_IMAGES_PER_SCENE = 2
MAX_IMAGE_BYTES = 24 * 1024 * 1024
IMAGE_TIMEOUT_SECONDS = 90
DOWNLOAD_TIMEOUT_SECONDS = 60
#: 参考图（图生图 / 图片编辑）上限：输入与压缩后均不超过 8MB。
MAX_REFERENCE_BYTES = 8 * 1024 * 1024
#: 参考图压缩参数：最长边 1600、JPEG 质量 85（与服务端出图尺寸 1024-1536 对齐）。
REFERENCE_MAX_SIDE = 1600
REFERENCE_JPEG_QUALITY = 85
#: 单门课程最多在 config 中保留的「已生成配图」摘要条数（避免课程行过大）。
MAX_REMEMBERED_IMAGES = 20
_OPENAI_COMPATIBLE_KINDS = ("openai", "azure_openai")
_IMAGE_MODEL_MARKERS = (
    "dall-e", "dalle", "gpt-image", "flux", "stable-diffusion", "stablediffusion",
    "sd3", "sdxl", "imagen", "cogview", "kolors", "seedream", "wanx", "qwen-image",
    "image",
)
_MIME_BY_SUFFIX = {
    "png": "image/png", "jpeg": "image/jpeg", "jpg": "image/jpeg", "webp": "image/webp",
    "gif": "image/gif",
}

#: 可内联返回的图片类型（按内容识别，与前端 ``<img>`` 支持范围一致）。
INLINE_IMAGE_TYPES = ("image/png", "image/jpeg", "image/webp", "image/gif")
#: 课堂播放所需的音频类型（由本平台 TTS/旁白管线生成，前端用 ``<audio>`` 内联播放）。
INLINE_AUDIO_TYPES = (
    "audio/mpeg", "audio/mp3", "audio/wav", "audio/x-wav", "audio/ogg", "audio/opus",
    "audio/aac", "audio/flac",
)
#: 课堂视频类型（由本平台 ffmpeg 渲染管线生成）。
INLINE_VIDEO_TYPES = ("video/mp4",)
#: 允许内联返回的白名单；其它类型一律走 ``Content-Disposition: attachment`` 隔离下载。
INLINE_MEDIA_TYPES = frozenset(INLINE_IMAGE_TYPES + INLINE_AUDIO_TYPES + INLINE_VIDEO_TYPES)
#: 可执行/主动内容：不能内联，且下载时统一降级为二进制，避免浏览器解释执行。
ACTIVE_CONTENT_TYPES = frozenset({
    "text/html", "application/xhtml+xml", "image/svg+xml", "application/xml",
    "text/xml", "application/javascript", "text/javascript", "text/css",
})
#: 服务端自身管线生成的素材 kind（仅用于内容无法识别时的保守回退，不信任导入声明）。
TRUSTED_ASSET_KINDS = frozenset({"tts", "video"})
_EXTENSION_BY_MIME = {
    "image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif",
    "audio/mpeg": ".mp3", "audio/mp3": ".mp3", "audio/wav": ".wav", "audio/x-wav": ".wav",
    "audio/ogg": ".ogg", "audio/opus": ".opus", "audio/aac": ".aac", "audio/flac": ".flac",
    "video/mp4": ".mp4", "application/pdf": ".pdf", "application/zip": ".zip",
    "text/plain": ".txt", "application/json": ".json",
}


# ---------------------------------------------------------------------------
# 能力探测（复用 ai_providers / ai_models，不新增依赖）
# ---------------------------------------------------------------------------

def _has_image_capability(model_row) -> bool:
    if not isinstance(model_row, dict):
        return False
    caps = cdb.json_load(model_row.get("caps_json"), {})
    if not isinstance(caps, dict):
        return False
    return bool(caps.get("image") or caps.get("image_generation") or caps.get("images"))


def image_candidates(user_id: int) -> list[dict]:
    """返回 ``[{provider, model, explicit}]``：用户已配置、具备图片生成能力的连接（按优先级）。"""
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
        flagged = [model for model in models if _has_image_capability(meta.get(model))]
        if flagged:
            candidates.append({"provider": provider, "model": flagged[0], "explicit": True})
            continue
        if str(provider.get("kind") or "").lower() not in _OPENAI_COMPATIBLE_KINDS:
            continue
        marked = [
            model for model in models
            if any(marker in model.lower() for marker in _IMAGE_MODEL_MARKERS)
        ]
        if marked:
            candidates.append({"provider": provider, "model": marked[0], "explicit": False})
    return candidates


def _reference_capability() -> dict:
    """``/meta.images`` 的参考图能力位（前端据此显示「参考图」上传区）。"""
    return {
        "max_side": REFERENCE_MAX_SIDE,
        "jpeg_quality": REFERENCE_JPEG_QUALITY,
        "max_bytes": MAX_REFERENCE_BYTES,
        "formats": ["image/jpeg", "image/png", "image/webp"],
    }


def image_provider_info(user_id: int) -> dict:
    """``GET /meta`` 用：当前用户可用的配图连接摘要（没有也返回 enabled=false 结构）。"""
    candidates = image_candidates(user_id)
    if not candidates:
        return {
            "enabled": False,
            "provider_id": None,
            "provider": None,
            "model": None,
            "max_per_scene": MAX_IMAGES_PER_SCENE,
            "size": DEFAULT_SIZE,
            "supports_reference": True,
            "reference": _reference_capability(),
        }
    candidate = candidates[0]
    provider = candidate["provider"]
    return {
        "enabled": True,
        "provider_id": int(provider["id"]),
        "provider": provider.get("name") or "",
        "model": str(candidate["model"]),
        "max_per_scene": MAX_IMAGES_PER_SCENE,
        "size": DEFAULT_SIZE,
        "supports_reference": True,
        "reference": _reference_capability(),
    }


def resolve_candidate(user_id: int, provider_id: int | None = None,
                      model: str | None = None) -> dict | None:
    """解析本次配图使用的连接：显式指定优先，否则取第一个可用候选；都没有返回 None。"""
    if provider_id is not None or model:
        if not provider_id or not model:
            return None
        from ..ai.providers import _selected_provider

        provider = _selected_provider(user_id, provider_id, model)
        return {"provider": provider, "model": model, "explicit": True}
    candidates = image_candidates(user_id)
    return candidates[0] if candidates else None


# ---------------------------------------------------------------------------
# HTTP（OpenAI 兼容 /images/generations）
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 素材类型识别（按内容解码，不信课程包/上游声明）
# ---------------------------------------------------------------------------

def _normalize_mime(value) -> str:
    return str(value or "").split(";")[0].strip().lower()


def _looks_like_text(sample: bytes) -> bool:
    if not sample:
        return False
    for encoding in ("utf-8", "gbk", "latin-1"):
        try:
            text = sample.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
        if not text:
            return False
        printable = sum(1 for char in text if char.isprintable() or char in "\r\n\t")
        return printable / len(text) >= 0.9
    return False


def sniff_media_type(data: bytes) -> str:
    """按字节内容（魔数/结构）识别素材真实类型；无法可靠识别时返回 ``application/octet-stream``。

    只用于服务端判断，**不采信**客户端/课程包声明的 ``mimeType``；识别范围覆盖现有课堂素材
    （PNG/JPEG/WEBP/GIF、MP3/WAV/OGG/OPUS/AAC/FLAC、MP4），以及需要隔离的 SVG/HTML/XML 等主动内容。
    """
    raw = bytes(data or b"")
    if not raw:
        return "application/octet-stream"
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    if raw[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WAVE":
        return "audio/wav"
    if raw[:4] == b"fLaC":
        return "audio/flac"
    if raw[:4] == b"OggS":
        head = raw[:512]
        if b"OpusHead" in head:
            return "audio/opus"
        return "audio/ogg"
    if raw[:3] == b"ID3" or (len(raw) >= 2 and raw[0] == 0xFF and (raw[1] & 0xE0) == 0xE0):
        # MPEG 音频帧同步；ADTS 的 0xF0 掩码与 MPEG 有交集，按更具体的 ADTS 判定。
        if len(raw) >= 2 and (raw[1] & 0xF6) == 0xF0:
            return "audio/aac"
        return "audio/mpeg"
    if len(raw) >= 8 and raw[4:8] == b"ftyp":
        brand = raw[8:12]
        if brand in (b"avif", b"avis"):
            return "image/avif"
        if brand in (b"heic", b"heix", b"mif1", b"msf1"):
            return "image/heic"
        if brand == b"qt  ":
            return "video/quicktime"
        return "video/mp4"
    if raw[:4] == b"\x1aE\xdf\xa3":
        return "video/webm"
    if raw[:5] == b"%PDF-":
        return "application/pdf"
    if raw[:2] == b"PK":
        return "application/zip"
    head = raw[:2048].lstrip(b"\xef\xbb\xbf \t\r\n")
    lowered = head[:1024].lower()
    if b"<svg" in lowered:
        return "image/svg+xml"
    if (
        lowered.startswith(b"<?xml") or lowered.startswith(b"<!doctype html")
        or lowered.startswith(b"<html") or b"<html" in lowered
        or b"<script" in lowered or b"javascript:" in lowered
    ):
        # SVG/HTML/XML 等主动内容：识别为对应类型，由服务端强制隔离下载（绝不内联）。
        return "application/xml" if lowered.startswith(b"<?xml") else "text/html"
    if _looks_like_text(raw[:512]):
        return "text/plain"
    return "application/octet-stream"


def sniff_image_type(data: bytes) -> str:
    """返回按内容识别的白名单图片类型；不是支持的图片格式时返回空字符串。"""
    detected = sniff_media_type(data)
    return detected if detected in INLINE_IMAGE_TYPES else ""


def inline_media_type(detected: str) -> str:
    """可内联返回的真实类型；不在白名单时返回空字符串。"""
    mime = _normalize_mime(detected)
    return mime if mime in INLINE_MEDIA_TYPES else ""


def download_media_type(detected: str) -> str:
    """隔离下载用的 media type：主动内容统一降级为二进制，其余按真实类型返回。"""
    mime = _normalize_mime(detected)
    if mime in ACTIVE_CONTENT_TYPES:
        return "application/octet-stream"
    if not mime or mime == "application/octet-stream":
        return "application/octet-stream"
    return mime


def content_disposition(asset_id, media_type: str) -> str:
    """隔离下载的 ``Content-Disposition``（文件名只用 ASCII 安全字符，避免响应头注入）。"""
    extension = _EXTENSION_BY_MIME.get(_normalize_mime(media_type), ".bin")
    try:
        numeric_id = int(asset_id or 0)
    except (TypeError, ValueError):
        numeric_id = 0
    return 'attachment; filename="asset-%d%s"' % (numeric_id, extension)


def asset_serving_plan(asset: dict, content: bytes) -> dict:
    """决定素材响应的 ``media_type`` 与是否内联（``get_asset`` 的唯一类型判定入口）。

    - 按内容识别真实类型，只有白名单媒体（图片 / 本平台生成的音视频）允许内联；
    - 其余（SVG/HTML/PDF/ZIP 等附件）走 ``attachment`` 隔离下载，主动内容降级为二进制；
    - 仅当内容无法识别（octet-stream）且是本平台自身管线的 ``tts``/``video`` 素材时，才
      保守回退到库内记录的类型，保证旧素材仍可播放。
    """
    raw = bytes(content or b"")
    detected = sniff_media_type(raw)
    inline = inline_media_type(detected)
    kind = str((asset or {}).get("kind") or "").strip().lower()
    stored = _normalize_mime((asset or {}).get("mime_type"))
    if not inline and detected == "application/octet-stream" and kind in TRUSTED_ASSET_KINDS \
            and stored in INLINE_MEDIA_TYPES:
        detected = stored
        inline = stored
    if inline:
        return {"inline": True, "media_type": inline, "detected_mime": detected, "disposition": ""}
    media_type = download_media_type(detected)
    return {
        "inline": False,
        "media_type": media_type,
        "detected_mime": detected,
        "disposition": content_disposition((asset or {}).get("id"), media_type),
    }


def classify_import_media(declared_mime, data: bytes) -> dict:
    """课程包导入的类型控制：按内容识别真实类型，不采信包内声明。

    返回 ``{declared_mime, detected_mime, mime_type, inline_allowed, warnings}``：

    - ``mime_type``：实际落库/写存储用的类型（主动内容降级为 ``application/octet-stream``）；
    - ``inline_allowed``：是否属于允许内联返回的白名单；
    - 声明与内容不符时给出中文 ``warnings``（导入仍继续，降级为隔离下载）。
    """
    declared = _normalize_mime(declared_mime) or "application/octet-stream"
    detected = sniff_media_type(data)
    inline_allowed = bool(inline_media_type(detected))
    media_type = detected if inline_allowed else download_media_type(detected)
    warnings: list[str] = []
    if declared != detected and declared != "application/octet-stream" \
            and detected != "application/octet-stream":
        warnings.append("素材声明的类型（%s）与内容不符（实际 %s），已按内容类型处理"
                        % (declared, detected))
    if not inline_allowed and declared in INLINE_IMAGE_TYPES:
        warnings.append("素材声明为可内联图片（%s）但内容不是支持的图片格式，已降级为隔离下载"
                        % declared)
    return {
        "declared_mime": declared,
        "detected_mime": detected,
        "mime_type": media_type,
        "inline_allowed": inline_allowed,
        "warnings": warnings,
    }


def _suffix_for(mime: str) -> str:
    mime = str(mime or "").split(";")[0].strip().lower()
    for suffix, candidate in _MIME_BY_SUFFIX.items():
        if candidate == mime:
            return suffix
    return "png"


def _decode_base64_image(value) -> bytes | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.startswith("data:"):
        if "," in text:
            text = text.split(",", 1)[1]
    try:
        return base64.b64decode(text, validate=False)
    except (ValueError, TypeError):
        return None


async def _download_image(url: str) -> bytes:
    """兼容返回 URL 的图片接口：走 netguard 校验下载（限制大小与超时）。"""
    from ..ai import httpx as ai_httpx
    from ..ai.providers import _validate_url

    endpoint = _validate_url(url, resolve=True)
    client_kwargs = netguard.client_kwargs(endpoint, timeout_seconds=DOWNLOAD_TIMEOUT_SECONDS)
    async with ai_httpx.AsyncClient(**client_kwargs) as client:
        response = await client.get(endpoint)
        if not 200 <= response.status_code < 300:
            raise ValueError("配图下载返回 HTTP %s" % response.status_code)
        content = response.content
        if not content:
            raise ValueError("配图下载为空")
        return content


async def _request_image(provider: dict, model: str, prompt: str, *, size: str = DEFAULT_SIZE,
                         reference: bytes | None = None,
                         reference_mime: str = "image/jpeg",
                         warnings: list[str] | None = None) -> tuple[bytes, str]:
    """调用厂商图片生成接口，返回 ``(bytes, mime)``；失败抛异常由调用方降级。

    - MiniMax：``POST /image_generation``（模型 ``image-01`` / ``image-01-live``，base64 返回）；
      ``reference`` 非空时按官方图片编辑协议传 ``subject_reference``（**必须** data URI，
      纯 base64 会被上游当作 URL 拒绝）；
    - 其他：OpenAI 兼容 ``/images/generations``；``reference`` 非空时先尝试 ``/images/edits``，
      失败回退文生图并把原因写入 ``warnings``（不伪造编辑成功）。
    """
    from ..ai import audio as ai_audio
    from ..ai import httpx as ai_httpx
    from ..ai.providers import _decrypt_key, _validate_url, provider_extra_headers

    note = warnings if warnings is not None else []
    base_url = _validate_url(provider.get("base_url") or "", resolve=True)
    api_key = _decrypt_key(provider.get("encrypted_key") or "")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if api_key:
        headers["Authorization"] = "Bearer %s" % api_key
    try:
        headers.update(provider_extra_headers(provider) or {})
    except Exception:  # 自定义头非法时按无自定义头处理，失败最终由调用方降级
        pass

    if ai_audio.is_minimax_provider(provider):
        return await _minimax_image(
            base_url, headers, model, prompt, size,
            client_kwargs=netguard.client_kwargs(base_url, timeout_seconds=IMAGE_TIMEOUT_SECONDS),
            reference=reference, reference_mime=reference_mime,
        )

    if reference:
        try:
            return await _openai_image_edit(
                base_url, headers, model, prompt, size,
                reference=reference, reference_mime=reference_mime,
            )
        except ValueError as exc:
            note.append("图片编辑接口不可用，已回退为文生图（%s）" % str(exc)[:120])

    endpoint = netguard.join_endpoint(base_url, "/images/generations")
    attempts = [
        {"model": model, "prompt": prompt, "n": 1, "size": size, "response_format": "b64_json"},
        {"model": model, "prompt": prompt, "n": 1, "size": size},
        {"model": model, "prompt": prompt, "n": 1},
    ]
    client_kwargs = netguard.client_kwargs(endpoint, timeout_seconds=IMAGE_TIMEOUT_SECONDS)
    last_error = "配图服务不可用"
    async with ai_httpx.AsyncClient(**client_kwargs) as client:
        for body in attempts:
            response = await client.post(endpoint, headers=headers, json=body)
            if 200 <= response.status_code < 300:
                payload = response.json()
                return await _image_from_payload(payload, response.headers.get("content-type") or "")
            text = ""
            try:
                text = (response.text or "")[:300]
            except Exception:
                text = ""
            last_error = "配图服务返回 HTTP %s" % response.status_code
            # 仅对「参数不被支持」做降级重试；鉴权/额度/服务端错误直接失败。
            lowered = text.lower()
            if response.status_code not in (400, 422) or not any(
                marker in lowered for marker in ("response_format", "size", "unsupported", "unknown parameter",
                                                 "invalid parameter", "extra fields")
            ):
                break
    raise ValueError(last_error)


async def _openai_image_edit(base_url: str, headers: dict, model: str, prompt: str, size: str,
                             *, reference: bytes, reference_mime: str = "image/jpeg"):
    """OpenAI 兼容 ``/images/edits``（multipart）：成功返回 ``(bytes, mime)``，失败抛 ValueError。"""
    from ..ai import httpx as ai_httpx

    endpoint = netguard.join_endpoint(base_url, "/images/edits")
    request_headers = {key: value for key, value in headers.items() if key.lower() != "content-type"}
    data = {
        "model": model,
        "prompt": prompt,
        "n": "1",
        "size": size,
        "response_format": "b64_json",
    }
    files = {"image": ("reference.jpg", reference, reference_mime or "image/jpeg")}
    client_kwargs = netguard.client_kwargs(endpoint, timeout_seconds=IMAGE_TIMEOUT_SECONDS)
    async with ai_httpx.AsyncClient(**client_kwargs) as client:
        response = await client.post(endpoint, headers=request_headers, data=data, files=files)
    if not 200 <= response.status_code < 300:
        text = ""
        try:
            text = (response.text or "")[:160]
        except Exception:
            text = ""
        raise ValueError("图片编辑返回 HTTP %s%s" % (
            response.status_code, ("：%s" % text) if text else "",
        ))
    payload = response.json()
    return await _image_from_payload(payload, response.headers.get("content-type") or "")


def _minimax_model(model: str) -> str:
    """MiniMax 图片模型 id 以 ``image`` 开头（``image-01`` / ``image-01-live``）。"""
    value = str(model or "").strip()
    return value if value.lower().startswith("image") else "image-01"


def _aspect_ratio_for(size: str) -> str:
    mapping = {
        "1024x1024": "1:1", "1536x1024": "3:2", "1024x1536": "2:3",
        "1280x720": "16:9", "720x1280": "9:16", "1152x864": "4:3", "864x1152": "3:4",
    }
    return mapping.get(str(size or "").strip().lower(), "1:1")


async def _minimax_image(base_url: str, headers: dict, model: str, prompt: str, size: str,
                         *, client_kwargs: dict, reference: bytes | None = None,
                         reference_mime: str = "image/jpeg") -> tuple[bytes, str]:
    """MiniMax 文生图 / 图片编辑：``data.image_base64`` 或 ``data.image_urls``。

    图片编辑按官方协议传 ``subject_reference=[{"type":"character","image_file":"data:...;base64,..."}]``；
    ``image_file`` 必须是 data URI（实测纯 base64 会被上游当 URL 拒绝）。
    """
    from ..ai import httpx as ai_httpx

    endpoint = netguard.join_endpoint(base_url, "/image_generation")
    body = {
        "model": _minimax_model(model),
        "prompt": prompt,
        "aspect_ratio": _aspect_ratio_for(size),
        "response_format": "base64",
        "n": 1,
    }
    if reference:
        encoded = base64.b64encode(reference).decode("ascii")
        body["subject_reference"] = [{
            "type": "character",
            "image_file": "data:%s;base64,%s" % (reference_mime or "image/jpeg", encoded),
        }]
    async with ai_httpx.AsyncClient(**client_kwargs) as client:
        response = await client.post(endpoint, headers=headers, json=body)
    if not 200 <= response.status_code < 300:
        raise ValueError("配图服务返回 HTTP %s" % response.status_code)
    try:
        payload = response.json()
    except Exception as exc:  # noqa: BLE001
        raise ValueError("配图服务返回结构无法解析") from exc
    base_resp = (payload.get("base_resp") or {}) if isinstance(payload, dict) else {}
    if base_resp and int(base_resp.get("status_code") or 0) != 0:
        raise ValueError("配图服务错误：%s" % (base_resp.get("status_msg") or "未知错误"))
    data = payload.get("data") or {}
    images = data.get("image_base64") or data.get("image") or []
    if isinstance(images, str):
        images = [images]
    raw = _decode_base64_image(images[0]) if images else None
    if raw is None:
        urls = data.get("image_urls") or []
        if isinstance(urls, str):
            urls = [urls]
        if urls:
            raw = await _download_image(urls[0])
    if not raw:
        raise ValueError("配图服务未返回图片数据")
    if len(raw) > MAX_IMAGE_BYTES:
        raise ValueError("配图超过大小上限（%d MB）" % (MAX_IMAGE_BYTES // (1024 * 1024)))
    detected = sniff_image_type(raw)
    if not detected:
        raise ValueError("配图服务返回的内容不是支持的图片格式（PNG/JPEG/WEBP/GIF）")
    return raw, detected


async def _image_from_payload(payload, content_type: str) -> tuple[bytes, str]:
    data = payload.get("data") if isinstance(payload, dict) else None
    item = data[0] if isinstance(data, list) and data and isinstance(data[0], dict) else {}
    image_bytes = _decode_base64_image(
        item.get("b64_json") or item.get("b64") or item.get("image") or item.get("image_base64")
    )
    if image_bytes is None:
        url = str(item.get("url") or "").strip()
        if url:
            image_bytes = await _download_image(url)
    if not image_bytes:
        raise ValueError("配图服务未返回图片数据")
    if len(image_bytes) > MAX_IMAGE_BYTES:
        raise ValueError("配图超过大小上限（%d MB）" % (MAX_IMAGE_BYTES // (1024 * 1024)))
    # 不采信上游/中间层的 content-type：按内容解码识别真实图片类型，非白名单图片直接失败。
    detected = sniff_image_type(image_bytes)
    if not detected:
        raise ValueError("配图服务返回的内容不是支持的图片格式（PNG/JPEG/WEBP/GIF）")
    return image_bytes, detected


# ---------------------------------------------------------------------------
# 参考图压缩（Pillow：最长边 1600 / JPEG 85 / ≤8MB）
# ---------------------------------------------------------------------------

def compress_reference_image(data: bytes) -> tuple[bytes, str]:
    """把用户上传的参考图压缩为 JPEG，返回 ``(bytes, mime)``；非法/超限抛 ValueError（API 转 422）。

    - 最长边压到 :data:`REFERENCE_MAX_SIDE`（1600），不放大；
    - 统一转 JPEG（质量 85），透明背景铺白；
    - 输入与压缩结果都不超过 :data:`MAX_REFERENCE_BYTES`（8MB）。
    """
    raw = bytes(data or b"")
    if not raw:
        raise ValueError("参考图为空，请重新选择图片")
    if len(raw) > MAX_REFERENCE_BYTES:
        raise ValueError("参考图不能超过 %d MB，请压缩后重试" % (MAX_REFERENCE_BYTES // (1024 * 1024)))
    try:
        from PIL import Image, ImageOps
    except ImportError as exc:  # pragma: no cover - 环境已装 Pillow，缺包时给出明确引导
        raise ValueError("服务端缺少图片处理库（Pillow），暂时无法处理参考图") from exc
    resampling = getattr(getattr(Image, "Resampling", Image), "LANCZOS", None)
    try:
        with Image.open(io.BytesIO(raw)) as source:
            image = ImageOps.exif_transpose(source)
            if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
                rgba = image.convert("RGBA")
                background = Image.new("RGB", rgba.size, (255, 255, 255))
                background.paste(rgba, mask=rgba.split()[-1])
                image = background
            else:
                image = image.convert("RGB")
            image.thumbnail((REFERENCE_MAX_SIDE, REFERENCE_MAX_SIDE), resampling or Image.BICUBIC)
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=REFERENCE_JPEG_QUALITY, optimize=True)
            output = buffer.getvalue()
    except ValueError:
        raise
    except Exception as exc:  # noqa: BLE001 - 损坏/非图片统一按可读错误返回
        raise ValueError("参考图无法识别，请上传 JPG / PNG / WEBP 格式的图片") from exc
    if not output:
        raise ValueError("参考图压缩失败，请换一张图片重试")
    if len(output) > MAX_REFERENCE_BYTES:
        raise ValueError("参考图压缩后仍超过 %d MB，请换一张更小的图片"
                         % (MAX_REFERENCE_BYTES // (1024 * 1024)))
    return output, "image/jpeg"


# ---------------------------------------------------------------------------
# 内容寻址落库
# ---------------------------------------------------------------------------

def save_image_asset(user_id: int, course_id: int, data: bytes, mime: str, *,
                     prompt: str = "", scene_id: int | None = None,
                     extra: dict | None = None) -> dict:
    """按 sha256 内容寻址写入 ``cls_assets``（同用户同图复用一行，图片只落盘一次）。

    只接受按内容识别后的白名单图片（PNG/JPEG/WEBP/GIF）；调用方传入的 ``mime`` 仅作参考，
    实际落库类型由内容决定，避免把非图片内容按图片类型内联返回。
    """
    from .. import db

    raw = bytes(data or b"")
    detected = sniff_image_type(raw)
    if not detected:
        raise ValueError("配图素材不是支持的图片格式（PNG/JPEG/WEBP/GIF）")
    mime = detected
    digest = hashlib.sha256(raw).hexdigest()
    with db.connection() as con:
        existing = con.execute(
            "SELECT * FROM cls_assets WHERE user_id=? AND kind=? AND sha256=? ORDER BY id LIMIT 1",
            (user_id, IMAGE_KIND, digest),
        ).fetchone()
    if existing is not None:
        return dict(existing)
    suffix = _suffix_for(mime)
    storage_name = "image-%s.%s" % (digest[:32], suffix)
    storage.save_file(IMAGE_FOLDER, storage_name, raw, mime)
    now = db.utcnow()
    meta = {"prompt": str(prompt or "")[:600], "scene_id": scene_id, "size": len(raw)}
    for key, value in (extra or {}).items():
        meta[str(key)] = value
    with db.connection() as con:
        asset_id = con.execute(
            "INSERT INTO cls_assets(user_id,course_id,kind,storage_name,mime_type,sha256,meta_json,created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (user_id, course_id, IMAGE_KIND, storage_name, mime, digest,
             cdb.json_dump(meta), now),
        ).lastrowid
        row = con.execute("SELECT * FROM cls_assets WHERE id=?", (asset_id,)).fetchone()
    return dict(row)


def remember_generated_image(user_id: int, course_id: int, payload: dict) -> None:
    """把成功生成的配图摘要并入课程 ``config.generated_images``（读-改-写，保留其它配置）。

    仅保存可展示摘要（asset_id / url / prompt / size / edit / created_at），最多
    :data:`MAX_REMEMBERED_IMAGES` 条；重复 asset_id 只保留最新一条。配置写失败不影响出图结果。
    """
    from .. import db

    item = {
        "asset_id": int(payload.get("id") or 0),
        "url": str(payload.get("url") or ""),
        "mime_type": str(payload.get("mime_type") or ""),
        "prompt": str(payload.get("prompt") or "")[:600],
        "size": int(payload.get("size") or 0),
        "edit": bool(payload.get("edit")),
        "scene_id": payload.get("scene_id"),
        "created_at": db.utcnow(),
    }
    try:
        with db.connection() as con:
            db.write_lock(con, "classroom-course")
            row = con.execute(
                "SELECT config_json FROM cls_courses WHERE id=? AND user_id=?", (course_id, user_id),
            ).fetchone()
            if row is None:
                return
            config = cdb.json_load(row["config_json"], {})
            config = config if isinstance(config, dict) else {}
            images = config.get("generated_images")
            images = [entry for entry in images if isinstance(entry, dict)] if isinstance(images, list) else []
            images = [entry for entry in images if int(entry.get("asset_id") or 0) != item["asset_id"]]
            images.insert(0, item)
            config["generated_images"] = images[:MAX_REMEMBERED_IMAGES]
            con.execute(
                "UPDATE cls_courses SET config_json=?, updated_at=? WHERE id=? AND user_id=?",
                (cdb.json_dump(config), db.utcnow(), course_id, user_id),
            )
    except Exception:  # 摘要写失败不影响已落库的图片素材
        log.warning("Could not remember generated image for course %s", course_id, exc_info=True)


async def generate_scene_image(*, user_id: int, course_id: int, candidate: dict, prompt: str,
                               scene_id: int | None = None,
                               reference: bytes | None = None) -> dict:
    """生成一张配图并落库；返回 ``{id, url, mime_type, prompt, size, edit, warnings}``，失败抛异常。

    ``reference`` 为参考图原始字节：先压缩（最长边 1600 / JPEG 85 / ≤8MB）再按厂商协议做
    图片编辑（MiniMax ``subject_reference`` / OpenAI 兼容 ``/images/edits``，失败回退文生图）。
    参考图非法时抛 :class:`ValueError`，由 API 层转 422 中文引导。
    """
    warnings: list[str] = []
    reference_bytes = None
    reference_mime = "image/jpeg"
    if reference:
        reference_bytes, reference_mime = compress_reference_image(reference)
    if reference_bytes:
        image_bytes, mime = await _request_image(
            candidate["provider"], str(candidate["model"]), prompt,
            reference=reference_bytes, reference_mime=reference_mime, warnings=warnings,
        )
    else:
        # 兼容既有调用方/测试桩：无参考图时保持原三参数签名（不传可选关键字）。
        image_bytes, mime = await _request_image(
            candidate["provider"], str(candidate["model"]), prompt,
        )
    row = save_image_asset(
        user_id, course_id, image_bytes, mime, prompt=prompt, scene_id=scene_id,
        extra={"edit": bool(reference_bytes)},
    )
    payload = tts.asset_public(row)
    payload["prompt"] = str(prompt or "")[:600]
    payload["size"] = len(image_bytes)
    payload["sha256"] = str(row.get("sha256") or "")[:64]
    payload["edit"] = bool(reference_bytes)
    payload["warnings"] = warnings
    return payload


# ---------------------------------------------------------------------------
# 素材引用查询与安全回收（跨课程共享素材只回收「无存活引用」的文件）
# ---------------------------------------------------------------------------
#
# 背景：课堂素材（图片 / TTS 音频 / 视频）按内容哈希去重，同一行 ``cls_assets`` 可能被多门
# 课程引用；导入的素材又可能以同一 ``storage_name`` 重复落库。删课时必须现查「还有哪些存活
# 课程/场景引用它」，只回收完全无引用的素材，避免删掉别的课程仍在使用的内容。

#: 这些键名在场景/课程 JSON 中可能保存素材 id。
_ASSET_ID_KEYS = frozenset({"asset_id", "assetid", "audio_id", "audioid", "ttsassetid", "video_asset_id"})
_ASSET_URL_RE = re.compile(r"/api/classroom/assets/(\d+)")


def _collect_asset_ids(value, found: set[int]) -> None:
    """递归收集 JSON 结构中的素材引用（``asset_id`` 类键名 + ``/api/classroom/assets/N`` 链接）。"""
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).strip().lower() in _ASSET_ID_KEYS:
                try:
                    asset_id = int(item)
                except (TypeError, ValueError):
                    asset_id = 0
                if asset_id > 0:
                    found.add(asset_id)
            _collect_asset_ids(item, found)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _collect_asset_ids(item, found)
    elif isinstance(value, str):
        for match in _ASSET_URL_RE.finditer(value):
            found.add(int(match.group(1)))


def scene_asset_ids(content, meta=None) -> set[int]:
    """场景内容 + ``meta``（动作如 ``play_audio``）引用的素材 id 集合。"""
    found: set[int] = set()
    _collect_asset_ids(content, found)
    _collect_asset_ids(meta, found)
    return found


def _scene_reference_ids(scene_row) -> set[int]:
    row = dict(scene_row) if scene_row is not None else {}
    found: set[int] = set()
    _collect_asset_ids(cdb.json_load(row.get("content_json"), {}), found)
    _collect_asset_ids(cdb.json_load(row.get("meta_json"), {}), found)
    return found


def _course_reference_ids(course_row) -> set[int]:
    row = dict(course_row) if course_row is not None else {}
    found: set[int] = set()
    _collect_asset_ids(cdb.json_load(row.get("config_json"), {}), found)
    _collect_asset_ids(cdb.json_load(row.get("cover_json"), {}), found)
    return found


def _surviving_asset_references(con, user_id: int, exclude_course_id: int | None = None
                                ) -> dict[int, list[dict]]:
    """现查用户其它课程/场景对素材的存活引用：``{asset_id: [{kind, course_id, scene_id?}]}``。"""
    references: dict[int, list[dict]] = {}
    scene_sql = "SELECT id, course_id, content_json, meta_json FROM cls_scenes WHERE user_id=?"
    scene_params: list = [user_id]
    if exclude_course_id is not None:
        scene_sql += " AND course_id<>?"
        scene_params.append(int(exclude_course_id))
    for row in con.execute(scene_sql, tuple(scene_params)).fetchall():
        for asset_id in _scene_reference_ids(row):
            references.setdefault(asset_id, []).append({
                "kind": "scene", "scene_id": int(row["id"]), "course_id": int(row["course_id"]),
            })
    course_sql = "SELECT id, config_json, cover_json FROM cls_courses WHERE user_id=?"
    course_params: list = [user_id]
    if exclude_course_id is not None:
        course_sql += " AND id<>?"
        course_params.append(int(exclude_course_id))
    for row in con.execute(course_sql, tuple(course_params)).fetchall():
        for asset_id in _course_reference_ids(row):
            references.setdefault(asset_id, []).append({
                "kind": "course_config", "course_id": int(row["id"]),
            })
    for entries in references.values():
        entries.sort(key=lambda item: (str(item.get("kind")), int(item.get("id") or item.get("scene_id") or 0)))
    return references


def asset_references(user_id: int, asset_ids: list[int] | None = None) -> dict[int, list[dict]]:
    """跨课程存活引用查询（可测试的公开入口）；``asset_ids`` 为空时返回该用户全部素材引用。"""
    from .. import db

    with db.connection() as con:
        references = _surviving_asset_references(con, user_id, exclude_course_id=None)
    if asset_ids is None:
        return references
    wanted = {int(item) for item in asset_ids if str(item).strip().lstrip("-").isdigit()}
    return {asset_id: references[asset_id] for asset_id in wanted if asset_id in references}


def asset_reference_counts(user_id: int, asset_ids: list[int] | None = None) -> dict[int, int]:
    """素材的存活引用计数：``{asset_id: 引用条数}``（同一素材被多门课程引用时 > 1）。"""
    return {
        asset_id: len(entries)
        for asset_id, entries in asset_references(user_id, asset_ids).items()
    }


def _keeper_course_id(references: list[dict]) -> int:
    """为仍有引用的共享素材挑选「归属课程」：优先场景引用最多的课程，其次课程 id 最小（可重复迁移）。"""
    scores: dict[int, int] = {}
    for reference in references or []:
        course_id = int(reference.get("course_id") or 0)
        if course_id <= 0:
            continue
        weight = 2 if reference.get("kind") == "scene" else 1
        scores[course_id] = scores.get(course_id, 0) + weight
    if not scores:
        return 0
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))[0][0]


def reclaim_course_assets(*, user_id: int, course_id: int) -> dict:
    """删除课程时安全回收素材：只删除「无存活引用且文件未被其它素材行共享」的素材。

    步骤（同一写事务内）：

    1. 列出该课程的素材行，并现查其它课程/场景的存活引用；
    2. 仍有引用的素材把 ``course_id`` 迁移到另一个引用课程（删除归属课后仍可继续访问）；
    3. 同一 ``storage_name`` 还被其它素材行使用（导入去重共享文件）时保留文件；
    4. 删除课程（级联删场景/素材），事务提交后 best-effort 删除无引用文件，并复查避免误删
       并发新写入的同名素材。

    返回审计信息（可直接作为删除接口的 ``assets`` 字段）：

    - ``examined``：本课程登记过的素材行数；
    - ``retained``：仍有存活引用、已迁移归属课程的素材（含 ``references``）；
    - ``reclaimed``：行已删除且文件已删除的素材；
    - ``shared_files_kept``：素材行已删但同内容文件仍被其它素材行/并发写入使用；
    - ``files_removed`` / ``file_errors``：文件删除数量与失败明细（删除失败不影响课程删除）。
    """
    from .. import db

    audit: dict = {
        "course_id": int(course_id),
        "examined": 0,
        "retained": [],
        "reclaimed": [],
        "shared_files_kept": [],
        "files_removed": 0,
        "file_errors": [],
    }
    files_to_remove: list[dict] = []
    with db.connection() as con:
        db.write_lock(con, "classroom-assets")
        candidates = [dict(row) for row in con.execute(
            "SELECT * FROM cls_assets WHERE user_id=? AND course_id=? ORDER BY id",
            (user_id, course_id),
        ).fetchall()]
        audit["examined"] = len(candidates)
        drop_ids: list[int] = []
        if candidates:
            surviving = _surviving_asset_references(con, user_id, exclude_course_id=course_id)
            shared_names = {
                str(row["storage_name"]) for row in con.execute(
                    "SELECT storage_name FROM cls_assets WHERE user_id=? "
                    "AND (course_id IS NULL OR course_id<>?)",
                    (user_id, course_id),
                ).fetchall()
            }
            drop_ids = []
            for asset in candidates:
                asset_id = int(asset["id"])
                references = surviving.get(asset_id) or []
                record = {
                    "asset_id": asset_id,
                    "kind": asset["kind"],
                    "storage_name": asset["storage_name"],
                    "sha256": asset["sha256"],
                    "mime_type": asset["mime_type"],
                }
                if references:
                    keeper = _keeper_course_id(references)
                    if keeper:
                        con.execute(
                            "UPDATE cls_assets SET course_id=? WHERE id=? AND user_id=?",
                            (keeper, asset_id, user_id),
                        )
                        audit["retained"].append({
                            **record, "references": references,
                            "reassigned_to_course_id": keeper,
                        })
                        continue
                if str(asset["storage_name"]) in shared_names:
                    # 同一内容文件还被其它素材行使用：删掉本课程的素材行，但保留文件。
                    audit["shared_files_kept"].append({**record, "references": references})
                    drop_ids.append(asset_id)
                    continue
                files_to_remove.append({**record, "references": references})
                drop_ids.append(asset_id)
        con.execute("DELETE FROM cls_courses WHERE id=? AND user_id=?", (course_id, user_id))
        if drop_ids:
            # 显式删除本次判定可回收的行（外键级联之外再保一道，兼容不同数据库行为）。
            placeholders = ",".join("?" for _ in drop_ids)
            con.execute(
                "DELETE FROM cls_assets WHERE user_id=? AND id IN (%s)" % placeholders,
                (user_id, *drop_ids),
            )
    reclaimed: list[dict] = []
    for item in files_to_remove:
        try:
            with db.connection() as con:
                still_used = con.execute(
                    "SELECT 1 FROM cls_assets WHERE user_id=? AND storage_name=? LIMIT 1",
                    (user_id, item["storage_name"]),
                ).fetchone()
            if still_used is not None:  # 并发写入了同内容素材：保留文件
                audit["shared_files_kept"].append(item)
                continue
            storage.delete_file(IMAGE_FOLDER, item["storage_name"])
            audit["files_removed"] += 1
            reclaimed.append(item)
        except Exception as exc:  # 文件缺失/存储不可用不应阻塞课程删除
            audit["file_errors"].append({
                "asset_id": item["asset_id"],
                "error": "%s: %s" % (type(exc).__name__, str(exc)[:120]),
            })
    audit["reclaimed"] = reclaimed
    return audit


def missing_scene_asset_ids(user_id: int, scenes: list[dict]) -> list[int]:
    """页面降级用：返回场景引用了但已不存在（被回收/丢失）的素材 id（升序）。"""
    from .. import db

    referenced: set[int] = set()
    for scene in scenes or []:
        referenced |= _scene_reference_ids(scene)
    if not referenced:
        return []
    placeholders = ",".join("?" for _ in referenced)
    with db.connection() as con:
        rows = con.execute(
            "SELECT id FROM cls_assets WHERE user_id=? AND id IN (%s)" % placeholders,
            (user_id, *sorted(referenced)),
        ).fetchall()
    existing = {int(row["id"]) for row in rows}
    return sorted(referenced - existing)


__all__ = [
    "ACTIVE_CONTENT_TYPES", "DEFAULT_SIZE", "IMAGE_FOLDER", "IMAGE_KIND", "INLINE_AUDIO_TYPES",
    "INLINE_IMAGE_TYPES", "INLINE_MEDIA_TYPES", "INLINE_VIDEO_TYPES", "MAX_IMAGES_PER_SCENE",
    "MAX_IMAGE_BYTES", "MAX_REFERENCE_BYTES", "REFERENCE_JPEG_QUALITY", "REFERENCE_MAX_SIDE",
    "TRUSTED_ASSET_KINDS", "asset_reference_counts", "asset_references", "asset_serving_plan",
    "classify_import_media", "compress_reference_image", "content_disposition", "download_media_type",
    "generate_scene_image", "image_candidates", "image_provider_info", "inline_media_type",
    "missing_scene_asset_ids", "reclaim_course_assets", "remember_generated_image",
    "resolve_candidate", "save_image_asset", "scene_asset_ids", "sniff_image_type",
    "sniff_media_type",
]
