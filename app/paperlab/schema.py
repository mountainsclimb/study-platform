"""试卷工坊结构校验与规范化。

两处复用：
- 识别导入：``normalize_page_result`` 校验视觉模型返回的页内 JSON（严格，失败让
  ``call_structured`` 重试一次）；
- AI 补全：``validate_enrichment`` 校验答案/解析/评分标准/知识点，并保持 MIXED 子题结构。

所有 ``ValueError`` 的消息都会以中文反馈给模型（重试提示）或用户，不包含内部路径。
"""
from __future__ import annotations

from difflib import SequenceMatcher
import re
import unicodedata

from .assets import normalize_asset_ref
from .db import (
    CHOICE_TYPES,
    DIFFICULTY_ALIASES,
    MAX_OPTIONS,
    MAX_PARTS,
    MAX_STEM_CHARS,
    MAX_TAGS,
    Q_TYPE_ALIASES,
    Q_TYPES,
    VERIFY_STEM_THRESHOLD,
    json_load,
)

_LABEL_RE = re.compile(r"^[A-Za-zＡ-Ｚａ-ｚ0-9一二三四五六七八九十]{1,4}$")
_OPTION_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"

# 统一题目引用（方案 §6.2）：``bank:<qid>`` / ``app:<id>`` / ``official:<id>``。
# qid 允许字母数字、下划线、点、冒号与连字符；拒绝空白、路径分隔符与上跳目录。
_SOURCE_REF_RE = re.compile(r"^[A-Za-z0-9_一-鿿][A-Za-z0-9_.:\-一-鿿]{0,299}$")
_SOURCE_PREFIXES = ("bank", "app", "official", "official_plaza")


def normalize_bank_qid(value) -> str | None:
    """校验只读题库 QID（如 ``rwk:rwk-0c02738f716b70:14-1``）；非法返回 None。"""
    if value is None or isinstance(value, (dict, list)):
        return None
    text = str(value).strip()
    if not text or len(text) > 300:
        return None
    if not _SOURCE_REF_RE.match(text) or ".." in text:
        return None
    return text


def normalize_source_ref(value) -> str | None:
    """校验统一题目引用 ``bank:<qid>`` / ``app:<id>``；非法返回 None。"""
    if value is None or isinstance(value, (dict, list)):
        return None
    text = str(value).strip()
    if not text or len(text) > 320:
        return None
    prefix, sep, rest = text.partition(":")
    if not sep or prefix not in _SOURCE_PREFIXES:
        return None
    rest = rest.strip()
    if not rest or not _SOURCE_REF_RE.match(rest) or ".." in rest:
        return None
    return f"{prefix}:{rest}"


# ---------------------------------------------------------------------------
# 基础类型归一化
# ---------------------------------------------------------------------------

def safe_int(value, default: int | None = None) -> int | None:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    try:
        text = str(value).strip()
        if not text:
            return default
        return int(float(text))
    except (TypeError, ValueError):
        return default


def safe_float(value, default: float | None = None) -> float | None:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    try:
        text = str(value).strip().replace("%", "")
        if not text:
            return default
        return float(text)
    except (TypeError, ValueError):
        return default


def normalize_qtype(value) -> str | None:
    if not isinstance(value, str):
        return None
    return Q_TYPE_ALIASES.get(value.strip())


def normalize_difficulty(value) -> float | None:
    """难度归一到 0~1；兼容 0-5 / 0-10 分制与「简单/中等/困难」标签。"""
    if isinstance(value, str):
        label = value.strip()
        if label in DIFFICULTY_ALIASES:
            return DIFFICULTY_ALIASES[label]
        number = safe_float(label)
    else:
        number = safe_float(value)
    if number is None:
        return None
    if number < 0:
        return None
    if number > 1:
        if number <= 5:
            number = number / 5.0
        elif number <= 10:
            number = number / 10.0
        else:
            return None
    return round(max(0.0, min(1.0, number)), 3)


