"""广场服务层：投稿快照、来源/授权校验、评审阈值、状态机与序列化。

核心规则（方案 §5、§6）：

* 投稿内容固定为**不可变快照**（``plaza_submission_versions`` 逐版本留档）；
  个人题后续修改不会影响已投稿版本，作者需显式创建新版本；
* 公开投影只读取 ``verified`` / ``published``，且**绝不**输出内部 ``user_id``、
  用户名、模型配置、私有附件存储路径或评审员身份；
* 评审阈值集中定义在 :mod:`app.plaza.db`：同版 ≥5 位独立评审、≥4 通过、
  其中 ≥2 位学科复核员通过，且无未解决 reject 阻断；
* 冲突声明、作者回避、编辑/签发回避都在服务层统一判定，接口层只做参数校验。
"""
from __future__ import annotations

import hashlib

from fastapi import Depends, HTTPException

from ..auth import current_user
from ..db import connection, utcnow
from . import db

# ---------------------------------------------------------------------------
# 权限依赖
# ---------------------------------------------------------------------------

def require_plaza_role(*roles: str):
    """要求任一广场角色；``admin`` 直接放行（沿用现有后台角色）。"""
    allowed = tuple(roles)

    def dependency(user=Depends(current_user)):
        if str(user.get("role") or "") == "admin":
            return user
        db.ensure_tables()
        with connection() as con:
            granted = db.role_names(con, user["id"])
        if not granted.intersection(allowed):
            raise HTTPException(403, "需要评审员或平台编辑权限")
        return user

    return dependency


require_editor = require_plaza_role("editor")
require_reviewer = require_plaza_role("reviewer", "subject_reviewer", "editor")


# ---------------------------------------------------------------------------
# 文本与来源声明
# ---------------------------------------------------------------------------

def _text(value, limit: int) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else ""
    return str(value).strip()[:limit]


#: 来源与授权声明允许的字段（全部以文本存储；``note`` 仅供作者/编辑内部查看）。
SOURCE_TEXT_KEYS = (
    "url", "original_author", "license_basis", "rights_scope", "attribution",
    "reference", "page_no", "question_no", "note",
)
SOURCE_TEXT_LIMITS = {
    "url": 500,
    "original_author": 200,
    "license_basis": 300,
    "rights_scope": 500,
    "attribution": 300,
    "reference": 300,
    "page_no": 40,
    "question_no": 40,
    "note": 1000,
}
SOURCE_PUBLIC_KEYS = (
    "url", "original_author", "license_basis", "rights_scope", "attribution",
    "reference", "page_no", "question_no",
)


def normalize_source(raw, existing=None) -> dict:
    """规整来源/授权声明：只保留已知字段，合并旧值；未知字段忽略。"""
    source = {key: _text((existing or {}).get(key), SOURCE_TEXT_LIMITS[key])
              for key in SOURCE_TEXT_KEYS}
    if isinstance(raw, dict):
        for key in SOURCE_TEXT_KEYS:
            if key in raw:
                source[key] = _text(raw[key], SOURCE_TEXT_LIMITS[key])
    return source


def _has_provenance(source: dict) -> bool:
    return any(source.get(key) for key in
               ("url", "page_no", "question_no", "original_author", "reference"))


def source_checks(source: dict, license_confirmed: bool) -> list[dict]:
    """来源与授权声明的校验项（投稿/送审都复用同一口径）。"""
    return [
        {
            "key": "license_confirmed",
            "label": "已确认授权与来源声明",
            "ok": bool(license_confirmed),
            "blocking": True,
            "hint": "请勾选确认：内容来源合法、许可范围允许公开展示。",
        },
        {
            "key": "license_basis",
            "label": "许可依据",
            "ok": bool(source.get("license_basis")),
            "blocking": True,
            "hint": "填写许可依据，例如「原创」「CC BY-SA 4.0」「已获原作者书面授权」。",
        },
        {
            "key": "rights_scope",
            "label": "权利范围",
            "ok": bool(source.get("rights_scope")),
            "blocking": True,
            "hint": "说明展示、下载、导出、衍生分别允许的范围。",
        },
        {
            "key": "attribution",
            "label": "署名要求",
            "ok": bool(source.get("attribution")),
            "blocking": True,
            "hint": "填写署名要求；不需要署名时写「无需署名」。",
        },
        {
            "key": "provenance",
            "label": "可追溯出处",
            "ok": _has_provenance(source),
            "blocking": True,
            "hint": "至少填写出处 URL、页码、题号、原作者或引用说明之一。",
        },
    ]


