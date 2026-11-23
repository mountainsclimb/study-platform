#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""题库近似去重（RapidFuzz，只读、不改库）。

按《开源组件整合实施方案》§2.2 落地：

* 只以只读方式打开题库（``模式=ro``），不写任何库、不改任何状态；
* 按学科分组，再按题干长度分桶（桶宽 30 字，相邻桶也比对），桶内两两计算
  RapidFuzz 相似度；
* 输出不低于阈值的题目对（题号、学科、相似度、题干摘要）到 JSON 与 Markdown，
  只作为人工复核清单；是否隔离由管理员在内容管理页按 ``content_isolations`` 的
  现有流程决定。

用法：
    python scripts/dedup_questions.py
    python scripts/dedup_questions.py --threshold 92 --out screenshots/dedup-questions
    python scripts/dedup_questions.py --bank-dir ../高考题库_按学科/releases/bank-20261121-r5

依赖：``rapidfuzz``（开发依赖，见 requirements-dev.txt；不进生产镜像）。
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import unicodedata

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "screenshots" / "dedup-questions"
DEFAULT_THRESHOLD = 90.0
DEFAULT_MIN_LENGTH = 8
LENGTH_BUCKET = 30
DEFAULT_MAX_PAIRS = 5000
DEFAULT_MAX_PER_QUESTION = 5
STEM_SUMMARY_CHARS = 120
RAPIDFUZZ_REQUIRED = "rapidfuzz"

# 题干预处理：NFKC 归一化（全角/半角统一）、小写、去掉空白与常见标点。
_PUNCTUATION = frozenset(
    " \t\r\n　，。、；：？！“”‘’（）【】《》,.;:?!\"'()[]<>·…—_-"
)


def _fuzz():
    """延迟导入 RapidFuzz；缺失时给出可执行的安装提示。"""
    try:
        from rapidfuzz import fuzz
    except ImportError:  # pragma: no cover - 依赖已写入 requirements-dev.txt
        raise SystemExit(
            "缺少依赖 rapidfuzz。请先安装开发依赖：pip install -r requirements-dev.txt"
        ) from None
    return fuzz


def compact_stem(text: str) -> str:
    """规范题干用于比较：NFKC、小写、去空白与常见标点。"""
    value = unicodedata.normalize("NFKC", str(text or "")).lower()
    return "".join(char for char in value if char not in _PUNCTUATION)


def resolve_bank_path(bank_dir: str | None) -> Path:
    """解析题库数据库路径。

    优先级：``--bank-dir`` > 环境变量 ``STUDY_BANK_DIR`` > 仓库同级题库的
    ``releases/CURRENT`` 指针 > 题库根目录。``--bank-dir`` 既可以是发布包目录，
    也可以是题库根目录（此时跟随 ``releases/CURRENT``）。
    """
    if bank_dir:
        candidate = Path(bank_dir).expanduser().resolve()
        if candidate.is_file():
            return candidate
        pointer = candidate / "releases" / "CURRENT"
        if pointer.is_file():
            version = pointer.read_text(encoding="utf-8-sig").strip()
            followed = candidate / "releases" / version / "题库.db"
            if version and followed.is_file():
                return followed
        return candidate / "题库.db"

    configured = os.environ.get("STUDY_BANK_DIR", "").strip()
    if configured:
        return resolve_bank_path(configured)

    root = ROOT.parent / "高考题库_按学科"
    pointer = root / "releases" / "CURRENT"
    if pointer.is_file():
        version = pointer.read_text(encoding="utf-8-sig").strip()
        followed = root / "releases" / version / "题库.db"
        if version and followed.is_file():
            return followed
    return root / "题库.db"


def _open_readonly(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise SystemExit(f"未找到题库：{path}（可用 --bank-dir 指定）")
    return sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=30)


def load_questions(path: Path, *, min_length: int = DEFAULT_MIN_LENGTH) -> list[dict]:
    """读取题库题干（只读）。缺列或坏行的记录跳过，不猜测、不补写。"""
    con = _open_readonly(path)
    try:
        columns = {row[1] for row in con.execute("PRAGMA table_info(questions)")}
        if "stem_plain" not in columns:
            raise SystemExit("题库 questions 表缺少 stem_plain 列，无法做题干去重")
        selected = ["qid", "question_id", "subject", "stem_plain"]
        for optional in ("paper_name", "year", "practice_status"):
            if optional in columns:
                selected.append(optional)
        rows = con.execute(
            "SELECT %s FROM questions" % ", ".join(selected)
        ).fetchall()
        names = [item[0] for item in con.execute(
            "SELECT %s FROM questions LIMIT 0" % ", ".join(selected)
        ).description]
    finally:
        con.close()

    questions: list[dict] = []
    for row in rows:
        record = dict(zip(names, row))
        stem = str(record.get("stem_plain") or "").strip()
        compact = compact_stem(stem)
        if len(compact) < min_length:
            continue
        questions.append({
            "qid": str(record.get("qid") or ""),
            "question_id": str(record.get("question_id") or ""),
            "subject": str(record.get("subject") or "未分类"),
            "stem": stem,
            "compact": compact,
            "length": len(compact),
            "paper_name": str(record.get("paper_name") or ""),
            "year": str(record.get("year") or ""),
        })
    return questions


