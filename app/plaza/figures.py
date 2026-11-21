"""广场公开衍生图：登记、授权与匿名读取（方案 §5「文件、版权和未成年人」）。

规则：

* 投稿快照里的配图只有「可公开副本」才能被匿名读取。进入公开可见状态
  （``community_review`` / ``verified`` / ``published``）时为快照逐图建立公开衍生记录：
  复制原始字节、校验 ``sha256``、以 ``public_derivative=1`` + ``status='published'``
  登记到 ``pl_assets``，原件归属与用途不变；
* 无法公开的引用（题库原始素材、未登记旧文件名、他人私有资产、白名单外格式、哈希不一致）
  **不阻断**发布：逐图登记降级原因，公开详情返回 ``figure_notice``，发布响应给编辑提示；
* 匿名读图必须同时满足：稿件仍处于公开状态（``active release`` 或社区待核验快照）、
  映射记录属于**当前版本**且状态为 ``active``、资产 ``public_derivative`` 且状态可用、
  内容类型在白名单内；其余一律 404/415，不泄露存储路径与他人私有资产存在性。
"""
from __future__ import annotations

import hashlib

from fastapi import HTTPException

from .. import storage
from ..db import utcnow
from . import db

#: 无法公开的原因（中文提示按原因聚合，编辑端与公开详情共用同一口径）。
FIGURE_MESSAGES = {
    "bank": "配图来自题库原始素材，匿名访客暂不能查看（配图需登录后查看）。",
    "unregistered": "配图引用未登记归属，暂不可公开。",
    "private": "配图属于其他用户的私有资产，不能公开。",
    "disabled": "配图已失效或停用，暂不可公开。",
    "missing": "配图文件缺失，暂不可公开。",
    "integrity": "配图内容校验失败（sha256 不一致），暂不可公开。",
    "media_type": "配图格式不在公开白名单（png/jpeg/webp）内，暂不可公开。",
    "unresolved": "配图引用无法解析，暂不可公开。",
    "error": "配图处理失败，暂不可公开。",
}

#: 公开详情里给访客的一句话提示（按优先级取第一条）。
FIGURE_VISITOR_NOTICES = (
    ("bank", "配图需登录后查看"),
    ("private", "部分配图暂不可公开查看"),
    ("unregistered", "部分配图暂不可公开查看"),
    ("disabled", "部分配图暂不可公开查看"),
    ("missing", "部分配图暂不可公开查看"),
    ("integrity", "部分配图暂不可公开查看"),
    ("media_type", "部分配图暂不可公开查看"),
    ("unresolved", "部分配图暂不可公开查看"),
    ("error", "部分配图暂不可公开查看"),
)

#: 公开副本可复用的资产状态（本人原件仍可为 active；公开副本固定 published）。
_READABLE_ASSET_STATUSES = ("active", "published")
_COPY_LIMIT_BYTES = 20 * 1024 * 1024
_TEXT_LIMIT = 180


def _text(value, limit: int = _TEXT_LIMIT) -> str:
    if value is None or isinstance(value, (dict, list)):
        return ""
    return str(value).strip()[:limit]


def snapshot_images(snapshot: dict) -> list[dict]:
    content = snapshot.get("content") if isinstance(snapshot, dict) else None
    images = content.get("images") if isinstance(content, dict) else None
    if not isinstance(images, list):
        return []
    return [image for image in images if isinstance(image, dict)]


def _lazy_assets():
    from ..paperlab import assets as paperlab_assets
    return paperlab_assets


def _normalize_media_type(value, storage_ref: str = "") -> str:
    """规整内容类型；缺失时按存储文件后缀推断（只用于白名单判断）。"""
    media = _text(value, 40).lower()
    if media:
        return media
    lower = _text(storage_ref, 500).lower()
    for suffix, candidate in (
        (".png", "image/png"), (".jpg", "image/jpeg"),
        (".jpeg", "image/jpeg"), (".webp", "image/webp"),
    ):
        if lower.endswith(suffix):
            return candidate
    return ""