def public_source(source: dict) -> dict:
    """公开投影：只保留来源声明中的展示字段，不输出内部确认者信息。"""
    return {key: str(source.get(key) or "") for key in SOURCE_PUBLIC_KEYS}


# ---------------------------------------------------------------------------
# 不可变内容快照
# ---------------------------------------------------------------------------

def _as_text(value, limit: int = 20000) -> str:
    if value is None or isinstance(value, (dict, list)):
        return ""
    return str(value).strip()[:limit]


def _clean_options(raw, limit: int = 12) -> list[dict]:
    options: list[dict] = []
    if not isinstance(raw, list):
        return options
    for index, item in enumerate(raw[:limit]):
        if isinstance(item, dict):
            label = _as_text(item.get("label") or item.get("key"), 8)
            text = _as_text(item.get("text") if item.get("text") is not None else item.get("content"), 4000)
        else:
            label = ""
            text = _as_text(item, 4000)
        if not text:
            continue
        options.append({"label": label or chr(ord("A") + index), "text": text})
    return options


def _clean_answers(raw, limit: int = 48) -> list[str]:
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    answers: list[str] = []
    for item in raw[:limit]:
        if isinstance(item, dict):
            item = item.get("answer") if item.get("answer") is not None else item.get("text")
        text = _as_text(item, 4000)
        if text:
            answers.append(text)
    return answers


def _clean_parts(raw, limit: int = 20) -> list[dict]:
    if not isinstance(raw, list):
        return []
    parts: list[dict] = []
    for index, part in enumerate(raw[:limit]):
        if not isinstance(part, dict):
            continue
        parts.append({
            "index": int(part.get("index") or index + 1),
            "type": _as_text(part.get("type"), 20),
            "stem": _as_text(part.get("stem")),
            "options": _clean_options(part.get("options")),
            "answers": _clean_answers(part.get("answers")),
            "explanation": _as_text(part.get("explanation")),
            "rubric": _as_text(part.get("rubric")),
            "knowledge_tags": [_as_text(tag, 80) for tag in (part.get("knowledge_tags") or [])
                               if _as_text(tag, 80)][:30],
            "score": part.get("score") if isinstance(part.get("score"), (int, float)) else None,
        })
    return parts


def _looks_like_storage_path(value) -> bool:
    """旧数据里可能残留题库相对路径/绝对路径：只用于标记「不可公开展示」，不落库。"""
    text = _as_text(value, 400)
    if not text:
        return False
    return any(sep in text for sep in ("/", "\\", ":"))


def _clean_images(raw, limit: int = 20) -> list[dict]:
    """只保留服务端资产引用（asset_id/token），不保存存储路径。

    无法归一化的旧引用（题库原始素材相对路径等）不落原文，只保留
    ``{"asset": "", "source": "bank", "unavailable": True}`` 降级标记：
    公开详情据此返回 ``figure_notice``，编辑端不因此阻断发布。
    """
    if not isinstance(raw, list):
        return []
    images: list[dict] = []
    try:
        from ..paperlab import assets as paperlab_assets
    except Exception:  # noqa: BLE001 - 资产模块不可用时按无配图处理
        paperlab_assets = None
    for image in raw[:limit]:
        if not isinstance(image, dict):
            continue
        raw_asset = image.get("asset")
        ref = ""
        if paperlab_assets is not None:
            ref = paperlab_assets.normalize_asset_ref(raw_asset)
        else:
            ref = _as_text(raw_asset, 180)
        if not ref:
            # 不可归一化：只记录来源标记，绝不保存原始路径/文件名。
            source = _as_text(image.get("source") or image.get("origin"), 40).lower()
            is_bank = (
                bool(_as_text(image.get("bank"), 40))
                or source.startswith("bank")
                or _looks_like_storage_path(raw_asset)
            )
            if not (_as_text(raw_asset, 400) or is_bank):
                continue  # 完全空引用，不占用配图序号
            marker = {"asset": "", "unavailable": True}
            if is_bank:
                marker["source"] = "bank"
            else:
                marker["source"] = "unresolved"
            for key in ("role", "alt"):
                value = _as_text(image.get(key), 200)
                if value:
                    marker[key] = value
            images.append(marker)
            continue
        item = {"asset": ref}
        for key in ("role", "alt"):
            value = _as_text(image.get(key), 200)
            if value:
                item[key] = value
        images.append(item)
    return images