def _buckets(items: list[dict], width: int = LENGTH_BUCKET) -> dict[int, list[int]]:
    buckets: dict[int, list[int]] = {}
    for index, item in enumerate(items):
        buckets.setdefault(item["length"] // width, []).append(index)
    return buckets


def find_duplicate_pairs(
    questions: list[dict],
    *,
    threshold: float = DEFAULT_THRESHOLD,
    max_pairs: int = DEFAULT_MAX_PAIRS,
    max_per_question: int = DEFAULT_MAX_PER_QUESTION,
) -> list[dict]:
    """按学科分组、长度分桶后两两比对，返回相似度不低于阈值的题目对。

    * 桶宽 ``LENGTH_BUCKET``，同时与后一个桶比较，避免长度略跨桶的近重复漏检；
    * 每个题目最多保留 ``max_per_question`` 条（按相似度取最高），避免「重复源题」
      在清单里刷屏；总对数上限 ``max_pairs``，超出时按相似度截断并记录。
    """
    fuzz = _fuzz()
    threshold = min(100.0, max(0.0, float(threshold)))
    by_subject: dict[str, list[dict]] = {}
    for item in questions:
        by_subject.setdefault(item["subject"], []).append(item)

    pairs: list[dict] = []
    for subject in sorted(by_subject):
        if len(pairs) >= max_pairs:
            break
        items = sorted(by_subject[subject], key=lambda row: (row["length"], row["qid"]))
        buckets = _buckets(items)
        candidates: list[dict] = []
        seen: set[tuple[str, str]] = set()
        for key in sorted(buckets):
            left_indexes = buckets[key]
            right_indexes = buckets.get(key + 1, [])
            for position, left_index in enumerate(left_indexes):
                left = items[left_index]
                compare_indexes = left_indexes[position + 1:] + right_indexes
                for right_index in compare_indexes:
                    right = items[right_index]
                    pair_key = (left["qid"], right["qid"])
                    if pair_key in seen:
                        continue
                    seen.add(pair_key)
                    score = float(fuzz.ratio(left["compact"], right["compact"]))
                    if score < threshold:
                        continue
                    candidates.append({
                        "score": round(score, 2),
                        "left": left,
                        "right": right,
                    })
        # 每个题目只保留相似度最高的若干条。
        candidates.sort(
            key=lambda item: (-item["score"], item["left"]["qid"], item["right"]["qid"])
        )
        kept_per_question: dict[str, int] = {}
        for item in candidates:
            left_id, right_id = item["left"]["qid"], item["right"]["qid"]
            if kept_per_question.get(left_id, 0) >= max_per_question:
                continue
            if kept_per_question.get(right_id, 0) >= max_per_question:
                continue
            kept_per_question[left_id] = kept_per_question.get(left_id, 0) + 1
            kept_per_question[right_id] = kept_per_question.get(right_id, 0) + 1
            pairs.append(item)
            if len(pairs) >= max_pairs:
                break
        if len(pairs) >= max_pairs:
            break

    pairs.sort(key=lambda item: (-item["score"], item["left"]["qid"], item["right"]["qid"]))
    return pairs


def _summary(text: str, limit: int = STEM_SUMMARY_CHARS) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[: limit - 1] + "…"


def build_report(
    pairs: list[dict],
    *,
    bank_path: Path,
    total_questions: int,
    threshold: float,
    min_length: int,
    truncated: bool = False,
) -> dict:
    records = []
    for item in pairs:
        left, right = item["left"], item["right"]
        records.append({
            "qid_a": left["qid"],
            "qid_b": right["qid"],
            "question_id_a": left["question_id"],
            "question_id_b": right["question_id"],
            "subject": left["subject"],
            "similarity": item["score"],
            "length_a": left["length"],
            "length_b": right["length"],
            "paper_a": left["paper_name"],
            "paper_b": right["paper_name"],
            "stem_a": _summary(left["stem"]),
            "stem_b": _summary(right["stem"]),
        })
    by_subject: dict[str, int] = {}
    for record in records:
        by_subject[record["subject"]] = by_subject.get(record["subject"], 0) + 1
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "bank_path": str(bank_path),
        "total_questions": total_questions,
        "threshold": threshold,
        "min_length": min_length,
        "pair_count": len(records),
        "truncated": truncated,
        "pairs_by_subject": dict(sorted(by_subject.items())),
        "pairs": records,
        "notice": (
            "本清单只用于人工复核，脚本未写入题库或应用库；"
            "是否隔离请按内容管理页的 content_isolations 流程处理。"
        ),
    }


