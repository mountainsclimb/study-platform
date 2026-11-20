"""AI workflows package: providers, protocol adapters, Cross-Verification 2.0, Notes Agent, Upload pipeline & streaming jobs."""
import asyncio
import socket

from fastapi import APIRouter
import httpx

from .. import db
from ..db import ensure_column
from . import netguard
from .adapters import MAX_RESPONSE_BYTES, SYSTEM, _call_model, call_structured
from .embeddings import embedding_status
from .jobs import (
    MAX_ACTIVE,
    JobInput,
    _job_public,
    _job_update,
    _run_job,
    _tasks,
    reap_stale_jobs,
    recover_queued_jobs,
    redis_url,
    router as jobs_router,
)
from .notes_agent import (
    MAX_DOCUMENT_CHARS,
    MAX_INPUT_CHARS,
    TEMPLATES,
    _extract_document,
    _notes_job,
    assemble_prompt_layers,
    load_prompt_grid,
    router as notes_router,
)
from .providers import (
    PRESETS,
    _allowed_hostnames,
    _cipher,
    _identity,
    _owned,
    _selected_provider,
    _validate_url,
    infer_capabilities,
    router as providers_router,
)
from .upload_pipeline import _recognize_job, _tagging_job, router as upload_router
from .verify import (
    MAX_FILE,
    _bank_evidence,
    _explain_job,
    _file_path,
    _grade_job,
    _image_part,
    _parse_grade,
    _parse_rubric,
    _usable_reference,
    compare_answers,
    load_explain_modes,
)

PROMPT_VERSION = "study-2026-11-20-v2"

router = APIRouter()
router.include_router(providers_router)
router.include_router(upload_router)
router.include_router(notes_router)
router.include_router(jobs_router)


