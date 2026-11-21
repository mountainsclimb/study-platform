"""M5 集成测试：机械/工厂演示模板库、固定种子仿真与课堂固定版本导出。

真实应用（client/second_client）：模板元数据 → 确定性构建（工程读数）→ 工厂 DES 固定种子可复现 →
mech_3d/factory_sim 学习附件 → 课堂 demo 场景 → 离线 HTML 帧就绪标记；两账号隔离。
前端 3D 组件与渲染降级由 ``scripts/integration/ui_static_accept.py`` 与组件自测覆盖。
"""
from __future__ import annotations

import json

GEAR_PARAMS = {"seed": 7, "input_rpm": 1500, "driver_teeth": 12, "driven_teeth": 48}
SIM_PARAMS = {"seed": 42, "sim_seconds": 240, "arrival_interval_s": 6,
              "service_s": [4, 7, 5], "buffer_capacity": 3}


def _build(client, template_id: str, params: dict | None = None) -> dict:
    response = client.post("/api/demos/build", json={"template_id": template_id, "params": params or {}})
    assert response.status_code == 200, response.text
    return response.json()


def test_templates_include_mech_and_factory(client):
    response = client.get("/api/demos/templates")
    assert response.status_code == 200
    items = response.json()["items"]
    ids = {item["template_id"] for item in items}
    kinds = {item["kind"] for item in items}
    assert {"gearbox-3d", "pump-3d", "factory-line"} <= ids
    assert {"mech_3d", "factory_sim"} <= kinds
    gear = next(item for item in items if item["template_id"] == "gearbox-3d")
    for param in gear["params"]:
        assert {"name", "label", "default"} <= set(param)


def test_mech_build_is_deterministic_with_readouts(client):
    first = _build(client, "gearbox-3d", GEAR_PARAMS)
    second = _build(client, "gearbox-3d", GEAR_PARAMS)
    assert json.dumps(first["data"], sort_keys=True) == json.dumps(second["data"], sort_keys=True)
    assert first["seed"] == 7
    data = first["data"]
    assert data["parts"] and data["steps"] and data["readouts"]
    readouts = {item["id"]: item for item in data["readouts"]}
    assert abs(float(readouts["ratio"]["value"]) - 4.0) < 1e-6
    assert abs(float(readouts["output_rpm"]["value"]) - 375.0) < 1e-3
    assert data.get("basis") == "local-teaching"
    assert "教学" in str(data.get("source_note") or "")


def test_build_validation(client):
    assert client.post("/api/demos/build", json={"template_id": "nope"}).status_code == 404
    out_of_range = client.post(
        "/api/demos/build", json={"template_id": "gearbox-3d", "params": {"input_rpm": 99999}}
    )
    assert out_of_range.status_code == 422
    unknown_param = client.post(
        "/api/demos/build", json={"template_id": "gearbox-3d", "params": {"nonsense": 1}}
    )
    assert unknown_param.status_code == 422
    bad_service = client.post(
        "/api/demos/simulate",
        json={"template_id": "factory-line", "params": {**SIM_PARAMS, "service_s": [1, 2]}},
    )
    assert bad_service.status_code == 422


def test_factory_simulation_is_reproducible(client, second_client):
    first = client.post("/api/demos/simulate", json={"template_id": "factory-line", "params": SIM_PARAMS})
    second = client.post("/api/demos/simulate", json={"template_id": "factory-line", "params": SIM_PARAMS})
    assert first.status_code == 200 and second.status_code == 200
    run_a = first.json()["run"]
    run_b = second.json()["run"]
    assert run_a["seed"] == 42
    assert json.dumps(run_a["result"], sort_keys=True) == json.dumps(run_b["result"], sort_keys=True)
    result = run_a["result"]
    metrics = result["metrics"]
    assert metrics["completed"] <= metrics["arrivals"]
    assert 0 <= metrics["bottleneck_station"] <= 2
    assert metrics["bottleneck_reason"]
    assert result["events"] and all(event["t"] <= metrics["sim_seconds"] for event in result["events"])
    # 运行记录本人可回读、他人 404。
    run_id = run_a["run_id"]
    assert client.get(f"/api/demos/simulate/{run_id}").status_code == 200
    assert second_client.get(f"/api/demos/simulate/{run_id}").status_code == 404
    # 不同种子应产生不同的事件/metrics（确定性但非恒定）。
    other = client.post("/api/demos/simulate", json={"template_id": "factory-line",
                                                     "params": {**SIM_PARAMS, "seed": 43}})
    assert other.status_code == 200
    assert json.dumps(other.json()["run"]["result"], sort_keys=True) != json.dumps(result, sort_keys=True)


def _artifact(client, kind: str, title: str, data: dict) -> dict:
    response = client.post(
        "/api/artifacts",
        json={"kind": kind, "title": title, "data": data, "preview": {"kind": "mech-demo"}},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    return {"artifact_id": body["artifact"]["artifact_id"], "revision": body["revision"]["revision"]}


def test_mech_artifact_attaches_to_classroom_and_exports_frame_ready(client):
    mech = _artifact(client, "mech_3d", "M5 机械集成演示", _build(client, "gearbox-3d", GEAR_PARAMS)["data"])
    course = client.post(
        "/api/classroom/courses",
        json={"topic": "M5 集成课堂", "subject": "物理", "grade": "高中"},
    ).json()["course"]
    attached = client.post(
        f"/api/classroom/courses/{course['id']}/scenes/demo",
        json={"artifact_id": mech["artifact_id"], "revision": mech["revision"]},
    )
    assert attached.status_code in (200, 201), attached.text
    html = client.get(f"/api/classroom/courses/{course['id']}/export", params={"format": "html"})
    assert html.status_code == 200
    text = html.text
    assert "cls-demo-scene" in text
    assert "zhixu-demo-frame-ready" in text and "data-demo-frame-ready" in text
    assert "机械 3D" in text

    sim = client.post("/api/demos/simulate", json={"template_id": "factory-line", "params": SIM_PARAMS}).json()
    factory = _artifact(client, "factory_sim", "M5 工厂集成演示", sim["run"]["result"])
    attached2 = client.post(
        f"/api/classroom/courses/{course['id']}/scenes/demo",
        json={"artifact_id": factory["artifact_id"], "revision": factory["revision"]},
    )
    assert attached2.status_code in (200, 201)
    html2 = client.get(f"/api/classroom/courses/{course['id']}/export", params={"format": "html"})
    assert "工厂仿真" in html2.text