def _read_asset_bytes(asset_row: dict) -> bytes | None:
    folder = _text(asset_row.get("folder"), 300)
    reference = _text(asset_row.get("storage_ref"), 500)
    if not folder or not reference:
        return None
    try:
        raw = storage.read_file(folder, reference)
    except HTTPException:
        return None
    except OSError:
        return None
    except Exception:  # noqa: BLE001 - 对象存储异常按不可读处理，不阻断发布
        return None
    if not raw or len(raw) > _COPY_LIMIT_BYTES:
        return None
    return raw


def _image_source_kind(image: dict) -> str:
    """判断快照配图的来源：``bank`` 表示题库原始素材（无个人资产归属）。"""
    source = _text(image.get("source") or image.get("origin"), 60).lower()
    if source.startswith("bank"):
        return "bank"
    if str(image.get("bank") or "").strip():
        return "bank"
    return ""


def _resolve_source_asset(ref: str, author_id: int) -> dict | None:
    """把快照里的配图引用解析成资产行（先 asset_id，再旧文件名；不做归属判断）。"""
    assets = _lazy_assets()
    row = assets.fetch_asset(ref)
    if row is not None:
        return row
    own = assets.find_own_asset_by_ref(author_id, ref)
    if own is not None:
        return own
    return assets.find_any_asset_by_ref(ref)


def build_public_copy(con, asset_row: dict, *, author_id: int,
                      question_id: int | None = None) -> dict:
    """为一条个人资产生成公开副本（复制字节 + sha256 校验 + public_derivative 登记）。

    返回 ``{"status": "active"|"unavailable", "reason": str, "public_asset_id": str,
    "media_type": str, "sha256": str}``；任何异常都降级为 unavailable，不向上抛。
    """
    assets = _lazy_assets()
    if not asset_row:
        return {"status": "unavailable", "reason": "unregistered"}
    if str(asset_row.get("status") or "") not in _READABLE_ASSET_STATUSES:
        return {"status": "unavailable", "reason": "disabled"}
    owner_id = int(asset_row.get("user_id") or 0)
    if owner_id != int(author_id) and not assets.asset_is_public(asset_row):
        return {"status": "unavailable", "reason": "private"}
    media_type = _normalize_media_type(asset_row.get("media_type"), asset_row.get("storage_ref"))
    if media_type not in db.PUBLIC_IMAGE_MEDIA_TYPES:
        return {"status": "unavailable", "reason": "media_type", "media_type": media_type}
    if assets.asset_is_public(asset_row):
        # 已经是公开衍生图：直接复用，不重复复制字节。
        return {
            "status": "active",
            "public_asset_id": str(asset_row.get("asset_id") or ""),
            "media_type": media_type,
            "sha256": _text(asset_row.get("sha256"), 80),
        }
    raw = _read_asset_bytes(asset_row)
    if raw is None:
        return {"status": "unavailable", "reason": "missing"}
    digest = hashlib.sha256(raw).hexdigest()
    expected = _text(asset_row.get("sha256"), 80).lower()
    if expected and expected != digest:
        # 原件与登记哈希不一致：拒绝公开（可能是文件被替换）。
        return {"status": "unavailable", "reason": "integrity"}
    try:
        public_id = assets.save_asset_bytes(
            int(author_id), raw, media_type=media_type, purpose="question_image",
            question_id=question_id, status="published", public_derivative=True, con=con,
        )
    except Exception:  # noqa: BLE001 - 单个配图登记失败降级，不阻断发布
        return {"status": "unavailable", "reason": "error"}
    return {
        "status": "active",
        "public_asset_id": public_id,
        "media_type": media_type,
        "sha256": digest,
    }


