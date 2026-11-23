"""学习附件（learning artifact）共用模块（方案 §4「学习附件」/§5.4 导出与权限/§8 工单 C）。

题目、笔记、AI 课堂、复习卡与 AI 助手共用同一份**带版本的学习附件**：函数图、解题白板、
阅读摘录、流程动画、等距结构、科学演示与 3D 演示都以「公开的 ``artifact_id`` + 整数
``revision``」被引用。模块只存受校验的 JSON 数据与 JSON 预览，不存、不执行、不输出 HTML/脚本；
预览与导出由消费方按版本自行渲染。

数据语义
--------

* **归属**：每张附件只属于创建它的用户；他人读取、改版、关联、删除一律 404，不泄露存在性。
* **版本不可变**：每次内容变化都新增一条 ``study_artifact_revisions``（``current_revision``
  加一），历史版本原样保留；只改标题不产生新版本。
* **关联版本**：``study_artifact_links.revision`` 为 ``NULL`` 表示跟随最新版（个人笔记等），
  为整数表示固定版本（课堂发布、导出与冻结回放）；重链同一目标只更新 ``revision``。
* **软删除**：``deleted_at`` 置位后附件读取 404、列表与按目标查询排除，但历史版本与关联记录
  保留（备份、审计与已冻结引用仍可核对）。
* **发布状态**：``visibility`` 默认 ``private``，``published_revision`` 预留给课堂/导出锁定
  公开版本的接线方；本模块暂无发布接口。

限额与错误口径（全部中文 detail）
--------------------------------

* ``data`` 序列化后 ≤ 256 KiB、``preview`` ≤ 64 KiB → 413；
* 标题 ≤ 120 字符、单个附件 ≤ 20 条关联、``kind`` / ``target_kind`` 白名单、``artifact_id``
  格式 → 422；
* 附件不存在或不属于当前用户 → 404；重复 ``artifact_id`` 与 ``expected_revision`` CAS
  失败 → 409。

存储约束：只通过 ``app.db`` 的 ``connection()`` 访问应用库（不缓存 ``DATA_DIR``），时间为
``db.utcnow()``，JSON 列存 TEXT，SQL 占位符统一 ``?``，不使用 SQLite 专有函数/PRAGMA/
RETURNING，因此 SQLite 与 PostgreSQL 行为一致；所有写路径先取 ``db.write_lock(con,
"study-artifact")`` 串行化竞争写。

接线方式：应用启动时调用 :func:`init_study_artifacts`，并把 :data:`router` 挂到主应用
（``app.include_router(study_artifact.router)``）；题目/笔记/课堂/复习通过本模块公开函数
读写，不直接操作表。
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import re
import secrets
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from . import db
from .auth import current_user

router = APIRouter(prefix="/api/artifacts", tags=["Study artifacts"])

#: 附件类型白名单（与方案 §4「首批函数图、解题白板、阅读摘录、流程动画、等距结构及科学演示」一致）。
ARTIFACT_KINDS = (
    "function_graph",
    "whiteboard",
    "reading_excerpt",
    "flow_animation",
    "isometric",
    "science_demo",
    "demo_3d",
    # M5：机械/工厂演示模板库（app/demo_library.py）产出的机械 3D 与工厂仿真数据。
    "mech_3d",
    "factory_sim",
    # 批次五（§6.1）：JSXGraph 交互几何（预设编号 + 数值参数）。
    "geometry_demo",
    # 批次五（§6.2）：3Dmol 分子模型（SMILES + 显示样式）。
    "molecule_3d",
    # 批次五（§6.3）：Matter.js 物理自由沙盒（探索模式）。
    "physics_sandbox",
)
#: 附件可关联的目标类型白名单。
TARGET_KINDS = ("question", "note", "classroom", "review", "assistant")

MAX_DATA_BYTES = 256 * 1024
MAX_PREVIEW_BYTES = 64 * 1024
MAX_TITLE_LENGTH = 120
MAX_LINKS_PER_ARTIFACT = 20
MAX_SOURCE_REF_LENGTH = 500
MAX_SOURCE_KIND_LENGTH = 40
MAX_TARGET_KEY_LENGTH = 300
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100
DEFAULT_VISIBILITY = "private"

#: 调用方可传入的公开附件 ID：小写字母或数字开头，仅含小写字母、数字、下划线、短横线，8–64 位。
ARTIFACT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{7,63}$")

#: 进程内就绪标记（DATA_DIR + 存储引擎）：直接调用本模块函数（未经 lifespan）时懒建表。
_ready_key: tuple[str, bool] | None = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS study_artifacts(
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    artifact_id TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    source_ref TEXT,
    source_kind TEXT,
    current_revision INTEGER NOT NULL DEFAULT 1,
    published_revision INTEGER,
    visibility TEXT NOT NULL DEFAULT 'private',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT);
CREATE TABLE IF NOT EXISTS study_artifact_revisions(
    id INTEGER PRIMARY KEY,
    artifact_id INTEGER NOT NULL,
    revision INTEGER NOT NULL,
    data_json TEXT NOT NULL,
    preview_json TEXT,
    checksum TEXT NOT NULL,
    author_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(artifact_id, revision));
CREATE TABLE IF NOT EXISTS study_artifact_links(
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    artifact_id INTEGER NOT NULL,
    target_kind TEXT NOT NULL,
    target_key TEXT NOT NULL,
    revision INTEGER,
    created_at TEXT NOT NULL,
    UNIQUE(user_id, artifact_id, target_kind, target_key));
CREATE INDEX IF NOT EXISTS idx_study_artifacts_user
    ON study_artifacts(user_id, updated_at);
CREATE INDEX IF NOT EXISTS idx_study_artifacts_kind
    ON study_artifacts(user_id, kind, updated_at);
CREATE INDEX IF NOT EXISTS idx_study_artifact_links_target
    ON study_artifact_links(user_id, target_kind, target_key);
"""


