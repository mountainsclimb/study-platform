"""高考词汇本与四档复习（方案 §7 批次六：ECDICT）。

数据来源：`app/data/vocab_seed.json`（由 `scripts/build_vocab.py` 从 ECDICT 构建，
MIT License，来源提交与 SHA-256 见同目录 `vocab_seed.README.md`）。

边界：

- 只新增 `vocab_items` / `vocab_cards` 两张业务表；`vocab_ratings` 是评分幂等日志
  （与 `review_ratings` 同语义，不参与其它模块读取）；
- 排程直接复用 `app.study_review.schedule()`，状态结构与既有复习卡一致；
- 种子缺失时模块整体返回 `available: false`，不影响应用启动与其它功能；
- 不改 `review_cards`、`question_ref` 与错题本的任何语义。
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field

from . import db, study_review
from .auth import current_user

SEED_PATH = Path(__file__).resolve().parent / "data" / "vocab_seed.json"
README_PATH = Path(__file__).resolve().parent / "data" / "vocab_seed.README.md"
SEED_FIELDS = (
    "word", "phonetic", "translation", "pos", "tag",
    "collins", "oxford", "bnc", "frq", "exchange",
)
MAX_WORD_CHARS = 64
MAX_IDEMPOTENCY_KEY_LENGTH = 120
MAX_LIMIT = 200
DEFAULT_LIMIT = 50
_IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9._:+-]+$")

router = APIRouter(prefix="/api/vocab", tags=["vocab"])

_SCHEMA = """
CREATE TABLE IF NOT EXISTS vocab_items (
    id INTEGER PRIMARY KEY,
    word TEXT NOT NULL UNIQUE,
    phonetic TEXT NOT NULL DEFAULT '',
    translation TEXT NOT NULL,
    pos TEXT NOT NULL DEFAULT '',
    tag TEXT NOT NULL DEFAULT '',
    collins INTEGER,
    oxford INTEGER,
    bnc INTEGER,
    frq INTEGER,
    exchange TEXT NOT NULL DEFAULT '',
    seed_sha TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS vocab_items_rank ON vocab_items(frq, word);

CREATE TABLE IF NOT EXISTS vocab_cards (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    word TEXT NOT NULL,
    state_json TEXT NOT NULL,
    due_at TEXT NOT NULL,
    last_rating INTEGER,
    reps INTEGER NOT NULL DEFAULT 0,
    lapses INTEGER NOT NULL DEFAULT 0,
    version INTEGER NOT NULL DEFAULT 1,
    archived INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(user_id, word)
);
CREATE INDEX IF NOT EXISTS vocab_cards_due ON vocab_cards(user_id, archived, due_at);

CREATE TABLE IF NOT EXISTS vocab_ratings (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    card_id INTEGER NOT NULL,
    rating INTEGER NOT NULL,
    idempotency_key TEXT NOT NULL,
    card_version INTEGER NOT NULL,
    rated_at TEXT NOT NULL,
    due_at TEXT NOT NULL,
    result_json TEXT NOT NULL,
    state_before_json TEXT,
    state_after_json TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(user_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS vocab_ratings_card ON vocab_ratings(user_id, card_id, id);
"""

_CARD_COLUMNS = (
    "id, user_id, word, state_json, due_at, last_rating, reps, lapses, "
    "version, archived, created_at, updated_at"
)
_SEED_STATE = {
    "available": False,
    "reason": "词汇种子尚未加载",
    "count": 0,
    "sha": None,
    "source_commit": None,
}
# 进程级种子缓存：文件未变化时只解析一次；每个测试库仍按需整批写入（懒加载）。
_SEED_CACHE: dict = {
    "path": None,
    "mtime_ns": None,
    "size": None,
    "sha": None,
    "meta": {},
    "rows": (),
}
_SEED_LOAD_LOCK = threading.Lock()


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


def _load_state(raw) -> dict:
    if isinstance(raw, dict):
        return dict(raw)
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _dump_state(state: dict) -> str:
    return json.dumps(state or {}, ensure_ascii=False, sort_keys=True)


def _state_from_row(row) -> dict:
    raw = _load_state(row["state_json"])
    return {
        "state": str(raw.get("state") or study_review.STATE_NEW).strip().lower(),
        "stability": raw.get("stability"),
        "difficulty": raw.get("difficulty"),
        "reps": int(row["reps"] or 0),
        "lapses": int(row["lapses"] or 0),
        "last_rating": row["last_rating"],
        "last_review": raw.get("last_review"),
        "due": row["due_at"],
    }


def _require_available() -> None:
    if not _SEED_STATE["available"]:
        raise HTTPException(503, _SEED_STATE["reason"] or "词汇模块暂不可用")


def _item_public(row) -> dict:
    tag = str(row["tag"] or "")
    return {
        "word": row["word"],
        "phonetic": row["phonetic"] or "",
        "translation": row["translation"] or "",
        "pos": row["pos"] or "",
        "tag": tag,
        "tags": tag.split(),
        "collins": row["collins"],
        "oxford": row["oxford"],
        "bnc": row["bnc"],
        "frq": row["frq"],
        "exchange": row["exchange"] or "",
    }


def _card_public(row, item=None) -> dict:
    state = _state_from_row(row)
    version = int(row["version"] or 1)
    payload = {
        "card_id": row["id"],
        "word": row["word"],
        "state": state["state"],
        "stability": state["stability"],
        "difficulty": state["difficulty"],
        "reps": state["reps"],
        "lapses": state["lapses"],
        "last_rating": state["last_rating"],
        "last_review": state["last_review"],
        "due_at": row["due_at"],
        "version": version,
        "card_version": version,
        "archived": bool(row["archived"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "rating_scale": list(study_review.RATING_SCALE),
        "rating_labels": {str(key): label for key, label in study_review.RATING_LABELS.items()},
    }
    if item is not None:
        payload.update({
            "phonetic": item["phonetic"] or "",
            "translation": item["translation"] or "",
            "pos": item["pos"] or "",
            "tags": str(item["tag"] or "").split(),
            "tag": item["tag"] or "",
            "exchange": item["exchange"] or "",
        })
    return payload


def _read_seed(path: Path) -> dict:
    """读取并缓存种子（按 mtime/size 失效）；只解析，不写数据库。"""
    stat = path.stat()
    if (
        _SEED_CACHE["path"] == str(path)
        and _SEED_CACHE["mtime_ns"] == stat.st_mtime_ns
        and _SEED_CACHE["size"] == stat.st_size
        and _SEED_CACHE["rows"]
    ):
        return _SEED_CACHE
    raw = path.read_bytes()
    payload = json.loads(raw.decode("utf-8"))
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        raise ValueError("种子缺少 items")
    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    rows = []
    for item in items:
        if not isinstance(item, dict):
            continue
        word = str(item.get("word") or "").strip().lower()[:MAX_WORD_CHARS]
        if not word:
            continue
        rows.append((
            word,
            str(item.get("phonetic") or "")[:100],
            str(item.get("translation") or ""),
            str(item.get("pos") or "")[:120],
            str(item.get("tag") or "")[:120],
            item.get("collins"), item.get("oxford"), item.get("bnc"), item.get("frq"),
            str(item.get("exchange") or "")[:300],
        ))
    if not rows:
        raise ValueError("种子没有有效词条")
    _SEED_CACHE.update({
        "path": str(path),
        "mtime_ns": stat.st_mtime_ns,
        "size": stat.st_size,
        "sha": hashlib.sha256(raw).hexdigest(),
        "meta": meta,
        "rows": tuple(rows),
    })
    return _SEED_CACHE


def init_vocab(seed_path: Path | None = None) -> None:
    """幂等建表并登记种子元信息；词条在首次访问词汇功能时整批装入（懒加载）。

    这样普通测试/启动不会为每个临时数据库写入 8000 条词条；只有真正打开词表、
    加入/复习词汇时才调用 :func:`_ensure_seed_loaded()`。
    """
    path = Path(seed_path) if seed_path is not None else SEED_PATH
    with db.connection() as con:
        if db.postgres_enabled():
            db.write_lock(con, "vocab")
        con.executescript(_SCHEMA)

    try:
        seed = _read_seed(path)
    except (OSError, ValueError, UnicodeError):
        _SEED_STATE.update({
            "available": False,
            "reason": "词汇种子缺失或无法解析，词汇模块暂不可用。",
            "count": 0,
            "sha": None,
            "source_commit": None,
        })
        return
    _SEED_STATE.update({
        "available": True,
        "reason": "",
        "count": len(seed["rows"]),
        "sha": seed["sha"],
        "source_commit": (seed.get("meta") or {}).get("source_commit"),
    })


def _ensure_seed_loaded() -> None:
    """首次访问词汇功能时把缓存种子整批写入当前数据库（跨进程写锁保护）。"""
    _require_available()
    seed = _SEED_CACHE
    if not seed["rows"]:
        seed = _read_seed(Path(seed["path"]) if seed.get("path") else SEED_PATH)
    with _SEED_LOAD_LOCK:
        with db.connection() as con:
            if db.postgres_enabled():
                db.write_lock(con, "vocab")
            row = con.execute(
                "SELECT COUNT(*) AS total, MIN(seed_sha) AS min_sha, MAX(seed_sha) AS max_sha "
                "FROM vocab_items"
            ).fetchone()
            if (row and int(row["total"] or 0) == len(seed["rows"])
                    and row["min_sha"] == seed["sha"] and row["max_sha"] == seed["sha"]):
                return
            now = db.utcnow()
            con.execute("DELETE FROM vocab_items")
            con.executemany(
                "INSERT INTO vocab_items(word, phonetic, translation, pos, tag, collins, "
                "oxford, bnc, frq, exchange, seed_sha, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                [(*values, seed["sha"], now) for values in seed["rows"]],
            )


def seed_status() -> dict:
    return dict(_SEED_STATE)


def home_summary(user_id: int) -> dict:
    """首页加法键：词汇总量与当前到期数；模块不可用时如实返回。"""
    if not _SEED_STATE["available"]:
        return {"available": False, "due_today": 0, "total": 0}
    now = _iso(study_review.now_utc())
    with db.connection() as con:
        row = con.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN archived=0 AND due_at<=? THEN 1 ELSE 0 END) AS due "
            "FROM vocab_cards WHERE user_id=? AND archived=0",
            (now, user_id),
        ).fetchone()
    return {
        "available": True,
        "due_today": int(row["due"] or 0),
        "total": int(row["total"] or 0),
    }


def export_items(user_id: int) -> list[dict]:
    """非归档词汇卡 + 词条，供 Anki 导出（正面单词/音标，背面释义/标签）。"""
    with db.connection() as con:
        rows = con.execute(
            "SELECT c.id, c.word, i.phonetic, i.translation, i.tag "
            "FROM vocab_cards c JOIN vocab_items i ON i.word=c.word "
            "WHERE c.user_id=? AND c.archived=0 ORDER BY c.due_at, c.id",
            (user_id,),
        ).fetchall()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# HTTP 路由
# ---------------------------------------------------------------------------


class CardIn(BaseModel):
    word: str = Field(min_length=1, max_length=MAX_WORD_CHARS)


class RatingIn(BaseModel):
    rating: int
    card_version: int | None = None
    idempotency_key: str | None = Field(default=None, max_length=MAX_IDEMPOTENCY_KEY_LENGTH)


@router.get("/meta")
def api_meta(user=Depends(current_user)):
    return {
        "available": _SEED_STATE["available"],
        "count": _SEED_STATE["count"],
        "seed_sha": _SEED_STATE["sha"],
        "source": "ECDICT",
        "source_commit": _SEED_STATE["source_commit"],
        "fields": list(SEED_FIELDS),
        "note": _SEED_STATE["reason"] or None,
    }


@router.get("/items")
def api_items(
    tag: str | None = Query(default=None, max_length=40),
    q: str | None = Query(default=None, max_length=80),
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=20, ge=1, le=100),
    user=Depends(current_user),
):
    """词表分页：按高考/四级等标签与关键词筛选，按词频升序。"""
    _require_available()
    _ensure_seed_loaded()
    where: list[str] = []
    params: list = []
    tag_text = str(tag or "").strip()
    if tag_text:
        where.append("(' ' || tag || ' ') LIKE ?")
        params.append(f"% {tag_text} %")
    keyword = str(q or "").strip()
    if keyword:
        where.append("(word LIKE ? OR translation LIKE ?)")
        params.extend([f"%{keyword}%", f"%{keyword}%"])
    clause = " WHERE " + " AND ".join(where) if where else ""
    with db.connection() as con:
        total = int(con.execute(
            f"SELECT COUNT(*) FROM vocab_items{clause}", params,
        ).fetchone()[0])
        rows = con.execute(
            "SELECT * FROM vocab_items" + clause
            + " ORDER BY COALESCE(NULLIF(frq, 0), 999999999), word LIMIT ? OFFSET ?",
            (*params, limit, (page - 1) * limit),
        ).fetchall()
    return {
        "available": True,
        "items": [_item_public(row) for row in rows],
        "total": total,
        "page": page,
        "limit": limit,
    }


@router.get("/stats")
def api_stats(user=Depends(current_user)):
    _require_available()
    now = _iso(study_review.now_utc())
    with db.connection() as con:
        rows = con.execute(
            f"SELECT {_CARD_COLUMNS} FROM vocab_cards WHERE user_id=? AND archived=0",
            (user["id"],),
        ).fetchall()
    counts = {"new": 0, "learning": 0, "review": 0, "relearning": 0}
    due = 0
    for row in rows:
        state = _state_from_row(row)["state"]
        counts[state] = counts.get(state, 0) + 1
        if row["due_at"] <= now:
            due += 1
    return {
        "available": True,
        "total": len(rows),
        "due_today": due,
        **counts,
    }


@router.post("/cards")
def api_create_card(payload: CardIn, user=Depends(current_user)):
    _require_available()
    _ensure_seed_loaded()
    word = payload.word.strip().lower()
    with db.connection() as con:
        if db.postgres_enabled():
            db.write_lock(con, "vocab")
        item = con.execute("SELECT * FROM vocab_items WHERE word=?", (word,)).fetchone()
        if item is None:
            raise HTTPException(404, "词表中没有这个单词")
        existing = con.execute(
            f"SELECT {_CARD_COLUMNS} FROM vocab_cards WHERE user_id=? AND word=?",
            (user["id"], word),
        ).fetchone()
        if existing is not None:
            reactivated = bool(int(existing["archived"] or 0))
            if reactivated:
                con.execute(
                    "UPDATE vocab_cards SET archived=0, updated_at=? WHERE id=? AND user_id=?",
                    (db.utcnow(), existing["id"], user["id"]),
                )
                existing = con.execute(
                    f"SELECT {_CARD_COLUMNS} FROM vocab_cards WHERE id=? AND user_id=?",
                    (existing["id"], user["id"]),
                ).fetchone()
            return {"created": False, "reactivated": reactivated, "card": _card_public(existing, item)}
        now = study_review.now_utc()
        due_at = _iso(now)
        state = {
            "state": study_review.STATE_NEW,
            "stability": None,
            "difficulty": None,
            "reps": 0,
            "lapses": 0,
            "last_rating": None,
            "last_review": None,
            "due": due_at,
        }
        now_iso = db.utcnow()
        cursor = con.execute(
            "INSERT INTO vocab_cards(user_id, word, state_json, due_at, last_rating, reps, "
            "lapses, version, archived, created_at, updated_at) "
            "VALUES(?,?,?,?,NULL,0,0,1,0,?,?)",
            (user["id"], word, _dump_state(state), due_at, now_iso, now_iso),
        )
        row = con.execute(
            f"SELECT {_CARD_COLUMNS} FROM vocab_cards WHERE id=? AND user_id=?",
            (cursor.lastrowid, user["id"]),
        ).fetchone()
    return {"created": True, "reactivated": False, "card": _card_public(row, item)}


_CARD_JOIN_COLUMNS = (
    "c.id, c.user_id, c.word, c.state_json, c.due_at, c.last_rating, c.reps, c.lapses, "
    "c.version, c.archived, c.created_at, c.updated_at, "
    "i.phonetic, i.translation, i.pos, i.tag, i.exchange"
)


@router.get("/cards/due")
def api_due_cards(
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    include_future: bool = Query(default=False),
    user=Depends(current_user),
):
    _require_available()
    _ensure_seed_loaded()
    now = _iso(study_review.now_utc())
    clause = "c.user_id=? AND c.archived=0" + ("" if include_future else " AND c.due_at<=?")
    params: list = [user["id"]] + ([] if include_future else [now])
    with db.connection() as con:
        total = int(con.execute(
            "SELECT COUNT(*) FROM vocab_cards c WHERE " + clause, params,
        ).fetchone()[0])
        rows = con.execute(
            "SELECT " + _CARD_JOIN_COLUMNS + " FROM vocab_cards c "
            "JOIN vocab_items i ON i.word=c.word WHERE " + clause
            + " ORDER BY c.due_at, c.id LIMIT ?",
            (*params, limit),
        ).fetchall()
    return {
        "available": True,
        "cards": [_card_public(row, row) for row in rows],
        "total": total,
        "now": now,
    }


@router.get("/cards/{card_id}")
def api_get_card(card_id: int, user=Depends(current_user)):
    _require_available()
    _ensure_seed_loaded()
    with db.connection() as con:
        row = con.execute(
            "SELECT " + _CARD_JOIN_COLUMNS + " FROM vocab_cards c "
            "JOIN vocab_items i ON i.word=c.word WHERE c.id=? AND c.user_id=?",
            (card_id, user["id"]),
        ).fetchone()
    if row is None:
        raise HTTPException(404, "词汇卡不存在")
    return {"card": _card_public(row, row)}


def _idempotency_key(value) -> str:
    if value is None:
        return "srv-" + uuid.uuid4().hex
    text = str(value).strip()
    if not text:
        return "srv-" + uuid.uuid4().hex
    if len(text) > MAX_IDEMPOTENCY_KEY_LENGTH or not _IDEMPOTENCY_KEY_RE.match(text):
        raise HTTPException(422, "幂等键需为 ≤120 位字母、数字或 ._:+-")
    return text


@router.post("/cards/{card_id}/ratings")
def api_rate_card(card_id: int, payload: RatingIn, user=Depends(current_user)):
    _require_available()
    _ensure_seed_loaded()
    key = _idempotency_key(payload.idempotency_key)
    with db.connection() as con:
        if db.postgres_enabled():
            db.write_lock(con, "vocab")
        row = con.execute(
            "SELECT " + _CARD_JOIN_COLUMNS + " FROM vocab_cards c "
            "JOIN vocab_items i ON i.word=c.word WHERE c.id=? AND c.user_id=?",
            (card_id, user["id"]),
        ).fetchone()
        if row is None:
            raise HTTPException(404, "词汇卡不存在")

        recorded = con.execute(
            "SELECT * FROM vocab_ratings WHERE user_id=? AND idempotency_key=?",
            (user["id"], key),
        ).fetchone()
        if recorded is not None:
            try:
                result = json.loads(recorded["result_json"])
            except (TypeError, ValueError):
                result = {"due_at": recorded["due_at"], "rating": recorded["rating"]}
            current = con.execute(
                "SELECT " + _CARD_JOIN_COLUMNS + " FROM vocab_cards c "
                "JOIN vocab_items i ON i.word=c.word WHERE c.id=? AND c.user_id=?",
                (recorded["card_id"], user["id"]),
            ).fetchone()
            if current is not None:
                result["card"] = _card_public(current, current)
            result["replayed"] = True
            result["replay"] = True
            result["idempotency_key"] = key
            result["note"] = "该评分已应用（幂等重放），未重复写入。"
            return result

        current_version = int(row["version"] or 1)
        if payload.card_version is not None:
            if payload.card_version != current_version:
                raise HTTPException(409, "词汇卡已被更新，请刷新后重试")
        before = _state_from_row(row)
        moment = study_review.now_utc()
        after = study_review.schedule(before, payload.rating, moment)
        due_at = str(after["due"])
        version_after = current_version + 1
        now_iso = db.utcnow()
        cursor = con.execute(
            "UPDATE vocab_cards SET state_json=?, due_at=?, last_rating=?, reps=?, lapses=?, "
            "version=?, updated_at=? WHERE id=? AND user_id=? AND version=?",
            (
                _dump_state(after), due_at, payload.rating,
                int(after.get("reps") or 0), int(after.get("lapses") or 0),
                version_after, now_iso, card_id, user["id"], current_version,
            ),
        )
        if not getattr(cursor, "rowcount", 1):
            raise HTTPException(409, "词汇卡已被更新，请刷新后重试")
        updated = con.execute(
            "SELECT " + _CARD_JOIN_COLUMNS + " FROM vocab_cards c "
            "JOIN vocab_items i ON i.word=c.word WHERE c.id=? AND c.user_id=?",
            (card_id, user["id"]),
        ).fetchone()
        result = {
            "card": _card_public(updated, updated),
            "rating": payload.rating,
            "due_at": due_at,
            "replayed": False,
            "replay": False,
            "idempotency_key": key,
            "algorithm": study_review.FSRS_ALGORITHM,
            "algorithm_version": study_review.FSRS_ALGORITHM_VERSION,
            "note": "评分已应用，下一次到期时间以服务端返回的 due_at 为准。",
        }
        con.execute(
            "INSERT INTO vocab_ratings(user_id, card_id, rating, idempotency_key, "
            "card_version, rated_at, due_at, result_json, state_before_json, "
            "state_after_json, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                user["id"], card_id, payload.rating, key, current_version,
                now_iso, due_at, _dump_state(result),
                _dump_state(before), _dump_state(after), now_iso,
            ),
        )
    return result


@router.get("/export")
def api_export(user=Depends(current_user)):
    """导出当前用户词汇卡为 Anki 牌组（正面单词/音标，背面释义/标签）。"""
    _require_available()
    _ensure_seed_loaded()
    from . import anki_export

    items = export_items(user["id"])
    if not items:
        raise HTTPException(404, "还没有加入复习的词汇")
    groups = anki_export.vocab_deck(items)
    try:
        data = anki_export.build_apkg(groups)
    except ImportError:
        raise HTTPException(503, "Anki 导出依赖 genanki 未安装")
    if not data:
        raise HTTPException(503, "未能生成 Anki 牌组")
    return Response(
        data,
        media_type="application/octet-stream",
        headers=anki_export.attachment_headers("知序词汇.apkg"),
    )
