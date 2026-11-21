"""个人题库配图资产登记与所有权校验（P0「题图需要独立的所有权校验」）。

背景：旧实现把内容寻址文件名（``<sha256 前 32 位>.png``）直接写进
``pl_questions.content_json.images[i].asset``，读图接口只校验题目归属，附件本身
没有所有者/用途登记——客户端若在 content 里塞入他人的文件名就能借自己的题目读走图片。

本模块引入服务端签发的资产记录（``pl_assets``）：

- ``asset_id``：服务端随机签发、不可猜的 token，新配图在 content 中只保存它；
- ``user_id`` / ``folder`` / ``storage_ref`` / ``media_type`` / ``sha256`` / ``bytes`` /
  ``purpose`` / ``status`` / ``public_derivative``：归属、用途与可审计元数据；
- 读图必须同时通过：题目属于当前用户且未删除（由调用方 ``fetch_question`` 保证）、
  资产存在且可用、资产属于当前用户（或已标记 ``public_derivative`` 且已发布）；
  任何失败统一 404，不泄露他人资产是否存在；
- 旧数据里的原始文件名引用走 ``migrate_legacy_assets``（或导入入库时的
  ``adopt_reference``）确认归属后登记为资产行，登记后仍可按原文件名读取；
- ``public_derivative`` 为后续「平台精选」预留，默认 0（不实现广场）。
"""
from __future__ import annotations

import hashlib
import re
import secrets

from fastapi import HTTPException

from .. import storage
from ..db import connection, utcnow, write_lock
from .db import ASSET_FOLDER, json_load

# 资产状态：active=本人可用；published=已发布（配合 public_derivative 供他人读）；disabled=失效
ASSET_STATUSES = ("active", "published", "disabled")
ASSET_PURPOSES = ("question_image", "import_crop", "annotation_crop", "legacy_migration")

# 允许的引用形态：不可猜 token（pl_xxx）或旧的内容寻址文件名（hash.png），
# 一律不得含路径分隔符、冒号、上跳目录（storage.local_path 会再次校验路径）。
_ASSET_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,180}$")
_TOKEN_BYTES = 20

_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def new_asset_id() -> str:
    """生成服务端资产 ID：随机不可猜，同时用作新配图的存储文件名。"""
    return "pl_" + secrets.token_hex(_TOKEN_BYTES)


def normalize_asset_ref(value) -> str:
    """把 content 里的配图引用规整为安全 token；非法（含路径/空白）返回空串。"""
    ref = str(value or "").strip()
    if not ref or not _ASSET_REF_RE.match(ref):
        return ""
    if ".." in ref:
        return ""
    return ref


def _row_dict(row) -> dict:
    if row is None:
        return {}
    if isinstance(row, dict):
        return row
    try:
        return dict(row)
    except Exception:  # noqa: BLE001 - 行对象不可解析时按不存在处理
        return {}


def _media_type_for(storage_ref: str, fallback: str = "image/png") -> str:
    lower = str(storage_ref or "").lower()
    for suffix, media_type in _MEDIA_TYPES.items():
        if lower.endswith(suffix):
            return media_type
    return fallback


def _file_bytes(folder: str, storage_ref: str):
    """读取存储中的资产字节；不存在/不可读返回 None（迁移与登记用）。"""
    try:
        return storage.read_file(folder, storage_ref)
    except HTTPException:
        return None
    except OSError:
        return None


# ---------------------------------------------------------------------------
# 登记 / 查询
# ---------------------------------------------------------------------------

def register_asset(user_id: int, *, storage_ref: str, media_type: str = "image/png",
                   purpose: str = "question_image", folder: str = ASSET_FOLDER,
                   sha256: str = "", size: int = 0, question_id: int | None = None,
                   status: str = "active", public_derivative: bool = False,
                   asset_id: str | None = None, con=None) -> dict:
    """登记一条资产记录（文件已存在），返回资产行。

    ``asset_id`` 通常由本函数签发（随机不可猜）；保存新文件时调用方可先取 id 作为
    存储文件名（``save_asset_bytes``），保证本地存储名与 asset_id 一致、不暴露旧命名。
    ``con`` 传入时复用调用方事务（避免 SQLite 嵌套 ``BEGIN IMMEDIATE`` 死锁）。
    """
    if status not in ASSET_STATUSES:
        raise ValueError("未知的资产状态")
    token = normalize_asset_ref(asset_id) or new_asset_id()
    now = utcnow()
    values = (
        token, int(user_id), folder, str(storage_ref), str(media_type or ""),
        str(sha256 or ""), int(size or 0), str(purpose or "question_image"),
        status, 1 if public_derivative else 0,
        int(question_id) if question_id is not None else None, now,
    )
    sql = (
        "INSERT INTO pl_assets(asset_id,user_id,folder,storage_ref,media_type,sha256,"
        "bytes,purpose,status,public_derivative,question_id,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
    )
    if con is not None:
        # 调用方已持有写锁/事务：同连接写入，返回构造行（未提交前其他连接不可见）。
        con.execute(sql, values)
        return {
            "asset_id": token, "user_id": int(user_id), "folder": folder,
            "storage_ref": str(storage_ref), "media_type": str(media_type or ""),
            "sha256": str(sha256 or ""), "bytes": int(size or 0),
            "purpose": str(purpose or "question_image"), "status": status,
            "public_derivative": 1 if public_derivative else 0,
            "question_id": int(question_id) if question_id is not None else None,
            "created_at": now,
        }
    with connection() as own:
        write_lock(own, "paperlab-asset")
        own.execute(sql, values)
    return fetch_asset(token) or {}