def normalize_tags(raw, *, limit: int = MAX_TAGS) -> list[str]:
    """知识点标签：去空、去重、限制数量；也接受逗号分隔字符串。"""
    items: list[str] = []
    if isinstance(raw, str):
        items = re.split(r"[,，、;；\n]+", raw)
    elif isinstance(raw, (list, tuple, set)):
        items = list(raw)
    tags: list[str] = []
    seen: set[str] = set()
    for item in items:
        tag = str(item).strip()[:80]
        if not tag or tag in seen:
            continue
        seen.add(tag)
        tags.append(tag)
        if len(tags) >= limit:
            break
    return tags


# ---------------------------------------------------------------------------
# bbox / 选项 / 答案
# ---------------------------------------------------------------------------

def normalize_bbox(raw, *, width: int = 0, height: int = 0, label: str = "题目"):
    """把模型/人工给的 bbox 归一化为 {x,y,w,h}（图片左上角为原点，0~1）。

    兼容 ``{x,y,w,h}``、``{left,top,width,height}``、``{x1,y1,x2,y2}`` 与像素坐标
    （提供页面宽高时自动除以页面尺寸）。非法坐标抛 ValueError（触发模型重试）。
    """
    if not isinstance(raw, dict):
        raise ValueError(f"{label}缺少 bbox 坐标")
    x = safe_float(raw.get("x"))
    if x is None:
        x = safe_float(raw.get("left"))
    if x is None:
        x = safe_float(raw.get("x1"))
        if x is None:
            x = safe_float(raw.get("x_min"))
    y = safe_float(raw.get("y"))
    if y is None:
        y = safe_float(raw.get("top"))
    if y is None:
        y = safe_float(raw.get("y1"))
        if y is None:
            y = safe_float(raw.get("y_min"))
    w = safe_float(raw.get("w"))
    if w is None:
        w = safe_float(raw.get("width"))
    if w is None:
        x2 = safe_float(raw.get("x2"))
        if x2 is None:
            x2 = safe_float(raw.get("right"))
        if x2 is None:
            x2 = safe_float(raw.get("x_max"))
        if x is not None and x2 is not None:
            w = x2 - x
    h = safe_float(raw.get("h"))
    if h is None:
        h = safe_float(raw.get("height"))
    if h is None:
        y2 = safe_float(raw.get("y2"))
        if y2 is None:
            y2 = safe_float(raw.get("bottom"))
        if y2 is None:
            y2 = safe_float(raw.get("y_max"))
        if y is not None and y2 is not None:
            h = y2 - y
    if x is None or y is None or w is None or h is None:
        raise ValueError(f"{label}缺少完整的 bbox（需要 x/y/w/h）")
    if w <= 0 or h <= 0:
        raise ValueError(f"{label}的 bbox 宽高必须大于 0")
    # 像素坐标自动换算为归一化坐标（视觉模型偶尔直接输出像素框）。
    if width > 0 and height > 0 and max(abs(x), abs(y), abs(w), abs(h)) > 1.5:
        if x > width * 1.05 or y > height * 1.05 or w > width * 1.05 or h > height * 1.05:
            raise ValueError(f"{label}的 bbox 超出页面范围")
        x, y, w, h = x / width, y / height, w / width, h / height
    x = max(0.0, min(1.0, x))
    y = max(0.0, min(1.0, y))
    w = max(0.0, min(1.0 - x, w))
    h = max(0.0, min(1.0 - y, h))
    if w < 0.002 or h < 0.002:
        raise ValueError(f"{label}的 bbox 退化（宽或高过小）")
    return {"x": round(x, 4), "y": round(y, 4), "w": round(w, 4), "h": round(h, 4)}


def _normalize_label(raw, index: int) -> str:
    label = str(raw or "").strip().upper()
    label = label.replace("．", "").replace(".", "").replace("、", "")
    if _LABEL_RE.match(label):
        return label[:4]
    return _OPTION_LETTERS[index] if index < len(_OPTION_LETTERS) else str(index + 1)


