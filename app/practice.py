"""Read-only question bank, FTS5 search, knowledge points, private attempts, mastery & interleaved review."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import csv
from functools import lru_cache
import hashlib
from io import BytesIO, StringIO
import json
import logging
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, Response
from PIL import Image, ImageFilter, ImageOps, ImageStat, UnidentifiedImageError
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from .auth import current_user, require_admin
from . import db, storage
from .db import BANK_DIR, DATA_DIR, connection, ensure_column, utcnow
from .review_schedule import fsrs_enabled, fsrs_review

router = APIRouter(prefix="/api", tags=["practice"])
LEVELS = ("未掌握", "初步掌握", "比较掌握", "熟练掌握")
DIFFICULTIES = ("待标注", "简单", "中等", "困难")
ERROR_TAG_PRESETS = ("概念不清", "审题失误", "计算错误", "方法不会", "记忆遗漏", "粗心")
TYPE_LABELS = {
    "single_choice": "单选题", "multi_choice": "多选题", "fill_blank": "填空题",
    "calculation": "计算题", "short_answer": "简答题", "sub_short_answer": "材料子题",
    "material_group": "材料题", "essay": "作文", "unknown": "其他",
}
DEFAULT_PREFS = {
    "interleave_per_page": 2,
    "same_kp_only": False,
    "show_review_strip": True,
    "ai_mode": "fast",
}
# Audited material sections include published explanations. They must never be sent
# to a learner before submission; keep them out of the practice pool for review.
QUARANTINED_GROUPS = frozenset({
    "3597febaaede0d74:m_0001", "0bad2393ec198c91:m_0002",
    "45261e31764158e6:m_0002", "45261e31764158e6:m_0003",
    "45261e31764158e6:m_0004",
})
QUARANTINED_QUESTIONS = frozenset({
    "d2aa42179cd42df7:21",  # Missing associated material.
    "cf04cad2296e2558:59",  # Extracted answer/analysis stored as a question stem.
    "03063fd6a07bea8b:20", "03063fd6a07bea8b:21", "03063fd6a07bea8b:23",
})
MAX_IMAGE_BYTES = 10 * 1024 * 1024
# Read-model snapshot columns (snapshot contract §5). Older banks do not carry them;
# every read path falls back to the legacy behaviour when the column is missing.
SNAPSHOT_QUESTION_COLUMNS = frozenset({
    "bank_version", "source_question_id", "parent_source_question_id", "group_kind",
    "practice_status", "grading_status", "content_revision", "answer_revision",
    "pua_remaining", "stem_blocks_json", "options_blocks_json", "answer_blocks_json",
    "solution_blocks_json", "shared_blocks_json", "member_index_json", "quality_flags_json",
})
SNAPSHOT_GROUP_COLUMNS = frozenset({
    "bank_version", "group_kind", "parent_source_question_id", "shared_blocks_json",
    "member_index_json", "n_members", "n_practice",
})
PRACTICE_STATUSES = ("practice", "review", "context", "quarantined")
GRADING_STATUSES = ("auto", "pending", "disabled")
PENDING_GRADE_MESSAGE = "参考答案待核验，暂不自动判分"
QUARANTINE_DETAIL_MESSAGE = "该题材料缺失或混入答案，已进入数据复核队列"
QUARANTINE_SUBMIT_MESSAGE = "该题材料缺失或混入答案，暂不可作答"
CONTEXT_SUBMIT_MESSAGE = "该题仅为上下文材料，不可作答"
# 原卷公开字段里出现以下标记，说明题干/材料混入了答案或解析；先停止作答，待数据修正。
ANSWER_LEAK_MARKERS = ("【答案】", "【详解】", "【解析】", "【解答】", "参考答案：")
_log = logging.getLogger("zhixu.practice")
_fts_lock = threading.Lock()
_bank_hash_cache: dict[str, tuple[float, int, str]] = {}
KP_SEED_PATH = Path(__file__).resolve().parent / "data" / "kp_seed.json"
# FTS 索引结构版本：变更列（如 v2 增加 stem_compact 去空白列）时需要重建。
FTS_SCHEMA = "2"
# PostgreSQL 关键词检索：镜像表已为 stem_compact 建 GIN(trgm) 索引，查询必须直接命中该列。
# 旧写法 ``COALESCE(q.stem_compact, q.stem_plain) ILIKE ?`` 会让优化器放弃索引走全表扫。
_PG_BANK_CONTAINS = (
    "(q.stem_compact ILIKE ? ESCAPE '\\' OR q.paper_name ILIKE ? ESCAPE '\\' "
    "OR q.search_material_text ILIKE ? ESCAPE '\\')"
)
# /api/questions 的二次复习槽位参数上限：防止恶意超长 seen_reviews 撑爆 SQL 变量数。
MAX_SEEN_REVIEWS = 200
# IN (...) 分批大小：SQLite 默认最多 32,766 个变量、PG 协议上限 65,535，统一保守分批。
IN_CHUNK_SIZE = 400


def _bank_dir() -> Path:
    return db.BANK_DIR


def _data_dir() -> Path:
    return db.DATA_DIR


_SEARCH_NOISE_RE = re.compile(r"[\s\W]+", re.UNICODE)


def _compact_search_text(text: str | None) -> str:
    """检索用紧凑文本：去掉空白与标点（与 search_similar_questions 的清洗口径一致）。"""
    return _SEARCH_NOISE_RE.sub("", text or "")


def _bank_table(con, name: str) -> str:
    """题库表名：PostgreSQL 镜像住在 ``bank`` schema，SQLite 直接 ATTACH/只读打开。

    双引擎手写表名最容易在 PG 分支漏 ``bank.`` 前缀（组接口曾因此 500），
    统一从这里取，避免每个查询各自决定。
    """
    if isinstance(con, db._Postgres):
        return f"bank.{name}"
    return name


def _bank_group_join(con, alias: str = "g") -> str:
    """题目 → 题组左连接（兼容 R3 中 796 题 group_id 已带 ``<doc>:`` 前缀）。"""
    groups = _bank_table(con, "groups")
    return (
        f"LEFT JOIN {groups} {alias} ON {alias}.group_id = CASE "
        f"WHEN q.group_id IS NULL THEN NULL "
        f"WHEN q.group_id LIKE q.doc_id || ':%' THEN q.group_id "
        f"ELSE q.doc_id || ':' || q.group_id END"
    )


def _bank_sha256() -> str:
    path = _bank_dir() / "题库.db"
    if not path.is_file():
        return ""
    st = path.stat()
    key = str(path.resolve())
    cached = _bank_hash_cache.get(key)
    if cached and cached[0] == st.st_mtime and cached[1] == st.st_size:
        return cached[2]
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    digest = h.hexdigest()
    _bank_hash_cache[key] = (st.st_mtime, st.st_size, digest)
    return digest


# 题库检索索引：随题库内容摘要（库文件 + WAL）刷新。
#   * bank_leak_flags：公开字段是否混入答案解析 / 是否隔离（检索期单列整型过滤）；
#   * bank_search_meta：SQLite 短词 LIKE 的只读镜像（PG 检索直接命中 GIN 索引，不再维护）。
LEAK_FLAGS_SCHEMA = "3"
LEAK_FLAGS_META_KEY = "bank_leak_sha256_" + LEAK_FLAGS_SCHEMA
# 进程内「题库摘要 + 应用库实例」就绪标记：命中即 O(1) 返回，检索主事务内不发任何 DDL。
_verified_index_key: tuple | None = None
_verified_fts_key: tuple | None = None
INDEX_PREWARM_ENV = "STUDY_INDEX_PREWARM"


def _bank_content_digest() -> str:
    """题库内容摘要（库文件 + WAL），用于判定检索索引是否需要重建。"""
    path = _bank_dir() / "题库.db"
    if not path.is_file():
        return ""
    try:
        return db._bank_digest(db._bank_signature(path))
    except OSError:
        return ""


def _index_cache_key(digest: str) -> tuple:
    """索引就绪标记：题库摘要 + 应用库实例（换题库/换库文件后必须重建）。"""
    if db.postgres_enabled():
        return (digest, "postgresql")
    path = _data_dir() / "app.sqlite3"
    try:
        identity = path.stat().st_ino
    except OSError:
        identity = 0
    return (digest, str(path), identity)


def _ensure_leak_flags_tables(con) -> None:
    """建表/补列/建索引：只在 ``init_practice()``（进程启动）执行一次。

    P0（PG 锁风暴）：运行时检索路径禁止再发 DDL。PostgreSQL 的
    ``CREATE TABLE IF NOT EXISTS`` 也会拿锁，``ALTER TABLE ... ADD COLUMN IF NOT EXISTS``
    即使列已存在也会拿表级 ACCESS EXCLUSIVE 并持有到事务结束——检索期调用会把并发检索
    串行化。旧库缺列/缺表统一由启动初始化补齐。
    """
    con.execute(
        "CREATE TABLE IF NOT EXISTS bank_leak_flags ("
        "qid TEXT PRIMARY KEY, leak INTEGER NOT NULL DEFAULT 0, "
        "excluded INTEGER NOT NULL DEFAULT 0, has_solution INTEGER NOT NULL DEFAULT 0)"
    )
    # 旧库的 bank_leak_flags 缺列：补列而不是整表重建。
    ensure_column(con, "bank_leak_flags", "excluded", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(con, "bank_leak_flags", "has_solution", "INTEGER NOT NULL DEFAULT 0")
    # 检索只过滤 leak/excluded 的少量行：部分索引比全表主键扫描更小（SQLite/PG 均支持）。
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_bank_leak_flags_active "
        "ON bank_leak_flags(qid) WHERE leak=1 OR excluded=1"
    )
    con.execute(
        "CREATE TABLE IF NOT EXISTS bank_search_meta ("
        "qid TEXT PRIMARY KEY, stem_plain TEXT NOT NULL DEFAULT '', "
        "paper_name TEXT NOT NULL DEFAULT '', material_text TEXT NOT NULL DEFAULT '')"
    )


def _compute_bank_index_rows(con) -> list[tuple[str, int, int, int, str, str, str]]:
    """一次性算出检索索引行：
    ``(qid, leak, excluded, has_solution, stem_plain, paper_name, material_text)``。

    leak：公开字段（题干/选项/共用材料/材料父题/共用块）混入答案解析标记；
    excluded：硬隔离题/题组、context 与 quarantined 状态（等同旧检索门禁）；
    has_solution：纯文本解析非空或块级解析非空（与详情页同口径）；
    search meta：短词 LIKE 所需的紧凑镜像。
    """
    question_columns = _bank_question_columns(con)
    if not question_columns:
        return []
    group_columns = _bank_group_columns(con)
    columns: list[str] = [
        "q.qid",
        "COALESCE(q.stem_plain,'')",
        "COALESCE(q.paper_name,'')",
        "q.doc_id" if "doc_id" in question_columns else "NULL",
        "q.group_id" if "group_id" in question_columns else "NULL",
        "COALESCE(q.practice_status,'practice')" if "practice_status" in question_columns else "'practice'",
        "COALESCE(q.solution,'')" if "solution" in question_columns else "''",
        "COALESCE(q.solution_blocks_json,'')" if "solution_blocks_json" in question_columns else "''",
    ]
    leak_indexes = [1]
    for column in ("stem_blocks_json", "options_blocks_json", "options_json"):
        if column in question_columns:
            leak_indexes.append(len(columns))
            columns.append(f"COALESCE(q.{column},'')")
    material_index = None
    if group_columns:
        for column in ("material_text", "parent_stem", "shared_blocks_json"):
            if column not in group_columns:
                continue
            if column == "material_text":
                material_index = len(columns)
            leak_indexes.append(len(columns))
            columns.append(f"COALESCE(g.{column},'')")
    join = _bank_group_join(con) if group_columns else ""
    sql = f"SELECT {', '.join(columns)} FROM {_bank_table(con, 'questions')} q {join}"
    results: list[tuple[str, int, int, int, str, str, str]] = []
    for row in con.execute(sql).fetchall():
        qid = row[0]
        stem_plain = row[1] or ""
        paper_name = row[2] or ""
        doc_id = row[3]
        group_id = row[4]
        practice_status = row[5] or "practice"
        solution_text = row[6] or ""
        solution_blocks = _dict_list(row[7])
        leak = 0
        for index in leak_indexes:
            if any(marker in (row[index] or "") for marker in ANSWER_LEAK_MARKERS):
                leak = 1
                break
        key = _group_key({"group_id": group_id, "doc_id": doc_id})
        excluded = 1 if (
            qid in QUARANTINED_QUESTIONS
            or key in QUARANTINED_GROUPS
            or practice_status in ("context", "quarantined")
        ) else 0
        has_solution = 1 if (solution_text.strip() or solution_blocks) else 0
        material_text = (row[material_index] or "") if material_index is not None else ""
        results.append((qid, leak, excluded, has_solution, stem_plain, paper_name, material_text))
    return results


def _executemany(con, statement: str, rows) -> None:
    if not rows:
        return
    if isinstance(con, db._Postgres):
        with con.raw.cursor() as cursor:
            cursor.executemany(db._postgres_params(statement), rows)
        return
    con.executemany(statement, rows)


def _chunked(values: list, size: int | None = None):
    """按 ``IN (...)`` 参数上限分批（SQLite 3.32+ 32,766 / 旧版 999，PG 协议 65,535）。"""
    step = size or IN_CHUNK_SIZE
    for start in range(0, len(values), step):
        yield values[start:start + step]


def _write_bank_index(con, rows, digest: str) -> None:
    """刷新检索索引数据行（无 DDL：表结构由启动初始化保证）。"""
    con.execute("DELETE FROM bank_leak_flags")
    _executemany(
        con,
        "INSERT INTO bank_leak_flags(qid,leak,excluded,has_solution) VALUES (?,?,?,?)",
        [(row[0], row[1], row[2], row[3]) for row in rows],
    )
    if not isinstance(con, db._Postgres):
        # 短词 LIKE 镜像只有 SQLite 检索使用；PG 直接命中 bank.questions 的 trigram GIN。
        con.execute("DELETE FROM bank_search_meta")
        _executemany(
            con,
            "INSERT INTO bank_search_meta(qid,stem_plain,paper_name,material_text) VALUES (?,?,?,?)",
            [(row[0], row[4], row[5], row[6]) for row in rows],
        )
    con.execute(
        "INSERT INTO bank_meta(key, value) VALUES (?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (LEAK_FLAGS_META_KEY, digest),
    )


def _ensure_leak_flags_locked(con, digest: str) -> bool:
    """调用方需持有 ``_fts_lock``；摘要一致时只做一次 SELECT，不写索引数据。"""
    if not digest:
        return False
    meta = con.execute(
        "SELECT value FROM bank_meta WHERE key=?", (LEAK_FLAGS_META_KEY,)
    ).fetchone()
    if meta and meta[0] == digest:
        return True
    _write_bank_index(con, _compute_bank_index_rows(con), digest)
    return True


def _mark_index_verified(digest: str) -> None:
    global _verified_index_key
    _verified_index_key = _index_cache_key(digest)


def _mark_fts_verified(digest: str) -> None:
    global _verified_fts_key
    _verified_fts_key = _index_cache_key(digest)


def ensure_leak_flags(con=None) -> bool:
    """确保泄密标记表与当前题库一致；检索入口先调用。

    摘要未变时走进程内 O(1) 快路径，不查库也不发 DDL；只有题库换版（摘要变化）时
    才在锁内重建索引数据（建表/补列仍只在启动时做）。
    """
    digest = _bank_content_digest()
    if not digest:
        return False
    key = _index_cache_key(digest)
    if _verified_index_key == key:
        return True
    with _fts_lock:
        if _verified_index_key == key:
            return True
        own = con is None
        if own:
            ctx = connection(attach_bank=True)
            con = ctx.__enter__()
        try:
            ok = _ensure_leak_flags_locked(con, digest)
            if ok:
                _mark_index_verified(digest)
            return ok
        finally:
            if own:
                ctx.__exit__(None, None, None)


def _install_bank_leak_flags(con, digest: str | None = None) -> bool:
    """供 PostgreSQL 题库镜像刷新路径调用（与 ``bank_fts`` 构建同属题库摘要刷新）。"""
    digest = digest or _bank_content_digest()
    if not digest:
        return False
    key = _index_cache_key(digest)
    if _verified_index_key == key:
        return True
    with _fts_lock:
        if _verified_index_key == key:
            return True
        ok = _ensure_leak_flags_locked(con, digest)
        if ok:
            _mark_index_verified(digest)
        return ok


def _prewarm_enabled() -> bool:
    value = os.environ.get(INDEX_PREWARM_ENV, "1").strip().lower()
    return value not in ("0", "false", "no", "off")


def prewarm_bank_indexes() -> bool:
    """启动时预热批量检索索引（FTS + 泄密标记），避免首个检索请求长阻塞。

    SQLite 冷启动需要重建 7,659 题的 FTS 与标记（实测 1–2s，受机器负载影响，属于
    题库换版后的一次性成本；后续启动摘要命中只需毫秒级）。测试或极简部署可用
    ``STUDY_INDEX_PREWARM=0`` 关闭。SQLite 预热失败不阻断启动，首个检索请求会重试；
    PostgreSQL 镜像/连接错误属于部署问题，启动阶段直接抛出以便快速失败。
    """
    if not (_bank_dir() / "题库.db").is_file():
        return False
    started = time.perf_counter()
    try:
        if not db.postgres_enabled():
            ensure_fts_index()
        ensure_leak_flags()
    except (sqlite3.Error, OSError) as exc:
        if db.postgres_enabled():
            raise
        _log.warning("检索索引预热失败，将在首个检索请求重试：%s", type(exc).__name__)
        return False
    _log.info("检索索引预热完成：%.0f ms", (time.perf_counter() - started) * 1000)
    return True


def _seed_knowledge_points(con: sqlite3.Connection):
    if con.execute("SELECT COUNT(*) FROM knowledge_points").fetchone()[0] > 0:
        return
    if not KP_SEED_PATH.is_file():
        return
    data = json.loads(KP_SEED_PATH.read_text(encoding="utf-8"))
    for subject, chapters in data.items():
        for c_idx, ch in enumerate(chapters, 1):
            c_id = con.execute(
                "INSERT INTO knowledge_points(subject,parent_id,name,level,aliases_json,description,sort,source) "
                "VALUES (?,NULL,?,1,'[]','',?,'official')",
                (subject, ch["name"], c_idx),
            ).lastrowid
            for s_idx, sec in enumerate(ch.get("children", []), 1):
                s_id = con.execute(
                    "INSERT INTO knowledge_points(subject,parent_id,name,level,aliases_json,description,sort,source) "
                    "VALUES (?,?,?,2,'[]','',?,'official')",
                    (subject, c_id, sec["name"], s_idx),
                ).lastrowid
                for k_idx, kp_name in enumerate(sec.get("children", []), 1):
                    con.execute(
                        "INSERT INTO knowledge_points(subject,parent_id,name,level,aliases_json,description,sort,source) "
                        "VALUES (?,?,?,3,'[]','',?,'official')",
                        (subject, s_id, kp_name, k_idx),
                    )


def init_practice():
    with connection() as con:
        if db.postgres_enabled():
            db.write_lock(con, "practice-schema")
        con.executescript("""
            CREATE TABLE IF NOT EXISTS attempts (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                qid TEXT NOT NULL, answer_text TEXT NOT NULL DEFAULT '',
                image_path TEXT, result TEXT NOT NULL CHECK(result IN ('correct','incorrect','pending')),
                grade_source TEXT NOT NULL DEFAULT 'reference', report_json TEXT,
                created_at TEXT NOT NULL, graded_at TEXT
            );
            CREATE INDEX IF NOT EXISTS attempts_user_question ON attempts(user_id,qid,id DESC);
            CREATE INDEX IF NOT EXISTS attempts_qid_result ON attempts(qid,result);
            CREATE TABLE IF NOT EXISTS mastery (
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                qid TEXT NOT NULL, level TEXT NOT NULL DEFAULT '未掌握',
                updated_at TEXT NOT NULL, PRIMARY KEY(user_id,qid)
            );
            CREATE TABLE IF NOT EXISTS wrong_questions (
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                qid TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
                failures INTEGER NOT NULL DEFAULT 0, streak INTEGER NOT NULL DEFAULT 0,
                next_review_at TEXT NOT NULL, added_at TEXT NOT NULL, last_result TEXT,
                PRIMARY KEY(user_id,qid)
            );
            CREATE INDEX IF NOT EXISTS wrong_due ON wrong_questions(user_id,next_review_at);
            CREATE TABLE IF NOT EXISTS question_metadata (
                qid TEXT PRIMARY KEY, difficulty TEXT NOT NULL DEFAULT '待标注',
                updated_by INTEGER REFERENCES users(id), updated_at TEXT NOT NULL
            );
            -- 题库检索索引（随题库摘要刷新）；旧库启动时自动补齐。
            CREATE TABLE IF NOT EXISTS bank_leak_flags (
                qid TEXT PRIMARY KEY, leak INTEGER NOT NULL DEFAULT 0,
                excluded INTEGER NOT NULL DEFAULT 0,
                has_solution INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS bank_search_meta (
                qid TEXT PRIMARY KEY, stem_plain TEXT NOT NULL DEFAULT '',
                paper_name TEXT NOT NULL DEFAULT '', material_text TEXT NOT NULL DEFAULT ''
            );
        """)
        ensure_column(con, "wrong_questions", "interval_days", "REAL NOT NULL DEFAULT 1")
        ensure_column(con, "wrong_questions", "ease", "REAL NOT NULL DEFAULT 2.0")
        ensure_column(con, "wrong_questions", "review_count", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(con, "wrong_questions", "archived", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(con, "wrong_questions", "error_tags_json", "TEXT NOT NULL DEFAULT '[]'")
        ensure_column(con, "wrong_questions", "last_wrong_at", "TEXT")
        ensure_column(con, "wrong_questions", "stability", "REAL")
        ensure_column(con, "wrong_questions", "difficulty", "REAL")
        ensure_column(con, "wrong_questions", "last_review_at", "TEXT")
        # 最近一次「掌握度操作」推升间隔时对应的作答 id：同一轮作答最多推升一次。
        ensure_column(con, "wrong_questions", "last_mastery_push_attempt", "INTEGER")
        ensure_column(con, "question_metadata", "ai_difficulty", "TEXT")
        ensure_column(con, "question_metadata", "ai_difficulty_score", "REAL")
        ensure_column(con, "question_metadata", "ai_model", "TEXT")
        ensure_column(con, "question_metadata", "ai_updated_at", "TEXT")
        # 旧库的检索索引表可能缺列/缺索引：只在启动初始化补齐（检索路径不再发 DDL）。
        _ensure_leak_flags_tables(con)
        _seed_knowledge_points(con)
    (_data_dir() / "attempts").mkdir(parents=True, exist_ok=True)
    # P1：批量 FTS/泄密标记构建放到启动阶段，避免首个检索请求长时间阻塞事件循环。
    if _prewarm_enabled():
        prewarm_bank_indexes()


def ensure_fts_index(con: sqlite3.Connection | None = None):
    """Build or rebuild the FTS5 trigram index in app.sqlite3 if source bank changed."""
    digest = _bank_sha256()
    if not digest:
        return False
    content_digest = _bank_content_digest()
    key = _index_cache_key(content_digest) if content_digest else None
    if key is not None and _verified_fts_key == key:
        return True
    meta_key = "bank_sha256_" + FTS_SCHEMA
    with _fts_lock:
        if key is not None and _verified_fts_key == key:
            return True
        own = con is None
        if own:
            ctx = connection(attach_bank=True)
            con = ctx.__enter__()
        try:
            content_digest = _bank_content_digest()
            key = _index_cache_key(content_digest) if content_digest else None
            has_table = con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='bank_fts'"
            ).fetchone()
            meta = con.execute("SELECT value FROM bank_meta WHERE key=?", (meta_key,)).fetchone()
            if has_table and meta and meta[0] == digest:
                # 泄密标记与 FTS 同属题库摘要派生的索引：同一次判定里保证两者都已就绪。
                if _ensure_leak_flags_locked(con, content_digest):
                    _mark_index_verified(content_digest)
                if key is not None:
                    _mark_fts_verified(content_digest)
                return True
            con.execute("DROP TABLE IF EXISTS bank_fts")
            con.execute(
                "CREATE VIRTUAL TABLE bank_fts USING fts5("
                "qid UNINDEXED, stem_plain, paper_name, material_text, stem_compact, tokenize='trigram')"
            )
            questions = _bank_table(con, "questions")
            rows = con.execute(f"""
                SELECT q.qid, COALESCE(q.stem_plain,''), COALESCE(q.paper_name,''),
                       COALESCE(g.material_text,'')
                FROM {questions} q
                {_bank_group_join(con)}
            """).fetchall()
            payload = []
            for row in rows:
                stem = row[1] or ""
                payload.append((row[0], stem, row[2] or "", row[3] or "", _compact_search_text(stem)))
            con.executemany(
                "INSERT INTO bank_fts(qid, stem_plain, paper_name, material_text, stem_compact) "
                "VALUES (?,?,?,?,?)", payload,
            )
            con.execute(
                "INSERT INTO bank_meta(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (meta_key, digest),
            )
            if _ensure_leak_flags_locked(con, content_digest):
                _mark_index_verified(content_digest)
            if key is not None:
                _mark_fts_verified(content_digest)
            if not own:
                con.commit()
            return True
        finally:
            if own:
                ctx.__exit__(None, None, None)


@contextmanager
def _bank():
    path = _bank_dir() / "题库.db"
    if not path.is_file():
        raise HTTPException(503, "未找到题库，请检查 STUDY_BANK_DIR 配置")
    con = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    try:
        yield con
    finally:
        con.close()


@contextmanager
def _app_with_bank():
    try:
        with connection(attach_bank=True) as con:
            yield con
    except FileNotFoundError:
        raise HTTPException(503, "未找到题库，请检查 STUDY_BANK_DIR 配置") from None


def _json(value, default):
    if not value:
        return default
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


def _row_value(row, key, default=None):
    """Read an optional column from sqlite3.Row, a dict, or the PostgreSQL adapter row."""
    try:
        keys = row.keys()
    except AttributeError:
        return default
    return row[key] if key in keys else default


def _text(value):
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return None if value is None else str(value)


def _dict_list(value):
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    data = _json(value, [])
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def _int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _data_quality_flags(value):
    """Flatten data-side [{"code","message"}] quality flags into display strings."""
    items = _json(value, [])
    flags = []
    if not isinstance(items, list):
        return flags
    for item in items:
        if isinstance(item, dict):
            message = _text(item.get("message")) or _text(item.get("code"))
        else:
            message = _text(item)
        if message and message not in flags:
            flags.append(message)
    return flags


def _merge_options(options, options_blocks):
    """Attach snapshot blocks to each option while keeping the legacy {label,text} shape."""
    entries = options_blocks if isinstance(options_blocks, list) else []
    if not isinstance(options, list) or not options or not entries:
        return options
    by_label = {}
    for entry in entries:
        if isinstance(entry, dict) and entry.get("label") is not None:
            by_label[str(entry["label"])] = entry.get("blocks")
    merged = []
    for index, option in enumerate(options):
        if not isinstance(option, dict):
            merged.append(option)
            continue
        option = dict(option)
        blocks = by_label.get(str(option.get("label", "")))
        if blocks is None and index < len(entries) and isinstance(entries[index], dict):
            blocks = entries[index].get("blocks")
        if isinstance(blocks, list):
            option["blocks"] = blocks
        merged.append(option)
    return merged


def _member_counts(stored, member_index: list[dict]) -> tuple[int, int]:
    """n_members / n_practice with a legacy fallback for banks without the new columns."""
    n_members = _int_or_none(stored.get("n_members"))
    if n_members is None:
        n_members = len(member_index) if member_index else (_int_or_none(stored.get("n_children_total")) or 0)
    n_practice = _int_or_none(stored.get("n_practice"))
    if n_practice is None:
        if member_index:
            n_practice = sum(
                1 for entry in member_index
                if entry.get("in_bank") is not False
                and (_text(entry.get("practice_status")) or "practice") in ("practice", "review")
            )
        else:
            n_practice = n_members
    return n_members, n_practice


@lru_cache(maxsize=8)
def _sqlite_question_columns(path: str, mtime_ns: int, size: int) -> frozenset[str]:
    probe = sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True, timeout=10)
    try:
        return frozenset(row[1] for row in probe.execute("PRAGMA table_info(questions)"))
    finally:
        probe.close()


def _bank_question_columns(con: sqlite3.Connection | None = None) -> frozenset[str]:
    """Column names of the read-only bank's questions table (empty when it cannot be read)."""
    if db.postgres_enabled() and (con is None or isinstance(con, db._Postgres)):
        if con is None:
            return frozenset()
        try:
            rows = con.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='bank' AND table_name='questions'"
            ).fetchall()
        except Exception:
            return frozenset()
        return frozenset(row[0] for row in rows)
    path = _bank_dir() / "题库.db"
    try:
        stat = path.stat()
    except OSError:
        return frozenset()
    try:
        return _sqlite_question_columns(str(path.resolve()), stat.st_mtime_ns, stat.st_size)
    except sqlite3.Error:
        return frozenset()