def clean_content(raw) -> dict:
    """把个人题 content 投影成投稿快照允许的白名单结构（不复制任意键）。"""
    if not isinstance(raw, dict):
        raw = {}
    content = {
        "stem": _as_text(raw.get("stem")),
        "options": _clean_options(raw.get("options")),
        "answers": _clean_answers(raw.get("answers")),
        "explanation": _as_text(raw.get("explanation")),
        "rubric": _as_text(raw.get("rubric")),
        "parts": _clean_parts(raw.get("parts")),
        "images": _clean_images(raw.get("images")),
    }
    score = raw.get("score")
    if isinstance(score, (int, float)) and not isinstance(score, bool) and 0 <= float(score) <= 1000:
        content["score"] = round(float(score), 2)
    return content


def _difficulty(value) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return round(max(0.0, min(1.0, number)), 3)


def _tags(raw) -> list[str]:
    value = db.json_load(raw, [])
    if not isinstance(value, list):
        return []
    return [str(tag).strip()[:80] for tag in value if str(tag).strip()][:30]


def build_snapshot(question: dict) -> dict:
    """从个人题（只读 ``pl_questions`` 行）生成不可变内容快照。"""
    content_raw = db.json_load(question.get("content_json"), {})
    if not isinstance(content_raw, dict):
        content_raw = {}
    snapshot = {
        "question_id": int(question["id"]),
        "question_version": int(question.get("version") or 1),
        "type": str(question.get("type") or "SUBJECTIVE"),
        "subject": str(question.get("subject") or "").strip()[:40],
        "grade": str(question.get("grade") or "").strip()[:40],
        "difficulty": _difficulty(question.get("difficulty")),
        "knowledge_tags": _tags(question.get("knowledge_tags_json")),
        "content": clean_content(content_raw),
        "verify_status": str(question.get("verify_status") or "pending"),
    }
    return snapshot


def snapshot_fingerprint(snapshot: dict) -> str:
    """内容指纹：排除采集时间等元数据，用于判断「是否有实质修改」。"""
    payload = {
        "type": snapshot.get("type") or "",
        "subject": snapshot.get("subject") or "",
        "grade": snapshot.get("grade") or "",
        "difficulty": snapshot.get("difficulty"),
        "knowledge_tags": snapshot.get("knowledge_tags") or [],
        "content": snapshot.get("content") or {},
    }
    return hashlib.sha256(db.json_dump(payload).encode("utf-8")).hexdigest()


def image_check(user_id: int, content: dict) -> tuple[bool, str]:
    """配图引用归属校验（复用 paperlab.assets 的既有规则）。"""
    images = content.get("images") if isinstance(content, dict) else None
    if not images:
        return True, ""
    try:
        from ..paperlab import assets as paperlab_assets
        paperlab_assets.validate_content_image_refs(int(user_id), content)
    except HTTPException as exc:
        return False, str(exc.detail)
    except Exception:  # noqa: BLE001 - 资产模块异常不能阻断投稿草稿创建
        return False, "配图引用暂无法校验，请稍后重试"
    return True, ""


def snapshot_checks(snapshot: dict, source: dict, license_confirmed: bool,
                    image_ok: bool = True, image_hint: str = "") -> list[dict]:
    content = snapshot.get("content") if isinstance(snapshot.get("content"), dict) else {}
    has_answer = bool(content.get("answers"))
    if not has_answer:
        for part in content.get("parts") or []:
            if isinstance(part, dict) and part.get("answers"):
                has_answer = True
                break
    checks = [
        {
            "key": "stem",
            "label": "题干完整",
            "ok": bool(content.get("stem")),
            "blocking": True,
            "hint": "题面不能为空。",
        },
        {
            "key": "answer",
            "label": "参考答案",
            "ok": has_answer,
            "blocking": True,
            "hint": "至少填写一个参考答案（含子题答案），否则无法进入社区核验。",
        },
        {
            "key": "subject",
            "label": "学科",
            "ok": bool(snapshot.get("subject")),
            "blocking": True,
            "hint": "请先在个人题中补充学科。",
        },
        {
            "key": "type",
            "label": "题型",
            "ok": bool(snapshot.get("type")),
            "blocking": True,
            "hint": "请先在个人题中补充题型。",
        },
        {
            "key": "images",
            "label": "配图归属",
            "ok": bool(image_ok),
            "blocking": True,
            "hint": image_hint or "配图必须来自本人上传或已公开授权的资产。",
        },
        {
            "key": "self_verified",
            "label": "作者已自检",
            "ok": str(snapshot.get("verify_status") or "") == "verified",
            "blocking": False,
            "hint": "建议先在个人题库中核验答案与解析（不阻断投稿）。",
        },
    ]
    return checks + source_checks(source, license_confirmed)