def normalize_options(raw) -> list[dict]:
    """选项归一化为 ``[{label,text}]``，label 唯一，最多 12 个。"""
    if raw is None:
        return []
    if not isinstance(raw, (list, tuple)):
        raise ValueError("options 必须是数组")
    options: list[dict] = []
    seen: set[str] = set()
    for index, item in enumerate(raw[:MAX_OPTIONS]):
        if isinstance(item, dict):
            label = _normalize_label(item.get("label") or item.get("key"), index)
            text = str(item.get("text") if item.get("text") is not None else item.get("content") or "").strip()
        else:
            label = _OPTION_LETTERS[index] if index < len(_OPTION_LETTERS) else str(index + 1)
            text = str(item or "").strip()
        if not text:
            raise ValueError(f"选项 {label} 缺少内容")
        if label in seen:
            raise ValueError(f"选项标号重复：{label}")
        seen.add(label)
        options.append({"label": label, "text": text[:4000]})
    return options


def normalize_answers(raw) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        text = raw.strip()
        return [text] if text else []
    if not isinstance(raw, (list, tuple)):
        raise ValueError("answers 必须是数组")
    answers: list[str] = []
    for item in raw[:MAX_OPTIONS * 4]:
        if isinstance(item, dict):
            item = item.get("answer") if item.get("answer") is not None else item.get("text") or ""
        text = str(item if item is not None else "").strip()
        if text:
            answers.append(text[:4000])
    return answers


def _option_labels(options: list[dict]) -> set[str]:
    return {str(option.get("label") or "").strip().upper() for option in options if option.get("label")}


def validate_choice_answers(qtype: str, options: list[dict], answers: list[str], *, where: str = "") -> None:
    """选择题答案必须落在选项 label 上；单选恰好 1 个。答案为空时不校验（识别草稿允许无答案）。"""
    if qtype not in CHOICE_TYPES or not answers:
        return
    labels = _option_labels(options)
    if not labels:
        raise ValueError(f"{where}选择题缺少选项")
    cleaned = [str(answer).strip().upper() for answer in answers]
    unknown = [answer for answer in cleaned if answer not in labels]
    if unknown:
        raise ValueError(
            f"{where}选择题答案 {'/'.join(unknown)} 不在选项 {'/'.join(sorted(labels))} 中"
        )
    if qtype == "SINGLE" and len(cleaned) > 1:
        raise ValueError(f"{where}单选题只能有一个答案")


# ---------------------------------------------------------------------------
# 题目内容
# ---------------------------------------------------------------------------

def _as_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return ""
    return str(value).strip()


def normalize_part(raw, index: int, *, width: int = 0, height: int = 0,
                   require_answers: bool = False) -> dict:
    if not isinstance(raw, dict):
        raise ValueError(f"第 {index + 1} 个子题必须是 JSON 对象")
    qtype = normalize_qtype(raw.get("type")) or "SUBJECTIVE"
    options = normalize_options(raw.get("options"))
    if qtype in CHOICE_TYPES and not options:
        raise ValueError(f"第 {index + 1} 个子题（{Q_TYPES[qtype]}）缺少选项")
    answers = normalize_answers(raw.get("answers"))
    validate_choice_answers(qtype, options, answers, where=f"第 {index + 1} 个子题")
    if require_answers and not answers:
        raise ValueError(f"第 {index + 1} 个子题缺少答案")
    part = {
        "index": safe_int(raw.get("index"), index + 1) or (index + 1),
        "type": qtype,
        "stem": _as_text(raw.get("stem"))[:MAX_STEM_CHARS],
        "options": options,
        "answers": answers,
        "explanation": _as_text(raw.get("explanation")),
        "rubric": _as_text(raw.get("rubric")),
        "knowledge_tags": normalize_tags(raw.get("knowledge_tags")),
        "score": safe_float(raw.get("score")),
    }
    return part


def _normalize_question_image(raw, *, width: int, height: int, strict_bbox: bool,
                              label: str) -> dict:
    if not isinstance(raw, dict):
        raise ValueError(f"{label}必须是 JSON 对象")
    image: dict = {}
    asset = _as_text(raw.get("asset"))
    if asset:
        # P0：配图引用只能来自服务端签发的 asset（或已登记迁移的旧文件名 token），
        # 拒绝路径/空白/上跳目录等客户端构造值；归属校验在 bank 写入层完成。
        normalized_asset = normalize_asset_ref(asset)
        if not normalized_asset:
            raise ValueError(f"{label}的配图引用非法")
        image["asset"] = normalized_asset
    bbox_raw = raw.get("bbox")
    if bbox_raw is not None or strict_bbox:
        image["bbox"] = normalize_bbox(bbox_raw, width=width, height=height, label=label)
    role = _as_text(raw.get("role"))
    if role:
        image["role"] = role[:40]
    return image