def save_asset_bytes(user_id: int, data: bytes, *, media_type: str = "image/png",
                     purpose: str = "question_image", folder: str = ASSET_FOLDER,
                     question_id: int | None = None,
                     public_derivative: bool = False,
                     status: str = "active", con=None) -> str:
    """保存图片字节并登记资产，返回服务端签发的 ``asset_id``（写盘失败不落库）。"""
    raw = bytes(data or b"")
    if not raw:
        raise ValueError("配图内容为空")
    asset_id = new_asset_id()
    digest = hashlib.sha256(raw).hexdigest()
    reference = storage.save_file(folder, asset_id, raw, media_type)
    try:
        register_asset(
            user_id, storage_ref=reference, media_type=media_type, purpose=purpose,
            folder=folder, sha256=digest, size=len(raw), question_id=question_id,
            status=status, public_derivative=public_derivative, asset_id=asset_id, con=con,
        )
    except Exception:
        storage.discard_file(folder, reference)
        raise
    return asset_id


def fetch_asset(asset_id: str) -> dict | None:
    """按服务端 asset_id 取资产行（不做归属判断）。"""
    ref = normalize_asset_ref(asset_id)
    if not ref:
        return None
    with connection() as con:
        row = con.execute("SELECT * FROM pl_assets WHERE asset_id=?", (ref,)).fetchone()
    return _row_dict(row) or None


def find_own_asset_by_ref(user_id: int, storage_ref: str) -> dict | None:
    """按旧存储引用（内容寻址文件名）取当前用户的资产行。"""
    ref = normalize_asset_ref(storage_ref)
    if not ref:
        return None
    with connection() as con:
        row = con.execute(
            "SELECT * FROM pl_assets WHERE user_id=? AND folder=? AND storage_ref=? "
            "ORDER BY id DESC LIMIT 1",
            (int(user_id), ASSET_FOLDER, ref),
        ).fetchone()
    return _row_dict(row) or None


def find_any_asset_by_ref(storage_ref: str) -> dict | None:
    """按旧存储引用取任意用户的资产行（仅用于迁移/公开衍生图判断，读图前须过归属）。"""
    ref = normalize_asset_ref(storage_ref)
    if not ref:
        return None
    with connection() as con:
        row = con.execute(
            "SELECT * FROM pl_assets WHERE folder=? AND storage_ref=? ORDER BY id DESC LIMIT 1",
            (ASSET_FOLDER, ref),
        ).fetchone()
    return _row_dict(row) or None


def asset_is_owned(row: dict, user_id: int) -> bool:
    return bool(row) and int(row.get("user_id") or 0) == int(user_id)


def asset_is_public(row: dict) -> bool:
    """是否已作为「平台精选」衍生图公开（预留给后续广场，不在此实现）。"""
    return (
        bool(row)
        and bool(row.get("public_derivative"))
        and str(row.get("status") or "") == "published"
    )


def resolve_asset_for_read(value, user_id: int | None) -> dict | None:
    """解析 content 里的配图引用并校验可读性；不可读返回 None（调用方统一 404）。

    - 先按 ``asset_id`` 精确查找，再按旧文件名（storage_ref）查找；
    - 资产必须处于 active/published；
    - 本人资产直接可读；他人资产仅在 ``public_derivative`` 且 published 时可读；
      ``user_id`` 为 None 表示调用方已完成上层归属校验（如本人试卷导出），不做二次归属，
      但仍要求资产可用（避免读取已失效记录）。
    """
    row = _resolve_asset_row(value, user_id)
    if not row:
        return None
    if str(row.get("status") or "") not in ("active", "published"):
        return None
    if user_id is None or asset_is_owned(row, user_id) or asset_is_public(row):
        return row
    return None