def _record_figure(con, existing: dict | None, values: dict) -> None:
    if existing:
        con.execute(
            "UPDATE plaza_public_assets SET source_ref=?, public_asset_id=?, media_type=?, "
            "sha256=?, status=?, reason=? WHERE id=?",
            (
                values["source_ref"], values["public_asset_id"], values["media_type"],
                values["sha256"], values["status"], values["reason"], int(existing["id"]),
            ),
        )
        return
    db.insert_row(con, "plaza_public_assets", {**values, "created_at": utcnow()})


def _visitor_notice(reasons: list[str]) -> str:
    for reason, message in FIGURE_VISITOR_NOTICES:
        if reason in reasons:
            return message
    return ""


def _reuse_active_figure(prior: dict | None) -> dict | None:
    """已登记且公开副本仍可用时复用（幂等调用不重复复制字节）。"""
    if not prior or str(prior.get("status") or "") != "active":
        return None
    public_asset_id = str(prior.get("public_asset_id") or "")
    if not public_asset_id:
        return None
    asset_row = fetch_public_asset(public_asset_id)
    if not asset_readable(asset_row):
        return None
    return {
        "status": "active",
        "public_asset_id": public_asset_id,
        "media_type": str(prior.get("media_type") or asset_row.get("media_type") or ""),
        "sha256": str(prior.get("sha256") or asset_row.get("sha256") or ""),
        "source_ref": str(prior.get("source_ref") or ""),
    }


def ensure_public_figures(con, row: dict, *, version: int | None = None) -> dict:
    """为投稿快照逐图登记公开衍生记录（幂等；可在初审通过与发布时重复调用）。

    调用方需持有 ``write_lock(con, "plaza-write")``。返回摘要：公开数量、降级数量、
    逐图状态与给编辑/访客的提示（不包含任何存储路径或他人私有资产信息）。
    """
    submission_id = int(row["id"])
    author_id = int(row["author_id"])
    version = int(version if version is not None else (row.get("current_version") or 1))
    snapshot = db.json_load(row.get("snapshot_json"), {})
    if not isinstance(snapshot, dict):
        snapshot = {}
    images = snapshot_images(snapshot)
    existing_rows = {
        int(item["image_index"]): item
        for item in (
            db._row_dict(raw) for raw in con.execute(
                "SELECT * FROM plaza_public_assets WHERE submission_id=? AND version=?",
                (submission_id, version),
            ).fetchall()
        )
        if item
    }
    total = len(images)
    items: list[dict] = []
    reasons: list[str] = []
    public_count = 0
    for index, image in enumerate(images):
        prior = existing_rows.get(index)
        source_kind = _image_source_kind(image)
        result: dict
        reused = _reuse_active_figure(prior)
        if reused is not None:
            # 幂等：已登记且公开副本仍可用时直接复用，不重复复制字节。
            result = reused
            source_kind = ""
        elif source_kind == "bank":
            result = {"status": "unavailable", "reason": "bank"}
        else:
            ref = ""
            try:
                ref = _lazy_assets().normalize_asset_ref(image.get("asset"))
            except Exception:  # noqa: BLE001 - 资产模块异常按无法解析处理
                ref = ""
            if not ref:
                result = {"status": "unavailable", "reason": "unresolved"}
            else:
                asset_row = _resolve_source_asset(ref, author_id)
                if asset_row is None:
                    result = {"status": "unavailable", "reason": "unregistered"}
                else:
                    try:
                        result = build_public_copy(
                            con, asset_row, author_id=author_id,
                            question_id=int(row.get("question_id") or 0) or None,
                        )
                    except Exception:  # noqa: BLE001 - 单图失败只降级
                        result = {"status": "unavailable", "reason": "error"}
                    result["source_ref"] = ref
        if result.get("status") != "active":
            reason = str(result.get("reason") or "error")
            if reason not in FIGURE_MESSAGES:
                reason = "error"
            result = {
                "status": "unavailable", "reason": reason,
                "public_asset_id": "", "media_type": "", "sha256": "",
                "source_ref": str(result.get("source_ref") or ""),
            }
            reasons.append(reason)
        else:
            public_count += 1
        values = {
            "submission_id": submission_id,
            "version": version,
            "image_index": index,
            "source_ref": _text(result.get("source_ref"), 200),
            "public_asset_id": _text(result.get("public_asset_id"), 200),
            "media_type": _text(result.get("media_type"), 40).lower(),
            "sha256": _text(result.get("sha256"), 80),
            "status": result["status"],
            "reason": str(result.get("reason") or ""),
        }
        _record_figure(con, prior, values)
        items.append({
            "index": index,
            "status": result["status"],
            "reason": str(result.get("reason") or ""),
            "media_type": values["media_type"],
        })
    # 旧版本的公开图不再可匿名读取（映射保留供审计，状态置 disabled）。
    con.execute(
        "UPDATE plaza_public_assets SET status='disabled' "
        "WHERE submission_id=? AND version<>? AND status='active'",
        (submission_id, version),
    )
    notices = [
        f"第 {item['index'] + 1} 张配图：{FIGURE_MESSAGES.get(item['reason'], item['reason'])}"
        for item in items
        if item["status"] != "active"
    ]
    unavailable_count = total - public_count
    return {
        "version": version,
        "total": total,
        "public": public_count,
        "unavailable": unavailable_count,
        "items": items,
        "notices": notices,
        "figure_notice": _visitor_notice(reasons),
        "blocking": False,
    }