# ---------------------------------------------------------------------------
# 建表 / 初始化
# ---------------------------------------------------------------------------


def _schema_key() -> tuple[str, bool]:
    return (str(db.DATA_DIR), db.postgres_enabled())


def init_study_artifacts() -> None:
    """应用启动时调用：幂等创建学习附件三张表与索引（SQLite / PostgreSQL 同一套 DDL）。"""
    global _ready_key
    with db.connection() as con:
        db.write_lock(con, "study-artifact")
        con.executescript(_SCHEMA)
    _ready_key = _schema_key()


def _ensure_ready() -> None:
    """懒初始化：未经 lifespan 直接调用本模块函数时也保证表存在（建表仍只在本模块 init 完成）。"""
    if _ready_key != _schema_key():
        init_study_artifacts()


# ---------------------------------------------------------------------------
# 连接与写锁
# ---------------------------------------------------------------------------


def _lock_write(con) -> None:
    """在写事务内取跨进程命名写锁；外层事务已持锁时（SQLite 不可嵌套 BEGIN IMMEDIATE）复用。"""
    if getattr(con, "in_transaction", False):
        return
    db.write_lock(con, "study-artifact")


@contextmanager
def _write_connection(con=None):
    """取得写连接：``con`` 为空时自建并提交，否则复用调用方事务（由调用方负责提交）。"""
    if con is not None:
        _lock_write(con)
        yield con
        return
    with db.connection() as own:
        _lock_write(own)
        yield own


@contextmanager
def _read_connection(con=None):
    """取得读连接：``con`` 为空时自建只读连接，否则复用调用方连接。"""
    if con is not None:
        yield con
        return
    with db.connection() as own:
        yield own


# ---------------------------------------------------------------------------
# 校验与序列化
# ---------------------------------------------------------------------------


def _validate_user_id(user_id) -> int:
    if isinstance(user_id, bool):
        raise HTTPException(422, "用户标识不正确")
    try:
        value = int(user_id)
    except (TypeError, ValueError):
        raise HTTPException(422, "用户标识不正确") from None
    if value < 1:
        raise HTTPException(422, "用户标识不正确")
    return value


def _validate_kind(kind) -> str:
    value = "" if kind is None else str(kind).strip()
    if value not in ARTIFACT_KINDS:
        raise HTTPException(422, "附件类型不正确，可选：" + "、".join(ARTIFACT_KINDS))
    return value


def _validate_target_kind(target_kind) -> str:
    value = "" if target_kind is None else str(target_kind).strip()
    if value not in TARGET_KINDS:
        raise HTTPException(422, "关联目标类型不正确，可选：" + "、".join(TARGET_KINDS))
    return value


def _validate_target_key(target_key) -> str:
    if not isinstance(target_key, str):
        raise HTTPException(422, "关联目标键不正确")
    value = target_key.strip()
    if not value:
        raise HTTPException(422, "关联目标键不能为空")
    if len(value) > MAX_TARGET_KEY_LENGTH:
        raise HTTPException(422, "关联目标键过长（最多 300 个字符）")
    return value


def _validate_title(title) -> str:
    if not isinstance(title, str):
        raise HTTPException(422, "标题不正确")
    value = title.strip()
    if not value:
        raise HTTPException(422, "标题不能为空")
    if len(value) > MAX_TITLE_LENGTH:
        raise HTTPException(422, "标题最多 120 个字符")
    return value


def _validate_artifact_id(artifact_id) -> str:
    """校验公开附件 ID；调用方不传时服务端生成 24 位随机十六进制串。"""
    if artifact_id is None:
        return secrets.token_hex(12)
    if not isinstance(artifact_id, str):
        raise HTTPException(422, "附件 ID 不正确")
    value = artifact_id.strip()
    if not ARTIFACT_ID_RE.match(value):
        raise HTTPException(
            422,
            "附件 ID 格式不正确：需以小写字母或数字开头，只含小写字母、数字、下划线、短横线，"
            "长度 8–64 位",
        )
    return value


def _require_artifact_id(artifact_id) -> str:
    if artifact_id is None:
        raise HTTPException(422, "缺少附件 ID")
    return _validate_artifact_id(artifact_id)


def _validate_revision_number(value) -> int:
    if isinstance(value, bool) or value is None:
        raise HTTPException(422, "版本号不正确")
    if isinstance(value, float) and not value.is_integer():
        raise HTTPException(422, "版本号不正确")
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise HTTPException(422, "版本号不正确") from None
    if number < 1:
        raise HTTPException(422, "版本号不正确")
    return number


