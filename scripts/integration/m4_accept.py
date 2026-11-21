"""M4 端到端验收：课堂版本化演示场景 + 固定版本导出 + 学习附件跨页读取。

对应《知序功能整合与优化总方案》§4「学习附件」、§5.4「导出」与并行方案 M4：
课堂插入已保存的函数图/流程演示为**固定版本**场景，播放器按快照渲染；PPTX/HTML/ZIP/PDF
导出使用该固定版本（海报 + 说明 + 数据 JSON）；笔记可通过 by-target 读取同一附件。

TestClient + 临时数据目录运行；不访问外网、不调用真实模型、题库只读。
输出 `screenshots/m4-accept-report.json`；退出码 0=全部通过、1=存在失败、2=环境错误。
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
DEFAULT_REPORT = ROOT / "screenshots" / "m4-accept-report.json"
CHECKS: list[dict] = []

FLOW_DATA = {
    "layout": "loop",
    "style": "sketch",
    "title": "M4 验收复习闭环",
    "nodes": [
        {"id": "n1", "label": "答错", "kind": "start"},
        {"id": "n2", "label": "归因", "kind": "process"},
        {"id": "n3", "label": "变式练习", "kind": "process"},
        {"id": "n4", "label": "独立复述", "kind": "process"},
        {"id": "n5", "label": "到期复习", "kind": "end"},
    ],
    "edges": [
        {"from": "n1", "to": "n2"},
        {"from": "n2", "to": "n3"},
        {"from": "n3", "to": "n4"},
        {"from": "n4", "to": "n5"},
        {"from": "n5", "to": "n3", "kind": "feedback"},
    ],
    "steps": [{"label": "答错后先归因"}, {"label": "做同类变式"}, {"label": "独立复述"}, {"label": "按到期复习"}],
}


def record(name: str, ok: bool, detail: str, **extra) -> bool:
    entry = {"step": name, "ok": bool(ok), "detail": detail}
    entry.update(extra)
    CHECKS.append(entry)
    print(f"[{'OK' if ok else 'FAIL'}] {name}：{detail}")
    return ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="M4 端到端验收（课堂版本化演示场景与固定版本导出）")
    parser.add_argument("--report", default=str(DEFAULT_REPORT))
    parser.add_argument("--keep-data", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    started = time.time()
    data_dir = Path(tempfile.mkdtemp(prefix="zhixu-m4-accept-"))
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
        "scope": "M4 课堂版本化演示场景与固定版本导出端到端验收",
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
                json={"username": "M4验收管理员" + secrets.token_hex(2), "password": "m4-accept-123"},
            )
            record("账号 A 注册（管理员）", user.status_code == 200,
                   f"user_id={(user.json() or {}).get('id')}")
            with TestClient(app) as client_b:
                other = client_b.post(
                    "/api/auth/register",
                    json={"username": "M4验收学生" + secrets.token_hex(2), "password": "m4-accept-456"},
                )
                record("账号 B 注册", other.status_code == 200,
                       f"user_id={(other.json() or {}).get('id')}")
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
# 演示附件 → 课堂固定版本场景 → 导出 → 跨页读取
# ---------------------------------------------------------------------------


def _create_artifact(client) -> dict | None:
    created = client.post(
        "/api/artifacts",
        json={
            "kind": "flow_animation",
            "title": "M4 验收流程演示",
            "data": FLOW_DATA,
            "preview": {"kind": "principle-demo", "preset_id": "m4-accept"},
        },
    )
    body = created.json() if created.status_code == 201 else {}
    artifact_id = (body.get("artifact") or {}).get("artifact_id")
    revision = (body.get("revision") or {}).get("revision")
    return {"artifact_id": artifact_id, "revision": revision} if artifact_id else None


def _create_course(client) -> dict | None:
    created = client.post(
        "/api/classroom/courses",
        json={"topic": "M4 验收课堂：复习闭环原理演示", "subject": "物理", "grade": "高中"},
    )
    body = created.json() if created.status_code in (200, 201) else {}
    course = body.get("course") or {}
    return course if course.get("id") else None


def _run_flows(client_a, client_b, report: dict) -> None:
    artifact = _create_artifact(client_a)
    record("创建流程演示学习附件", bool(artifact),
           f"artifact_id={(artifact or {}).get('artifact_id')} revision={(artifact or {}).get('revision')}")
    if not artifact:
        return
    report["artifact"] = artifact
    course = _create_course(client_a)
    record("创建验收课堂", bool(course), f"course_id={(course or {}).get('id')}")
    if not course:
        return
    course_id = course["id"]
    report["course_id"] = course_id

    meta = client_a.get("/api/classroom/meta").json()
    record(
        "课堂 meta 声明手工演示场景类型（既有生成类型不变）",
        meta.get("manual_scene_types") == ["demo"]
        and set(meta.get("scene_types") or []) == {"slide", "quiz", "interactive", "pbl"},
        f"scene_types={meta.get('scene_types')} manual={meta.get('manual_scene_types')}",
    )

    attached = client_a.post(
        f"/api/classroom/courses/{course_id}/scenes/demo",
        json={"artifact_id": artifact["artifact_id"], "source_note": "M4 验收固定版本演示"},
    )
    attach_body = attached.json() if attached.status_code in (200, 201) else {}
    scene = attach_body.get("scene") or {}
    content = scene.get("content") or {}
    record(
        "课堂插入固定版本演示场景",
        attached.status_code in (200, 201)
        and scene.get("scene_type") == "demo"
        and (content.get("artifact") or {}).get("artifact_id") == artifact["artifact_id"]
        and (content.get("artifact") or {}).get("revision") == artifact["revision"]
        and content.get("fixed") is True,
        f"scene_id={scene.get('id')} revision={(content.get('artifact') or {}).get('revision')}",
    )
    scene_id = scene.get("id")
    if not scene_id:
        return
    report["scene"] = {"scene_id": scene_id, "course_id": course_id}

    # 源附件升版后，课堂场景仍保留固定版本（引用快照不追随最新）。
    patched = client_a.patch(
        f"/api/artifacts/{artifact['artifact_id']}",
        json={
            "data": {**FLOW_DATA, "title": "M4 验收流程演示 v2"},
            "expected_revision": artifact["revision"],
        },
    )
    record("源附件升版（v2）", patched.status_code == 200, f"HTTP {patched.status_code}")
    course_after = client_a.get(f"/api/classroom/courses/{course_id}")
    scenes = (course_after.json() or {}).get("scenes") or []
    demo = next((item for item in scenes if item.get("scene_type") == "demo"), {})
    record(
        "课堂保留固定版本（不随源附件更新）",
        (demo.get("content") or {}).get("artifact", {}).get("revision") == artifact["revision"],
        f"课堂版本={(demo.get('content') or {}).get('artifact', {}).get('revision')} 源最新=2",
    )

    # 导出：PPTX / HTML / ZIP / PDF 使用固定版本。
    pptx = client_a.get(f"/api/classroom/courses/{course_id}/export", params={"format": "pptx"})
    record("导出 PPTX", pptx.status_code == 200 and pptx.content[:2] == b"PK",
           f"HTTP {pptx.status_code} bytes={len(pptx.content)}")
    pptx_text = ""
    if pptx.status_code == 200:
        try:
            from pptx import Presentation

            presentation = Presentation(io.BytesIO(pptx.content))
            texts = []
            for slide in presentation.slides:
                for shape in slide.shapes:
                    if getattr(shape, "has_text_frame", False):
                        texts.append(shape.text_frame.text)
            pptx_text = "\n".join(texts)
        except Exception:  # noqa: BLE001 - 解析失败按无文本处理，由断言暴露
            pptx_text = ""
    record(
        "PPTX 含固定版本与演示标题",
        "固定版本" in pptx_text and "M4 验收流程演示" in pptx_text,
        f"文本片段={'固定版本' in pptx_text}",
    )
    html = client_a.get(f"/api/classroom/courses/{course_id}/export", params={"format": "html"})
    html_text = html.text if html.status_code == 200 else ""
    record(
        "离线 HTML 含固定版本演示片段与快照数据",
        html.status_code == 200 and "data-fixed-revision" in html_text
        and artifact["artifact_id"] in html_text and "cls-demo-scene" in html_text,
        f"HTTP {html.status_code} bytes={len(html_text)}",
    )
    zip_resp = client_a.get(f"/api/classroom/courses/{course_id}/export", params={"format": "zip"})
    zip_names: list[str] = []
    if zip_resp.status_code == 200:
        import zipfile

        try:
            with zipfile.ZipFile(io.BytesIO(zip_resp.content)) as archive:
                zip_names = archive.namelist()
        except zipfile.BadZipFile:
            zip_names = []
    record(
        "ZIP 含演示快照 JSON",
        any(name.startswith("demos/") and name.endswith(".json") for name in zip_names),
        f"entries={len(zip_names)} demos={[n for n in zip_names if n.startswith('demos/')][:3]}",
    )
    # 导出 ZIP 可完整导回：演示场景按固定版本快照还原。
    if zip_resp.status_code == 200:
        imported = client_a.post(
            "/api/classroom/courses/import",
            files={"file": ("m4-export.zip", zip_resp.content, "application/zip")},
        )
        imported_body = imported.json() if imported.status_code in (200, 201) else {}
        imported_course = imported_body.get("course") or {}
        record("ZIP 导回课堂（含演示场景）", imported.status_code in (200, 201) and bool(imported_course.get("id")),
               f"HTTP {imported.status_code} course_id={imported_course.get('id')}")
        if imported_course.get("id"):
            imported_detail = client_a.get(f"/api/classroom/courses/{imported_course['id']}").json()
            imported_demo = next(
                (scene for scene in imported_detail.get("scenes") or []
                 if scene.get("scene_type") == "demo"), {},
            )
            record(
                "导回后的演示仍为原固定版本",
                (imported_demo.get("content") or {}).get("artifact", {}).get("revision")
                == artifact["revision"],
                f"导入版本={(imported_demo.get('content') or {}).get('artifact', {}).get('revision')}",
            )
    pdf = client_a.get(f"/api/classroom/courses/{course_id}/export", params={"format": "pdf"})
    record("PDF 导出请求可用（或按现有降级中文提示）",
           pdf.status_code in (200, 503),
           f"HTTP {pdf.status_code}")

    # 附件跨页读取：笔记 target 可按 by-target 读到。
    linked = client_a.post(
        "/api/artifacts",
        json={
            "kind": "function_graph", "title": "M4 笔记函数图",
            "data": {"version": 1, "source": "mafs-compatible",
                     "expressions": [{"id": "f1", "latex": "x^2", "color": "#2b7de9", "visible": True}],
                     "params": [], "xRange": [-5, 5], "yRange": [-5, 5], "points": [], "labels": []},
            "links": [{"target_kind": "note", "target_key": "9001"}],
        },
    )
    by_target = client_a.get(
        "/api/artifacts/by-target", params={"target_kind": "note", "target_key": "9001"}
    )
    note_items = by_target.json().get("items") if by_target.status_code == 200 else []
    record(
        "笔记可按 by-target 读取学习附件（课堂/笔记同源）",
        linked.status_code == 201 and bool(note_items),
        f"items={len(note_items or [])}",
    )

    # 两账号隔离 + 场景删除。
    foreign = client_b.post(
        f"/api/classroom/courses/{course_id}/scenes/demo",
        json={"artifact_id": artifact["artifact_id"]},
    )
    record("他人课程不可插入演示（404）", foreign.status_code == 404, f"HTTP {foreign.status_code}")
    removed = client_a.delete(f"/api/classroom/courses/{course_id}/scenes/{scene_id}")
    record("删除演示场景", removed.status_code == 200,
           f"HTTP {removed.status_code} course={(removed.json() or {}).get('course')}")
    final = client_a.get(f"/api/classroom/courses/{course_id}").json()
    record(
        "删除后场景列表清空且顺序连续",
        [scene.get("scene_type") for scene in final.get("scenes") or []] == [],
        f"scenes={len(final.get('scenes') or [])}",
    )


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