def init_ai():
    with db.connection() as con:
        db.write_lock(con, "ai-schema")
        con.executescript("""
            CREATE TABLE IF NOT EXISTS ai_providers(
                id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
                name TEXT NOT NULL, kind TEXT NOT NULL, base_url TEXT NOT NULL,
                encrypted_key TEXT NOT NULL, key_hint TEXT NOT NULL,
                models_json TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS ai_uploads(
                id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
                filename TEXT NOT NULL, storage_name TEXT NOT NULL, media_type TEXT NOT NULL,
                text_content TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS ai_documents(
                id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
                title TEXT NOT NULL, storage_name TEXT NOT NULL, pages INTEGER NOT NULL,
                status TEXT NOT NULL, chunks_json TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS ai_notes(
                id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
                document_id INTEGER REFERENCES ai_documents(id), title TEXT NOT NULL,
                content TEXT NOT NULL, sources_json TEXT NOT NULL,
                origin TEXT NOT NULL DEFAULT 'private', published INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS ai_jobs(
                id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
                task TEXT NOT NULL, status TEXT NOT NULL, stage TEXT NOT NULL,
                request_json TEXT NOT NULL, result_json TEXT, error TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS ai_audit(
                id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
                action TEXT NOT NULL, resource_id INTEGER NOT NULL, created_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS ai_jobs_user ON ai_jobs(user_id,created_at);
        """)
        ensure_column(con, "ai_jobs", "partial_content", "TEXT NOT NULL DEFAULT ''")
        ensure_column(con, "ai_jobs", "checkpoint_json", "TEXT")
        ensure_column(con, "ai_jobs", "cancelled", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(con, "ai_jobs", "dispatch_token", "TEXT")
        ensure_column(con, "ai_uploads", "extra_files_json", "TEXT NOT NULL DEFAULT '[]'")
        ensure_column(con, "ai_uploads", "recognized_json", "TEXT")
        ensure_column(con, "ai_uploads", "confirmed_json", "TEXT")
        ensure_column(con, "ai_uploads", "status", "TEXT NOT NULL DEFAULT 'uploaded'")
        ensure_column(con, "ai_documents", "ocr_pages_json", "TEXT")
        ensure_column(con, "ai_documents", "doc_version", "INTEGER NOT NULL DEFAULT 1")
        ensure_column(con, "ai_documents", "ocr_range_json", "TEXT NOT NULL DEFAULT '{}'")
        # 向量索引按资料版本与内容摘要写入：晚完成的旧批次不会覆盖新版本的索引。
        ensure_column(con, "ai_documents", "vector_status", "TEXT NOT NULL DEFAULT 'pending'")
        ensure_column(con, "ai_documents", "vector_digest", "TEXT")
        ensure_column(con, "ai_documents", "vector_updated_at", "TEXT")
        ensure_column(con, "ai_notes", "kp_id", "INTEGER")
        ensure_column(con, "ai_notes", "subject", "TEXT")
        ensure_column(con, "ai_notes", "doc_ids_json", "TEXT NOT NULL DEFAULT '[]'")
        ensure_column(con, "ai_notes", "prompt_config_json", "TEXT NOT NULL DEFAULT '{}'")
        # P1: 产物与任务标识绑定，同一任务重试只产生一份最终产物（幂等写入）。
        ensure_column(con, "ai_notes", "job_id", "INTEGER")
        ensure_column(con, "ai_notes", "source_versions_json", "TEXT NOT NULL DEFAULT '{}'")
        ensure_column(con, "ai_explanations", "job_id", "INTEGER")
        ensure_column(con, "ai_jobs", "cancel_requested_at", "TEXT")
        ensure_column(con, "ai_jobs", "finished_at", "TEXT")
        # 任务租约心跳：Worker 宕机后租约超期的任务可被回收为 interrupted，不再永久占用 MAX_ACTIVE。
        ensure_column(con, "ai_jobs", "heartbeat_at", "TEXT")
        # 自定义厂商：鉴权方式 / 加密的自定义请求头 / 协议兼容微调（旧记录自动取默认值）。
        ensure_column(con, "ai_providers", "auth_mode", "TEXT NOT NULL DEFAULT 'auto'")
        ensure_column(con, "ai_providers", "extra_headers_enc", "TEXT NOT NULL DEFAULT ''")
        ensure_column(con, "ai_providers", "compat_json", "TEXT NOT NULL DEFAULT '{}'")
        con.execute(
            "CREATE TABLE IF NOT EXISTS ai_embeddings_config("
            "user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,"
            "mode TEXT NOT NULL DEFAULT 'off',"
            "provider_id INTEGER,"
            "base_url TEXT NOT NULL DEFAULT '',"
            "model TEXT NOT NULL DEFAULT '',"
            "api_key_enc TEXT NOT NULL DEFAULT '',"
            "updated_at TEXT NOT NULL)"
        )
        con.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_ai_notes_job ON ai_notes(job_id)")
        con.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_ai_explanations_job ON ai_explanations(job_id)")
    # 启动恢复：宁可靠租约判定，也不能跳过 dispatch_token 非空的任务（否则旧任务永久占满账户配额）。
    reap_stale_jobs()
    (db.DATA_DIR / "ai-files").mkdir(parents=True, exist_ok=True)
    _cipher()


async def shutdown_ai():
    pending = list(_tasks)
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


__all__ = [
    "router", "init_ai", "shutdown_ai", "PROMPT_VERSION", "PRESETS", "TEMPLATES",
    "JobInput", "_cipher", "_validate_url", "_call_model", "call_structured",
    "_parse_grade", "_parse_rubric", "_usable_reference", "_extract_document",
    "_bank_evidence", "_grade_job", "_explain_job", "_notes_job", "_recognize_job",
    "_tagging_job", "_run_job", "compare_answers", "assemble_prompt_layers",
    "load_prompt_grid", "load_explain_modes", "infer_capabilities",
    "embedding_status", "netguard", "httpx", "socket",
]
