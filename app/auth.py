"""Cookie sessions and password hashing for local multi-user use."""
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import os
import re
import secrets
import time

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from .db import connection, get_setting, set_setting, utcnow, username_predicate, write_lock

router = APIRouter(prefix="/api/auth", tags=["accounts"])
COOKIE = "study_session"
# 进程内桶仅保留给旧调用方清理；真正的限流跨进程落在 auth_rate_events 表。
_failures = defaultdict(deque)
_ip_failures = defaultdict(deque)
_reg_attempts = defaultdict(deque)

RATE_WINDOWS = {
    "register": 600,
    "reset": 300,
    "reset-ip": 300,
    "reset-user": 900,
    "login": 300,
    "login-ip": 300,
    "login-user": 900,
    "change-password": 600,
    "recovery-codes": 600,
}


def _recent(bucket, now, window=300):
    while bucket and bucket[0] < now - window:
        bucket.popleft()
    return bucket


def _rate_key(scope: str, *parts: str) -> str:
    return scope + ":" + ":".join(str(part).casefold() for part in parts)


def _rate_reserve(scope: str, bucket: str, limit: int) -> str:
    """原子预约一次认证尝试：计数与写入在同一把跨进程写锁内完成。

    旧实现先查计数、失败后再记录，并发请求会一起通过检查（实测 10 次限制下进入
    12 个密码核验）。现在把「检查 + 占用」放进同一事务，超限直接 429；
    成功后调用 `_rate_clear` 释放本次账号维度的占用。
    """
    window = RATE_WINDOWS.get(scope, 300)
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(seconds=window)).isoformat()
    with connection() as con:
        write_lock(con, "auth-rate")
        con.execute("DELETE FROM auth_rate_events WHERE at < ?",
                    ((now - timedelta(days=1)).isoformat(),))
        count = con.execute(
            "SELECT COUNT(*) FROM auth_rate_events WHERE bucket=? AND at>=?", (bucket, cutoff)
        ).fetchone()[0]
        if count >= limit:
            minutes = max(1, window // 60)
            raise HTTPException(429, f"尝试次数过多，请 {minutes} 分钟后再试")
        con.execute(
            "INSERT INTO auth_rate_events(bucket,kind,at) VALUES (?,?,?)",
            (bucket, scope, now.isoformat()),
        )
    return now.isoformat()


def _rate_guard(scope: str, bucket: str, limit: int):
    """只检查不占用（用于 IP 维度等辅助阈值）。"""
    window = RATE_WINDOWS.get(scope, 300)
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=window)).isoformat()
    with connection() as con:
        count = con.execute(
            "SELECT COUNT(*) FROM auth_rate_events WHERE bucket=? AND at>=?", (bucket, cutoff)
        ).fetchone()[0]
    if count >= limit:
        minutes = max(1, window // 60)
        raise HTTPException(429, f"尝试次数过多，请 {minutes} 分钟后再试")


def _rate_record(scope: str, bucket: str):
    now = datetime.now(timezone.utc)
    with connection() as con:
        write_lock(con, "auth-rate")
        # 清理一天前的记录，避免限流表无限增长。
        con.execute("DELETE FROM auth_rate_events WHERE at < ?", ((now - timedelta(days=1)).isoformat(),))
        con.execute(
            "INSERT INTO auth_rate_events(bucket,kind,at) VALUES (?,?,?)",
            (bucket, scope, now.isoformat()),
        )


def _rate_clear(bucket: str):
    with connection() as con:
        con.execute("DELETE FROM auth_rate_events WHERE bucket=?", (bucket,))


def _public_mode() -> bool:
    return os.environ.get("STUDY_PUBLIC_MODE", "").strip().lower() in {"1", "true", "yes", "on"}


def registration_mode() -> str:
    """注册开关：数据库设置优先，其次环境变量；公网模式默认关闭注册。"""
    stored = get_setting("registration_mode")
    if stored in ("open", "closed"):
        return stored
    env = os.environ.get("STUDY_REGISTRATION_MODE", "").strip().lower()
    if env in ("open", "closed"):
        return env
    return "closed" if _public_mode() else "open"


def _admin_exists() -> bool:
    try:
        with connection() as con:
            return con.execute("SELECT 1 FROM users WHERE role='admin' LIMIT 1").fetchone() is not None
    except Exception:
        return False


def _admin_init_token() -> str:
    return os.environ.get("STUDY_ADMIN_INIT_TOKEN", "").strip()


class Credentials(BaseModel):
    username: str = Field(min_length=2, max_length=40)
    password: str = Field(min_length=8, max_length=128)


class AdminInitInput(Credentials):
    token: str = Field(min_length=8, max_length=200)


def _hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    value = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()
    return f"scrypt${salt}${value}"


# 用户名不存在时也执行一次同成本的 scrypt 核验，避免用响应耗时枚举已注册用户名。
# 在模块导入期算好（约 30–60ms），请求路径上只需一次 _verify。
_TIMING_HASH = _hash(secrets.token_hex(16))


def _verify(password, saved):
    try:
        _, salt, _ = saved.split("$")
        return hmac.compare_digest(_hash(password, salt), saved)
    except (ValueError, TypeError):
        return False


def _public(row):
    return {"id": row["id"], "username": row["username"], "role": row["role"]}


def current_user(request: Request):
    raw = request.cookies.get(COOKIE)
    if not raw:
        raise HTTPException(401, "请先登录")
    digest = hashlib.sha256(raw.encode()).hexdigest()
    with connection() as con:
        row = con.execute(
            "SELECT u.id,u.username,u.role FROM sessions s JOIN users u ON u.id=s.user_id "
            "WHERE s.token_hash=? AND s.expires_at>?", (digest, utcnow())
        ).fetchone()
    if not row:
        raise HTTPException(401, "登录已过期，请重新登录")
    return _public(row)


def require_admin(user=Depends(current_user)):
    if user["role"] != "admin":
        raise HTTPException(403, "需要管理员权限")
    return user


def _session(user, response, request):
    raw = secrets.token_urlsafe(40)
    now = datetime.now(timezone.utc)
    with connection() as con:
        con.execute("DELETE FROM sessions WHERE expires_at<=?", (now.isoformat(),))
        old = request.cookies.get(COOKIE)
        if old:
            con.execute("DELETE FROM sessions WHERE token_hash=?", (hashlib.sha256(old.encode()).hexdigest(),))
        con.execute("INSERT INTO sessions VALUES (?,?,?,?)", (
            hashlib.sha256(raw.encode()).hexdigest(), user["id"],
            (now + timedelta(days=7)).isoformat(), now.isoformat(),
        ))
    response.set_cookie(COOKIE, raw, max_age=7 * 86400, httponly=True, samesite="strict",
                        # 公网模式强制 Secure：即使反向代理/协议判断失误也不允许明文回传会话。
                        secure=True if _public_mode() else request.url.scheme == "https", path="/")


@router.post("/register")
def register(body: Credentials, request: Request, response: Response):
    if registration_mode() == "closed":
        raise HTTPException(403, "注册暂未开放，请联系管理员开通账号或使用管理员初始化入口")
    address = request.client.host if request.client else "local"
    _rate_reserve("register", _rate_key("register", address), 20)
    name = body.username.strip()
    if not re.fullmatch(r"[\w\-\u4e00-\u9fff]{2,40}", name):
        raise HTTPException(422, "用户名限 2—40 位汉字、字母、数字、下划线或短横线")
    # First-account ownership is a single transaction, even if two users register together.
    with connection() as con:
        write_lock(con, "accounts")
        total = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        # 公网模式不接受「首个注册者自动成为管理员」，必须走一次性管理员初始化。
        role = "admin" if total == 0 and not _public_mode() else "student"
        cur = con.execute(
            "INSERT INTO users(username,password_hash,role,created_at) VALUES (?,?,?,?) ON CONFLICT DO NOTHING",
            (name, _hash(body.password), role, utcnow()),
        )
        if not cur.rowcount:
            raise HTTPException(409, "这个用户名已被使用")
        user = {"id": cur.lastrowid, "username": name, "role": role}
    _session(user, response, request)
    return user


@router.post("/init-admin")
def init_admin(body: AdminInitInput, request: Request, response: Response):
    """受控的一次性管理员初始化：需要运营配置的初始化口令，且只能成功一次。"""
    address = request.client.host if request.client else "local"
    key = _rate_key("init-admin", address)
    token = _admin_init_token()
    if not token:
        raise HTTPException(503, "未配置管理员初始化口令（STUDY_ADMIN_INIT_TOKEN），无法初始化")
    # 原子预约：检查与占用在同一跨进程写事务内完成，并发请求不能突破 10 次上限。
    _rate_reserve("reset", key, 10)
    if not hmac.compare_digest(body.token.strip(), token):
        raise HTTPException(403, "管理员初始化口令不正确")
    name = body.username.strip()
    if not re.fullmatch(r"[\w\-\u4e00-\u9fff]{2,40}", name):
        raise HTTPException(422, "用户名限 2—40 位汉字、字母、数字、下划线或短横线")
    with connection() as con:
        write_lock(con, "accounts")
        if con.execute("SELECT COUNT(*) FROM users WHERE role='admin'").fetchone()[0] > 0:
            raise HTTPException(409, "管理员账号已存在，不能重复初始化")
        cur = con.execute(
            "INSERT INTO users(username,password_hash,role,created_at) VALUES (?,?,?,?) ON CONFLICT DO NOTHING",
            (name, _hash(body.password), "admin", utcnow()),
        )
        if not cur.rowcount:
            raise HTTPException(409, "这个用户名已被使用")
        user = {"id": cur.lastrowid, "username": name, "role": "admin"}
    _rate_clear(key)
    _session(user, response, request)
    return {**user, "message": "管理员初始化完成，请及时生成恢复码并保存到离机介质。"}


@router.post("/login")
def login(body: Credentials, request: Request, response: Response):
    name = body.username.strip()
    address = request.client.host if request.client else "local"
    key = _rate_key("login", address, name)
    ip_key = _rate_key("login-ip", address)
    # 用户名维度全局限流：同一账号跨 IP 的暴破总量（15 分钟 20 次）也要受限。
    user_key = _rate_key("login-user", name)
    _rate_reserve("login", key, 10)
    _rate_reserve("login-user", user_key, 20)
    _rate_guard("login-ip", ip_key, 50)
    with connection() as con:
        row = con.execute(f"SELECT * FROM users WHERE {username_predicate(con)}", (name,)).fetchone()
    if row is None:
        # 用户名不存在：跑一次同成本核验，让耗时与「用户存在但密码错误」接近。
        _verify(body.password, _TIMING_HASH)
        password_ok = False
    else:
        password_ok = _verify(body.password, row["password_hash"])
    if row is None or not password_ok:
        # 账号维度已预约占用；IP 维度在失败时补记。
        _rate_record("login-ip", ip_key)
        raise HTTPException(401, "用户名或密码不正确")
    _rate_clear(key)
    _rate_clear(user_key)
    user = _public(row)
    _session(user, response, request)
    return user


@router.get("/me")
def me(user=Depends(current_user)):
    return user


@router.post("/logout")
def logout(request: Request, response: Response):
    raw = request.cookies.get(COOKIE)
    if raw:
        with connection() as con:
            con.execute("DELETE FROM sessions WHERE token_hash=?", (hashlib.sha256(raw.encode()).hexdigest(),))
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}