def _validate_source_ref(source_ref) -> str | None:
    if source_ref is None:
        return None
    if not isinstance(source_ref, str):
        raise HTTPException(422, "来源引用不正确")
    value = source_ref.strip()
    if not value:
        return None
    if len(value) > MAX_SOURCE_REF_LENGTH:
        raise HTTPException(422, "来源引用过长（最多 500 个字符）")
    return value


def _validate_source_kind(source_kind) -> str | None:
    if source_kind is None:
        return None
    if not isinstance(source_kind, str):
        raise HTTPException(422, "来源类型不正确")
    value = source_kind.strip()
    if not value:
        return None
    if len(value) > MAX_SOURCE_KIND_LENGTH:
        raise HTTPException(422, "来源类型过长（最多 40 个字符）")
    return value


def _validate_page(page) -> int:
    if isinstance(page, bool):
        raise HTTPException(422, "页码不正确")
    try:
        value = int(page)
    except (TypeError, ValueError):
        raise HTTPException(422, "页码不正确") from None
    if value < 1:
        raise HTTPException(422, "页码不正确")
    return value


def _validate_page_size(page_size) -> int:
    if isinstance(page_size, bool):
        raise HTTPException(422, "每页数量不正确")
    try:
        value = int(page_size)
    except (TypeError, ValueError):
        raise HTTPException(422, "每页数量不正确") from None
    if value < 1 or value > MAX_PAGE_SIZE:
        raise HTTPException(422, "每页数量需在 1–100 之间")
    return value


def _dump_json(value) -> str:
    """稳定序列化：键排序、紧凑分隔符、不转义中文（校验、checksum 与落库共用同一口径）。

    ``allow_nan=False``：拒绝 NaN / Infinity 等非标准 JSON 数值，避免落库后响应渲染失败。
    """
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
    except (TypeError, ValueError):
        raise HTTPException(422, "附件内容包含无法序列化的数据") from None


def _serialize_data(data) -> str:
    if not isinstance(data, dict):
        raise HTTPException(422, "附件内容 data 必须是 JSON 对象")
    payload = _dump_json(data)
    if len(payload.encode("utf-8")) > MAX_DATA_BYTES:
        raise HTTPException(413, "附件内容超过 256 KiB 上限，请精简后再保存")
    return payload


def _serialize_preview(preview) -> str | None:
    if preview is None:
        return None
    if not isinstance(preview, dict):
        raise HTTPException(422, "附件预览 preview 必须是 JSON 对象")
    payload = _dump_json(preview)
    if len(payload.encode("utf-8")) > MAX_PREVIEW_BYTES:
        raise HTTPException(413, "附件预览超过 64 KiB 上限，请精简后再保存")
    return payload


def _checksum(serialized_data: str) -> str:
    """内容校验和：对稳定序列化后的 UTF-8 字节做 SHA-256。"""
    return hashlib.sha256(serialized_data.encode("utf-8")).hexdigest()


def _normalize_links(links) -> list[tuple[str, str, int | None]]:
    """校验创建时携带的关联列表，返回 ``[(target_kind, target_key, revision)]``。"""
    if links is None:
        return []
    if not isinstance(links, (list, tuple)):
        raise HTTPException(422, "links 必须是数组")
    if len(links) > MAX_LINKS_PER_ARTIFACT:
        raise HTTPException(422, "单个附件最多关联 20 个目标")
    items: list[tuple[str, str, int | None]] = []
    seen: set[tuple[str, str]] = set()
    for entry in links:
        if not isinstance(entry, dict):
            raise HTTPException(422, "links 的每一项必须是对象")
        target_kind = _validate_target_kind(entry.get("target_kind"))
        target_key = _validate_target_key(entry.get("target_key"))
        raw_revision = entry.get("revision")
        revision = None if raw_revision is None else _validate_revision_number(raw_revision)
        pair = (target_kind, target_key)
        if pair in seen:
            raise HTTPException(422, "links 中存在重复的关联目标")
        seen.add(pair)
        items.append((target_kind, target_key, revision))
    return items


def _is_unique_violation(exc: Exception) -> bool:
    """跨引擎判断唯一约束冲突（SQLite: UNIQUE constraint failed；PG: duplicate key value）。"""
    message = str(exc).lower()
    return "unique" in message or "duplicate" in message


# ---------------------------------------------------------------------------
# 行投影
# ---------------------------------------------------------------------------


def _artifact_dict(row) -> dict:
    """附件元数据（不含内容，也不暴露 user_id）。"""
    return {
        "artifact_id": str(row["artifact_id"]),
        "kind": str(row["kind"]),
        "title": str(row["title"]),
        "source_ref": row["source_ref"],
        "source_kind": row["source_kind"],
        "current_revision": int(row["current_revision"]),
        "published_revision": (
            None if row["published_revision"] is None else int(row["published_revision"])
        ),
        "visibility": str(row["visibility"] or DEFAULT_VISIBILITY),
        "created_at": str(row["created_at"] or ""),
        "updated_at": str(row["updated_at"] or ""),
    }