@lru_cache(maxsize=8)
def _sqlite_group_columns(path: str, mtime_ns: int, size: int) -> frozenset[str]:
    probe = sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True, timeout=10)
    try:
        return frozenset(row[1] for row in probe.execute("PRAGMA table_info(groups)"))
    finally:
        probe.close()


def _bank_group_columns(con: sqlite3.Connection | None = None) -> frozenset[str]:
    """Column names of the read-only bank's groups table (empty when it cannot be read)."""
    if db.postgres_enabled() and (con is None or isinstance(con, db._Postgres)):
        if con is None:
            return frozenset()
        try:
            rows = con.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='bank' AND table_name='groups'"
            ).fetchall()
        except Exception:
            return frozenset()
        return frozenset(row[0] for row in rows)
    path = _bank_dir() / "题库.db"
    try:
        stat = path.stat()
    except OSError:
        return frozenset()
    try:
        return _sqlite_group_columns(str(path.resolve()), stat.st_mtime_ns, stat.st_size)
    except sqlite3.Error:
        return frozenset()


@lru_cache(maxsize=16)
def _source_groups(subject):
    """SQLite omits group image metadata, so supplement from original JSONL."""
    result = {}
    bank_root = _bank_dir().resolve()
    source = (bank_root / subject / "groups.jsonl").resolve()
    if not source.is_relative_to(bank_root) or not source.is_file():
        return result
    with source.open(encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                continue
            group = json.loads(line)
            result[group["group_id"]] = {
                "material_images": group.get("material_images") or [],
            }
    return result


def _group_key(row):
    group_id = row.get("group_id")
    if not group_id:
        return None
    doc_id = row.get("doc_id")
    if not doc_id:
        return str(group_id)
    doc_id = str(doc_id)
    return group_id if str(group_id).startswith(doc_id + ":") else doc_id + ":" + str(group_id)


def _has_reference(text):
    return bool(text and re.search(r"[\w\u4e00-\u9fff]", re.sub(r"[_\W]+", "", text), re.UNICODE))


def _has_answer_marker(text) -> bool:
    value = str(text or "")
    return any(marker in value for marker in ANSWER_LEAK_MARKERS)


def _blocks_have_answer_marker(blocks) -> bool:
    for block in blocks or []:
        if isinstance(block, dict):
            if _has_answer_marker(block.get("text")) or _has_answer_marker(block.get("latex")):
                return True
    return False


def _redact_member_blocks(entries) -> list[dict]:
    """只读成员（含上下文成员）的块若混入答案解析，先剔除并标记数量，避免题组接口泄露。"""
    result = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        entry = dict(entry)
        blocks = entry.get("blocks")
        if isinstance(blocks, list):
            kept = [
                block for block in blocks
                if not (isinstance(block, dict) and (
                    _has_answer_marker(block.get("text")) or _has_answer_marker(block.get("latex"))
                ))
            ]
            if len(kept) != len(blocks):
                entry["blocks"] = kept
                entry["blocks_redacted"] = len(blocks) - len(kept)
        result.append(entry)
    return result


def _public_answer_leak(question: dict) -> bool:
    """公开题干、选项或共用材料里混入答案解析时，先隔离而不是展示给答题用户。"""
    if _has_answer_marker(question.get("stem_plain")):
        return True
    for key in ("stem_blocks", "shared_blocks"):
        if _blocks_have_answer_marker(question.get(key)):
            return True
    for option in question.get("options") or []:
        if not isinstance(option, dict):
            continue
        if _has_answer_marker(option.get("text")) or _blocks_have_answer_marker(option.get("blocks")):
            return True
    group = question.get("group") or {}
    if _blocks_have_answer_marker(group.get("shared_blocks")):
        return True
    return _has_answer_marker(group.get("material_text")) or _has_answer_marker(group.get("parent_stem"))


def _answer_leak_sql(con, *, prefix: str = "q", include_group: bool | None = None) -> tuple[list[str], list]:
    """检索时排除公开字段混入答案解析、或数据侧已隔离的题。

    两项判定都预计算在 ``bank_leak_flags``（构建时一次性检查题干/选项/共用材料/
    材料父题/共用块 7 个字段 + 隔离状态），检索期只做单列整型过滤；SQLite 与
    PostgreSQL 生成完全相同的条件，不再拼接 7 列字符串做 NOT LIKE 全表扫描。
    ``include_group`` 仅为兼容旧调用保留：标记表本身已包含题组材料。
    """
    clause = (
        f"NOT EXISTS (SELECT 1 FROM bank_leak_flags f "
        f"WHERE f.qid={prefix}.qid AND (f.leak=1 OR f.excluded=1))"
    )
    return [clause], []


def _group_answer_leak(stored: dict) -> bool:
    if _blocks_have_answer_marker(_dict_list(stored.get("shared_blocks_json"))):
        return True
    return _has_answer_marker(stored.get("material_text")) or _has_answer_marker(stored.get("parent_stem"))


_PUA_CHAR_RE = re.compile(r"[\ue000-\uf8ff]")
_BARE_ASSET_RE = re.compile(r"\[(?:图|公式)\d+\]")


def _block_public_problem(blocks) -> str | None:
    """公开块里的可见内容问题：私用区乱码、裸素材标记、答案解析标记。"""
    for block in blocks or []:
        if not isinstance(block, dict):
            continue
        text = str(block.get("text") or "")
        if _PUA_CHAR_RE.search(text):
            return "私用区字符"
        if _BARE_ASSET_RE.search(text):
            return "未解析的素材标记"
        marker = _has_answer_marker(text)
        if marker:
            return "答案解析标记"
    return None


def _group_public_issues(stored: dict) -> list[str]:
    """组级公开内容质量判定：材料、父题、共用块、只读成员块使用同一套规则。

    新格式 ``shared_blocks_json`` 非空时，旧列 ``material_text``/``parent_stem`` 只是
    共用块丢失结构后的文本投影，其中的 ``[图N]``/``[公式N]`` 是正常形态；此时不再
    按「未解析素材标记」拦截，但仍检查私用区字符、答案解析标记与共用块本身。
    """
    issues: list[str] = []
    if _group_answer_leak(stored):
        issues.append("共用材料混入答案解析")
    structured_shared = bool(_dict_list(stored.get("shared_blocks_json")))
    for label, text in (("材料", stored.get("material_text")), ("父题", stored.get("parent_stem"))):
        if _PUA_CHAR_RE.search(str(text or "")):
            issues.append("%s含私用区字符" % label)
        elif not structured_shared and _BARE_ASSET_RE.search(str(text or "")):
            issues.append("%s含未解析素材标记" % label)
    block_problem = _block_public_problem(_dict_list(stored.get("shared_blocks_json")))
    if block_problem:
        issues.append("共用块含" + block_problem)
    # 只读成员块在返回前会被剔除答案段，这里按剔除后的结果判定；被剔除的部分只提示复核。
    for entry in _redact_member_blocks(_dict_list(stored.get("member_index_json"))):
        if entry.get("in_bank") is not False:
            continue
        member_problem = _block_public_problem(_dict_list(entry.get("blocks")))
        if member_problem:
            issues.append("只读成员含" + member_problem)
            break
    return list(dict.fromkeys(issues))


def _quality(row, group):
    flags = []
    if not _has_reference(row.get("answer_text")):
        flags.append("参考答案为占位内容，暂不可判分")
    labels = [str(option.get("label", "")) for option in row.get("options", [])]
    for option in row.get("options", []):
        if re.search(r"(?:^|\s|[])([A-H])[.．、]\s*", option.get("text", "")):
            flags.append("疑似选项合并，自动判题暂停，待复核")
            break
    if len(set(labels)) != len(labels):
        flags.append("选项标签重复，自动判题暂停，待复核")
    reference = (row.get("answer_text") or "").strip().upper()
    if row.get("type") == "single_choice" and re.fullmatch(r"[A-H]{2,}", reference):
        flags.append("单选题参考答案含多个选项，题型与答案冲突，自动判题暂停")
    if re.fullmatch(r"[A-H]+", reference) and len(set(reference)) != len(reference):
        flags.append("参考选项重复，可能混入多题答案，自动判题暂停")
    if row.get("group_id") and not group:
        flags.append("题目缺少关联材料，待复核")
    if _group_key(row) in QUARANTINED_GROUPS:
        flags.append("原材料混入答案解析，已隔离待复核")
    if row.get("qid") in QUARANTINED_QUESTIONS - {"d2aa42179cd42df7:21"}:
        flags.append("原题干混入答案解析，已隔离待复核")
    if group and "[图]" in group.get("material_text", "") and not group.get("material_images"):
        flags.append("材料可能有未定位图片，建议核对原卷")
    return flags


def _stat_difficulty(graded_count: int, correct_count: int) -> tuple[str | None, float | None]:
    if not graded_count or graded_count < 20:
        acc = round(100.0 * correct_count / graded_count, 1) if graded_count else None
        return None, acc
    ratio = correct_count / graded_count
    acc = round(100.0 * ratio, 1)
    if ratio >= 0.75:
        return "简单", acc
    if ratio >= 0.45:
        return "中等", acc
    return "困难", acc


def _resolve_difficulty(meta_row, stat_total: int = 0, stat_correct: int = 0, include_ai: bool = False):
    admin_diff = meta_row["difficulty"] if meta_row and meta_row["difficulty"] and meta_row["difficulty"] != "待标注" else None
    ai_diff = meta_row["ai_difficulty"] if meta_row and meta_row["ai_difficulty"] else None
    ai_score = meta_row["ai_difficulty_score"] if meta_row else None
    stat_diff, stat_acc = _stat_difficulty(stat_total, stat_correct)
    if admin_diff:
        diff, source = admin_diff, "管理员标注"
    elif stat_diff:
        diff, source = stat_diff, "统计"
    elif include_ai and ai_diff:
        diff, source = ai_diff, "AI 预估"
    else:
        diff, source = "待标注", ""
    return {
        "difficulty": diff,
        "difficulty_source": source,
        "ai_difficulty": ai_diff,
        "ai_difficulty_score": ai_score,
        "stat_accuracy": stat_acc,
        "stat_attempts": stat_total,
    }


def load_question(qid: str) -> dict:
    """Trusted backend projection. Never return this dictionary without filtering."""
    with _bank() as con:
        row = con.execute(
            f"SELECT * FROM {_bank_table(con, 'questions')} WHERE qid=?", (qid,)
        ).fetchone()
        if row is None:
            raise HTTPException(404, "题目不存在")
        question = dict(row)
        question["options"] = _json(question.pop("options_json", None), [])
        question["images"] = _json(question.pop("images_json", None), [])
        # 快照契约 §5 新增列：缺失列时全部回落到旧行为，禁止 500。
        question["stem_blocks"] = _dict_list(question.pop("stem_blocks_json", None))
        question["options_blocks"] = _dict_list(question.pop("options_blocks_json", None))
        question["answer_blocks"] = _dict_list(question.pop("answer_blocks_json", None))
        question["solution_blocks"] = _dict_list(question.pop("solution_blocks_json", None))
        question["shared_blocks"] = _dict_list(question.pop("shared_blocks_json", None))
        question["member_index"] = _dict_list(question.pop("member_index_json", None))
        question["member_index"] = _overlay_member_live_status(
            con, _redact_member_blocks(question["member_index"]))
        question["data_quality_flags"] = _data_quality_flags(question.pop("quality_flags_json", None))
        question["bank_version"] = _text(question.get("bank_version"))
        question["source_question_id"] = _text(question.get("source_question_id")) or question.get("question_id")
        question["parent_source_question_id"] = _text(question.get("parent_source_question_id"))
        question["group_kind"] = _text(question.get("group_kind"))
        question["content_revision"] = _text(question.get("content_revision"))
        question["answer_revision"] = _text(question.get("answer_revision"))
        question["options"] = _merge_options(question["options"], question["options_blocks"])
        group = None
        key = _group_key(question)
        if key:
            stored = con.execute(
                f"SELECT * FROM {_bank_table(con, 'groups')} WHERE group_id=?", (key,)
            ).fetchone()
            if stored:
                stored = dict(stored)
                member_index = _dict_list(stored.get("member_index_json")) or question["member_index"]
                member_index = _overlay_member_live_status(con, _redact_member_blocks(member_index))
                shared_blocks = _dict_list(stored.get("shared_blocks_json")) or question["shared_blocks"]
                # 组级实时计数：快照 n_practice 可能因隔离过期，详情页与组接口必须一致。
                live_practice = sum(1 for entry in _group_member_view_list(member_index)
                                    if entry.get("in_bank") is not False and entry.get("available"))
                n_members = len(member_index) or (_int_or_none(stored.get("n_members"))
                                                  or _int_or_none(stored.get("n_children_total")) or 0)
                n_practice = live_practice
                group = {
                    "group_id": key, "material_text": stored.get("material_text") or "",
                    "parent_stem": stored.get("parent_stem") or "",
                    "section_title": stored.get("section_title"),
                    "material_kind": stored.get("material_kind"),
                    "n_children_total": stored.get("n_children_total"),
                    "material_images": _source_groups(question["subject"]).get(key, {}).get("material_images", []),
                    "group_kind": _text(stored.get("group_kind")) or question["group_kind"],
                    "parent_source_question_id": _text(stored.get("parent_source_question_id"))
                    or question["parent_source_question_id"],
                    "shared_blocks": shared_blocks,
                    "member_index": member_index,
                    "n_members": n_members,
                    "n_practice": n_practice,
                }
    question["group"] = group
    answer_leak = _public_answer_leak(question)
    rule_flags = _quality(question, group)
    if answer_leak and "原卷题干或共用材料混入答案解析，已停止作答，待数据修正" not in rule_flags:
        rule_flags.append("原卷题干或共用材料混入答案解析，已停止作答，待数据修正")
    question["grading_flags"] = rule_flags
    status = _text(question.get("practice_status")) or "practice"
    grading = _text(question.get("grading_status")) or "auto"
    question["practice_status"] = status
    question["grading_status"] = grading
    hard_quarantined = qid in QUARANTINED_QUESTIONS or key in QUARANTINED_GROUPS
    isolation = _active_isolations().get(qid)
    quarantined = hard_quarantined or isolation is not None or status == "quarantined" or answer_leak
    context_only = status == "context"
    question["quarantined"] = quarantined
    question["practice_allowed"] = not quarantined and not context_only
    flags = list(rule_flags)
    for flag in question["data_quality_flags"]:
        if flag not in flags:
            flags.append(flag)
    reason = None
    if quarantined:
        reasons = [flag for flag in rule_flags if "隔离" in flag]
        if status == "quarantined":
            reasons.append("数据侧标记为隔离")
        if isolation:
            reasons.append(isolation.get("reason") or "管理员按报错证据临时隔离")
        if answer_leak:
            reasons.append("原卷题干或共用材料混入答案解析")
        if not reasons:
            reasons.extend(question["data_quality_flags"][:2])
        reason = "；".join(dict.fromkeys(reasons)) or "数据复核"
        if status == "quarantined" and "数据侧标记为隔离，已进入数据复核队列" not in flags:
            flags.append("数据侧标记为隔离，已进入数据复核队列")
        if isolation and "管理员已按报错证据临时隔离，待校勘发布" not in flags:
            flags.append("管理员已按报错证据临时隔离，待校勘发布")
    elif context_only:
        flags.append("该题仅为上下文材料，不可作答")
    elif status != "practice" or grading != "auto":
        parts = []
        if grading != "auto":
            parts.append(f"grading_status:{grading}")
        if status != "practice":
            parts.append(f"practice_status:{status}")
        flags.append(f"{PENDING_GRADE_MESSAGE}（{'；'.join(parts)}）")
    question["quality_flags"] = flags
    question["quarantine_reason"] = reason
    question["answer"] = {
        "text": question.get("answer_text") or "", "kind": question.get("answer_kind"),
        "note": question.get("answer_note"), "solution": question.get("solution") or "",
    }
    question["has_answer"] = _has_reference(question.get("answer_text"))
    # R3 有 170 题只有块级解析（solution 纯文本为空）：解析块非空同样算有解析。
    question["has_solution"] = (
        bool((question.get("solution") or "").strip())
        or bool(question.get("solution_blocks"))
    )
    question["type_label"] = TYPE_LABELS.get(question["type"], question["type"])
    return question


def latest_attempt(user_id: int, qid: str):
    with connection() as con:
        row = con.execute("SELECT * FROM attempts WHERE user_id=? AND qid=? ORDER BY id DESC LIMIT 1", (user_id, qid)).fetchone()
    return dict(row) if row else None


def ensure_unlocked(user_id: int, qid: str):
    if latest_attempt(user_id, qid) is None:
        raise HTTPException(403, "请先提交作答选项或自己的作答图片，再查看答案与解析")


def _attempt_public(row):
    if not row:
        return None
    return {
        "id": row["id"], "qid": row["qid"], "answer_text": row["answer_text"],
        "result": row["result"], "grade_source": row["grade_source"],
        "has_image": bool(row.get("image_path")),
        "image_url": f"/api/attempt-image/{row['id']}" if row.get("image_path") else None,
        "report": _json(row.get("report_json"), None), "created_at": row["created_at"],
    }


def _question_kps_batch(con: sqlite3.Connection, qids: list[str]) -> dict[str, list[dict]]:
    if not qids:
        return {}
    marks = ",".join("?" for _ in qids)
    rows = con.execute(
        f"SELECT qk.qid, qk.kp_id, kp.name, kp.subject, qk.source, qk.confidence, qk.model "
        f"FROM question_kp qk JOIN knowledge_points kp ON kp.id=qk.kp_id "
        f"WHERE qk.qid IN ({marks}) ORDER BY qk.weight DESC, kp.id",
        qids,
    ).fetchall()
    out: dict[str, list[dict]] = {q: [] for q in qids}
    for r in rows:
        out.setdefault(r["qid"], []).append({
            "kp_id": r["kp_id"], "name": r["name"], "subject": r["subject"],
            "source": r["source"], "confidence": r["confidence"], "model": r["model"],
        })
    return out


def _question_methods_batch(con: sqlite3.Connection, qids: list[str]) -> dict[str, list[dict]]:
    if not qids:
        return {}
    marks = ",".join("?" for _ in qids)
    rows = con.execute(
        f"SELECT qm.qid, m.id AS method_id, m.name, m.subject, qm.source "
        f"FROM question_method qm JOIN methods m ON m.id=qm.method_id "
        f"WHERE qm.qid IN ({marks}) ORDER BY m.id",
        qids,
    ).fetchall()
    out: dict[str, list[dict]] = {q: [] for q in qids}
    for r in rows:
        out.setdefault(r["qid"], []).append({
            "method_id": r["method_id"], "name": r["name"], "subject": r["subject"], "source": r["source"],
        })
    return out


def _public_question(question, user_id, summary=False):
    attempt = latest_attempt(user_id, question["qid"])
    unlocked = attempt is not None
    allowed = ("qid", "subject", "year", "province", "paper_name", "paper_type", "type", "type_label",
               "number_label", "stem_plain", "options", "images", "objective", "has_answer", "has_solution",
               "quality_flags", "practice_allowed", "bank_version", "practice_status", "grading_status",
               "content_revision", "answer_revision", "parent_source_question_id", "group_kind",
               "stem_blocks", "options_blocks", "shared_blocks", "member_index")
    result = {key: question.get(key) for key in allowed}
    with connection() as con:
        metadata = con.execute("SELECT * FROM question_metadata WHERE qid=?", (question["qid"],)).fetchone()
        mastery = con.execute("SELECT level FROM mastery WHERE user_id=? AND qid=?", (user_id, question["qid"])).fetchone()
        stat_row = con.execute(
            "SELECT COUNT(*) AS total, SUM(CASE WHEN result='correct' THEN 1 ELSE 0 END) AS ok "
            "FROM attempts WHERE qid=? AND result!='pending'", (question["qid"],)
        ).fetchone()
        wrong = con.execute("SELECT * FROM wrong_questions WHERE user_id=? AND qid=?", (user_id, question["qid"])).fetchone()
        kps = _question_kps_batch(con, [question["qid"]]).get(question["qid"], [])
        methods = _question_methods_batch(con, [question["qid"]]).get(question["qid"], [])
        exp_count = con.execute(
            "SELECT COUNT(*) FROM ai_explanations WHERE user_id=? AND qid=?", (user_id, question["qid"])
        ).fetchone()[0]
        history = [] if summary else [
            _attempt_public(dict(r)) for r in con.execute(
                "SELECT * FROM attempts WHERE user_id=? AND qid=? ORDER BY id DESC LIMIT 20",
                (user_id, question["qid"]),
            ).fetchall()
        ]
    diff_info = _resolve_difficulty(metadata, stat_row["total"] or 0, stat_row["ok"] or 0)
    result.update(diff_info)
    result.update({
        "mastery": mastery[0] if mastery else "未掌握",
        "unlocked": unlocked,
        "latest_attempt": _attempt_public(attempt),
        "knowledge_points": kps,
        "methods": methods,
        "has_ai_explanation": exp_count > 0,
        "wrong_info": {
            "in_wrongbook": bool(wrong and not wrong["archived"]),
            "archived": bool(wrong and wrong["archived"]),
            "failures": wrong["failures"] if wrong else 0,
            "streak": wrong["streak"] if wrong else 0,
            "interval_days": wrong["interval_days"] if wrong else 1,
            "next_review_at": wrong["next_review_at"] if wrong else None,
            "error_tags": _json(wrong["error_tags_json"], []) if wrong else [],
        },
    })
    if not summary:
        result["group"] = question.get("group")
        result["attempts_history"] = history
        if unlocked:
            result["answer"] = question["answer"] if question["has_answer"] else None
            result["answer_text"] = question["answer_text"] if question["has_answer"] else ""
            result["solution"] = question.get("solution") or ""
            # 快照契约 §6.1：答案/解析块只在作答后返回。
            result["answer_blocks"] = question.get("answer_blocks") or []
            result["solution_blocks"] = question.get("solution_blocks") or []
    return result


def _fetch_bank_rows(con, qids: list[str]) -> list:
    """按给定 qid 顺序取回题库行（先排 qid 再取宽行，避免对全表宽行排序）。"""
    if not qids:
        return []
    marks = ",".join("?" for _ in qids)
    rows = con.execute(
        f"SELECT q.* FROM {_bank_table(con, 'questions')} q WHERE q.qid IN ({marks})", qids
    ).fetchall()
    by_qid = {row["qid"]: row for row in rows}
    return [by_qid[qid] for qid in qids if qid in by_qid]


def _batch_summaries(con: sqlite3.Connection, bank_rows: list[sqlite3.Row], user_id: int, include_ai_diff: bool = False) -> list[dict]:
    """Build public summary cards for a page of bank rows without N+1 queries."""
    if not bank_rows:
        return []
    qids = [r["qid"] for r in bank_rows]
    marks = ",".join("?" for _ in qids)
    meta_map = {r["qid"]: r for r in con.execute(f"SELECT * FROM question_metadata WHERE qid IN ({marks})", qids)}
    mastery_map = {
        r["qid"]: r["level"]
        for r in con.execute(f"SELECT qid, level FROM mastery WHERE user_id=? AND qid IN ({marks})", [user_id, *qids])
    }
    latest_map = {}
    for r in con.execute(
        f"SELECT a.* FROM attempts a INNER JOIN ("
        f"  SELECT qid, MAX(id) AS max_id FROM attempts WHERE user_id=? AND qid IN ({marks}) GROUP BY qid"
        f") t ON a.id = t.max_id",
        [user_id, *qids],
    ):
        latest_map[r["qid"]] = dict(r)
    stat_map = {
        r["qid"]: (r["total"] or 0, r["ok"] or 0)
        for r in con.execute(
            f"SELECT qid, COUNT(*) AS total, SUM(CASE WHEN result='correct' THEN 1 ELSE 0 END) AS ok "
            f"FROM attempts WHERE qid IN ({marks}) AND result!='pending' GROUP BY qid",
            qids,
        )
    }
    kp_map = _question_kps_batch(con, qids)
    method_map = _question_methods_batch(con, qids)
    exp_set = {
        r[0] for r in con.execute(
            f"SELECT DISTINCT qid FROM ai_explanations WHERE user_id=? AND qid IN ({marks})",
            [user_id, *qids],
        )
    }
    items = []
    for row in bank_rows:
        qid = row["qid"]
        options = _json(row["options_json"], [])
        images = _json(row["images_json"], [])
        attempt = latest_map.get(qid)
        st_total, st_ok = stat_map.get(qid, (0, 0))
        diff_info = _resolve_difficulty(meta_map.get(qid), st_total, st_ok, include_ai=include_ai_diff)
        practice_status = _text(_row_value(row, "practice_status")) or "practice"
        grading_status = _text(_row_value(row, "grading_status")) or "auto"
        isolated = qid in _active_isolations()
        q_dict = {
            "qid": qid,
            "subject": row["subject"],
            "year": row["year"],
            "province": row["province"],
            "paper_name": row["paper_name"],
            "paper_type": row["paper_type"],
            "type": row["type"],
            "type_label": TYPE_LABELS.get(row["type"], row["type"]),
            "number_label": row["number_label"],
            # 契约 §4.1：列表卡以匹配的 qid 指向具体小题，并补出原卷题号与题组位置。
            "group_id": _group_key({"group_id": _row_value(row, "group_id"), "doc_id": row["doc_id"]}),
            "group_role": _row_value(row, "group_role"),
            "stem_plain": row["stem_plain"],
            "options": options,
            "images": images,
            "objective": row["objective"],
            "has_answer": _has_reference(row["answer_text"]),
            # 与详情页同口径：纯文本解析为空但块级解析非空时也算「有解析」。
            "has_solution": (
                bool((row["solution"] or "").strip())
                or bool(_dict_list(_row_value(row, "solution_blocks_json")))
            ),
            "quality_flags": [],
            "practice_allowed": practice_status not in ("quarantined", "context") and not isolated,
            "isolated": isolated,
            "bank_version": _row_value(row, "bank_version"),
            "practice_status": practice_status,
            "grading_status": grading_status,
            "mastery": mastery_map.get(qid, "未掌握"),
            "unlocked": attempt is not None,
            "latest_attempt": _attempt_public(attempt),
            "knowledge_points": kp_map.get(qid, []),
            "methods": method_map.get(qid, []),
            "has_ai_explanation": qid in exp_set,
        }
        q_dict.update(diff_info)
        items.append(q_dict)
    return items


def _excluded_sql(alias: str = "q"):
    prefix = f"{alias}." if alias else ""
    group_marks = ",".join("?" for _ in QUARANTINED_GROUPS)
    q_marks = ",".join("?" for _ in QUARANTINED_QUESTIONS)
    # R3 有 796 题 group_id 已带 ``<doc>:`` 前缀，另一种仍是裸 group_id；
    # 两种形态都要能匹配到隔离题组，否则带前缀的行会漏出隔离名单。
    group_ref = f"{prefix}doc_id || ':' || {prefix}group_id"
    clause = (
        f"{prefix}qid NOT IN ({q_marks}) AND "
        f"({prefix}group_id IS NULL OR ("
        f"{prefix}group_id NOT IN ({group_marks}) AND "
        f"{group_ref} NOT IN ({group_marks})))"
    )
    values = [*sorted(QUARANTINED_QUESTIONS), *sorted(QUARANTINED_GROUPS), *sorted(QUARANTINED_GROUPS)]
    # 管理员临时隔离的题同样不进入检索与练习列表（可随时撤销）。
    isolated = sorted(_active_isolations())
    if isolated:
        isolated_marks = ",".join("?" for _ in isolated)
        clause += f" AND {prefix}qid NOT IN ({isolated_marks})"
        values.extend(isolated)
    return clause, values


def _expand_kp_ids(con: sqlite3.Connection, kp_ids: list[int]) -> list[int]:
    if not kp_ids:
        return []
    rows = con.execute("SELECT id, parent_id FROM knowledge_points").fetchall()
    children: dict[int, list[int]] = {}
    for r in rows:
        if r["parent_id"] is not None:
            children.setdefault(r["parent_id"], []).append(r["id"])
    expanded = set(kp_ids)
    stack = list(kp_ids)
    while stack:
        cur = stack.pop()
        for ch in children.get(cur, []):
            if ch not in expanded:
                expanded.add(ch)
                stack.append(ch)
    return sorted(expanded)


def _split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [v.strip() for v in value.split(",") if v.strip()]


def _seen_review_set(seen_reviews: str | None, items: list[dict]) -> set[str]:
    """本页已展示的 qid + 客户端回传的历史槽位（最多 200 项，避免撑爆 SQL 变量上限）。"""
    seen = set(_split_csv(seen_reviews)[:MAX_SEEN_REVIEWS])
    seen.update(item["qid"] for item in items)
    return seen


def _contains_pattern(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _has_solution_sql(columns, prefix: str = "q") -> tuple[str, str]:
    """``(有解析条件, 无解析条件)``：与详情页 has_solution 同口径（纯文本或块级解析）。"""
    text = f"({prefix}.solution IS NOT NULL AND TRIM({prefix}.solution) != '')"
    if "solution_blocks_json" not in columns:
        return text, f"(NOT {text})"
    blocks = (
        f"({prefix}.solution_blocks_json IS NOT NULL "
        f"AND TRIM({prefix}.solution_blocks_json) NOT IN ('', '[]', 'null'))"
    )
    return f"({text} OR {blocks})", f"(NOT {text} AND NOT {blocks})"


def _excluded_search_sql(prefix: str = "q") -> tuple[list[str], list]:
    """检索期仍需动态排除管理员临时隔离的题（其余隔离/泄密已进 bank_leak_flags）。"""
    isolated = sorted(_active_isolations())
    if not isolated:
        return [], []
    marks = ",".join("?" for _ in isolated)
    return [f"{prefix}qid NOT IN ({marks})"], isolated


def _build_search_sql(
    con: sqlite3.Connection,
    user_id: int,
    *,
    subject: str | None = None,
    type: str | None = None,
    province: str | None = None,
    year: int | None = None,
    year_from: int | None = None,
    year_to: int | None = None,
    difficulty: str | None = None,
    include_ai_difficulty: bool = False,
    kp_id: str | None = None,
    method_id: str | None = None,
    status: str | None = None,
    has_answer: bool | None = None,
    has_solution: bool | None = None,
    has_ai_explanation: bool | None = None,
    q: str | None = None,
):
    # 检索索引（泄密/隔离标记 + 题干/材料镜像）按题库摘要预计算；旧库/换版时自动补齐。
    ensure_leak_flags(con)
    # 公开字段混入答案解析、context/quarantined 状态：单列整型过滤，双引擎同口径。
    leak_clauses, values = _answer_leak_sql(con)
    clauses = list(leak_clauses)
    iso_clauses, iso_values = _excluded_search_sql("q")
    clauses.extend(iso_clauses)
    values.extend(iso_values)
    joins = []

    subjects = _split_csv(subject)
    if subjects:
        clauses.append("q.subject IN (" + ",".join("?" for _ in subjects) + ")")
        values.extend(subjects)

    types = _split_csv(type)
    if types:
        clauses.append("q.type IN (" + ",".join("?" for _ in types) + ")")
        values.extend(types)

    provinces = _split_csv(province)
    if provinces:
        clauses.append("q.province IN (" + ",".join("?" for _ in provinces) + ")")
        values.extend(provinces)

    if year is not None:
        clauses.append("q.year = ?")
        values.append(year)
    if year_from is not None:
        clauses.append("q.year >= ?")
        values.append(year_from)
    if year_to is not None:
        clauses.append("q.year <= ?")
        values.append(year_to)

    if has_answer is True:
        clauses.append("q.answer_text IS NOT NULL AND TRIM(REPLACE(REPLACE(q.answer_text,'_',''),'＿','')) != ''")
    elif has_answer is False:
        clauses.append("(q.answer_text IS NULL OR TRIM(REPLACE(REPLACE(q.answer_text,'_',''),'＿','')) = '')")

    if has_solution is True:
        # has_solution 预计算在索引表：含「只有块级解析」的题（与详情页同口径）。
        clauses.append(
            "EXISTS (SELECT 1 FROM bank_leak_flags f WHERE f.qid=q.qid AND f.has_solution=1)"
        )
    elif has_solution is False:
        clauses.append(
            "NOT EXISTS (SELECT 1 FROM bank_leak_flags f WHERE f.qid=q.qid AND f.has_solution=1)"
        )

    if has_ai_explanation is True:
        clauses.append("EXISTS (SELECT 1 FROM ai_explanations ae WHERE ae.user_id=? AND ae.qid=q.qid)")
        values.append(user_id)
    elif has_ai_explanation is False:
        clauses.append("NOT EXISTS (SELECT 1 FROM ai_explanations ae WHERE ae.user_id=? AND ae.qid=q.qid)")
        values.append(user_id)

    if kp_id:
        try:
            raw_ids = [int(x) for x in _split_csv(kp_id)]
        except ValueError:
            raise HTTPException(422, "知识点 ID 格式无效") from None
        all_kp_ids = _expand_kp_ids(con, raw_ids)
        if all_kp_ids:
            marks = ",".join("?" for _ in all_kp_ids)
            clauses.append(f"EXISTS (SELECT 1 FROM question_kp qk WHERE qk.qid=q.qid AND qk.kp_id IN ({marks}))")
            values.extend(all_kp_ids)

    if method_id:
        try:
            m_ids = [int(x) for x in _split_csv(method_id)]
        except ValueError:
            raise HTTPException(422, "题型方法 ID 格式无效") from None
        if m_ids:
            marks = ",".join("?" for _ in m_ids)
            clauses.append(f"EXISTS (SELECT 1 FROM question_method qme WHERE qme.qid=q.qid AND qme.method_id IN ({marks}))")
            values.extend(m_ids)

    if difficulty:
        if difficulty not in DIFFICULTIES:
            raise HTTPException(422, "难度筛选值无效")
        joins.append("LEFT JOIN question_metadata qm ON qm.qid = q.qid")
        # 3-source effective difficulty in SQL: admin -> statistical (>=20 graded attempts) -> optional AI -> 待标注
        stat_sub = (
            "(SELECT CASE "
            "WHEN COUNT(*) < 20 THEN NULL "
            "WHEN 1.0 * SUM(CASE WHEN result='correct' THEN 1 ELSE 0 END) / COUNT(*) >= 0.75 THEN '简单' "
            "WHEN 1.0 * SUM(CASE WHEN result='correct' THEN 1 ELSE 0 END) / COUNT(*) >= 0.45 THEN '中等' "
            "ELSE '困难' END FROM attempts a_st WHERE a_st.qid=q.qid AND a_st.result!='pending')"
        )
        eff_expr = (
            f"COALESCE("
            f"NULLIF(NULLIF(qm.difficulty, ''), '待标注'), "
            f"{stat_sub}, "
            + ("NULLIF(qm.ai_difficulty, ''), " if include_ai_difficulty else "")
            + "'待标注')"
        )
        clauses.append(f"{eff_expr} = ?")
        values.append(difficulty)

    if status:
        if status == "unattempted":
            clauses.append("NOT EXISTS (SELECT 1 FROM attempts att WHERE att.user_id=? AND att.qid=q.qid)")
            values.append(user_id)
        elif status == "wrong":
            clauses.append("EXISTS (SELECT 1 FROM wrong_questions wq WHERE wq.user_id=? AND wq.qid=q.qid AND wq.archived=0)")
            values.append(user_id)
        elif status == "due":
            clauses.append("EXISTS (SELECT 1 FROM wrong_questions wq WHERE wq.user_id=? AND wq.qid=q.qid AND wq.archived=0 AND wq.next_review_at<=?)")
            values.extend([user_id, utcnow()])
        elif status in LEVELS:
            if status == "未掌握":
                clauses.append("(NOT EXISTS (SELECT 1 FROM mastery ms WHERE ms.user_id=? AND ms.qid=q.qid) OR EXISTS (SELECT 1 FROM mastery ms WHERE ms.user_id=? AND ms.qid=q.qid AND ms.level='未掌握'))")
                values.extend([user_id, user_id])
            else:
                clauses.append("EXISTS (SELECT 1 FROM mastery ms WHERE ms.user_id=? AND ms.qid=q.qid AND ms.level=?)")
                values.extend([user_id, status])
        else:
            raise HTTPException(422, "作答状态筛选值无效")

    if q and q.strip():
        terms = [t for t in re.split(r"\s+", q.strip()) if t]
        if db.postgres_enabled():
            for term in terms:
                clauses.append(_PG_BANK_CONTAINS)
                values.extend([_contains_pattern(term)] * 3)
        else:
            fts_terms = [t for t in terms if len(t) >= 3]
            if fts_terms:
                ensure_fts_index(con)
                fts_query = " AND ".join('"' + t.replace('"', '""') + '"' for t in fts_terms)
                clauses.append("q.qid IN (SELECT qid FROM bank_fts WHERE bank_fts MATCH ?)")
                values.append(fts_query)
            for term in terms:
                if len(term) < 3:
                    # 2 字词无法用 trigram FTS：查预计算的小镜像表，避免逐行读宽行 LIKE 全扫。
                    clauses.append(
                        "q.qid IN (SELECT sm.qid FROM bank_search_meta sm "
                        "WHERE sm.stem_plain LIKE ? ESCAPE '\\' "
                        "OR sm.paper_name LIKE ? ESCAPE '\\' "
                        "OR sm.material_text LIKE ? ESCAPE '\\')"
                    )
                    values.extend([_contains_pattern(term)] * 3)

    return " ".join(joins), " AND ".join(clauses), values


def search_similar_questions(text: str, subject: str | None = None, limit: int = 5) -> list[dict]:
    """Find similar bank questions using trigram search and character overlap scoring."""
    cleaned = re.sub(r"[\s\W]+", "", text or "")
    if len(cleaned) < 4:
        return []
    # Extract representative 4-6 char chunks from the stem for trigram matching
    step = max(1, len(cleaned) // 8)
    snippets = []
    for i in range(0, max(1, len(cleaned) - 3), step):
        seg = cleaned[i:i + 5]
        if len(seg) >= 3 and seg not in snippets:
            snippets.append(seg)
        if len(snippets) >= 6:
            break
    if not snippets:
        return []
    with _app_with_bank() as con:
        ensure_leak_flags(con)
        excl_clauses, excl_params = _answer_leak_sql(con, include_group=False)
        excl_clauses = list(excl_clauses)
        excl_params = list(excl_params)
        iso_clauses, iso_values = _excluded_search_sql("q")
        excl_clauses.extend(iso_clauses)
        excl_params.extend(iso_values)
        excl = " AND ".join(excl_clauses)
        questions = _bank_table(con, "questions")
        sql = "SELECT q.qid, q.subject, q.year, q.province, q.paper_name, q.type, q.stem_plain, q.has_figure "
        if db.postgres_enabled():
            contains_any = " OR ".join([_PG_BANK_CONTAINS] * len(snippets))
            sql += f"FROM {questions} q WHERE ({contains_any}) AND {excl}"
            params = [value for snippet in snippets for value in [_contains_pattern(snippet)] * 3]
            params.extend(excl_params)
        else:
            ensure_fts_index(con)
            fts_expr = " OR ".join('"' + s.replace('"', '""') + '"' for s in snippets)
            sql += (f"FROM bank_fts f JOIN {questions} q ON q.qid = f.qid "
                    f"WHERE bank_fts MATCH ? AND {excl}")
            params = [fts_expr, *excl_params]
        if subject:
            sql += " AND q.subject = ?"
            params.append(subject)
        sql += " LIMIT 40"
        candidates = con.execute(sql, params).fetchall()
    # Combine trigram Jaccard & n-gram vector cosine similarity for higher recall/precision
    from .search_vector import text_vector_similarity
    src_grams = {cleaned[i:i + 3] for i in range(len(cleaned) - 2)}
    scored = []
    for r in candidates:
        cand_clean = re.sub(r"[\s\W]+", "", r["stem_plain"] or "")
        if len(cand_clean) < 3:
            continue
        cand_grams = {cand_clean[i:i + 3] for i in range(len(cand_clean) - 2)}
        inter = len(src_grams & cand_grams)
        union = len(src_grams | cand_grams) or 1
        jacc = inter / union
        vec_sim = text_vector_similarity(text, r["stem_plain"] or "")
        score = round(max(jacc, 0.5 * jacc + 0.5 * vec_sim), 3)
        if score >= 0.12:
            scored.append({
                "qid": r["qid"], "subject": r["subject"], "year": r["year"],
                "province": r["province"], "paper_name": r["paper_name"],
                "type": r["type"], "type_label": TYPE_LABELS.get(r["type"], r["type"]),
                "stem_plain": (r["stem_plain"] or "")[:200],
                "similarity": score,
                "exact_match": score >= 0.65,
            })
    scored.sort(key=lambda x: x["similarity"], reverse=True)
    return scored[:limit]


def get_user_prefs(user_id: int) -> dict:
    with connection() as con:
        row = con.execute("SELECT prefs_json FROM user_preferences WHERE user_id=?", (user_id,)).fetchone()
    saved = _json(row["prefs_json"], {}) if row else {}
    return {**DEFAULT_PREFS, **saved}


class PreferencesPatch(BaseModel):
    interleave_per_page: int | None = Field(default=None, ge=0, le=2)
    same_kp_only: bool | None = None
    show_review_strip: bool | None = None
    ai_mode: str | None = Field(default=None, pattern="^(fast|accurate)$")


@router.get("/preferences")
def read_preferences(user=Depends(current_user)):
    return get_user_prefs(user["id"])


@router.patch("/preferences")
def update_preferences(data: PreferencesPatch, user=Depends(current_user)):
    prefs = get_user_prefs(user["id"])
    for k, v in data.model_dump(exclude_none=True).items():
        prefs[k] = v
    with connection() as con:
        con.execute(
            "INSERT INTO user_preferences(user_id, prefs_json, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET prefs_json=excluded.prefs_json, updated_at=excluded.updated_at",
            (user["id"], json.dumps(prefs, ensure_ascii=False), utcnow()),
        )
    return prefs


# /api/catalog 结果按「题库摘要 + 临时隔离集合」进程内缓存：题库与隔离不变时直接复用，
# 消除每请求 6 次全表扫描；题库换版/管理员隔离变化自动失效。
_CATALOG_CACHE: dict = {"key": None, "value": None}
_CATALOG_LOCK = threading.Lock()


def _catalog_cache_key() -> tuple:
    return (_bank_content_digest(), tuple(sorted(_active_isolations())))


def _build_catalog() -> dict:
    with _bank() as con:
        questions = _bank_table(con, "questions")
        total = con.execute(f"SELECT COUNT(*) FROM {questions}").fetchone()[0]
        clause, params = _excluded_sql("")
        status_clause = ""
        if "practice_status" in _bank_question_columns(con):
            # context 不可作答、quarantined 已隔离，两者都不计入可练总数。
            status_clause = " AND COALESCE(practice_status,'practice') NOT IN ('context','quarantined')"
        available = con.execute(
            f"SELECT COUNT(*) FROM {questions} WHERE " + clause + status_clause, params
        ).fetchone()[0]
        return {
            "subjects": [r[0] for r in con.execute(f"SELECT DISTINCT subject FROM {questions} ORDER BY subject")],
            "types": [r[0] for r in con.execute(f"SELECT DISTINCT type FROM {questions} ORDER BY type")],
            "provinces": [r[0] for r in con.execute(
                f"SELECT DISTINCT province FROM {questions} WHERE province IS NOT NULL ORDER BY province")],
            "years": [r[0] for r in con.execute(f"SELECT DISTINCT year FROM {questions} ORDER BY year DESC")],
            "total": total, "source_total": total, "available_total": available,
            "quarantined_count": total - available, "difficulties": list(DIFFICULTIES),
            "type_labels": TYPE_LABELS, "error_tag_presets": list(ERROR_TAG_PRESETS),
            "difficulty_note": "难度按「管理员标注 → 统计难度(≥20人次) → AI预估(可选)」顺序显示",
        }


@router.get("/catalog")
def catalog(user=Depends(current_user)):
    key = _catalog_cache_key()
    if not key[0]:
        # 题库不可读：保持原有 503 语义（缓存不吞掉部署错误）。
        raise HTTPException(503, "未找到题库，请检查 STUDY_BANK_DIR 配置")
    if _CATALOG_CACHE["key"] == key and _CATALOG_CACHE["value"] is not None:
        return _CATALOG_CACHE["value"]
    with _CATALOG_LOCK:
        if _CATALOG_CACHE["key"] == key and _CATALOG_CACHE["value"] is not None:
            return _CATALOG_CACHE["value"]
        result = _build_catalog()
        _CATALOG_CACHE["key"] = key
        _CATALOG_CACHE["value"] = result
        return result


def invalidate_catalog_cache() -> None:
    """题库/隔离变化后强制失效（测试与运维脚本可显式调用）。"""
    _CATALOG_CACHE["key"] = None
    _CATALOG_CACHE["value"] = None


def _days_since(iso_str: str | None) -> int:
    if not iso_str:
        return 0
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0, (datetime.now(timezone.utc) - dt).days)
    except ValueError:
        return 0


@router.get("/questions")
def list_questions(
    subject: str | None = None,
    type: str | None = None,
    province: str | None = None,
    year: int | None = None,
    year_from: int | None = None,
    year_to: int | None = None,
    difficulty: str | None = None,
    include_ai_difficulty: bool = False,
    kp_id: str | None = None,
    method_id: str | None = None,
    status: str | None = None,
    has_answer: bool | None = None,
    has_solution: bool | None = None,
    has_ai_explanation: bool | None = None,
    sort: str = Query("year_desc", pattern="^(year_desc|year_asc|difficulty|accuracy|random)$"),
    q: str | None = Query(None, max_length=200),
    include_reviews: bool = False,
    seen_reviews: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(12, ge=1, le=50),
    user=Depends(current_user),
):
    order_map = {
        "year_desc": "q.year DESC, q.subject, q.doc_id, q.order_in_paper",
        "year_asc": "q.year ASC, q.subject, q.doc_id, q.order_in_paper",
        "difficulty": "CASE (SELECT difficulty FROM question_metadata WHERE qid=q.qid) WHEN '困难' THEN 1 WHEN '中等' THEN 2 WHEN '简单' THEN 3 ELSE 4 END, q.year DESC",
        "accuracy": "COALESCE((SELECT 1.0*SUM(CASE WHEN result='correct' THEN 1 ELSE 0 END)/NULLIF(COUNT(*),0) FROM attempts WHERE qid=q.qid AND result!='pending'), 2.0) ASC, q.year DESC",
        "random": "RANDOM()",
    }
    with _app_with_bank() as con:
        join_sql, where_sql, params = _build_search_sql(
            con, user["id"], subject=subject, type=type, province=province,
            year=year, year_from=year_from, year_to=year_to,
            difficulty=difficulty, include_ai_difficulty=include_ai_difficulty,
            kp_id=kp_id, method_id=method_id, status=status, has_answer=has_answer,
            has_solution=has_solution, has_ai_explanation=has_ai_explanation, q=q,
        )
        questions = _bank_table(con, "questions")
        total = con.execute(f"SELECT COUNT(*) FROM {questions} q {join_sql} WHERE {where_sql}", params).fetchone()[0]
        # 先只取排好序的 qid，再按 qid 取回 12 行宽数据：排序不再搬运整行导致翻页变慢。
        qid_rows = con.execute(
            f"SELECT q.qid FROM {questions} q {join_sql} WHERE {where_sql} "
            f"ORDER BY {order_map[sort]} LIMIT ? OFFSET ?",
            [*params, page_size, (page - 1) * page_size],
        ).fetchall()
        rows = _fetch_bank_rows(con, [row[0] for row in qid_rows])
        items = _batch_summaries(con, rows, user["id"], include_ai_diff=include_ai_difficulty)
        review_slots = []

        if include_reviews and items:
            prefs = get_user_prefs(user["id"])
            max_slots = min(2, int(prefs.get("interleave_per_page", 2)))
            if max_slots > 0:
                seen_set = _seen_review_set(seen_reviews, items)
                rev_where = where_sql + " AND EXISTS (SELECT 1 FROM wrong_questions wq_i WHERE wq_i.user_id=? AND wq_i.qid=q.qid AND wq_i.archived=0 AND wq_i.next_review_at<=?)"
                rev_params = [*params, user["id"], utcnow()]
                # §4.1: same_kp_only restricts interleaved wrong items to KPs present on current page when kp_id filter is not set
                if prefs.get("same_kp_only") and not kp_id:
                    page_kp_ids = sorted({kp["kp_id"] for it in items for kp in it.get("knowledge_points", [])})
                    if page_kp_ids:
                        kp_marks = ",".join("?" for _ in page_kp_ids)
                        rev_where += f" AND EXISTS (SELECT 1 FROM question_kp qk_s WHERE qk_s.qid=q.qid AND qk_s.kp_id IN ({kp_marks}))"
                        rev_params.extend(page_kp_ids)
                    else:
                        rev_where += " AND 0=1"
                if seen_set:
                    rev_where += " AND q.qid NOT IN (" + ",".join("?" for _ in seen_set) + ")"
                    rev_params.extend(sorted(seen_set))
                due_rows = con.execute(
                    f"SELECT q.*, wq_r.last_wrong_at, wq_r.added_at AS wrong_added_at, wq_r.failures, wq_r.error_tags_json, wq_r.next_review_at "
                    f"FROM {questions} q {join_sql} "
                    f"JOIN wrong_questions wq_r ON wq_r.qid=q.qid AND wq_r.user_id=? "
                    f"WHERE {rev_where} ORDER BY wq_r.next_review_at ASC LIMIT ?",
                    [user["id"], *rev_params, max_slots],
                ).fetchall()
                if due_rows:
                    due_cards = _batch_summaries(con, due_rows, user["id"], include_ai_diff=include_ai_difficulty)
                    insert_positions = [3, 8]
                    for idx, (card, raw) in enumerate(zip(due_cards, due_rows)):
                        days = _days_since(raw["last_wrong_at"] or raw["wrong_added_at"])
                        card["slot"] = "review"
                        card["days_since_wrong"] = days
                        card["failures"] = raw["failures"]
                        card["error_tags"] = _json(raw["error_tags_json"], [])
                        card["review_label"] = f"上次错于 {days} 天前" if days > 0 else "今天答错待复习"
                        pos = min(insert_positions[idx], len(items))
                        items.insert(pos, card)
                        review_slots.append({"index": pos, "qid": card["qid"], "days_since_wrong": days})

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "review_slots": review_slots,
    }


@router.get("/questions/{qid:path}/issues")
def list_my_question_issues(qid: str, user=Depends(current_user)):
    """只返回本人提交的报错，避免泄露其他用户的反馈内容。"""
    with connection() as con:
        rows = con.execute(
            "SELECT * FROM content_issues WHERE qid=? AND reporter_id=? ORDER BY id DESC LIMIT 20",
            (qid, user["id"]),
        ).fetchall()
    return {"items": [_issue_public(row) for row in rows]}


def _detail_blocked_message(question) -> str:
    message = QUARANTINE_DETAIL_MESSAGE
    reason = question.get("quarantine_reason")
    return f"{message}（隔离原因：{reason}）" if reason else message


@router.get("/questions/{qid:path}")
def question_detail(qid: str, user=Depends(current_user)):
    question = load_question(qid)
    if question["quarantined"]:
        raise HTTPException(409, _detail_blocked_message(question))
    # context 成员可读但不可作答（快照契约 §6.1），作答门禁在 POST /api/attempts。
    return _public_question(question, user["id"])


def _live_member_status(con, qids: list[str]) -> dict[str, dict]:
    """按题库实时状态叠加硬隔离与管理员隔离，得到成员当前是否可作答。

    member_index 是构建期快照；题目被隔离后快照不会自动更新，因此入口和计数必须以这里为准。
    """
    wanted = [qid for qid in qids if qid]
    if not wanted:
        return {}
    status: dict[str, dict] = {}
    marks = ",".join("?" for _ in wanted)
    columns = _bank_question_columns(con)
    select = ["qid"]
    for column in ("practice_status", "grading_status"):
        if column in columns:
            select.append(column)
    rows = con.execute(
        "SELECT %s FROM %s WHERE qid IN (%s)"
        % (", ".join(select), _bank_table(con, "questions"), marks),
        wanted,
    ).fetchall()
    found = set()
    for row in rows:
        qid = row["qid"]
        found.add(qid)
        raw_status = _text(_row_value(row, "practice_status"))
        raw_grading = _text(_row_value(row, "grading_status"))
        status[qid] = {
            "practice_status": raw_status or "practice",
            "grading_status": raw_grading or "auto",
            # 快照列在旧行上可能为 NULL：此时以构建期 member_index 为准，不要用默认值覆盖。
            "has_status": bool(raw_status),
            "exists": True,
        }
    isolated = _active_isolations()
    for qid in wanted:
        entry = status.setdefault(qid, {
            "practice_status": "practice", "grading_status": "auto",
            "has_status": False, "exists": qid in found,
        })
        if qid in QUARANTINED_QUESTIONS:
            entry.update(practice_status="quarantined", grading_status="disabled", has_status=True)
        if qid in isolated:
            entry.update(practice_status="quarantined", grading_status="disabled",
                         has_status=True, isolated=True)
            entry.setdefault("isolation_reason", isolated[qid].get("reason"))
        entry["practice_allowed"] = (
            entry["exists"] and entry["practice_status"] in ("practice", "review")
        )
    return status


def _overlay_member_live_status(con, entries: list[dict]) -> list[dict]:
    """把 member_index 中的构建期状态替换为实时状态；不可作答成员单列原因。"""
    if not entries:
        return entries
    qids = [entry.get("qid") for entry in entries if entry.get("in_bank") is not False]
    live = _live_member_status(con, qids)
    for entry in entries:
        if entry.get("in_bank") is False:
            continue
        qid = entry.get("qid")
        state = live.get(qid) or {}
        if not state:
            continue
        if state.get("has_status"):
            entry["practice_status"] = state.get("practice_status") or entry.get("practice_status")
            entry["grading_status"] = state.get("grading_status") or entry.get("grading_status")
        entry["available"] = bool(state.get("practice_allowed"))
        if state.get("isolated"):
            entry["isolated"] = True
        if not state.get("exists"):
            entry["available"] = False
            entry["missing"] = True
    return entries


def _group_member_view(entry: dict) -> dict:
    in_bank = entry.get("in_bank") is not False
    qid = _text(entry.get("qid")) if in_bank else None
    status = _text(entry.get("practice_status")) or ("practice" if in_bank else "context")
    grading = _text(entry.get("grading_status")) or ("auto" if in_bank else "disabled")
    member = {
        "qid": qid, "in_bank": in_bank,
        "source_question_id": _text(entry.get("source_question_id")),
        "number_label": entry.get("number_label"),
        "sub_question_label": entry.get("sub_question_label"),
        "type": entry.get("type"), "group_role": entry.get("group_role"),
        "order": entry.get("order"),
        "practice_status": status, "grading_status": grading,
        # 是否可点开作答：由实时状态决定，前端据此禁用入口。
        "available": bool(entry.get("available", in_bank and status in ("practice", "review"))),
    }
    for flag in ("isolated", "missing", "isolation_reason"):
        if entry.get(flag):
            member[flag] = entry[flag]
    if not in_bank:
        blocks = _dict_list(entry.get("blocks"))
        if blocks:
            member["blocks"] = blocks
        if entry.get("blocks_redacted"):
            member["blocks_redacted"] = entry["blocks_redacted"]
    return member


def _group_member_view_list(entries: list[dict]) -> list[dict]:
    """轻量成员状态视图（不展开只读块），用于实时计数。"""
    out = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        out.append(_group_member_view(entry))
    return out


def _group_members(con, group_id: str, stored: dict) -> list[dict]:
    """Member summaries in original paper order. Never carries answers or solutions."""
    member_index = _dict_list(stored.get("member_index_json"))
    if member_index:
        entries = _overlay_member_live_status(con, _redact_member_blocks(member_index))
        return [_group_member_view(entry) for entry in entries]
    # 旧题库没有 member_index：回到用 questions 表反查成员（旧接口行为兜底）。
    questions = _bank_table(con, "questions")
    rows = con.execute(
        f"SELECT q.qid, q.number_label, q.group_role, q.order_in_paper, q.type FROM {questions} q "
        "WHERE CASE WHEN q.group_id IS NULL THEN NULL "
        "WHEN q.group_id LIKE q.doc_id || ':%' THEN q.group_id "
        "ELSE q.doc_id || ':' || q.group_id END = ? "
        "ORDER BY q.order_in_paper, q.qid",
        (group_id,),
    ).fetchall()
    members = []
    for row in rows:
        if row["qid"] in QUARANTINED_QUESTIONS:
            continue
        members.append({
            "qid": row["qid"], "in_bank": True, "source_question_id": None,
            "number_label": row["number_label"], "sub_question_label": None,
            "type": row["type"], "group_role": row["group_role"],
            "order": row["order_in_paper"], "practice_status": "practice",
            "grading_status": "auto",
        })
    return members


def _group_progress(con, user_id: int, qids: list[str]) -> dict[str, dict]:
    """Per-user progress summaries for group members; no answers are read here."""
    progress = {qid: {"attempted": False, "result": None, "mastery": "未掌握"} for qid in qids if qid}
    if not progress:
        return progress
    qid_list = list(progress)
    marks = ",".join("?" for _ in qid_list)
    for row in con.execute(
        f"SELECT qid, level FROM mastery WHERE user_id=? AND qid IN ({marks})",
        [user_id, *qid_list],
    ):
        progress[row["qid"]]["mastery"] = row["level"]
    for row in con.execute(
        f"SELECT a.qid, a.result FROM attempts a INNER JOIN ("
        f"  SELECT qid, MAX(id) AS max_id FROM attempts WHERE user_id=? AND qid IN ({marks}) GROUP BY qid"
        f") t ON a.id = t.max_id",
        [user_id, *qid_list],
    ):
        progress[row["qid"]]["attempted"] = True
        progress[row["qid"]]["result"] = row["result"]
    return progress


@router.get("/groups/{group_id:path}")
def group_detail(group_id: str, user=Depends(current_user)):
    """Group read model (snapshot contract §6.2). group_id contains ':' so use a path param."""
    if group_id in QUARANTINED_GROUPS:
        raise HTTPException(409, QUARANTINE_DETAIL_MESSAGE)
    with _app_with_bank() as con:
        stored = con.execute(
            f"SELECT * FROM {_bank_table(con, 'groups')} WHERE group_id=?", (group_id,)
        ).fetchone()
        if stored is None:
            raise HTTPException(404, "题组不存在")
        stored = dict(stored)
        group_issues = _group_public_issues(stored)
        if group_issues:
            raise HTTPException(
                409,
                "该题组公开内容待复核，已暂停展示与作答（%s）" % "；".join(group_issues[:3]),
            )
        members = _group_members(con, group_id, stored)
        progress = _group_progress(con, user["id"], [member["qid"] for member in members if member["qid"]])
    for member in members:
        member["progress"] = progress.get(
            member["qid"], {"attempted": False, "result": None, "mastery": "未掌握"}
        )
    isolated = _active_isolations()
    for member in members:
        if member.get("qid") and member["qid"] in isolated:
            member["isolated"] = True
            if member.get("in_bank") is not False:
                member["practice_status"] = "quarantined"
                member["available"] = False
    # 可作答数量与入口统一按实时成员状态计算（快照里的 n_practice 可能已过期）。
    n_members = len(members) or (_int_or_none(stored.get("n_members")) or 0)
    practice_members = [member for member in members
                        if member["in_bank"] and member.get("available")]
    n_practice = len(practice_members)
    n_readonly = sum(1 for member in members if member["in_bank"] is False)
    n_unavailable = sum(1 for member in members
                        if member["in_bank"] and not member.get("available"))
    # 组级隔离：材料、父题与共用块都为空且没有可作答成员时，整组按「材料已隔离」返回。
    group_isolated = (
        not (stored.get("material_text") or "").strip()
        and not (stored.get("parent_stem") or "").strip()
        and not _dict_list(stored.get("shared_blocks_json"))
        and n_practice == 0
        and any(member.get("practice_status") == "quarantined" for member in members)
    )
    subject = stored.get("subject")
    return {
        "group_id": group_id,
        "bank_version": _text(stored.get("bank_version")),
        "subject": subject,
        "year": stored.get("year"),
        "group_kind": _text(stored.get("group_kind")),
        "parent_source_question_id": _text(stored.get("parent_source_question_id")),
        "parent_number_label": _parent_number_label(stored, members),
        "section_title": stored.get("section_title"),
        "shared_blocks": _dict_list(stored.get("shared_blocks_json")),
        "material_images": _source_groups(subject or "").get(group_id, {}).get("material_images", []),
        "n_members": n_members,
        "n_practice": n_practice,
        "n_readonly": n_readonly,
        "n_unavailable": n_unavailable,
        # 数据侧已隔离整组公开内容（乱码/无法确认来源）时返回结构化状态，
        # 而不是 200 + 空材料让页面显示半截内容。
        "isolated": bool(group_isolated),
        "notice": ("该题组材料在数据侧已隔离，仅保留历史题号与处理说明。" if group_isolated else None),
        "members": members,
    }


def _parent_number_label(stored: dict, members: list[dict]) -> str | None:
    """父题的原卷题号：优先取父题成员，其次取快照字段，供页面统一显示「原卷第 N 题」。"""
    for member in members:
        if member.get("group_role") == "parent" and member.get("number_label"):
            return str(member["number_label"])
    for member in members:
        label = str(member.get("number_label") or "")
        if label and not label.startswith(("（", "(")):
            return label
    parent = stored.get("parent_label")
    return str(parent) if parent else None


@router.get("/reveal")
def reveal(qid: str, user=Depends(current_user)):
    ensure_unlocked(user["id"], qid)
    return question_detail(qid, user)


@router.get("/attempts/history")
def attempt_history(qid: str, user=Depends(current_user)):
    with connection() as con:
        rows = con.execute(
            "SELECT * FROM attempts WHERE user_id=? AND qid=? ORDER BY id DESC LIMIT 30",
            (user["id"], qid),
        ).fetchall()
    return {"items": [_attempt_public(dict(r)) for r in rows]}


MAX_REVIEW_INTERVAL_DAYS = 36500


def _next_interval_for_mastery(level: str, current_interval: float, streak: int) -> float:
    prev = max(1.0, float(current_interval or 1.0))
    if level == "未掌握":
        return 1.0
    if level == "初步掌握":
        return 2.0
    if level == "比较掌握":
        return float(max(4, round(prev * 2.0)))
    if level == "熟练掌握":
        return float(max(7, round(prev * 2.5)))
    return float((1, 3, 7, 14, 30)[min(streak, 4)])


def _clamp_interval(days: float) -> float:
    return float(min(MAX_REVIEW_INTERVAL_DAYS, max(1.0, float(days or 1.0))))


def _schedule_result(con, user_id, qid, verdict):
    reviewed_at = datetime.now(timezone.utc)
    now = reviewed_at.isoformat()
    wrong = con.execute("SELECT * FROM wrong_questions WHERE user_id=? AND qid=?", (user_id, qid)).fetchone()
    active = wrong and not wrong["archived"]
    fsrs_interval = None
    stability = difficulty = last_review_at = None
    if (verdict == "incorrect" or (verdict == "correct" and active)) and fsrs_enabled(con):
        fsrs_interval, stability, difficulty = fsrs_review(
            verdict == "correct",
            wrong["stability"] if wrong else None,
            wrong["difficulty"] if wrong else None,
            wrong["last_review_at"] if wrong else None,
            reviewed_at,
        )
        last_review_at = now
    if verdict == "incorrect":
        due = (reviewed_at + timedelta(days=1)).isoformat()
        con.execute(
            "INSERT INTO wrong_questions(user_id,qid,reason,failures,streak,next_review_at,added_at,last_result,"
            "interval_days,review_count,archived,last_wrong_at,stability,difficulty,last_review_at,"
            "last_mastery_push_attempt) "
            "VALUES(?,?,'答题错误',1,0,?,?,?,1,0,0,?,?,?,?,NULL) "
            "ON CONFLICT(user_id,qid) DO UPDATE SET failures=wrong_questions.failures+1,"
            "streak=0,next_review_at=excluded.next_review_at,"
            "last_result=excluded.last_result,interval_days=1,archived=0,last_wrong_at=excluded.last_wrong_at,"
            "stability=excluded.stability,difficulty=excluded.difficulty,last_review_at=excluded.last_review_at,"
            "last_mastery_push_attempt=NULL",
            (user_id, qid, due, now, verdict, now, stability, difficulty, last_review_at),
        )
        con.execute(
            "INSERT INTO mastery(user_id,qid,level,updated_at) VALUES(?,?,'未掌握',?) "
            "ON CONFLICT(user_id,qid) DO UPDATE SET level='未掌握',updated_at=excluded.updated_at",
            (user_id, qid, now),
        )
    elif verdict == "correct" and active:
        streak = wrong["streak"] + 1
        review_count = (wrong["review_count"] or 0) + 1
        if fsrs_interval is not None:
            interval = fsrs_interval
        else:
            mastery_row = con.execute("SELECT level FROM mastery WHERE user_id=? AND qid=?", (user_id, qid)).fetchone()
            level = mastery_row["level"] if mastery_row else "初步掌握"
            interval = _next_interval_for_mastery(level, wrong["interval_days"] or 1.0, streak)
        interval = _clamp_interval(interval)
        due = (reviewed_at + timedelta(days=interval)).isoformat()
        archived = 1 if streak >= 3 else 0
        # 新的一轮作答开始：允许随后 mark_mastery 再推升一次间隔。
        con.execute(
            "UPDATE wrong_questions SET streak=?,review_count=?,interval_days=?,next_review_at=?,last_result=?,archived=?,"
            "stability=?,difficulty=?,last_review_at=?,last_mastery_push_attempt=NULL "
            "WHERE user_id=? AND qid=?",
            (streak, review_count, interval, due, verdict, archived, stability, difficulty, last_review_at, user_id, qid),
        )
    elif verdict == "pending":
        con.execute(
            "UPDATE mastery SET level='初步掌握',updated_at=? WHERE user_id=? AND qid=? AND level IN ('比较掌握','熟练掌握')",
            (now, user_id, qid),
        )


def _check_not_blank(image: Image.Image):
    """Lightweight Pillow variance & edge-density check to prevent blank-image unlocking."""
    w, h = image.size
    if w < 100 or h < 100:
        # 小尺寸图片本身就是绕过空白检测的漏洞，直接拒绝；不得为测试夹具保留豁免。
        raise HTTPException(422, "图片分辨率过低，请上传正常尺寸的作答图片")
    gray = image.convert("L")
    gray.thumbnail((512, 512))
    gw, gh = gray.size
    stddev = ImageStat.Stat(gray).stddev[0]
    edges = gray.filter(ImageFilter.FIND_EDGES).crop((2, 2, max(3, gw - 2), max(3, gh - 2)))
    edge_mean = ImageStat.Stat(edges).mean[0]
    if stddev < 2.0 and edge_mean < 1.5:
        raise HTTPException(422, "上传的图片接近空白，请上传包含实际手写或作答内容的清晰图片")


def _save_upload_image(content, folder: str = "attempts"):
    if len(content) > MAX_IMAGE_BYTES:
        raise HTTPException(413, "图片不能超过 10 MB")
    try:
        with Image.open(BytesIO(content)) as image:
            if image.format not in ("PNG", "JPEG", "WEBP"):
                raise HTTPException(415, "仅支持 PNG、JPEG、WebP 图片")
            if image.width * image.height > 25_000_000:
                raise HTTPException(413, "图片尺寸过大，请压缩后上传")
            image.load()
            transposed = ImageOps.exif_transpose(image).convert("RGB")
            _check_not_blank(transposed)
            output = BytesIO()
            transposed.save(output, format="JPEG", quality=92)
    except HTTPException:
        raise
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        raise HTTPException(415, "无法读取图片，请上传有效的图片文件") from None
    name = secrets.token_hex(20) + ".jpg"
    return storage.save_file(folder, name, output.getvalue(), "image/jpeg")


def _save_image(content):
    return _save_upload_image(content, "attempts")


def _choice_answer(text, options, multi):
    labels = {str(o["label"]).upper() for o in options}
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        parsed = text
    if isinstance(parsed, list):
        selections = [str(value).strip().upper() for value in parsed]
    elif isinstance(parsed, str):
        compact = re.sub(r"[\s,，、;；]+", "", parsed.upper())
        selections = list(compact)
    else:
        raise HTTPException(422, "请选择有效选项")
    if not selections or len(set(selections)) != len(selections) or not set(selections).issubset(labels):
        raise HTTPException(422, "提交的选项无效")
    if not multi and len(selections) != 1:
        raise HTTPException(422, "单选题只能提交一个选项")
    return "".join(sorted(selections))


def _record_attempt(user_id, qid, submitted, image_name, verdict, can_grade, now):
    """同步写入作答与复习调度；``async`` 端点经由 threadpool 调用，避免阻塞事件循环。"""
    with connection() as con:
        if db.postgres_enabled():
            db.write_lock(con, f"progress:{user_id}")
        cursor = con.execute(
            "INSERT INTO attempts(user_id,qid,answer_text,image_path,result,grade_source,created_at,graded_at) VALUES(?,?,?,?,?,?,?,?)",
            (user_id, qid, submitted, image_name, verdict, "reference" if can_grade else "pending", now, now if can_grade else None),
        )
        attempt_id = cursor.lastrowid
        _schedule_result(con, user_id, qid, verdict)
    return attempt_id


def _attempt_submission_payload(user_id, qid, question, verdict, attempt_id):
    return {
        "attempt": _attempt_public(latest_attempt(user_id, qid)),
        "question": _public_question(question, user_id),
        "result": verdict,
        "attempt_id": attempt_id,
    }


@router.post("/attempts")
async def submit_attempt(qid: str = Form(...), answer: str | None = Form(None), file: UploadFile | None = File(None), user=Depends(current_user)):
    # 题库/应用库读取都是同步 I/O：统一放 threadpool，别让单个作答把事件循环卡住。
    question = await run_in_threadpool(load_question, qid)
    if question["quarantined"]:
        raise HTTPException(409, QUARANTINE_SUBMIT_MESSAGE)
    if not question["practice_allowed"]:
        raise HTTPException(409, CONTEXT_SUBMIT_MESSAGE)
    if answer and len(answer) > 12000:
        raise HTTPException(413, "作答文字过长")
    content = await file.read(MAX_IMAGE_BYTES + 1) if file else None
    if file and not content:
        raise HTTPException(422, "作答图片不能为空")
    if not (answer and answer.strip()) and not content:
        raise HTTPException(422, "请提交作答选项或自己的作答图片")
    choice = bool(question["objective"] and question["options"])
    submitted = (answer or "").strip()
    if submitted and choice:
        submitted = _choice_answer(submitted, question["options"], question["type"] == "multi_choice")
    elif submitted and not content:
        raise HTTPException(422, "主观题请上传自己的作答图片后提交")
    image_name = await run_in_threadpool(_save_image, content) if content else None
    reference = question.get("answer_text", "").strip().upper()
    reference_valid = bool(re.fullmatch(r"[A-H]+", reference)) and set(reference).issubset({o["label"] for o in question["options"]})
    # 快照契约 §6.3：只有 practice+auto 的题才自动判分；review/pending/disabled 保留作答判 pending。
    # 数据侧标记「答案解析公式无法渲染」时也不能自动判分（作答仍保存）。
    answer_flags = " ".join(question.get("data_quality_flags") or [])
    formula_gap = "公式无法渲染" in answer_flags or "公式缺失" in answer_flags
    auto_grade = (question["practice_status"] == "practice"
                  and question["grading_status"] == "auto" and not formula_gap)
    can_grade = choice and submitted and reference_valid and auto_grade and not question["grading_flags"]
    verdict = ("correct" if set(submitted) == set(reference) else "incorrect") if can_grade else "pending"
    now = utcnow()
    try:
        attempt_id = await run_in_threadpool(
            _record_attempt, user["id"], qid, submitted, image_name, verdict, can_grade, now,
        )
    except sqlite3.IntegrityError:
        # 外键/唯一约束失败（用户或题库记录刚被清理）→ 客户端可重试的 422，而不是 500。
        if image_name:
            await run_in_threadpool(storage.discard_file, "attempts", image_name)
        raise HTTPException(422, "作答数据与题库记录不一致，请刷新页面后重试") from None
    except Exception:
        if image_name:
            await run_in_threadpool(storage.discard_file, "attempts", image_name)
        raise
    return await run_in_threadpool(
        _attempt_submission_payload, user["id"], qid, question, verdict, attempt_id,
    )


def _attempt_image_reference(user_id: int, attempt_id: int):
    with connection() as con:
        row = con.execute("SELECT image_path FROM attempts WHERE id=? AND user_id=?", (attempt_id, user_id)).fetchone()
    if not row:
        raise HTTPException(404, "作答记录不存在")
    return row[0]


def get_attempt_image(user_id: int, attempt_id: int):
    reference = _attempt_image_reference(user_id, attempt_id)
    return storage.read_file("attempts", reference) if reference else None


def set_ai_grade(user_id: int, attempt_id: int, verdict: str, report: dict):
    if verdict not in ("correct", "incorrect", "pending"):
        raise ValueError("invalid grade verdict")
    with connection() as con:
        db.write_lock(con, f"progress:{user_id}")
        row = con.execute("SELECT * FROM attempts WHERE id=? AND user_id=?", (attempt_id, user_id)).fetchone()
        if not row:
            raise HTTPException(404, "作答记录不存在")
        if row["grade_source"] == "reference" and row["result"] in ("correct", "incorrect"):
            raise HTTPException(409, "该次作答已有原卷客观判题结果，不能由 AI 覆盖")
        latest = con.execute("SELECT MAX(id) FROM attempts WHERE user_id=? AND qid=?", (user_id, row["qid"])).fetchone()[0]
        if latest != attempt_id:
            raise HTTPException(409, "已有更新作答，本次 AI 结果不覆盖最新记录")
        if verdict == "correct" and not report.get("verified", report.get("agreement", False)):
            raise HTTPException(422, "没有独立核验一致证据，不能判为正确")
        verified = bool(report.get("verified", report.get("agreement", False)))
        source = "ai_cross_verified" if verified and verdict != "pending" else "ai_pending"
        con.execute(
            "UPDATE attempts SET result=?,grade_source=?,report_json=?,graded_at=? WHERE id=?",
            (verdict, source, json.dumps(report, ensure_ascii=False), utcnow(), attempt_id),
        )
        if row["result"] != verdict:
            _schedule_result(con, user_id, row["qid"], verdict)


@router.get("/attempt-image/{id}")
def attempt_image(id: int, user=Depends(current_user)):
    reference = _attempt_image_reference(user["id"], id)
    if not reference:
        raise HTTPException(404, "该次作答没有图片")
    return storage.file_response("attempts", reference, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@lru_cache(maxsize=8)
def _asset_manifest_index(bank_root: str, stamp: float, size: int):
    """素材清单索引：``(subject, images/xxx) → {asset_id, refs}``（快照包才有该文件）。"""
    index: dict[tuple[str, str], dict] = {}
    path = Path(bank_root) / "assets_manifest.jsonl"
    if not path.is_file():
        return index
    try:
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                refs = [ref for ref in (row.get("refs") or []) if isinstance(ref, dict)]
                entry = {
                    "asset_id": _text(row.get("asset_id")),
                    "sha256": _text(row.get("sha256")),
                    "refs": refs,
                }
                for relative in row.get("paths") or []:
                    parts = str(relative).split("/", 1)
                    if len(parts) != 2:
                        continue
                    index[(parts[0], parts[1])] = entry
    except OSError:
        return index
    return index


def _asset_manifest_entry(bank_root: Path, subject: str, file: str):
    path = bank_root / "assets_manifest.jsonl"
    if not path.is_file():
        return None
    stat = path.stat()
    return _asset_manifest_index(str(bank_root), stat.st_mtime, stat.st_size).get((subject, file))


@lru_cache(maxsize=8)
def _bank_version_cached(path: str, mtime_ns: int, size: int) -> str:
    probe = sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True, timeout=10)
    try:
        row = probe.execute("SELECT bank_version FROM questions LIMIT 1").fetchone()
        return str(row[0]) if row and row[0] else ""
    except sqlite3.Error:
        return ""
    finally:
        probe.close()


def _current_bank_version() -> str:
    """题库版本（快照包才带）。旧库返回空字符串，不做版本门禁。"""
    path = _bank_dir() / "题库.db"
    try:
        stat = path.stat()
    except OSError:
        return ""
    try:
        return _bank_version_cached(str(path.resolve()), stat.st_mtime_ns, stat.st_size)
    except sqlite3.Error:
        return ""


def _question_group_key(qid: str) -> str | None:
    with _bank() as con:
        row = con.execute(
            f"SELECT doc_id, group_id FROM {_bank_table(con, 'questions')} WHERE qid=?", (qid,)
        ).fetchone()
    return _group_key(dict(row)) if row else None


# 作答前可见的素材栏位：题干、选项、材料与共用题干。
_PUBLIC_ASSET_FIELDS = frozenset({"stem", "options", "material", "shared"})
_ANSWER_ASSET_FIELDS = frozenset({"answer", "solution"})


def _asset_ref_field(ref: dict) -> str:
    return _text(ref.get("field")) or ""


def _asset_ref_public(ref: dict) -> bool:
    """只有经校勘的公开内容引用可以提升可见级别；legacy 兜底不算。"""
    permission = _text(ref.get("permission")) or "public"
    return permission == "public" and _asset_ref_field(ref) in _PUBLIC_ASSET_FIELDS


def _asset_ref_gated(ref: dict) -> bool:
    permission = _text(ref.get("permission")) or "public"
    return permission != "public" or _asset_ref_field(ref) in _ANSWER_ASSET_FIELDS


def _require_attempt(user_id: int, qid: str) -> None:
    with connection() as con:
        unlocked = con.execute(
            "SELECT 1 FROM attempts WHERE user_id=? AND qid=? LIMIT 1", (user_id, qid)
        ).fetchone() is not None
    if not unlocked:
        raise HTTPException(403, "该素材在作答后可见")


def _asset_authorized(entry: dict, qid: str | None, user_id: int) -> None:
    """按题目归属和作答状态校验素材读权限；不满足时抛出 403/404。

    规则（下一轮优化方案 P0「题干图片」+ 核收记录阻断项 2）：
    * 本题/本题组若把该文件作为**公开内容**引用（stem/options/material/shared），作答前即可显示，
      否则题干图在零作答访问时会被自身答案引用连带拦截；
    * 只有答案/解析区引用、没有公开内容引用时必须先作答本题；
    * legacy 兜底引用不构成公开授权；
    * 未携带小题的旧请求只放行没有答案区引用的公开内容素材。
    """
    refs = entry.get("refs") or []
    if not refs:
        raise HTTPException(404, "题图不存在")
    if qid:
        scoped = [ref for ref in refs if _text(ref.get("qid")) == qid]
        group_key = _question_group_key(qid)
        group_refs = [
            ref for ref in refs
            if not _text(ref.get("qid")) and group_key and _text(ref.get("group_id")) == group_key
        ]
        related = scoped + group_refs
        if any(_asset_ref_public(ref) for ref in related):
            # 公开内容引用优先：这是题干/选项/材料图，必须能直接显示。
            return
        if any(_asset_ref_gated(ref) for ref in related):
            _require_attempt(user_id, qid)
            return
        if related:
            raise HTTPException(403, "该素材不属于当前小题")
        # 本题/本题组没有直接引用时一律拒绝：不能因为同一文件在别处有公开引用，
        # 就让任意 qid 零作答读取答案/解析图（P0 鉴权绕过）。
        raise HTTPException(403, "该素材不属于当前小题")
    if any(_asset_ref_public(ref) for ref in refs) and not any(_asset_ref_gated(ref) for ref in refs):
        return
    raise HTTPException(403, "该素材需在对应小题作答后查看")


@router.get("/bank-image")
def bank_image(
    subject: str,
    file: str,
    qid: str | None = Query(None, max_length=300),
    asset_id: str | None = Query(None, max_length=80),
    bank_version: str | None = Query(None, max_length=120),
    preview: bool = False,
    user=Depends(current_user),
):
    if bank_version:
        current = _current_bank_version()
        if current and bank_version != current:
            raise HTTPException(409, "题库已更新，请刷新页面后重试")
    with _bank() as con:
        questions = _bank_table(con, "questions")
        images = _bank_table(con, "images")
        if con.execute(f"SELECT 1 FROM {questions} WHERE subject=? LIMIT 1", (subject,)).fetchone() is None:
            raise HTTPException(404, "学科不存在")
    bank_root = _bank_dir()
    root = (bank_root / subject / "images").resolve()
    path = (bank_root / subject / file).resolve()
    if not path.is_relative_to(root) or not path.is_file() or path.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
        raise HTTPException(404, "题图不存在")
    if preview:
        # 管理员原卷预览使用单独授权入口，不受作答门禁影响。
        if user["role"] != "admin":
            raise HTTPException(403, "仅管理员可预览原卷素材")
        with _bank() as con:
            known = con.execute(
                f"SELECT 1 FROM {images} WHERE subject=? AND file=?", (subject, file)
            ).fetchone()
        if not known and not _asset_manifest_entry(bank_root, subject, file):
            raise HTTPException(404, "题图不存在")
        return FileResponse(path, headers={"Cache-Control": "private, max-age=3600", "Vary": "Cookie"})
    with _bank() as con:
        known = con.execute(
            f"SELECT 1 FROM {_bank_table(con, 'images')} WHERE subject=? AND file=?", (subject, file)
        ).fetchone()
    entry = _asset_manifest_entry(bank_root, subject, file)
    if entry is not None:
        # 快照素材即使同时出现在旧 images 表里，也按清单的题目/作答引用授权。
        if asset_id and entry.get("asset_id") and asset_id != entry["asset_id"]:
            raise HTTPException(403, "素材标识与题目记录不一致")
        _asset_authorized(entry, qid, user["id"])
    elif not known:
        group_images = {im.get("file") for value in _source_groups(subject).values() for im in value["material_images"]}
        if file not in group_images:
            raise HTTPException(404, "题图不存在")
    return FileResponse(path, headers={"Cache-Control": "private, max-age=3600", "Vary": "Cookie"})


class MasteryInput(BaseModel):
    qid: str = Field(max_length=160)
    level: str


@router.post("/mastery")
def mark_mastery(data: MasteryInput, user=Depends(current_user)):
    load_question(data.qid)
    if data.level not in LEVELS:
        raise HTTPException(422, "掌握程度无效")
    now = utcnow()
    with connection() as con:
        db.write_lock(con, f"progress:{user['id']}")
        attempt = con.execute(
            "SELECT id, result FROM attempts WHERE user_id=? AND qid=? ORDER BY id DESC LIMIT 1",
            (user["id"], data.qid),
        ).fetchone()
        # §4.2: 「未掌握」随时可选；「初步掌握」至少作答过一次；「比较/熟练掌握」需最新作答判对
        if data.level != "未掌握" and not attempt:
            raise HTTPException(403, "请先完成一次作答")
        if data.level in LEVELS[2:] and attempt["result"] != "correct":
            raise HTTPException(403, "最新一次作答判定正确后，才能标注比较掌握或熟练掌握")
        con.execute(
            "INSERT INTO mastery(user_id,qid,level,updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(user_id,qid) DO UPDATE SET level=excluded.level,updated_at=excluded.updated_at",
            (user["id"], data.qid, data.level, now),
        )
        # Link mastery to wrongbook review schedule
        wrong = con.execute(
            "SELECT * FROM wrong_questions WHERE user_id=? AND qid=? AND archived=0",
            (user["id"], data.qid),
        ).fetchone()
        if wrong and not (wrong["last_review_at"] and fsrs_enabled(con)):
            pushed_attempt = _row_value(wrong, "last_mastery_push_attempt")
            current_attempt = attempt["id"] if attempt else None
            interval = None
            due = None
            if data.level == "未掌握":
                interval = 1.0
                pushed_attempt = None
            elif current_attempt is not None and pushed_attempt == current_attempt:
                # 同一轮作答最多推升一次：连点「熟练掌握」不得把间隔累乘到异常值，
                # 也不重写 next_review_at（避免微秒级无意义变化）。
                interval = None
            else:
                interval = _clamp_interval(
                    _next_interval_for_mastery(data.level, wrong["interval_days"] or 1.0, wrong["streak"] or 0)
                )
                pushed_attempt = current_attempt
            if interval is not None:
                due = (datetime.now(timezone.utc) + timedelta(days=interval)).isoformat()
                archived = 1 if (data.level == "熟练掌握" and (wrong["streak"] or 0) >= 3) else 0
                con.execute(
                    "UPDATE wrong_questions SET interval_days=?, next_review_at=?, archived=?, "
                    "last_mastery_push_attempt=? WHERE user_id=? AND qid=?",
                    (interval, due, archived, pushed_attempt, user["id"], data.qid),
                )
    return {"qid": data.qid, "level": data.level}


class WrongInput(BaseModel):
    qid: str = Field(max_length=160)
    reason: str = Field(default="手动加入错题", max_length=300)
    error_tags: list[str] = Field(default_factory=list, max_length=10)


class ErrorTagsInput(BaseModel):
    qid: str = Field(max_length=160)
    error_tags: list[str] = Field(default_factory=list, max_length=10)
    reason: str | None = Field(default=None, max_length=300)


@router.post("/wrongbook")
def add_wrong(data: WrongInput, user=Depends(current_user)):
    question = load_question(data.qid)
    if question["quarantined"]:
        raise HTTPException(409, "该题已隔离待复核")
    if not question["practice_allowed"]:
        raise HTTPException(409, CONTEXT_SUBMIT_MESSAGE)
    tags = [t.strip()[:40] for t in data.error_tags if t.strip()]
    now = utcnow()
    with connection() as con:
        # 重新入本按新错题处理：重置连对计数与间隔，避免沿用归档前的长期排程。
        con.execute(
            "INSERT INTO wrong_questions(user_id,qid,reason,next_review_at,added_at,error_tags_json,archived,last_wrong_at,"
            "streak,interval_days,last_mastery_push_attempt) "
            "VALUES(?,?,?,?,?,?,0,?,0,1,NULL) "
            "ON CONFLICT(user_id,qid) DO UPDATE SET reason=excluded.reason, archived=0, "
            "streak=0, interval_days=1, next_review_at=excluded.next_review_at, "
            "last_mastery_push_attempt=NULL, "
            "error_tags_json=CASE WHEN excluded.error_tags_json!='[]' THEN excluded.error_tags_json ELSE wrong_questions.error_tags_json END",
            (user["id"], data.qid, data.reason, now, now, json.dumps(tags, ensure_ascii=False), now),
        )
    return {"qid": data.qid, "added": True}


@router.post("/wrongbook/tags")
def update_wrong_tags(data: ErrorTagsInput, user=Depends(current_user)):
    tags = list(dict.fromkeys(t.strip()[:40] for t in data.error_tags if t.strip()))
    with connection() as con:
        row = con.execute("SELECT 1 FROM wrong_questions WHERE user_id=? AND qid=?", (user["id"], data.qid)).fetchone()
        if not row:
            raise HTTPException(404, "该题不在错题本中")
        if data.reason is not None:
            con.execute(
                "UPDATE wrong_questions SET error_tags_json=?, reason=? WHERE user_id=? AND qid=?",
                (json.dumps(tags, ensure_ascii=False), data.reason, user["id"], data.qid),
            )
        else:
            con.execute(
                "UPDATE wrong_questions SET error_tags_json=? WHERE user_id=? AND qid=?",
                (json.dumps(tags, ensure_ascii=False), user["id"], data.qid),
            )
    return {"qid": data.qid, "error_tags": tags}


@router.delete("/wrongbook/uploads/{upload_id}")
def archive_upload_wrongbook(upload_id: int, user=Depends(current_user)):
    with connection() as con:
        cur = con.execute(
            "UPDATE upload_wrong_questions SET archived=1 WHERE user_id=? AND upload_id=?",
            (user["id"], upload_id),
        )
        if cur.rowcount == 0:
            raise HTTPException(404, "该拍题不在错题本中")
    return {"upload_id": upload_id, "archived": True}


@router.delete("/wrongbook/{qid:path}")
def archive_wrong(qid: str, permanent: bool = False, user=Depends(current_user)):
    with connection() as con:
        row = con.execute("SELECT 1 FROM wrong_questions WHERE user_id=? AND qid=?", (user["id"], qid)).fetchone()
        if not row:
            raise HTTPException(404, "该题不在错题本中")
        if permanent:
            con.execute("DELETE FROM wrong_questions WHERE user_id=? AND qid=?", (user["id"], qid))
        else:
            con.execute("UPDATE wrong_questions SET archived=1 WHERE user_id=? AND qid=?", (user["id"], qid))
    return {"qid": qid, "archived": True, "permanent": permanent}


@router.post("/wrongbook/restore")
def restore_wrong(data: WrongInput, user=Depends(current_user)):
    with connection() as con:
        # 恢复错题等于重新入本：重置连对计数与间隔，让调度从 1 天重新开始。
        con.execute(
            "UPDATE wrong_questions SET archived=0, streak=0, interval_days=1, "
            "last_mastery_push_attempt=NULL, next_review_at=? WHERE user_id=? AND qid=?",
            (utcnow(), user["id"], data.qid),
        )
    return {"qid": data.qid, "restored": True}


def _wrong_items(
    user_id,
    subject=None,
    due_only=False,
    type=None,
    province=None,
    year=None,
    difficulty=None,
    q=None,
    kp_id=None,
    error_tag=None,
    include_archived=False,
    page=1,
    page_size=200,
):
    with _app_with_bank() as con:
        join_sql, where_sql, params = _build_search_sql(
            con, user_id, subject=subject, type=type, province=province,
            year=year, difficulty=difficulty, kp_id=kp_id, q=q,
        )
        w_clauses = ["wq.user_id = ?"]
        w_params = [user_id]
        if not include_archived:
            w_clauses.append("wq.archived = 0")
        if due_only:
            w_clauses.append("wq.next_review_at <= ?")
            w_params.append(utcnow())
        if error_tag:
            # error_tags_json 是 JSON 数组文本：按 `"tag"` 精确匹配；必须转义 LIKE 通配符，
            # 否则用户传入 % 或 _ 会扩大匹配范围（错题本筛选结果失真）。
            w_clauses.append("wq.error_tags_json LIKE ? ESCAPE '\\'")
            w_params.append(_contains_pattern(f'"{error_tag}"'))
        full_where = f"{where_sql} AND " + " AND ".join(w_clauses)
        full_params = [*params, *w_params]
        bank_questions = _bank_table(con, "questions")
        total = con.execute(
            f"SELECT COUNT(*) FROM wrong_questions wq JOIN {bank_questions} q ON q.qid=wq.qid {join_sql} WHERE {full_where}",
            full_params,
        ).fetchone()[0]
        rows = con.execute(
            f"SELECT q.*, wq.reason, wq.failures, wq.streak, wq.next_review_at, wq.added_at, "
            f"wq.last_result, wq.interval_days, wq.review_count, wq.archived, wq.error_tags_json, wq.last_wrong_at "
            f"FROM wrong_questions wq JOIN {bank_questions} q ON q.qid=wq.qid {join_sql} "
            f"WHERE {full_where} ORDER BY wq.next_review_at ASC, wq.qid ASC LIMIT ? OFFSET ?",
            [*full_params, page_size, (page - 1) * page_size],
        ).fetchall()
        cards = _batch_summaries(con, rows, user_id)
        now_iso = utcnow()
        for card, raw in zip(cards, rows):
            card.update({
                "reason": raw["reason"],
                "failures": raw["failures"],
                "streak": raw["streak"],
                "next_review_at": raw["next_review_at"],
                "added_at": raw["added_at"],
                "last_result": raw["last_result"],
                "interval_days": raw["interval_days"],
                "review_count": raw["review_count"],
                "archived": bool(raw["archived"]),
                "error_tags": _json(raw["error_tags_json"], []),
                "days_since_wrong": _days_since(raw["last_wrong_at"] or raw["added_at"]),
                "due": raw["next_review_at"] <= now_iso,
            })
        return cards, total


@router.get("/wrongbook")
def wrongbook(
    subject: str | None = None,
    due_only: bool = False,
    kp_id: str | None = None,
    error_tag: str | None = None,
    include_archived: bool = False,
    page: int = Query(1, ge=1),
    page_size: int = Query(24, ge=1, le=200),
    user=Depends(current_user),
):
    items, total = _wrong_items(
        user["id"], subject=subject, due_only=due_only, kp_id=kp_id,
        error_tag=error_tag, include_archived=include_archived, page=page, page_size=page_size,
    )
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@router.get("/wrongbook/by-kp")
def wrongbook_by_kp(subject: str | None = None, user=Depends(current_user)):
    with connection() as con:
        sql = (
            "SELECT kp.id AS kp_id, kp.subject, kp.name, COUNT(DISTINCT wq.qid) AS wrong_count, "
            "SUM(CASE WHEN wq.next_review_at<=? THEN 1 ELSE 0 END) AS due_count "
            "FROM wrong_questions wq "
            "JOIN question_kp qk ON qk.qid=wq.qid "
            "JOIN knowledge_points kp ON kp.id=qk.kp_id "
            "WHERE wq.user_id=? AND wq.archived=0"
        )
        params = [utcnow(), user["id"]]
        if subject:
            sql += " AND kp.subject=?"
            params.append(subject)
        sql += " GROUP BY kp.id ORDER BY wrong_count DESC, kp.name ASC"
        rows = [dict(r) for r in con.execute(sql, params).fetchall()]
    return {"items": rows}


@router.get("/wrongbook/session")
def wrongbook_review_session(subject: str | None = None, limit: int = Query(20, ge=1, le=50), user=Depends(current_user)):
    """Return ordered due wrong questions for continuous review mode."""
    items, total = _wrong_items(user["id"], subject=subject, due_only=True, page=1, page_size=limit)
    return {"items": items, "total": total}


@router.get("/reviews")
def reviews(
    subject: str | None = None,
    type: str | None = None,
    province: str | None = None,
    year: int | None = None,
    difficulty: str | None = None,
    kp_id: str | None = None,
    q: str | None = Query(None, max_length=200),
    user=Depends(current_user),
):
    items, total = _wrong_items(
        user["id"], subject=subject, due_only=True, type=type, province=province,
        year=year, difficulty=difficulty, q=q, kp_id=kp_id, page=1, page_size=4,
    )
    return {"items": items, "total": total, "message": "到期错题推荐，不改变检索结果与总数"}


@router.get("/stats")
def stats(user=Depends(current_user)):
    with connection() as con:
        attempts = con.execute("SELECT COUNT(*) FROM attempts WHERE user_id=?", (user["id"],)).fetchone()[0]
        practiced = con.execute("SELECT COUNT(DISTINCT qid) FROM attempts WHERE user_id=?", (user["id"],)).fetchone()[0]
        correct = con.execute("SELECT COUNT(*) FROM attempts WHERE user_id=? AND result='correct'", (user["id"],)).fetchone()[0]
        graded = con.execute("SELECT COUNT(*) FROM attempts WHERE user_id=? AND result!='pending'", (user["id"],)).fetchone()[0]
        wrong_count = con.execute("SELECT COUNT(*) FROM wrong_questions WHERE user_id=? AND archived=0", (user["id"],)).fetchone()[0]
        due_count = con.execute(
            "SELECT COUNT(*) FROM wrong_questions WHERE user_id=? AND archived=0 AND next_review_at<=?",
            (user["id"], utcnow()),
        ).fetchone()[0]
        counts = {row[0]: row[1] for row in con.execute("SELECT level,COUNT(*) FROM mastery WHERE user_id=? GROUP BY level", (user["id"],))}
        recent = [dict(row) for row in con.execute("SELECT * FROM attempts WHERE user_id=? ORDER BY id DESC LIMIT 6", (user["id"],))]
        qids = [row[0] for row in con.execute("SELECT DISTINCT qid FROM attempts WHERE user_id=?", (user["id"],))]
    progress = {}
    with _bank() as con:
        questions = _bank_table(con, "questions")
        for row in con.execute(f"SELECT subject,COUNT(*) FROM {questions} GROUP BY subject"):
            progress[row[0]] = {"subject": row[0], "total": row[1], "practiced": 0}
        # 分批查询：已练题数可能很大，避免单条 IN (...) 超出 SQLITE_MAX_VARIABLE_NUMBER。
        practiced_by_subject: dict[str, int] = {}
        for chunk in _chunked(qids):
            marks = ",".join("?" for _ in chunk)
            for row in con.execute(
                f"SELECT subject,COUNT(*) FROM {questions} WHERE qid IN ({marks}) GROUP BY subject", chunk
            ):
                practiced_by_subject[row[0]] = practiced_by_subject.get(row[0], 0) + row[1]
        for subject, count in practiced_by_subject.items():
            bucket = progress.get(subject)
            if bucket is not None:
                bucket["practiced"] = count
        total_questions = con.execute(f"SELECT COUNT(*) FROM {questions}").fetchone()[0]
    return {
        "total_questions": total_questions, "attempts": attempts, "practiced": practiced, "correct": correct,
        "graded_attempts": graded, "accuracy": round(100 * correct / graded, 1) if graded else 0,
        "wrong_count": wrong_count, "due_count": due_count,
        "mastered": counts.get("熟练掌握", 0), "mastery_counts": {level: counts.get(level, 0) for level in LEVELS},
        "subject_progress": list(progress.values()), "recent_attempts": [_attempt_public(row) for row in recent],
    }


class MetadataInput(BaseModel):
    qid: str = Field(max_length=160)
    difficulty: str


@router.post("/question-metadata")
def set_question_metadata(data: MetadataInput, user=Depends(require_admin)):
    load_question(data.qid)
    if data.difficulty not in DIFFICULTIES:
        raise HTTPException(422, "难度值无效")
    with connection() as con:
        con.execute(
            "INSERT INTO question_metadata(qid,difficulty,updated_by,updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(qid) DO UPDATE SET difficulty=excluded.difficulty,updated_by=excluded.updated_by,updated_at=excluded.updated_at",
            (data.qid, data.difficulty, user["id"], utcnow()),
        )
    return {"qid": data.qid, "difficulty": data.difficulty, "source": "管理员标注"}


# ---------------------------------------------------------------------------
# P1: 题目报错与管理员校勘闭环
# 链路：待核对 → 待修订 / 确认无误 → 待发布 → 已发布；有证据时可先临时隔离。
# 题库保持只读，校勘记录保存在应用库，供题库重建消费。
# ---------------------------------------------------------------------------

ISSUE_FIELDS = ("stem", "options", "formula", "image", "answer", "solution", "other")
ISSUE_TYPES = ("formula", "stem_missing", "option", "image", "answer", "other")
ISSUE_STATUSES = ("pending_check", "to_revise", "confirmed", "to_publish", "published")
ISSUE_STATUS_LABELS = {
    "pending_check": "待核对",
    "to_revise": "待修订",
    "confirmed": "确认无误",
    "to_publish": "待发布",
    "published": "已发布",
}
ISSUE_FIELD_LABELS = {
    "stem": "题干", "options": "选项", "formula": "公式", "image": "图片",
    "answer": "参考答案", "solution": "解析", "other": "其他",
}
ISSUE_TYPE_LABELS = {
    "formula": "公式显示或内容有误", "stem_missing": "题干或条件缺失",
    "option": "选项边界或内容有误", "image": "图片缺失或错位",
    "answer": "参考答案有误", "other": "其他问题",
}
ISSUE_RISK = {
    "formula": "high", "stem_missing": "high", "answer": "high",
    "option": "medium", "image": "medium", "other": "low",
}
ISSUE_TRANSITIONS = {
    "pending_check": ("to_revise", "confirmed"),
    "to_revise": ("to_publish", "confirmed"),
    "confirmed": ("to_publish",),
    "to_publish": ("published", "to_revise"),
    "published": ("to_revise",),
}

_ISOLATION_CACHE = {"key": None, "at": 0.0, "items": {}}


def _active_isolations(force: bool = False) -> dict:
    """当前生效的临时隔离（每 5 秒刷新，避免每题一次查询）。"""
    now = time.monotonic()
    key = str(db.DATA_DIR)
    if (
        not force
        and _ISOLATION_CACHE["key"] == key
        and now - _ISOLATION_CACHE["at"] < 5
    ):
        return _ISOLATION_CACHE["items"]
    try:
        with connection() as con:
            rows = con.execute(
                "SELECT qid, group_id, issue_id, reason, created_at FROM content_isolations WHERE revoked_at IS NULL"
            ).fetchall()
        items = {row["qid"]: dict(row) for row in rows}
    except Exception:
        return _ISOLATION_CACHE["items"]
    _ISOLATION_CACHE.update(key=key, at=now, items=items)
    return items


def _invalidate_isolations():
    _ISOLATION_CACHE["key"] = None
    _ISOLATION_CACHE["at"] = 0.0
    return _active_isolations(force=True)


def _issue_snapshot(question: dict, field: str) -> dict:
    """记录报错时的原题显示，供管理员对比前后版本。"""
    group = question.get("group") or {}
    data = {
        "bank_version": question.get("bank_version"),
        "content_revision": question.get("content_revision"),
        "answer_revision": question.get("answer_revision"),
        "number_label": question.get("number_label"),
        "paper_name": question.get("paper_name"),
        "group_id": group.get("group_id"),
        "stem_excerpt": (question.get("stem_plain") or "")[:600],
    }
    if field == "options":
        data["options"] = question.get("options") or []
        data["option_labels"] = [str(o.get("label", "")) for o in question.get("options") or []]
    elif field in ("answer", "solution"):
        data["answer_text"] = (question.get("answer_text") or "")[:2000]
        data["solution_excerpt"] = (question.get("solution") or "")[:2000]
        data["answer_blocks"] = question.get("answer_blocks") or []
        data["solution_blocks"] = question.get("solution_blocks") or []
    elif field in ("formula", "image"):
        data["images"] = [
            {"file": item.get("file"), "role": item.get("role")} for item in question.get("images") or []
        ]
        data["block_ids"] = [
            block.get("block_id")
            for block in (question.get("stem_blocks") or []) + (question.get("options_blocks") or [])
            if isinstance(block, dict)
        ][:80]
    return data


def _issue_public(row) -> dict:
    data = dict(row)
    return {
        "id": data["id"], "qid": data["qid"], "group_id": data.get("group_id"),
        "field": data.get("field"), "field_label": ISSUE_FIELD_LABELS.get(data.get("field"), data.get("field")),
        "issue_type": data.get("issue_type"),
        "issue_type_label": ISSUE_TYPE_LABELS.get(data.get("issue_type"), data.get("issue_type")),
        "note": data.get("note") or "", "status": data.get("status"),
        "status_label": ISSUE_STATUS_LABELS.get(data.get("status"), data.get("status")),
        "risk": data.get("risk"), "decision": data.get("decision"),
        "evidence": data.get("evidence"), "created_at": data.get("created_at"),
        "updated_at": data.get("updated_at"), "resolved_at": data.get("resolved_at"),
        "screenshot_url": f"/api/issue-image/{data['id']}" if data.get("screenshot_path") else None,
    }


def _issue_admin_public(row, question: dict | None, isolated: bool) -> dict:
    data = dict(row)
    item = _issue_public(data)
    item.update({
        "subject": data.get("subject"),
        "bank_version": data.get("bank_version"),
        "reporter": data.get("reporter_name"),
        "reviewer": data.get("reviewer_name"),
        "before": _json(data.get("before_json"), {}),
        "after": _json(data.get("after_json"), None),
        "question": question,
        "isolated": isolated,
    })
    return item


def _issue_question_info(qids: list[str]) -> dict[str, dict]:
    info: dict[str, dict] = {}
    if not qids:
        return info
    with _bank() as con:
        columns = _bank_question_columns(con)
        questions = _bank_table(con, "questions")
        selected = ["qid", "subject", "year", "province", "paper_name", "number_label", "group_id", "doc_id"]
        for optional in ("bank_version", "practice_status", "content_revision", "answer_revision"):
            if optional in columns:
                selected.append(optional)
        select_sql = ", ".join(selected)
        for start in range(0, len(qids), 400):
            chunk = qids[start:start + 400]
            marks = ",".join("?" for _ in chunk)
            for row in con.execute(
                f"SELECT {select_sql} FROM {questions} WHERE qid IN ({marks})",
                chunk,
            ):
                record = dict(row)
                group_id = record.pop("group_id", None)
                doc_id = record.pop("doc_id", None)
                record["group_id"] = _group_key({"group_id": group_id, "doc_id": doc_id}) if group_id else None
                info[row["qid"]] = record
    return info


def _record_question_issue(qid, group, question, field, issue_type, note, image_name, risk, user_id, now):
    """同步写入报错记录；``async`` 端点经由 threadpool 调用。"""
    with connection() as con:
        return con.execute(
            "INSERT INTO content_issues(qid,group_id,bank_version,subject,field,issue_type,note,"
            "screenshot_path,status,risk,reporter_id,before_json,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                qid, group.get("group_id"), question.get("bank_version"), question.get("subject"),
                field, issue_type, note, image_name, "pending_check", risk, user_id,
                json.dumps(_issue_snapshot(question, field), ensure_ascii=False), now, now,
            ),
        ).lastrowid


