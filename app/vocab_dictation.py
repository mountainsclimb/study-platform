"""词汇听写练习（批次六后续）：本地 TTS 发音 + 本地 ASR 转写 + 编辑距离评分。

依赖批次三的 `app.ai.speech_local`（sherpa-onnx，可选、默认关闭）：
- `GET  /api/vocab/dictation/status`：TTS/ASR 可用性与模型名；
- `GET  /api/vocab/dictation/next?exclude=1,2`：取一张词汇卡，返回**音频**与中文释义，
  不返回单词本身（避免前端直接看到答案）；音频按单词 LRU 缓存；
- `POST /api/vocab/dictation/answer`：上传 16k 单声道 PCM16 WAV，服务端转写并与
  目标单词做规范化编辑距离评分，返回 `correct/score/transcript/expected`。

全部依赖缺失/未开启时返回 `available:false` 或 503，不产生占位音频/假分数；
课堂/主站 CSP 不变（音频走同源接口，前端只播放 data: audio/wav）。
"""
from __future__ import annotations

import asyncio
import base64
from functools import lru_cache
import re

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from . import db
from .ai import speech_local
from .auth import current_user
from . import vocab

router = APIRouter(prefix="/api/vocab/dictation", tags=["Vocab dictation"])

MAX_ASR_BYTES = 25 * 1024 * 1024
MAX_EXCLUDE_IDS = 200
MAX_TRANSCRIPT_CHARS = 500
_TTS_CACHE_SIZE = 256
_WORD_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")


def normalize_spelling(value: str) -> str:
    """小写并去掉空格/连字符/标点，用于听写比对（ice cream ≈ icecream）。"""
    text = str(value or "").strip().lower()
    text = text.replace("’", "'").replace("‘", "'")
    return _WORD_NORMALIZE_RE.sub("", text)


def levenshtein(left: str, right: str) -> int:
    """标准编辑距离（插入/删除/替换各计 1）。"""
    if left == right:
        return 0
    if not left:
        return len(right)
    if not right:
        return len(left)
    previous = list(range(len(right) + 1))
    for i, char_left in enumerate(left, start=1):
        current = [i]
        for j, char_right in enumerate(right, start=1):
            cost = 0 if char_left == char_right else 1
            current.append(min(
                previous[j] + 1,
                current[j - 1] + 1,
                previous[j - 1] + cost,
            ))
        previous = current
    return previous[-1]


def score_attempt(expected: str, transcript: str) -> dict:
    """规范化后按编辑距离评分；correct 以 80 分为线，完全一致必对。

    返回 ``{correct, score, distance, expected_normalized, transcript_normalized}``；
    空转写记 0 分。评分只反映听写转写与目标拼写的接近程度，不代表发音质量。
    """
    expected_norm = normalize_spelling(expected)
    transcript_norm = normalize_spelling(transcript)
    if not expected_norm:
        return {
            "correct": False, "score": 0, "distance": 0,
            "expected_normalized": "", "transcript_normalized": transcript_norm,
            "reason": "目标单词为空",
        }
    if not transcript_norm:
        return {
            "correct": False, "score": 0, "distance": len(expected_norm),
            "expected_normalized": expected_norm, "transcript_normalized": "",
            "reason": "没有识别到文本",
        }
    distance = levenshtein(expected_norm, transcript_norm)
    longest = max(len(expected_norm), len(transcript_norm)) or 1
    score = max(0, min(100, round(100 * (1 - distance / longest))))
    return {
        "correct": distance == 0 or (score >= 80 and len(expected_norm) >= 4),
        "score": score,
        "distance": distance,
        "expected_normalized": expected_norm,
        "transcript_normalized": transcript_norm,
        "reason": "",
    }


@lru_cache(maxsize=_TTS_CACHE_SIZE)
def _synthesize_word(word: str) -> bytes:
    wav, _mime = speech_local.synthesize(word, speed=1.0)
    return wav


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
        wav = await asyncio.wait_for(
            asyncio.to_thread(_synthesize_word, word),
            timeout=speech_local.SYNTH_TIMEOUT_SECONDS,
        )
    except speech_local.SpeechUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - 本地合成异常统一中文降级
        raise HTTPException(502, "本地语音合成失败（%s）" % type(exc).__name__) from exc
    audio = "data:audio/wav;base64," + base64.b64encode(wav).decode("ascii")
    return {
        "available": True,
        "done": False,
        "card_id": int(row["id"]),
        "audio": audio,
        "audio_bytes": len(wav),
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