def preview_public(artifact) -> dict:
    """跨页面引用的公开投影：只暴露最小元数据，不暴露归属与内容。

    供题目、笔记、课堂、复习与导出在只读场景展示附件卡片；不含 ``user_id``、``data``、
    ``preview`` 等私有内容，调用方按 ``artifact_id + revision`` 再走授权读取。
    """
    if artifact is None:
        return {}
    if not isinstance(artifact, dict):
        artifact = {key: artifact[key] for key in artifact.keys()}
    return {
        "artifact_id": artifact.get("artifact_id"),
        "kind": artifact.get("kind"),
        "title": artifact.get("title"),
        "current_revision": artifact.get("current_revision"),
        "updated_at": artifact.get("updated_at"),
        "source_ref": artifact.get("source_ref"),
    }


def _parse_json(text, default):
    if text is None or text == "":
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


def _revision_dict(row) -> dict:
    """单个版本：解析后的 data/preview + checksum + 作者与时间，供授权读取与跨页读取。"""
    return {
        "revision": int(row["revision"]),
        "data": _parse_json(row["data_json"], {}),
        "preview": _parse_json(row["preview_json"], None),
        "checksum": str(row["checksum"] or ""),
        "created_at": str(row["created_at"] or ""),
        "author_id": int(row["author_id"]),
    }


def _link_dict(row, public_artifact_id: str) -> dict:
    """关联投影：``revision=None`` 即跟随最新版，整数即固定版本。"""
    return {
        "artifact_id": public_artifact_id,
        "target_kind": str(row["target_kind"]),
        "target_key": str(row["target_key"]),
        "revision": None if row["revision"] is None else int(row["revision"]),
        "created_at": str(row["created_at"] or ""),
    }


# ---------------------------------------------------------------------------
# 数据库读取 / 写入基础件
# ---------------------------------------------------------------------------


def _require_owned_artifact(con, user_id: int, public_artifact_id: str):
    """读取当前用户未删除的附件行；不存在、已删除或属于他人都返回同一个 404。"""
    row = con.execute(
        "SELECT * FROM study_artifacts WHERE artifact_id=? AND user_id=? AND deleted_at IS NULL",
        (public_artifact_id, user_id),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "附件不存在或无权访问")
    return row


def _artifact_row_by_pk(con, artifact_pk: int):
    row = con.execute("SELECT * FROM study_artifacts WHERE id=?", (artifact_pk,)).fetchone()
    if row is None:  # pragma: no cover - 事务内刚写入或已校验存在
        raise HTTPException(404, "附件不存在或无权访问")
    return row


def _revision_row(con, artifact_pk: int, revision: int):
    return con.execute(
        "SELECT * FROM study_artifact_revisions WHERE artifact_id=? AND revision=?",
        (artifact_pk, revision),
    ).fetchone()


def _link_row(con, user_id: int, artifact_pk: int, target_kind: str, target_key: str):
    return con.execute(
        "SELECT * FROM study_artifact_links WHERE user_id=? AND artifact_id=? "
        "AND target_kind=? AND target_key=?",
        (user_id, artifact_pk, target_kind, target_key),
    ).fetchone()


def _insert_revision(
    con,
    artifact_pk: int,
    revision: int,
    data_json: str,
    preview_json: str | None,
    checksum: str,
    author_id: int,
    created_at: str,
) -> None:
    con.execute(
        "INSERT INTO study_artifact_revisions(artifact_id, revision, data_json, preview_json, "
        "checksum, author_id, created_at) VALUES (?,?,?,?,?,?,?)",
        (artifact_pk, revision, data_json, preview_json, checksum, author_id, created_at),
    )


def _upsert_link(
    con,
    user_id: int,
    artifact_pk: int,
    target_kind: str,
    target_key: str,
    revision: int | None,
    created_at: str,
) -> None:
    """写入或更新关联：同一目标重复关联只更新 ``revision``（不改变创建时间）。"""
    existing = _link_row(con, user_id, artifact_pk, target_kind, target_key)
    if existing is None:
        count = con.execute(
            "SELECT COUNT(*) FROM study_artifact_links WHERE user_id=? AND artifact_id=?",
            (user_id, artifact_pk),
        ).fetchone()[0]
        if int(count) >= MAX_LINKS_PER_ARTIFACT:
            raise HTTPException(422, "单个附件最多关联 20 个目标")
        con.execute(
            "INSERT INTO study_artifact_links(user_id, artifact_id, target_kind, target_key, "
            "revision, created_at) VALUES (?,?,?,?,?,?)",
            (user_id, artifact_pk, target_kind, target_key, revision, created_at),
        )
        return
    con.execute(
        "UPDATE study_artifact_links SET revision=? WHERE id=?",
        (revision, existing["id"]),
    )


# ---------------------------------------------------------------------------
# 公开函数：创建 / 追加版本 / 读取 / 列表
# ---------------------------------------------------------------------------