@router.post("/questions/{qid:path}/issues")
async def report_question_issue(
    qid: str,
    field: str = Form("stem"),
    issue_type: str = Form("other"),
    note: str = Form(""),
    screenshot: UploadFile | None = File(None),
    user=Depends(current_user),
):
    question = await run_in_threadpool(load_question, qid)
    if field not in ISSUE_FIELDS:
        raise HTTPException(422, "报错字段无效")
    if issue_type not in ISSUE_TYPES:
        raise HTTPException(422, "报错类型无效")
    note = (note or "").strip()
    if len(note) > 2000:
        raise HTTPException(413, "补充说明过长")
    content = await screenshot.read(MAX_IMAGE_BYTES + 1) if screenshot else None
    if screenshot and not content:
        raise HTTPException(422, "截图不能为空")
    image_name = None
    if content:
        image_name = await run_in_threadpool(_save_upload_image, content, "issues")
    now = utcnow()
    risk = ISSUE_RISK.get(issue_type, "medium")
    group = question.get("group") or {}
    try:
        issue_id = await run_in_threadpool(
            _record_question_issue, qid, group, question, field, issue_type, note,
            image_name, risk, user["id"], now,
        )
    except sqlite3.IntegrityError:
        # 报错记录关联的用户/题目状态在提交瞬间被清理时返回可重试的 422，而不是 500。
        if image_name:
            await run_in_threadpool(storage.discard_file, "issues", image_name)
        raise HTTPException(422, "报错数据与题库记录不一致，请刷新页面后重试") from None
    except Exception:
        if image_name:
            await run_in_threadpool(storage.discard_file, "issues", image_name)
        raise
    return {
        "id": issue_id, "status": "pending_check",
        "status_label": ISSUE_STATUS_LABELS["pending_check"], "risk": risk,
        "message": "报错已提交，管理员核对后会更新处理状态。",
    }