def _resolve_asset_row(value, user_id: int | None) -> dict | None:
    ref = normalize_asset_ref(value)
    if not ref:
        return None
    row = fetch_asset(ref)
    if row is not None:
        return row
    if user_id is not None:
        own = find_own_asset_by_ref(user_id, ref)
        if own is not None:
            return own
    return find_any_asset_by_ref(ref)


# ---------------------------------------------------------------------------
# 写入校验：content.images[i].asset 必须可归属
# ---------------------------------------------------------------------------

def _image_refs(content) -> list[str]:
    if not isinstance(content, dict):
        return []
    images = content.get("images")
    if not isinstance(images, list):
        return []
    refs: list[str] = []
    for image in images:
        if isinstance(image, dict):
            ref = normalize_asset_ref(image.get("asset"))
            if ref:
                refs.append(ref)
    return refs


def validate_content_image_refs(user_id: int, content, *,
                                existing_content=None) -> None:
    """校验题目 content 里的配图引用（创建/修改题目时调用）。

    规则：
    - 已登记资产：必须属于当前用户（或已公开衍生的他人资产），且状态可用；
    - 未登记的旧文件名：只允许在「编辑旧内容且原样保留该引用」时通过，避免借新内容
      注入他人/未知存储引用；确认归属由 ``migrate_legacy_assets`` 完成；
    - 非法引用（路径、超长、非 token 字符）一律 422。
    """
    refs = _image_refs(content)
    if not refs:
        return
    legacy_allowed = set(_image_refs(existing_content))
    for index, ref in enumerate(refs, start=1):
        row = fetch_asset(ref)
        if row is not None:
            if not asset_is_owned(row, user_id) and not asset_is_public(row):
                raise HTTPException(422, f"第 {index} 张配图引用无效")
            if str(row.get("status") or "") not in ("active", "published"):
                raise HTTPException(422, f"第 {index} 张配图已失效")
            continue
        own = find_own_asset_by_ref(user_id, ref)
        if own is not None:
            if str(own.get("status") or "") in ("active", "published"):
                continue
            raise HTTPException(422, f"第 {index} 张配图已失效")
        other = find_any_asset_by_ref(ref)
        if other is not None:
            if asset_is_public(other):
                continue
            raise HTTPException(422, f"第 {index} 张配图引用无效")
        # 未登记的旧文件名：仅允许原样保留（迁移前编辑旧内容）。
        if ref in legacy_allowed:
            continue
        raise HTTPException(422, f"第 {index} 张配图引用无效")


# ---------------------------------------------------------------------------
# 旧引用登记（导入入库采用 / 迁移）
# ---------------------------------------------------------------------------

def sanitize_draft_images(user_id: int, draft: dict) -> tuple[dict, list[str]]:
    """入库前过滤草稿里的配图引用：只保留当前用户可用资产 / 已公开衍生图。

    返回 ``(draft, warnings)``；无法归属的引用被移除并给出中文提示，不会把他人资产
    写进本人题目（读图侧仍会再校验一次）。
    """
    if not isinstance(draft, dict):
        return draft, []
    images = draft.get("images")
    if not isinstance(images, list) or not images:
        return draft, []
    warnings: list[str] = []
    cleaned: list[dict] = []
    for index, image in enumerate(images):
        if not isinstance(image, dict):
            continue
        updated = dict(image)
        ref = normalize_asset_ref(updated.get("asset"))
        if updated.get("asset") and not ref:
            updated.pop("asset", None)
            warnings.append(f"第 {index + 1} 张配图的引用格式非法，已移除")
        elif ref:
            row = resolve_asset_for_read(ref, int(user_id))
            if row is None:
                updated.pop("asset", None)
                warnings.append(f"第 {index + 1} 张配图的引用无法归属到当前用户，已移除")
            else:
                updated["asset"] = ref
        cleaned.append(updated)
    draft = dict(draft)
    draft["images"] = cleaned
    return draft, warnings


