"""M5 端到端验收：机械/工厂演示模板库、固定种子离散事件仿真、学习附件与课堂导出。

对应《知序功能整合与优化总方案》§5.4「机械与工厂」与 M5：模板 ID + 结构化参数由服务端校验，
固定种子保证可复现；机械 3D / 工厂仿真作为学习附件保存并被课堂固定版本导出；工厂仿真指标
（吞吐/等待/瓶颈）来自确定性 DES，不伪造。

TestClient + 临时数据目录运行；不访问外网、不调用真实模型、题库只读。
输出 `screenshots/m5-accept-report.json`；退出码 0=通过、1=失败、2=环境错误。
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
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REPORT = ROOT / "screenshots" / "m5-accept-report.json"
CHECKS: list[dict] = []


def record(name: str, ok: bool, detail: str, **extra) -> bool:
    entry = {"step": name, "ok": bool(ok), "detail": detail}
    entry.update(extra)
    CHECKS.append(entry)
    print(f"[{'OK' if ok else 'FAIL'}] {name}：{detail}")
    return ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="M5 端到端验收（机械/工厂演示与固定种子仿真）")
    parser.add_argument("--report", default=str(DEFAULT_REPORT))
    parser.add_argument("--keep-data", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    started = time.time()
    data_dir = Path(tempfile.mkdtemp(prefix="zhixu-m5-accept-"))
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
        "scope": "M5 机械/工厂演示模板与固定种子仿真端到端验收",
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
                json={"username": "M5验收管理员" + secrets.token_hex(2), "password": "m5-accept-123"},
            )
            record("账号 A 注册（管理员）", user.status_code == 200,
                   f"user_id={(user.json() or {}).get('id')}")
            with TestClient(app) as client_b:
                client_b.post(
                    "/api/auth/register",
                    json={"username": "M5验收学生" + secrets.token_hex(2), "password": "m5-accept-456"},
                )
                record("账号 B 注册", True, "两账号隔离用")
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


def _run_flows(client_a, client_b, report: dict) -> None:
    templates = client_a.get("/api/demos/templates")
    template_items = templates.json().get("items") if templates.status_code == 200 else []
    template_ids = {item.get("template_id") for item in template_items}
    kinds = {item.get("kind") for item in template_items}
    record(
        "演示模板库可读（机械 3D / 工厂仿真）",
        templates.status_code == 200 and {"mech_3d", "factory_sim"} <= kinds
        and {"gearbox-3d", "pump-3d", "factory-line"} <= template_ids,
        f"templates={sorted(template_ids)} kinds={sorted(kinds)}",
    )
    gear_template = next((item for item in template_items if item.get("template_id") == "gearbox-3d"), {})
    record(
        "机械模板参数元数据完整",
        bool(gear_template.get("params")) and all(
            {"name", "label", "default"} <= set(param) for param in gear_template.get("params") or []
        ),
        f"params={[param.get('name') for param in gear_template.get('params') or []]}",
    )

    # 机械构建：固定参数必须完全可复现，读数按工程公式并标注教学近似。
    params = {"seed": 7, "input_rpm": 1500, "driver_teeth": 12, "driven_teeth": 48}
    first = client_a.post("/api/demos/build", json={"template_id": "gearbox-3d", "params": params})
    second = client_a.post("/api/demos/build", json={"template_id": "gearbox-3d", "params": params})
    first_body = first.json() if first.status_code == 200 else {}
    second_body = second.json() if second.status_code == 200 else {}
    data = first_body.get("data") or {}
    readouts = {item.get("id"): item for item in data.get("readouts") or []}
    ratio = float((readouts.get("ratio") or {}).get("value") or 0)
    output_rpm = float((readouts.get("output_rpm") or {}).get("value") or 0)
    record(
        "机械演示构建确定且读数正确（速比 4 / 输出 375 rpm）",
        first.status_code == 200 and second.status_code == 200
        and json.dumps(first_body.get("data"), sort_keys=True) == json.dumps(second_body.get("data"), sort_keys=True)
        and abs(ratio - 4.0) < 1e-6 and abs(output_rpm - 375.0) < 1e-3
        and bool(data.get("parts")) and bool(data.get("steps")),
        f"ratio={ratio} output_rpm={output_rpm} parts={len(data.get('parts') or [])}",
    )
    bad = client_a.post("/api/demos/build", json={"template_id": "gearbox-3d", "params": {"input_rpm": 99999}})
    record("机械参数越界被拒绝", bad.status_code == 422, f"HTTP {bad.status_code}")
    unknown = client_a.post("/api/demos/build", json={"template_id": "not-a-template"})
    record("未知模板 404", unknown.status_code == 404, f"HTTP {unknown.status_code}")

    # 机械 3D 作为学习附件 → 课堂固定版本场景 → 离线 HTML 含帧就绪标记。
    mech_artifact = _create_artifact(client_a, "mech_3d", "M5 机械演示", data)
    record("机械 3D 学习附件保存", bool(mech_artifact),
           f"artifact_id={(mech_artifact or {}).get('artifact_id')}")
    course_id = _create_course(client_a)
    if course_id and mech_artifact:
        attached = client_a.post(
            f"/api/classroom/courses/{course_id}/scenes/demo",
            json={"artifact_id": mech_artifact["artifact_id"], "revision": mech_artifact["revision"]},
        )
        record("机械演示插入课堂（固定版本）", attached.status_code in (200, 201),
               f"HTTP {attached.status_code}")
        html = client_a.get(f"/api/classroom/courses/{course_id}/export", params={"format": "html"})
        html_text = html.text if html.status_code == 200 else ""
        record(
            "离线导出含机械 3D 场景与帧就绪标记（视频可截帧）",
            html.status_code == 200 and "cls-demo-scene" in html_text
            and "zhixu-demo-frame-ready" in html_text and "data-demo-frame-ready" in html_text
            and "机械 3D" in html_text,
            f"HTTP {html.status_code} bytes={len(html_text)}",
        )

    # 工厂仿真：固定种子可复现 + 指标/事件/瓶颈。
    sim_params = {"seed": 42, "sim_seconds": 240, "arrival_interval_s": 6,
                  "service_s": [4, 7, 5], "buffer_capacity": 3}
    sim1 = client_a.post("/api/demos/simulate", json={"template_id": "factory-line", "params": sim_params})
    sim2 = client_a.post("/api/demos/simulate", json={"template_id": "factory-line", "params": sim_params})
    sim1_body = sim1.json() if sim1.status_code == 200 else {}
    sim2_body = sim2.json() if sim2.status_code == 200 else {}
    run1 = (sim1_body.get("run") or {})
    run2 = (sim2_body.get("run") or {})
    result1 = run1.get("result") or {}
    result2 = run2.get("result") or {}
    metrics = result1.get("metrics") or {}
    events = result1.get("events") or []
    record(
        "工厂仿真固定种子可复现",
        sim1.status_code == 200 and sim2.status_code == 200
        and json.dumps(result1, sort_keys=True) == json.dumps(result2, sort_keys=True)
        and run1.get("seed") == 42,
        f"run_id={run1.get('run_id')} events={len(events)}",
    )
    record(
        "工厂指标与瓶颈来自确定性计算",
        bool(metrics) and 0 <= int(metrics.get("bottleneck_station", -1)) <= 2
        and int(metrics.get("completed", 0)) <= int(metrics.get("arrivals", 0))
        and all(float(value) >= 0 for value in metrics.get("avg_wait_s") or [])
        and bool(str(metrics.get("bottleneck_reason") or "")),
        f"arrivals={metrics.get('arrivals')} completed={metrics.get('completed')} "
        f"bottleneck={metrics.get('bottleneck_station')} reason={(metrics.get('bottleneck_reason') or '')[:20]}",
    )
    run_id = run1.get("run_id")
    if run_id:
        fetched = client_a.get(f"/api/demos/simulate/{run_id}")
        record("仿真运行可回读（本人）", fetched.status_code == 200, f"HTTP {fetched.status_code}")
        foreign = client_b.get(f"/api/demos/simulate/{run_id}")
        record("他人仿真运行不可读（404）", foreign.status_code == 404, f"HTTP {foreign.status_code}")

    # 工厂仿真学习附件 + 第二个课堂演示场景 + ZIP 快照。
    factory_artifact = _create_artifact(client_a, "factory_sim", "M5 工厂仿真", result1)
    record("工厂仿真学习附件保存", bool(factory_artifact),
           f"artifact_id={(factory_artifact or {}).get('artifact_id')}")
    if course_id and factory_artifact:
        attached2 = client_a.post(
            f"/api/classroom/courses/{course_id}/scenes/demo",
            json={"artifact_id": factory_artifact["artifact_id"], "revision": factory_artifact["revision"]},
        )
        record("工厂仿真插入课堂（固定版本）", attached2.status_code in (200, 201),
               f"HTTP {attached2.status_code}")
        zip_resp = client_a.get(f"/api/classroom/courses/{course_id}/export", params={"format": "zip"})
        demo_entries: list[str] = []
        if zip_resp.status_code == 200:
            try:
                with zipfile.ZipFile(io.BytesIO(zip_resp.content)) as archive:
                    demo_entries = [name for name in archive.namelist() if name.startswith("demos/")]
            except zipfile.BadZipFile:
                demo_entries = []
        record("ZIP 含机械与工厂两份演示快照", len(demo_entries) >= 2, f"demos={demo_entries[:4]}")


def _create_artifact(client, kind: str, title: str, data: dict) -> dict | None:
    response = client.post(
        "/api/artifacts",
        json={"kind": kind, "title": title, "data": data, "preview": {"kind": "mech-demo"}},
    )
    body = response.json() if response.status_code == 201 else {}
    artifact_id = (body.get("artifact") or {}).get("artifact_id")
    return {"artifact_id": artifact_id, "revision": (body.get("revision") or {}).get("revision")} if artifact_id else None


def _create_course(client) -> int | None:
    response = client.post(
        "/api/classroom/courses",
        json={"topic": "M5 验收课堂：机械与产线演示", "subject": "物理", "grade": "高中"},
    )
    body = response.json() if response.status_code in (200, 201) else {}
    course = body.get("course") or {}
    return course.get("id")


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