@router.get("/issue-image/{issue_id}")
def issue_screenshot(issue_id: int, user=Depends(current_user)):
    with connection() as con:
        row = con.execute(
            "SELECT reporter_id, screenshot_path FROM content_issues WHERE id=?", (issue_id,)
        ).fetchone()
    if not row or not row["screenshot_path"]:
        raise HTTPException(404, "该报错没有截图")
    if user["role"] != "admin" and row["reporter_id"] != user["id"]:
        raise HTTPException(403, "没有查看该截图的权限")
    return storage.file_response("issues", row["screenshot_path"], media_type="image/jpeg",
                                 headers={"Cache-Control": "no-store"})


@router.get("/admin/content-issues")
def list_content_issues(
    status: str | None = None,
    subject: str | None = None,
    issue_type: str | None = None,
    risk: str | None = None,
    bank_version: str | None = None,
    qid: str | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=50),
    user=Depends(require_admin),
):
    clauses, params = [], []
    if status:
        if status not in ISSUE_STATUSES:
            raise HTTPException(422, "审核状态筛选值无效")
        clauses.append("i.status=?")
        params.append(status)
    if subject:
        clauses.append("i.subject=?")
        params.append(subject)
    if issue_type:
        if issue_type not in ISSUE_TYPES:
            raise HTTPException(422, "报错类型筛选值无效")
        clauses.append("i.issue_type=?")
        params.append(issue_type)
    if risk:
        if risk not in ("high", "medium", "low"):
            raise HTTPException(422, "风险筛选值无效")
        clauses.append("i.risk=?")
        params.append(risk)
    if bank_version:
        clauses.append("i.bank_version=?")
        params.append(bank_version)
    if qid:
        clauses.append("i.qid=?")
        params.append(qid)
    where = " AND ".join(clauses) if clauses else "1=1"
    base = (
        "FROM content_issues i LEFT JOIN users ru ON ru.id=i.reporter_id "
        "LEFT JOIN users vu ON vu.id=i.reviewer_id WHERE " + where
    )
    with connection() as con:
        total = con.execute("SELECT COUNT(*) " + base, params).fetchone()[0]
        rows = con.execute(
            "SELECT i.*, ru.username AS reporter_name, vu.username AS reviewer_name " + base +
            " ORDER BY CASE i.status WHEN 'pending_check' THEN 0 WHEN 'to_revise' THEN 1"
            " WHEN 'to_publish' THEN 2 WHEN 'confirmed' THEN 3 ELSE 4 END, i.id DESC LIMIT ? OFFSET ?",
            [*params, limit + 1, offset],
        ).fetchall()
    page = rows[:limit]
    info = _issue_question_info(sorted({row["qid"] for row in page}))
    isolated = _active_isolations()
    revision_report = _bank_revision_report()
    items = []
    for row in page:
        question = info.get(row["qid"]) or {}
        state = _revision_state(dict(row), revision_report)
        # 修订号一致性：已生效/已发布的修订，其产出修订号应与题库当前值一致。
        produced = state.get("after_revision")
        current = question.get("content_revision")
        state["current_revision"] = current
        state["revision_matches"] = (produced == current) if produced and current else None
        items.append(dict(_issue_admin_public(
            row, question, row["qid"] in isolated), revision=state))
    return {
        "items": items,
        "total": total,
        "has_more": len(rows) > limit,
        "statuses": [
            {"value": value, "label": ISSUE_STATUS_LABELS[value]} for value in ISSUE_STATUSES
        ],
        "types": [
            {"value": value, "label": ISSUE_TYPE_LABELS[value]} for value in ISSUE_TYPES
        ],
        "revisions": {
            "present": revision_report.get("present"),
            "bank_version": revision_report.get("bank_version"),
            "counts": revision_report.get("counts"),
        },
    }