def normalize_question_content(raw, qtype: str, *, width: int = 0, height: int = 0,
                               strict_bbox: bool = False) -> dict:
    """把题目 content 归一化为契约结构（stem/options/answers/explanation/rubric/parts/images）。"""
    if not isinstance(raw, dict):
        raise ValueError("content 必须是 JSON 对象")
    content = dict(raw)
    stem = _as_text(raw.get("stem"))
    if not stem:
        raise ValueError("题干不能为空")
    if len(stem) > MAX_STEM_CHARS:
        raise ValueError(f"题干超过 {MAX_STEM_CHARS} 字上限")
    content["stem"] = stem

    options = normalize_options(raw.get("options"))
    if qtype in CHOICE_TYPES and not options:
        raise ValueError(f"{Q_TYPES.get(qtype, qtype)}缺少选项")
    content["options"] = options

    answers = normalize_answers(raw.get("answers"))
    validate_choice_answers(qtype, options, answers)
    content["answers"] = answers

    content["explanation"] = _as_text(raw.get("explanation"))
    content["rubric"] = _as_text(raw.get("rubric"))

    raw_parts = raw.get("parts")
    parts: list[dict] = []
    if raw_parts:
        if not isinstance(raw_parts, list):
            raise ValueError("parts 必须是数组")
        if len(raw_parts) > MAX_PARTS:
            raise ValueError(f"子题数量超过 {MAX_PARTS} 个上限")
        for index, part in enumerate(raw_parts):
            parts.append(normalize_part(part, index, width=width, height=height))
    if qtype == "MIXED" and not parts:
        raise ValueError("混合题必须包含子题（parts）")
    if qtype != "MIXED" and parts:
        raise ValueError("非混合题不能包含子题（parts）")
    content["parts"] = parts

    raw_images = raw.get("images")
    images: list[dict] = []
    if raw_images is not None:
        if not isinstance(raw_images, list):
            raise ValueError("images 必须是数组")
        for index, image in enumerate(raw_images[:20]):
            images.append(_normalize_question_image(
                image, width=width, height=height, strict_bbox=strict_bbox,
                label=f"第 {index + 1} 张配图",
            ))
    content["images"] = images

    bbox = raw.get("bbox")
    if bbox is not None:
        content["bbox"] = normalize_bbox(bbox, width=width, height=height, label="题目")
    source_page = safe_int(raw.get("source_page"))
    if source_page is not None and source_page > 0:
        content["source_page"] = source_page
    score = safe_float(raw.get("score"))
    if score is not None and 0 <= score <= 1000:
        content["score"] = round(score, 2)
    confidence = safe_float(raw.get("confidence"))
    if confidence is not None:
        content["confidence"] = round(max(0.0, min(1.0, confidence)), 3)
    return content


def normalize_draft(raw, *, page_no: int, width: int = 0, height: int = 0,
                    default_subject: str = "", default_grade: str = "") -> dict:
    """校验并归一化单题识别草稿（严格：bbox 必填、清点/选项/答案 label 合法）。"""
    if not isinstance(raw, dict):
        raise ValueError("题目必须是 JSON 对象")
    qtype = normalize_qtype(raw.get("type"))
    if qtype is None:
        raise ValueError(f"未知题型：{raw.get('type')!r}")
    if not isinstance(raw.get("bbox"), dict):
        raise ValueError("缺少题目 bbox 坐标")
    content = normalize_question_content(
        raw, qtype, width=width, height=height, strict_bbox=True,
    )
    subject = _as_text(raw.get("subject")) or default_subject
    grade = _as_text(raw.get("grade")) or default_grade
    draft = {
        "page_no": int(page_no),
        "bbox": content["bbox"],
        "type": qtype,
        "subject": subject[:40],
        "grade": grade[:40],
        "stem": content["stem"],
        "options": content["options"],
        "answers": content["answers"],
        "explanation": content["explanation"],
        "rubric": content["rubric"],
        "knowledge_tags": normalize_tags(raw.get("knowledge_tags")),
        "difficulty": normalize_difficulty(raw.get("difficulty")),
        "score": content.get("score"),
        "images": content["images"],
        "parts": content["parts"],
        "confidence": safe_float(raw.get("confidence")),
    }
    if draft["confidence"] is not None:
        draft["confidence"] = round(max(0.0, min(1.0, draft["confidence"])), 3)
    source_page = safe_int(raw.get("source_page"))
    draft["source_page"] = source_page if source_page and source_page > 0 else int(page_no)
    return draft