def create_artifact(
    user_id,
    *,
    kind,
    title,
    data,
    preview=None,
    source_ref=None,
    source_kind=None,
    artifact_id=None,
    links=None,
    con=None,
) -> dict:
    """创建附件与第 1 版内容，返回 ``{artifact, revision}``。

    ``artifact_id`` 可不传（服务端生成 ``secrets.token_hex(12)``）；传入时必须匹配
    ``^[a-z0-9][a-z0-9_-]{7,63}$``，与已有附件（含已软删除）重复时返回 409。
    ``links`` 为 ``[{target_kind, target_key, revision?}]``，``revision`` 省略表示跟随最新版。
    """
    _ensure_ready()
    uid = _validate_user_id(user_id)
    kind_value = _validate_kind(kind)
    title_value = _validate_title(title)
    data_payload = _serialize_data(data)
    preview_payload = _serialize_preview(preview)
    public_id = _validate_artifact_id(artifact_id)
    provided_id = artifact_id is not None
    ref_value = _validate_source_ref(source_ref)
    source_kind_value = _validate_source_kind(source_kind)
    link_items = _normalize_links(links)
    for _target_kind, _target_key, link_revision in link_items:
        if link_revision is not None and link_revision > 1:
            raise HTTPException(422, "关联版本不存在，请使用已有版本号")
    content_checksum = _checksum(data_payload)
    now = db.utcnow()
    with _write_connection(con) as active:
        if provided_id:
            duplicate = active.execute(
                "SELECT 1 FROM study_artifacts WHERE artifact_id=?", (public_id,)
            ).fetchone()
            if duplicate is not None:
                raise HTTPException(409, "附件 ID 已存在，请更换")
        try:
            cursor = active.execute(
                "INSERT INTO study_artifacts(user_id, artifact_id, kind, title, source_ref, "
                "source_kind, current_revision, published_revision, visibility, created_at, "
                "updated_at, deleted_at) VALUES (?,?,?,?,?,?,1,NULL,?,?,?,NULL)",
                (
                    uid,
                    public_id,
                    kind_value,
                    title_value,
                    ref_value,
                    source_kind_value,
                    DEFAULT_VISIBILITY,
                    now,
                    now,
                ),
            )
        except Exception as exc:  # noqa: BLE001 - 仅识别唯一约束竞争
            if _is_unique_violation(exc):
                raise HTTPException(409, "附件 ID 已存在，请更换") from None
            raise
        artifact_pk = cursor.lastrowid
        _insert_revision(
            active, artifact_pk, 1, data_payload, preview_payload, content_checksum, uid, now
        )
        for target_kind, target_key, link_revision in link_items:
            _upsert_link(active, uid, artifact_pk, target_kind, target_key, link_revision, now)
        artifact_row = _artifact_row_by_pk(active, artifact_pk)
        revision_row = _revision_row(active, artifact_pk, 1)
    return {
        "artifact": _artifact_dict(artifact_row),
        "revision": _revision_dict(revision_row),
    }


def add_revision(
    user_id,
    artifact_id,
    *,
    data,
    preview=None,
    title=None,
    expected_revision=None,
    con=None,
) -> dict:
    """追加一个不可变内容版本，返回 ``{artifact, revision}``。

    ``expected_revision`` 为乐观锁：与当前 ``current_revision`` 不一致时返回 409
    「附件已被更新，请刷新后重试」，不发生任何写入。``title`` 省略时保留原标题。
    """
    _ensure_ready()
    uid = _validate_user_id(user_id)
    public_id = _require_artifact_id(artifact_id)
    data_payload = _serialize_data(data)
    preview_payload = _serialize_preview(preview)
    title_value = None if title is None else _validate_title(title)
    expected = (
        None if expected_revision is None else _validate_revision_number(expected_revision)
    )
    content_checksum = _checksum(data_payload)
    now = db.utcnow()
    with _write_connection(con) as active:
        row = _require_owned_artifact(active, uid, public_id)
        current = int(row["current_revision"])
        if expected is not None and expected != current:
            raise HTTPException(409, "附件已被更新，请刷新后重试")
        new_revision = current + 1
        artifact_pk = int(row["id"])
        _insert_revision(
            active,
            artifact_pk,
            new_revision,
            data_payload,
            preview_payload,
            content_checksum,
            uid,
            now,
        )
        active.execute(
            "UPDATE study_artifacts SET current_revision=?, title=?, updated_at=? WHERE id=?",
            (
                new_revision,
                title_value if title_value is not None else row["title"],
                now,
                artifact_pk,
            ),
        )
        artifact_row = _artifact_row_by_pk(active, artifact_pk)
        revision_row = _revision_row(active, artifact_pk, new_revision)
    return {
        "artifact": _artifact_dict(artifact_row),
        "revision": _revision_dict(revision_row),
    }


def _rename_artifact(user_id, artifact_id, title, expected_revision=None, con=None) -> dict:
    """只改标题（不产生新版本）；返回 ``{artifact, revision}``，``revision`` 为当前最新版。"""
    _ensure_ready()
    uid = _validate_user_id(user_id)
    public_id = _require_artifact_id(artifact_id)
    title_value = _validate_title(title)
    expected = (
        None if expected_revision is None else _validate_revision_number(expected_revision)
    )
    now = db.utcnow()
    with _write_connection(con) as active:
        row = _require_owned_artifact(active, uid, public_id)
        current = int(row["current_revision"])
        if expected is not None and expected != current:
            raise HTTPException(409, "附件已被更新，请刷新后重试")
        artifact_pk = int(row["id"])
        active.execute(
            "UPDATE study_artifacts SET title=?, updated_at=? WHERE id=?",
            (title_value, now, artifact_pk),
        )
        artifact_row = _artifact_row_by_pk(active, artifact_pk)
        revision_row = _revision_row(active, artifact_pk, int(artifact_row["current_revision"]))
    if revision_row is None:  # pragma: no cover - 正常数据必有当前版本行
        raise HTTPException(404, "附件不存在或无权访问")
    return {
        "artifact": _artifact_dict(artifact_row),
        "revision": _revision_dict(revision_row),
    }