@lru_cache(maxsize=8)
def _revision_report(bank_root: str, stamp: float, size: int) -> dict:
    """读取发布包里的校勘应用报告（题库重建产物），用于展示「生效于版本」。"""
    data = {"present": False, "bank_version": None, "counts": {}, "applied": {}}
    path = Path(bank_root) / "revision_report.json"
    try:
        if not path.is_file():
            return data
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return data
    applied_map: dict[str, list[dict]] = {}
    for row in payload.get("applied") or []:
        qid = _text(row.get("qid"))
        if not qid:
            continue
        applied_map.setdefault(qid, []).append({
            "issue_id": row.get("issue_id"),
            "after_revision": row.get("after_revision"),
            "applied_fields": row.get("applied_fields") or [],
        })
    return {
        "present": True,
        "bank_version": _text(payload.get("bank_version")),
        "counts": payload.get("counts") or {},
        "applied": applied_map,
    }


def _bank_revision_report() -> dict:
    root = _bank_dir()
    path = root / "revision_report.json"
    try:
        stat = path.stat()
    except OSError:
        return {"present": False, "bank_version": _current_bank_version() or None,
                "counts": {}, "applied": {}}
    report = _revision_report(str(root.resolve()), stat.st_mtime, stat.st_size)
    if report.get("present") and not report.get("bank_version"):
        report["bank_version"] = _current_bank_version() or None
    return report


