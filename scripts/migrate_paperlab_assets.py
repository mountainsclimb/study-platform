#!/usr/bin/env python
"""试卷工坊配图资产迁移：把旧 content 里的内容寻址文件名登记为 ``pl_assets`` 资产。

背景（P0「题图需要独立的所有权校验」）：旧版本把裁切后的内容寻址文件名直接写进
``content.images[i].asset``，附件本身没有所有者登记。本脚本扫描 ``pl_questions`` 的
content，按题目 owner 为旧文件名建立资产行（确认归属），迁移后旧引用即可继续读取；
新产生的配图一律由服务端签发 asset_id，不再需要本脚本。

特性：
- 默认 ``--dry-run``（只读预检，不写库）；
- 幂等：已登记的资产/同一用户已认领的旧文件名不会被重复登记；
- 只登记、不改写题目 content（旧引用登记后仍可用；前端 URL 不变）；
- 不触碰只读高考题库，也不删除任何文件。

用法（在 study-platform 目录）::

    python scripts/migrate_paperlab_assets.py --dry-run
    python scripts/migrate_paperlab_assets.py --apply --user-id 1
    python scripts/migrate_paperlab_assets.py --apply --json data/paperlab-assets-migration.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="试卷工坊配图资产迁移（默认 dry-run，只读预检）",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true",
                      help="实际写库登记资产（缺省只预检，不写库）")
    mode.add_argument("--dry-run", action="store_true",
                      help="只读预检（默认行为，显式写出便于运维脚本）")
    parser.add_argument("--user-id", type=int, default=None,
                        help="只处理指定用户的题目（缺省处理全部用户）")
    parser.add_argument("--limit", type=int, default=None,
                        help="最多扫描多少道题（调试/分批用）")
    parser.add_argument("--json", dest="json_path", default=None,
                        help="把完整报告写入指定 JSON 文件（目录不存在会自动创建）")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    from app.paperlab import assets

    apply = bool(args.apply)
    try:
        report = assets.migrate_legacy_assets(
            user_id=args.user_id, dry_run=not apply, limit=args.limit,
        )
    except Exception as exc:  # noqa: BLE001 - 首次部署尚未建表时给出可操作提示
        message = str(exc)
        if "pl_assets" in message or "no such table" in message.lower():
            print(
                "[paperlab-assets] 资产表 pl_assets 尚不存在：请先启动一次新版应用"
                "（启动时 init_tables 幂等建表），再执行本脚本",
                file=sys.stderr,
            )
            return 2
        raise
    mode = "apply" if args.apply else "dry-run"
    summary = {
        "mode": mode,
        "scanned_questions": report["scanned_questions"],
        "references": report["references"],
        "migrated": report["migrated"],
        "already": report["already"],
        "missing": report["missing"],
        "conflicts": len(report["conflicts"]),
    }
    print("[paperlab-assets] " + json.dumps(summary, ensure_ascii=False))
    if report["details"]:
        preview = report["details"][:20]
        print("[paperlab-assets] 明细（最多 20 条）：")
        for detail in preview:
            print("  " + json.dumps(detail, ensure_ascii=False))
        if len(report["details"]) > len(preview):
            print(f"  …… 其余 {len(report['details']) - len(preview)} 条见 JSON 报告")
    if args.json_path:
        path = Path(args.json_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        print(f"[paperlab-assets] 报告已写入 {path}")
    if not args.apply:
        print("[paperlab-assets] 当前为 dry-run（未写库）；确认无误后加 --apply 执行登记")
    if report["conflicts"]:
        print("[paperlab-assets] 存在登记失败条目，请查看 JSON 报告后人工复核", file=sys.stderr)
        return 1
    if report["missing"]:
        print("[paperlab-assets] 有旧引用的文件已缺失（保留原样，未登记）", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
