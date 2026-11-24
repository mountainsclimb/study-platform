"""词汇听写练习（批次六后续）：本地 TTS 发音 + 本地 ASR 转写 + 编辑距离评分。

依赖批次三的 `app.ai.speech_local`（sherpa-onnx，可选、默认关闭）：
- `GET  /api/vocab/dictation/status`：TTS/ASR 可用性与模型名；
- `GET  /api/vocab/dictation/next?exclude=1,2`：取一张词汇卡，返回**音频**与中文释义，
  不返回单词本身（避免前端直接看到答案）；音频按单词 LRU 缓存；
- `POST /api/vocab/dictation/answer`：上传 16k 单声道 PCM16 WAV，服务端转写并与
  目标单词做规范化编辑距离评分，返回 `correct/score/transcript/expected`。

全部依赖缺失/未开启时返回 `available:false` 或 503，不产生占位音频/假分数；
课堂/主站 CSP 不变（音频走同源接口，前端只播放同源 data URI）。
合成与评分复用通用语音层：`app.ai.audio_compress` 压缩（失败回退 WAV）、
`app.ai.speech_score` 编辑距离评分，与跟读检查保持同一套文本口径。
"""
from __future__ import annotations

import asyncio
import base64
from functools import lru_cache

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from . import db
from .ai import audio_compress, speech_local
from .ai.speech_score import levenshtein, normalize_spelling, score_attempt
from .auth import current_user
from . import vocab

router = APIRouter(prefix="/api/vocab/dictation", tags=["Vocab dictation"])

MAX_ASR_BYTES = 25 * 1024 * 1024
MAX_EXCLUDE_IDS = 200
MAX_TRANSCRIPT_CHARS = 500
_TTS_CACHE_SIZE = 256


@lru_cache(maxsize=_TTS_CACHE_SIZE)
def _synthesize_word(word: str) -> tuple[bytes, str]:
    """合成单词发音并压缩；返回 ``(音频, MIME)``，压缩失败时仍是 WAV。"""
    audio, mime = speech_local.synthesize(word, speed=1.0)
    return audio_compress.compress_speech(audio, mime)[:2]


