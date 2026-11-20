"""Golden benchmark builder & Prompt/Model/Cross-Verification A/B evaluator.

用法：
  python -m app.eval --build-golden                 # 旧 100 题文本基线（eval/golden.jsonl）
  python -m app.eval --build-golden --strata        # 分层评测集（eval/golden-v2.jsonl）
  python -m app.eval --provider-id 1 --model gpt-4o [--verify-provider-id 2 --verify-model deepseek-chat] [--limit 20]
  python -m app.eval --provider-id 1 --model gpt-4o --verify-provider-id 2 --verify-model deepseek-chat \
      --judge-provider-id 3 --judge-model judge-model [--out-jsonl eval/run.jsonl [--resume]]
  python -m app.eval --compare report_a.json report_b.json

分层评测集（P1「评测分层与可追溯」）：
  text             纯文本题干
  figure_formula   题干/选项/材料含图片、公式块或占位符
  material_group   有 group_id 的阅读材料/共用题干题
  group_multipart  题组成员数 > 1 的多问题组

一个题目可以同时命中多个分层，`strata` 按固定顺序列出全部命中项，`stratum`
给出互斥的主分层（group_multipart > material_group > figure_formula > text），
便于按层抽样、按层统计，同时不丢失“题组里还带图”这类组合信息。
"""
import argparse
import asyncio
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import random
import re
import threading

from . import ai, content_refs, db, practice

DEFAULT_GOLDEN = Path(__file__).resolve().parents[1] / "eval" / "golden.jsonl"
DEFAULT_GOLDEN_V2 = Path(__file__).resolve().parents[1] / "eval" / "golden-v2.jsonl"

# 分层顺序固定：既用于抽样配额，也用于 strata 列表的稳定排序。
STRATA = ("text", "figure_formula", "material_group", "group_multipart")
STRATA_LABELS = {
    "text": "纯文本题干",
    "figure_formula": "原卷图片与公式",
    "material_group": "阅读材料/共用题干",
    "group_multipart": "多问题组",
}
# 主分层优先级：题组 > 材料 > 图/公式 > 纯文本。
_STRATUM_PRIORITY = ("group_multipart", "material_group", "figure_formula", "text")

# 块级内容引用与图片采集统一由 app.content_refs 实现（与线上 AI 讲解/判分共用同一口径），
# 这里仅保留同名别名，避免评测内部调用点与既有 golden 记录字段发生变化。
MAX_IMAGE_BYTES = content_refs.MAX_IMAGE_BYTES
MAX_TOTAL_IMAGE_BYTES = content_refs.MAX_TOTAL_IMAGE_BYTES
MAX_IMAGE_COUNT = content_refs.MAX_IMAGE_COUNT

FIGURE_PLACEHOLDER_RE = content_refs.FIGURE_PLACEHOLDER_RE
FORMULA_PLACEHOLDER_RE = content_refs.FORMULA_PLACEHOLDER_RE
VISUAL_PLACEHOLDER_RE = content_refs.VISUAL_PLACEHOLDER_RE

# 这些质量标记意味着参考答案或自动判分不可靠，不进入分层评测集；
# “材料可能有未定位图片”等缺项标记反而要保留，交给 input_completeness 记录。
_UNRELIABLE_FLAGS = (
    "暂不可判分",
    "参考答案为占位",
    "选项合并",
    "选项标签重复",
    "题型与答案冲突",
    "参考选项重复",
    "缺少关联材料",
    "已隔离",
    "数据复核",
    "仅为上下文",
)


# ---------------------------------------------------------------------------
# 基础小工具
# ---------------------------------------------------------------------------

def _to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _json_value(value, default):
    if isinstance(value, (list, dict)):
        return value
    if not value:
        return default
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


def _json_list(value) -> list[dict]:
    data = _json_value(value, [])
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "y")
    return bool(value)


_asset_file = content_refs.asset_file
_asset_role = content_refs.asset_role
_image_role = content_refs.image_role


def _canonical_group_id(row: dict) -> str | None:
    """题库 group_id 有的带 doc_id 前缀、有的不带，统一成 groups 表主键。"""
    group_id = row.get("group_id")
    if not group_id:
        return None
    group_id = str(group_id)
    doc_id = row.get("doc_id")
    if doc_id and not group_id.startswith(str(doc_id) + ":"):
        # 兼容旧快照：questions.group_id = m_0004，groups.group_id = doc:m_0004
        return f"{doc_id}:{group_id}"
    return group_id


def _table_columns(con, table: str) -> frozenset[str]:
    try:
        return frozenset(row[1] for row in con.execute(f'PRAGMA table_info("{table}")'))
    except Exception:
        return frozenset()


# ---------------------------------------------------------------------------
# 块结构：与 web/js/core/dom.js 的 blockKindOf 保持同一口径（实现见 app.content_refs）
# ---------------------------------------------------------------------------

_block_kind = content_refs.block_kind
_blocks_readable_text = content_refs.blocks_readable_text


# ---------------------------------------------------------------------------
# 分层判定
# ---------------------------------------------------------------------------

def _member_count(question: dict) -> int:
    """题组成员数；优先取题库显式字段，其次退回成员索引/children。"""
    group = question.get("group") if isinstance(question.get("group"), dict) else {}
    counts: list[int] = []
    for value in (
        group.get("n_members"),
        question.get("n_members"),
        group.get("n_children_total"),
        question.get("n_children_total"),
    ):
        number = _to_int(value)
        if number is not None:
            counts.append(number)
    for value in (group.get("member_index"), question.get("member_index")):
        if isinstance(value, list) and value:
            counts.append(len(value))
    children = question.get("children")
    if children is None:
        children = question.get("children_json")
    if isinstance(children, str):
        children = _json_value(children, None)
    if isinstance(children, list) and children:
        counts.append(len(children))
    return max(counts) if counts else 0


def _question_has_visual(question: dict) -> bool:
    """题干/选项/共用材料是否含图片、公式块或 [图N]/[公式N] 占位。"""
    if _truthy(question.get("has_figure")) or _truthy(question.get("has_formula")):
        return True
    if any(_image_role(image) in ("figure", "formula") for image in _json_list(question.get("images"))):
        return True
    if VISUAL_PLACEHOLDER_RE.search(str(question.get("stem_plain") or "")):
        return True
    for option in question.get("options") or []:
        if isinstance(option, dict) and VISUAL_PLACEHOLDER_RE.search(str(option.get("text") or "")):
            return True

    group = question.get("group") if isinstance(question.get("group"), dict) else {}
    blocks: list[dict] = []
    blocks.extend(block for block in question.get("stem_blocks") or [] if isinstance(block, dict))
    for entry in question.get("options_blocks") or []:
        if isinstance(entry, dict):
            blocks.extend(block for block in entry.get("blocks") or [] if isinstance(block, dict))
    blocks.extend(block for block in question.get("shared_blocks") or [] if isinstance(block, dict))
    blocks.extend(block for block in group.get("shared_blocks") or [] if isinstance(block, dict))
    for block in blocks:
        kind = _block_kind(block)
        if kind in ("image", "math"):
            return True
        if kind == "text" and VISUAL_PLACEHOLDER_RE.search(str(block.get("text") or "")):
            return True

    if _json_list(group.get("material_images")):
        return True
    if VISUAL_PLACEHOLDER_RE.search(str(group.get("material_text") or "")):
        return True
    return False


def classify_strata(question: dict) -> list[str]:
    """返回题目命中的全部分层（固定顺序），纯文本题返回 ["text"]。"""
    found = set()
    if _question_has_visual(question):
        found.add("figure_formula")
    group = question.get("group") if isinstance(question.get("group"), dict) else {}
    if question.get("group_id") or group.get("group_id"):
        found.add("material_group")
    if _member_count(question) > 1:
        found.add("group_multipart")
    if not found:
        found.add("text")
    return [stratum for stratum in STRATA if stratum in found]


def primary_stratum(strata) -> str:
    """互斥主分层，用于分层层级统计和报告展示。"""
    if isinstance(strata, dict):
        strata = classify_strata(strata)
    if isinstance(strata, str):
        return strata if strata in STRATA else "text"
    values = set(strata or [])
    for name in _STRATUM_PRIORITY:
        if name in values:
            return name
    return "text"


# ---------------------------------------------------------------------------
# 实际题库内容引用（content_refs）：实现见 app/content_refs.py
# ---------------------------------------------------------------------------

# 兼容既有调用点与 golden 字段契约。
_make_ref = content_refs.make_ref
_content_view = content_refs.content_view
build_content_refs = content_refs.build_content_refs


# ---------------------------------------------------------------------------
# 输入完整率与实际图片输入
# ---------------------------------------------------------------------------

def describe_missing_input(ref: dict) -> str:
    label = ref.get("field_label") or ref.get("field") or "内容"
    role = ref.get("role")
    noun = "公式图" if role == "formula" else ("原卷图" if role == "figure" else "图片")
    asset = ref.get("asset")
    if ref.get("has_image") or ref.get("kind") == "image":
        if asset:
            return f"{label}{noun}未送入（{asset}）"
        return f"{label}{noun}未送入（{ref.get('missing_reason') or '题库缺项'}）"
    return f"{label}内容未送入"


