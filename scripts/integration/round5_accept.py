"""第五轮端到端验收（W1/G1）：拍题块 → 作答证据 → 错题 → 四档排程 → 学习附件 → 跨页读取。

对应《知序功能整合与优化总方案》§8「整体验收」与并行方案 G1：
「拍题确认、入库、独立作答与错题重开通过；三种题源兼容，重复提交和重判只增加一项
有效证据，附件读写通过两账号及答前门控」。

本脚本使用 FastAPI TestClient 在**临时数据目录**内跑完整链路，不访问外网、不读写生产数据、
不调用真实模型；题库维持只读（固定 releases/CURRENT）。输出 `screenshots/round5-accept-report.json`
（或 `--report` 指定路径）。退出码：0=全部通过；1=存在失败项；2=参数/环境错误。

用法（仓库根执行）::

    python -X utf8 scripts/integration/round5_accept.py
    python -X utf8 scripts/integration/round5_accept.py --keep-data --report .omx/tmp/r5-accept.json
    python -X utf8 scripts/integration/round5_accept.py --skip-ocr-draft      # 不触发本地 OCR 推理
"""
from __future__ import annotations

import argparse
import io
import json
import os
import secrets
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REPORT = ROOT / "screenshots" / "round5-accept-report.json"
FIXED_CLOCK = "2026-11-22T00:00:00+00:00"


def _png_bytes() -> bytes:
    """生成一张带文字的验收用 PNG（不依赖外部样本文件）。"""
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (720, 420), "white")
    draw = ImageDraw.Draw(image)
    draw.text((24, 24), "已知函数 f(x)=x^2+2x, 求 f(1).", fill="black")
    draw.line((20, 90, 700, 90), fill="black", width=2)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


CHECKS: list[dict] = []


def record(name: str, ok: bool, detail: str, **extra) -> bool:
    entry = {"step": name, "ok": bool(ok), "detail": detail}
    entry.update(extra)
    CHECKS.append(entry)
    marker = "OK" if ok else "FAIL"
    print(f"[{marker}] {name}：{detail}")
    return ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="第五轮端到端验收（临时数据目录，TestClient）")
    parser.add_argument("--report", default=str(DEFAULT_REPORT), help="验收报告输出路径")
    parser.add_argument("--keep-data", action="store_true", help="保留临时数据目录（默认成功后清理）")
    parser.add_argument("--skip-ocr-draft", action="store_true", help="跳过 POST ocr-draft 的本地 OCR 推理")
    parser.add_argument("--quiet", action="store_true", help="只输出汇总")
    args = parser.parse_args(argv)

    started = time.time()
    data_dir = Path(tempfile.mkdtemp(prefix="zhixu-r5-accept-"))
    os.environ["STUDY_DATA_DIR"] = str(data_dir)
    os.environ["STUDY_ALLOWED_HOSTS"] = "127.0.0.1,localhost,[::1],testserver"
    os.environ["STUDY_INDEX_PREWARM"] = "0"
    os.environ["STUDY_REVIEW_CLOCK"] = FIXED_CLOCK
    os.environ.pop("STUDY_REVIEW_ENGINE", None)
    sys.path.insert(0, str(ROOT))

    try:
        from fastapi.testclient import TestClient

        from app.main import app
    except Exception as error:  # noqa: BLE001 - 导入失败属于环境错误
        print(f"[FAIL] 无法加载应用：{type(error).__name__}: {error}")
        return 2

    report: dict = {
        "scope": "第五轮 W1/G1 端到端验收",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "clock": FIXED_CLOCK,
        "data_dir": str(data_dir),
        "bank_version": None,
        "build_id": None,
        "ok": False,
        "checks": CHECKS,
    }
    try:
        with TestClient(app) as client_a:
            health = client_a.get("/api/health").json()
            report["bank_version"] = health.get("bank_version")
            report["build_id"] = health.get("build_id")
            record("应用就绪", health.get("status") == "ok",
                   f"build={report['build_id']} bank={report['bank_version']}")
            user_a = _register(client_a, "验收管理员", f"r5-admin-{secrets.token_hex(3)}")
            record("账号 A 注册（管理员）", bool(user_a), f"user_id={user_a.get('id') if user_a else '?'}")
            with TestClient(app) as client_b:
                user_b = _register(client_b, "验收学生", f"r5-student-{secrets.token_hex(3)}")
                record("账号 B 注册", bool(user_b), f"user_id={user_b.get('id') if user_b else '?'}")
                _run_flows(args, client_a, client_b, report)
    except Exception as error:  # noqa: BLE001 - 验收脚本要给出可读失败而不是堆栈中断
        import traceback

        traceback.print_exc()
        record("验收执行", False, f"未捕获异常：{type(error).__name__}: {error}")
    finally:
        report["ok"] = all(entry["ok"] for entry in CHECKS) and bool(CHECKS)
        report["duration_seconds"] = round(time.time() - started, 2)
        _write_report(report, Path(args.report), quiet=args.quiet)
        if not args.keep_data:
            _cleanup(data_dir)
    if not args.quiet:
        passed = sum(1 for entry in CHECKS if entry["ok"])
        print(f"\n汇总：{passed}/{len(CHECKS)} 通过 · 用时 {report['duration_seconds']}s · 报告 {args.report}")
        print("结论：", "ACCEPT OK" if report["ok"] else "ACCEPT FAIL")
    return 0 if report["ok"] else 1