def normalize_skip(raw, *, page_no: int) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("skipped 条目必须是 JSON 对象")
    reason = _as_text(raw.get("reason")) or "无法辨认"
    skipped = {
        "page_no": int(page_no),
        "reason": reason[:500],
    }
    if isinstance(raw.get("bbox"), dict):
        skipped["bbox"] = normalize_bbox(raw["bbox"], label="跳过题")
    source_page = safe_int(raw.get("page_no"))
    skipped["source_page"] = source_page if source_page and source_page > 0 else int(page_no)
    return skipped


def normalize_page_result(parsed, *, page_no: int, width: int = 0, height: int = 0,
                         subject: str = "", grade: str = "") -> dict:
    """校验单页视觉识别结果（page_inventory / questions / skipped）。

    清点必须与「识别数 + 跳过数」一致（照 OMP record_source_inventory 规则），
    不合法抛 ValueError，由 ``call_structured`` 带错误原因重试一次。
    """
    if not isinstance(parsed, dict):
        raise ValueError("识别结果不是 JSON 对象")
    questions = parsed.get("questions")
    if questions is None:
        raise ValueError("识别结果缺少 questions 数组")
    if not isinstance(questions, list):
        raise ValueError("questions 必须是数组")
    skipped_raw = parsed.get("skipped")
    if skipped_raw is None:
        skipped_raw = []
    if not isinstance(skipped_raw, list):
        raise ValueError("skipped 必须是数组")
    if parsed.get("page_inventory") is None or not isinstance(parsed.get("page_inventory"), dict):
        raise ValueError("识别结果缺少 page_inventory 页内清点")
    inventory = parsed["page_inventory"]
    qcount = safe_int(inventory.get("question_count"))
    scount = safe_int(inventory.get("skip_count"))
    if qcount is None or qcount < 0:
        raise ValueError("page_inventory.question_count 必须是非负整数")
    if scount is None or scount < 0:
        raise ValueError("page_inventory.skip_count 必须是非负整数")
    if qcount != len(questions) + len(skipped_raw):
        raise ValueError(
            f"页内清点数量 {qcount} 与识别结果不一致：识别 {len(questions)} 题 + 跳过 "
            f"{len(skipped_raw)} 题"
        )
    if scount != len(skipped_raw):
        raise ValueError(f"page_inventory.skip_count={scount} 与 skipped 数组长度 {len(skipped_raw)} 不一致")
    if len(questions) > 40:
        raise ValueError("单页识别题目数量超过 40 题上限")

    normalized_questions: list[dict] = []
    for index, raw in enumerate(questions):
        try:
            normalized_questions.append(normalize_draft(
                raw, page_no=page_no, width=width, height=height,
                default_subject=subject, default_grade=grade,
            ))
        except ValueError as exc:
            raise ValueError(f"第 {index + 1} 题格式不正确：{exc}") from None
    normalized_skipped: list[dict] = []
    for index, raw in enumerate(skipped_raw):
        try:
            normalized_skipped.append(normalize_skip(raw, page_no=page_no))
        except ValueError as exc:
            raise ValueError(f"第 {index + 1} 个跳过项格式不正确：{exc}") from None
    return {
        "page_inventory": {"question_count": qcount, "skip_count": scount},
        "questions": normalized_questions,
        "skipped": normalized_skipped,
    }