def public_figure_map(con, row: dict, *, version: int | None = None) -> dict[int, dict]:
    """当前版本快照的公开图映射（image_index → 映射行；只读，不含存储路径）。"""
    submission_id = int(row["id"])
    version = int(version if version is not None else (row.get("current_version") or 1))
    rows = con.execute(
        "SELECT image_index, public_asset_id, media_type, sha256, status, reason "
        "FROM plaza_public_assets WHERE submission_id=? AND version=? ORDER BY image_index",
        (submission_id, version),
    ).fetchall()
    return {int(item["image_index"]): dict(item) for item in rows}


def figure_notice_from_map(image_map: dict[int, dict]) -> str:
    reasons = [
        str(item.get("reason") or "")
        for item in image_map.values()
        if str(item.get("status") or "") == "unavailable"
    ]
    return _visitor_notice(reasons)


def fetch_public_asset(public_asset_id: str) -> dict | None:
    """按公开副本 asset_id 取资产行（不含归属判断）。"""
    try:
        ref = _lazy_assets().normalize_asset_ref(public_asset_id)
    except Exception:  # noqa: BLE001
        return None
    if not ref:
        return None
    return _lazy_assets().fetch_asset(ref)


def asset_readable(row: dict) -> bool:
    """公开副本自身可读：``public_derivative`` 且状态可用。

    内容类型白名单在 :func:`serve_public_image` 里单独判断（非白名单返回 415，不输出字节）。
    """
    if not row:
        return False
    if not bool(row.get("public_derivative")):
        return False
    return str(row.get("status") or "") in _READABLE_ASSET_STATUSES


def submission_publicly_visible(con, row: dict | None) -> bool:
    """稿件当前是否处于匿名可读状态（含 active release 校验，撤回后立即失效）。"""
    if not row:
        return False
    status = str(row.get("status") or "")
    if status not in db.PUBLIC_STATUSES:
        return False
    if not str(row.get("public_id") or "").strip():
        return False
    if status == "published":
        released = con.execute(
            "SELECT 1 FROM plaza_releases WHERE submission_id=? AND status='active' "
            "AND public_id=? LIMIT 1",
            (int(row["id"]), str(row.get("public_id") or "")),
        ).fetchone()
        if released is None:
            return False
    return True