def _revision_state(issue_row, report: dict) -> dict:
    """三态：已审核（导出记录本身）→ 待进入构建 → 生效于版本。"""
    qid = _text(issue_row.get("qid"))
    entries = (report.get("applied") or {}).get(qid) or []
    issue_id = issue_row.get("id")
    for entry in entries:
        if entry.get("issue_id") == issue_id:
            return {
                "state": "applied",
                "label": "已生效于 %s" % (report.get("bank_version") or "发布版本"),
                "applied_in": report.get("bank_version"),
                "after_revision": entry.get("after_revision"),
                "applied_fields": entry.get("applied_fields") or [],
            }
    if issue_row.get("status") == "published":
        return {"state": "pending_build", "label": "已审核，待进入下一题库版本",
                "applied_in": None, "after_revision": None, "applied_fields": []}
    return {"state": "review", "label": "审核中", "applied_in": None,
            "after_revision": None, "applied_fields": []}


@router.get("/admin/revisions")
def admin_revisions(user=Depends(require_admin)):
    """当前题库版本的校勘应用报告：管理员核对「修订何时生效」。"""
    report = _bank_revision_report()
    return {
        "present": report.get("present"),
        "bank_version": report.get("bank_version"),
        "counts": report.get("counts"),
        "applied_count": len(report.get("applied") or {}),
        "notice": (None if report.get("present")
                   else "当前题库版本没有校勘应用报告（尚未有已审核修订进入构建）。"),
    }