# ---------------------------------------------------------------------------
# AI 补全（ENRICH）校验
# ---------------------------------------------------------------------------

def _part_by_index(parts: list) -> dict[int, dict]:
    result: dict[int, dict] = {}
    for position, part in enumerate(parts):
        if not isinstance(part, dict):
            continue
        index = safe_int(part.get("index"), position + 1) or (position + 1)
        result[index] = part
    return result


def validate_enrichment(parsed, question: dict) -> dict:
    """校验 ENRICH 模型输出（照 OMP 硬校验）：

    - 每题/每子题答案非空；选择题答案落在选项 label；
    - 解析非空；主观题必须给 rubric；
    - 填空题保持「每空一个答案」的数量；
    - MIXED 必须保留全部子题（数量/顺序一致），子题结构不被改写。
    """
    if not isinstance(parsed, dict):
        raise ValueError("补全结果不是 JSON 对象")
    qtype = question.get("type") or "SUBJECTIVE"
    content = json_load(question.get("content_json"), {})
    if not isinstance(content, dict):
        content = {}
    options = content.get("options") if isinstance(content.get("options"), list) else []
    original_answers = content.get("answers") if isinstance(content.get("answers"), list) else []

    answers = normalize_answers(parsed.get("answers"))
    validate_choice_answers(qtype, options, answers, where="补全结果")
    explanation = _as_text(parsed.get("explanation"))
    if not explanation:
        raise ValueError("补全结果缺少解析（explanation）")
    rubric = _as_text(parsed.get("rubric"))
    knowledge_tags = normalize_tags(parsed.get("knowledge_tags"))
    if not knowledge_tags:
        raise ValueError("补全结果缺少知识点（knowledge_tags）")

    result: dict = {
        "answers": answers,
        "explanation": explanation,
        "rubric": rubric,
        "knowledge_tags": knowledge_tags,
        "parts": [],
    }

    if qtype == "MIXED":
        original_parts = content.get("parts") if isinstance(content.get("parts"), list) else []
        raw_parts = parsed.get("parts")
        if not original_parts:
            raise ValueError("原题缺少子题结构，无法按混合题补全")
        if not isinstance(raw_parts, list) or len(raw_parts) != len(original_parts):
            raise ValueError(
                f"混合题补全必须保留全部 {len(original_parts)} 个子题，received="
                f"{len(raw_parts) if isinstance(raw_parts, list) else '非数组'}"
            )
        raw_by_index = _part_by_index(raw_parts)
        normalized_parts: list[dict] = []
        for position, original in enumerate(original_parts):
            if not isinstance(original, dict):
                continue
            part_index = safe_int(original.get("index"), position + 1) or (position + 1)
            raw_part = raw_by_index.get(part_index)
            if raw_part is None:
                raise ValueError(f"混合题缺少第 {part_index} 个子题的补全结果")
            normalized_parts.append(_validate_part_enrichment(raw_part, original, part_index))
        if not normalized_parts:
            raise ValueError("混合题补全后没有有效子题")
        result["parts"] = normalized_parts
        if not answers:
            # 混合题答案在各子题里；顶层 answers 允许为空。
            result["answers"] = []
    else:
        if not answers:
            raise ValueError("补全结果缺少答案（answers）")
        if qtype == "SUBJECTIVE" and not rubric:
            raise ValueError("主观题补全必须提供评分标准（rubric）")
        if qtype == "BLANK" and original_answers:
            if len(original_answers) != len(answers):
                raise ValueError(
                    f"填空题必须保持每空一个答案：原题 {len(original_answers)} 空，"
                    f"补全返回 {len(answers)} 个答案"
                )
    return result


