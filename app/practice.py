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
from . import db, question_ref, storage
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
# 个人题（app:<pl_questions.id>）在 practice 侧的题型映射：工坊题型 → 题库练习题型。
APP_PRACTICE_TYPE_MAP = {
    "SINGLE": "single_choice",
    "MULTIPLE": "multi_choice",
    "BLANK": "fill_blank",
    "SUBJECTIVE": "short_answer",
    "MIXED": "material_group",
}
# 幂等台账（一次作答只入账一次 / 整卷逐题只入账一次）：
# 主键 (user_id, kind, ledger_key) 是数据库级唯一约束，并发重复提交由数据库兜底。
LEDGER_KIND_ATTEMPT = "attempt"
LEDGER_KIND_PAPER_ITEM = "paper_item"
PENDING_GRADE_MESSAGE = "参考答案待核验，暂不自动判分"
QUARANTINE_DETAIL_MESSAGE = "该题材料缺失或混入答案，已进入数据复核队列"
QUARANTINE_SUBMIT_MESSAGE = "该题材料缺失或混入答案，暂不可作答"
CONTEXT_SUBMIT_MESSAGE = "该题仅为上下文材料，不可作答"
# 原卷公开字段里出现以下标记，说明题干/材料混入了答案或解析；先停止作答，待数据修正。
ANSWER_LEAK_MARKERS = ("【答案】", "【详解】", "【解析】", "【解答】", "参考答案：")
_log = logging.getLogger("zhixu.practice")
# RLock（可重入）：PG 镜像刷新路径 ensure_leak_flags → connection(attach_bank=True)
# → _ensure_postgres_bank → _install_bank_leak_flags 会在持锁状态下再次进入本锁；
# 普通 Lock 在 PG 首次启动时自死锁（部署联验 2026-11-25 实测发现）。跨线程互斥语义不变。
_fts_lock = threading.RLock()
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
            -- 幂等台账：单题作答（attempt）与整卷逐题入账（paper_item）共用；
            -- (user_id, kind, ledger_key) 主键即数据库唯一约束，重复/并发提交只入账一次。
            CREATE TABLE IF NOT EXISTS submission_ledger (
                user_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                ledger_key TEXT NOT NULL,
                qid TEXT NOT NULL,
                attempt_id INTEGER,
                created_at TEXT NOT NULL,
                PRIMARY KEY (user_id, kind, ledger_key)
            );
            CREATE INDEX IF NOT EXISTS submission_ledger_attempt ON submission_ledger(attempt_id);
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


# r5 发布包把 questions/groups 的所有列都建成 TEXT（objective='0'/'1'、order_in_paper='10'、
# year='2010'）。Python 里 bool('0') 为真、TEXT 列按字典序排序都与数值语义不符，因此读取层
# 统一走下面三个函数：标志位显式正规化、数字字段安全转型、排序/比较用引擎无关的显式转型。
_FLAG_TRUE_TEXT = frozenset({"1", "true", "t", "yes", "y", "on"})
_FLAG_FALSE_TEXT = frozenset({"", "0", "false", "f", "no", "n", "off", "none", "null"})


def _flag(value) -> bool:
    """把 TEXT/INTEGER 混合来源的标志位统一成 bool（'0'/''/None → False）。"""
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value).strip().lower()
    if text in _FLAG_TRUE_TEXT:
        return True
    if text in _FLAG_FALSE_TEXT:
        return False
    try:
        return float(text) != 0
    except ValueError:
        return True


def _normalize_question_row(question: dict) -> dict:
    """正规化题库行里的数字/标志字段，让 SQLite TEXT 列与 PG INTEGER 镜像输出同一类型。"""
    for field in ("year", "order_in_paper", "pua_remaining"):
        if field in question:
            question[field] = _int_or_none(question[field])
    for field in ("objective", "has_figure", "has_formula"):
        if field in question:
            question[field] = _flag(question[field])
    return question


def _int_sql(con, expression: str) -> str:
    """题序/年份的数值表达式：SQLite 侧对 TEXT 列显式 CAST，PG 镜像导入层已落 INTEGER。

    ``CAST(NULLIF(TRIM(col),'') AS INTEGER)`` 在 SQLite 下对非数字文本安全地得到 0；
    PostgreSQL 若仍是 TEXT 列则 ``col = <int>`` 会直接报类型错误，因此镜像必须按语义建
    INTEGER（见 ``db._ensure_postgres_bank``），这里直接引用数值列并保留索引可用。
    """
    if isinstance(con, db._Postgres):
        return f"COALESCE({expression}, 0)"
    return f"COALESCE(CAST(NULLIF(TRIM({expression}), '') AS INTEGER), 0)"