def get_artifact(user_id, artifact_id, *, revision=None, con=None) -> dict:
    """读取附件指定版本（``revision`` 省略读当前最新版），返回 ``{artifact, revision}``。

    他人附件、已软删除附件与不存在的版本统一 404，不泄露存在性。
    """
    _ensure_ready()
    uid = _validate_user_id(user_id)
    public_id = _require_artifact_id(artifact_id)
    wanted = None if revision is None else _validate_revision_number(revision)
    with _read_connection(con) as active:
        row = _require_owned_artifact(active, uid, public_id)
        artifact_pk = int(row["id"])
        target_revision = int(row["current_revision"]) if wanted is None else wanted
        revision_row = _revision_row(active, artifact_pk, target_revision)
        if revision_row is None:
            raise HTTPException(404, "附件不存在或无权访问")
        artifact_row = row
    return {
        "artifact": _artifact_dict(artifact_row),
        "revision": _revision_dict(revision_row),
    }


def list_artifacts(
    user_id,
    *,
    kind=None,
    target_kind=None,
    target_key=None,
    page=1,
    page_size=DEFAULT_PAGE_SIZE,
    con=None,
) -> dict:
    """分页列出本人未删除附件（只含公开元数据，不含 ``data``），可按类型与关联目标过滤。"""
    _ensure_ready()
    uid = _validate_user_id(user_id)
    kind_value = (
        None if kind is None or str(kind).strip() == "" else _validate_kind(kind)
    )
    target_kind_value = (
        None
        if target_kind is None or str(target_kind).strip() == ""
        else _validate_target_kind(target_kind)
    )
    target_key_value = (
        None if target_key is None or str(target_key).strip() == "" else _validate_target_key(target_key)
    )
    page_value = _validate_page(page)
    size_value = _validate_page_size(page_size)

    conditions = ["a.user_id=?", "a.deleted_at IS NULL"]
    params: list[Any] = [uid]
    if kind_value is not None:
        conditions.append("a.kind=?")
        params.append(kind_value)
    if target_kind_value is not None or target_key_value is not None:
        sub_conditions = ["l.user_id=?", "l.artifact_id=a.id"]
        sub_params: list[Any] = [uid]
        if target_kind_value is not None:
            sub_conditions.append("l.target_kind=?")
            sub_params.append(target_kind_value)
        if target_key_value is not None:
            sub_conditions.append("l.target_key=?")
            sub_params.append(target_key_value)
        conditions.append(
            "EXISTS (SELECT 1 FROM study_artifact_links l WHERE "
            + " AND ".join(sub_conditions)
            + ")"
        )
        params.extend(sub_params)
    where = " AND ".join(conditions)
    offset = (page_value - 1) * size_value
    with _read_connection(con) as active:
        total = int(
            active.execute(
                f"SELECT COUNT(*) FROM study_artifacts a WHERE {where}", tuple(params)
            ).fetchone()[0]
        )
        rows = active.execute(
            f"SELECT a.* FROM study_artifacts a WHERE {where} "
            "ORDER BY a.updated_at DESC, a.id DESC LIMIT ? OFFSET ?",
            (*params, size_value, offset),
        ).fetchall()
    items = [preview_public(_artifact_dict(row)) for row in rows]
    return {"items": items, "total": total, "page": page_value, "page_size": size_value}


def _list_revisions(user_id, artifact_id, *, con=None) -> dict:
    """列出附件全部历史版本（仅元数据：版本号、checksum、时间、作者），最新在前。"""
    _ensure_ready()
    uid = _validate_user_id(user_id)
    public_id = _require_artifact_id(artifact_id)
    with _read_connection(con) as active:
        row = _require_owned_artifact(active, uid, public_id)
        rows = active.execute(
            "SELECT revision, checksum, created_at, author_id FROM study_artifact_revisions "
            "WHERE artifact_id=? ORDER BY revision DESC",
            (int(row["id"]),),
        ).fetchall()
    items = [
        {
            "revision": int(item["revision"]),
            "checksum": str(item["checksum"] or ""),
            "created_at": str(item["created_at"] or ""),
            "author_id": int(item["author_id"]),
        }
        for item in rows
    ]
    return {"items": items, "total": len(items)}


def _soft_delete(user_id, artifact_id, *, con=None) -> dict:
    """软删除附件：置 ``deleted_at``，历史版本与关联记录保留但读取 404。"""
    _ensure_ready()
    uid = _validate_user_id(user_id)
    public_id = _require_artifact_id(artifact_id)
    now = db.utcnow()
    with _write_connection(con) as active:
        row = _require_owned_artifact(active, uid, public_id)
        active.execute(
            "UPDATE study_artifacts SET deleted_at=?, updated_at=? WHERE id=?",
            (now, now, int(row["id"])),
        )
    return {"deleted": True, "artifact_id": public_id}