def _register(client, username: str, password: str) -> dict | None:
    response = client.post("/api/auth/register", json={"username": username, "password": password})
    if response.status_code != 200:
        record("注册", False, f"HTTP {response.status_code} {response.text[:200]}")
        return None
    return response.json()


def _pick_bank_question(client) -> dict | None:
    """从只读题库挑一道可练、可自动判分的选择题（用于错题与四档排程链路）。"""
    for page in (1, 2):
        response = client.get("/api/questions", params={"subject": "数学", "page": page, "page_size": 30})
        if response.status_code != 200:
            continue
        for item in response.json().get("items") or []:
            if item.get("type") != "single_choice":
                continue
            if not item.get("practice_allowed", True):
                continue
            detail = client.get(f"/api/questions/{item['qid']}")
            if detail.status_code != 200:
                continue
            body = detail.json()
            options = [option for option in body.get("options") or [] if option.get("label")]
            if len(options) >= 2:
                return {"ref": body.get("ref") or item["qid"], "qid": item["qid"], "options": options}
    return None


def _run_flows(args, client_a, client_b, report: dict) -> None:
    # ------------------------------------------------------------------
    # 1. 拍题确认：识别块 + 公式校对字段往返（不依赖真实 OCR 引擎）
    # ------------------------------------------------------------------
    upload = client_a.post("/api/uploads/question", files={"file": ("accept.png", _png_bytes(), "image/png")})
    if upload.status_code != 200:
        record("拍题上传", False, f"HTTP {upload.status_code} {upload.text[:200]}")
    else:
        upload_id = upload.json()["id"]
        record("拍题上传", True, f"upload_id={upload_id}")
        blocks = [
            {
                "id": "b1", "kind": "text", "text": "已知函数 f(x)=x²+2x。", "latex": None,
                "order": 0, "page": 1, "confidence": 0.98,
                "bbox": {"x": 10, "y": 10, "w": 200, "h": 24}, "norm": {"x": 0.05, "y": 0.05, "w": 0.5, "h": 0.06},
            },
            {
                "id": "b2", "kind": "math", "text": "x^2+2x=0", "latex": "x^{2}+2x=0",
                "order": 1, "page": 1, "confidence": 0.9, "needs_review": False,
                "bbox": {"x": 10, "y": 40, "w": 180, "h": 24}, "norm": {"x": 0.05, "y": 0.15, "w": 0.45, "h": 0.06},
            },
        ]
        confirm = client_a.patch(
            f"/api/uploads/{upload_id}/confirm",
            json={"stem": "已知函数 f(x)=x²+2x。", "type": "short_answer", "blocks": blocks},
        )
        ok = confirm.status_code == 200 and len((confirm.json().get("confirmed") or {}).get("blocks") or []) == 2
        record("拍题确认含识别块", ok, f"HTTP {confirm.status_code}")
        intake = client_a.get(f"/api/uploads/{upload_id}/intake")
        confirmed_blocks = ((intake.json().get("confirmed") or {}).get("blocks")) or []
        record(
            "确认块往返（含公式 LaTeX 与原图定位）",
            bool(confirmed_blocks) and confirmed_blocks[1].get("latex") == "x^{2}+2x=0"
            and bool(confirmed_blocks[1].get("bbox")),
            f"blocks={len(confirmed_blocks)}",
        )
        bad = client_a.patch(
            f"/api/uploads/{upload_id}/confirm",
            json={"stem": "仍然合法的题干", "blocks": [{"id": "b1", "kind": "unknown", "text": "x"}]},
        )
        record("非法识别块被拒绝", bad.status_code == 422, f"HTTP {bad.status_code}")
        if not args.skip_ocr_draft:
            draft = client_a.post(f"/api/uploads/{upload_id}/ocr-draft")
            body = draft.json() if draft.status_code == 200 else {}
            record(
                "OCR 草稿接口兼容（可用/不可用均 200 且带 blocks）",
                draft.status_code == 200 and isinstance(body.get("blocks"), list),
                f"HTTP {draft.status_code} ocr_available={body.get('ocr_available')}",
            )

    # ------------------------------------------------------------------
    # 2. 独立作答：一份题只入账一次；证据与错题同事务落库
    # ------------------------------------------------------------------
    picked = _pick_bank_question(client_a)
    if not picked:
        record("题库取样", False, "未取到可自动判分的题库选择题")
        return
    record("题库取样", True, f"{picked['ref']}（{len(picked['options'])} 选项）")
    labels = [str(option["label"]).upper() for option in picked["options"]]
    submission_id = "accept-r5-" + secrets.token_hex(3)
    wrong_label = labels[-1]
    submit = client_a.post(
        "/api/attempts",
        data={"qid": picked["ref"], "answer": wrong_label, "submission_id": submission_id},
    )
    body = submit.json() if submit.status_code == 200 else {}
    record(
        "独立作答入账", submit.status_code == 200 and body.get("attempt_id"),
        f"HTTP {submit.status_code} result={body.get('result')}",
    )
    duplicate = client_a.post(
        "/api/attempts",
        data={"qid": picked["ref"], "answer": wrong_label, "submission_id": submission_id},
    )
    duplicate_body = duplicate.json() if duplicate.status_code == 200 else {}
    history = client_a.get("/api/attempts/history", params={"qid": picked["ref"]}).json().get("items") or []
    record(
        "重复提交只入账一次",
        duplicate_body.get("duplicate") is True and len(history) == 1,
        f"duplicate={duplicate_body.get('duplicate')} history={len(history)}",
    )
    evidence = client_a.get(f"/api/evidence/question/{picked['ref']}")
    evidence_body = evidence.json() if evidence.status_code == 200 else {}
    counts = evidence_body.get("counts") or {}
    record(
        "作答证据可读且只一条有效事件",
        evidence.status_code == 200 and int(counts.get("total") or 0) == 1,
        f"total={counts.get('total')} verdict={(evidence_body.get('effective') or {}).get('verdict')}",
    )
    wrongbook_body = client_a.get("/api/wrongbook", params={"page_size": 200}).json()
    wrongbook = wrongbook_body.get("items") or []
    wrong_hit = any((item.get("ref") == picked["ref"]) or (item.get("qid") == picked["qid"]) for item in wrongbook)
    record("错误作答进入错题本", wrong_hit, f"错题条数={len(wrongbook)}")
    wrong_item = next(
        (item for item in wrongbook if item.get("ref") == picked["ref"] or item.get("qid") == picked["qid"]), {},
    )
    record(
        "错题到期来源为服务端复习卡（M2 统一到期）",
        wrong_item.get("due_source") == "review-cards" and bool(wrong_item.get("next_review_at")),
        f"due_source={wrong_item.get('due_source')} due={wrong_item.get('next_review_at')}",
    )

    # ------------------------------------------------------------------
    # 3. 四档复习：迁移 → 到期 → 评分 → 幂等重放 → 版本冲突
    # ------------------------------------------------------------------
    sync = client_a.post("/api/review-cards/sync")
    sync_body = sync.json() if sync.status_code == 200 else {}
    due = client_a.get("/api/review-cards/due", params={"include_future": "true", "limit": 200})
    due_body = due.json() if due.status_code == 200 else {}
    cards = [
        card for card in due_body.get("items") or []
        if card.get("question_ref") == picked["ref"] or card.get("qid") == picked["qid"]
    ]
    record(
        "错题迁移为复习卡", sync.status_code == 200 and bool(cards),
        f"created={sync_body.get('created')} 命中卡={len(cards)}",
    )
    if cards:
        card = cards[0]
        key = "accept-rating-" + secrets.token_hex(3)
        rated = client_a.post(
            f"/api/review-cards/{card['card_id']}/ratings",
            json={"rating": 3, "card_version": card.get("version") or 1, "idempotency_key": key},
        )
        rated_body = rated.json() if rated.status_code == 200 else {}
        new_due = rated_body.get("due_at")
        record(
            "四档评分写入服务端到期时间",
            rated.status_code == 200 and bool(new_due) and rated_body.get("degraded") is False,
            f"rating=3 due={new_due} algorithm={rated_body.get('algorithm')} {rated_body.get('algorithm_version')}",
        )
        replay = client_a.post(
            f"/api/review-cards/{card['card_id']}/ratings",
            json={"rating": 3, "card_version": card.get("version") or 1, "idempotency_key": key},
        )
        replay_body = replay.json() if replay.status_code == 200 else {}
        record(
            "幂等键重放不重复推进",
            replay.status_code == 200 and replay_body.get("replayed") is True
            and replay_body.get("due_at") == new_due,
            f"replayed={replay_body.get('replayed')} due一致={replay_body.get('due_at') == new_due}",
        )
        stale = client_a.post(
            f"/api/review-cards/{card['card_id']}/ratings",
            json={"rating": 4, "card_version": card.get("version") or 1,
                  "idempotency_key": "accept-stale-" + secrets.token_hex(3)},
        )
        record("过期卡片版本被拒绝", stale.status_code == 409, f"HTTP {stale.status_code}")
        # M2：评分后旧错题表被同一事务镜像，错题本/首页/学情/统计读到同一个到期值。
        wb_after = client_a.get("/api/wrongbook", params={"page_size": 200}).json()
        wb_after_item = next(
            (item for item in wb_after.get("items") or []
             if item.get("ref") == picked["ref"] or item.get("qid") == picked["qid"]), {},
        )
        home_after = client_a.get("/api/insight/home").json()
        overview_after = client_a.get("/api/insight/overview").json()
        stats_after = client_a.get("/api/stats").json()
        home_review = home_after.get("review") or {}
        overview_review = overview_after.get("review") or {}
        stats_review = stats_after.get("review") or {}
        record(
            "评分后错题本/首页/学情/统计统一到期",
            wb_after_item.get("next_review_at") == new_due
            and home_review.get("available") is True
            and home_review.get("due_source") == "review-cards"
            and overview_review.get("due_source") == "review-cards"
            and stats_review.get("due_source") == "review-cards",
            f"错题本={wb_after_item.get('next_review_at')} 首页due_today={home_review.get('due_today')} "
            f"学情due_now={overview_review.get('due_now')} 统计due_now={stats_review.get('due_now')}",
        )
        compare = client_a.get("/api/review-cards/compare")
        compare_body = compare.json() if compare.status_code == 200 else {}
        record(
            "镜像后新旧到期对账无漂移",
            compare.status_code == 200
            and compare_body.get("matched") == compare_body.get("checked")
            and not compare_body.get("mismatched"),
            f"checked={compare_body.get('checked')} matched={compare_body.get('matched')} "
            f"mismatched={len(compare_body.get('mismatched') or [])}",
        )
        foreign = client_b.post(
            f"/api/review-cards/{card['card_id']}/ratings",
            json={"rating": 3, "card_version": 1, "idempotency_key": "accept-foreign-" + secrets.token_hex(3)},
        )
        record("他人复习卡不可评分（404 不泄露存在性）", foreign.status_code == 404, f"HTTP {foreign.status_code}")

    # ------------------------------------------------------------------
    # 4. 学习附件：创建/版本/固定版本链接/跨账号 404/题目详情聚合
    # ------------------------------------------------------------------
    created = client_a.post(
        "/api/artifacts",
        json={
            "kind": "function_graph",
            "title": "二次函数参数图（验收）",
            "data": {"type": "quadratic", "a": 1, "b": 2, "c": 0, "xRange": [-5, 3]},
            "preview": {"kind": "svg-points", "points": [[0, 0], [1, 3]]},
            "source_ref": picked["ref"],
            "source_kind": "question",
            "links": [{"target_kind": "question", "target_key": picked["ref"], "revision": 1}],
        },
    )
    created_body = created.json() if created.status_code == 201 else {}
    artifact = created_body.get("artifact") or {}
    artifact_id = artifact.get("artifact_id")
    record("学习附件创建并固定版本链接", created.status_code == 201 and bool(artifact_id),
           f"artifact_id={artifact_id} revision={(created_body.get('revision') or {}).get('revision')}")
    if artifact_id:
        revised = client_a.patch(
            f"/api/artifacts/{artifact_id}",
            json={"data": {"type": "quadratic", "a": 1, "b": 2, "c": 1, "xRange": [-4, 4]},
                  "expected_revision": 1},
        )
        record("附件新版本（CAS）", revised.status_code == 200, f"HTTP {revised.status_code}")
        stale_revision = client_a.patch(
            f"/api/artifacts/{artifact_id}",
            json={"data": {"type": "quadratic", "a": 2}, "expected_revision": 1},
        )
        record("附件过期版本被拒绝", stale_revision.status_code == 409, f"HTTP {stale_revision.status_code}")
        by_target = client_a.get(
            "/api/artifacts/by-target", params={"target_kind": "question", "target_key": picked["ref"]}
        )
        items = by_target.json().get("items") if by_target.status_code == 200 else []
        pinned = (items or [{}])[0]
        record(
            "题目按固定版本读取附件（发布/导出语义）",
            by_target.status_code == 200 and bool(items)
            and int((pinned.get("revision") or 0)) == 1,
            f"读到 revision={pinned.get('revision')}",
        )
        foreign_artifact = client_b.get(f"/api/artifacts/{artifact_id}")
        record("附件跨账号不可读（404）", foreign_artifact.status_code == 404, f"HTTP {foreign_artifact.status_code}")
        detail = client_a.get(f"/api/questions/{picked['ref']}")
        study = (detail.json() or {}).get("study") or {}
        record(
            "题目详情聚合证据/附件/复习卡",
            detail.status_code == 200 and study.get("evidence") is not None
            and (study.get("artifact_total") or 0) >= 1,
            f"evidence_total={((study.get('evidence') or {}).get('counts') or {}).get('total')} "
            f"artifacts={study.get('artifact_total')}",
        )

    # ------------------------------------------------------------------
    # 5. 自评与窗口：自评不覆盖核验证据；汇总可读
    # ------------------------------------------------------------------
    self_report = client_a.post(
        "/api/evidence/self-report", json={"ref": picked["ref"], "verdict": "incorrect", "note": "验收自评"}
    )
    record("学生自评独立保存", self_report.status_code == 200, f"HTTP {self_report.status_code}")
    summary = client_a.get("/api/evidence/summary", params={"days": 90})
    summary_body = summary.json() if summary.status_code == 200 else {}
    record(
        "证据汇总可读（含口径说明）",
        summary.status_code == 200 and bool(summary_body.get("caliber")),
        f"有效事件={summary_body.get('effective_count')}",
    )
    stats = client_a.get("/api/review-cards/stats")
    record("复习统计可读", stats.status_code == 200, f"due_now={(stats.json() if stats.status_code == 200 else {}).get('due_now')}")
    report["sample_question_ref"] = picked["ref"]
    report["artifact_id"] = artifact_id


def _write_report(report: dict, path: Path, *, quiet: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if quiet:
        print(f"[{'OK' if report.get('ok') else 'FAIL'}] {path}")


def _cleanup(data_dir: Path) -> None:
    import shutil

    try:
        shutil.rmtree(data_dir, ignore_errors=True)
    except OSError:
        pass


if __name__ == "__main__":
    raise SystemExit(main())
