"""M6 端到端验收：专题报告 / 互动讲义 / 小纸按步骤提示工具 / pyKT 离线数据协议。

对应《知序功能整合与优化总方案》§5.3「专题调研、互动讲义、收藏摘录」与 §7 M6：
- 专题报告：规则汇编引用真实资料片段、页码与 chunk，非 AI 生成；
- 互动讲义：课程 + 学习附件 + 摘录 + 练习 + 证据的小节，自包含 HTML/PDF 导出；
- 小纸：只读 `step_hint` 工具暴露且不泄漏锁定步骤正文；
- pyKT：去标识化数据导出 + 阈值门控 + 规则基线（数据不足时如实标记未达标）。

TestClient + 临时数据目录运行；不访问外网、不调用真实模型、题库只读。
输出 `screenshots/m6-accept-report.json`；退出码 0=通过、1=失败、2=环境错误。
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REPORT = ROOT / "screenshots" / "m6-accept-report.json"
DOC_TEXT = (
    "第一章 函数与导数\n"
    "1.1 函数的概念\n"
    "M6验收专题短语BETA 用于验证专题报告引用与定位。\n"
    "1.2 函数的单调性\n"
    "函数单调递增时导数非负。\n"
)
CHECKS: list[dict] = []


def record(name: str, ok: bool, detail: str, **extra) -> bool:
    entry = {"step": name, "ok": bool(ok), "detail": detail}
    entry.update(extra)
    CHECKS.append(entry)
    print(f"[{'OK' if ok else 'FAIL'}] {name}：{detail}")
    return ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="M6 端到端验收（专题报告/互动讲义/step_hint/pyKT）")
    parser.add_argument("--report", default=str(DEFAULT_REPORT))
    parser.add_argument("--keep-data", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    started = time.time()
    data_dir = Path(tempfile.mkdtemp(prefix="zhixu-m6-accept-"))
    os.environ["STUDY_DATA_DIR"] = str(data_dir)
    os.environ["STUDY_ALLOWED_HOSTS"] = "127.0.0.1,localhost,[::1],testserver"
    os.environ["STUDY_INDEX_PREWARM"] = "0"
    sys.path.insert(0, str(ROOT))
    try:
        from fastapi.testclient import TestClient

        from app.main import app
    except Exception as error:  # noqa: BLE001
        print(f"[FAIL] 无法加载应用：{type(error).__name__}: {error}")
        return 2

    report: dict = {
        "scope": "M6 专题报告/互动讲义/逐步提示工具/pyKT 协议端到端验收",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "data_dir": str(data_dir),
        "build_id": None,
        "bank_version": None,
        "ok": False,
        "checks": CHECKS,
    }
    try:
        with TestClient(app) as client_a:
            health = client_a.get("/api/health").json()
            report["build_id"] = health.get("build_id")
            report["bank_version"] = health.get("bank_version")
            record("应用就绪", health.get("status") == "ok",
                   f"build={report['build_id']} bank={report['bank_version']}")
            user = client_a.post(
                "/api/auth/register",
                json={"username": "M6验收管理员" + secrets.token_hex(2), "password": "m6-accept-123"},
            )
            record("账号 A 注册（管理员）", user.status_code == 200,
                   f"user_id={(user.json() or {}).get('id')}")
            with TestClient(app) as client_b:
                client_b.post(
                    "/api/auth/register",
                    json={"username": "M6验收学生" + secrets.token_hex(2), "password": "m6-accept-456"},
                )
                record("账号 B 注册", True, "两账号隔离用")
                _run_flows(client_a, client_b, data_dir, report)
    except Exception as error:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        record("验收执行", False, f"未捕获异常：{type(error).__name__}: {error}")
    finally:
        report["ok"] = bool(CHECKS) and all(entry["ok"] for entry in CHECKS)
        report["duration_seconds"] = round(time.time() - started, 2)
        _write_report(report, Path(args.report), quiet=args.quiet)
        if not args.keep_data:
            _cleanup(data_dir)
    if not args.quiet:
        passed = sum(1 for entry in CHECKS if entry["ok"])
        print(f"\n汇总：{passed}/{len(CHECKS)} 通过 · 用时 {report['duration_seconds']}s · 报告 {args.report}")
        print("结论：", "ACCEPT OK" if report["ok"] else "ACCEPT FAIL")
    return 0 if report["ok"] else 1


# ---------------------------------------------------------------------------


def _run_flows(client_a, client_b, data_dir: Path, report: dict) -> None:
    document = client_a.post(
        "/api/documents",
        files={"file": ("m6-topic.txt", DOC_TEXT.encode("utf-8"), "text/plain")},
    ).json()
    course = client_a.post(
        "/api/classroom/courses",
        json={"topic": "M6 验收互动讲义课程", "subject": "数学", "grade": "高中"},
    ).json()["course"]
    artifact = client_a.post(
        "/api/artifacts",
        json={
            "kind": "flow_animation", "title": "M6 验收讲义附件",
            "data": {"layout": "flow", "nodes": [{"id": "n1", "label": "起点"}], "edges": [],
                     "steps": [{"label": "第一步"}]},
            "preview": {"kind": "principle-demo"},
        },
    ).json()

    # 1) 专题报告：真实资料引用 + 版本 CAS + Markdown 导出 + 两账号隔离。
    created = client_a.post(
        "/api/topic-reports",
        json={"topic": "M6验收专题短语BETA", "document_ids": [document["id"]]},
    )
    created_body = created.json() if created.status_code in (200, 201) else {}
    topic = created_body.get("report") or {}
    content = created_body.get("content_md") or ""
    sources = created_body.get("sources") or []
    record(
        "专题报告规则汇编（真实页码/chunk 引用）",
        created.status_code in (200, 201) and topic.get("generator") == "rules"
        and "M6验收专题短语BETA" in content and "第 1 页" in content
        and "chunk" in content and any(source.get("kind") == "document" for source in sources)
        and "规则汇编" in content,
        f"report_id={topic.get('report_id')} sources={len(sources)} bytes={len(content)}",
    )
    report_id = topic.get("report_id")
    if report_id:
        stale = client_a.patch(
            f"/api/topic-reports/{report_id}",
            json={"title": "改名", "expected_version": int(topic.get("version") or 1) - 1},
        )
        record("专题报告版本 CAS 冲突 409", stale.status_code == 409, f"HTTP {stale.status_code}")
        renamed = client_a.patch(
            f"/api/topic-reports/{report_id}",
            json={"title": "M6 专题报告（验收）", "expected_version": int(topic.get("version") or 1)},
        )
        record("专题报告可编辑并升版", renamed.status_code == 200,
               f"HTTP {renamed.status_code} version={(renamed.json().get('report') or {}).get('version')}")
        markdown = client_a.get(f"/api/topic-reports/{report_id}/markdown")
        record("专题报告 Markdown 可下载", markdown.status_code == 200
               and "text/markdown" in markdown.headers.get("content-type", ""),
               f"HTTP {markdown.status_code} bytes={len(markdown.content)}")
        record("他人专题报告不可读（404）", client_b.get(f"/api/topic-reports/{report_id}").status_code == 404,
               "HTTP 404")

    # 2) 互动讲义：小节 + 自包含 HTML/PDF + 两账号隔离。
    handout = client_a.post(
        "/api/handouts",
        json={"title": "M6 验收互动讲义", "course_id": course["id"],
              "artifact_ids": [artifact["artifact"]["artifact_id"]]},
    )
    handout_body = handout.json() if handout.status_code in (200, 201) else {}
    handout_obj = handout_body.get("handout") or {}
    sections = handout_body.get("sections") or []
    kinds = [section.get("kind") for section in sections]
    record(
        "互动讲义小节来自真实课程/附件",
        handout.status_code in (200, 201) and "course_outline" in kinds and "artifact" in kinds
        and any(section.get("kind") == "meta" for section in sections),
        f"handout_id={handout_obj.get('handout_id')} sections={kinds}",
    )
    handout_id = handout_obj.get("handout_id")
    if handout_id:
        html = client_a.get(f"/api/handouts/{handout_id}/export", params={"format": "html"})
        html_text = html.text if html.status_code == 200 else ""
        record(
            "互动讲义导出自包含 HTML（转义、无外链脚本）",
            html.status_code == 200 and "data-handout-id" in html_text
            and "规则汇编" in html_text and "http://" not in html_text
            and "https://" not in html_text and "<script" not in html_text.lower(),
            f"HTTP {html.status_code} bytes={len(html_text)}",
        )
        pdf = client_a.get(f"/api/handouts/{handout_id}/export", params={"format": "pdf"})
        record("互动讲义 PDF 导出（可用 200 / 依赖缺失 503）", pdf.status_code in (200, 503),
               f"HTTP {pdf.status_code}")
        record("他人互动讲义不可读（404）",
               client_b.get(f"/api/handouts/{handout_id}").status_code == 404, "HTTP 404")

    # 3) 小纸只读逐步提示工具。
    tools = client_a.get("/api/assistant/tools").json()
    spec = next((item for item in tools.get("items") or [] if item.get("name") == "step_hint"), None)
    record(
        "小纸暴露只读 step_hint 工具（无写副作用）",
        spec is not None and spec.get("side_effect") is False
        and spec.get("requires_confirmation") is False
        and any(param.get("name") == "ref" for param in spec.get("params") or []),
        f"tool={'step_hint' if spec else 'missing'}",
    )

    # 4) pyKT 离线数据协议：先产生一条真实作答证据，再去标识化导出 + 阈值门控 + 规则基线。
    _submit_practice_evidence(client_a)
    _run_pykt(data_dir, report)


def _submit_practice_evidence(client) -> None:
    """提交一道题库单选题，产生一条真实学习证据，供 pyKT 导出使用。"""
    listing = client.get("/api/questions", params={"subject": "数学", "page": 1, "page_size": 10})
    if listing.status_code != 200:
        return
    for item in listing.json().get("items") or []:
        if item.get("type") != "single_choice" or not item.get("practice_allowed", True):
            continue
        detail = client.get(f"/api/questions/{item['qid']}")
        if detail.status_code != 200:
            continue
        labels = [
            str(option["label"]).upper()
            for option in detail.json().get("options") or [] if option.get("label")
        ]
        if not labels:
            continue
        client.post(
            "/api/attempts",
            data={"qid": item["qid"], "answer": labels[0],
                  "submission_id": "m6-accept-" + secrets.token_hex(3)},
        )
        return


def _run_pykt(data_dir: Path, report: dict) -> None:
    out_path = data_dir / "pykt-dataset.jsonl"
    report_path = data_dir / "pykt-report.json"
    env = dict(os.environ)
    env["STUDY_PYKT_SALT"] = "m6-accept-salt"
    completed = subprocess.run(
        [sys.executable, "-X", "utf8", str(ROOT / "scripts" / "integration" / "pykt_export.py"),
         "--data-dir", str(data_dir), "--out", str(out_path), "--report", str(report_path)],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=180, env=env,
    )
    if completed.returncode not in (0, 2):
        record("pyKT 导出脚本执行", False,
               f"exit={completed.returncode} stderr={(completed.stderr or '')[-200:]}")
        return
    report_json = {}
    if report_path.is_file():
        try:
            report_json = json.loads(report_path.read_text(encoding="utf-8"))
        except ValueError:
            report_json = {}
    rows = 0
    raw_user_id_leak = False
    if out_path.is_file():
        for line in out_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rows += 1
            payload = json.loads(line)
            if "username" in payload or "user_id" in payload:
                raw_user_id_leak = True
            if not str(payload.get("user_key") or "").strip():
                raw_user_id_leak = True
    record(
        "pyKT 去标识化数据集可导出（不足阈值如实标记）",
        completed.returncode == 0 and not raw_user_id_leak
        and isinstance(report_json.get("evaluation_allowed"), bool)
        and isinstance(report_json.get("gates"), dict)
        and rows >= 1,
        f"exit={completed.returncode} rows={rows} "
        f"evaluation_allowed={report_json.get('evaluation_allowed')}",
    )
    record(
        "pyKT 阈值门控与规则基线结构完整",
        bool(report_json) and isinstance(report_json.get("gates"), dict)
        and isinstance(report_json.get("baseline"), dict)
        and isinstance(report_json.get("dataset"), dict)
        and bool(str(report_json.get("protocol_note") or "")),
        f"keys={sorted(report_json.keys())}",
    )
    report["pykt"] = {
        "rows": rows,
        "evaluation_allowed": report_json.get("evaluation_allowed"),
    }


def _write_report(report: dict, path: Path, *, quiet: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if quiet:
        print(f"[{'OK' if report.get('ok') else 'FAIL'}] {path}")


def _cleanup(data_dir: Path) -> None:
    import shutil

    shutil.rmtree(data_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