# ---------------------------------------------------------------------------
# 公开函数：关联目标与按目标读取
# ---------------------------------------------------------------------------


def link_artifact(
    user_id,
    artifact_id,
    target_kind,
    target_key,
    *,
    revision=None,
    con=None,
) -> dict:
    """把附件关联到目标（题目/笔记/课堂/复习/助手），返回 ``{link}``。

    ``revision`` 省略表示跟随最新版；给整数表示固定版本（须为已有版本）。同一目标重链
    只更新版本，不新增记录；单附件关联目标数上限 20。
    """
    _ensure_ready()
    uid = _validate_user_id(user_id)
    public_id = _require_artifact_id(artifact_id)
    target_kind_value = _validate_target_kind(target_kind)
    target_key_value = _validate_target_key(target_key)
    fixed_revision = None if revision is None else _validate_revision_number(revision)
    now = db.utcnow()
    with _write_connection(con) as active:
        row = _require_owned_artifact(active, uid, public_id)
        artifact_pk = int(row["id"])
        if fixed_revision is not None and fixed_revision > int(row["current_revision"]):
            raise HTTPException(422, "关联版本不存在，请使用已有版本号")
        _upsert_link(
            active,
            uid,
            artifact_pk,
            target_kind_value,
            target_key_value,
            fixed_revision,
            now,
        )
        link_row = _link_row(
            active, uid, artifact_pk, target_kind_value, target_key_value
        )
    if link_row is None:  # pragma: no cover - 刚写入或更新
        raise HTTPException(404, "关联不存在")
    return {"link": _link_dict(link_row, public_id)}


def unlink_artifact(user_id, artifact_id, target_kind, target_key, con=None) -> dict:
    """解除附件与某个目标的关联，返回 ``{deleted: true}``；关联不存在返回 404。"""
    _ensure_ready()
    uid = _validate_user_id(user_id)
    public_id = _require_artifact_id(artifact_id)
    target_kind_value = _validate_target_kind(target_kind)
    target_key_value = _validate_target_key(target_key)
    with _write_connection(con) as active:
        row = _require_owned_artifact(active, uid, public_id)
        cursor = active.execute(
            "DELETE FROM study_artifact_links WHERE user_id=? AND artifact_id=? "
            "AND target_kind=? AND target_key=?",
            (uid, int(row["id"]), target_kind_value, target_key_value),
        )
        if int(cursor.rowcount or 0) == 0:
            raise HTTPException(404, "关联不存在")
    return {"deleted": True}


def artifacts_for_target(user_id, target_kind, target_key, *, con=None) -> dict:
    """读取挂到某个目标上的全部附件（按链接版本固定或跟随最新）。

    返回 ``{items: [{artifact, revision, data, preview, link}], total, skipped}``：
    已软删除附件与被固定到不存在版本的关联直接跳过（不产生占位条目），``skipped`` 记录
    被跳过的关联数，便于消费方提示「部分附件已失效」。
    """
    _ensure_ready()
    uid = _validate_user_id(user_id)
    target_kind_value = _validate_target_kind(target_kind)
    target_key_value = _validate_target_key(target_key)
    items = []
    skipped = 0
    with _read_connection(con) as active:
        link_rows = active.execute(
            "SELECT * FROM study_artifact_links WHERE user_id=? AND target_kind=? "
            "AND target_key=? ORDER BY id",
            (uid, target_kind_value, target_key_value),
        ).fetchall()
        for link_row in link_rows:
            artifact_row = active.execute(
                "SELECT * FROM study_artifacts WHERE id=? AND user_id=? AND deleted_at IS NULL",
                (int(link_row["artifact_id"]), uid),
            ).fetchone()
            if artifact_row is None:
                skipped += 1
                continue
            artifact_pk = int(artifact_row["id"])
            target_revision = (
                int(artifact_row["current_revision"])
                if link_row["revision"] is None
                else int(link_row["revision"])
            )
            revision_row = _revision_row(active, artifact_pk, target_revision)
            if revision_row is None:
                skipped += 1
                continue
            revision_payload = _revision_dict(revision_row)
            items.append(
                {
                    "artifact": preview_public(_artifact_dict(artifact_row)),
                    "revision": revision_payload["revision"],
                    "data": revision_payload["data"],
                    "preview": revision_payload["preview"],
                    "link": _link_dict(link_row, str(artifact_row["artifact_id"])),
                }
            )
    return {"items": items, "total": len(items), "skipped": skipped}


# ---------------------------------------------------------------------------
# HTTP 请求模型
# ---------------------------------------------------------------------------


class CreateArtifactInput(BaseModel):
    """创建附件请求体；``data`` / ``preview`` 只接受 JSON 对象，由服务端再校验。"""

    kind: str = ""
    title: str = ""
    data: Any = None
    preview: Any = None
    source_ref: str | None = None
    source_kind: str | None = None
    artifact_id: str | None = None
    links: Any = None


class UpdateArtifactInput(BaseModel):
    """更新附件请求体：有 ``data``/``preview`` 时新增版本，仅 ``title`` 时不新增版本。"""

    data: Any = None
    preview: Any = None
    title: str | None = None
    expected_revision: int | None = None


