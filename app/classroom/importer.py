"""课程 ZIP 导入：把 ``app/classroom/exports.py`` 导出的课程包还原成当前用户的新课程。

契约 ``INTEGRATION-CONTRACT.md`` §4.1：``POST /api/classroom/import``（multipart ``.zip``，≤200MB）
→ 校验 manifest/课程结构/场景内容/素材 sha256 → 创建课程与场景（**不调用模型**）；损坏或冲突明确 422。

ZIP 结构（formatVersion=1）::

    manifest.json          # format/formatVersion/course/scenes/resources
    course.json            # 课程公开字段
    scenes/scene_001.json  # 每个场景：order/type/title/content/actions/meta
    assets/**              # 已落库素材（TTS 音频等），manifest.resources 登记 sha256

安全边界：条目名必须相对路径且不含 ``..``/盘符/反斜杠；条目数与解压总大小有上限；
素材逐个校验 sha256；**素材类型按内容解码识别，不采信 manifest 的 ``mimeType`` 声明**
（非白名单图片/音视频落为隔离下载附件，SVG/HTML 等主动内容降级为二进制）；
场景内容复用 DSL 归一化（交互 HTML 重新过静态安全校验），导入的课程一律归属当前登录用户。
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from ..auth import current_user
from .. import storage
from . import db as cdb
from . import images as image_slots, interactive, multiagent, pbl, schema

router = APIRouter()

#: 导入文件上限（压缩包字节数）。
MAX_IMPORT_BYTES = 200 * 1024 * 1024
MAX_ZIP_ENTRIES = 2000
MAX_UNCOMPRESSED_BYTES = 500 * 1024 * 1024
MAX_SCENES = 100
MAX_RESOURCES = 500
MAX_ASSET_BYTES = 100 * 1024 * 1024
MAX_SCENE_JSON_BYTES = 4 * 1024 * 1024
ZIP_FORMAT = "zhixu-classroom-zip"
SUPPORTED_FORMAT_VERSIONS = (1,)

_ASSET_SUFFIX = {
    "audio/mpeg": ".mp3", "audio/mp3": ".mp3", "audio/wav": ".wav", "audio/x-wav": ".wav",
    "audio/ogg": ".ogg", "audio/opus": ".opus", "audio/aac": ".aac", "audio/flac": ".flac",
    "image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif",
    "video/mp4": ".mp4", "application/pdf": ".pdf", "application/zip": ".zip",
    "text/plain": ".txt",
}


def _db():
    from .. import db

    return db


def _text(value, limit: int = 500) -> str:
    if value is None or isinstance(value, (dict, list, bool)):
        return ""
    return str(value).strip()[:limit]


def _int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# ZIP 读取与安全校验
# ---------------------------------------------------------------------------

def _check_member_name(name: str) -> str:
    text = str(name or "")
    if not text or text.startswith("/") or text.startswith("\\") or "\\" in text:
        raise HTTPException(422, "课程包包含非法文件路径")
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts) or ":" in parts[0]:
        raise HTTPException(422, "课程包包含非法文件路径")
    return "/".join(parts)


def _open_archive(data: bytes) -> zipfile.ZipFile:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, OSError):
        raise HTTPException(422, "上传的文件不是有效的 ZIP 课程包") from None
    infos = archive.infolist()
    if len(infos) > MAX_ZIP_ENTRIES:
        raise HTTPException(422, "课程包文件数量过多（上限 %d）" % MAX_ZIP_ENTRIES)
    total = 0
    for info in infos:
        _check_member_name(info.filename)
        total += int(info.file_size or 0)
    if total > MAX_UNCOMPRESSED_BYTES:
        raise HTTPException(422, "课程包解压后体积过大（上限 %d MB）" % (MAX_UNCOMPRESSED_BYTES // (1024 * 1024)))
    return archive


def _member(archive: zipfile.ZipFile, name: str, *, required: bool = True,
            limit: int = MAX_SCENE_JSON_BYTES) -> bytes | None:
    safe_name = _check_member_name(name)
    try:
        info = archive.getinfo(safe_name)
    except KeyError:
        if required:
            raise HTTPException(422, "课程包缺少文件：%s" % safe_name)
        return None
    if int(info.file_size or 0) > limit:
        raise HTTPException(422, "课程包内文件过大：%s" % safe_name)
    try:
        return archive.read(safe_name)
    except (zipfile.BadZipFile, OSError, RuntimeError):
        raise HTTPException(422, "课程包文件损坏：%s" % safe_name) from None


def _json_member(archive: zipfile.ZipFile, name: str) -> dict:
    raw = _member(archive, name)
    try:
        value = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError):
        raise HTTPException(422, "课程包内 %s 不是合法 JSON" % name) from None
    if not isinstance(value, dict):
        raise HTTPException(422, "课程包内 %s 必须是 JSON 对象" % name)
    return value


# ---------------------------------------------------------------------------
# 场景内容校验（复用 DSL；旧占位内容走兼容路径）
# ---------------------------------------------------------------------------

def _scene_element_ids(content: dict) -> list[str]:
    if not isinstance(content, dict):
        return []
    return schema.slide_element_ids(content)


def _validate_scene_content(scene_type: str, content: dict, title: str) -> tuple[dict, list[str]]:
    warnings: list[str] = []
    if scene_type == "slide":
        normalized, notes = schema.normalize_slide_content(content)
    elif scene_type == "quiz":
        normalized, notes = schema.normalize_quiz_content(content)
    elif scene_type == "interactive":
        widget = schema.normalize_widget(content.get("widget"), "simulation")
        try:
            normalized, notes = interactive.normalize_generated_interactive(
                content, title=title, widget=widget,
            )
        except schema.ClassroomSchemaError:
            normalized, notes = schema.normalize_interactive_content(content)
            warnings.append("交互场景内容按兼容模式导入（可能为旧版占位页）")
    elif scene_type == "pbl":
        try:
            normalized, notes = pbl.normalize_pbl_content(content)
        except schema.ClassroomSchemaError:
            normalized, notes = schema.normalize_pbl_content(content)
            warnings.append("PBL 项目按兼容模式导入（旧版结构，可能缺少目标/检查点）")
    else:  # pragma: no cover - 调用方已校验 scene_type
        raise HTTPException(422, "不支持的场景类型：%s" % scene_type)
    warnings.extend(notes)
    return normalized, warnings


def _prepare_scene(entry: dict, raw: dict, index: int) -> dict:
    scene_type = schema.normalize_scene_type(
        raw.get("scene_type") or raw.get("type") or entry.get("type"), default="")
    if scene_type not in schema.SCENE_TYPES:
        raise HTTPException(422, "第 %d 个场景类型不支持：%s" % (index, raw.get("scene_type") or raw.get("type")))
    title = _text(raw.get("title") or entry.get("title"), 120) or ("导入场景 %d" % index)
    content = raw.get("content")
    if not isinstance(content, dict):
        raise HTTPException(422, "第 %d 个场景缺少 content 对象" % index)
    normalized, warnings = _validate_scene_content(scene_type, content, title)
    actions_raw = raw.get("actions")
    meta_raw = raw.get("meta") if isinstance(raw.get("meta"), dict) else {}
    if not isinstance(actions_raw, list):
        actions_raw = meta_raw.get("actions") if isinstance(meta_raw.get("actions"), list) else []
    actions, action_warnings = schema.normalize_actions(
        actions_raw, element_ids=_scene_element_ids(normalized), max_actions=schema.MAX_ACTIONS,
    )
    warnings.extend(action_warnings)
    meta: dict = {}
    for key in ("prompt_version", "model", "digest"):
        value = meta_raw.get(key)
        if isinstance(value, str) and value.strip():
            meta[key] = value.strip()[:500]
    for key in ("position", "total"):
        value = meta_raw.get(key)
        if isinstance(value, int):
            meta[key] = value
    meta["placeholder"] = bool(meta_raw.get("placeholder"))
    meta["requires_generation"] = bool(meta_raw.get("requires_generation") or meta["placeholder"])
    meta["actions"] = actions
    meta["warnings"] = warnings[:40]
    meta["imported"] = True
    return {
        "order_index": int(_int(entry.get("order") or raw.get("order_index") or raw.get("order"), index)),
        "scene_type": scene_type,
        "title": title[:120],
        "content": normalized,
        "meta": meta,
    }


# ---------------------------------------------------------------------------
# 素材
# ---------------------------------------------------------------------------

def _prepare_resources(archive: zipfile.ZipFile, prefix: str, manifest: dict) -> list[dict]:
    raw_resources = manifest.get("resources")
    if raw_resources is None:
        return []
    if not isinstance(raw_resources, list):
        raise HTTPException(422, "manifest.resources 必须是数组")
    if len(raw_resources) > MAX_RESOURCES:
        raise HTTPException(422, "课程包素材数量过多（上限 %d）" % MAX_RESOURCES)
    prepared: list[dict] = []
    seen: set[str] = set()
    for item in raw_resources:
        if not isinstance(item, dict):
            raise HTTPException(422, "manifest.resources 存在非法条目")
        path = _check_member_name(prefix + _text(item.get("path"), 300))
        if path in seen:
            continue
        seen.add(path)
        data = _member(archive, path, limit=MAX_ASSET_BYTES)
        if data is None:  # pragma: no cover - required=True 时不会返回 None
            continue
        sha = _text(item.get("sha256"), 64).lower()
        if sha and hashlib.sha256(data).hexdigest() != sha:
            raise HTTPException(422, "课程包素材校验失败（sha256 不匹配）：%s" % path)
        # 类型控制：按内容解码识别真实类型，不采信 manifest 里的 mimeType 声明；
        # 非白名单（SVG/HTML/PDF 等）落为隔离下载附件，主动内容再降级为二进制。
        classification = image_slots.classify_import_media(item.get("mimeType"), data)
        kind = re.sub(r"[^a-z0-9_-]+", "-", _text(item.get("kind"), 40).lower()) or "file"
        prepared.append({
            "data": data,
            "sha256": sha or hashlib.sha256(data).hexdigest(),
            "mime_type": classification["mime_type"],
            "declared_mime": classification["declared_mime"],
            "detected_mime": classification["detected_mime"],
            "inline_allowed": classification["inline_allowed"],
            "warnings": classification["warnings"],
            "kind": kind,
            "path": path,
            "asset_id": _int(item.get("assetId")),
        })
    return prepared


def _write_asset_files(resources: list[dict]) -> list[dict]:
    """把素材写进本平台私有存储（内容寻址命名），返回可落库的元数据。"""
    written: list[dict] = []
    for resource in resources:
        digest = resource["sha256"]
        suffix = _ASSET_SUFFIX.get(str(resource["mime_type"]).split(";")[0].strip().lower(), ".bin")
        storage_name = "imported-%s%s" % (digest[:32], suffix)
        try:
            storage.save_file(cdb.ASSET_FOLDER, storage_name, resource["data"], resource["mime_type"])
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(503, "课程包素材写入失败，请稍后重试") from None
        written.append({**resource, "storage_name": storage_name})
    return written


# ---------------------------------------------------------------------------
# 导入主流程
# ---------------------------------------------------------------------------

def _course_from_payload(course_data: dict, manifest: dict) -> dict:
    manifest_course = manifest.get("course") if isinstance(manifest.get("course"), dict) else {}
    title = _text(course_data.get("title") or manifest_course.get("title"), 60)
    topic = _text(course_data.get("topic") or manifest_course.get("topic") or title, 500)
    if len(topic) < 2:
        topic = (title or "导入课程")[:500]
        if len(topic) < 2:
            topic = "导入课程"
    if not title:
        title = topic[:30]
    mode = _text(course_data.get("mode") or manifest_course.get("mode"), 20)
    if not mode and manifest.get("interactiveMode"):
        mode = "interactive"
    if mode not in schema.MODES:
        mode = "standard"
    outline = course_data.get("outline") if isinstance(course_data.get("outline"), dict) else {}
    config = course_data.get("config") if isinstance(course_data.get("config"), dict) else {}
    config = dict(config)
    config.pop("document_ids", None)
    # 角色名册：优先 manifest.agents，其次 course.json 的 config.agents；一律重新校验后再落库。
    raw_roster = manifest.get("agents")
    if not isinstance(raw_roster, dict):
        raw_roster = config.get(multiagent.CONFIG_KEY)
    config.pop(multiagent.CONFIG_KEY, None)
    config.pop(multiagent.CONFIG_META_KEY, None)
    if isinstance(raw_roster, dict):
        try:
            roster, _roster_warnings = multiagent.normalize_agent_roster(raw_roster)
        except (schema.ClassroomSchemaError, ValueError):
            roster = None
        if roster:
            config[multiagent.CONFIG_KEY] = roster
            config[multiagent.CONFIG_META_KEY] = {
                "prompt_version": _text(manifest.get("agentsPromptVersion"), 60) or multiagent.PROMPT_VERSION,
                "imported": True,
                "agent_count": len(multiagent.roster_agents(roster)),
            }
    config["imported"] = {
        "format": ZIP_FORMAT,
        "format_version": _int(manifest.get("formatVersion"), 1),
        "exported_at": _text(manifest.get("exportedAt"), 60),
        "source_course_id": _int(manifest.get("courseId")),
    }
    cover = course_data.get("cover") if isinstance(course_data.get("cover"), dict) else {}
    return {
        "title": title,
        "topic": topic,
        "subject": _text(course_data.get("subject") or manifest_course.get("subject"), 40),
        "grade": _text(course_data.get("grade") or manifest_course.get("grade"), 40),
        "mode": mode,
        "description": _text(course_data.get("description"), 600),
        "requirements": _text(course_data.get("requirements"), 4000),
        "outline": outline,
        "config": config,
        "cover": cover,
    }


def _remap_scene_assets(content, asset_map: dict[int, int]) -> dict:
    """导入时把 slide image 元素的素材引用（asset_id / /api/classroom/assets/N）映射到新素材 id。"""
    if not asset_map or not isinstance(content, dict):
        return content
    canvas = content.get("canvas")
    if not isinstance(canvas, dict):
        return content
    elements = canvas.get("elements")
    if not isinstance(elements, list):
        return content
    for element in elements:
        if not isinstance(element, dict) or element.get("type") != "image":
            continue
        try:
            old_id = int(element.get("asset_id") or 0)
        except (TypeError, ValueError):
            old_id = 0
        if old_id <= 0:
            match = _ASSET_URL_RE.search(str(element.get("src") or ""))
            if match:
                old_id = int(match.group(1))
            elif str(element.get("src") or "").strip().isdigit():
                old_id = int(str(element.get("src")).strip())
        new_id = asset_map.get(old_id)
        if new_id:
            element["asset_id"] = int(new_id)
            element["src"] = "/api/classroom/assets/%d" % int(new_id)
    return content


_ASSET_URL_RE = re.compile(r"/api/classroom/assets/(\d+)")


def _insert_course(user_id: int, course: dict, scenes: list[dict], resources: list[dict]) -> tuple[int, int]:
    now = _db().utcnow()
    status = "ready" if scenes else "draft"
    with _db().connection() as con:
        _db().write_lock(con, "classroom-course")
        course_id = con.execute(
            "INSERT INTO cls_courses(user_id,title,topic,subject,grade,mode,description,requirements,status,"
            "outline_json,config_json,scene_count,cover_json,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                user_id, course["title"], course["topic"], course["subject"], course["grade"],
                course["mode"], course["description"], course["requirements"], status,
                cdb.json_dump(course["outline"]), cdb.json_dump(course["config"]),
                len(scenes), cdb.json_dump(course["cover"]), now, now,
            ),
        ).lastrowid
        # 先落素材，建立「包内旧素材 id → 新素材 id」映射，供场景图片引用回链。
        asset_map: dict[int, int] = {}
        for resource in resources:
            meta = {"imported": True, "source_path": resource["path"]}
            declared_mime = str(resource.get("declared_mime") or "")
            detected_mime = str(resource.get("detected_mime") or "")
            if declared_mime and declared_mime != resource["mime_type"]:
                meta["declared_mime_type"] = declared_mime
            if detected_mime:
                meta["detected_mime_type"] = detected_mime
            if resource.get("inline_allowed") is False:
                # 非白名单附件（SVG/HTML/PDF 等）：只允许隔离下载，不内联。
                meta["inline_allowed"] = False
            if resource.get("warnings"):
                meta["type_warnings"] = list(resource["warnings"])[:4]
            new_asset_id = con.execute(
                "INSERT INTO cls_assets(user_id,course_id,kind,storage_name,mime_type,sha256,meta_json,created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    user_id, course_id, resource["kind"], resource["storage_name"],
                    resource["mime_type"], resource["sha256"],
                    cdb.json_dump(meta), now,
                ),
            ).lastrowid
            source_asset_id = _int(resource.get("asset_id"))
            if source_asset_id > 0:
                asset_map[source_asset_id] = int(new_asset_id)
        inserted: dict[int, int] = {}
        for scene in scenes:
            order_index = int(scene["order_index"])
            while order_index in inserted:
                order_index += 1
            content = _remap_scene_assets(scene["content"], asset_map)
            inserted[order_index] = con.execute(
                "INSERT INTO cls_scenes(course_id,user_id,scene_type,order_index,title,content_json,meta_json,"
                "status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,'ready',?,?)",
                (
                    course_id, user_id, scene["scene_type"], order_index, scene["title"],
                    cdb.json_dump(content), cdb.json_dump(scene["meta"]), now, now,
                ),
            ).lastrowid
        return int(course_id), len(inserted)


async def _read_upload(file: UploadFile) -> bytes:
    filename = str(getattr(file, "filename", "") or "").strip()
    if filename and not filename.lower().endswith(".zip"):
        raise HTTPException(422, "请上传 .zip 课程包")
    try:
        data = await file.read(MAX_IMPORT_BYTES + 1)
    except Exception:
        raise HTTPException(422, "读取上传文件失败") from None
    if not data:
        raise HTTPException(422, "上传的课程包为空")
    if len(data) > MAX_IMPORT_BYTES:
        raise HTTPException(413, "课程包不能超过 %d MB" % (MAX_IMPORT_BYTES // (1024 * 1024)))
    return data


@router.post("/import")
@router.post("/courses/import")
async def import_course(file: UploadFile = File(...), user=Depends(current_user)):
    """导入课程 ZIP：创建新课程与场景（不执行模型）。"""
    data = await _read_upload(file)
    archive = _open_archive(data)
    try:
        names = {info.filename for info in archive.infolist()}
        manifest_name = "manifest.json" if "manifest.json" in names else next(
            (name for name in sorted(names) if name.endswith("/manifest.json")), None)
        if not manifest_name:
            raise HTTPException(422, "课程包缺少 manifest.json")
        prefix = manifest_name[: -len("manifest.json")]
        manifest = _json_member(archive, manifest_name)
        if _text(manifest.get("format"), 60) != ZIP_FORMAT:
            raise HTTPException(422, "不是知序 AI 课堂课程包（manifest.format 不符）")
        version = _int(manifest.get("formatVersion"), 0)
        if version not in SUPPORTED_FORMAT_VERSIONS:
            raise HTTPException(422, "课程包版本不受支持（formatVersion=%s）" % (version or "未知"))
        course_data = _json_member(archive, prefix + "course.json")
        entries = manifest.get("scenes")
        if not isinstance(entries, list) or not entries:
            raise HTTPException(422, "课程包 manifest.scenes 为空，无法导入课程")
        if len(entries) > MAX_SCENES:
            raise HTTPException(422, "课程包场景数量过多（上限 %d）" % MAX_SCENES)
        scenes: list[dict] = []
        for index, entry in enumerate(entries, 1):
            if not isinstance(entry, dict):
                raise HTTPException(422, "课程包 manifest.scenes 存在非法条目")
            scene_path = _text(entry.get("path"), 300) or ("scenes/scene_%03d.json" % index)
            raw = _json_member(archive, prefix + scene_path)
            scenes.append(_prepare_scene(entry, raw, index))
        resources = _prepare_resources(archive, prefix, manifest)
    finally:
        archive.close()
    resources = _write_asset_files(resources)
    course = _course_from_payload(course_data, manifest)
    course_id, scene_count = _insert_course(user["id"], course, scenes, resources)
    stored = cdb.owned_course(course_id, user["id"])
    asset_warnings = [warning for resource in resources for warning in (resource.get("warnings") or [])]
    return {
        "course": cdb.public_course(stored),
        "scene_count": scene_count,
        "imported_assets": len(resources),
        # 新增字段（只加不改）：素材类型声明与内容不符时的中文提示，导入本身照常完成。
        "asset_warnings": asset_warnings[:20],
    }


__all__ = [
    "MAX_IMPORT_BYTES", "SUPPORTED_FORMAT_VERSIONS", "ZIP_FORMAT", "import_course", "router",
]