def compute_input_completeness(content_refs, *, delivered_assets=(), image_notes=None) -> dict:
    """按“题库应有内容单元”计算 0–1 输入完整率，并返回缺失项中文说明。

    - 文本/表格/纯 LaTeX 公式块视为随题干发送；
    - 图片/公式图只有真正作为图片输入送达，才算该单元完整；
    - `image_notes` 为 {asset: 不可送原因}，用于把 12MB/20MB/不可读等降级原因写清楚。
    """
    delivered = set(delivered_assets or ())
    notes = image_notes or {}
    marked: list[dict] = []
    missing: list[str] = []
    expected_units = 0
    delivered_units = 0

    for ref in content_refs or []:
        if not isinstance(ref, dict):
            continue
        item = dict(ref)
        required = item.get("required", True) is not False
        if required:
            expected_units += 1
        if item.get("has_image"):
            item["delivered"] = bool(item.get("asset")) and item["asset"] in delivered
        else:
            item["delivered"] = True
        if required:
            if item["delivered"]:
                delivered_units += 1
            else:
                asset = item.get("asset")
                reason = notes.get(asset) if asset else None
                if asset and reason:
                    label = item.get("field_label") or item.get("field") or "内容"
                    role = item.get("role")
                    noun = "公式图" if role == "formula" else ("原卷图" if role == "figure" else "图片")
                    message = f"{label}{noun}未送入（{asset}；{reason}）"
                else:
                    message = describe_missing_input(item)
                if message not in missing:
                    missing.append(message)
        marked.append(item)

    rate = 1.0 if expected_units == 0 else round(delivered_units / expected_units, 4)
    return {
        "input_completeness": rate,
        "missing_inputs": missing,
        "content_refs": marked,
        "expected_units": expected_units,
        "delivered_units": delivered_units,
    }


# 素材解析与图片读取同样复用公共实现（保证与线上 AI 判分读取到同一批原卷图）。
_resolve_bank_asset = content_refs.resolve_bank_asset
# collect_image_parts 的形参名 content_refs 会遮蔽模块名，这里保留一个独立引用。
_content_refs_impl = content_refs


def collect_image_parts(item: dict, content_refs=None):
    """把 content_refs 中的原卷图/公式图真正读成模型图片输入。

    返回 (image_parts, delivered_assets, notes)。单图 >12MB、单题总量 >20MB、
    超过 40 张或文件不可读时跳过该图并写降级原因，不抛异常中断评测。
    原卷答案解析（required=False）的可选单元不进独立求解输入，避免参考答案泄题。
    """
    refs = content_refs if content_refs is not None else (item.get("content_refs") or [])
    assets: list[str] = []
    seen = set()
    for ref in refs or []:
        if not isinstance(ref, dict) or not ref.get("has_image") or ref.get("required") is False:
            continue
        asset = ref.get("asset")
        if not asset or asset in seen:
            continue
        seen.add(asset)
        assets.append(str(asset))
    if not assets:
        return [], set(), {}

    subject = str(item.get("subject") or "")
    parts: list[dict] = []
    delivered: set[str] = set()
    notes: dict[str, str] = {}
    total_bytes = 0
    for asset in assets:
        if len(parts) >= MAX_IMAGE_COUNT:
            notes[asset] = "题图数量超过 40 张上限"
            continue
        path = _resolve_bank_asset(subject, asset)
        if path is None:
            notes[asset] = "题库中找不到该图片文件"
            continue
        try:
            size = path.stat().st_size
        except OSError:
            notes[asset] = "图片文件无法读取"
            continue
        if size > MAX_IMAGE_BYTES:
            notes[asset] = "单图超过 12 MB 限制"
            continue
        if total_bytes + size > MAX_TOTAL_IMAGE_BYTES:
            notes[asset] = "题图总量超过 20 MB 限制"
            continue
        try:
            part = _content_refs_impl.image_part(path)
        except Exception as exc:
            notes[asset] = f"图片无法读取（{type(exc).__name__}）"
            continue
        parts.append(part)
        delivered.add(asset)
        total_bytes += size
    return parts, delivered, notes


# ---------------------------------------------------------------------------
# 题库版本 / 可信答案来源
# ---------------------------------------------------------------------------

def _bank_version_defaults() -> tuple[str, str]:
    """题库未声明版本时依次退回 manifest.json 与题库文件内容摘要。"""
    root = Path(db.BANK_DIR)
    manifest = root / "manifest.json"
    if manifest.is_file():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            version = data.get("bank_version") if isinstance(data, dict) else None
            if version:
                return str(version), "manifest"
        except Exception:
            pass
    bank_db = root / "题库.db"
    if bank_db.is_file():
        digest = hashlib.sha256()
        try:
            with bank_db.open("rb") as stream:
                while chunk := stream.read(1 << 20):
                    digest.update(chunk)
            return f"sha256:{digest.hexdigest()[:12]}", "content_sha256"
        except OSError:
            pass
    return "未标注", "unknown"


def _resolve_bank_version(question: dict, default_version: str, default_source: str):
    version = question.get("bank_version")
    if version and str(version).strip():
        return str(version).strip(), "question"
    return default_version, default_source


def _reference_source(question: dict) -> str:
    parts = ["题库 answer_text（卷内原文答案"]
    binding = question.get("binding_method")
    if binding:
        parts.append(f"binding_method={binding}")
    revision = question.get("answer_revision")
    if revision:
        parts.append(f"answer_revision={revision}")
    note = str(question.get("answer_note") or "").strip()
    if note:
        parts.append(f"answer_note={note}")
    return "；".join(parts) + "）"


def _golden_v2_record(
    question: dict,
    *,
    default_bank_version: str = "未标注",
    default_bank_version_source: str = "unknown",
) -> dict:
    view = _content_view(question)
    strata = classify_strata(question)
    bank_version, bank_version_source = _resolve_bank_version(
        question, default_bank_version, default_bank_version_source
    )
    public_options = [{"label": option.get("label") or "", "text": option.get("text") or ""}
                      for option in view["options"]]
    group = view["group"]
    return {
        "qid": str(question.get("qid") or ""),
        "subject": str(question.get("subject") or ""),
        "type": str(question.get("type") or ""),
        "type_label": question.get("type_label") or question.get("type") or "",
        "objective": bool(question.get("objective")),
        "year": question.get("year"),
        "province": question.get("province"),
        "paper_name": question.get("paper_name"),
        "strata": strata,
        "stratum": primary_stratum(strata),
        "bank_version": bank_version,
        "bank_version_source": bank_version_source,
        "answer_kind": _infer_answer_kind(question),
        "reference_answer": str(question.get("answer_text") or "").strip(),
        "reference_source": _reference_source(question),
        "content_refs": build_content_refs(question, view=view),
        "stem_plain": view["stem_plain"],
        "stem_text": view["stem_text"],
        "options": public_options,
        "material_text": view["material_text"],
        "shared_text": view["shared_text"],
        "group_id": question.get("group_id") or group.get("group_id"),
        "group_role": question.get("group_role") or group.get("group_role"),
        "bank_quality_flags": list(question.get("quality_flags") or []),
        "images_count": len(_json_list(question.get("images"))),
        "prompt_version": ai.PROMPT_VERSION,
    }


# ---------------------------------------------------------------------------
# 旧 100 题基线（保持既有行为，不改变输出字段）
# ---------------------------------------------------------------------------

def _infer_answer_kind(q: dict) -> str:
    ref = (q.get("answer_text") or "").strip().upper()
    if q.get("objective") and re.fullmatch(r"[A-H]+", ref):
        return "choice"
    if re.fullmatch(r"[+-]?\d+(?:\.\d+)?(?:/[+-]?\d+)?", ref):
        return "number"
    if any(ch in ref for ch in ("=", "^", "\\", "+", "-", "/", "√", "π")) and len(ref) <= 60:
        return "expression"
    return "text"