def adopt_reference(user_id: int, value, *, purpose: str = "legacy_migration",
                    question_id: int | None = None,
                    allow_claim: bool = True, con=None) -> str | None:
    """把 content 里的旧文件名引用确认归属并登记，返回可写回 content 的 asset_id。

    - 已是本人在用资产：原样返回其 asset_id；
    - 旧文件名已被本人登记：返回迁移后的 asset_id（content 可平滑升级）；
    - 旧文件名已被他人登记：拒绝（返回 None），不泄露他人资产；
    - 未登记的旧文件名：``allow_claim`` 为真且文件存在时登记到当前用户（迁移/入库采用）。
    ``con`` 传入时复用调用方事务（避免 SQLite 嵌套写锁）。
    """
    ref = normalize_asset_ref(value)
    if not ref:
        return None
    row = fetch_asset(ref)
    if row is not None:
        if asset_is_owned(row, user_id) and str(row.get("status") or "") != "disabled":
            return str(row["asset_id"])
        return None
    own = find_own_asset_by_ref(user_id, ref)
    if own is not None:
        if str(own.get("status") or "") != "disabled":
            return str(own["asset_id"])
        return None
    if not allow_claim:
        return None
    if find_any_asset_by_ref(ref) is not None:
        # 已被其他用户登记：不做「先到先得」式抢占，避免借旧文件名读他人资产。
        return None
    raw = _file_bytes(ASSET_FOLDER, ref)
    if not raw:
        return None
    registered = register_asset(
        user_id, storage_ref=ref, media_type=_media_type_for(ref),
        purpose=purpose, sha256=hashlib.sha256(raw).hexdigest(), size=len(raw),
        question_id=question_id, con=con,
    )
    return str(registered.get("asset_id") or "") or None


# ---------------------------------------------------------------------------
# 迁移：旧 content 里的原始文件名 → pl_assets 登记（不删除、不改写已有数据）
# ---------------------------------------------------------------------------

def migrate_legacy_assets(*, user_id: int | None = None, dry_run: bool = False,
                          limit: int | None = None) -> dict:
    """扫描题目 content 的旧配图引用并登记资产（幂等；默认只扫描不写库）。

    返回报告：``scanned_questions`` / ``references`` / ``migrated`` / ``already`` /
    ``missing`` / ``conflicts`` / ``details``。不修改 content（旧引用登记后即可按原
    文件名读取，新引用一律用 asset_id）。
    """
    where = ["deleted_at IS NULL"]
    params: list = []
    if user_id is not None:
        where.append("user_id=?")
        params.append(int(user_id))
    clause = " AND ".join(where)
    sql = f"SELECT id,user_id,content_json FROM pl_questions WHERE {clause} ORDER BY id"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))
    with connection() as con:
        rows = con.execute(sql, tuple(params)).fetchall()
    report = {
        "dry_run": bool(dry_run),
        "scanned_questions": 0,
        "references": 0,
        "migrated": 0,
        "already": 0,
        "missing": 0,
        "conflicts": [],
        "details": [],
    }
    for row in rows:
        row = _row_dict(row)
        question_id = int(row.get("id") or 0)
        owner_id = int(row.get("user_id") or 0)
        content = json_load(row.get("content_json"), {})
        refs = _image_refs(content)
        if not refs:
            continue
        report["scanned_questions"] += 1
        for ref in refs:
            report["references"] += 1
            detail = {"question_id": question_id, "user_id": owner_id, "ref": ref}
            if fetch_asset(ref) is not None:
                report["already"] += 1
                detail["result"] = "already"
                report["details"].append(detail)
                continue
            own = find_own_asset_by_ref(owner_id, ref)
            if own is not None:
                report["already"] += 1
                detail["result"] = "already"
                detail["asset_id"] = own.get("asset_id")
                report["details"].append(detail)
                continue
            if dry_run:
                if _file_bytes(ASSET_FOLDER, ref) is not None:
                    report["migrated"] += 1
                    detail["result"] = "would_migrate"
                else:
                    report["missing"] += 1
                    detail["result"] = "missing_file"
                report["details"].append(detail)
                continue
            raw = _file_bytes(ASSET_FOLDER, ref)
            if not raw:
                report["missing"] += 1
                detail["result"] = "missing_file"
                report["details"].append(detail)
                continue
            try:
                registered = register_asset(
                    owner_id, storage_ref=ref, media_type=_media_type_for(ref),
                    purpose="legacy_migration",
                    sha256=hashlib.sha256(raw).hexdigest(), size=len(raw),
                    question_id=question_id,
                )
            except Exception as exc:  # noqa: BLE001 - 单条登记失败只记录，不中断迁移
                report["conflicts"].append({
                    "question_id": question_id, "user_id": owner_id, "ref": ref,
                    "error": type(exc).__name__,
                })
                detail["result"] = "conflict"
                report["details"].append(detail)
                continue
            report["migrated"] += 1
            detail["result"] = "migrated"
            detail["asset_id"] = registered.get("asset_id")
            report["details"].append(detail)
    return report


__all__ = [
    "ASSET_STATUSES",
    "ASSET_PURPOSES",
    "adopt_reference",
    "asset_is_owned",
    "asset_is_public",
    "fetch_asset",
    "find_any_asset_by_ref",
    "find_own_asset_by_ref",
    "migrate_legacy_assets",
    "new_asset_id",
    "normalize_asset_ref",
    "register_asset",
    "resolve_asset_for_read",
    "sanitize_draft_images",
    "save_asset_bytes",
    "validate_content_image_refs",
]