def _validate_part_enrichment(raw_part, original: dict, part_index: int) -> dict:
    if not isinstance(raw_part, dict):
        raise ValueError(f"第 {part_index} 个子题补全结果必须是 JSON 对象")
    part_type = normalize_qtype(original.get("type")) or "SUBJECTIVE"
    options = original.get("options") if isinstance(original.get("options"), list) else []
    answers = normalize_answers(raw_part.get("answers"))
    if not answers:
        raise ValueError(f"第 {part_index} 个子题缺少答案")
    validate_choice_answers(part_type, options, answers, where=f"第 {part_index} 个子题")
    rubric = _as_text(raw_part.get("rubric"))
    if part_type == "SUBJECTIVE" and not rubric:
        raise ValueError(f"第 {part_index} 个子题是主观题，必须提供评分标准（rubric）")
    return {
        "index": part_index,
        "answers": answers,
        "explanation": _as_text(raw_part.get("explanation")),
        "rubric": rubric,
        "knowledge_tags": normalize_tags(raw_part.get("knowledge_tags")),
    }


# ---------------------------------------------------------------------------
# 人工编辑草稿
# ---------------------------------------------------------------------------

def apply_draft_edit(existing: dict, patch: dict, *, page_no: int, width: int = 0,
                     height: int = 0, default_subject: str = "", default_grade: str = "") -> dict:
    """把人工编辑合并进已有草稿并重新走严格校验（bbox 调整、题干/答案/知识点改写）。"""
    if not isinstance(patch, dict):
        raise ValueError("draft 必须是 JSON 对象")
    merged = dict(existing or {})
    for key, value in patch.items():
        if key in {"page_no", "source_page"}:
            continue
        merged[key] = value
    return normalize_draft(
        merged, page_no=page_no, width=width, height=height,
        default_subject=default_subject, default_grade=default_grade,
    )


# ---------------------------------------------------------------------------
# 双模型交叉核验（P2）：题干相似度 / 选项集合 / 答案集合 → diff 结构
# ---------------------------------------------------------------------------

# 归一化时去掉的字符：中英文标点、成对括号、LaTeX 分隔符与反引号。
# 数学运算符（+ - * / \ | = < > % ^ & ~ _）保留，避免把不同公式误判为相同。
_COMPARE_STRIP_CHARS = (
    "，。、；：？！“”‘’（）【】《》〈〉「」『』,.;:?!\"'()[]{}·…—–`$"
)
_WHITESPACE_RE = re.compile(r"\s+")


def normalize_compare_text(value) -> str:
    """比对用归一化：NFKC + casefold + 去空白/标点，只保留文字、字母、数字与数学符号。"""
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return ""
    text = unicodedata.normalize("NFKC", str(value))
    text = text.casefold()
    text = _WHITESPACE_RE.sub("", text)
    return text.translate({ord(char): None for char in _COMPARE_STRIP_CHARS})


def stem_similarity(left, right) -> float:
    """两道题题干的归一化相似度（``difflib.SequenceMatcher``，0~1，保留 4 位）。"""
    a = normalize_compare_text(left)
    b = normalize_compare_text(right)
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return round(SequenceMatcher(None, a, b).ratio(), 4)


def _normalize_option_label(value) -> str:
    label = unicodedata.normalize("NFKC", str(value or "")).strip().casefold()
    return label.replace("．", "").replace(".", "").replace("、", "")


def _option_signature(raw_options, *, prefix: str = "") -> list[str]:
    """选项集合签名：``label:归一化文本``，排序去重（顺序不同不影响相等判断）。"""
    signature: list[str] = []
    if not isinstance(raw_options, (list, tuple)):
        return signature
    for index, option in enumerate(raw_options):
        if isinstance(option, dict):
            label = _normalize_option_label(option.get("label"))
            text = normalize_compare_text(option.get("text"))
        else:
            label = ""
            text = normalize_compare_text(option)
        if not label:
            label = str(index + 1)
        signature.append(f"{prefix}{label}:{text}")
    return sorted(set(signature))


def _answer_signature(raw_answers, *, prefix: str = "") -> list[str]:
    """答案集合签名：归一化去重（大小写/全半角/标点差异不算不一致）。"""
    signature: list[str] = []
    if not isinstance(raw_answers, (list, tuple)):
        return signature
    for answer in raw_answers:
        if isinstance(answer, dict):
            answer = answer.get("answer") if answer.get("answer") is not None else answer.get("text")
        text = normalize_compare_text(answer)
        if text:
            signature.append(prefix + text)
    return sorted(set(signature))