def blocking_checks(checks: list[dict]) -> list[dict]:
    return [check for check in checks if check.get("blocking") and not check.get("ok")]


def checks_payload(checks: list[dict]) -> dict:
    return {
        "checked": len(checks),
        "blocking": blocking_checks(checks),
        "warnings": [check for check in checks if not check.get("blocking") and not check.get("ok")],
        "items": checks,
    }


def stem_preview(snapshot: dict, limit: int = 120) -> str:
    content = snapshot.get("content") if isinstance(snapshot.get("content"), dict) else {}
    stem = str(content.get("stem") or "").strip()
    return stem[:limit] + ("…" if len(stem) > limit else "")


# ---------------------------------------------------------------------------
# 评审汇总与阈值
# ---------------------------------------------------------------------------

def deadline_passed(row: dict) -> bool:
    deadline = str((row or {}).get("review_deadline_at") or "")
    return bool(deadline) and deadline <= utcnow()


def review_summary(con, row: dict, version: int | None = None) -> dict:
    """按版本聚合评审（独立票口径：排除作者自评与已声明冲突的票）。"""
    submission_id = int(row["id"])
    author_id = int(row["author_id"])
    version = int(version if version is not None else (row.get("current_version") or 1))
    rows = con.execute(
        "SELECT reviewer_id, reviewer_role, verdict, conflict_declared FROM plaza_reviews "
        "WHERE submission_id=? AND version=?",
        (submission_id, version),
    ).fetchall()
    independent = [
        item for item in rows
        if not int(item["conflict_declared"] or 0) and int(item["reviewer_id"]) != author_id
    ]
    verdicts = [str(item["verdict"] or "") for item in independent]
    approves = verdicts.count("approve")
    revises = verdicts.count("revise")
    rejects = verdicts.count("reject")
    unsures = verdicts.count("unsure")
    subject_approves = sum(
        1 for item in independent
        if str(item["reviewer_role"] or "") == "subject_reviewer"
        and str(item["verdict"] or "") == "approve"
    )
    threshold_met = (
        len(independent) >= db.THRESHOLD_MIN_REVIEWS
        and approves >= db.THRESHOLD_MIN_APPROVES
        and subject_approves >= db.THRESHOLD_MIN_SUBJECT_APPROVES
        and rejects == 0
    )
    return {
        "version": version,
        "independent_reviews": len(independent),
        "approves": approves,
        "revises": revises,
        "rejects": rejects,
        "unsure": unsures,
        "subject_reviewer_approves": subject_approves,
        "conflict_declared": len(rows) - len(independent),
        "blocking": rejects > 0,
        "threshold_met": threshold_met,
        "thresholds": {
            "min_reviews": db.THRESHOLD_MIN_REVIEWS,
            "min_approves": db.THRESHOLD_MIN_APPROVES,
            "min_subject_reviewer_approves": db.THRESHOLD_MIN_SUBJECT_APPROVES,
        },
    }


def has_own_review(con, row: dict, reviewer_id: int) -> dict | None:
    review = con.execute(
        "SELECT * FROM plaza_reviews WHERE submission_id=? AND version=? AND reviewer_id=?",
        (int(row["id"]), int(row.get("current_version") or 1), int(reviewer_id)),
    ).fetchone()
    if review is None:
        return None
    return dict(review)


def reviews_for_version(con, row: dict, version: int | None = None) -> list[dict]:
    version = int(version if version is not None else (row.get("current_version") or 1))
    rows = con.execute(
        "SELECT * FROM plaza_reviews WHERE submission_id=? AND version=? ORDER BY id",
        (int(row["id"]), version),
    ).fetchall()
    return [dict(item) for item in rows]