def _member_sort(entries: list[dict]) -> list[dict]:
    """按数值 order 排序题组成员快照；缺 order 的保持原相对顺序排在后面。"""
    if not entries:
        return entries
    indexed = list(enumerate(entries))
    indexed.sort(key=lambda pair: (
        0 if _int_or_none(pair[1].get("order")) is not None else 1,
        _int_or_none(pair[1].get("order")) or 0,
        pair[0],
    ))
    return [entry for _, entry in indexed]


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
    """Trusted backend projection. Never return this dictionary without filtering.

    ``official:<public_id>`` 引用转 ``app/official.py`` 读取平台精选公开题（按查询时
    可见性判定，撤回/争议立即 404）；其余（裸 qid / ``bank:``）仍读只读题库。
    """
    text = str(qid or "")
    if text.lower().startswith(question_ref.SOURCE_OFFICIAL + ":"):
        from . import official as official_module

        return official_module.load_official_question(text.split(":", 1)[1].strip())
    with _bank() as con:
        row = con.execute(
            f"SELECT * FROM {_bank_table(con, 'questions')} WHERE qid=?", (qid,)
        ).fetchone()
        if row is None:
            raise HTTPException(404, "题目不存在")
        question = dict(row)
        _normalize_question_row(question)
        question["options"] = _json(question.pop("options_json", None), [])
        question["images"] = _json(question.pop("images_json", None), [])
        # 快照契约 §5 新增列：缺失列时全部回落到旧行为，禁止 500。
        question["stem_blocks"] = _dict_list(question.pop("stem_blocks_json", None))
        question["options_blocks"] = _dict_list(question.pop("options_blocks_json", None))
        question["answer_blocks"] = _dict_list(question.pop("answer_blocks_json", None))
        question["solution_blocks"] = _dict_list(question.pop("solution_blocks_json", None))
        question["shared_blocks"] = _dict_list(question.pop("shared_blocks_json", None))
        question["member_index"] = _dict_list(question.pop("member_index_json", None))
        question["member_index"] = _member_sort(
            _overlay_member_live_status(con, _redact_member_blocks(question["member_index"])))
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
                member_index = _member_sort(
                    _overlay_member_live_status(con, _redact_member_blocks(member_index)))
                shared_blocks = _dict_list(stored.get("shared_blocks_json")) or question["shared_blocks"]
                # 组级实时计数：快照 n_practice 可能因隔离过期，详情页与组接口必须一致。
                live_practice = sum(1 for entry in _group_member_view_list(member_index)
                                    if entry.get("in_bank") is not False and entry.get("available"))
                if not member_index:
                    # 无 member_index 的题库（r5 的 member_index_json 全为空）走 questions 反查：
                    # 用实时可作答成员数，避免详情页 n_practice 恒为 0 而组接口是真实值。
                    live_practice = sum(
                        1 for member in _group_members(con, key, stored)
                        if member.get("in_bank") and member.get("available")
                    )
                n_members = len(member_index) or (_int_or_none(stored.get("n_members"))
                                                  or _int_or_none(stored.get("n_children_total")) or 0)
                n_practice = live_practice
                group = {
                    "group_id": key, "material_text": stored.get("material_text") or "",
                    "parent_stem": stored.get("parent_stem") or "",
                    "section_title": stored.get("section_title"),
                    "material_kind": stored.get("material_kind"),
                    "n_children_total": _int_or_none(stored.get("n_children_total")),
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


# ---------------------------------------------------------------------------
# 统一题目引用：bank:<qid> / app:<pl_questions.id>（official 预留）
# 个人题读取统一走 paperlab 的归属校验（本人 + 未删除），不越权读取他人题目。
# ---------------------------------------------------------------------------

def _ref_error(exc) -> HTTPException:
    return HTTPException(422, str(exc))


def _storage_qid(value) -> str:
    """把任意引用/旧裸 qid 转成写库键；bank 保持裸 qid，app 用 app:<id>。"""
    try:
        return question_ref.storage_key(value)
    except question_ref.QuestionRefError as exc:
        raise _ref_error(exc) from None


def _parse_ref(value):
    try:
        return question_ref.parse(value)
    except question_ref.QuestionRefError as exc:
        raise _ref_error(exc) from None


def _paperlab_modules():
    """惰性导入工坊模块：practice 先于 paperlab 初始化，避免模块级循环依赖。"""
    from .paperlab import db as paperlab_db
    return paperlab_db


def _app_question_images(question_id: int, content: dict) -> list[dict]:
    """把个人题 content.images 转成前端可读的配图列表。

    ``file``/``asset`` 都是服务端签发的资产引用，``/api/bank-image?qid=app:<id>``
    会再次校验归属与「该资产确实被本题引用」；``url`` 作为等价直链备用。
    """
    images: list[dict] = []
    for index, image in enumerate(content.get("images") or []):
        if not isinstance(image, dict):
            continue
        asset = _text(image.get("asset"))
        if not asset:
            continue
        images.append({
            "index": index,
            "asset": asset,
            "file": asset,
            "role": _text(image.get("role")) or "figure",
            "url": f"/api/paperlab/questions/{int(question_id)}/images/{index}",
        })
    return images


def _app_content_answers(content: dict) -> list[str]:
    answers = [str(item).strip() for item in content.get("answers") or [] if str(item).strip()]
    if not answers:
        # 材料题/混合题：顶层没有答案时汇总子题答案，仅用于展示，不参与自动判分。
        for part in content.get("parts") or []:
            if isinstance(part, dict):
                answers.extend(str(item).strip() for item in part.get("answers") or [] if str(item).strip())
    return answers


def _app_question_dict(row: dict) -> dict:
    """把 ``pl_questions`` 行投影成与 ``load_question`` 同形的练习题目字典。"""
    question_id = int(row.get("id") or 0)
    db_module = _paperlab_modules()
    content = db_module.json_load(row.get("content_json"), {})
    if not isinstance(content, dict):
        content = {}
    ftype = str(row.get("type") or "SUBJECTIVE").upper()
    options = [
        {"label": str(option.get("label") or "").strip(), "text": str(option.get("text") or "")}
        for option in content.get("options") or []
        if isinstance(option, dict) and str(option.get("label") or "").strip()
    ]
    answers = _app_content_answers(content)
    explanation = str(content.get("explanation") or "")
    rubric = str(content.get("rubric") or "")
    stem = str(content.get("stem") or "").strip()
    practice_type = APP_PRACTICE_TYPE_MAP.get(ftype, "short_answer")
    objective = ftype in ("SINGLE", "MULTIPLE") and bool(options)
    auto_gradeable = (objective or ftype == "BLANK") and bool(answers)
    images = _app_question_images(question_id, content)
    stored_blocks = content.get("blocks") if isinstance(content.get("blocks"), list) else []
    stem_blocks: list[dict] = [
        {key: block[key] for key in ("kind", "text", "latex", "table_html", "order", "page")
         if key in block}
        for block in stored_blocks[:200] if isinstance(block, dict)
    ] if stored_blocks else ([{"kind": "text", "text": stem}] if stem else [])
    for image in images:
        stem_blocks.append({
            "kind": "image",
            "asset": {"file": image["asset"], "role": image.get("role") or "figure"},
        })
    tags = db_module.json_load(row.get("knowledge_tags_json"), [])
    if not isinstance(tags, list):
        tags = []
    version = str(row.get("version") or 1)
    qid = f"{question_ref.SOURCE_APP}:{question_id}"
    answer_text = "；".join(answers)
    return {
        "qid": qid,
        "source": question_ref.SOURCE_APP,
        "ref": qid,
        "source_label": question_ref.SOURCE_LABELS[question_ref.SOURCE_APP],
        "subject": str(row.get("subject") or ""),
        "grade": str(row.get("grade") or ""),
        "year": None,
        "province": "",
        "paper_name": "",
        "paper_type": str(row.get("source") or "app"),
        "type": practice_type,
        "type_label": TYPE_LABELS.get(practice_type, practice_type),
        "number_label": None,
        "group_id": None,
        "group_role": None,
        "stem_plain": stem,
        "stem_blocks": stem_blocks,
        "options": options,
        "options_blocks": [],
        "images": images,
        "objective": objective,
        "has_answer": bool(answers),
        "has_solution": bool(explanation.strip() or rubric.strip()),
        "quality_flags": [],
        "grading_flags": [],
        "data_quality_flags": [],
        "practice_allowed": True,
        "quarantined": False,
        "quarantine_reason": None,
        "practice_status": "practice",
        "grading_status": "auto" if auto_gradeable else "pending",
        "bank_version": "",
        "content_revision": version,
        "answer_revision": version,
        "parent_source_question_id": None,
        "group_kind": None,
        "shared_blocks": [],
        "member_index": [],
        "group": None,
        "answer": {"text": answer_text, "kind": "reference", "note": rubric or None,
                   "solution": explanation},
        "answer_text": answer_text,
        "solution": explanation,
        "answer_blocks": [],
        "solution_blocks": [],
        "knowledge_tags": [str(tag) for tag in tags if str(tag).strip()],
        # 内部字段：不进入 _public_question 白名单，仅供作答判分使用。
        "app_content": content,
        "app_type": ftype,
    }


def _find_app_question_row(user_id: int, question_id: int) -> dict | None:
    """按工坊归属校验读取个人题原始行（本人 + 未删除）；不存在返回 None。"""
    db_module = _paperlab_modules()
    try:
        return db_module.find_question(int(question_id), int(user_id))
    except HTTPException:
        return None
    except Exception:  # noqa: BLE001 - 工坊表未初始化时按不存在处理，不 500
        return None


def _load_app_question(user_id: int, question_id: int) -> dict:
    """读取本人个人题并投影成练习题目；他人/已删除统一 404。"""
    row = _find_app_question_row(user_id, question_id)
    if not row:
        raise HTTPException(404, "题目不存在")
    return _app_question_dict(row)


def load_question_ref(ref, user_id: int) -> dict:
    """按统一引用读取题目：bank 走只读题库，app 走 paperlab 归属校验，official 走公开题。"""
    parsed = _parse_ref(ref)
    if parsed.source == question_ref.SOURCE_APP:
        return _load_app_question(user_id, int(parsed.key))
    if parsed.source == question_ref.SOURCE_OFFICIAL:
        return _official_module().load_official_question(parsed.key)
    return load_question(parsed.key)


def _official_module():
    """惰性导入平台精选读取层（避免模块级循环依赖）。"""
    from . import official as official_module

    return official_module


def _assert_ref_owned(value, user_id: int) -> str:
    """写操作前的归属校验：app 引用必须是本人未删除的题目；返回写库键。

    official 引用不做归属校验（公开题人人可读），但必须当前可见：撤回/争议/未发布
    统一 404，历史记录仍可凭写库键读取。
    """
    parsed = _parse_ref(value)
    if parsed.source == question_ref.SOURCE_APP:
        if _find_app_question_row(user_id, int(parsed.key)) is None:
            raise HTTPException(404, "题目不存在")
    elif parsed.source == question_ref.SOURCE_OFFICIAL:
        _official_module().load_official_question(parsed.key)
    return parsed.storage_key


# ---------------------------------------------------------------------------
# 幂等台账：一次作答只入账一次
# ---------------------------------------------------------------------------

def _ledger_claim(con, user_id: int, kind: str, ledger_key: str, qid: str,
                  attempt_id: int | None = None, now: str | None = None) -> bool:
    """占用幂等键：数据库主键唯一约束保证并发只成功一次。"""
    cursor = con.execute(
        "INSERT INTO submission_ledger(user_id,kind,ledger_key,qid,attempt_id,created_at) "
        "VALUES(?,?,?,?,?,?) ON CONFLICT(user_id,kind,ledger_key) DO NOTHING",
        (int(user_id), kind, ledger_key, qid, attempt_id, now or utcnow()),
    )
    return cursor.rowcount == 1


def _ledger_bind_attempt(con, user_id: int, kind: str, ledger_key: str, attempt_id: int) -> None:
    con.execute(
        "UPDATE submission_ledger SET attempt_id=? WHERE user_id=? AND kind=? AND ledger_key=?",
        (int(attempt_id), int(user_id), kind, ledger_key),
    )


def _ledger_entry(con, user_id: int, kind: str, ledger_key: str):
    return con.execute(
        "SELECT * FROM submission_ledger WHERE user_id=? AND kind=? AND ledger_key=?",
        (int(user_id), kind, ledger_key),
    ).fetchone()


def _ledger_release(con, user_id: int, kind: str, ledger_key: str) -> None:
    """事务内回滚幂等键（仅在同一事务里失败回收时使用）。"""
    con.execute(
        "DELETE FROM submission_ledger WHERE user_id=? AND kind=? AND ledger_key=?",
        (int(user_id), kind, ledger_key),
    )


def _attempt_row_for_user(con, user_id: int, attempt_id: int | None):
    if attempt_id is None:
        return None
    return con.execute(
        "SELECT * FROM attempts WHERE id=? AND user_id=?", (int(attempt_id), int(user_id))
    ).fetchone()


def _find_submission_attempt(user_id: int, ledger_key: str) -> dict | None:
    """已入账的同一提交：返回既有作答（用于直接回放结果，不新增行）。"""
    with connection() as con:
        entry = _ledger_entry(con, user_id, LEDGER_KIND_ATTEMPT, ledger_key)
        if entry is None:
            return None
        row = _attempt_row_for_user(con, user_id, entry["attempt_id"])
    if row is None:
        return None
    return {"attempt_id": int(row["id"]), "attempt": dict(row), "duplicate": True}


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
    qid = row["qid"]
    parsed = question_ref.try_parse(qid)
    source = parsed.source if parsed is not None else question_ref.SOURCE_BANK
    return {
        "id": row["id"], "qid": qid,
        # 统一题目引用：历史作答也带来源，前端可直接按 ref 解析（题库裸 qid 仍向后兼容）。
        "ref": parsed.ref if parsed is not None else qid,
        "source": source,
        "source_label": question_ref.SOURCE_LABELS.get(source, source),
        "answer_text": row["answer_text"],
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
    # 统一题目引用：题库题返回 bank:<qid>，个人题返回 app:<id> 与「我的题库」来源标签；
    # 旧接口字段（qid 仍为写库键）保持不变。
    parsed_ref = question_ref.try_parse(question["qid"])
    if parsed_ref is not None:
        result["ref"] = parsed_ref.ref
        result["source"] = parsed_ref.source
        result["source_label"] = parsed_ref.source_label
    question_source = question.get("source")
    if question_source in (question_ref.SOURCE_APP, question_ref.SOURCE_OFFICIAL):
        result["knowledge_tags"] = list(question.get("knowledge_tags") or [])
        result["grade"] = question.get("grade") or ""
    if question_source == question_ref.SOURCE_OFFICIAL:
        # 平台精选题：公开题号/审核状态/发布版本等随详情返回，前端据此显示来源徽标与
        # 「社区待核验/已核验/平台精选」状态；撤回后题目本身已 404，不会走到这里。
        for key in (
            "public_id", "official_status", "status_label", "review_status",
            "review_status_label", "published_version", "published_at", "featured",
            "community_pending", "notice", "source_declaration", "figure_notice",
        ):
            if key in question:
                result[key] = question[key]
        result["unavailable"] = False
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
            # 统一题目引用：列表卡也带来源与引用，前端可统一按 ref 处理个人题/题库题。
            "ref": question_ref.build(question_ref.SOURCE_BANK, qid),
            "source": question_ref.SOURCE_BANK,
            "source_label": question_ref.SOURCE_LABELS[question_ref.SOURCE_BANK],
            "subject": row["subject"],
            "year": _int_or_none(row["year"]),
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
            "objective": _flag(row["objective"]),
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
    column = f"{prefix}.qid" if prefix else "qid"
    return [f"{column} NOT IN ({marks})"], isolated


def _build_search_sql(
    con: sqlite3.Connection,
    user_id: int,
    *,
    subject: str | None = None,
    type: str | None = None,
    province: str | None = None,
    grade_filter: dict | None = None,
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

    # 学段筛选：只读题库当前发布包（r5）没有 grade 列，此时安全降级为不筛选（由调用方
    # 在 grade_filter 里返回 applied=false 说明）；未来题库带 grade 列时按标准年级/
    # 学段前缀过滤。未知/非法值在调用方已降级，不会走到这里报 422。
    grade_values = list((grade_filter or {}).get("values") or [])
    grade_stages = list((grade_filter or {}).get("stages") or [])
    if (grade_values or grade_stages) and "grade" in _bank_question_columns(con):
        grade_parts: list[str] = []
        if grade_values:
            grade_parts.append("q.grade IN (" + ",".join("?" for _ in grade_values) + ")")
        for stage in grade_stages:
            grade_parts.append("q.grade LIKE ?")
        clauses.append("(" + " OR ".join(grade_parts) + ")")
        values.extend(grade_values)
        values.extend(stage + "%" for stage in grade_stages)

    # r5 的 year 是 TEXT：必须按数值转型比较，否则 '999' 会大于 '2026'。
    # 非法筛选值安全降级为不过滤（FastAPI 层通常已把非整数输入挡成 422）。
    year_expr = _int_sql(con, "q.year")
    year_value = _int_or_none(year)
    year_from_value = _int_or_none(year_from)
    year_to_value = _int_or_none(year_to)
    if year_value is not None:
        clauses.append(f"{year_expr} = ?")
        values.append(year_value)
    if year_from_value is not None:
        clauses.append(f"{year_expr} >= ?")
        values.append(year_from_value)
    if year_to_value is not None:
        clauses.append(f"{year_expr} <= ?")
        values.append(year_to_value)

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
            values.extend([user_id, _review_clock_iso()])
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
        order_sql = ""
        order_params: list = []
        if db.postgres_enabled():
            # 候选必须按命中质量排序：r5 的 LaTeX 题干会命中大量公共三角片段，
            # 无序 LIMIT 40 会把题干完全相同的题挤出去（test_v2 拍照相似题回归）。
            conditions = [_PG_BANK_CONTAINS for _ in snippets]
            contains_any = " OR ".join(conditions)
            snippet_params = [value for snippet in snippets for value in [_contains_pattern(snippet)] * 3]
            hit_score = " + ".join(
                f"(CASE WHEN ({condition}) THEN 1 ELSE 0 END)" for condition in conditions
            )
            sql += f"FROM {questions} q WHERE ({contains_any}) AND {excl}"
            params = [*snippet_params, *excl_params]
            order_sql = f" ORDER BY ({hit_score}) DESC"
            order_params = list(snippet_params)
        else:
            ensure_fts_index(con)
            fts_expr = " OR ".join('"' + s.replace('"', '""') + '"' for s in snippets)
            sql += (f"FROM bank_fts f JOIN {questions} q ON q.qid = f.qid "
                    f"WHERE bank_fts MATCH ? AND {excl}")
            params = [fts_expr, *excl_params]
            # FTS5 的 rank 是 bm25 相关度：相同题干稳定排在最前，LIMIT 截断不会漏掉它。
            order_sql = " ORDER BY f.rank"
        if subject:
            sql += " AND q.subject = ?"
            params.append(subject)
        params.extend(order_params)
        sql += order_sql + " LIMIT 40"
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
                "qid": r["qid"], "subject": r["subject"], "year": _int_or_none(r["year"]),
                "province": r["province"], "paper_name": r["paper_name"],
                "type": r["type"], "type_label": TYPE_LABELS.get(r["type"], r["type"]),
                "stem_plain": (r["stem_plain"] or "")[:200],
                "similarity": score,
                "exact_match": score >= 0.65,
            })
    scored.sort(key=lambda x: x["similarity"], reverse=True)
    reranked = False
    try:
        from .ai import rerank as rerank_module

        if rerank_module.available() and scored:
            # 只把相似度前 20 条送交叉编码器，控制 CPU 时延（与 rank_chunks_hybrid 一致）。
            pool = scored[:20]
            texts = [str(item.get("stem_plain") or "")[:512] for item in pool]
            scores = list(rerank_module.score_texts(text, texts) or [])
            if len(scores) == len(pool):
                for item, score in zip(pool, scores):
                    item["rerank_score"] = round(float(score), 4)
                pool.sort(
                    key=lambda x: (x.get("rerank_score", 0.0), x["similarity"]),
                    reverse=True,
                )
                scored = pool + scored[len(pool):]
                reranked = True
    except Exception:  # 重排只是增强：失败时保持原有相似度排序
        reranked = False
    if not reranked:
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


#: 标准年级兜底（与 ``app/paperlab/db.py::GRADES`` 同口径；paperlab 不可用时仍能下发筛选选项）。
_FALLBACK_GRADES = (
    "小学一年级", "小学二年级", "小学三年级", "小学四年级", "小学五年级", "小学六年级",
    "初中一年级", "初中二年级", "初中三年级",
    "高中一年级", "高中二年级", "高中三年级",
)
_FALLBACK_GRADE_STAGES = ("小学", "初中", "高中")


def _question_grade_options() -> list[str]:
    """可筛选的标准年级列表（与我的题库/试卷工坊共用 ``paperlab.db.GRADES``）。"""
    try:
        from .paperlab import db as paperlab_db

        grades = [str(item) for item in (paperlab_db.GRADES or []) if str(item).strip()]
        return grades or list(_FALLBACK_GRADES)
    except Exception:  # noqa: BLE001 - 试卷工坊不可用时用内置标准年级兜底
        return list(_FALLBACK_GRADES)


def _question_grade_stages() -> list[str]:
    """学段列表（小学/初中/高中）；与 ``app/official.py::GRADE_STAGES`` 同口径。"""
    try:
        return list(_official_module().GRADE_STAGES)
    except Exception:  # noqa: BLE001
        return list(_FALLBACK_GRADE_STAGES)



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
            # 年份按数值排序并输出 int（r5 的 year 是 TEXT，字典序对四位年份碰巧一致，
            # 但输出类型会随题库版本漂移；这里统一成稳定类型）。
            "years": sorted(
                {_int_or_none(r[0]) for r in con.execute(f"SELECT DISTINCT year FROM {questions}")} - {None},
                reverse=True,
            ),
            "total": total, "source_total": total, "available_total": available,
            "quarantined_count": total - available, "difficulties": list(DIFFICULTIES),
            # 学段筛选元数据：前端用标准年级 + 学段生成筛选项（official 按快照 grade 过滤；
            # 当前只读题库 r5 无 grade 列时题库侧安全降级不筛选，grade_note 说明口径）。
            "grades": _question_grade_options(),
            "grade_stages": _question_grade_stages(),
            "grade_note": (
                "学段筛选支持标准年级（精确匹配）与学段（小学/初中/高中，前缀匹配）；"
                "平台精选按快照年级过滤，题库侧仅在有年级字段时生效。"
            ),
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


def _bank_questions_page(
    user_id: int,
    *,
    subject: str | None = None,
    type: str | None = None,
    province: str | None = None,
    grade_filter: dict | None = None,
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
    sort: str = "year_desc",
    q: str | None = None,
    include_reviews: bool = False,
    seen_reviews: str | None = None,
    page: int = 1,
    page_size: int = 12,
) -> dict:
    """题库题分页（原 ``GET /api/questions`` 主体；``source=bank`` 行为保持不变）。"""
    grade_filter = grade_filter if isinstance(grade_filter, dict) else None
    with _app_with_bank() as con:
        grade_applied = bool(
            grade_filter
            and grade_filter.get("valid")
            and (grade_filter.get("values") or grade_filter.get("stages"))
            and "grade" in _bank_question_columns(con)
        )
        # r5 题序/年份都是 TEXT：显式数值转型后再排序（'10' 不能排在 '2' 前面）。
        year_expr = _int_sql(con, "q.year")
        order_expr = _int_sql(con, "q.order_in_paper")
        order_map = {
            "year_desc": f"{year_expr} DESC, q.subject, q.doc_id, {order_expr}",
            "year_asc": f"{year_expr} ASC, q.subject, q.doc_id, {order_expr}",
            "difficulty": (
                "CASE (SELECT difficulty FROM question_metadata WHERE qid=q.qid) "
                f"WHEN '困难' THEN 1 WHEN '中等' THEN 2 WHEN '简单' THEN 3 ELSE 4 END, {year_expr} DESC"
            ),
            "accuracy": (
                "COALESCE((SELECT 1.0*SUM(CASE WHEN result='correct' THEN 1 ELSE 0 END)"
                f"/NULLIF(COUNT(*),0) FROM attempts WHERE qid=q.qid AND result!='pending'), 2.0) ASC, {year_expr} DESC"
            ),
            "random": "RANDOM()",
        }
        join_sql, where_sql, params = _build_search_sql(
            con, user_id, subject=subject, type=type, province=province,
            grade_filter=grade_filter,
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
        items = _batch_summaries(con, rows, user_id, include_ai_diff=include_ai_difficulty)
        review_slots = []

        if include_reviews and items:
            prefs = get_user_prefs(user_id)
            max_slots = min(2, int(prefs.get("interleave_per_page", 2)))
            if max_slots > 0:
                seen_set = _seen_review_set(seen_reviews, items)
                rev_where = where_sql + " AND EXISTS (SELECT 1 FROM wrong_questions wq_i WHERE wq_i.user_id=? AND wq_i.qid=q.qid AND wq_i.archived=0 AND wq_i.next_review_at<=?)"
                rev_params = [*params, user_id, _review_clock_iso()]
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
                    [user_id, *rev_params, max_slots],
                ).fetchall()
                if due_rows:
                    due_cards = _batch_summaries(con, due_rows, user_id, include_ai_diff=include_ai_difficulty)
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
    return {"items": items, "total": total, "review_slots": review_slots,
            "grade_filter_applied": grade_applied}


def _official_questions_page(
    user_id: int,
    *,
    subject: str | None = None,
    type: str | None = None,
    q: str | None = None,
    grade_filter: dict | None = None,
    offset: int = 0,
    page_size: int = 12,
) -> dict:
    """平台精选公开题分页：只应用学科/题型/关键词/学段筛选（题库专属筛选不适用）。"""
    payload = _official_module().search_official(
        keyword=q, subject=subject, type=type, grade=grade_filter,
        limit=page_size, offset=offset,
    )
    questions = [item for item in (payload.get("items") or []) if isinstance(item, dict)]
    qids = [question["qid"] for question in questions]
    latest_map = _latest_attempt_map(user_id, qids)
    mastery_map = _mastery_map(user_id, qids)
    items = [
        _official_summary_card(
            question, latest_map.get(question["qid"]), mastery_map.get(question["qid"]),
        )
        for question in questions
    ]
    return {"items": items, "total": int(payload.get("total") or 0)}


#: ``GET /api/questions`` 的来源筛选：bank（默认，既有行为）/ official（平台精选）/ all（合并）。
QUESTION_SOURCES = ("bank", "official", "all")


def _normalize_question_grade_filter(grade) -> dict | None:
    """学段筛选参数规范化（非法/未知安全降级为不筛选，不抛 422）。"""
    text = str(grade or "").strip()
    if not text:
        return None
    try:
        return _official_module().normalize_grade_filter(text)
    except Exception:  # noqa: BLE001 - 学段筛选项异常不能拖垮题目列表
        return {
            "requested": text, "values": [], "stages": [], "valid": False,
            "degraded": True, "reason": "学段筛选暂不可用，已忽略该筛选",
        }


def _grade_filter_meta(normalized: dict | None, *, bank_applied: bool = False,
                       official_applied: bool = False) -> dict:
    """学段筛选响应元数据（applied 表示该筛选在本响应中是否真正生效）。"""
    normalized = normalized if isinstance(normalized, dict) else {}
    return {
        "requested": normalized.get("requested") or "",
        "applied": bool(bank_applied or official_applied),
        "bank_applied": bool(bank_applied),
        "official_applied": bool(official_applied),
        "values": list(normalized.get("values") or []),
        "stages": list(normalized.get("stages") or []),
        "reason": normalized.get("reason") or "",
    }


@router.get("/questions")
def list_questions(
    subject: str | None = None,
    type: str | None = None,
    province: str | None = None,
    grade: str | None = None,
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
    source: str = Query("bank"),
    page: int = Query(1, ge=1),
    page_size: int = Query(12, ge=1, le=50),
    user=Depends(current_user),
):
    """题目列表：默认 ``source=bank``（既有行为/结构不变）。

    - ``source=official``：平台精选公开题（社区待核验/已核验/精选），支持学科/题型/关键词，
      撤回/争议在查询时立即不可见；
    - ``source=all``：题库题 + 平台精选合并（题库在前），返回 ``bank_total``/``official_total``
      各自总数；二次复习槽位仅 ``source=bank`` 提供；
    - ``grade``：学段筛选（标准年级精确匹配或学段前缀匹配；official 用快照 grade，题库无
      年级字段时安全降级不筛选），非法/未知值不 422，响应追加 ``grade_filter`` 说明；
    - 非法 ``source`` 统一 422。
    """
    source_key = str(source or "bank").strip().lower()
    if source_key not in QUESTION_SOURCES:
        raise HTTPException(422, "来源筛选值无效，只支持 bank / official / all")
    grade_filter = _normalize_question_grade_filter(grade)
    bank_kwargs = dict(
        subject=subject, type=type, province=province, grade_filter=grade_filter,
        year=year, year_from=year_from,
        year_to=year_to, difficulty=difficulty, include_ai_difficulty=include_ai_difficulty,
        kp_id=kp_id, method_id=method_id, status=status, has_answer=has_answer,
        has_solution=has_solution, has_ai_explanation=has_ai_explanation, sort=sort,
        q=q, seen_reviews=seen_reviews,
    )
    if source_key == "bank":
        payload = _bank_questions_page(
            user["id"], include_reviews=include_reviews, page=page, page_size=page_size,
            **bank_kwargs,
        )
        bank_applied = bool(payload.pop("grade_filter_applied", False))
        payload.update({"page": page, "page_size": page_size})
        if grade_filter is not None:
            meta = _grade_filter_meta(grade_filter, bank_applied=bank_applied)
            if grade_filter.get("valid") and not bank_applied:
                meta["reason"] = (
                    "当前只读题库没有年级字段，学段筛选在题库侧暂不生效"
                    "（平台精选按快照年级过滤）"
                )
            payload["grade_filter"] = meta
        return payload
    if source_key == "official":
        page_payload = _official_questions_page(
            user["id"], subject=subject, type=type, q=q, grade_filter=grade_filter,
            offset=(page - 1) * page_size, page_size=page_size,
        )
        result = {
            "items": page_payload["items"],
            "total": page_payload["total"],
            "page": page,
            "page_size": page_size,
            "review_slots": [],
            "source": "official",
            "bank_total": 0,
            "official_total": page_payload["total"],
            "notice": "平台精选列表支持学科、题型、关键词与学段筛选；年份/省份/难度等题库专属筛选不适用。",
        }
        if grade_filter is not None:
            official_applied = bool(
                grade_filter.get("valid")
                and (grade_filter.get("values") or grade_filter.get("stages"))
            )
            result["grade_filter"] = _grade_filter_meta(
                grade_filter, official_applied=official_applied,
            )
        return result
    # source=all：题库在前、精选在后，按合并总偏移严格分页，两类各自返回 total。
    bank_payload = _bank_questions_page(
        user["id"], include_reviews=False, page=page, page_size=page_size, **bank_kwargs,
    )
    bank_applied = bool(bank_payload.pop("grade_filter_applied", False))
    bank_total = int(bank_payload["total"])
    bank_items = list(bank_payload["items"])
    offset = (page - 1) * page_size
    remaining = page_size - len(bank_items)
    official_offset = max(0, offset - bank_total)
    official_items: list[dict] = []
    if remaining > 0:
        official_payload = _official_questions_page(
            user["id"], subject=subject, type=type, q=q, grade_filter=grade_filter,
            offset=official_offset, page_size=remaining,
        )
        official_items = official_payload["items"]
        official_total = int(official_payload["total"])
    else:
        official_total = _official_questions_page(
            user["id"], subject=subject, type=type, q=q, grade_filter=grade_filter,
            offset=0, page_size=0,
        )["total"]
    result = {
        "items": [*bank_items, *official_items],
        "total": bank_total + official_total,
        "page": page,
        "page_size": page_size,
        "review_slots": [],
        "source": "all",
        "bank_total": bank_total,
        "official_total": official_total,
        "notice": "合并列表按「高考题库在前、平台精选在后」排序；二次复习槽位仅在 source=bank 时返回。",
    }
    if grade_filter is not None:
        official_applied = bool(
            grade_filter.get("valid") and (grade_filter.get("values") or grade_filter.get("stages"))
        )
        meta = _grade_filter_meta(
            grade_filter, bank_applied=bank_applied, official_applied=official_applied,
        )
        # 合并列表里「学段筛选完整生效」要求题库与精选两侧都能过滤；题库无年级字段时
        # applied=false，前端会提示「仅平台精选生效」而不是把题库结果伪装成已按学段过滤。
        meta["applied"] = bool(bank_applied and official_applied)
        if official_applied and not bank_applied and not grade_filter.get("degraded"):
            meta["reason"] = (
                "当前只读题库没有年级字段，学段筛选仅对平台精选生效"
            )
        result["grade_filter"] = meta
    return result


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


def _study_context(user_id: int, ref: str) -> dict:
    """第五轮：题目详情的共用学习上下文（证据 / 附件 / 复习卡）。

    读取失败一律降级为空结构并给中文提示，详情主流程不得因共用模块不可用而 500。
    """
    context = {
        "evidence": None,
        "artifacts": [],
        "artifact_total": 0,
        "review_card": None,
        "review_rating_scale": [1, 2, 3, 4],
        "notice": "",
    }
    try:
        from . import study_evidence

        context["evidence"] = study_evidence.question_evidence(user_id, ref)
    except Exception as error:  # noqa: BLE001
        _log.warning("题目详情读取学习证据失败：%s", type(error).__name__)
        context["notice"] = "学习证据暂不可读"
    try:
        from . import study_artifact

        artifacts = study_artifact.artifacts_for_target(user_id, "question", ref)
        context["artifacts"] = list(artifacts.get("items") or [])
        context["artifact_total"] = int(artifacts.get("total") or 0)
    except Exception as error:  # noqa: BLE001
        _log.warning("题目详情读取学习附件失败：%s", type(error).__name__)
        context["notice"] = context["notice"] or "学习附件暂不可读"
    try:
        from . import study_review

        context["review_card"] = study_review.card_by_ref(user_id, ref)
    except Exception as error:  # noqa: BLE001
        _log.warning("题目详情读取复习卡失败：%s", type(error).__name__)
    return context


def _question_detail_payload(question, user_id: int):
    if question["quarantined"]:
        raise HTTPException(409, _detail_blocked_message(question))
    # context 成员可读但不可作答（快照契约 §6.1），作答门禁在 POST /api/attempts。
    payload = _public_question(question, user_id)
    # 第五轮（方案 §4）：详情带同一题目引用下的证据、附件与复习卡，供跨页面入口使用。
    ref = payload.get("ref") or question_ref.build(question_ref.SOURCE_BANK, question.get("qid") or "")
    payload["study"] = _study_context(user_id, ref)
    return payload


@router.get("/questions/{qid:path}")
def question_detail(qid: str, user=Depends(current_user)):
    # bank:<qid> / 旧裸 qid 走只读题库；app:<id> 走 paperlab 归属校验（他人题目 404）。
    question = load_question_ref(qid, user["id"])
    return _question_detail_payload(question, user["id"])


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
        "order": _int_or_none(entry.get("order")),
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
    member_index = _member_sort(_dict_list(stored.get("member_index_json")))
    if member_index:
        entries = _overlay_member_live_status(con, _redact_member_blocks(member_index))
        return [_group_member_view(entry) for entry in entries]
    # 旧题库没有 member_index：回到用 questions 表反查成员（旧接口行为兜底）。
    # r5 的 order_in_paper 是 TEXT，必须按数值排序，否则 '10' 会排在 '2' 前面。
    # group_id 有两种形态（R3 有 796 题已带 ``<doc>:`` 前缀）：拆成等值条件而不是 CASE
    # 表达式，才能命中题库自带的 idx_q_group 索引，避免题详情页每开一题就全表扫描；
    # 成员实时状态随同一条索引查询取回，不再按 qid 二次全表扫描（r5 的 qid 无索引）。
    questions = _bank_table(con, "questions")
    order_expr = _int_sql(con, "q.order_in_paper")
    bare_group_id = group_id.rsplit(":", 1)[-1] if ":" in group_id else group_id
    columns = _bank_question_columns(con)
    select = ["q.qid", "q.number_label", "q.group_role", "q.order_in_paper", "q.type"]
    select.extend(
        f"q.{column}" for column in ("practice_status", "grading_status") if column in columns
    )
    rows = con.execute(
        f"SELECT {', '.join(select)} FROM {questions} q "
        "WHERE q.group_id = ? "
        "OR (q.group_id = ? AND q.doc_id || ':' || q.group_id = ?) "
        f"ORDER BY {order_expr}, q.qid",
        (group_id, bare_group_id, group_id),
    ).fetchall()
    isolated = _active_isolations()
    members = []
    for row in rows:
        qid = row["qid"]
        if qid in QUARANTINED_QUESTIONS:
            continue
        practice_status = _text(_row_value(row, "practice_status")) or "practice"
        grading_status = _text(_row_value(row, "grading_status")) or "auto"
        member = {
            "qid": qid, "in_bank": True, "source_question_id": None,
            "number_label": row["number_label"], "sub_question_label": None,
            "type": row["type"], "group_role": row["group_role"],
            "order": _int_or_none(row["order_in_paper"]),
            "practice_status": practice_status, "grading_status": grading_status,
        }
        isolation = isolated.get(qid)
        if isolation is not None:
            member.update(practice_status="quarantined", grading_status="disabled", isolated=True)
            member["isolation_reason"] = isolation.get("reason")
        member["available"] = member["practice_status"] in ("practice", "review")
        members.append(_group_member_view(member))
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
        "year": _int_or_none(stored.get("year")),
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
    question = load_question_ref(qid, user["id"])
    if latest_attempt(user["id"], question["qid"]) is None:
        raise HTTPException(403, "请先提交作答选项或自己的作答图片，再查看答案与解析")
    return _question_detail_payload(question, user["id"])


@router.get("/attempts/history")
def attempt_history(qid: str, user=Depends(current_user)):
    storage_qid = _assert_ref_owned(qid, user["id"])
    with connection() as con:
        rows = con.execute(
            "SELECT * FROM attempts WHERE user_id=? AND qid=? ORDER BY id DESC LIMIT 30",
            (user["id"], storage_qid),
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


def _record_study_evidence(con, user_id, qid, attempt_id, verdict, can_grade):
    """第五轮：作答同事务写入共享学习证据，并同步四档复习卡（方案 §4）。

    证据是作答的投影：共享模块不可用或写入失败时只记日志，绝不影响作答与错题调度
    主流程；重判（AI 复核）走 :func:`set_ai_grade` 的 revise 路径，不新增有效证据。
    """
    try:
        from . import study_evidence

        # 练习/作答页不提供分步提示，答案在提交后判分（提交前未揭晓）→ 记录为
        # 「无提示、作答前未揭晓」的独立作答；掌握路径的「两道不同题独立答对」据此核验。
        # AI 复核重判只升版该事件，不改变提示/揭晓状态。
        study_evidence.record_attempt_evidence(
            user_id, ref=qid, attempt_id=int(attempt_id), verdict=verdict,
            grade_source="reference" if can_grade else "pending",
            prompt_state="none", reveal_state="before", con=con,
        )
    except Exception as error:  # noqa: BLE001 - 证据是投影，失败不阻断作答
        _log.warning("学习证据写入失败（作答已保存）：%s", type(error).__name__)
    try:
        from . import study_review

        study_review.sync_wrong_question(user_id, qid, con=con)
    except Exception as error:  # noqa: BLE001 - 复习卡同步失败不影响作答与错题本
        _log.warning("复习卡同步失败（作答已保存）：%s", type(error).__name__)


def _record_attempt(user_id, qid, submitted, image_name, verdict, can_grade, now, submission_key=None):
    """同步写入作答与复习调度；``async`` 端点经由 threadpool 调用，避免阻塞事件循环。

    ``submission_key`` 非空时走幂等台账：同一用户同一次提交（客户端 ``submission_id``）
    重试只入账一次，重复调用返回已有作答而不是新增行。台账占用与作答插入在同一事务，
    唯一性由 ``submission_ledger`` 主键保证，并发重复提交同样只会成功入账一次。
    """
    with connection() as con:
        if submission_key or db.postgres_enabled():
            # 幂等路径显式取写锁：并发同一提交在此串行化，落败方拿到已入账结果。
            db.write_lock(con, f"progress:{user_id}")
        if submission_key and not _ledger_claim(
            con, user_id, LEDGER_KIND_ATTEMPT, submission_key, qid, now=now,
        ):
            entry = _ledger_entry(con, user_id, LEDGER_KIND_ATTEMPT, submission_key)
            row = _attempt_row_for_user(
                con, user_id, entry["attempt_id"] if entry is not None else None,
            )
            return {
                "attempt_id": int(row["id"]) if row is not None else None,
                "attempt": dict(row) if row is not None else None,
                "duplicate": True,
            }
        cursor = con.execute(
            "INSERT INTO attempts(user_id,qid,answer_text,image_path,result,grade_source,created_at,graded_at) VALUES(?,?,?,?,?,?,?,?)",
            (user_id, qid, submitted, image_name, verdict, "reference" if can_grade else "pending", now, now if can_grade else None),
        )
        attempt_id = cursor.lastrowid
        if submission_key:
            _ledger_bind_attempt(
                con, user_id, LEDGER_KIND_ATTEMPT, submission_key, int(attempt_id),
            )
        _schedule_result(con, user_id, qid, verdict)
        _record_study_evidence(con, user_id, qid, int(attempt_id), verdict, can_grade)
    return {"attempt_id": int(attempt_id), "attempt": None, "duplicate": False}


def _attempt_submission_payload(user_id, qid, question, verdict, attempt_id,
                                *, attempt_row=None, duplicate=False):
    attempt = _attempt_public(attempt_row) if attempt_row is not None else _attempt_public(latest_attempt(user_id, qid))
    return {
        "attempt": attempt,
        "question": _public_question(question, user_id),
        "result": verdict,
        "attempt_id": attempt_id,
        "duplicate": bool(duplicate),
    }


def _app_graded_verdict(question, submitted_choice: str, submitted_text: str):
    """个人题判分：客观题（单选/多选/填空）按规则判，主观题/混合题保留待核验。

    返回 ``(can_grade, verdict)``；无参考答案或题型不参与自动判分时 ``can_grade=False``。
    """
    ftype = str(question.get("app_type") or "").upper()
    content = question.get("app_content") or {}
    answers = _app_content_answers(content if isinstance(content, dict) else {})
    if not answers:
        return False, "pending"
    if ftype in ("SINGLE", "MULTIPLE"):
        if not submitted_choice:
            return False, "pending"
        labels = {str(option.get("label") or "").upper() for option in question.get("options") or []}
        reference = {label for answer in answers for label in str(answer).upper() if label in labels}
        if not reference:
            return False, "pending"
        return True, ("correct" if set(submitted_choice) == reference else "incorrect")
    if ftype == "BLANK":
        text = (submitted_text or "").strip()
        if not text:
            return False, "pending"
        return True, ("correct" if _app_blank_matches(text, answers) else "incorrect")
    return False, "pending"


_APP_ANSWER_SPLIT_RE = re.compile(r"[;；|]|(?:\s+或\s+)")
_APP_ANSWER_TRANSLATE = str.maketrans({
    "（": "(", "）": ")", "，": ",", "。": ".", "；": ";", "：": ":", "、": ",",
})


def _normalize_app_answer(value) -> str:
    text = str(value if value is not None else "").strip().casefold()
    text = text.replace("$", "").replace("　", " ")
    return re.sub(r"\s+", "", text).translate(_APP_ANSWER_TRANSLATE)


def _app_blank_matches(submitted: str, answers: list[str]) -> bool:
    """填空题文本比对：支持「一个空多个可接受答案」与多空分号分隔。"""
    groups: list[list[str]] = []
    for answer in answers:
        alternatives = [
            normalized for normalized in
            (_normalize_app_answer(part) for part in _APP_ANSWER_SPLIT_RE.split(str(answer)))
            if normalized
        ]
        if alternatives:
            groups.append(alternatives)
    if not groups:
        return False
    values = [
        normalized for normalized in
        (_normalize_app_answer(part) for part in _APP_ANSWER_SPLIT_RE.split(str(submitted or "")))
        if normalized
    ]
    if not values:
        return False
    if len(values) == len(groups):
        return all(value in group for value, group in zip(values, groups))
    # 多空被合写成一串（如两空写成「612」）时，与拼接后的参考答案比较。
    joined_reference = {_normalize_app_answer("".join(group[0] for group in groups))}
    return _normalize_app_answer("".join(values)) in joined_reference


@router.post("/attempts")
async def submit_attempt(
    qid: str = Form(...),
    answer: str | None = Form(None),
    file: UploadFile | None = File(None),
    submission_id: str | None = Form(None),
    user=Depends(current_user),
):
    # 统一题目引用：bank:<qid> / 裸 qid（兼容旧客户端）/ app:<pl_questions.id>。
    parsed_ref = _parse_ref(qid)
    storage_qid = parsed_ref.storage_key
    # 题库/应用库读取都是同步 I/O：统一放 threadpool，别让单个作答把事件循环卡住。
    question = await run_in_threadpool(load_question_ref, qid, user["id"])
    if question["quarantined"]:
        raise HTTPException(409, QUARANTINE_SUBMIT_MESSAGE)
    if not question["practice_allowed"]:
        raise HTTPException(409, CONTEXT_SUBMIT_MESSAGE)
    if answer and len(answer) > 12000:
        raise HTTPException(413, "作答文字过长")
    # 幂等：带 submission_id 的提交重试直接回放已入账结果，不再新增作答行。
    dedupe_key = None
    if submission_id is not None and str(submission_id).strip():
        try:
            dedupe_key = question_ref.submission_key(storage_qid, submission_id)
        except question_ref.QuestionRefError as exc:
            raise HTTPException(422, str(exc)) from None
        existing = await run_in_threadpool(_find_submission_attempt, user["id"], dedupe_key)
        if existing is not None:
            return await run_in_threadpool(
                _attempt_submission_payload, user["id"], storage_qid, question,
                existing["attempt"]["result"], existing["attempt_id"],
                attempt_row=existing["attempt"], duplicate=True,
            )
    content = await file.read(MAX_IMAGE_BYTES + 1) if file else None
    if file and not content:
        raise HTTPException(422, "作答图片不能为空")
    if not (answer and answer.strip()) and not content:
        raise HTTPException(422, "请提交作答选项或自己的作答图片")
    app_source = question.get("source") == question_ref.SOURCE_APP
    official_source = question.get("source") == question_ref.SOURCE_OFFICIAL
    # r5 的 objective 是 TEXT（'0'/'1'）：先正规化再判型。有选项的题按选项格式校验输入，
    # 但只有 objective 为真且选项非空时才允许选择题判分；objective='0' 同时含选项的异常题
    # 保留作答、判 pending（r5 有 184 条可练非客观题带选项，不能当选择题判分）。
    objective = _flag(question.get("objective"))
    options = [
        option for option in (question.get("options") or [])
        if isinstance(option, dict) and option.get("label") is not None
    ]
    choice_format = bool(options)
    graded_choice = objective and choice_format
    submitted = (answer or "").strip()
    submitted_choice = ""
    if submitted and choice_format:
        submitted_choice = _choice_answer(submitted, options, question.get("type") == "multi_choice")
        submitted = submitted_choice
    elif submitted and not content and not app_source and not official_source:
        raise HTTPException(422, "主观题请上传自己的作答图片后提交")
    image_name = await run_in_threadpool(_save_image, content) if content else None
    reference = (question.get("answer_text") or "").strip().upper()
    reference_valid = bool(re.fullmatch(r"[A-H]+", reference)) and set(reference).issubset(
        {str(option["label"]).upper() for option in options}
    )
    # 快照契约 §6.3：只有 practice+auto 的题才自动判分；review/pending/disabled 保留作答判 pending。
    # 数据侧标记「答案解析公式无法渲染」时也不能自动判分（作答仍保存）。
    answer_flags = " ".join(question.get("data_quality_flags") or [])
    formula_gap = "公式无法渲染" in answer_flags or "公式缺失" in answer_flags
    auto_grade = (question["practice_status"] == "practice"
                  and question["grading_status"] == "auto" and not formula_gap)
    if app_source or official_source:
        # 个人题/平台精选题：与题库题同一门槛（客观题判分、主观题 pending），
        # 填空题按文本规则判分；平台精选题的参考答案来自不可变公开快照。
        can_grade, verdict = _app_graded_verdict(question, submitted_choice, submitted)
        can_grade = can_grade and auto_grade and not question["grading_flags"]
        if not can_grade:
            verdict = "pending"
    else:
        can_grade = graded_choice and submitted and reference_valid and auto_grade and not question["grading_flags"]
        verdict = ("correct" if set(submitted) == set(reference) else "incorrect") if can_grade else "pending"
    now = utcnow()
    try:
        recorded = await run_in_threadpool(
            _record_attempt, user["id"], storage_qid, submitted, image_name, verdict, can_grade,
            now, dedupe_key,
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
    if recorded.get("duplicate"):
        # 并发下另一请求已先入账：丢弃本次新存图片，回放既有结果。
        if image_name:
            await run_in_threadpool(storage.discard_file, "attempts", image_name)
        attempt_row = recorded.get("attempt")
        replay_verdict = attempt_row["result"] if attempt_row else "pending"
        return await run_in_threadpool(
            _attempt_submission_payload, user["id"], storage_qid, question, replay_verdict,
            recorded.get("attempt_id"), attempt_row=attempt_row, duplicate=True,
        )
    return await run_in_threadpool(
        _attempt_submission_payload, user["id"], storage_qid, question, verdict,
        recorded.get("attempt_id"),
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
        # 第五轮：AI 复核重判只升版同一条证据，不新增有效证据（方案 §4「判分修订」）。
        try:
            from . import study_evidence

            study_evidence.revise_attempt_evidence(
                user_id, attempt_id=attempt_id, verdict=verdict,
                grade_source="ai_cross_verified" if source == "ai_cross_verified" else "pending",
                con=con,
            )
        except Exception as error:  # noqa: BLE001 - 重判证据失败不影响作答记录本身
            _log.warning("AI 重判证据写入失败（作答已保存）：%s", type(error).__name__)
        try:
            from . import study_review

            study_review.sync_wrong_question(user_id, row["qid"], con=con)
        except Exception as error:  # noqa: BLE001
            _log.warning("复习卡同步失败（重判已保存）：%s", type(error).__name__)


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


def _app_question_image_response(parsed, file: str, asset_id: str | None, user_id: int):
    """个人题配图读取：复用 paperlab 资产归属校验，不通过 ``/api/bank-image`` 越权。

    同时校验：题目属于当前用户且未删除（``find_question``）、请求的资产确实被本题
    content 引用、资产可读（``assets.resolve_asset_for_read``）。任何失败统一 404，
    不泄露他人资产是否存在。
    """
    from .paperlab import assets as paperlab_assets
    from .paperlab.db import ASSET_FOLDER

    row = _find_app_question_row(user_id, int(parsed.key))
    if not row:
        raise HTTPException(404, "题图不存在")
    db_module = _paperlab_modules()
    content = db_module.json_load(row.get("content_json"), {})
    content = content if isinstance(content, dict) else {}
    known = {
        _text(image.get("asset"))
        for image in content.get("images") or []
        if isinstance(image, dict) and _text(image.get("asset"))
    }
    wanted = [value for value in (asset_id, file) if value]
    matched = next((value for value in wanted if value in known), None)
    if matched is None:
        raise HTTPException(404, "题图不存在")
    asset = paperlab_assets.resolve_asset_for_read(matched, int(user_id))
    if not asset:
        raise HTTPException(404, "题图不存在")
    return storage.file_response(
        str(asset.get("folder") or ASSET_FOLDER),
        str(asset.get("storage_ref") or ""),
        media_type=str(asset.get("media_type") or "image/png"),
        headers={"Cache-Control": "private, max-age=3600", "Vary": "Cookie"},
    )


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
    parsed_qid = question_ref.try_parse(qid) if qid else None
    if parsed_qid is not None and parsed_qid.source == question_ref.SOURCE_APP:
        # 个人题配图走独立归属校验分支，不查只读题库目录。
        return _app_question_image_response(parsed_qid, file, asset_id, user["id"])
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
    question = load_question_ref(data.qid, user["id"])
    storage_qid = question["qid"]
    if data.level not in LEVELS:
        raise HTTPException(422, "掌握程度无效")
    now = utcnow()
    with connection() as con:
        db.write_lock(con, f"progress:{user['id']}")
        attempt = con.execute(
            "SELECT id, result FROM attempts WHERE user_id=? AND qid=? ORDER BY id DESC LIMIT 1",
            (user["id"], storage_qid),
        ).fetchone()
        # §4.2: 「未掌握」随时可选；「初步掌握」至少作答过一次；「比较/熟练掌握」需最新作答判对
        if data.level != "未掌握" and not attempt:
            raise HTTPException(403, "请先完成一次作答")
        if data.level in LEVELS[2:] and attempt["result"] != "correct":
            raise HTTPException(403, "最新一次作答判定正确后，才能标注比较掌握或熟练掌握")
        con.execute(
            "INSERT INTO mastery(user_id,qid,level,updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(user_id,qid) DO UPDATE SET level=excluded.level,updated_at=excluded.updated_at",
            (user["id"], storage_qid, data.level, now),
        )
        # Link mastery to wrongbook review schedule
        wrong = con.execute(
            "SELECT * FROM wrong_questions WHERE user_id=? AND qid=? AND archived=0",
            (user["id"], storage_qid),
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
                    (interval, due, archived, pushed_attempt, user["id"], storage_qid),
                )
    return {"qid": storage_qid, "level": data.level}


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
    question = load_question_ref(data.qid, user["id"])
    storage_qid = question["qid"]
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
            (user["id"], storage_qid, data.reason, now, now, json.dumps(tags, ensure_ascii=False), now),
        )
    return {"qid": storage_qid, "added": True}


@router.post("/wrongbook/tags")
def update_wrong_tags(data: ErrorTagsInput, user=Depends(current_user)):
    storage_qid = _assert_ref_owned(data.qid, user["id"])
    tags = list(dict.fromkeys(t.strip()[:40] for t in data.error_tags if t.strip()))
    with connection() as con:
        row = con.execute("SELECT 1 FROM wrong_questions WHERE user_id=? AND qid=?", (user["id"], storage_qid)).fetchone()
        if not row:
            raise HTTPException(404, "该题不在错题本中")
        if data.reason is not None:
            con.execute(
                "UPDATE wrong_questions SET error_tags_json=?, reason=? WHERE user_id=? AND qid=?",
                (json.dumps(tags, ensure_ascii=False), data.reason, user["id"], storage_qid),
            )
        else:
            con.execute(
                "UPDATE wrong_questions SET error_tags_json=? WHERE user_id=? AND qid=?",
                (json.dumps(tags, ensure_ascii=False), user["id"], storage_qid),
            )
    return {"qid": storage_qid, "error_tags": tags}


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
    # 归档/删除只操作本人错题行：个人题即使已被删除，也允许清理历史错题记录。
    storage_qid = _storage_qid(qid)
    with connection() as con:
        row = con.execute("SELECT 1 FROM wrong_questions WHERE user_id=? AND qid=?", (user["id"], storage_qid)).fetchone()
        if not row:
            raise HTTPException(404, "该题不在错题本中")
        if permanent:
            con.execute("DELETE FROM wrong_questions WHERE user_id=? AND qid=?", (user["id"], storage_qid))
        else:
            con.execute("UPDATE wrong_questions SET archived=1 WHERE user_id=? AND qid=?", (user["id"], storage_qid))
    return {"qid": storage_qid, "archived": True, "permanent": permanent}


@router.post("/wrongbook/restore")
def restore_wrong(data: WrongInput, user=Depends(current_user)):
    storage_qid = _assert_ref_owned(data.qid, user["id"])
    with connection() as con:
        # 恢复错题等于重新入本：重置连对计数与间隔，让调度从 1 天重新开始。
        con.execute(
            "UPDATE wrong_questions SET archived=0, streak=0, interval_days=1, "
            "last_mastery_push_attempt=NULL, next_review_at=? WHERE user_id=? AND qid=?",
            (utcnow(), user["id"], storage_qid),
        )
    return {"qid": storage_qid, "restored": True}


APP_QID_PREFIX = f"{question_ref.SOURCE_APP}:"
OFFICIAL_QID_PREFIX = f"{question_ref.SOURCE_OFFICIAL}:"


def _is_app_qid(value) -> bool:
    return str(value or "").startswith(APP_QID_PREFIX)


def _is_official_qid(value) -> bool:
    return str(value or "").startswith(OFFICIAL_QID_PREFIX)


def _app_question_for_card(user_id: int, qid: str) -> dict | None:
    """按 ``app:<id>`` 读取个人题练习投影；他人/已删除返回 None（错题卡降级显示）。"""
    parsed = question_ref.try_parse(qid)
    if parsed is None or parsed.source != question_ref.SOURCE_APP:
        return None
    row = _find_app_question_row(user_id, int(parsed.key))
    return _app_question_dict(row) if row else None


def _latest_attempt_map(user_id: int, qids: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    if not qids:
        return out
    with connection() as con:
        for chunk in _chunked(qids):
            marks = ",".join("?" for _ in chunk)
            rows = con.execute(
                f"SELECT a.* FROM attempts a INNER JOIN ("
                f" SELECT qid, MAX(id) AS max_id FROM attempts WHERE user_id=? AND qid IN ({marks}) GROUP BY qid"
                f") t ON a.id=t.max_id",
                [user_id, *chunk],
            ).fetchall()
            for row in rows:
                out[row["qid"]] = dict(row)
    return out


def _mastery_map(user_id: int, qids: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    if not qids:
        return out
    with connection() as con:
        for chunk in _chunked(qids):
            marks = ",".join("?" for _ in chunk)
            for row in con.execute(
                f"SELECT qid, level FROM mastery WHERE user_id=? AND qid IN ({marks})",
                [user_id, *chunk],
            ):
                out[row["qid"]] = row["level"]
    return out


def _app_summary_card(question: dict, latest_row: dict | None, mastery_level: str | None) -> dict:
    """个人题错题卡：与 ``_batch_summaries`` 的题库卡同形，前端无需分支。"""
    qid = question["qid"]
    return {
        "qid": qid,
        "ref": qid,
        "source": question_ref.SOURCE_APP,
        "source_label": question_ref.SOURCE_LABELS[question_ref.SOURCE_APP],
        "subject": question.get("subject") or "",
        "grade": question.get("grade") or "",
        "year": None,
        "province": "",
        "paper_name": "",
        "paper_type": "app",
        "type": question.get("type") or "short_answer",
        "type_label": question.get("type_label") or TYPE_LABELS.get(question.get("type") or "", "其他"),
        "number_label": None,
        "group_id": None,
        "group_role": None,
        "stem_plain": question.get("stem_plain") or "",
        "stem_blocks": question.get("stem_blocks") or [],
        "options": question.get("options") or [],
        "images": question.get("images") or [],
        "objective": bool(question.get("objective")),
        "has_answer": bool(question.get("has_answer")),
        "has_solution": bool(question.get("has_solution")),
        "quality_flags": [],
        "practice_allowed": True,
        "isolated": False,
        "bank_version": "",
        "practice_status": question.get("practice_status") or "practice",
        "grading_status": question.get("grading_status") or "pending",
        "content_revision": question.get("content_revision"),
        "answer_revision": question.get("answer_revision"),
        "mastery": mastery_level or "未掌握",
        "unlocked": latest_row is not None,
        "latest_attempt": _attempt_public(latest_row) if latest_row else None,
        "knowledge_points": [],
        "methods": [],
        "has_ai_explanation": False,
        "knowledge_tags": list(question.get("knowledge_tags") or []),
        "difficulty": "待标注",
        "difficulty_source": "",
        "ai_difficulty": None,
        "ai_difficulty_score": None,
        "stat_accuracy": None,
        "stat_attempts": 0,
        "unavailable": False,
    }


def _missing_app_card(qid: str) -> dict:
    """个人题已被删除/不可读时的占位卡：保留错题记录与引用，不 500、不丢历史。"""
    return {
        "qid": qid,
        "ref": qid,
        "source": question_ref.SOURCE_APP,
        "source_label": question_ref.SOURCE_LABELS[question_ref.SOURCE_APP],
        "subject": "",
        "year": None,
        "province": "",
        "paper_name": "",
        "type": "unknown",
        "type_label": TYPE_LABELS["unknown"],
        "number_label": None,
        "group_id": None,
        "stem_plain": "",
        "options": [],
        "images": [],
        "objective": False,
        "has_answer": False,
        "has_solution": False,
        "quality_flags": [],
        "practice_allowed": False,
        "isolated": False,
        "bank_version": "",
        "practice_status": "practice",
        "grading_status": "pending",
        "mastery": "未掌握",
        "unlocked": False,
        "latest_attempt": None,
        "knowledge_points": [],
        "methods": [],
        "has_ai_explanation": False,
        "difficulty": "待标注",
        "difficulty_source": "",
        "unavailable": True,
    }


def _official_question_for_card(user_id: int, qid: str) -> dict | None:
    """按 ``official:<public_id>`` 读取平台精选题投影；撤回/争议/不存在返回 None。

    ``user_id`` 仅为与个人题读取保持同签名（公开题人人可读，与用户无关）。
    """
    parsed = question_ref.try_parse(qid)
    if parsed is None or parsed.source != question_ref.SOURCE_OFFICIAL:
        return None
    try:
        return _official_module().load_official_question(parsed.key)
    except HTTPException:
        return None


def _official_summary_card(question: dict, latest_row: dict | None,
                           mastery_level: str | None) -> dict:
    """平台精选题卡片：与题库卡/个人题卡同形，前端无需为新来源再加分支。"""
    source_meta = {
        "public_id": question.get("public_id") or "",
        "official_status": question.get("official_status") or "",
        "status_label": question.get("status_label") or "",
        "review_status": question.get("review_status") or "",
        "review_status_label": question.get("review_status_label") or "",
        "published_version": question.get("published_version"),
        "featured": bool(question.get("featured")),
        "community_pending": bool(question.get("community_pending")),
        "notice": question.get("notice") or "",
        "source_declaration": question.get("source_declaration") or {},
        "figure_notice": question.get("figure_notice") or "",
    }
    return {
        "qid": question["qid"],
        "ref": question["qid"],
        "source": question_ref.SOURCE_OFFICIAL,
        "source_label": question_ref.SOURCE_LABELS[question_ref.SOURCE_OFFICIAL],
        **source_meta,
        "subject": question.get("subject") or "",
        "grade": question.get("grade") or "",
        "year": None,
        "province": "",
        "paper_name": "",
        "paper_type": "official",
        "type": question.get("type") or "short_answer",
        "type_label": question.get("type_label") or TYPE_LABELS.get(
            question.get("type") or "", "其他"),
        "number_label": None,
        "group_id": None,
        "group_role": None,
        "stem_plain": question.get("stem_plain") or "",
        "stem_blocks": question.get("stem_blocks") or [],
        "options": question.get("options") or [],
        "images": question.get("images") or [],
        "objective": bool(question.get("objective")),
        "has_answer": bool(question.get("has_answer")),
        "has_solution": bool(question.get("has_solution")),
        "quality_flags": [],
        "practice_allowed": True,
        "isolated": False,
        "bank_version": "",
        "practice_status": question.get("practice_status") or "practice",
        "grading_status": question.get("grading_status") or "pending",
        "content_revision": question.get("content_revision"),
        "answer_revision": question.get("answer_revision"),
        "mastery": mastery_level or "未掌握",
        "unlocked": latest_row is not None,
        "latest_attempt": _attempt_public(latest_row) if latest_row else None,
        "knowledge_points": [],
        "methods": [],
        "has_ai_explanation": False,
        "knowledge_tags": list(question.get("knowledge_tags") or []),
        "difficulty": "待标注",
        "difficulty_source": "",
        "ai_difficulty": None,
        "ai_difficulty_score": None,
        "stat_accuracy": None,
        "stat_attempts": 0,
        "unavailable": False,
        "withdrawn": False,
    }


def _missing_official_card(qid: str) -> dict:
    """平台精选题当前不可见（撤回/争议/未发布）时的占位卡。

    历史作答、错题与成绩保留；只显示「内容已撤回/暂停展示」，不回显受限正文与配图。
    """
    parsed = question_ref.try_parse(qid)
    public_id = parsed.key if parsed is not None else str(qid or "").split(":", 1)[-1]
    try:
        info = _official_module().inspect_official(public_id)
    except Exception:  # noqa: BLE001 - 广场表不可读时按不可见占位，不阻断错题/复习列表
        info = {}
    status = str(info.get("status") or "")
    return {
        "qid": qid,
        "ref": qid,
        "source": question_ref.SOURCE_OFFICIAL,
        "source_label": question_ref.SOURCE_LABELS[question_ref.SOURCE_OFFICIAL],
        "public_id": public_id,
        "official_status": status,
        "status_label": info.get("status_label") or "",
        "review_status": info.get("review_status") or "",
        "review_status_label": info.get("review_status_label") or "",
        "published_version": None,
        "featured": False,
        "community_pending": False,
        "notice": info.get("notice") or "",
        "subject": "",
        "year": None,
        "province": "",
        "paper_name": "",
        "type": "unknown",
        "type_label": TYPE_LABELS["unknown"],
        "number_label": None,
        "group_id": None,
        "stem_plain": "",
        "options": [],
        "images": [],
        "objective": False,
        "has_answer": False,
        "has_solution": False,
        "quality_flags": [],
        "practice_allowed": False,
        "isolated": False,
        "bank_version": "",
        "practice_status": "practice",
        "grading_status": "pending",
        "mastery": "未掌握",
        "unlocked": False,
        "latest_attempt": None,
        "knowledge_points": [],
        "methods": [],
        "has_ai_explanation": False,
        "difficulty": "待标注",
        "difficulty_source": "",
        "ai_difficulty": None,
        "ai_difficulty_score": None,
        "stat_accuracy": None,
        "stat_attempts": 0,
        "unavailable": True,
        "withdrawn": status == "withdrawn",
    }


def _review_clock_iso() -> str:
    """复习到期比较使用服务端复习时钟（默认真实 UTC；验收可用 STUDY_REVIEW_CLOCK 固定）。

    M2 起复习卡是到期真相源：旧到期过滤/``due`` 标记与复习模块必须用同一个时钟，
    否则固定时钟验收或将来注入服务端时钟时，「卡到期」与「错题到期」会判断不一致。
    复习模块不可用时回退 ``db.utcnow()``。
    """
    try:
        from . import study_review

        return study_review.now_utc().isoformat()
    except Exception:  # noqa: BLE001 - 回退旧时钟，不阻断错题/复习列表
        return utcnow()


def _app_wrong_rows(user_id, *, due_only=False, include_archived=False, error_tag=None):
    """应用库里的个人题错题行（与题库错题同一张 wrong_questions 表）。"""
    clauses = ["wq.user_id = ?", "wq.qid LIKE 'app:%'"]
    params: list = [user_id]
    if not include_archived:
        clauses.append("wq.archived = 0")
    if due_only:
        clauses.append("wq.next_review_at <= ?")
        params.append(_review_clock_iso())
    if error_tag:
        clauses.append("wq.error_tags_json LIKE ? ESCAPE '\\'")
        params.append(_contains_pattern(f'"{error_tag}"'))
    with connection() as con:
        rows = con.execute(
            "SELECT wq.* FROM wrong_questions wq WHERE " + " AND ".join(clauses)
            + " ORDER BY wq.next_review_at ASC, wq.qid ASC",
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def _app_wrong_cards(user_id, rows: list[dict]) -> list[dict]:
    qids = [row["qid"] for row in rows]
    latest_map = _latest_attempt_map(user_id, qids)
    mastery_map = _mastery_map(user_id, qids)
    now_iso = _review_clock_iso()
    cards = []
    for row in rows:
        qid = row["qid"]
        question = _app_question_for_card(user_id, qid)
        card = _app_summary_card(question, latest_map.get(qid), mastery_map.get(qid)) \
            if question else _missing_app_card(qid)
        card.update({
            "reason": row.get("reason"),
            "failures": row.get("failures"),
            "streak": row.get("streak"),
            "next_review_at": row.get("next_review_at"),
            "added_at": row.get("added_at"),
            "last_result": row.get("last_result"),
            "interval_days": row.get("interval_days"),
            "review_count": row.get("review_count"),
            "archived": bool(row.get("archived")),
            "error_tags": _json(row.get("error_tags_json"), []),
            "days_since_wrong": _days_since(row.get("last_wrong_at") or row.get("added_at")),
            "due": bool(row.get("next_review_at")) and row["next_review_at"] <= now_iso,
        })
        cards.append(card)
    return cards


def _official_wrong_rows(user_id, *, due_only=False, include_archived=False, error_tag=None):
    """应用库里的平台精选题错题行（与题库/个人题同一张 ``wrong_questions`` 表）。"""
    clauses = ["wq.user_id = ?", f"wq.qid LIKE '{OFFICIAL_QID_PREFIX}%'"]
    params: list = [user_id]
    if not include_archived:
        clauses.append("wq.archived = 0")
    if due_only:
        clauses.append("wq.next_review_at <= ?")
        params.append(_review_clock_iso())
    if error_tag:
        clauses.append("wq.error_tags_json LIKE ? ESCAPE '\\'")
        params.append(_contains_pattern(f'"{error_tag}"'))
    with connection() as con:
        rows = con.execute(
            "SELECT wq.* FROM wrong_questions wq WHERE " + " AND ".join(clauses)
            + " ORDER BY wq.next_review_at ASC, wq.qid ASC",
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def _official_wrong_cards(user_id, rows: list[dict]) -> list[dict]:
    """平台精选题错题卡：题目可见时给正文，撤回/争议后降级为「内容已撤回」占位卡。"""
    qids = [row["qid"] for row in rows]
    latest_map = _latest_attempt_map(user_id, qids)
    mastery_map = _mastery_map(user_id, qids)
    now_iso = _review_clock_iso()
    cards = []
    for row in rows:
        qid = row["qid"]
        question = _official_question_for_card(user_id, qid)
        if question:
            card = _official_summary_card(
                question, latest_map.get(qid), mastery_map.get(qid),
            )
        else:
            card = _missing_official_card(qid)
        card.update({
            "reason": row.get("reason"),
            "failures": row.get("failures"),
            "streak": row.get("streak"),
            "next_review_at": row.get("next_review_at"),
            "added_at": row.get("added_at"),
            "last_result": row.get("last_result"),
            "interval_days": row.get("interval_days"),
            "review_count": row.get("review_count"),
            "archived": bool(row.get("archived")),
            "error_tags": _json(row.get("error_tags_json"), []),
            "days_since_wrong": _days_since(row.get("last_wrong_at") or row.get("added_at")),
            "due": bool(row.get("next_review_at")) and row["next_review_at"] <= now_iso,
        })
        cards.append(card)
    return cards


def _app_card_matches(card, *, subject=None, type=None, province=None, year=None,
                      difficulty=None, q=None, kp_id=None, method_id=None) -> bool:
    """把题库侧筛选套到个人题卡：只支持学科/题型/关键词，其余高级筛选直接排除。"""
    if subject:
        if card.get("subject") not in _split_csv(subject):
            return False
    if type:
        if card.get("type") not in _split_csv(type):
            return False
    if province or year or difficulty or kp_id or method_id:
        return False
    if q and q.strip():
        haystack = str(card.get("stem_plain") or "").casefold()
        terms = [term for term in re.split(r"\s+", q.strip()) if term]
        if not all(term.casefold() in haystack for term in terms):
            return False
    return True


def _apply_review_due(user_id: int, items: list[dict]) -> list[dict]:
    """M2：用复习卡的权威到期值覆盖旧读投影（错题本/复习队列与首页/学情同一个到期值）。

    复习卡写入时已把到期镜像到 ``wrong_questions``（见 study_review._mirror_due_to_wrong），
    这里再次以卡为准覆盖展示字段，并标注 ``due_source``，便于验收核对「所有入口同一天」。
    模块不可用时静默降级为旧值，不影响错题/复习列表。
    """
    try:
        from . import study_review

        cards = study_review.due_map(user_id)
    except Exception as error:  # noqa: BLE001 - 复习排程不可用不能拖垮错题本
        _log.warning("复习卡到期读取失败（沿用旧到期）：%s", type(error).__name__)
        cards = {}
    now_iso = _review_clock_iso()
    for item in items:
        entry = cards.get(str(item.get("qid") or ""))
        if not entry:
            item.setdefault("due_source", "legacy")
            continue
        item["next_review_at"] = entry["due_at"]
        item["due"] = str(entry["due_at"]) <= now_iso
        item["due_source"] = "review-cards"
        item["review_card_id"] = entry["card_id"]
        item["review_card_state"] = entry["state"]
        item["review_card_rated"] = bool(entry.get("rated"))
    return items


def _review_stats_block(user_id: int) -> dict:
    """M2：/api/stats 的复习排程统一口径（读服务端复习卡；失败降级为旧错题到期）。"""
    try:
        from . import study_review

        stats = study_review.stats(user_id)
        return {
            "available": True,
            "due_now": int(stats.get("due_now") or 0),
            "due_today": int(stats.get("due_today") or 0),
            "total": int(stats.get("total") or 0),
            "algorithm": stats.get("algorithm"),
            "algorithm_version": stats.get("algorithm_version"),
            "due_source": "review-cards",
            "note": "",
        }
    except Exception as error:  # noqa: BLE001 - 排程不可用时统计仍返回旧口径
        return {
            "available": False,
            "due_now": None,
            "due_today": None,
            "total": None,
            "algorithm": None,
            "algorithm_version": None,
            "due_source": "legacy",
            "note": f"复习排程暂不可用，due_count 沿用旧错题到期口径（{type(error).__name__}）",
        }


def _wrong_items_bank(
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
    """题库错题（旧路径保持原语义与 SQL 分页）。"""
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
            w_params.append(_review_clock_iso())
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
        now_iso = _review_clock_iso()
        for card, raw in zip(cards, rows):
            card["ref"] = card.get("ref") or question_ref.build(question_ref.SOURCE_BANK, card["qid"])
            card["source"] = question_ref.SOURCE_BANK
            card["source_label"] = question_ref.SOURCE_LABELS[question_ref.SOURCE_BANK]
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
    """错题列表：题库题 + 个人题（app）+ 平台精选（official）统一结构，按复习时间合并排序。

    没有 app/official 错题时走原题库 SQL 分页（行为/性能不变）；存在其它来源错题时，
    取到 ``page*page_size`` 条题库卡与全部应用侧错题卡合并后再切页，保证三种来源同队列。
    平台精选题被撤回/争议后降级为「内容已撤回」占位卡（历史成绩保留）。
    """
    app_rows = _app_wrong_rows(
        user_id, due_only=due_only, include_archived=include_archived, error_tag=error_tag,
    )
    official_rows = _official_wrong_rows(
        user_id, due_only=due_only, include_archived=include_archived, error_tag=error_tag,
    )
    app_cards = [
        card for card in _app_wrong_cards(user_id, app_rows)
        if _app_card_matches(
            card, subject=subject, type=type, province=province, year=year,
            difficulty=difficulty, q=q, kp_id=kp_id,
        )
    ] if app_rows else []
    official_cards = [
        card for card in _official_wrong_cards(user_id, official_rows)
        if _app_card_matches(
            card, subject=subject, type=type, province=province, year=year,
            difficulty=difficulty, q=q, kp_id=kp_id,
        )
    ] if official_rows else []
    if not app_cards and not official_cards:
        cards, total = _wrong_items_bank(
            user_id, subject=subject, due_only=due_only, type=type, province=province,
            year=year, difficulty=difficulty, q=q, kp_id=kp_id, error_tag=error_tag,
            include_archived=include_archived, page=page, page_size=page_size,
        )
        return _apply_review_due(user_id, cards), total
    window = max(int(page) * int(page_size), int(page_size))
    bank_cards, bank_total = _wrong_items_bank(
        user_id, subject=subject, due_only=due_only, type=type, province=province,
        year=year, difficulty=difficulty, q=q, kp_id=kp_id, error_tag=error_tag,
        include_archived=include_archived, page=1, page_size=window,
    )
    merged = sorted(
        [*bank_cards, *app_cards, *official_cards],
        key=lambda card: (str(card.get("next_review_at") or ""), str(card.get("qid") or "")),
    )
    start = (max(int(page), 1) - 1) * int(page_size)
    return _apply_review_due(user_id, merged[start:start + int(page_size)]), (
        bank_total + len(app_cards) + len(official_cards)
    )


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
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        # M2：到期值以服务端复习卡为准（卡到期镜像到旧错题字段），所有页面同一日期。
        "due_source": "review-cards",
        "due_note": "错题到期时间由服务端四档复习排程写入；首页、复习队列、学情与学习计划使用同一天。",
    }


@router.get("/wrongbook/export")
def export_wrongbook(
    format: str = Query("apkg", pattern="^(apkg|csv)$"),
    subject: str | None = None,
    include_archived: bool = False,
    limit: int = Query(200, ge=1, le=200),
    user=Depends(current_user),
):
    """错题本导出：`apkg`（Anki 牌组，按学科分组，正面题干+选项，背面答案+解析+错因）
    或 `csv`（Front/Back/Tags 三列，可直接导入 Anki）。"""
    from . import anki_export

    groups = anki_export.wrongbook_cards(
        user["id"], subject=subject, include_archived=include_archived, limit=limit,
    )
    total = sum(len(cards) for _deck, cards in groups)
    if not total:
        raise HTTPException(404, "错题本里还没有可导出的题目")
    if format == "csv":
        buf = StringIO()
        writer = csv.writer(buf)
        writer.writerow(["Front", "Back", "Tags"])
        for _deck, cards in groups:
            for card in cards:
                writer.writerow([
                    anki_export.html_to_text(card["front"]),
                    anki_export.html_to_text(card["back"]),
                    " ".join(card["tags"]),
                ])
        return Response(
            content="\ufeff" + buf.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers=anki_export.attachment_headers("知序错题.csv"),
        )
    try:
        data = anki_export.build_apkg(groups)
    except ImportError:
        raise HTTPException(503, "Anki 导出组件未安装：请执行 pip install genanki==0.13.1")
    if not data:
        raise HTTPException(500, "Anki 牌组生成失败，请稍后重试")
    return Response(
        content=data,
        media_type="application/vnd.anki",
        headers=anki_export.attachment_headers(f"知序错题-{datetime.now().strftime('%Y%m%d')}.apkg"),
    )


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
        params = [_review_clock_iso(), user["id"]]
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
    return {
        "items": items,
        "total": total,
        "message": "到期错题推荐，不改变检索结果与总数",
        "due_source": "review-cards",
        "due_note": "到期时间以服务端四档复习排程为准（与首页/错题本/学情同一日期）。",
    }


def _pending_attempt_cards(user_id, *, subject=None, exclude=None, limit=50) -> list[dict]:
    """未判分题目：最近一次作答为 pending 的题（题库题 + 个人题统一结构）。

    未判分题不写入 ``wrong_questions`` 的错题调度，单列在复习队列，等判分/自评后再入队。
    """
    exclude = set(exclude or ())
    with connection() as con:
        rows = con.execute(
            "SELECT a.* FROM attempts a INNER JOIN ("
            " SELECT qid, MAX(id) AS max_id FROM attempts WHERE user_id=? GROUP BY qid"
            ") t ON a.id=t.max_id WHERE a.result='pending' ORDER BY a.id DESC LIMIT ?",
            (int(user_id), int(limit) * 4),
        ).fetchall()
        in_wrongbook = {
            row[0] for row in con.execute(
                "SELECT qid FROM wrong_questions WHERE user_id=? AND archived=0", (int(user_id),)
            )
        }
    attempts = [dict(row) for row in rows if row["qid"] not in exclude]
    cards: list[dict] = []
    bank_attempts = [
        row for row in attempts
        if not _is_app_qid(row["qid"]) and not _is_official_qid(row["qid"])
    ]
    if bank_attempts:
        qids = [row["qid"] for row in bank_attempts]
        by_qid = {row["qid"]: row for row in bank_attempts}
        with _app_with_bank() as con:
            bank_rows = _fetch_bank_rows(con, qids)
            bank_cards = _batch_summaries(con, bank_rows, user_id)
        for card in bank_cards:
            qid = card["qid"]
            card.setdefault("ref", question_ref.build(question_ref.SOURCE_BANK, qid))
            card.setdefault("source", question_ref.SOURCE_BANK)
            card.setdefault("source_label", question_ref.SOURCE_LABELS[question_ref.SOURCE_BANK])
            card.update({
                "graded": False,
                "pending": True,
                "status": "pending",
                "in_wrongbook": qid in in_wrongbook,
                "latest_attempt": _attempt_public(by_qid.get(qid)),
            })
            cards.append(card)
    for row in attempts:
        if not _is_app_qid(row["qid"]):
            continue
        qid = row["qid"]
        question = _app_question_for_card(user_id, qid)
        card = _app_summary_card(question, row, None) if question else _missing_app_card(qid)
        card.update({
            "graded": False,
            "pending": True,
            "status": "pending",
            "in_wrongbook": qid in in_wrongbook,
            "latest_attempt": _attempt_public(row),
        })
        cards.append(card)
    for row in attempts:
        if not _is_official_qid(row["qid"]):
            continue
        qid = row["qid"]
        question = _official_question_for_card(user_id, qid)
        card = _official_summary_card(question, row, None) if question else _missing_official_card(qid)
        card.update({
            "graded": False,
            "pending": True,
            "status": "pending",
            "in_wrongbook": qid in in_wrongbook,
            "latest_attempt": _attempt_public(row),
        })
        cards.append(card)
    if subject:
        subjects = _split_csv(subject)
        cards = [card for card in cards if card.get("subject") in subjects]
    return cards[:int(limit)]


@router.get("/review/queue")
def review_queue(
    subject: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    user=Depends(current_user),
):
    """统一复习队列：题库题、个人题（app 引用）与平台精选（official）同一结构；未判分题单列。

    ``items`` 为已判分的到期/历史错题（含 ``ref``、``source``、``source_label``、
    ``graded``）；``pending_items`` 为最近一次作答仍未判分的题（``graded=false``），
    单列且不参与错题调度。平台精选题被撤回/争议后降级为「内容已撤回」占位卡。
    """
    cards, _total = _wrong_items(
        user["id"], subject=subject, include_archived=False, page=1, page_size=limit,
    )
    graded: list[dict] = []
    pending: list[dict] = []
    pending_qids: set[str] = set()
    for card in cards:
        latest = card.get("latest_attempt") or {}
        if card.get("last_result") == "pending" or latest.get("result") == "pending":
            pending_qids.add(card["qid"])
            pending.append({
                **card, "graded": False, "pending": True,
                "status": "pending", "in_wrongbook": True,
            })
        else:
            graded.append({**card, "graded": True, "pending": False, "status": "graded"})
    pending.extend(
        _pending_attempt_cards(user["id"], subject=subject, exclude=pending_qids, limit=limit)
    )
    return {
        "items": graded,
        "pending_items": pending,
        "total": len(graded) + len(pending),
        "graded_count": len(graded),
        "pending_count": len(pending),
        "limit": limit,
        "message": "题库题、个人题与平台精选题在同一复习队列；未判分题单独列出，不计入错题复习安排",
        "due_source": "review-cards",
        "due_note": "队列内到期时间以服务端四档复习排程为准；评分后同一日期同时反映到首页、错题本与学情。",
    }


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
            (user["id"], _review_clock_iso()),
        ).fetchone()[0]
        counts = {row[0]: row[1] for row in con.execute("SELECT level,COUNT(*) FROM mastery WHERE user_id=? GROUP BY level", (user["id"],))}
        recent = [dict(row) for row in con.execute("SELECT * FROM attempts WHERE user_id=? ORDER BY id DESC LIMIT 6", (user["id"],))]
        qids = [row[0] for row in con.execute("SELECT DISTINCT qid FROM attempts WHERE user_id=?", (user["id"],))]
        # 个人题（app:<id>）与题库题同一口径单独计数：不改变既有 total/practiced 语义。
        app_attempts = con.execute(
            "SELECT COUNT(*) FROM attempts WHERE user_id=? AND qid LIKE 'app:%'", (user["id"],)
        ).fetchone()[0]
        app_practiced = con.execute(
            "SELECT COUNT(DISTINCT qid) FROM attempts WHERE user_id=? AND qid LIKE 'app:%'", (user["id"],)
        ).fetchone()[0]
        app_wrong_count = con.execute(
            "SELECT COUNT(*) FROM wrong_questions WHERE user_id=? AND archived=0 AND qid LIKE 'app:%'",
            (user["id"],),
        ).fetchone()[0]
        # 平台精选（official:<public_id>）同样单独计数：既有字段语义保持不变。
        official_attempts = con.execute(
            "SELECT COUNT(*) FROM attempts WHERE user_id=? AND qid LIKE 'official:%'", (user["id"],)
        ).fetchone()[0]
        official_practiced = con.execute(
            "SELECT COUNT(DISTINCT qid) FROM attempts WHERE user_id=? AND qid LIKE 'official:%'",
            (user["id"],),
        ).fetchone()[0]
        official_wrong_count = con.execute(
            "SELECT COUNT(*) FROM wrong_questions WHERE user_id=? AND archived=0 "
            "AND qid LIKE 'official:%'",
            (user["id"],),
        ).fetchone()[0]
    # 平台精选统计口径（可见性查询时判定，撤回/争议不计入）：与首页/学情共用同一口径；
    # 既有平铺字段 official_attempts/official_practiced/official_wrong_count 语义保持不变。
    try:
        official_caliber = _official_module().official_caliber(user["id"])
    except Exception:  # noqa: BLE001 - 精选口径失败不能拖垮整体学习统计
        official_caliber = {
            "available": False, "total_questions": 0, "practice_pool": 0,
            "auto_gradable": 0, "attempts": 0, "practiced": 0, "wrong_count": 0,
            "due_reviews": 0, "note": "平台精选统计暂不可用",
        }
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
        "app_attempts": app_attempts, "app_practiced": app_practiced, "app_wrong_count": app_wrong_count,
        "official_attempts": official_attempts, "official_practiced": official_practiced,
        "official_wrong_count": official_wrong_count,
        "official": official_caliber,
        # M2：复习排程统一口径（与首页/错题/学情同一到期值）。
        "review": _review_stats_block(user["id"]),
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
                # r5 的 year 是 TEXT：后台报错工单展示与详情页保持同一数字类型。
                record["year"] = _int_or_none(record.get("year"))
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
            "WHERE qk.source IN ('ai','ai_pending_review','local_ai','local_ai_pending_review')"
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