@router.get("/admin/content-issues/export")
def export_content_issues(status: str = Query("published"), user=Depends(require_admin)):
    """导出已发布校勘，供题库重建按版本消费。"""
    if status not in ISSUE_STATUSES:
        raise HTTPException(422, "审核状态筛选值无效")
    with connection() as con:
        rows = con.execute(
            "SELECT i.*, vu.username AS reviewer_name FROM content_issues i "
            "LEFT JOIN users vu ON vu.id=i.reviewer_id WHERE i.status=? ORDER BY i.id",
            (status,),
        ).fetchall()
    lines = []
    for row in rows:
        lines.append(json.dumps({
            "issue_id": row["id"], "qid": row["qid"], "group_id": row["group_id"],
            "subject": row["subject"], "bank_version": row["bank_version"],
            "field": row["field"], "issue_type": row["issue_type"],
            "before": _json(row["before_json"], {}), "after": _json(row["after_json"], None),
            "decision": row["decision"], "evidence": row["evidence"],
            "reviewer": row["reviewer_name"], "resolved_at": row["resolved_at"],
            "content_revision": (_json(row["before_json"], {}) or {}).get("content_revision"),
        }, ensure_ascii=False))
    body = "\n".join(lines) + ("\n" if lines else "")
    return Response(
        body, media_type="application/x-ndjson; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="content-issues-{status}.jsonl"'},
    )