def _review_public(item: dict) -> dict:
    return {
        "verdict": str(item.get("verdict") or ""),
        "reviewer_role": str(item.get("reviewer_role") or ""),
        "reasons": str(item.get("reasons") or ""),
        "independent_answer": str(item.get("independent_answer") or ""),
        "conflict_declared": bool(item.get("conflict_declared")),
        "created_at": str(item.get("created_at") or ""),
    }


# ---------------------------------------------------------------------------
# 投稿序列化（作者 / 评审 / 编辑视角）
# ---------------------------------------------------------------------------

def _viewer_flags(con, row: dict, user: dict) -> dict:
    user_id = int(user["id"])
    is_admin = str(user.get("role") or "") == "admin"
    is_author = int(row["author_id"]) == user_id
    roles = db.active_roles(con, user_id) if user_id else []
    names = {str(item.get("role") or "") for item in roles}
    is_editor = is_admin or "editor" in names
    is_reviewer = is_admin or bool(names.intersection(db.REVIEWER_ROLES)) or "editor" in names
    if is_admin:
        kind = "admin"
    elif is_author:
        kind = "author"
    elif is_editor:
        kind = "editor"
    elif is_reviewer:
        kind = "reviewer"
    else:
        kind = "stranger"
    return {
        "user_id": user_id,
        "is_admin": is_admin,
        "is_author": is_author,
        "is_editor": is_editor,
        "is_reviewer": is_reviewer,
        "kind": kind,
        "role_names": names,
    }


def can_view_submission(con, row: dict, user: dict) -> bool:
    return _viewer_flags(con, row, user)["kind"] != "stranger"