def build_golden_dataset(out_path: Path = DEFAULT_GOLDEN, count: int = 100, seed: int = 20261120) -> list[dict]:
    """Stratified deterministic sample of `count` reliable questions across all subjects & types."""
    rng = random.Random(seed)
    with practice._bank() as con:
        excl, params = practice._excluded_sql("")
        rows = con.execute(
            f"SELECT qid, subject, type FROM questions WHERE {excl} ORDER BY subject, type, qid",
            params,
        ).fetchall()

    # Bucket clean, reliable-answer questions by subject
    by_subject: dict[str, list[dict]] = defaultdict(list)
    # Shuffle deterministically inside each subject before inspecting so sampling is fast & diverse
    raw_by_subj: dict[str, list[str]] = defaultdict(list)
    for r in rows:
        raw_by_subj[r["subject"]].append(r["qid"])

    per_subj_quota = max(1, count // max(1, len(raw_by_subj)))
    selected: list[dict] = []
    overflow_pool: list[dict] = []

    for subj in sorted(raw_by_subj):
        qids = list(raw_by_subj[subj])
        rng.shuffle(qids)
        type_buckets: dict[str, list[dict]] = defaultdict(list)
        for qid in qids:
            q = practice.load_question(qid)
            if not q["practice_allowed"] or q["quality_flags"] or not q["has_answer"]:
                continue
            # Prefer questions with clean text stems (avoid figure-only questions for text-model benchmarking)
            if not (q.get("stem_plain") or "").strip() or len(q["stem_plain"].strip()) < 15:
                continue
            if any(im.get("role") == "figure" for im in q.get("images", [])):
                continue
            item = {
                "qid": q["qid"],
                "subject": q["subject"],
                "year": q["year"],
                "province": q["province"],
                "paper_name": q["paper_name"],
                "type": q["type"],
                "type_label": q["type_label"],
                "objective": bool(q["objective"]),
                "stem_plain": q["stem_plain"],
                "options": q["options"],
                "reference_answer": (q.get("answer_text") or "").strip(),
                "answer_kind": _infer_answer_kind(q),
            }
            type_buckets[q["type"]].append(item)
            if sum(len(v) for v in type_buckets.values()) >= per_subj_quota * 3:
                break

        # Round-robin across question types within subject
        picked_subj: list[dict] = []
        type_keys = sorted(type_buckets.keys())
        idx = 0
        while len(picked_subj) < per_subj_quota and type_keys:
            tk = type_keys[idx % len(type_keys)]
            if type_buckets[tk]:
                picked_subj.append(type_buckets[tk].pop(0))
                idx += 1
            else:
                type_keys.remove(tk)
        selected.extend(picked_subj)
        for rem in type_buckets.values():
            overflow_pool.extend(rem)

    # Top up to exact `count` from remaining clean pool across subjects
    rng.shuffle(overflow_pool)
    seen = {it["qid"] for it in selected}
    for item in overflow_pool:
        if len(selected) >= count:
            break
        if item["qid"] not in seen:
            selected.append(item)
            seen.add(item["qid"])

    selected = selected[:count]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        for item in selected:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")
    return selected


def load_golden_dataset(path: Path = DEFAULT_GOLDEN) -> list[dict]:
    items = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                items.append(json.loads(line))
    return items


# ---------------------------------------------------------------------------
# 分层评测集构建（P1）
# ---------------------------------------------------------------------------

def _coarse_bank_rows():
    """只读扫描题库一次，取分层抽样需要的粗粒度元数据（不加载完整题目）。"""
    with practice._bank() as con:
        excl, params = practice._excluded_sql("")
        columns = _table_columns(con, "questions")
        select = ["qid", "subject", "type"]
        select.extend(
            column
            for column in (
                "doc_id", "group_id", "stem_plain", "images_json", "options_json",
                "has_figure", "has_formula", "practice_status", "grading_status", "bank_version",
            )
            if column in columns
        )
        rows = con.execute(
            f"SELECT {', '.join(select)} FROM questions WHERE {excl} ORDER BY subject, type, qid",
            params if params else (),
        ).fetchall()
        group_sizes = _group_member_counts(con)
    return [dict(row) for row in rows], group_sizes


def _group_member_counts(con) -> dict[str, int]:
    columns = _table_columns(con, "groups")
    if "group_id" not in columns:
        return {}
    select = ["group_id"]
    select.extend(column for column in ("n_members", "n_children_total", "children_json") if column in columns)
    try:
        rows = con.execute(f"SELECT {', '.join(select)} FROM groups").fetchall()
    except Exception:
        return {}
    sizes: dict[str, int] = {}
    for row in rows:
        data = dict(row)
        group_id = str(data.get("group_id") or "")
        if not group_id:
            continue
        counts = []
        for key in ("n_members", "n_children_total"):
            if key in data:
                number = _to_int(data.get(key))
                if number is not None:
                    counts.append(number)
        children = data.get("children_json")
        if isinstance(children, str) and children.strip():
            parsed = _json_value(children, None)
            if isinstance(parsed, list):
                counts.append(len(parsed))
        sizes[group_id] = max(counts) if counts else 0
    return sizes


def _coarse_eligible(row: dict) -> bool:
    status = str(row.get("practice_status") or "").strip()
    if status and status != "practice":
        return False
    grading = str(row.get("grading_status") or "").strip()
    if grading and grading != "auto":
        return False
    # 旧快照里有脱离题组的 sub_* 子题（没有 group_id），缺材料无法公平评测。
    if str(row.get("type") or "").startswith("sub_") and not row.get("group_id"):
        return False
    return True


def _coarse_strata(row: dict, group_sizes: dict[str, int]) -> list[str]:
    found = set()
    if (
        _truthy(row.get("has_figure"))
        or _truthy(row.get("has_formula"))
        or any(_image_role(image) in ("figure", "formula") for image in _json_list(row.get("images_json")))
        or VISUAL_PLACEHOLDER_RE.search(str(row.get("stem_plain") or ""))
        or VISUAL_PLACEHOLDER_RE.search(str(row.get("options_json") or ""))
    ):
        found.add("figure_formula")
    if row.get("group_id"):
        found.add("material_group")
    group_key = _canonical_group_id(row)
    if group_key and group_sizes.get(group_key, 0) > 1:
        found.add("group_multipart")
    if not found:
        found.add("text")
    return [stratum for stratum in STRATA if stratum in found]


def bank_strata_counts() -> dict[str, int]:
    """粗粒度统计题库各分层候选数（只读；正式抽样仍逐题 load_question 复核）。"""
    rows, group_sizes = _coarse_bank_rows()
    counts = {stratum: 0 for stratum in STRATA}
    for row in rows:
        if not _coarse_eligible(row):
            continue
        for stratum in _coarse_strata(row, group_sizes):
            counts[stratum] += 1
    return counts


def _interleave_by_subject(items: list[dict], rng: random.Random, offset: int = 0) -> list[dict]:
    """同一分层内按学科轮转，避免抽样集中到单个学科；结果由固定 seed 决定。"""
    groups: dict[str, list[dict]] = defaultdict(list)
    for item in items:
        groups[str(item.get("subject") or "未知")].append(item)
    keys = sorted(groups)
    if keys and offset:
        shift = offset % len(keys)
        keys = keys[shift:] + keys[:shift]
    for key in keys:
        rng.shuffle(groups[key])
    ordered: list[dict] = []
    active = keys
    while active:
        next_active = []
        for key in active:
            if not groups[key]:
                continue
            ordered.append(groups[key].pop(0))
            if groups[key]:
                next_active.append(key)
        active = next_active
    return ordered


def _question_eligible(question: dict) -> bool:
    if not question.get("practice_allowed"):
        return False
    if str(question.get("practice_status") or "practice").strip() != "practice":
        return False
    if str(question.get("grading_status") or "auto").strip() != "auto":
        return False
    if not question.get("has_answer"):
        return False
    if not str(question.get("answer_text") or "").strip():
        return False
    if str(question.get("type") or "").startswith("sub_") and not question.get("group_id"):
        return False
    for flag in question.get("quality_flags") or []:
        if any(bad in str(flag) for bad in _UNRELIABLE_FLAGS):
            return False
    return True


def _load_golden_v2_record(candidate: dict, default_version, default_source):
    try:
        question = practice.load_question(candidate["qid"])
    except Exception:
        return None
    if not _question_eligible(question):
        return None
    record = _golden_v2_record(
        question, default_bank_version=default_version, default_bank_version_source=default_source
    )
    if not record["reference_answer"]:
        return None
    # 纯文本题至少要有可读题干；图/公式/材料题允许题干很短，由 content_refs 补足。
    if record["strata"] == ["text"] and len(record["stem_text"].strip()) < 10:
        return None
    return record


def build_golden_v2(out_path: Path = DEFAULT_GOLDEN_V2, count: int = 100, seed: int = 20261120) -> list[dict]:
    """从只读题库确定性抽取四个分层的 golden-v2 评测集。

    默认写到 eval/golden-v2.jsonl，绝不覆盖旧基线 eval/golden.jsonl。
    每个分层尽量占满配额；同一题可同时命中多个分层，因此最终条数仍为 `count`
    （候选不足时可能少于 `count`，不会重复凑数）。
    """
    out_path = Path(out_path)
    if out_path.resolve() == DEFAULT_GOLDEN.resolve():
        raise ValueError("分层模式不会覆盖旧基线 eval/golden.jsonl，请用 --out 指定其他路径")
    if count <= 0:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text("", encoding="utf-8")
        return []

    rows, group_sizes = _coarse_bank_rows()
    candidates = []
    for row in rows:
        if not _coarse_eligible(row):
            continue
        strata = _coarse_strata(row, group_sizes)
        candidates.append({
            "qid": str(row.get("qid") or ""),
            "subject": str(row.get("subject") or ""),
            "type": str(row.get("type") or ""),
            "strata": strata,
        })
    candidates = [candidate for candidate in candidates if candidate["qid"]]
    if not candidates:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text("", encoding="utf-8")
        return []

    rng = random.Random(seed)
    pools = {}
    for index, stratum in enumerate(STRATA):
        pool = [candidate for candidate in candidates if stratum in candidate["strata"]]
        # 不同分层从不同学科起点轮转，小样本也能尽量覆盖多学科。
        pools[stratum] = _interleave_by_subject(pool, rng, offset=index * 2)
    targets = {stratum: count // len(STRATA) for stratum in STRATA}
    for index in range(count % len(STRATA)):
        targets[STRATA[index]] += 1

    default_version, default_source = _bank_version_defaults()
    selected: list[dict] = []
    chosen = set()
    attempted = set()
    covered = {stratum: 0 for stratum in STRATA}

    def accept(candidate: dict) -> bool:
        qid = candidate["qid"]
        if qid in chosen or qid in attempted:
            return False
        attempted.add(qid)
        record = _load_golden_v2_record(candidate, default_version, default_source)
        if record is None:
            return False
        selected.append(record)
        chosen.add(qid)
        for stratum in record["strata"]:
            if stratum in covered:
                covered[stratum] += 1
        return True

    # 先按分层配额抽样；一个多问题组可以同时满足 group_multipart 与 material_group。
    for stratum in STRATA:
        pool = pools[stratum]
        while covered[stratum] < targets[stratum] and pool:
            accept(pool.pop(0))

    # 配额被重叠分层“提前满足”时，用剩余候选补足总条数。
    if len(selected) < count:
        remaining = [
            candidate
            for candidate in candidates
            if candidate["qid"] not in chosen and candidate["qid"] not in attempted
        ]
        for candidate in _interleave_by_subject(remaining, rng):
            if len(selected) >= count:
                break
            accept(candidate)

    # 兜底：前面 pool 里尝试失败但尚未尝试的候选（理论上已被 pool 覆盖）。
    if len(selected) < count:
        for candidate in candidates:
            if len(selected) >= count:
                break
            if candidate["qid"] not in chosen and candidate["qid"] not in attempted:
                accept(candidate)

    selected.sort(key=lambda record: (
        STRATA.index(record["stratum"]), record["subject"], record["type"], record["qid"]
    ))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as stream:
        for record in selected:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    return selected


# ---------------------------------------------------------------------------
# 答案核验（保持既有语义；文本判分不再用脆弱子串包含）
# ---------------------------------------------------------------------------

_ANSWER_SET_SPLIT = re.compile(r"[;；,，、/|]+")
_TEXT_STRIP = re.compile(r"[\s，。；、,.!！?？：:]+")
# 参考答案被否定的邻近标记：「不是牛顿第二定律」不能因为高度相似就判对。
_NEGATION_MARKERS = ("不是", "并非", "不对", "不正确", "错误", "不选", "排除", "不属于")


def _clean_text_answer(value) -> str:
    return _TEXT_STRIP.sub("", str(value or "")).lower()


def _answer_token_set(value) -> set[str]:
    """把「A、C」「C, A」这类多项答案拆成规范化集合；无分隔符时返回单项集合。"""
    tokens = {_clean_text_answer(part) for part in _ANSWER_SET_SPLIT.split(str(value or ""))}
    return {token for token in tokens if token}


def _text_answer_matches(val, reference) -> bool:
    """文本答案的严格判分：相等 → 答案集合逐项相等 → 数值等价 → 语义相似度兜底。

    绝不使用「参考答案是预测的子串」这种规则：它会把「不是 A」误判为含 A 正确。
    """
    text_p, text_r = str(val or ""), str(reference or "")
    clean_p, clean_r = _clean_text_answer(text_p), _clean_text_answer(text_r)
    if not clean_p or not clean_r:
        return False
    if clean_p == clean_r:
        return True
    set_p, set_r = _answer_token_set(text_p), _answer_token_set(text_r)
    # 仅当参考答案确实含多个分项时才启用集合比较，避免单字答案被集合化。
    if len(set_r) > 1 and set_p == set_r:
        return True
    # 数值等价：1/2 == 0.5、3.14 m == 314 cm；单位不可比时保持 False。
    try:
        cmp = ai.compare_answers(
            {"kind": "number", "value": text_p}, {"kind": "number", "value": text_r},
        )
    except Exception:  # noqa: BLE001 - 判分绝不能因比对引擎异常而崩溃
        cmp = None
    if isinstance(cmp, dict) and cmp.get("comparable") and cmp.get("agreed"):
        return True
    # 参考答案在预测中被紧邻否定（不是/并非/不正确……）时，不允许判定为正确。
    position = clean_p.find(clean_r)
    if position >= 0:
        window = clean_p[max(0, position - 8):position]
        if any(marker in window for marker in _NEGATION_MARKERS):
            return False
    # 语义相似度兜底（不是子串包含）：长文本只要高度相似即可判对。
    from .search_vector import text_vector_similarity

    return text_vector_similarity(text_p, text_r) >= 0.72


def check_prediction_correct(pred_answer: dict | str | None, reference: str, kind: str) -> bool | None:
    """Evaluate a model's extracted final answer against the reliable reference answer."""
    if not pred_answer:
        return False
    if kind == "text":
        val = pred_answer.get("value") if isinstance(pred_answer, dict) else str(pred_answer)
        return _text_answer_matches(val, reference)
    ans_a = pred_answer if isinstance(pred_answer, dict) else {"kind": kind, "value": str(pred_answer)}
    ans_b = {"kind": kind, "value": reference}
    cmp = ai.compare_answers(ans_a, ans_b)
    return bool(cmp["comparable"] and cmp["agreed"])


async def _check_prediction_correct_async(pred_answer, reference: str, kind: str):
    """程序化比对可能触发受限 SymPy 子进程；在评测协程里放入线程，避免阻塞事件循环。"""
    return await asyncio.to_thread(check_prediction_correct, pred_answer, reference, kind)


# ---------------------------------------------------------------------------
# 费用估算：未知必须单独计数，绝不显示为 0 元
# ---------------------------------------------------------------------------

def _usage_tokens(usage):
    if not isinstance(usage, dict):
        return None, None
    input_tokens = output_tokens = None
    if "prompt_tokens" in usage:
        input_tokens = _to_int(usage.get("prompt_tokens"))
    elif "input_tokens" in usage:
        input_tokens = _to_int(usage.get("input_tokens"))
    if "completion_tokens" in usage:
        output_tokens = _to_int(usage.get("completion_tokens"))
    elif "output_tokens" in usage:
        output_tokens = _to_int(usage.get("output_tokens"))
    return input_tokens, output_tokens


def estimate_call_cost(usage, input_price, output_price) -> dict:
    """单次调用费用（元）。单价按 ai_models.input_price/output_price（元/百万 tokens）。"""
    input_tokens, output_tokens = _usage_tokens(usage)
    base = {
        "currency": "CNY",
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "input_price": input_price,
        "output_price": output_price,
    }
    if input_tokens is None or output_tokens is None:
        return {
            **base,
            "price_known": False,
            "unknown_cost_calls": 1,
            "estimated_cost": None,
            "known_cost_subtotal": None,
            "reason": "用量字段缺失",
        }
    if input_price is None or output_price is None:
        return {
            **base,
            "price_known": False,
            "unknown_cost_calls": 1,
            "estimated_cost": None,
            "known_cost_subtotal": None,
            "reason": "模型未填写单价",
        }
    cost = (
        (input_tokens / 1_000_000.0) * float(input_price)
        + (output_tokens / 1_000_000.0) * float(output_price)
    )
    return {
        **base,
        "price_known": True,
        "unknown_cost_calls": 0,
        "estimated_cost": round(cost, 6),
        "known_cost_subtotal": round(cost, 6),
        "reason": None,
    }


def combine_call_costs(call_costs: list[dict]) -> dict:
    """合并一题内主模型/核验模型的调用费用；任一调用未知则整题 estimated_cost 为 None。"""
    call_costs = [cost for cost in call_costs or [] if isinstance(cost, dict)]
    known = [cost for cost in call_costs if cost.get("price_known")]
    unknown = sum(int(cost.get("unknown_cost_calls") or 0) for cost in call_costs if not cost.get("price_known"))
    known_tokens_in = None
    known_tokens_out = None
    if known:
        known_tokens_in = sum(
            int(cost.get("input_tokens") or 0) for cost in known if cost.get("input_tokens") is not None
        )
        known_tokens_out = sum(
            int(cost.get("output_tokens") or 0) for cost in known if cost.get("output_tokens") is not None
        )
    subtotal = sum(float(cost.get("known_cost_subtotal") or 0) for cost in known)
    return {
        "currency": "CNY",
        "calls": len(call_costs),
        "known_cost_calls": len(known),
        "unknown_cost_calls": unknown,
        # 只要存在未知调用，整题不给出看似完整的费用数字。
        "estimated_cost": None if (unknown > 0 or not known) else round(subtotal, 6),
        "known_cost_subtotal": round(subtotal, 6) if known else None,
        "input_tokens": known_tokens_in,
        "output_tokens": known_tokens_out,
        "price_known": bool(known) and unknown == 0,
    }


def summarize_cost(records: list[dict]) -> dict:
    total_calls = known_calls = unknown_calls = 0
    known_subtotal = 0.0
    for record in records:
        cost = record.get("cost") if isinstance(record.get("cost"), dict) else {}
        total_calls += int(cost.get("calls") or 0)
        known_calls += int(cost.get("known_cost_calls") or 0)
        unknown_calls += int(cost.get("unknown_cost_calls") or 0)
        if cost.get("known_cost_subtotal") is not None:
            try:
                known_subtotal += float(cost["known_cost_subtotal"])
            except (TypeError, ValueError):
                pass
    estimated = None if (unknown_calls > 0 or known_calls == 0) else round(known_subtotal, 6)
    if total_calls == 0:
        note = "没有可计费调用"
    elif unknown_calls > 0:
        note = (
            f"{unknown_calls} 次调用缺少单价或用量，费用不可估算；"
            f"已知部分小计 {round(known_subtotal, 6)} 元（未把未知调用按 0 元计入）"
        )
    else:
        note = "按 ai_models 单价（元/百万 tokens）估算"
    return {
        "currency": "CNY",
        "estimated_cost": estimated,
        "known_cost_subtotal": round(known_subtotal, 6) if known_calls else None,
        "known_cost_calls": known_calls,
        "unknown_cost_calls": unknown_calls,
        "total_calls": total_calls,
        "cost_complete": total_calls > 0 and unknown_calls == 0,
        "note": note,
    }


# ---------------------------------------------------------------------------
# 指标聚合
# ---------------------------------------------------------------------------

def summarize_inputs(records: list[dict]) -> dict:
    values = [
        float(record["input_completeness"])
        for record in records
        if isinstance(record.get("input_completeness"), (int, float))
    ]
    missing_counts: Counter = Counter()
    records_missing = 0
    expected_units = delivered_units = 0
    for record in records:
        missing = record.get("missing_inputs") or []
        if missing:
            records_missing += 1
        for message in missing:
            if isinstance(message, str):
                missing_counts[message] += 1
        expected_units += int(record.get("input_expected_units") or 0)
        delivered_units += int(record.get("input_delivered_units") or 0)
    return {
        "input_completeness": round(sum(values) / len(values), 4) if values else None,
        "records_missing_inputs": records_missing,
        "missing_input_counts": dict(sorted(missing_counts.items())),
        "expected_units": expected_units,
        "delivered_units": delivered_units,
    }


_LITERAL_ONLY_METHOD = "literal_equal_unverified"


def _is_literal_only(record: dict) -> bool:
    """字面一致但未经程序化核验：不得计入一致率/高可信，只在报告里单列。"""
    return (
        record.get("verification_method") == _LITERAL_ONLY_METHOD
        or record.get("literal_only") is True
    )


def _verified_agreed(records: list[dict]) -> list[dict]:
    """一致性统计只含已核验结果：literal_equal_unverified 从分子中剔除。"""
    return [
        record for record in records
        if record.get("agreed") is True and not _is_literal_only(record)
    ]


def _high_confidence(records: list[dict]) -> list[dict]:
    """高可信 = 程序化核验一致 + 与原卷参考答案一致 + 非字面猜测。"""
    return [
        record for record in records
        if record.get("verified") is True
        and record.get("agreed") is True
        and record.get("matches_reference") is True
        and not _is_literal_only(record)
    ]


def _judge_summary(records: list[dict]) -> dict:
    attempts = [
        record for record in records
        if isinstance(record.get("judge"), dict) or record.get("judge_error")
    ]
    verdicts = [record for record in records if isinstance(record.get("judge"), dict)]
    agreed = sum(1 for record in verdicts if record.get("judge_agrees") is True)
    scored = [record for record in verdicts if record.get("judge_correct") is not None]
    correct = sum(1 for record in scored if record.get("judge_correct") is True)
    return {
        "judge_calls": len(attempts),
        "judge_verdicts": len(verdicts),
        "judge_errors": len(attempts) - len(verdicts),
        "judge_agreed_count": agreed,
        "judge_disagreed_count": len(verdicts) - agreed,
        "judge_agreement_rate": round(100.0 * agreed / len(verdicts), 2) if verdicts else None,
        "judge_correct_count": correct if scored else None,
        "judge_correct_rate": round(100.0 * correct / len(scored), 2) if scored else None,
    }


def _bucket_summary(records: list[dict], has_cross: bool) -> dict:
    total = len(records)
    correct = sum(1 for record in records if record.get("a_correct"))
    agreed = _verified_agreed(records)
    agreed_wrong = sum(1 for record in agreed if not record.get("a_correct"))
    values = [
        float(record["input_completeness"])
        for record in records
        if isinstance(record.get("input_completeness"), (int, float))
    ]
    literal_only = [record for record in records if _is_literal_only(record)]
    return {
        "total": total,
        "single_accuracy": round(100.0 * correct / total, 2) if total else None,
        "agreement_rate": round(100.0 * len(agreed) / total, 2) if has_cross and total else None,
        "agreed_but_wrong_rate": round(100.0 * agreed_wrong / len(agreed), 2) if agreed else None,
        "input_completeness": round(sum(values) / len(values), 4) if values else None,
        "literal_only_count": len(literal_only),
        "high_confidence_count": len(_high_confidence(records)),
        "judge_calls": _judge_summary(records)["judge_calls"] if has_cross else None,
    }


def _aggregate(records: list[dict], has_cross: bool) -> dict:
    total = len(records)
    single_ok = sum(1 for record in records if record.get("a_correct"))
    agreed_list = _verified_agreed(records)
    agreed_ok = sum(1 for record in agreed_list if record.get("a_correct"))
    agreed_wrong = sum(1 for record in agreed_list if not record.get("a_correct"))
    literal_only = [record for record in records if _is_literal_only(record)]
    high_confidence = _high_confidence(records)
    comparable_count = sum(
        1 for record in records
        if record.get("verification_comparable") is True and not _is_literal_only(record)
    )
    judge = _judge_summary(records)
    input_stats = summarize_inputs(records)
    cost = summarize_cost(records)

    by_subject = {}
    by_type = {}
    for key_name, bucket in (("subject", by_subject), ("type", by_type)):
        groups: dict[str, list[dict]] = defaultdict(list)
        for record in records:
            groups[record.get(key_name, "unknown")].append(record)
        for key, group in sorted(groups.items()):
            bucket[key] = _bucket_summary(group, has_cross)

    return {
        "total": total,
        "single_correct": single_ok,
        "single_accuracy": round(100.0 * single_ok / total, 2) if total else None,
        "has_cross_verify": has_cross,
        "agreed_count": len(agreed_list) if has_cross else None,
        "agreement_rate": round(100.0 * len(agreed_list) / total, 2) if has_cross and total else None,
        "cross_verified_accuracy": round(100.0 * agreed_ok / len(agreed_list), 2) if agreed_list else None,
        "agreed_but_wrong_count": agreed_wrong if has_cross else None,
        "agreed_but_wrong_rate": round(100.0 * agreed_wrong / len(agreed_list), 2) if agreed_list else None,
        # 口径对齐：字面一致（literal_equal_unverified）单列计数，不计入一致率/高可信。
        "literal_only_count": len(literal_only),
        "literal_only_agreed_count": sum(1 for record in literal_only if record.get("agreed") is True),
        "high_confidence_count": len(high_confidence),
        "high_confidence_rate": round(100.0 * len(high_confidence) / total, 2) if total else None,
        # 严格口径：只在校验结果内部算一致率（分母=可程序化比对题数）。
        "programmatic_agreement_rate": (
            round(100.0 * len(agreed_list) / comparable_count, 2) if comparable_count else None
        ),
        "effective_verification_rate": round(100.0 * comparable_count / total, 2) if has_cross and total else None,
        "programmatic_comparable_count": comparable_count if has_cross else None,
        # 裁判统计（A/B 不一致或程序化不可比时介入；结果纳入总体与分层统计）。
        "has_judge": bool(judge["judge_calls"]),
        **judge,
        "input_completeness": input_stats["input_completeness"],
        "input_stats": input_stats,
        "unknown_cost_calls": cost["unknown_cost_calls"],
        "cost": cost,
        "by_subject": by_subject,
        "by_type": by_type,
    }


def summarize_eval(records: list[dict]) -> dict:
    """聚合总体、分层、输入完整率、核验与费用指标（保留旧字段兼容既有读取）。"""
    total = len(records)
    if total == 0:
        return {"total": 0}

    has_cross = any(record.get("has_verifier") for record in records)
    summary = _aggregate(records, has_cross)

    strata_groups: dict[str, list[dict]] = defaultdict(list)
    for record in records:
        names = record.get("strata")
        if isinstance(names, str):
            names = [names]
        elif not isinstance(names, list) or not names:
            names = ["text"]
        for name in names:
            if name in STRATA:
                strata_groups[name].append(record)
    summary["strata_counts"] = {stratum: len(strata_groups.get(stratum, [])) for stratum in STRATA}
    summary["strata"] = {}
    for stratum in STRATA:
        group = strata_groups.get(stratum) or []
        if not group:
            summary["strata"][stratum] = {"total": 0}
            continue
        summary["strata"][stratum] = _aggregate(
            group, any(record.get("has_verifier") for record in group)
        )

    # 互斥主分层口径：每道题只计一次，便于和“一题多标签”的 strata 对照。
    primary_groups: dict[str, list[dict]] = defaultdict(list)
    for record in records:
        primary = record.get("stratum")
        if primary not in STRATA:
            primary = primary_stratum(record.get("strata"))
        primary_groups[primary].append(record)
    summary["stratum_breakdown"] = {
        stratum: (
            _aggregate(primary_groups[stratum], any(r.get("has_verifier") for r in primary_groups[stratum]))
            if primary_groups.get(stratum) else {"total": 0}
        )
        for stratum in STRATA
    }
    # 单独定位：模型答错题与题库输入缺项题（最多各 200 条，完整明细仍在 records）。
    summary["wrong_qids"] = [
        record.get("qid") for record in records if record.get("a_correct") is False
    ][:200]
    summary["missing_input_qids"] = [
        record.get("qid") for record in records if record.get("missing_inputs")
    ][:200]
    # 字面一致但未核验 / 裁判介入的题目单独列出，便于人工复核，不混入一致率。
    summary["literal_only_qids"] = [
        record.get("qid") for record in records if _is_literal_only(record)
    ][:200]
    summary["judge_qids"] = [
        record.get("qid") for record in records
        if isinstance(record.get("judge"), dict) or record.get("judge_error")
    ][:200]
    return summary


def delta_from(value_a, value_b, digits=2):
    if isinstance(value_a, (int, float)) and isinstance(value_b, (int, float)):
        return round(value_b - value_a, digits)
    return None


def compare_reports(report_a: dict, report_b: dict) -> dict:
    """A/B diff two evaluation reports (e.g. prompt v1 vs v2, or model A vs model B)."""
    ma, mb = report_a["metrics"], report_b["metrics"]
    rec_a = {r["qid"]: r for r in report_a.get("records", [])}
    rec_b = {r["qid"]: r for r in report_b.get("records", [])}
    common = sorted(set(rec_a) & set(rec_b))
    improved = [q for q in common if not rec_a[q].get("a_correct") and rec_b[q].get("a_correct")]
    regressed = [q for q in common if rec_a[q].get("a_correct") and not rec_b[q].get("a_correct")]

    def delta(key, digits=2):
        value_a, value_b = ma.get(key), mb.get(key)
        if isinstance(value_a, (int, float)) and isinstance(value_b, (int, float)):
            return round(value_b - value_a, digits)
        return None

    strata_a = ma.get("strata") if isinstance(ma.get("strata"), dict) else {}
    strata_b = mb.get("strata") if isinstance(mb.get("strata"), dict) else {}
    strata_delta = {}
    for name in sorted(set(strata_a) | set(strata_b)):
        block_a = strata_a.get(name) or {}
        block_b = strata_b.get(name) or {}
        strata_delta[name] = {
            "total_delta": (block_b.get("total") or 0) - (block_a.get("total") or 0),
            "single_accuracy_delta": delta_from(block_a.get("single_accuracy"), block_b.get("single_accuracy"), 2),
            "input_completeness_delta": delta_from(
                block_a.get("input_completeness"), block_b.get("input_completeness"), 4
            ),
        }

    # 题库版本不一致的报告不能直接比较：题目内容可能已变，差异无法归因到模型或提示词。
    bank_a = report_a.get("bank_version") or (report_a.get("metrics") or {}).get("bank_version")
    bank_b = report_b.get("bank_version") or (report_b.get("metrics") or {}).get("bank_version")
    bank_mismatch = bool(bank_a and bank_b and bank_a != bank_b)

    return {
        "name_a": report_a.get("name", "A"),
        "name_b": report_b.get("name", "B"),
        "bank_version_a": bank_a,
        "bank_version_b": bank_b,
        "bank_mismatch": bank_mismatch,
        "comparable": not bank_mismatch,
        "warning": (
            "两份报告基于不同题库版本（%s vs %s），差异不能直接归因到模型或提示词" % (bank_a, bank_b)
            if bank_mismatch else None
        ),
        "common_questions": len(common),
        "single_accuracy_delta": round((mb.get("single_accuracy") or 0) - (ma.get("single_accuracy") or 0), 2),
        "cross_accuracy_delta": (
            round(mb["cross_verified_accuracy"] - ma["cross_verified_accuracy"], 2)
            if mb.get("cross_verified_accuracy") is not None and ma.get("cross_verified_accuracy") is not None
            else None
        ),
        "agreed_but_wrong_delta": (
            round(mb["agreed_but_wrong_rate"] - ma["agreed_but_wrong_rate"], 2)
            if mb.get("agreed_but_wrong_rate") is not None and ma.get("agreed_but_wrong_rate") is not None
            else None
        ),
        "improved_count": len(improved),
        "regressed_count": len(regressed),
        "improved_qids": improved[:20],
        "regressed_qids": regressed[:20],
        # P1 新增：输入完整率、有效核验率、未知费用计数与分层差异。
        "input_completeness_delta": delta("input_completeness", 4),
        "effective_verification_rate_delta": delta("effective_verification_rate", 2),
        "unknown_cost_calls_delta": (
            mb["unknown_cost_calls"] - ma["unknown_cost_calls"]
            if isinstance(mb.get("unknown_cost_calls"), int) and isinstance(ma.get("unknown_cost_calls"), int)
            else None
        ),
        # P2 新增：口径对齐后的字面一致计数 / 高可信计数 / 裁判统计差异。
        "literal_only_count_delta": delta("literal_only_count", 0),
        "high_confidence_count_delta": delta("high_confidence_count", 0),
        "programmatic_agreement_rate_delta": delta("programmatic_agreement_rate", 2),
        "judge_calls_delta": delta("judge_calls", 0),
        "judge_agreement_rate_delta": delta("judge_agreement_rate", 2),
        "judge_correct_rate_delta": delta("judge_correct_rate", 2),
        "strata_delta": strata_delta,
    }


# ---------------------------------------------------------------------------
# 评测执行
# ---------------------------------------------------------------------------

def _model_prices(provider: dict | None, model: str | None):
    """读取 ai_models 单价（元/百万 tokens）；未配置/不可读时返回 (None, None)。"""
    provider = provider if isinstance(provider, dict) else {}
    user_id = provider.get("user_id")
    provider_id = provider.get("id")
    if not user_id or not provider_id or not model:
        return None, None
    try:
        with db.connection() as con:
            row = con.execute(
                "SELECT input_price, output_price FROM ai_models "
                "WHERE user_id=? AND provider_id=? AND model_id=?",
                (user_id, provider_id, model),
            ).fetchone()
    except Exception:
        return None, None
    if row is None:
        return None, None
    input_price = row["input_price"] if not isinstance(row, dict) else row.get("input_price")
    output_price = row["output_price"] if not isinstance(row, dict) else row.get("output_price")
    return input_price, output_price


def _item_content_refs(item: dict) -> list[dict]:
    """兼容旧 golden（无 content_refs）：按文字题干/选项补出内容单元。"""
    refs = item.get("content_refs")
    if isinstance(refs, list) and refs:
        return [dict(ref) for ref in refs if isinstance(ref, dict)]
    refs = []
    if str(item.get("stem_plain") or "").strip():
        refs.append(_make_ref("stem", "题干", None, "text"))
    for option in item.get("options") or []:
        if not isinstance(option, dict):
            continue
        label = str(option.get("label") or "")
        if str(option.get("text") or "").strip():
            refs.append(_make_ref(
                f"option:{label}" if label else "option",
                f"选项{label}" if label else "选项",
                None, "text",
            ))
    if str(item.get("material_text") or "").strip():
        refs.append(_make_ref("material", "材料", None, "text"))
    if str(item.get("shared_text") or "").strip():
        refs.append(_make_ref("shared", "共用题干", None, "text"))
    return refs


def _item_evidence(item: dict, fallback_formulas=None) -> dict:
    evidence = {
        "题干": item.get("stem_text") or item.get("stem_plain") or "",
        "选项": item.get("options") or [],
        "学科": item.get("subject") or "",
        "题型": item.get("type_label") or item.get("type") or "",
    }
    if str(item.get("material_text") or "").strip():
        evidence["材料"] = item["material_text"]
    if str(item.get("shared_text") or "").strip():
        evidence["共用题干"] = item["shared_text"]
    if fallback_formulas:
        evidence["公式文本降级"] = fallback_formulas
    return evidence


def _report_bank_version(records: list[dict]) -> str:
    versions = sorted({str(record.get("bank_version")) for record in records if record.get("bank_version")})
    if not versions:
        return "未标注"
    return versions[0] if len(versions) == 1 else "；".join(versions)


def _save_eval_report(report: dict) -> bool:
    """写入 eval_runs；旧库缺 013 新列时退回旧列插入，失败也不影响本地报告。"""
    try:
        metrics = report.get("metrics") or {}
        row_full = (
            report.get("name") or "eval-run",
            report.get("model") or "",
            report.get("verify_model"),
            report.get("prompt_version") or ai.PROMPT_VERSION,
            metrics.get("total") or 0,
            json.dumps(metrics, ensure_ascii=False),
            db.utcnow(),
            report.get("bank_version") or "未标注",
            json.dumps(metrics.get("strata_counts") or {}, ensure_ascii=False),
            json.dumps(metrics.get("input_stats") or {}, ensure_ascii=False),
            json.dumps(report, ensure_ascii=False),
        )
    except Exception:
        return False
    try:
        with db.connection() as con:
            con.execute(
                "INSERT INTO eval_runs(name,model,verify_model,prompt_version,total,metrics_json,created_at,"
                "bank_version,strata,input_stats_json,report_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                row_full,
            )
        return True
    except Exception:
        pass
    try:
        with db.connection() as con:
            con.execute(
                "INSERT INTO eval_runs(name,model,verify_model,prompt_version,total,metrics_json,created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                row_full[:7],
            )
        return True
    except Exception:
        return False


def _eval_mode(has_verifier: bool) -> str:
    """断点续评的 mode 维度：single=new 单模型，cross=开启核验模型。"""
    return "cross" if has_verifier else "single"


def _progress_key(qid, model: str, mode: str) -> str:
    return f"{qid}\t{model}\t{mode}"


def _load_progress_records(path, *, model: str, mode: str) -> dict:
    """读取 JSONL 已完成集合（坏行忽略）；键为 qid+model+mode，值保留整条记录。"""
    done: dict[str, dict] = {}
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return done
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(record, dict):
            continue
        if record.get("model") != model or record.get("mode") != mode:
            continue
        qid = record.get("qid")
        if not qid:
            continue
        done[_progress_key(str(qid), model, mode)] = record
    return done


class _ProgressWriter:
    """JSONL 逐题追加器：每题完成立即 flush，评测中断后可直接 --resume 续评。"""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = self.path.open("a", encoding="utf-8")
        self._lock = threading.Lock()
        self.written = 0

    def write(self, record: dict) -> None:
        try:
            line = json.dumps(record, ensure_ascii=False)
        except (TypeError, ValueError):
            return
        with self._lock:
            try:
                self._stream.write(line + "\n")
                self._stream.flush()
                self.written += 1
            except (OSError, ValueError):
                pass

    def close(self) -> None:
        with self._lock:
            try:
                self._stream.close()
            except (OSError, ValueError):
                pass


def _needs_judge(cmp) -> bool:
    """A/B 不一致、程序化不可比或仅字面一致时，裁判介入；已核验一致则省调用。"""
    if not isinstance(cmp, dict):
        return True
    if cmp.get("comparable") is not True:
        return True
    if cmp.get("agreed") is not True:
        return True
    return cmp.get("method") == _LITERAL_ONLY_METHOD


def _parse_judge_verdict(content):
    """严格解析裁判 JSON（复用 verify 的裁判契约）；返回 (dict|None, error|None)。"""
    from .ai.adapters import _strip_json_fences

    try:
        parsed = json.loads(_strip_json_fences(str(content or "")))
    except Exception:  # noqa: BLE001 - 模型输出不可控，解析失败降级为 judge_error
        return None, "judge_unparsed"
    if not isinstance(parsed, dict) or type(parsed.get("agreed")) is not bool:
        return None, "judge_format_invalid"
    summary = parsed.get("summary") or parsed.get("reason") or ""
    if not isinstance(summary, str):
        return None, "judge_format_invalid"
    parsed.setdefault("summary", summary)
    parsed.setdefault("differences", [])
    if parsed.get("which_is_right") not in ("A", "B", "neither", "unsure"):
        parsed["which_is_right"] = "unsure"
    return parsed, None


def _judge_verdict_correct(verdict: dict, record: dict):
    """裁判指认的答案是否与原卷参考答案一致；无法判定返回 None。"""
    which = verdict.get("which_is_right")
    a_ok = record.get("a_correct")
    b_ok = record.get("b_correct")
    if which == "A":
        return a_ok
    if which == "B":
        return b_ok
    if which == "neither":
        if a_ok is None or b_ok is None:
            return None
        return (not a_ok) and (not b_ok)
    return None


async def run_evaluation(
    items: list[dict],
    provider: dict,
    model: str,
    *,
    verifier: dict | None = None,
    verify_model: str | None = None,
    judge: dict | None = None,
    judge_model: str | None = None,
    requirements: str = "",
    concurrency: int = 3,
    name: str = "eval-run",
    progress_path: Path | str | None = None,
    resume: bool = False,
) -> dict:
    from .ai.verify import (
        _structured_solve_prompt,
        _try_parse_structured_solution,
        build_judge_prompt,
    )

    sem = asyncio.Semaphore(max(1, concurrency))
    price_a = _model_prices(provider, model)
    has_verifier = bool(verifier and verify_model)
    mode = _eval_mode(has_verifier)
    price_b = _model_prices(verifier, verify_model) if has_verifier else (None, None)
    # 裁判：只有显式传入 judge / judge_model 才启用；缺失的 provider/model 用核验模型兜底
    # （与线上 verify 的降级顺序一致），绝不使用主模型 A 当自己的裁判。
    has_judge = bool(has_verifier and (judge or judge_model))
    judge_provider = judge or verifier
    judge_model_eff = judge_model or verify_model
    price_c = _model_prices(judge_provider, judge_model_eff) if has_judge else (None, None)

    completed = (
        _load_progress_records(progress_path, model=model, mode=mode)
        if (resume and progress_path is not None) else {}
    )
    pending_items = [
        item for item in items
        if _progress_key(str(item.get("qid")), model, mode) not in completed
    ]
    reused_count = len(items) - len(pending_items)

    writer = None
    if progress_path is not None:
        try:
            writer = _ProgressWriter(progress_path)
        except OSError:
            writer = None

    def finish(rec: dict) -> dict:
        rec.setdefault("mode", mode)
        if writer is not None:
            writer.write(rec)
        return rec

    async def eval_one(item: dict) -> dict:
        refs = _item_content_refs(item)
        image_parts, delivered_assets, image_notes = await asyncio.to_thread(
            collect_image_parts, item, refs
        )
        stats = compute_input_completeness(
            refs, delivered_assets=delivered_assets, image_notes=image_notes
        )
        fallback_formulas = [
            ref.get("fallback_text")
            for ref in stats["content_refs"]
            if not ref.get("delivered") and ref.get("fallback_text")
        ]
        evidence = _item_evidence(item, fallback_formulas)
        prompt = _structured_solve_prompt(evidence, requirements)

        rec = {
            "qid": item["qid"],
            "subject": item.get("subject") or "",
            "type": item.get("type") or "",
            "strata": item.get("strata") or ["text"],
            "stratum": item.get("stratum") or primary_stratum(item.get("strata")),
            "bank_version": item.get("bank_version") or "未标注",
            "reference": item.get("reference_answer") or item.get("reference") or "",
            "reference_answer": item.get("reference_answer") or item.get("reference") or "",
            "reference_source": item.get("reference_source") or "调用方传入的参考答案",
            "prompt_version": ai.PROMPT_VERSION,
            "model": model,
            "mode": mode,
            "verify_model": verify_model if has_verifier else None,
            "input_completeness": stats["input_completeness"],
            "missing_inputs": stats["missing_inputs"],
            "content_refs": stats["content_refs"],
            "input_expected_units": stats["expected_units"],
            "input_delivered_units": stats["delivered_units"],
            "has_verifier": has_verifier,
            "has_judge": has_judge,
            "judge_model": judge_model_eff if has_judge else None,
            "agreed": None,
            "judge": None,
            "judge_agrees": None,
            "judge_correct": None,
        }
        call_costs = []
        try:
            async with sem:
                call_a = await ai._call_model(provider, model, prompt, image_parts or None, True, token_limit=2200)
            sol_a = _try_parse_structured_solution(call_a["content"])
            ans_a = (sol_a or {}).get("final_answer")
            a_ok = await _check_prediction_correct_async(
            ans_a, rec["reference"], item.get("answer_kind") or "text")
            rec["answer_a"] = (ans_a or {}).get("value") if isinstance(ans_a, dict) else ans_a
            rec["a_correct"] = bool(a_ok)
            rec["usage_a"] = call_a.get("usage") or {}
            call_costs.append(estimate_call_cost(rec["usage_a"], *price_a))
        except Exception as exc:
            rec["a_correct"] = False
            rec["error"] = str(exc)
            call_costs.append({
                "currency": "CNY",
                "input_tokens": None,
                "output_tokens": None,
                "price_known": False,
                "unknown_cost_calls": 1,
                "estimated_cost": None,
                "known_cost_subtotal": None,
                "reason": "主模型调用失败",
            })
            rec["cost"] = combine_call_costs(call_costs)
            return finish(rec)

        if has_verifier:
            cmp = None
            call_b_content = ""
            try:
                async with sem:
                    call_b = await ai._call_model(
                        verifier, verify_model, prompt, image_parts or None, True, token_limit=2200
                    )
                sol_b = _try_parse_structured_solution(call_b["content"])
                ans_b = (sol_b or {}).get("final_answer")
                cmp = await asyncio.to_thread(
                    ai.compare_answers, ans_a, ans_b, rec["reference"])
                rec["answer_b"] = (ans_b or {}).get("value") if isinstance(ans_b, dict) else ans_b
                rec["b_correct"] = bool(await _check_prediction_correct_async(
                    ans_b, rec["reference"], item.get("answer_kind") or "text"))
                rec["agreed"] = bool(cmp["comparable"] and cmp["agreed"])
                rec["verification_comparable"] = bool(cmp.get("comparable"))
                rec["verification_method"] = cmp.get("method")
                # 口径对齐：字面一致/已核验/与原卷一致分别落盘，供统计口径复用。
                rec["verified"] = bool(cmp.get("verified"))
                rec["literal_only"] = cmp.get("method") == _LITERAL_ONLY_METHOD
                rec["matches_reference"] = cmp.get("matches_reference")
                rec["reference_verified"] = cmp.get("reference_verified")
                if "trace" in cmp:
                    rec["verification_trace"] = cmp.get("trace")
                rec["usage_b"] = call_b.get("usage") or {}
                call_costs.append(estimate_call_cost(rec["usage_b"], *price_b))
                call_b_content = str(call_b.get("content") or "")
            except Exception as exc:
                rec["agreed"] = False
                rec["verification_comparable"] = False
                rec["verify_error"] = str(exc)
                call_costs.append({
                    "currency": "CNY",
                    "input_tokens": None,
                    "output_tokens": None,
                    "price_known": False,
                    "unknown_cost_calls": 1,
                    "estimated_cost": None,
                    "known_cost_subtotal": None,
                    "reason": "核验模型调用失败",
                })
            # A/B 不一致、程序化不可比或仅字面一致时，裁判模型介入（严格 JSON 契约）。
            if has_judge and isinstance(cmp, dict) and _needs_judge(cmp):
                judge_prompt = build_judge_prompt(
                    {**evidence, "原卷参考答案": rec["reference"]},
                    str(call_a.get("content") or ""),
                    call_b_content,
                )
                try:
                    async with sem:
                        j_call = await ai._call_model(
                            judge_provider, judge_model_eff, judge_prompt,
                            image_parts or None, True, token_limit=2000,
                        )
                    rec["usage_judge"] = j_call.get("usage") or {}
                    call_costs.append(estimate_call_cost(rec["usage_judge"], *price_c))
                    verdict, judge_error = _parse_judge_verdict(j_call.get("content"))
                    if judge_error:
                        rec["judge_error"] = judge_error
                    else:
                        rec["judge"] = {
                            "model": j_call.get("model") or judge_model_eff,
                            "provider_name": j_call.get("provider_name"),
                            **verdict,
                        }
                        rec["judge_agrees"] = bool(verdict.get("agreed"))
                        rec["judge_correct"] = _judge_verdict_correct(verdict, rec)
                except Exception as exc:
                    rec["judge_error"] = str(exc)
                    call_costs.append({
                        "currency": "CNY",
                        "input_tokens": None,
                        "output_tokens": None,
                        "price_known": False,
                        "unknown_cost_calls": 1,
                        "estimated_cost": None,
                        "known_cost_subtotal": None,
                        "reason": "裁判模型调用失败",
                    })
        rec["cost"] = combine_call_costs(call_costs)
        return finish(rec)

    def failed_record(item: dict, exc: BaseException) -> dict:
        return {
            "qid": item["qid"],
            "subject": item.get("subject") or "",
            "type": item.get("type") or "",
            "strata": item.get("strata") or ["text"],
            "stratum": item.get("stratum") or primary_stratum(item.get("strata")),
            "bank_version": item.get("bank_version") or "未标注",
            "reference": item.get("reference_answer") or "",
            "reference_answer": item.get("reference_answer") or "",
            "reference_source": item.get("reference_source") or "调用方传入的参考答案",
            "prompt_version": ai.PROMPT_VERSION,
            "model": model,
            "mode": mode,
            "verify_model": verify_model if has_verifier else None,
            "input_completeness": 0.0,
            "missing_inputs": ["评测执行异常，未能确认实际送模内容"],
            "content_refs": [],
            "input_expected_units": 0,
            "input_delivered_units": 0,
            "a_correct": False,
            "has_verifier": has_verifier,
            "has_judge": has_judge,
            "agreed": False,
            "verification_comparable": False,
            "cost": combine_call_costs([{
                "currency": "CNY",
                "input_tokens": None,
                "output_tokens": None,
                "price_known": False,
                "unknown_cost_calls": 1,
                "estimated_cost": None,
                "known_cost_subtotal": None,
                "reason": "评测执行异常",
            }]),
            "error": str(exc),
        }

    records_by_key: dict[str, dict] = {
        _progress_key(str(item.get("qid")), model, mode): completed[
            _progress_key(str(item.get("qid")), model, mode)
        ]
        for item in items
        if _progress_key(str(item.get("qid")), model, mode) in completed
    }
    try:
        results = await asyncio.gather(
            *(eval_one(it) for it in pending_items), return_exceptions=True,
        )
    except BaseException:
        # 取消/中断：已完成的题已逐题 flush，这里只需关闭文件后原样抛出。
        if writer is not None:
            writer.close()
        raise
    for item, result in zip(pending_items, results):
        if isinstance(result, Exception):
            rec = failed_record(item, result)
            if writer is not None:
                writer.write(rec)
        else:
            rec = result
        records_by_key[_progress_key(str(item.get("qid")), model, mode)] = rec
    if writer is not None:
        writer.close()

    records = [
        records_by_key[_progress_key(str(item.get("qid")), model, mode)]
        for item in items
    ]
    metrics = summarize_eval(records)
    report = {
        "name": name,
        "model": model,
        "verify_model": verify_model,
        "judge_model": judge_model_eff if has_judge else None,
        "mode": mode,
        "prompt_version": ai.PROMPT_VERSION,
        "created_at": db.utcnow(),
        "bank_version": _report_bank_version(records),
        "resumed_count": reused_count,
        "executed_count": len(pending_items),
        "progress_path": str(progress_path) if progress_path is not None else None,
        "metrics": metrics,
        "records": records,
    }
    try:
        saved = await asyncio.to_thread(_save_eval_report, report)
    except Exception:
        saved = False
    report["saved_to_eval_runs"] = saved
    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(description="知序 · 分层评测集构建与模型/提示词 A/B 评测工具")
    parser.add_argument("--build-golden", action="store_true", help="从只读题库抽样生成评测集")
    parser.add_argument("--strata", action="store_true", help="分层模式：生成 eval/golden-v2.jsonl（需配合 --build-golden）")
    parser.add_argument("--count", type=int, default=100, help="评测集题目数（默认 100）")
    parser.add_argument("--golden", type=Path, default=None,
                        help="评测集路径（默认 eval/golden.jsonl；分层模式默认 eval/golden-v2.jsonl）")
    parser.add_argument("--user-id", type=int, default=1, help="读取模型连接配置的用户 ID")
    parser.add_argument("--provider-id", type=int, help="主模型连接 ID")
    parser.add_argument("--model", type=str, help="主模型 ID")
    parser.add_argument("--verify-provider-id", type=int, help="核验模型连接 ID（开启交叉验证评测）")
    parser.add_argument("--verify-model", type=str, help="核验模型 ID")
    parser.add_argument("--judge-provider-id", type=int,
                        help="裁判模型连接 ID（A/B 不一致或程序化不可比时介入；缺省复用核验模型连接）")
    parser.add_argument("--judge-model", type=str,
                        help="裁判模型 ID（启用裁判；缺省复用核验模型）")
    parser.add_argument("--limit", type=int, default=100, help="本次评测抽跑前 N 题")
    parser.add_argument("--name", type=str, default="eval", help="本次评测名称")
    parser.add_argument("--requirements", type=str, default="", help="附加提示词要求（用于 A/B 对比）")
    parser.add_argument("--out", type=Path, help="保存评测报告 JSON 或评测集路径")
    parser.add_argument("--out-jsonl", type=Path,
                        help="逐题结果 JSONL 落盘路径（每题完成即追加，支持 --resume 断点续评）")
    parser.add_argument("--resume", action="store_true",
                        help="断点续评：读取 --out-jsonl 中已完成记录，跳过相同 qid+model+mode")
    parser.add_argument("--compare", nargs=2, type=Path, metavar=("REPORT_A", "REPORT_B"), help="对比两份评测报告")
    args = parser.parse_args(argv)

    if args.strata and not args.build_golden:
        parser.error("--strata 需与 --build-golden 一起使用")
    if args.resume and not args.out_jsonl:
        parser.error("--resume 需与 --out-jsonl 一起使用（从该 JSONL 读取已完成题目）")

    # 只读/离线路径（构建评测集、比较报告）不得初始化应用库和任务表：
    # 旧实现会在这些命令里调用 ai.init_ai()，把线上正在运行的任务标成 interrupted。
    if args.build_golden:
        if args.strata:
            target = args.out or args.golden or DEFAULT_GOLDEN_V2
            if Path(target).resolve() == DEFAULT_GOLDEN.resolve():
                parser.error("分层评测集不会覆盖旧基线 eval/golden.jsonl，请用 --out 指定其他路径")
            items = build_golden_v2(Path(target), count=args.count)
            strata_cnt = Counter()
            for item in items:
                strata_cnt.update(item["strata"])
            version = next((item.get("bank_version") for item in items if item.get("bank_version")), "未标注")
            print(f"已生成 {len(items)} 道分层评测题 -> {target}")
            print(f"题库版本：{version}")
            print("分层分布：", {stratum: strata_cnt.get(stratum, 0) for stratum in STRATA})
            return
        target = args.golden or args.out or DEFAULT_GOLDEN
        items = build_golden_dataset(Path(target), count=args.count)
        subj_cnt = defaultdict(int)
        for it in items:
            subj_cnt[it["subject"]] += 1
        print(f"已生成 {len(items)} 道评测题 -> {target}")
        print("学科分布：", dict(sorted(subj_cnt.items())))
        return

    if args.compare:
        ra = json.loads(args.compare[0].read_text(encoding="utf-8"))
        rb = json.loads(args.compare[1].read_text(encoding="utf-8"))
        diff = compare_reports(ra, rb)
        print(json.dumps(diff, ensure_ascii=False, indent=2))
        return

    if not args.provider_id or not args.model:
        parser.error("运行评测需指定 --provider-id 与 --model（或使用 --build-golden / --compare）")
    if args.judge_provider_id and not args.judge_model:
        parser.error("--judge-provider-id 需与 --judge-model 一起使用")
    if args.judge_model and not (args.verify_provider_id and args.verify_model):
        parser.error("启用裁判需要先配置 --verify-provider-id 与 --verify-model（提供 A/B 对照）")

    # 真正跑模型评测时才需要应用库；不调用 ai.init_ai()，避免改动在跑任务的状态。
    db.init_db()
    practice.init_practice()
    golden_path = args.golden or DEFAULT_GOLDEN
    items = load_golden_dataset(golden_path)[:args.limit]
    prov = ai._selected_provider(args.user_id, args.provider_id, args.model)
    ver = (
        ai._selected_provider(args.user_id, args.verify_provider_id, args.verify_model)
        if (args.verify_provider_id and args.verify_model) else None
    )
    judg = (
        ai._selected_provider(args.user_id, args.judge_provider_id, args.judge_model)
        if (args.judge_provider_id and args.judge_model) else None
    )
    report = asyncio.run(run_evaluation(
        items, prov, args.model, verifier=ver, verify_model=args.verify_model,
        judge=judg, judge_model=args.judge_model,
        requirements=args.requirements, name=args.name,
        progress_path=args.out_jsonl, resume=args.resume,
    ))
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["metrics"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