class IssueTransition(BaseModel):
    status: str
    decision: str = Field(default="", max_length=1000)
    evidence: str = Field(default="", max_length=2000)
    after: dict | None = None


@router.post("/admin/content-issues/{issue_id}/transition")
def transition_content_issue(issue_id: int, data: IssueTransition, user=Depends(require_admin)):
    if data.status not in ISSUE_STATUSES:
        raise HTTPException(422, "审核状态无效")
    with connection() as con:
        db.write_lock(con, "content-issue")
        row = con.execute("SELECT * FROM content_issues WHERE id=?", (issue_id,)).fetchone()
        if not row:
            raise HTTPException(404, "报错记录不存在")
        current = row["status"]
        if data.status != current and data.status not in ISSUE_TRANSITIONS.get(current, ()):
            raise HTTPException(
                409,
                f"审核状态不能从「{ISSUE_STATUS_LABELS.get(current, current)}」"
                f"直接变为「{ISSUE_STATUS_LABELS[data.status]}」",
            )
        decision = (data.decision or "").strip() or (row["decision"] or "")
        evidence = (data.evidence or "").strip() or (row["evidence"] or "")
        if data.status in ("to_publish", "published") and data.status != current:
            if not decision:
                raise HTTPException(422, "进入待发布或发布前必须填写修改说明或决定理由")
        # 「待发布 → 已发布」允许复用 to_publish 阶段已提交的 after_json：
        # 同一份拟修改内容不必重复提交，避免前端漏传导致发布被 422 卡死。
        existing_after = _json(row["after_json"], None)
        effective_after = data.after if data.after is not None else existing_after
        if data.status == "published":
            if not evidence:
                raise HTTPException(422, "发布前必须保留原卷依据")
            if effective_after is None:
                raise HTTPException(422, "发布前必须提交拟修改后的内容，供题库重建消费")
        now = utcnow()
        resolved = now if data.status in ("published", "confirmed") else None
        con.execute(
            "UPDATE content_issues SET status=?, decision=?, evidence=?, "
            "after_json=COALESCE(?, after_json), reviewer_id=?, updated_at=?, "
            "resolved_at=COALESCE(?, resolved_at) WHERE id=?",
            (
                data.status, decision, evidence,
                json.dumps(data.after, ensure_ascii=False) if data.after is not None else None,
                user["id"], now, resolved, issue_id,
            ),
        )
        con.execute(
            "INSERT INTO ai_audit(user_id,action,resource_id,created_at) VALUES (?,?,?,?)",
            (user["id"], f"content_issue:{data.status}", issue_id, now),
        )
        updated = con.execute(
            "SELECT i.*, ru.username AS reporter_name, vu.username AS reviewer_name FROM content_issues i "
            "LEFT JOIN users ru ON ru.id=i.reporter_id LEFT JOIN users vu ON vu.id=i.reviewer_id WHERE i.id=?",
            (issue_id,),
        ).fetchone()
    info = _issue_question_info([updated["qid"]]).get(updated["qid"])
    return _issue_admin_public(updated, info, updated["qid"] in _active_isolations())


class IsolationInput(BaseModel):
    active: bool = True
    reason: str = Field(default="", max_length=1000)
    issue_id: int | None = None


@router.post("/admin/questions/{qid:path}/isolation")
def set_question_isolation(qid: str, data: IsolationInput, user=Depends(require_admin)):
    """按证据临时隔离：题库只读，隔离在应用层生效并保留撤销记录。"""
    question = load_question(qid)
    group = question.get("group") or {}
    now = utcnow()
    reason = (data.reason or "").strip()
    if data.active and not reason:
        raise HTTPException(422, "隔离必须写明原卷证据或问题原因")
    with connection() as con:
        db.write_lock(con, "content-isolation")
        if data.active:
            con.execute(
                "INSERT INTO content_isolations(qid,group_id,issue_id,reason,created_by,created_at,revoked_at) "
                "VALUES (?,?,?,?,?,?,NULL) "
                "ON CONFLICT(qid) DO UPDATE SET group_id=excluded.group_id, issue_id=excluded.issue_id, "
                "reason=excluded.reason, created_by=excluded.created_by, created_at=excluded.created_at, revoked_at=NULL",
                (qid, group.get("group_id"), data.issue_id, reason, user["id"], now),
            )
        else:
            con.execute(
                "UPDATE content_isolations SET revoked_at=? WHERE qid=? AND revoked_at IS NULL",
                (now, qid),
            )
        con.execute(
            "INSERT INTO ai_audit(user_id,action,resource_id,created_at) VALUES (?,?,?,?)",
            (user["id"], f"content_isolation:{'on' if data.active else 'off'}", data.issue_id or 0, now),
        )
    _invalidate_isolations()
    return {
        "qid": qid, "isolated": data.active,
        "reason": reason if data.active else None,
        "message": "已临时隔离该题，作答与判分入口已暂停。" if data.active else "已撤销隔离。",
    }


# ---------------------------------------------------------------------------
# Knowledge Points Tree, Tagging & Review Queue (§2.3)
# ---------------------------------------------------------------------------

@router.get("/knowledge-points")
def list_knowledge_points(subject: str | None = None, user=Depends(current_user)):
    with connection() as con:
        sql = "SELECT * FROM knowledge_points"
        params = []
        if subject:
            sql += " WHERE subject=?"
            params.append(subject)
        sql += " ORDER BY subject, level, sort, id"
        rows = [dict(r) for r in con.execute(sql, params).fetchall()]
        q_counts = {
            r["kp_id"]: r["cnt"]
            for r in con.execute("SELECT kp_id, COUNT(*) AS cnt FROM question_kp GROUP BY kp_id")
        }
        w_counts = {
            r["kp_id"]: r["cnt"]
            for r in con.execute(
                "SELECT qk.kp_id, COUNT(DISTINCT wq.qid) AS cnt "
                "FROM wrong_questions wq JOIN question_kp qk ON qk.qid=wq.qid "
                "WHERE wq.user_id=? AND wq.archived=0 GROUP BY qk.kp_id",
                (user["id"],),
            )
        }
    nodes = []
    for r in rows:
        nodes.append({
            "id": r["id"], "subject": r["subject"], "parent_id": r["parent_id"],
            "name": r["name"], "level": r["level"], "aliases": _json(r["aliases_json"], []),
            "description": r["description"], "sort": r["sort"], "source": r["source"],
            "question_count": q_counts.get(r["id"], 0),
            "wrong_count": w_counts.get(r["id"], 0),
        })
    return {"items": nodes}


@router.get("/knowledge-points/{kp_id}/detail")
def knowledge_point_detail(kp_id: int, user=Depends(current_user)):
    with connection() as con:
        kp = con.execute("SELECT * FROM knowledge_points WHERE id=?", (kp_id,)).fetchone()
        if not kp:
            raise HTTPException(404, "知识点不存在")
        all_ids = _expand_kp_ids(con, [kp_id])
        marks = ",".join("?" for _ in all_ids)
        official_notes = [
            {"id": r["id"], "title": r["title"], "content": r["content"], "updated_at": r["updated_at"]}
            for r in con.execute(
                f"SELECT id, title, content, updated_at FROM ai_notes "
                f"WHERE origin='official' AND published=1 AND kp_id IN ({marks}) ORDER BY updated_at DESC",
                all_ids,
            ).fetchall()
        ]
        wrong_count = con.execute(
            f"SELECT COUNT(DISTINCT wq.qid) FROM wrong_questions wq "
            f"JOIN question_kp qk ON qk.qid=wq.qid WHERE wq.user_id=? AND wq.archived=0 AND qk.kp_id IN ({marks})",
            [user["id"], *all_ids],
        ).fetchone()[0]
        question_count = con.execute(
            f"SELECT COUNT(DISTINCT qid) FROM question_kp WHERE kp_id IN ({marks})", all_ids
        ).fetchone()[0]
    return {
        "kp": {
            "id": kp["id"], "subject": kp["subject"], "parent_id": kp["parent_id"],
            "name": kp["name"], "level": kp["level"], "description": kp["description"],
        },
        "official_notes": official_notes,
        "question_count": question_count,
        "wrong_count": wrong_count,
    }


class KPCreate(BaseModel):
    subject: str = Field(min_length=1, max_length=40)
    parent_id: int | None = None
    name: str = Field(min_length=1, max_length=120)
    level: int = Field(default=3, ge=1, le=3)
    aliases: list[str] = Field(default_factory=list)
    description: str = Field(default="", max_length=600)
    sort: int = 0


@router.post("/admin/knowledge-points")
def create_knowledge_point(data: KPCreate, user=Depends(require_admin)):
    with connection() as con:
        kp_id = con.execute(
            "INSERT INTO knowledge_points(subject,parent_id,name,level,aliases_json,description,sort,source) "
            "VALUES (?,?,?,?,?,?,?,'official')",
            (data.subject.strip(), data.parent_id, data.name.strip(), data.level,
             json.dumps(data.aliases, ensure_ascii=False), data.description.strip(), data.sort),
        ).lastrowid
        row = dict(con.execute("SELECT * FROM knowledge_points WHERE id=?", (kp_id,)).fetchone())
    return row


@router.delete("/admin/knowledge-points/{kp_id}")
def delete_knowledge_point(kp_id: int, user=Depends(require_admin)):
    with connection() as con:
        cur = con.execute("DELETE FROM knowledge_points WHERE id=?", (kp_id,))
        if cur.rowcount == 0:
            raise HTTPException(404, "知识点不存在")
    return {"deleted": True}


def _import_knowledge_points_sync(text: str, filename: str) -> int:
    """同步导入知识点树（threadpool 执行）；结构/编码问题统一转 422，不抛 500。"""
    added = 0
    with connection() as con:
        def get_or_create(subject, parent_id, kp_name, level):
            nonlocal added
            if parent_id is None:
                row = con.execute(
                    "SELECT id FROM knowledge_points WHERE subject=? AND parent_id IS NULL AND name=?",
                    (subject, kp_name),
                ).fetchone()
            else:
                row = con.execute(
                    "SELECT id FROM knowledge_points WHERE subject=? AND parent_id=? AND name=?",
                    (subject, parent_id, kp_name),
                ).fetchone()
            if row:
                return row[0]
            added += 1
            return con.execute(
                "INSERT INTO knowledge_points(subject,parent_id,name,level,source) VALUES (?,?,?,?,'official')",
                (subject, parent_id, kp_name, level),
            ).lastrowid

        if filename.endswith(".csv"):
            reader = csv.DictReader(StringIO(text))
            try:
                for r in reader:
                    subj = (r.get("subject") or r.get("学科") or "").strip()
                    ch = (r.get("chapter") or r.get("章") or "").strip()
                    sec = (r.get("section") or r.get("节") or "").strip()
                    pt = (r.get("point") or r.get("知识点") or "").strip()
                    if not subj or not ch:
                        continue
                    cid = get_or_create(subj, None, ch, 1)
                    if sec:
                        sid = get_or_create(subj, cid, sec, 2)
                        if pt:
                            get_or_create(subj, sid, pt, 3)
            except csv.Error:
                raise HTTPException(422, "CSV 格式无效，请检查表头与分隔符") from None
            return added

        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            raise HTTPException(422, "JSON 格式无效") from None
        if not isinstance(payload, dict):
            raise HTTPException(422, "JSON 顶层应为 {学科: [章节列表]}")
        try:
            for subj, chapters in payload.items():
                if not isinstance(chapters, list):
                    raise TypeError("chapters")
                for ch in chapters:
                    if not isinstance(ch, dict) or not ch.get("name"):
                        raise KeyError("name")
                    cid = get_or_create(str(subj), None, str(ch["name"]), 1)
                    children = ch.get("children") or []
                    if not isinstance(children, list):
                        raise TypeError("children")
                    for sec in children:
                        if not isinstance(sec, dict) or not sec.get("name"):
                            raise KeyError("name")
                        sid = get_or_create(str(subj), cid, str(sec["name"]), 2)
                        points = sec.get("children") or []
                        if not isinstance(points, list):
                            raise TypeError("children")
                        for pt in points:
                            pt_name = pt["name"] if isinstance(pt, dict) else str(pt)
                            if pt_name:
                                get_or_create(str(subj), sid, str(pt_name), 3)
        except (KeyError, TypeError, AttributeError):
            raise HTTPException(422, "JSON 结构无效：应为 {学科: [{name, children: [...]}]}") from None
    return added


@router.post("/admin/knowledge-points/import")
async def import_knowledge_points(file: UploadFile = File(...), user=Depends(require_admin)):
    """Import KP tree from JSON ({subject: [{name, children: ...}]}) or CSV (subject,chapter,section,point)."""
    raw = await file.read(5 * 1024 * 1024)
    if not raw:
        raise HTTPException(422, "导入文件为空")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(422, "导入文件必须为 UTF-8 编码（CSV 请另存为 UTF-8）") from None
    added = await run_in_threadpool(_import_knowledge_points_sync, text, (file.filename or "").lower())
    return {"imported": added}


class QuestionKPInput(BaseModel):
    qid: str = Field(max_length=160)
    kp_ids: list[int] = Field(max_length=10)


@router.post("/admin/question-kp")
def set_question_kps(data: QuestionKPInput, user=Depends(require_admin)):
    load_question(data.qid)
    now = utcnow()
    try:
        with connection() as con:
            con.execute("DELETE FROM question_kp WHERE qid=?", (data.qid,))
            for kp_id in dict.fromkeys(data.kp_ids):
                con.execute(
                    "INSERT INTO question_kp(qid,kp_id,weight,source,confidence,reviewed_by,created_at) "
                    "VALUES (?,?,1.0,'admin',1.0,?,?)",
                    (data.qid, kp_id, user["id"], now),
                )
    except sqlite3.IntegrityError:
        # 知识点在提交瞬间被删除等外键冲突 → 422（客户端可重试），而不是 500。
        raise HTTPException(422, "知识点 ID 无效或已被删除，请刷新后重试") from None
    return {"qid": data.qid, "kp_ids": data.kp_ids}


@router.get("/admin/kp-review-queue")
def kp_review_queue(subject: str | None = None, limit: int = Query(50, ge=1, le=200), user=Depends(require_admin)):
    with _app_with_bank() as con:
        sql = (
            "SELECT qk.qid, qk.kp_id, kp.name AS kp_name, kp.subject, qk.source, qk.model, qk.confidence, "
            "q.stem_plain, q.year, q.province, q.type "
            "FROM question_kp qk "
            "JOIN knowledge_points kp ON kp.id=qk.kp_id "
            f"JOIN {_bank_table(con, 'questions')} q ON q.qid=qk.qid "
            "WHERE qk.source IN ('ai','ai_pending_review')"
        )
        params = []
        if subject:
            sql += " AND kp.subject=?"
            params.append(subject)
        sql += " ORDER BY COALESCE(qk.confidence, 0) ASC, qk.created_at DESC LIMIT ?"
        params.append(limit)
        rows = [dict(r) for r in con.execute(sql, params).fetchall()]
    for r in rows:
        r["stem_plain"] = (r["stem_plain"] or "")[:180]
        r["type_label"] = TYPE_LABELS.get(r["type"], r["type"])
    return {"items": rows, "total": len(rows)}


class KPReviewAction(BaseModel):
    qid: str = Field(max_length=160)
    kp_id: int
    action: str = Field(pattern="^(approve|reject)$")


@router.post("/admin/question-kp/review")
def review_question_kp(data: KPReviewAction, user=Depends(require_admin)):
    with connection() as con:
        if data.action == "approve":
            con.execute(
                "UPDATE question_kp SET source='ai_reviewed', reviewed_by=? WHERE qid=? AND kp_id=?",
                (user["id"], data.qid, data.kp_id),
            )
        else:
            con.execute("DELETE FROM question_kp WHERE qid=? AND kp_id=?", (data.qid, data.kp_id))
    return {"ok": True, "qid": data.qid, "kp_id": data.kp_id, "action": data.action}



# ---------------------------------------------------------------------------
# Methods (题型/解法标签 §2.3) & Uploaded Photo Wrongbook (§8)
# ---------------------------------------------------------------------------

@router.get("/methods")
def list_methods(subject: str | None = None, user=Depends(current_user)):
    with connection() as con:
        sql = (
            "SELECT m.id, m.subject, m.name, COUNT(qm.qid) AS question_count "
            "FROM methods m LEFT JOIN question_method qm ON qm.method_id=m.id"
        )
        params = []
        if subject:
            sql += " WHERE m.subject=?"
            params.append(subject)
        sql += " GROUP BY m.id ORDER BY question_count DESC, m.name ASC"
        rows = [dict(r) for r in con.execute(sql, params).fetchall()]
    return {"items": rows}


class QuestionMethodInput(BaseModel):
    qid: str = Field(max_length=160)
    method_name: str = Field(min_length=1, max_length=80)


@router.post("/admin/question-method")
def add_question_method(data: QuestionMethodInput, user=Depends(require_admin)):
    q = load_question(data.qid)
    name = data.method_name.strip()
    with connection() as con:
        con.execute(
            "INSERT INTO methods(subject,name) VALUES (?,?) ON CONFLICT(subject,name) DO NOTHING",
            (q["subject"], name),
        )
        mid = con.execute("SELECT id FROM methods WHERE subject=? AND name=?", (q["subject"], name)).fetchone()[0]
        con.execute(
            "INSERT INTO question_method(qid,method_id,source) VALUES (?,?,'admin') "
            "ON CONFLICT(qid,method_id) DO UPDATE SET source=excluded.source",
            (data.qid, mid),
        )
    return {"qid": data.qid, "method_id": mid, "name": name, "subject": q["subject"]}


class UploadWrongInput(BaseModel):
    upload_id: int
    subject: str = Field(default="", max_length=40)
    stem_preview: str = Field(default="", max_length=600)
    reason: str = Field(default="拍题加入错题本", max_length=300)
    error_tags: list[str] = Field(default_factory=list, max_length=10)


@router.post("/wrongbook/upload")
def add_upload_to_wrongbook(data: UploadWrongInput, user=Depends(current_user)):
    with connection() as con:
        up = con.execute("SELECT * FROM ai_uploads WHERE id=? AND user_id=?", (data.upload_id, user["id"])).fetchone()
        if not up:
            raise HTTPException(404, "未找到该上传题目")
        confirmed = _json(up["confirmed_json"], None) if "confirmed_json" in up.keys() else None
        recognized = _json(up["recognized_json"], None) if "recognized_json" in up.keys() else None
        subj = data.subject.strip() or (confirmed or {}).get("subject") or (recognized or {}).get("subject") or "拍题"
        stem = data.stem_preview.strip() or (confirmed or {}).get("stem") or (recognized or {}).get("stem") or up["text_content"] or up["filename"]
        tags = list(dict.fromkeys(t.strip()[:40] for t in data.error_tags if t.strip()))
        now = utcnow()
        due = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        con.execute(
            "INSERT INTO upload_wrong_questions(user_id,upload_id,subject,stem_preview,reason,error_tags_json,next_review_at,added_at,archived) "
            "VALUES (?,?,?,?,?,?,?,?,0) "
            "ON CONFLICT(user_id,upload_id) DO UPDATE SET subject=excluded.subject, stem_preview=excluded.stem_preview, "
            "reason=excluded.reason, error_tags_json=excluded.error_tags_json, archived=0",
            (user["id"], data.upload_id, subj[:40], stem[:400], data.reason, json.dumps(tags, ensure_ascii=False), due, now),
        )
    return {"upload_id": data.upload_id, "added": True, "subject": subj}


@router.get("/wrongbook/uploads")
def list_upload_wrongbook(subject: str | None = None, include_archived: bool = False, user=Depends(current_user)):
    with connection() as con:
        sql = (
            "SELECT uw.*, u.filename, u.media_type "
            "FROM upload_wrong_questions uw JOIN ai_uploads u ON u.id=uw.upload_id "
            "WHERE uw.user_id=?"
        )
        params = [user["id"]]
        if not include_archived:
            sql += " AND uw.archived=0"
        if subject:
            sql += " AND uw.subject=?"
            params.append(subject)
        sql += " ORDER BY uw.added_at DESC LIMIT 100"
        rows = [dict(r) for r in con.execute(sql, params).fetchall()]
    for r in rows:
        r["error_tags"] = _json(r.pop("error_tags_json", "[]"), [])
        r["archived"] = bool(r["archived"])
        r["preview_url"] = f"/api/uploads/{r['upload_id']}" if str(r.get("media_type", "")).startswith("image/") else None
    return {"items": rows, "total": len(rows)}