def submission_detail(con, row: dict, user: dict) -> dict:
    """作者/评审/编辑视角的投稿详情；按视角裁剪他人投票与内部字段。"""
    flags = _viewer_flags(con, row, user)
    kind = flags["kind"]
    snapshot = db.json_load(row.get("snapshot_json"), {})
    if not isinstance(snapshot, dict):
        snapshot = {}
    source = db.json_load(row.get("source_json"), {})
    if not isinstance(source, dict):
        source = {}
    license_confirmed = bool(row.get("license_confirmed"))
    image_ok, image_hint = image_check(int(row["author_id"]), snapshot.get("content") or {})
    checks = snapshot_checks(snapshot, source, license_confirmed, image_ok, image_hint)
    summary = review_summary(con, row)
    current_version = int(row.get("current_version") or 1)
    mine = has_own_review(con, row, flags["user_id"])
    status = str(row.get("status") or "")

    # 申诉记录：作者只看自己的申诉；编辑/管理员看全部（申诉人脱敏）。
    appeals: list[dict] = []
    my_appeal: dict | None = None
    if kind in ("admin", "editor", "author"):
        if kind == "author":
            appeal_rows = con.execute(
                "SELECT * FROM plaza_appeals WHERE submission_id=? AND user_id=? "
                "ORDER BY id DESC",
                (int(row["id"]), flags["user_id"]),
            ).fetchall()
        else:
            appeal_rows = con.execute(
                "SELECT * FROM plaza_appeals WHERE submission_id=? ORDER BY id DESC",
                (int(row["id"]),),
            ).fetchall()
        for raw in appeal_rows:
            item = dict(raw)
            entry = {
                "id": int(item.get("id") or 0),
                "reason": str(item.get("reason") or ""),
                "status": str(item.get("status") or ""),
                "status_label": db.feedback_status_label(item.get("status")),
                "resolution": str(item.get("resolution") or ""),
                "created_at": str(item.get("created_at") or ""),
                "resolved_at": item.get("resolved_at"),
            }
            if kind != "author":
                entry["author_display"] = db.masked_display(
                    "申诉作者", item.get("user_id"),
                )
            appeals.append(entry)
        if kind == "author" and appeals:
            my_appeal = next(
                (item for item in appeals if item.get("status") == "pending"),
                appeals[0],
            )

    if kind in ("admin", "editor", "author"):
        visible_summary = summary
    elif mine is not None:
        visible_summary = summary
    else:
        visible_summary = {
            "hidden": True,
            "version": current_version,
            "notice": "提交你的独立评审后才能查看已有投票与意见。",
        }

    reviews: list[dict] = []
    if kind in ("admin", "editor"):
        reviews = []
        for item in reviews_for_version(con, row):
            view = _review_public(item)
            view["reviewer_id"] = int(item.get("reviewer_id") or 0)
            view["id"] = int(item.get("id") or 0)
            reviews.append(view)
    elif kind == "reviewer" and mine is not None:
        for item in reviews_for_version(con, row):
            view = _review_public(item)
            view["mine"] = int(item.get("reviewer_id") or 0) == flags["user_id"]
            reviews.append(view)
    elif kind == "author":
        # 作者只看到结论与理由，不看他人的独立作答。
        for item in reviews_for_version(con, row):
            reviews.append({
                "verdict": str(item.get("verdict") or ""),
                "reviewer_role": str(item.get("reviewer_role") or ""),
                "reasons": str(item.get("reasons") or ""),
                "created_at": str(item.get("created_at") or ""),
            })

    return {
        "id": int(row["id"]),
        "status": status,
        "status_label": db.STATUS_LABELS.get(status, status),
        "version": int(row.get("version") or 1),
        "current_version": current_version,
        "question_id": row.get("question_id"),
        "subject": snapshot.get("subject") or "",
        "type": snapshot.get("type") or "",
        "grade": snapshot.get("grade") or "",
        "snapshot": snapshot,
        "source": source,
        "license_confirmed": license_confirmed,
        "fingerprint": str(row.get("fingerprint") or ""),
        "public_id": row.get("public_id"),
        "public_visible": status in db.PUBLIC_STATUSES and bool(row.get("public_id")),
        "review_deadline_at": row.get("review_deadline_at"),
        "deadline_passed": deadline_passed(row),
        "review_summary": visible_summary,
        "reviews": reviews,
        "my_review": _review_public(mine) if mine is not None else None,
        "appeals": appeals,
        "my_appeal": my_appeal,
        "checks": checks,
        "blocking": [check["key"] for check in blocking_checks(checks)],
        "warnings": [check["key"] for check in checks
                     if not check.get("blocking") and not check.get("ok")],
        "viewer": {
            "kind": kind,
            "is_author": flags["is_author"],
            "is_editor": flags["is_editor"],
            "is_reviewer": flags["is_reviewer"],
            "roles": sorted(flags["role_names"]),
        },
        "can": {
            "submit": flags["is_author"] and status == "draft",
            "revise": flags["is_author"] and status in db.REVISABLE_STATUSES,
            "patch": flags["is_author"] and status == "draft",
            "withdraw": flags["is_author"] and status in db.WITHDRAWABLE_STATUSES,
            "appeal": flags["is_author"] and status in db.APPEALABLE_STATUSES,
            "initial_review": flags["is_editor"] and not flags["is_author"] and status == "pending_initial",
            "review": flags["is_reviewer"] and not flags["is_author"] and status == "community_review",
            "publish": flags["is_editor"] and not flags["is_author"]
                       and status in ("verified", "pending_release"),
            "rollback": flags["is_editor"] and not flags["is_author"]
                        and status == "published",
        },
        "created_at": str(row.get("created_at") or ""),
        "updated_at": str(row.get("updated_at") or ""),
    }


def author_list_item(con, row: dict) -> dict:
    snapshot = db.json_load(row.get("snapshot_json"), {})
    if not isinstance(snapshot, dict):
        snapshot = {}
    status = str(row.get("status") or "")
    return {
        "id": int(row["id"]),
        "status": status,
        "status_label": db.STATUS_LABELS.get(status, status),
        "version": int(row.get("version") or 1),
        "current_version": int(row.get("current_version") or 1),
        "subject": snapshot.get("subject") or "",
        "type": snapshot.get("type") or "",
        "grade": snapshot.get("grade") or "",
        "stem_preview": stem_preview(snapshot),
        "public_id": row.get("public_id"),
        "public_visible": status in db.PUBLIC_STATUSES and bool(row.get("public_id")),
        "review_deadline_at": row.get("review_deadline_at"),
        "deadline_passed": deadline_passed(row),
        "review_summary": review_summary(con, row),
        "created_at": str(row.get("created_at") or ""),
        "updated_at": str(row.get("updated_at") or ""),
    }


# ---------------------------------------------------------------------------
# 公开投影（匿名只读；禁止内部身份与私有附件路径）
# ---------------------------------------------------------------------------