def draft_option_signature(draft: dict) -> list[str]:
    """题目（含 MIXED 子题）的选项集合签名。"""
    draft = draft if isinstance(draft, dict) else {}
    signature = _option_signature(draft.get("options"))
    parts = draft.get("parts")
    if isinstance(parts, list):
        for index, part in enumerate(parts, start=1):
            if isinstance(part, dict):
                signature.extend(_option_signature(part.get("options"), prefix=f"p{index}:"))
    return sorted(set(signature))


def draft_answer_signature(draft: dict) -> list[str]:
    """题目（含 MIXED 子题）的答案集合签名；顶层无答案时回退到子题答案。"""
    draft = draft if isinstance(draft, dict) else {}
    signature = _answer_signature(draft.get("answers"))
    if not signature:
        parts = draft.get("parts")
        if isinstance(parts, list):
            for index, part in enumerate(parts, start=1):
                if isinstance(part, dict):
                    signature.extend(_answer_signature(part.get("answers"), prefix=f"p{index}:"))
    return sorted(set(signature))


def compare_item_drafts(primary: dict, verify: dict, *,
                        stem_threshold: float = VERIFY_STEM_THRESHOLD) -> dict:
    """逐题比对主模型与核验模型的草稿。

    返回 ``{stem_ratio, options_equal, answers_equal, flags[], stem_threshold}``；
    ``flags`` 为空表示两模型一致，否则逐项标注不一致位置（题干/选项/答案/题型）。
    """
    primary = primary if isinstance(primary, dict) else {}
    verify = verify if isinstance(verify, dict) else {}
    stem_ratio = stem_similarity(primary.get("stem"), verify.get("stem"))
    options_equal = draft_option_signature(primary) == draft_option_signature(verify)
    answers_equal = draft_answer_signature(primary) == draft_answer_signature(verify)
    flags: list[str] = []
    if stem_ratio < float(stem_threshold):
        flags.append("stem_mismatch")
    if not options_equal:
        flags.append("options_mismatch")
    if not answers_equal:
        flags.append("answers_mismatch")
    primary_type = normalize_qtype(primary.get("type")) or ""
    verify_type = normalize_qtype(verify.get("type")) or ""
    if primary_type and verify_type and primary_type != verify_type:
        flags.append("type_mismatch")
    return {
        "stem_ratio": stem_ratio,
        "options_equal": options_equal,
        "answers_equal": answers_equal,
        "flags": flags,
        "stem_threshold": round(float(stem_threshold), 4),
    }


def compare_page_results(primary_result, verify_result, *,
                         stem_threshold: float = VERIFY_STEM_THRESHOLD) -> dict:
    """逐页比对：主模型 ``questions`` 与核验模型 ``questions`` 按顺序逐题比较。

    核验模型少识别出题时，对应条目 ``flags=['missing_in_verify']``；两模型题数不一致
    通过 ``count_mismatch`` 标出（页面级待复核原因由调用方组装）。
    """
    primary = primary_result if isinstance(primary_result, dict) else {}
    verify = verify_result if isinstance(verify_result, dict) else {}
    primary_questions = primary.get("questions")
    verify_questions = verify.get("questions")
    primary_questions = list(primary_questions) if isinstance(primary_questions, list) else []
    verify_questions = list(verify_questions) if isinstance(verify_questions, list) else []
    entries: list[dict] = []
    for index, draft in enumerate(primary_questions):
        if index < len(verify_questions) and isinstance(verify_questions[index], dict):
            verify_draft = verify_questions[index]
            diff = compare_item_drafts(draft, verify_draft, stem_threshold=stem_threshold)
        else:
            verify_draft = None
            diff = {
                "stem_ratio": 0.0,
                "options_equal": False,
                "answers_equal": False,
                "flags": ["missing_in_verify"],
                "stem_threshold": round(float(stem_threshold), 4),
            }
        entries.append({
            "index": index,
            "diff": diff,
            "draft": verify_draft,
        })
    return {
        "primary_count": len(primary_questions),
        "verify_count": len(verify_questions),
        "count_mismatch": len(primary_questions) != len(verify_questions),
        "entries": entries,
    }