def _parse_exclude(raw: str) -> set[int]:
    values: set[int] = set()
    for chunk in str(raw or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            value = int(chunk)
        except ValueError:
            continue
        if value > 0:
            values.add(value)
        if len(values) >= MAX_EXCLUDE_IDS:
            break
    return values


def _speech_note(tts_ok: bool, asr_ok: bool) -> str:
    if tts_ok and asr_ok:
        return "本地语音可用：发音与识别都在本机完成。"
    missing = []
    if not tts_ok:
        missing.append("语音合成")
    if not asr_ok:
        missing.append("语音识别")
    return "本地" + "、".join(missing) + "不可用；听写练习暂不可用。"


@router.get("/status")
def dictation_status(user=Depends(current_user)):
    vocab_state = vocab.seed_status()
    tts_ok = bool(speech_local.tts_available())
    asr_ok = bool(speech_local.asr_available())
    return {
        "available": bool(vocab_state.get("available")) and tts_ok and asr_ok,
        "vocab_available": bool(vocab_state.get("available")),
        "tts_available": tts_ok,
        "asr_available": asr_ok,
        "tts_model": speech_local.model_label() if tts_ok else None,
        "asr_model": speech_local.asr_model_label() if asr_ok else None,
        "max_seconds": speech_local.max_asr_seconds(),
        "note": _speech_note(tts_ok, asr_ok),
    }


@router.get("/next")
async def next_dictation_item(
    exclude: str = "",
    user=Depends(current_user),
):
    """返回下一张词汇卡的发音与释义；不返回单词，答案在提交听写后才能看到。"""
    vocab._require_available()
    vocab._ensure_seed_loaded()
    if not speech_local.tts_available():
        raise HTTPException(503, "本地语音合成不可用，听写练习暂不可用")
    excluded = _parse_exclude(exclude)
    now = str(db.utcnow())
    clause = "c.user_id=? AND c.archived=0"
    params: list = [user["id"]]
    if excluded:
        marks = ",".join("?" for _ in excluded)
        clause += f" AND c.id NOT IN ({marks})"
        params.extend(sorted(excluded))
    with db.connection() as con:
        row = con.execute(
            "SELECT c.id, c.due_at, c.reps, i.word, i.translation, i.phonetic "
            "FROM vocab_cards c JOIN vocab_items i ON i.word=c.word "
            f"WHERE {clause} "
            "ORDER BY CASE WHEN c.due_at<=? THEN 0 ELSE 1 END, c.due_at, c.id LIMIT 1",
            (*params, now),
        ).fetchone()
    if row is None:
        return {
            "available": True,
            "done": True,
            "note": "这批词汇已经听过一遍；可回到词表加入更多单词。",
        }
    word = str(row["word"] or "")
    try:
        audio_bytes, audio_mime = await asyncio.wait_for(
            asyncio.to_thread(_synthesize_word, word),
            timeout=speech_local.SYNTH_TIMEOUT_SECONDS,
        )
    except speech_local.SpeechUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - 本地合成异常统一中文降级
        raise HTTPException(502, "本地语音合成失败（%s）" % type(exc).__name__) from exc
    audio = "data:%s;base64,%s" % (
        audio_mime or "audio/wav", base64.b64encode(audio_bytes).decode("ascii"),
    )
    return {
        "available": True,
        "done": False,
        "card_id": int(row["id"]),
        "audio": audio,
        "audio_mime": audio_mime or "audio/wav",
        "audio_bytes": len(audio_bytes),
        "meaning": row["translation"] or "",
        "phonetic_hint": row["phonetic"] or "",
        "reps": int(row["reps"] or 0),
    }


@router.post("/answer")
async def answer_dictation(
    card_id: int = Form(...),
    file: UploadFile = File(...),
    user=Depends(current_user),
):
    """上传听写录音：本地识别后按规范化编辑距离评分，并揭示目标单词与释义。"""
    vocab._require_available()
    vocab._ensure_seed_loaded()
    if not speech_local.asr_available():
        raise HTTPException(503, "本地语音识别不可用，听写练习暂不可用")
    with db.connection() as con:
        row = con.execute(
            "SELECT c.id, i.word, i.translation, i.phonetic "
            "FROM vocab_cards c JOIN vocab_items i ON i.word=c.word "
            "WHERE c.id=? AND c.user_id=? AND c.archived=0",
            (card_id, user["id"]),
        ).fetchone()
    if row is None:
        raise HTTPException(404, "词汇卡不存在")
    payload = await file.read(MAX_ASR_BYTES + 1)
    if not payload:
        raise HTTPException(422, "录音文件为空")
    if len(payload) > MAX_ASR_BYTES:
        raise HTTPException(413, "录音文件不能超过 25MB")
    mime = (file.content_type or "").split(";")[0].strip().lower()
    if mime and not mime.startswith("audio/") and mime != "application/octet-stream":
        raise HTTPException(422, "请上传音频文件（WAV）")
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(speech_local.transcribe, payload),
            timeout=speech_local.TRANSCRIBE_TIMEOUT_SECONDS,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except speech_local.SpeechUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, "本地语音识别失败（%s）" % type(exc).__name__) from exc
    transcript = str(result.get("text") or "").strip()[:MAX_TRANSCRIPT_CHARS]
    expected = str(row["word"] or "")
    scored = score_attempt(expected, transcript)
    return {
        "card_id": int(row["id"]),
        "correct": scored["correct"],
        "score": scored["score"],
        "distance": scored["distance"],
        "transcript": transcript,
        "expected": expected,
        "phonetic": row["phonetic"] or "",
        "translation": row["translation"] or "",
        "note": "评分只反映听写转写与目标拼写的接近程度，不代表发音质量。",
    }