def render_markdown(report: dict) -> str:
    lines = [
        "# 题库近似去重清单",
        "",
        f"- 生成时间：{report['generated_at']}",
        f"- 题库：`{report['bank_path']}`",
        f"- 题干总数（去短文本后）：{report['total_questions']}",
        f"- 相似度阈值：{report['threshold']}",
        f"- 题目对数：{report['pair_count']}"
        + ("（已达上限，按相似度截断）" if report["truncated"] else ""),
        "",
        "> 本清单只用于人工复核，脚本未写入任何库。",
        "",
    ]
    if report["pairs_by_subject"]:
        lines.append("## 按学科分布")
        lines.append("")
        lines.append("| 学科 | 对数 |")
        lines.append("|---|---|")
        for subject, count in report["pairs_by_subject"].items():
            lines.append(f"| {subject} | {count} |")
        lines.append("")
    lines.append("## 题目对")
    lines.append("")
    lines.append("| # | 相似度 | 学科 | 题号 A | 题号 B | 题干 A | 题干 B |")
    lines.append("|---|---|---|---|---|---|---|")
    for index, item in enumerate(report["pairs"], 1):
        lines.append(
            "| %d | %.2f | %s | `%s` | `%s` | %s | %s |" % (
                index,
                item["similarity"],
                item["subject"],
                item["qid_a"],
                item["qid_b"],
                item["stem_a"].replace("|", "\\|"),
                item["stem_b"].replace("|", "\\|"),
            )
        )
    lines.append("")
    return "\n".join(lines)


def write_outputs(report: dict, out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "dedup-questions.json"
    md_path = out_dir / "dedup-questions.md"
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    md_path.write_text(render_markdown(report), encoding="utf-8", newline="\n")
    return json_path, md_path


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="题库题干近似去重（只读，输出人工复核清单）。"
    )
    parser.add_argument(
        "--bank-dir",
        default="",
        help="题库发布包或题库根目录；默认跟随 releases/CURRENT 或 STUDY_BANK_DIR",
    )
    parser.add_argument(
        "--out", default=str(DEFAULT_OUT), help="输出目录（默认 screenshots/dedup-questions）"
    )
    parser.add_argument(
        "--threshold", type=float, default=DEFAULT_THRESHOLD, help="相似度阈值 0–100（默认 90）"
    )
    parser.add_argument(
        "--min-length", type=int, default=DEFAULT_MIN_LENGTH, help="参与比对的题干最短长度（默认 8）"
    )
    parser.add_argument(
        "--max-pairs", type=int, default=DEFAULT_MAX_PAIRS, help="最多输出的题目对数（默认 5000）"
    )
    parser.add_argument(
        "--max-per-question",
        type=int,
        default=DEFAULT_MAX_PER_QUESTION,
        help="每个题目最多保留的对数（默认 5）",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    bank_path = resolve_bank_path(args.bank_dir or None)
    print(f"题库：{bank_path}")
    print(f"读取题干（最短 {args.min_length} 字）…")
    questions = load_questions(bank_path, min_length=max(1, args.min_length))
    print(f"参与比对：{len(questions)} 题")
    if not questions:
        print("没有可参与比对的题干，输出空清单。")

    pairs = find_duplicate_pairs(
        questions,
        threshold=args.threshold,
        max_pairs=max(1, args.max_pairs),
        max_per_question=max(1, args.max_per_question),
    )
    truncated = len(pairs) >= max(1, args.max_pairs)
    report = build_report(
        pairs,
        bank_path=bank_path,
        total_questions=len(questions),
        threshold=args.threshold,
        min_length=args.min_length,
        truncated=truncated,
    )
    json_path, md_path = write_outputs(report, Path(args.out))
    print(f"题目对：{len(pairs)}")
    print(f"JSON：{json_path}")
    print(f"Markdown：{md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
