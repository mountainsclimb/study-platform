"""Entrypoint: python -m uvicorn app.main:app --host 127.0.0.1 --port 8765."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import hashlib
import hmac
import logging
import os
from pathlib import Path
import re
import socket
import sqlite3
import ssl
from urllib.parse import parse_qs, unquote, urlsplit

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import admission, auth, db

WEB_DIR = Path(__file__).resolve().parents[1] / "web"
health_log = logging.getLogger("zhixu.health")
VERSION = "0.1.0"


def _compute_build_id() -> str:
    """应用构建号：内容指纹（源码 + 前端资源），同一份代码稳定、改代码即变化。

    用于把 OpenAPI、就绪信息、响应头与页面上的版本对齐，识别「旧进程 / 旧标签页」。
    可用 ``STUDY_BUILD_ID`` 显式覆盖（容器镜像可写入提交号）。
    """
    override = os.environ.get("STUDY_BUILD_ID", "").strip()
    if override:
        return override
    digest = hashlib.sha256()
    root = Path(__file__).resolve().parents[1]
    patterns = [
        "app/**/*.py",
        "app/ai/prompts/**/*.yaml",
        "web/js/**/*.js",
        "web/styles.css",
        "web/styles-pages.css",
        "web/index.html",
    ]
    files = []
    for pattern in patterns:
        files.extend(root.glob(pattern))
    for path in sorted(files):
        if "__pycache__" in path.parts:
            continue
        try:
            digest.update(path.relative_to(root).as_posix().encode("utf-8"))
            digest.update(path.read_bytes())
        except OSError:
            continue
    return digest.hexdigest()[:12]


BUILD_ID = _compute_build_id()
BUILD_LABEL = "%s+%s" % (VERSION, BUILD_ID)

# 上游错误体/诊断信息里可能夹带密钥：统一脱敏后再返回，避免未鉴权回显。
_SECRET_PATTERNS = (
    (re.compile(r"sk-[A-Za-z0-9_\-]{8,}"), "sk-***"),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=\-]{8,}"), "Bearer ***"),
)


def _redact_secrets(value: str) -> str:
    """脱敏 OpenAI 风格 ``sk-...`` 与 ``Bearer ...`` 凭证片段。"""
    text = str(value)
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


_DEFAULT_PORTS = {"http": 80, "https": 443}


def _origin_matches_host(origin: str, host_header: str, request_scheme: str = "") -> bool:
    """规范化 CSRF Origin 与 Host 比较。

    - 主机名大小写不敏感；
    - 默认端口等价：``https://a.com:443`` ≡ ``https://a.com``，``http://a.com:80`` ≡ ``http://a.com``；
    - Host 头通常省略默认端口，此时接受 Origin 的「协议默认端口」；Host 显式带端口时必须完全一致。
    """
    try:
        parsed = urlsplit(origin)
        scheme = (parsed.scheme or "").lower()
        if scheme not in _DEFAULT_PORTS or not parsed.hostname:
            return False
        origin_port = parsed.port or _DEFAULT_PORTS[scheme]
        host = urlsplit("//" + (host_header or "").strip())
        hostname = host.hostname or ""
        host_port = host.port
    except ValueError:
        return False
    if not hostname:
        return False
    if parsed.hostname.lower() != hostname.lower():
        return False
    if host_port is None:
        # Host 未写端口：只接受 Origin 自身协议的默认端口（:443/https、:80/http）。
        return origin_port == _DEFAULT_PORTS[scheme]
    return origin_port == host_port


def _etag_for_response(response) -> str | None:
    """按文件 mtime_ns + size 生成强 ETag；非文件响应返回 None。"""
    path = getattr(response, "path", None)
    if not path:
        return None
    try:
        stat = Path(path).stat()
    except OSError:
        return None
    return '"%x-%x"' % (stat.st_mtime_ns, stat.st_size)


def _etag_matches(if_none_match: str | None, etag: str) -> bool:
    if not if_none_match:
        return False
    if if_none_match.strip() == "*":
        return True
    for token in if_none_match.split(","):
        token = token.strip()
        if token.startswith("W/"):
            token = token[2:].strip()
        if token == etag:
            return True
    return False


def _health_token_ok(request: Request) -> bool:
    """部署探针口令（STUDY_HEALTH_TOKEN）或管理员可读完整就绪详情。"""
    expected = os.environ.get("STUDY_HEALTH_TOKEN", "").strip()
    provided = request.headers.get("X-Health-Token", "")
    if not expected or not provided:
        return False
    # 常量时间比较；用字节比较避免非 ASCII 头内容触发 TypeError。
    return hmac.compare_digest(expected.encode("utf-8"), provided.encode("utf-8"))


def _ready_detail_allowed(request: Request) -> bool:
    if _health_token_ok(request):
        return True
    try:
        user = auth.current_user(request)
    except Exception:  # 未登录 / 数据库暂不可用都不能让就绪检查 500
        return False
    return bool(user and user.get("role") == "admin")


@asynccontextmanager
async def lifespan(app):
    from . import ai, practice
    db.init_db()
    practice.init_practice()
    ai.init_ai()
    dispatcher = asyncio.create_task(ai.recover_queued_jobs()) if ai.redis_url() else None
    try:
        yield
    finally:
        if dispatcher is not None:
            dispatcher.cancel()
            await asyncio.gather(dispatcher, return_exceptions=True)
        try:
            await ai.jobs.close_redis_connections()
        except Exception:  # noqa: BLE001 - 退出阶段不能因连接关闭异常阻断进程结束
            pass
        await ai.shutdown_ai()
        db.close_pool()


app = FastAPI(title="知序 · AI 题库与知识清单", version=BUILD_LABEL, lifespan=lifespan)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=[
    value.strip() for value in os.environ.get("STUDY_ALLOWED_HOSTS", "127.0.0.1,localhost,[::1]").split(",")
    if value.strip()
])
app.include_router(auth.router)


@app.exception_handler(HTTPException)
async def redacted_http_exception(request: Request, exc: HTTPException):
    """HTTP 错误体统一脱敏：任何端点回显的上游报错都不得带出 API Key。"""
    detail = _redact_secrets(exc.detail) if isinstance(exc.detail, str) else exc.detail
    return JSONResponse(
        {"detail": detail},
        status_code=exc.status_code,
        headers=getattr(exc, "headers", None),
    )


@app.middleware("http")
async def security_headers(request: Request, call_next):
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if origin and not _origin_matches_host(
            origin, request.headers.get("host", ""), request.url.scheme
        ):
            return JSONResponse({"detail": "不接受跨站提交"}, status_code=403)
        if request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse({"detail": "不接受跨站提交"}, status_code=403)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    # 构建号随每个响应下发：旧标签页发现构建号变化时提示刷新，避免用旧资源提交新数据。
    response.headers["X-Zhixu-Build"] = BUILD_ID
    response.headers["X-Zhixu-Bank"] = db.bank_version_hint() or ""
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data: blob:; font-src 'self' data:; "
        "style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; "
        "object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
    )
    if request.url.path.startswith("/api/"):
        # 端点自己声明的缓存策略优先（如 bank-image 的 private, max-age=3600）。
        response.headers.setdefault("Cache-Control", "no-store")
    return response


@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "bank_available": (db.BANK_DIR / "题库.db").is_file(),
        "version": VERSION,
        "build_id": BUILD_ID,
        "version_label": BUILD_LABEL,
        "bank_version": db.bank_version_hint(),
    }


# ---------------------------------------------------------------------------
# P2 存活与就绪：逐个依赖定位受影响功能，便于部署与恢复演练。
# ---------------------------------------------------------------------------

AFFECTED_FEATURES = {
    "app_db": ["登录与账号", "作答记录", "错题与复习", "笔记与资料"],
    "bank": ["题库检索", "题目详情与作答", "AI 讲解与核验"],
    "storage": ["资料上传", "作答图片", "笔记附件与导出"],
    "redis": ["AI 任务排队与跨进程取消"],
    "worker": ["AI 任务执行", "任务续跑与取消确认"],
}


def _check_app_db() -> dict:
    engine = "postgresql" if db.postgres_enabled() else "sqlite"
    try:
        with db.connection() as con:
            con.execute("SELECT 1").fetchone()
        return {"ok": True, "engine": engine, "detail": "应用库可连接"}
    except Exception as exc:  # 数据库驱动或文件不可用时仍要返回可读诊断
        health_log.warning("Application database is not ready: %s", type(exc).__name__)
        return {"ok": False, "engine": engine, "detail": f"应用库不可用（{type(exc).__name__}）"}


def _check_bank() -> dict:
    path = db.BANK_DIR / "题库.db"
    source = db.BANK_SOURCE
    if not path.is_file():
        return {"ok": False, "detail": "未找到题库文件，请检查 STUDY_BANK_DIR",
                "bank_version": None, "source": source, "bank_dir": str(db.BANK_DIR)}
    try:
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10) as con:
            columns = {row[1] for row in con.execute("PRAGMA table_info(questions)")}
            version = None
            if "bank_version" in columns:
                row = con.execute("SELECT bank_version FROM questions LIMIT 1").fetchone()
                version = row[0] if row else None
            total = con.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
        expected = os.environ.get("STUDY_EXPECTED_BANK_VERSION", "").strip()
        resolved = version or "baseline"
        if expected and resolved != expected:
            # 挂错目录或进程仍读旧版时直接降级，避免新旧混合服务。
            health_log.warning("Bank version mismatch: expected %s, got %s", expected, resolved)
            return {
                "ok": False,
                "detail": f"题库版本不匹配：期望 {expected}，实际 {resolved}",
                "questions": total,
                "bank_version": resolved,
                "expected_bank_version": expected,
                "source": source,
                "bank_dir": str(db.BANK_DIR),
            }
        return {
            "ok": total > 0,
            "detail": f"题库可读，共 {total} 道题" if total else "题库为空",
            "questions": total,
            "bank_version": resolved,
            "expected_bank_version": expected or None,
            # 读写的就是这个目录：便于核对「进程读的是哪一版」，避免挂错盘或旧进程。
            "source": source,
            "bank_dir": str(db.BANK_DIR),
        }
    except sqlite3.Error as exc:
        health_log.warning("Question bank is not ready: %s", exc)
        return {"ok": False, "detail": "题库文件无法读取，请检查题库版本",
                "bank_version": None, "source": source, "bank_dir": str(db.BANK_DIR)}


def _check_storage() -> dict:
    bucket = os.environ.get("S3_BUCKET", "").strip()
    if bucket:
        try:
            from .storage import _s3_client
            endpoint = os.environ.get("S3_ENDPOINT_URL") or None
            region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
            client = _s3_client(endpoint, region or ("us-east-1" if endpoint else None))
            client.head_bucket(Bucket=bucket)
            return {"ok": True, "backend": "s3", "detail": "对象存储可连接"}
        except Exception as exc:
            health_log.warning("Object storage is not ready: %s", type(exc).__name__)
            return {"ok": False, "backend": "s3", "detail": "对象存储不可用，请检查桶与访问密钥"}
    try:
        db.DATA_DIR.mkdir(parents=True, exist_ok=True)
        probe = db.DATA_DIR / ".ready-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return {"ok": True, "backend": "local", "detail": "本地数据目录可写"}
    except OSError as exc:
        health_log.warning("Local storage is not writable: %s", type(exc).__name__)
        return {"ok": False, "backend": "local", "detail": "本地数据目录不可写，请检查 STUDY_DATA_DIR"}


_REDIS_DEFAULT_PORT = 6379
_REDIS_DEFAULT_TIMEOUT = 2.0


def _redis_target(url: str) -> dict:
    """解析 REDIS_URL：rediss:// TLS、百分号编码密码、Redis 6+ ACL 用户名与 socket_timeout。

    只依赖标准库：用 RESP 协议直接做 AUTH/SELECT/PING，避免为了就绪检查引入 redis 包。
    """
    parts = urlsplit(url)
    scheme = (parts.scheme or "").strip().lower()
    if scheme not in ("redis", "rediss"):
        raise ValueError("REDIS_URL 协议必须是 redis:// 或 rediss://")
    username = unquote(parts.username) if parts.username else None
    password = unquote(parts.password) if parts.password else None
    if username and password is None:
        raise ValueError("REDIS_URL 中 ACL 用户名必须同时提供密码")
    db_index = 0
    if parts.path and parts.path.strip("/"):
        try:
            db_index = int(parts.path.strip("/").split("/")[0])
        except ValueError:
            db_index = 0
    timeout = _REDIS_DEFAULT_TIMEOUT
    if parts.query:
        raw_timeout = (parse_qs(parts.query).get("socket_timeout") or [""])[0]
        if raw_timeout:
            try:
                timeout = max(0.5, min(10.0, float(raw_timeout)))
            except ValueError:
                timeout = _REDIS_DEFAULT_TIMEOUT
    return {
        "host": parts.hostname or "127.0.0.1",
        "port": parts.port or _REDIS_DEFAULT_PORT,
        "username": username,
        "password": password,
        "db": db_index,
        "tls": scheme == "rediss",
        "timeout": timeout,
    }


def _redis_commands(target: dict) -> list[tuple[str, ...]]:
    """RESP 命令序列：AUTH（ACL 用户名/密码或单密码）→ SELECT → PING。"""
    commands: list[tuple[str, ...]] = []
    if target["username"] is not None and target["password"] is not None:
        commands.append(("AUTH", target["username"], target["password"]))
    elif target["password"] is not None:
        commands.append(("AUTH", target["password"]))
    if target["db"]:
        commands.append(("SELECT", str(target["db"])))
    commands.append(("PING",))
    return commands


def _resp_encode(args) -> bytes:
    payload = [("*%d\r\n" % len(args)).encode("utf-8")]
    for arg in args:
        data = str(arg).encode("utf-8")
        payload.append(("$%d\r\n" % len(data)).encode("utf-8"))
        payload.append(data + b"\r\n")
    return b"".join(payload)


class _RedisReader:
    """极简 RESP 行读取器：够 AUTH/SELECT/PING 的 +OK / -ERR / +PONG 回复即可。"""

    def __init__(self, sock, limit: int = 8192):
        self.sock = sock
        self.buffer = b""
        self.limit = limit

    def readline(self) -> bytes:
        while b"\r\n" not in self.buffer:
            chunk = self.sock.recv(512)
            if not chunk:
                raise OSError("redis_connection_closed")
            self.buffer += chunk
            if len(self.buffer) > self.limit:
                raise OSError("redis_reply_too_large")
        line, self.buffer = self.buffer.split(b"\r\n", 1)
        return line


def _redis_ping(target: dict) -> dict:
    """按目标配置完成 AUTH/SELECT/PING 握手；异常统一为 OSError 供上层诊断。"""
    sock = socket.create_connection((target["host"], target["port"]), timeout=target["timeout"])
    try:
        sock.settimeout(target["timeout"])
        if target["tls"]:
            context = ssl.create_default_context()
            sock = context.wrap_socket(sock, server_hostname=target["host"])
        reader = _RedisReader(sock)
        for command in _redis_commands(target):
            sock.sendall(_resp_encode(command))
            reply = reader.readline()
            name = command[0]
            if reply.startswith(b"-"):
                error = reply[1:].decode("utf-8", "replace").strip()
                kind = {"AUTH": "auth", "SELECT": "select"}.get(name, "ping")
                raise OSError(f"redis_{kind}_rejected:{error[:120]}")
            if name in ("AUTH", "SELECT") and not reply.startswith(b"+OK"):
                raise OSError("redis_%s_rejected" % name.lower())
            if name == "PING" and not reply.startswith(b"+PONG"):
                raise OSError("redis_ping_failed")
    finally:
        sock.close()
    return {
        "configured": True,
        "ok": True,
        "detail": "Redis 认证与 PING 均正常",
        "tls": target["tls"],
        "auth": bool(target["username"] or target["password"]),
        "db": target["db"],
    }


def _check_redis() -> dict:
    """Redis 就绪必须做带认证的协议检查，不能把「TCP 端口能连」当成可用。"""
    url = os.environ.get("REDIS_URL", "").strip()
    if not url:
        return {"configured": False, "ok": True, "detail": "未配置 Redis，使用本进程任务队列"}
    try:
        target = _redis_target(url)
    except ValueError:
        return {"configured": True, "ok": False, "detail": "REDIS_URL 格式无效"}
    try:
        return _redis_ping(target)
    except OSError as exc:
        health_log.warning("Redis is not ready: %s", type(exc).__name__)
        reason = (str(exc) or type(exc).__name__).lower()
        if "auth" in reason:
            detail = "Redis 认证失败，请检查 REDIS_URL 中的密码或 ACL 用户名"
        elif "select" in reason:
            detail = "Redis 数据库编号不可用，请检查 REDIS_URL 路径"
        elif "ssl" in reason or "certificate" in reason:
            detail = "Redis TLS 握手失败，请检查 rediss:// 证书配置"
        else:
            detail = "Redis 不可连接或 PING 失败，AI 任务无法排队"
        return {
            "configured": True, "ok": False, "detail": detail,
            "ai_queue_available": False, "tls": target["tls"],
        }


def _check_worker(redis_configured: bool) -> dict:
    if not redis_configured:
        return {"configured": False, "ok": True, "detail": "本进程直接执行任务"}
    try:
        with db.connection() as con:
            row = con.execute(
                "SELECT beat_at FROM service_heartbeat WHERE name='worker'"
            ).fetchone()
    except Exception:
        row = None
    if not row or not row["beat_at"]:
        return {"configured": True, "ok": False, "detail": "尚未收到 Worker 心跳"}
    try:
        beat = datetime.fromisoformat(row["beat_at"])
    except ValueError:
        return {"configured": True, "ok": False, "detail": "Worker 心跳时间无法解析"}
    if beat.tzinfo is None:
        beat = beat.replace(tzinfo=timezone.utc)
    age = max(0, int((datetime.now(timezone.utc) - beat).total_seconds()))
    ok = age <= 90
    if not ok:
        health_log.warning("Worker heartbeat is stale: %s seconds", age)
    return {
        "configured": True, "ok": ok, "age_seconds": age,
        "detail": f"Worker 最近心跳 {age} 秒前" + ("" if ok else "，可能已停止"),
    }


@app.get("/api/health/live")
def health_live():
    """存活检查：进程能响应即可，不检查外部依赖。"""
    return {
        "status": "ok", "version": VERSION,
        "build_id": BUILD_ID, "version_label": BUILD_LABEL,
        "bank_version": db.bank_version_hint(),
        "bank_source": db.BANK_SOURCE,
    }


@app.get("/api/health/ready")
def health_ready(request: Request):
    """就绪检查：题库版本、应用库、Redis、Worker 心跳与对象存储逐个核对。

    未登录/非管理员（且没有携带 ``X-Health-Token``）只返回精简结构：
    各依赖 ``ok`` 布尔值、受影响功能与整体可用性；绝对路径、精确题量、
    Redis/Worker 诊断等细节仅对管理员与部署探针口令开放。
    """
    checks = {
        "app_db": _check_app_db(),
        "bank": _check_bank(),
        "storage": _check_storage(),
    }
    checks["redis"] = _check_redis()
    checks["worker"] = _check_worker(bool(checks["redis"].get("configured")))
    degraded = [
        name for name, result in checks.items()
        if result.get("configured", True) and not result.get("ok")
    ]
    affected = sorted({feature for name in degraded for feature in AFFECTED_FEATURES.get(name, [])})
    if degraded:
        health_log.warning("Readiness degraded: %s", ", ".join(degraded))
    # 「基本学习可用」与「AI 队列可用」分开：Redis/Worker 异常不应阻止读题和提交作答。
    ai_ready = checks["redis"].get("ok", True) and checks["worker"].get("ok", True)
    study_ready = checks["app_db"].get("ok") and checks["bank"].get("ok") and checks["storage"].get("ok")
    payload = {
        "status": "ok" if not degraded else "degraded",
        "version": VERSION,
        "build_id": BUILD_ID,
        "study_ready": bool(study_ready),
        "ai_ready": bool(ai_ready),
        "degraded": degraded,
        "affected_features": affected,
        "checks": checks,
    }
    if _ready_detail_allowed(request):
        payload.update({
            "version_label": BUILD_LABEL,
            "bank_version": checks["bank"].get("bank_version"),
            "bank_source": checks["bank"].get("source"),
            "generated_at": db.utcnow(),
        })
        try:
            from .ai import embedding_status
            payload["embedding"] = embedding_status(None)
        except Exception:  # noqa: BLE001 - Embedding 状态是增强信息，不影响就绪
            payload["embedding"] = {"configured": False, "ready": False, "source": "unavailable"}
    else:
        # 匿名视图：只保留每个依赖的可用布尔值，不暴露宿主机目录/题量/Redis 细节。
        payload["checks"] = {
            name: {"ok": bool(result.get("ok"))} for name, result in checks.items()
        }
    return payload


@app.get("/api/admin/overview")
def admin_overview(user=Depends(auth.require_admin)):
    with db.connection() as con:
        users = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        tables = db.table_names(con)
        mapping = {"attempts": "attempts", "ai_jobs": "ai_jobs", "documents": "ai_documents", "notes": "ai_notes"}
        counts = {label: con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                  for label, table in mapping.items() if table in tables}
    return {"users": users, **counts, "source_readonly": True}


@app.post("/api/admin/backup")
def admin_backup(user=Depends(auth.require_admin)):
    from pathlib import Path
    from . import ops
    try:
        info = ops.create_backup()
    except ops.ExternalStorageBackupRequired as exc:
        raise HTTPException(409, str(exc)) from exc
    verified = ops.verify_backup(Path(info["path"]))
    return {**info, "verified": verified}


@app.get("/api/admin/eval-runs")
def admin_eval_runs(user=Depends(auth.require_admin)):
    import json
    with db.connection() as con:
        rows = con.execute("SELECT * FROM eval_runs ORDER BY id DESC LIMIT 20").fetchall()
    return {
        "items": [
            {
                "id": r["id"], "name": r["name"], "model": r["model"],
                "verify_model": r["verify_model"], "prompt_version": r["prompt_version"],
                "total": r["total"], "metrics": json.loads(r["metrics_json"]), "created_at": r["created_at"],
            }
            for r in rows
        ]
    }


from . import ai, practice  # noqa: E402


@app.get("/api/bank-image", include_in_schema=False)
def bank_image_with_etag(
    request: Request,
    subject: str,
    file: str,
    qid: str | None = Query(None, max_length=300),
    asset_id: str | None = Query(None, max_length=80),
    bank_version: str | None = Query(None, max_length=120),
    preview: bool = False,
    user=Depends(auth.current_user),
):
    """bank-image 的缓存再验证入口：复用原路由的全部鉴权，再补 ETag/304。

    原路由在 practice.py（本轮不改）：这里以同路径路由先行注册，调用原实现完成
    逐题素材授权后，按文件 mtime+size 生成 ETag；命中 If-None-Match 时返回 304，
    浏览器复用本地题图，单题加载不再重复下载。
    """
    response = practice.bank_image(
        subject=subject, file=file, qid=qid, asset_id=asset_id,
        bank_version=bank_version, preview=preview, user=user,
    )
    etag = _etag_for_response(response)
    if not etag:
        return response
    response.headers["ETag"] = etag
    if _etag_matches(request.headers.get("if-none-match"), etag):
        return Response(
            status_code=304,
            headers={
                "ETag": etag,
                "Cache-Control": response.headers.get("cache-control", "private, max-age=3600"),
                "Vary": response.headers.get("vary", "Cookie"),
            },
        )
    return response


app.include_router(practice.router)
app.include_router(ai.router)
app.include_router(admission.router)

app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


@app.get("/", include_in_schema=False)
def index():
    path = WEB_DIR / "index.html"
    if not path.is_file():
        raise HTTPException(503, "页面正在准备")
    return FileResponse(path)