class ChangePasswordInput(BaseModel):
    current_password: str = Field(min_length=8, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


class ResetPasswordInput(BaseModel):
    username: str = Field(min_length=2, max_length=40)
    recovery_code: str = Field(min_length=8, max_length=64)
    new_password: str = Field(min_length=8, max_length=128)


def _normalize_code(code: str) -> str:
    return re.sub(r"[\s\-]+", "", (code or "").strip().upper())


def _hash_code(user_id: int, raw_code: str) -> str:
    norm = _normalize_code(raw_code)
    return hashlib.sha256(f"{user_id}:{norm}".encode("utf-8")).hexdigest()


@router.post("/change-password")
def change_password(body: ChangePasswordInput, request: Request, response: Response, user=Depends(current_user)):
    # 账号维度原子限流（10 分钟 10 次）：会话被盗后也无法无限次猜当前密码。
    bucket = _rate_key("change-password", user["id"])
    _rate_reserve("change-password", bucket, 10)
    with connection() as con:
        write_lock(con, "account-recovery")
        row = con.execute("SELECT password_hash FROM users WHERE id=?", (user["id"],)).fetchone()
        if not row or not _verify(body.current_password, row["password_hash"]):
            raise HTTPException(401, "当前密码不正确")
        con.execute("UPDATE users SET password_hash=? WHERE id=?", (_hash(body.new_password), user["id"]))
        con.execute("DELETE FROM sessions WHERE user_id=?", (user["id"],))
    _rate_clear(bucket)
    _session(user, response, request)
    return {"ok": True, "message": "密码已修改，其他设备的登录会话已失效"}


class RecoveryCodesInput(BaseModel):
    current_password: str = Field(min_length=8, max_length=128)


@router.post("/recovery-codes")
def generate_recovery_codes(body: RecoveryCodesInput, user=Depends(current_user)):
    """Generate 8 single-use recovery codes (plaintext shown once, only hashes stored).

    恢复码可直接重置密码，属于高价值凭证：生成前必须用当前密码二次校验，
    并做账号维度原子限流（10 分钟 10 次），避免浏览器会话被临时占用时批量导出恢复码。
    """
    bucket = _rate_key("recovery-codes", user["id"])
    _rate_reserve("recovery-codes", bucket, 10)
    with connection() as con:
        row = con.execute("SELECT password_hash FROM users WHERE id=?", (user["id"],)).fetchone()
    if not row or not _verify(body.current_password, row["password_hash"]):
        raise HTTPException(401, "当前密码不正确，无法生成恢复码")
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    codes = []
    for _ in range(8):
        raw = "".join(secrets.choice(alphabet) for _ in range(12))
        codes.append(f"{raw[:4]}-{raw[4:8]}-{raw[8:]}")
    now = utcnow()
    with connection() as con:
        write_lock(con, "account-recovery")
        con.execute("DELETE FROM recovery_codes WHERE user_id=? AND used_at IS NULL", (user["id"],))
        for c in codes:
            con.execute(
                "INSERT INTO recovery_codes(user_id,code_hash,created_at) VALUES (?,?,?)",
                (user["id"], _hash_code(user["id"], c), now),
            )
    _rate_clear(bucket)
    return {
        "codes": codes,
        "notice": "请妥善保存这 8 组一次性恢复码，页面关闭后无法再次查看；忘记密码时可用任意一组重置密码。",
    }


@router.post("/reset-password")
def reset_password(body: ResetPasswordInput, request: Request):
    name = body.username.strip()
    address = request.client.host if request.client else "local"
    key = _rate_key("reset", address, name)
    ip_key = _rate_key("reset-ip", address)
    # 用户名维度全局限流：同一账号跨 IP 的恢复码尝试总量（15 分钟 20 次）受限。
    user_key = _rate_key("reset-user", name)
    _rate_reserve("reset", key, 10)
    _rate_reserve("reset-user", user_key, 20)
    _rate_guard("reset-ip", ip_key, 50)
    # 用户名不存在时也做一次同成本 scrypt：成功路径本就要为新密码做 scrypt，
    # 失败路径统一补一次假核验，避免用响应耗时枚举已注册账号。
    _verify(body.recovery_code, _TIMING_HASH)
    invalid = False
    with connection() as con:
        write_lock(con, "account-recovery")
        user_row = con.execute(f"SELECT id FROM users WHERE {username_predicate(con)}", (name,)).fetchone()
        uid = user_row["id"] if user_row else None
        code_id = None
        if uid is not None:
            digest = _hash_code(uid, body.recovery_code)
            code_row = con.execute(
                "SELECT id FROM recovery_codes WHERE user_id=? AND code_hash=? AND used_at IS NULL",
                (uid, digest),
            ).fetchone()
            code_id = code_row["id"] if code_row else None
        if uid is None or code_id is None:
            invalid = True
        else:
            con.execute("UPDATE recovery_codes SET used_at=? WHERE id=?", (utcnow(), code_id))
            con.execute("UPDATE users SET password_hash=? WHERE id=?", (_hash(body.new_password), uid))
            con.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
    if invalid:
        # 账号维度已预约占用；IP 维度在失败时补记。
        _rate_record("reset-ip", ip_key)
        raise HTTPException(401, "用户名或恢复码无效")
    _rate_clear(key)
    _rate_clear(user_key)
    return {"ok": True, "message": "密码已通过恢复码重置，请使用新密码登录"}


# ---------------------------------------------------------------------------
# P2: 公网账号策略（注册开关、管理员初始化状态）
# ---------------------------------------------------------------------------

@router.get("/registration-policy")
def registration_policy():
    """登录页读取：注册是否开放、是否还能做一次性管理员初始化。"""
    admin_ready = _admin_exists()
    return {
        "registration_open": registration_mode() == "open",
        "public_mode": _public_mode(),
        "admin_ready": admin_ready,
        "admin_init_available": bool(_admin_init_token()) and not admin_ready,
        "notice": (
            "首次部署请使用管理员初始化口令创建管理员账号；注册开放后新账号默认为学生。"
            if _public_mode() else ""
        ),
    }


class RegistrationInput(BaseModel):
    mode: str = Field(pattern="^(open|closed)$")


@router.get("/admin/registration")
def read_registration(user=Depends(require_admin)):
    return {
        "mode": registration_mode(),
        "public_mode": _public_mode(),
        "admin_ready": _admin_exists(),
        "admin_init_configured": bool(_admin_init_token()),
        "env_override": bool(os.environ.get("STUDY_REGISTRATION_MODE", "").strip()),
    }


@router.post("/admin/registration")
def update_registration(data: RegistrationInput, user=Depends(require_admin)):
    if data.mode == "open" and not _admin_exists():
        raise HTTPException(409, "请先完成管理员初始化，再开放注册")
    set_setting("registration_mode", data.mode)
    return {"mode": data.mode, "updated_at": utcnow()}