class LinkArtifactInput(BaseModel):
    """关联目标请求体。"""

    target_kind: str = ""
    target_key: str = ""
    revision: int | None = None


class UnlinkArtifactInput(BaseModel):
    """解除关联请求体。"""

    target_kind: str = ""
    target_key: str = ""


# ---------------------------------------------------------------------------
# HTTP 路由（静态路径必须注册在 /{artifact_id} 之前）
# ---------------------------------------------------------------------------


@router.get("")
def route_list_artifacts(
    kind: str | None = Query(default=None, max_length=40),
    target_kind: str | None = Query(default=None, max_length=40),
    target_key: str | None = Query(default=None, max_length=300),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    user=Depends(current_user),
):
    """列出我的学习附件（公开元数据，不含内容），可按类型 / 关联目标过滤并分页。"""
    return list_artifacts(
        user["id"],
        kind=kind,
        target_kind=target_kind,
        target_key=target_key,
        page=page,
        page_size=page_size,
    )


@router.get("/by-target")
def route_artifacts_by_target(
    target_kind: str | None = Query(default=None, max_length=40),
    target_key: str | None = Query(default=None, max_length=300),
    user=Depends(current_user),
):
    """按目标（题目/笔记/课堂/复习/助手）读取挂载的附件内容与固定/跟随版本。"""
    if not target_kind or not target_key:
        raise HTTPException(422, "请提供 target_kind 与 target_key")
    return artifacts_for_target(user["id"], target_kind, target_key)


@router.post("", status_code=201)
def route_create_artifact(body: CreateArtifactInput, user=Depends(current_user)):
    """创建学习附件与第 1 版内容（返回 ``{artifact, revision}``）。"""
    return create_artifact(
        user["id"],
        kind=body.kind,
        title=body.title,
        data=body.data,
        preview=body.preview,
        source_ref=body.source_ref,
        source_kind=body.source_kind,
        artifact_id=body.artifact_id,
        links=body.links,
    )


@router.get("/{artifact_id}")
def route_get_artifact(
    artifact_id: str,
    revision: int | None = Query(default=None, ge=1),
    user=Depends(current_user),
):
    """读取附件指定版本（省略 ``revision`` 读最新版）；他人或已删除附件返回 404。"""
    return get_artifact(user["id"], artifact_id, revision=revision)


@router.patch("/{artifact_id}")
def route_update_artifact(
    artifact_id: str,
    body: UpdateArtifactInput,
    user=Depends(current_user),
):
    """更新附件：内容变化新增版本（可用 ``expected_revision`` 做 CAS），仅标题不新增版本。"""
    provided = body.model_fields_set
    if not (provided - {"expected_revision"}):
        raise HTTPException(422, "请至少提供 data、preview 或 title 之一")
    title_value = None
    if "title" in provided:
        title_value = _validate_title(body.title)
    if "data" in provided or "preview" in provided:
        if "data" in provided:
            data_value = body.data
        else:
            # 只改预览：在读取到的当前内容上生成新版本，内容本身不变。
            data_value = get_artifact(user["id"], artifact_id)["revision"]["data"]
        return add_revision(
            user["id"],
            artifact_id,
            data=data_value,
            preview=body.preview if "preview" in provided else None,
            title=title_value,
            expected_revision=body.expected_revision,
        )
    return _rename_artifact(
        user["id"], artifact_id, title_value, body.expected_revision
    )


@router.delete("/{artifact_id}")
def route_delete_artifact(artifact_id: str, user=Depends(current_user)):
    """软删除附件：立即不可读，历史版本与关联记录保留。"""
    return _soft_delete(user["id"], artifact_id)


@router.get("/{artifact_id}/revisions")
def route_list_revisions(artifact_id: str, user=Depends(current_user)):
    """列出附件的全部历史版本元数据（最新在前）。"""
    return _list_revisions(user["id"], artifact_id)


@router.post("/{artifact_id}/links")
def route_link_artifact(
    artifact_id: str,
    body: LinkArtifactInput,
    user=Depends(current_user),
):
    """把附件关联到目标；``revision`` 省略跟随最新版，否则固定版本。"""
    return link_artifact(
        user["id"],
        artifact_id,
        body.target_kind,
        body.target_key,
        revision=body.revision,
    )


@router.delete("/{artifact_id}/links")
def route_unlink_artifact(
    artifact_id: str,
    body: UnlinkArtifactInput,
    user=Depends(current_user),
):
    """解除附件与目标的关联。"""
    return unlink_artifact(user["id"], artifact_id, body.target_kind, body.target_key)


__all__ = [
    "ARTIFACT_KINDS",
    "ARTIFACT_ID_RE",
    "DEFAULT_PAGE_SIZE",
    "MAX_DATA_BYTES",
    "MAX_LINKS_PER_ARTIFACT",
    "MAX_PAGE_SIZE",
    "MAX_PREVIEW_BYTES",
    "MAX_TITLE_LENGTH",
    "TARGET_KINDS",
    "add_revision",
    "artifacts_for_target",
    "create_artifact",
    "get_artifact",
    "init_study_artifacts",
    "link_artifact",
    "list_artifacts",
    "preview_public",
    "router",
    "unlink_artifact",
]