def _mapping_rows_for_asset(con, public_asset_id: str) -> list[dict]:
    rows = con.execute(
        "SELECT * FROM plaza_public_assets WHERE public_asset_id=? AND status='active' "
        "ORDER BY id DESC",
        (public_asset_id,),
    ).fetchall()
    return [dict(item) for item in rows]


def authorize_public_asset(con, public_asset_id: str) -> dict | None:
    """匿名读图授权：资产必须是公开副本，且被某个当前公开稿件的当前版本引用。

    成功返回资产行（供 ``serve_public_image`` 输出字节）；任何不满足返回 None（调用方 404）。
    """
    row = fetch_public_asset(public_asset_id)
    if not asset_readable(row):
        return None
    for mapping in _mapping_rows_for_asset(con, str(row.get("asset_id") or "")):
        submission = db.load_submission(con, int(mapping["submission_id"]))
        if not submission_publicly_visible(con, submission):
            continue
        if int(mapping.get("version") or 1) != int(submission.get("current_version") or 1):
            continue
        return row
    return None


def authorize_question_image(con, public_id: str, index: int) -> dict | None:
    """按「公开题号 + 配图序号」授权匿名读图（快照当前版本 + 公开状态 + 公开副本）。"""
    submission = db.load_submission_by_public_id(con, public_id)
    if not submission_publicly_visible(con, submission):
        return None
    version = int(submission.get("current_version") or 1)
    mapping = con.execute(
        "SELECT * FROM plaza_public_assets WHERE submission_id=? AND version=? "
        "AND image_index=? AND status='active'",
        (int(submission["id"]), version, int(index)),
    ).fetchone()
    if mapping is None:
        return None
    row = fetch_public_asset(str(dict(mapping).get("public_asset_id") or ""))
    if not asset_readable(row):
        return None
    return row


def serve_public_image(row: dict):
    """把公开副本按白名单内容类型内联返回；非白名单 415，文件缺失 404。"""
    media_type = _normalize_media_type(row.get("media_type"), row.get("storage_ref"))
    if media_type not in db.PUBLIC_IMAGE_MEDIA_TYPES:
        raise HTTPException(415, "该配图类型不支持公开查看")
    folder = _text(row.get("folder"), 300)
    reference = _text(row.get("storage_ref"), 500)
    if not folder or not reference:
        raise HTTPException(404, "配图不可公开访问")
    headers = {
        # 撤回/下架必须立即生效：公开图不做共享缓存，避免旧副本在浏览器/CDN 残留。
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
        "Content-Disposition": "inline",
    }
    try:
        return storage.file_response(
            folder, reference, media_type=media_type, headers=headers,
        )
    except HTTPException:
        raise HTTPException(404, "配图不可公开访问") from None
    except OSError:
        raise HTTPException(404, "配图不可公开访问") from None


def summary_for_editor(figures: dict) -> dict:
    """发布/初审响应里给编辑的配图摘要（不含内部路径）。"""
    return {
        "total": int(figures.get("total") or 0),
        "public": int(figures.get("public") or 0),
        "unavailable": int(figures.get("unavailable") or 0),
        "figure_notice": str(figures.get("figure_notice") or ""),
        "notices": list(figures.get("notices") or []),
        "items": [
            {
                "index": int(item.get("index") or 0),
                "status": str(item.get("status") or ""),
                "reason": str(item.get("reason") or ""),
            }
            for item in (figures.get("items") or [])
            if isinstance(item, dict)
        ],
    }


__all__ = [
    "FIGURE_MESSAGES",
    "FIGURE_VISITOR_NOTICES",
    "asset_readable",
    "authorize_public_asset",
    "authorize_question_image",
    "build_public_copy",
    "ensure_public_figures",
    "fetch_public_asset",
    "figure_notice_from_map",
    "public_figure_map",
    "serve_public_image",
    "snapshot_images",
    "submission_publicly_visible",
    "summary_for_editor",
]