def public_content(content: dict, image_map: dict | None = None,
                   public_id: str = "") -> dict:
    """公开题面/答案/解析投影：只保留白名单字段。

    配图只输出**公开衍生图**的读取地址（``url``/``asset_id``/``question_image_url``）；
    无法公开的配图输出 ``accessible=false`` 与原因码，``figure_notice`` 由详情层统一给提示。
    绝不输出原始资产 ID、存储路径或他人私有资产信息。
    """
    content = content if isinstance(content, dict) else {}
    image_map = image_map if isinstance(image_map, dict) else {}
    parts = []
    for part in content.get("parts") or []:
        if not isinstance(part, dict):
            continue
        parts.append({
            "index": part.get("index"),
            "type": part.get("type") or "",
            "stem": part.get("stem") or "",
            "options": [
                {"label": option.get("label") or "", "text": option.get("text") or ""}
                for option in (part.get("options") or []) if isinstance(option, dict)
            ],
            "answers": [str(answer) for answer in (part.get("answers") or [])],
            "explanation": part.get("explanation") or "",
            "rubric": part.get("rubric") or "",
        })
    images = []
    for index, image in enumerate(content.get("images") or []):
        if not isinstance(image, dict):
            continue
        mapped = image_map.get(index) if image_map else None
        public_asset_id = str((mapped or {}).get("public_asset_id") or "").strip()
        accessible = (
            bool(mapped)
            and str(mapped.get("status") or "") == "active"
            and bool(public_asset_id)
        )
        entry = {"index": index, "accessible": accessible}
        if accessible:
            entry["asset_id"] = public_asset_id
            entry["media_type"] = str(mapped.get("media_type") or "")
            entry["url"] = f"/api/plaza/assets/{public_asset_id}"
            if public_id:
                entry["question_image_url"] = \
                    f"/api/plaza/questions/{public_id}/images/{index}"
        else:
            reason = str((mapped or {}).get("reason") or "")
            if not reason:
                reason = "unavailable"
            entry["reason"] = reason
        images.append(entry)
    result = {
        "stem": content.get("stem") or "",
        "options": [
            {"label": option.get("label") or "", "text": option.get("text") or ""}
            for option in (content.get("options") or []) if isinstance(option, dict)
        ],
        "answers": [str(answer) for answer in (content.get("answers") or [])],
        "explanation": content.get("explanation") or "",
        "rubric": content.get("rubric") or "",
        "parts": parts,
        "images": images,
    }
    if content.get("score") is not None:
        result["score"] = content["score"]
    return result


_PUBLIC_NOTICES = {
    "community_review": (
        "该题已通过初审，正在社区核验（社区待核验）；答案与解析按需展开，"
        "内容可能继续修订，请以最终核验结果为准。"
    ),
    "verified": "该题由社区评审核验通过，尚未经平台编辑签发精选；答案与解析仅供自测参考。",
    "published": "该题已由平台编辑签发为精选，答案与解析可在本页直接查看。",
    "disputed": (
        "该题在社区评审中被提出有效的答案争议、缺图或条件缺失等问题，"
        "已暂停公开展示结论；作者修订后将重新核验。"
    ),
    "withdrawn": "该题已撤回，公开内容不再展示；如需了解处理进度请联系平台编辑。",
}


def withdrawn_notice() -> str:
    """撤回后的公开提示文案（旧链接仍可看到清晰状态）。"""
    return _PUBLIC_NOTICES["withdrawn"]


def disputed_notice() -> str:
    """有争议/待修订时的公开提示：只说明暂停展示的原因口径，不泄露评审意见与身份。"""
    return _PUBLIC_NOTICES["disputed"]


def status_notice(status: str) -> str:
    """按投稿状态返回公开提示文案（收藏/通知等处复用同一口径）。"""
    return _PUBLIC_NOTICES.get(str(status or ""), "")


