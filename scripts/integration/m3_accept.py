"""M3 端到端验收（第七轮）：逐步提示 / 资料原文定位与摘录 / 掌握路径 / 学习附件图形工具。

对应《知序功能整合与优化总方案》§5.2/§5.3/§5.4 的 M3 首批交付：
「小纸逐步提示状态机、资料原文定位（版本+页/chunk+摘录）、掌握路径（证据驱动完成条件）、
函数图/白板作为版本化学习附件跨页复用」。

使用 FastAPI TestClient 在临时数据目录内运行；不访问外网、不读写生产数据、不调用真实模型；
题库维持只读 releases/CURRENT。输出 `screenshots/m3-accept-report.json`。
退出码：0=全部通过；1=存在失败项；2=环境错误。

用法（仓库根执行）::

    python -X utf8 scripts/integration/m3_accept.py
    python -X utf8 scripts/integration/m3_accept.py --report .omx/tmp/m3.json --keep-data
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REPORT = ROOT / "screenshots" / "m3-accept-report.json"
DOC_TEXT = (
    "第一章 函数与导数\n"
    "1.1 函数的概念\n"
    "知序验收定位短语ABC123 用于验证资料原文定位与摘录功能。\n"
    "1.2 函数的单调性\n"
    "若函数在区间上单调递增，则其导数在该区间上非负。\n"
)
CHECKS: list[dict] = []


def record(name: str, ok: bool, detail: str, **extra) -> bool:
    entry = {"step": name, "ok": bool(ok), "detail": detail}
    entry.update(extra)
    CHECKS.append(entry)
    print(f"[{'OK' if ok else 'FAIL'}] {name}：{detail}")
    return ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="M3 端到端验收（临时数据目录 + TestClient）")
    parser.add_argument("--report", default=str(DEFAULT_REPORT))
    parser.add_argument("--keep-data", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    started = time.time()
    data_dir = Path(tempfile.mkdtemp(prefix="zhixu-m3-accept-"))
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
        "scope": "M3 逐步提示/资料定位/掌握路径/学习附件端到端验收",
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
            user_a = client_a.post(
                "/api/auth/register",
                json={"username": "M3验收管理员" + secrets.token_hex(2), "password": "m3-accept-123"},
            )
            record("账号 A 注册（管理员）", user_a.status_code == 200,
                   f"user_id={(user_a.json() or {}).get('id')}")
            with TestClient(app) as client_b:
                user_b = client_b.post(
                    "/api/auth/register",
                    json={"username": "M3验收学生" + secrets.token_hex(2), "password": "m3-accept-456"},
                )
                record("账号 B 注册", user_b.status_code == 200,
                       f"user_id={(user_b.json() or {}).get('id')}")
                _run_flows(client_a, client_b, report)
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
# 逐步提示
# ---------------------------------------------------------------------------


def _pick_question_with_solution(client) -> dict | None:
    for page in (1, 2):
        response = client.get("/api/questions", params={"subject": "数学", "page": page, "page_size": 30})
        if response.status_code != 200:
            continue
        for item in response.json().get("items") or []:
            if not item.get("has_solution"):
                continue
            detail = client.get(f"/api/questions/{item['qid']}")
            if detail.status_code != 200:
                continue
            body = detail.json()
            solution = str(body.get("solution") or body.get("answer_text") or "")
            # 未作答时解析不下发；改为读只读题库原文？用 knowledge/solution 可能在解锁后才有。
            if solution.strip():
                return {"ref": body.get("ref") or item["qid"], "solution": solution}
    return None


def _run_guided_hint(client_a, client_b, report: dict) -> None:
    picked = _pick_question_with_solution(client_a)
    if not picked:
        # 未作答时解析不下发：先用一次作答解锁，再读解析（只用于验收脚本内的题目引用）。
        picked = _unlock_question(client_a)
    if not picked:
        record("逐步提示取样", False, "题库中未取到带解析的数学题")
        return
    report["guided_question_ref"] = picked["ref"]
    created = client_a.post("/api/guided-hints", json={"ref": picked["ref"]})
    body = created.json() if created.status_code in (200, 201) else {}
    steps = body.get("steps") or []
    hint = body.get("hint") or {}
    locked_bodies_absent = all(
        ("locked" in step and step.get("body") in (None, "")) or ("locked" not in step)
        for step in steps
    )
    locked_titles = [step.get("title") for step in steps if step.get("locked")]
    record(
        "逐步提示会话创建（首步解锁，其余仅标题）",
        created.status_code in (200, 201) and len(steps) >= 2 and locked_bodies_absent and bool(locked_titles),
        f"steps={len(steps)} locked={len(locked_titles)} hint_id={hint.get('hint_id')}",
    )
    hint_id = hint.get("hint_id")
    if not hint_id:
        return
    view = client_a.get(f"/api/guided-hints/{hint_id}").json()
    serialized = json.dumps(view, ensure_ascii=False)
    leak = any(
        str(step.get("body") or "")[:40] in serialized
        for step in steps if step.get("locked") and step.get("body")
    )
    solution_probe = str(picked.get("solution") or "")[:40]
    record(
        "锁定步骤正文不出现在响应中",
        not leak and (not solution_probe or solution_probe not in serialized),
        f"solution_probe={'是' if solution_probe else '无'} leak={leak}",
    )
    blocked = client_a.post(f"/api/guided-hints/{hint_id}/advance", json={})
    blocked_body = blocked.json() if blocked.status_code == 200 else {}
    record(
        "未提交尝试时不能解锁下一步",
        blocked.status_code == 200 and blocked_body.get("requires_attempt") is True
        and blocked_body.get("unlocked") in (None, {}),
        f"requires_attempt={blocked_body.get('requires_attempt')}",
    )
    attempt = client_a.post(
        f"/api/guided-hints/{hint_id}/attempt",
        json={"step_position": 0, "text": "先把函数表达式与所求量列出，再判断用哪个公式。"},
    )
    record("提交第一步尝试", attempt.status_code == 200, f"HTTP {attempt.status_code}")
    advanced = client_a.post(f"/api/guided-hints/{hint_id}/advance", json={})
    advanced_body = advanced.json() if advanced.status_code == 200 else {}
    unlocked = advanced_body.get("unlocked") or {}
    record(
        "提交尝试后解锁下一步",
        advanced.status_code == 200 and bool(unlocked.get("body")),
        f"unlocked_position={unlocked.get('position')} body_len={len(str(unlocked.get('body') or ''))}",
    )
    # 逐级解锁到完成（最多 10 轮）；每次需要先 attempt 当前位置。
    for _ in range(10):
        state = client_a.get(f"/api/guided-hints/{hint_id}").json()
        unlocked_steps = [step for step in state.get("steps") or [] if not step.get("locked")]
        if not unlocked_steps:
            break
        current = unlocked_steps[-1]
        if state.get("hint", {}).get("status") == "completed":
            break
        if not current.get("attempted"):
            client_a.post(
                f"/api/guided-hints/{hint_id}/attempt",
                json={"step_position": current["position"], "text": "这一步我的尝试：" + current["title"]},
            )
        progress = client_a.post(f"/api/guided-hints/{hint_id}/advance", json={})
        if progress.status_code != 200:
            break
    final = client_a.get(f"/api/guided-hints/{hint_id}").json()
    final_steps = final.get("steps") or []
    full_steps = [step for step in final_steps if not step.get("locked")]
    record(
        "逐级尝试后解锁完整解析",
        (final.get("hint") or {}).get("status") == "completed"
        and any(step.get("kind") == "solution" for step in full_steps)
        and all(step.get("body") for step in full_steps),
        f"status={(final.get('hint') or {}).get('status')} unlocked={len(full_steps)}/{len(final_steps)}",
    )
    foreign = client_b.get(f"/api/guided-hints/{hint_id}")
    record("他人提示会话不可读（404）", foreign.status_code == 404, f"HTTP {foreign.status_code}")


def _unlock_question(client) -> dict | None:
    """作答一次解锁题目解析，再取带解析的数学题（只读题库）。"""
    for page in (1, 2):
        response = client.get("/api/questions", params={"subject": "数学", "page": page, "page_size": 30})
        if response.status_code != 200:
            continue
        for item in response.json().get("items") or []:
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
            client.post("/api/attempts", data={"qid": item["qid"], "answer": labels[0]})
            fresh = client.get(f"/api/questions/{item['qid']}")
            if fresh.status_code != 200:
                continue
            body = fresh.json()
            solution = str(body.get("solution") or body.get("answer_text") or "")
            if solution.strip():
                return {"ref": body.get("ref") or item["qid"], "solution": solution}
    return None


# ---------------------------------------------------------------------------
# 资料原文定位与摘录
# ---------------------------------------------------------------------------


def _run_document_location(client_a, client_b, report: dict) -> None:
    upload = client_a.post(
        "/api/documents",
        files={"file": ("m3-accept.txt", DOC_TEXT.encode("utf-8"), "text/plain")},
    )
    body = upload.json() if upload.status_code == 200 else {}
    document_id = body.get("id")
    record("上传验收资料（TXT）", upload.status_code == 200 and bool(document_id),
           f"document_id={document_id} chunks={body.get('chunks')}")
    if not document_id:
        return
    report["document_id"] = document_id
    located = client_a.post(f"/api/documents/{document_id}/locate", json={"quote": "验收定位短语ABC123"})
    located_body = located.json() if located.status_code == 200 else {}
    record(
        "按摘录定位原文（页/chunk/片段）",
        located.status_code == 200 and bool(located_body.get("snippet"))
        and "验收定位短语ABC123" in str(located_body.get("snippet"))
        and bool(located_body.get("chunk_id")),
        f"page={located_body.get('page')} chunk={located_body.get('chunk_id')} "
        f"doc_version={located_body.get('doc_version')}",
    )
    missing = client_a.post(f"/api/documents/{document_id}/locate", json={"quote": "不存在的摘录XYZ"})
    record("找不到的摘录返回 422", missing.status_code == 422, f"HTTP {missing.status_code}")
    annotation = client_a.post(
        "/api/doc-annotations",
        json={"document_id": document_id, "quote": "验收定位短语ABC123", "note": "验收摘录"},
    )
    annotation_body = annotation.json() if annotation.status_code in (200, 201) else {}
    annotation_obj = annotation_body.get("annotation") or {}
    record(
        "保存摘录（版本快照）",
        annotation.status_code in (200, 201) and bool(annotation_obj.get("annotation_id"))
        and bool(annotation_obj.get("doc_version")),
        f"annotation_id={annotation_obj.get('annotation_id')}",
    )
    listing = client_a.get("/api/doc-annotations", params={"document_id": document_id})
    items = listing.json().get("items") if listing.status_code == 200 else []
    record(
        "摘录列表可读且有效",
        listing.status_code == 200 and bool(items) and items[0].get("valid") is True,
        f"items={len(items or [])} valid={(items or [{}])[0].get('valid')}",
    )
    if annotation_obj.get("annotation_id"):
        refresh = client_a.post(f"/api/doc-annotations/{annotation_obj['annotation_id']}/refresh")
        record("摘录可重新定位（同版本）", refresh.status_code == 200, f"HTTP {refresh.status_code}")
    foreign = client_b.post(f"/api/documents/{document_id}/locate", json={"quote": "验收定位短语ABC123"})
    record("他人资料不可定位（404）", foreign.status_code == 404, f"HTTP {foreign.status_code}")


# ---------------------------------------------------------------------------
# 掌握路径
# ---------------------------------------------------------------------------


def _run_mastery_path(client_a, client_b, report: dict) -> None:
    points = client_a.get("/api/knowledge-points", params={"subject": "数学"})
    items = points.json().get("items") if points.status_code == 200 else []
    kp_ids = [int(item["id"]) for item in (items or []) if item.get("id")][:2]
    if not kp_ids:
        record("掌握路径取样", False, "应用库没有数学知识点")
        return
    report["mastery_kp_ids"] = kp_ids
    created = client_a.post("/api/mastery-paths", json={"kp_ids": kp_ids[:1], "source_kind": "insight"})
    body = created.json() if created.status_code in (200, 201) else {}
    path = body.get("path") or {}
    nodes = body.get("nodes") or []
    record(
        "生成掌握路径（逐知识点 2 节点）",
        created.status_code in (200, 201) and bool(path.get("path_id")) and len(nodes) == 2,
        f"path_id={path.get('path_id')} nodes={len(nodes)} sample_status={path.get('sample_status')}",
    )
    path_id = path.get("path_id")
    if not path_id:
        return
    detail = client_a.get(f"/api/mastery-paths/{path_id}")
    detail_body = detail.json() if detail.status_code == 200 else {}
    record("掌握路径详情可读（含进度/样本状态）",
           detail.status_code == 200 and "progress" in detail_body,
           f"progress={detail_body.get('progress')}")
    concept = next((node for node in nodes if node.get("kind") == "concept"), None)
    practice = next((node for node in nodes if node.get("kind") == "practice"), None)
    if concept:
        short = client_a.post(
            f"/api/mastery-paths/{path_id}/nodes/{concept['node_id']}/restate",
            json={"text": "太短"},
        )
        record("概念复述过短被拒绝", short.status_code == 422, f"HTTP {short.status_code}")
        restated = client_a.post(
            f"/api/mastery-paths/{path_id}/nodes/{concept['node_id']}/restate",
            json={"text": "函数的单调性指在区间上自变量增大时函数值同向变化，可用导数符号判断。"},
        )
        record(
            "概念复述完成节点（自评独立标记）",
            restated.status_code == 200
            and (restated.json().get("node") or {}).get("status") == "done",
            f"HTTP {restated.status_code}",
        )
    if practice:
        refreshed = client_a.post(f"/api/mastery-paths/{path_id}/nodes/{practice['node_id']}/refresh")
        refreshed_node = (refreshed.json() if refreshed.status_code == 200 else {}).get("node") or {}
        record(
            "变式练习节点按证据核验（无独立答对证据时保持待完成）",
            refreshed.status_code == 200 and refreshed_node.get("status") in ("pending", "in_progress"),
            f"status={refreshed_node.get('status')} evidence={(refreshed_node.get('evidence') or {}).get('note', '')[:30]}",
        )
    foreign = client_b.get(f"/api/mastery-paths/{path_id}")
    record("他人掌握路径不可读（404）", foreign.status_code == 404, f"HTTP {foreign.status_code}")
    archived = client_a.post(f"/api/mastery-paths/{path_id}/archive")
    record("掌握路径可归档", archived.status_code == 200, f"HTTP {archived.status_code}")


# ---------------------------------------------------------------------------
# 学习附件：函数图 / 白板（M3 图形工具的数据契约）
# ---------------------------------------------------------------------------


def _run_artifacts(client_a, picked_ref: str | None, report: dict) -> None:
    graph_data = {
        "version": 1,
        "source": "mafs-compatible",
        "expressions": [{"id": "f1", "latex": "a*x^2+b*x+c", "color": "#2b7de9", "visible": True}],
        "params": [
            {"name": "a", "value": 1, "min": -5, "max": 5, "step": 0.1},
            {"name": "b", "value": 2, "min": -10, "max": 10, "step": 0.1},
            {"name": "c", "value": 0, "min": -10, "max": 10, "step": 0.1},
        ],
        "xRange": [-6, 6],
        "yRange": [-8, 8],
        "points": [],
        "labels": [],
    }
    created = client_a.post(
        "/api/artifacts",
        json={
            "kind": "function_graph", "title": "M3 验收函数图", "data": graph_data,
            "preview": {"kind": "function-graph", "expressions": [{"latex": "a*x^2+b*x+c"}]},
            "source_ref": picked_ref, "source_kind": "question",
            "links": ([{"target_kind": "question", "target_key": picked_ref}] if picked_ref else []),
        },
    )
    body = created.json() if created.status_code == 201 else {}
    artifact_id = (body.get("artifact") or {}).get("artifact_id")
    record("函数图作为学习附件保存", created.status_code == 201 and bool(artifact_id),
           f"artifact_id={artifact_id}")
    board_data = {
        "version": 1,
        "source": "excalidraw-compatible",
        "appState": {"viewBackgroundColor": "#ffffff"},
        "elements": [
            {"id": "e1", "type": "rect", "x": 10, "y": 20, "width": 120, "height": 60,
             "strokeColor": "#1e1e1e", "backgroundColor": "transparent", "strokeWidth": 2, "opacity": 100},
            {"id": "e2", "type": "formula", "x": 30, "y": 100, "latex": "x^2+2x=0", "fontSize": 20,
             "strokeColor": "#1e1e1e"},
        ],
    }
    board = client_a.post(
        "/api/artifacts",
        json={
            "kind": "whiteboard", "title": "M3 验收白板", "data": board_data,
            "preview": {"kind": "whiteboard-image", "png": ""},
            "source_ref": picked_ref, "source_kind": "question",
            "links": ([{"target_kind": "question", "target_key": picked_ref}] if picked_ref else []),
        },
    )
    board_body = board.json() if board.status_code == 201 else {}
    board_id = (board_body.get("artifact") or {}).get("artifact_id")
    record("白板场景作为学习附件保存", board.status_code == 201 and bool(board_id),
           f"artifact_id={board_id}")
    if picked_ref:
        by_target = client_a.get(
            "/api/artifacts/by-target", params={"target_kind": "question", "target_key": picked_ref}
        )
        kinds = {
            (item.get("artifact") or {}).get("kind")
            for item in (by_target.json().get("items") or [])
        } if by_target.status_code == 200 else set()
        record("题目可按目标读取函数图与白板",
               {"function_graph", "whiteboard"} <= kinds,
               f"kinds={sorted(kinds)}")
    report["artifact_ids"] = [artifact_id, board_id]


# ---------------------------------------------------------------------------


def _run_flows(client_a, client_b, report: dict) -> None:
    _run_guided_hint(client_a, client_b, report)
    _run_document_location(client_a, client_b, report)
    _run_mastery_path(client_a, client_b, report)
    _run_artifacts(client_a, report.get("guided_question_ref"), report)


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