def public_review_progress(summary: dict, row: dict) -> dict:
    """公开评审进度摘要（只给聚合数，不含评审人身份与独立作答）。"""
    independent = int(summary.get("independent_reviews") or 0)
    approves = int(summary.get("approves") or 0)
    subject_approves = int(summary.get("subject_reviewer_approves") or 0)
    return {
        "independent_reviews": independent,
        "approves": approves,
        "revises": int(summary.get("revises") or 0),
        "rejects": int(summary.get("rejects") or 0),
        "unsure": int(summary.get("unsure") or 0),
        "subject_reviewer_approves": subject_approves,
        "threshold_met": bool(summary.get("threshold_met")),
        "min_reviews": db.THRESHOLD_MIN_REVIEWS,
        "min_approves": db.THRESHOLD_MIN_APPROVES,
        "min_subject_reviewer_approves": db.THRESHOLD_MIN_SUBJECT_APPROVES,
        "reviews_needed": max(0, db.THRESHOLD_MIN_REVIEWS - independent),
        "approves_needed": max(0, db.THRESHOLD_MIN_APPROVES - approves),
        "subject_approves_needed": max(
            0, db.THRESHOLD_MIN_SUBJECT_APPROVES - subject_approves,
        ),
        "deadline_at": str(row.get("review_deadline_at") or ""),
        "deadline_passed": deadline_passed(row),
    }


def _public_source_label(status: str) -> str:
    return "featured" if status == "published" else "community"


def public_payload(con, row: dict) -> dict:
    """公开详情：``community_review`` / ``verified`` / ``published`` 三种可见状态。"""
    from . import figures

    snapshot = db.json_load(row.get("snapshot_json"), {})
    if not isinstance(snapshot, dict):
        snapshot = {}
    source = db.json_load(row.get("source_json"), {})
    if not isinstance(source, dict):
        source = {}
    status = str(row.get("status") or "")
    public_id = str(row.get("public_id") or "")
    summary = review_summary(con, row)
    image_map = figures.public_figure_map(con, row)
    return {
        "public_id": public_id,
        "status": status,
        "status_label": db.STATUS_LABELS.get(status, status),
        "review_status": db.public_review_status(status),
        "source": _public_source_label(status),
        "featured": status == "published",
        "community_pending": status == "community_review",
        "visible": True,
        "subject": snapshot.get("subject") or "",
        "type": snapshot.get("type") or "",
        "grade": snapshot.get("grade") or "",
        "difficulty": snapshot.get("difficulty"),
        "knowledge_tags": list(snapshot.get("knowledge_tags") or []),
        "content": public_content(
            snapshot.get("content") or {}, image_map=image_map, public_id=public_id,
        ),
        "source_declaration": public_source(source),
        "review": public_review_progress(summary, row),
        "figure_notice": figures.figure_notice_from_map(image_map),
        "notice": _PUBLIC_NOTICES.get(status, ""),
    }


def public_list_item(con, row: dict) -> dict:
    snapshot = db.json_load(row.get("snapshot_json"), {})
    if not isinstance(snapshot, dict):
        snapshot = {}
    content = snapshot.get("content") if isinstance(snapshot.get("content"), dict) else {}
    status = str(row.get("status") or "")
    summary = review_summary(con, row)
    return {
        "public_id": str(row.get("public_id") or ""),
        "status": status,
        "status_label": db.STATUS_LABELS.get(status, status),
        "review_status": db.public_review_status(status),
        "source": _public_source_label(status),
        "featured": status == "published",
        "community_pending": status == "community_review",
        "subject": snapshot.get("subject") or "",
        "type": snapshot.get("type") or "",
        "grade": snapshot.get("grade") or "",
        "difficulty": snapshot.get("difficulty"),
        "knowledge_tags": list(snapshot.get("knowledge_tags") or []),
        "stem_preview": stem_preview(snapshot),
        "has_answer": bool(content.get("answers")),
        "has_figures": bool(content.get("images")),
        "review": public_review_progress(summary, row),
        "notice": _PUBLIC_NOTICES.get(status, ""),
    }


__all__ = [
    "SOURCE_TEXT_KEYS",
    "author_list_item",
    "blocking_checks",
    "build_snapshot",
    "can_view_submission",
    "checks_payload",
    "clean_content",
    "deadline_passed",
    "disputed_notice",
    "has_own_review",
    "image_check",
    "normalize_source",
    "public_content",
    "public_list_item",
    "public_payload",
    "public_review_progress",
    "public_source",
    "require_editor",
    "require_plaza_role",
    "require_reviewer",
    "review_summary",
    "reviews_for_version",
    "snapshot_checks",
    "snapshot_fingerprint",
    "source_checks",
    "status_notice",
    "stem_preview",
    "submission_detail",
    "withdrawn_notice",
]
