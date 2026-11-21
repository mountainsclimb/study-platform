"""M5 后端：``app/demo_library.py`` 机械/工厂演示模板库 + 固定种子 DES 测试。

本文件不依赖 ``app.main``（课堂附件接线用例除外）：用本地 ``FastAPI()`` 只挂载
``demo_library.router``，并通过 ``app.dependency_overrides[current_user]`` 注入当前用户；数据库走
``isolated_db`` 临时目录，显式调用 ``db.init_db()`` 与 ``demo_library.init_demo_library()``。

覆盖：
* ``demo_sim_runs`` 建表幂等与列结构；
* 模板列表（gearbox-3d / pump-3d / factory-line / factory-line-2d）与参数元数据完整性；
* 机械构建的确定性、工程读数公式（速比 / 输出转速 / 扭矩 / 分度圆直径）与默认值；
* 参数校验中文口径：未知模板 404、未知参数 / 越界 / 类型不符 / ``service_s`` 非长度 3 数字数组 422；
* 工厂 DES：build 与 simulate 结果一致、同种子逐字段可复现、不同种子不同；
* 运行记录持久化、本人可回读、他人 / 不存在 404；
* 指标与事件契约（吞吐 / 等待 / 利用率 / 瓶颈 / 事件时间窗口与排序 / 截断说明）；
* 小缓冲大流量下的 queue / blocked 事件与丢弃；
* 新增 ``mech_3d`` / ``factory_sim`` 学习附件经真实 client fixture 保存并插入课堂 demo 场景。
"""
from __future__ import annotations

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app import db, demo_library
from app.auth import current_user

USER_A = {"id": 101, "username": "甲同学", "role": "student"}
USER_B = {"id": 102, "username": "乙同学", "role": "student"}

GEAR_PARAMS = {"seed": 7, "input_rpm": 1500, "driver_teeth": 12, "driven_teeth": 48}
SIM_PARAMS = {
    "seed": 42,
    "sim_seconds": 240,
    "arrival_interval_s": 6,
    "service_s": [4, 7, 5],
    "buffer_capacity": 3,
}
SIM_KEYS = {
    "sim_seconds",
    "arrivals",
    "completed",
    "throughput_per_min",
    "avg_wait_s",
    "max_queue",
    "utilization",
    "bottleneck_station",
    "bottleneck_reason",
    "dropped",
    "blocked",
}


# ---------------------------------------------------------------------------
# 夹具与工具
# ---------------------------------------------------------------------------


@pytest.fixture
def demos(isolated_db):
    """隔离应用库 + 本地 FastAPI（只挂 demo router），返回 (TestClient, 用户持有器)。"""
    db.init_db()
    demo_library.init_demo_library()
    app = FastAPI()
    app.include_router(demo_library.router)
    holder = {"user": dict(USER_A)}
    app.dependency_overrides[current_user] = lambda: holder["user"]
    client = TestClient(app)
    try:
        yield client, holder
    finally:
        client.close()


def _switch_user(holder, user) -> None:
    holder["user"] = dict(user)


def _build(client, template_id, params=None) -> dict:
    response = client.post(
        "/api/demos/build", json={"template_id": template_id, "params": params or {}}
    )
    assert response.status_code == 200, response.text
    return response.json()


def _simulate(client, template_id="factory-line", params=None) -> dict:
    response = client.post(
        "/api/demos/simulate", json={"template_id": template_id, "params": params or {}}
    )
    assert response.status_code == 200, response.text
    return response.json()


def _stable(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _readouts(data) -> dict:
    return {item["id"]: item for item in data["readouts"]}


# ---------------------------------------------------------------------------
# 1. 建表幂等
# ---------------------------------------------------------------------------


def test_init_demo_library_creates_runs_table_idempotently(isolated_db):
    db.init_db()
    demo_library.init_demo_library()
    demo_library.init_demo_library()
    with db.connection() as con:
        tables = {
            row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        columns = [row[1] for row in con.execute("PRAGMA table_info(demo_sim_runs)")]
    assert "demo_sim_runs" in tables
    assert columns == [
        "id",
        "user_id",
        "template_id",
        "seed",
        "params_json",
        "result_json",
        "created_at",
    ]


# ---------------------------------------------------------------------------
# 2. 模板列表与参数元数据
# ---------------------------------------------------------------------------


def test_templates_list_metal_and_factory_metadata(demos):
    client, _holder = demos
    response = client.get("/api/demos/templates")
    assert response.status_code == 200, response.text
    body = response.json()
    items = body["items"]
    assert body["total"] == len(items) >= 4
    by_id = {item["template_id"]: item for item in items}
    assert {"gearbox-3d", "pump-3d", "factory-line", "factory-line-2d"} <= set(by_id)
    assert {"mech_3d", "factory_sim", "flow_animation"} <= {
        item["kind"] for item in items
    }
    for item in items:
        assert item["parametric"] is True
        assert item["title"] and item["description"] and item["source_note"]
        assert isinstance(item["assumptions"], list) and item["assumptions"]
        assert item["params"], item["template_id"]
        for spec in item["params"]:
            assert {"name", "label", "type", "default", "min", "max", "step", "unit"} <= set(spec)
            assert spec["type"] in {"int", "float", "list"}

    gearbox = by_id["gearbox-3d"]
    assert [spec["name"] for spec in gearbox["params"]] == [
        "seed",
        "input_rpm",
        "driver_teeth",
        "driven_teeth",
        "module_mm",
    ]
    assert by_id["factory-line"]["kind"] == "factory_sim"
    service = next(spec for spec in by_id["factory-line"]["params"] if spec["name"] == "service_s")
    assert service["type"] == "list"
    assert service["length"] == 3
    assert len(service["default"]) == 3


# ---------------------------------------------------------------------------
# 3. 机械构建：确定性 + 工程读数
# ---------------------------------------------------------------------------


def test_gearbox_build_deterministic_with_engineering_readouts(demos):
    client, _holder = demos
    first = _build(client, "gearbox-3d", GEAR_PARAMS)
    second = _build(client, "gearbox-3d", GEAR_PARAMS)
    assert _stable(first["data"]) == _stable(second["data"])
    assert first["seed"] == 7
    assert first["kind"] == "mech_3d"
    assert first["params"]["module_mm"] == 2.0  # 未传参数由默认值补齐
    data = first["data"]
    assert data["version"] == 1
    assert data["basis"] == "local-teaching"
    assert data["seed"] == 7
    assert data["parts"] and data["steps"] and data["readouts"] and data["camera"]
    readouts = _readouts(data)
    assert readouts["ratio"]["value"] == pytest.approx(4.0)
    assert readouts["output_rpm"]["value"] == pytest.approx(375.0)
    assert readouts["torque_amplification"]["value"] == pytest.approx(4.0)
    assert readouts["pitch_diameter_driver"]["value"] == pytest.approx(24.0)
    assert readouts["pitch_diameter_driven"]["value"] == pytest.approx(96.0)
    assert readouts["center_distance"]["value"] == pytest.approx(60.0)
    assert all("教学" in readout["note"] for readout in data["readouts"])
    # 步骤引用的部件必须都存在于部件树中，explode 在 0..1。
    part_ids = {part["id"] for part in data["parts"]}
    assert len(part_ids) == len(data["parts"])
    for step in data["steps"]:
        assert set(step["part_ids"]) <= part_ids
        assert 0.0 <= step["explode"] <= 1.0
    assert {"type", "position", "target"} <= set(data["camera"])


def test_gearbox_defaults_and_pump_readouts(demos):
    client, _holder = demos
    gearbox = _build(client, "gearbox-3d")
    assert gearbox["params"] == {
        "seed": 1,
        "input_rpm": 1200,
        "driver_teeth": 12,
        "driven_teeth": 48,
        "module_mm": 2.0,
    }
    assert _readouts(gearbox["data"])["output_rpm"]["value"] == pytest.approx(300.0)

    pump = _build(client, "pump-3d", {"seed": 5, "stroke_mm": 60, "rpm": 180, "bore_mm": 60})
    assert pump["kind"] == "mech_3d"
    assert pump["seed"] == 5
    assert pump["params"]["stroke_mm"] == 60
    readouts = _readouts(pump["data"])
    assert readouts["stroke"]["value"] == 60
    assert readouts["displacement_cm3"]["value"] == pytest.approx(169.646, abs=1e-2)
    assert readouts["flow_l_min"]["value"] == pytest.approx(30.5363, abs=1e-2)
    assert readouts["mean_piston_speed"]["value"] == pytest.approx(0.36)
    assert readouts["crank_omega"]["value"] == pytest.approx(18.8496, abs=1e-2)
    assert pump["data"]["basis"] == "local-teaching"
    assert {part["id"] for part in pump["data"]["parts"]} >= {
        "cylinder-block",
        "piston",
        "connecting-rod",
        "crankshaft",
        "intake-valve",
        "exhaust-valve",
    }


# ---------------------------------------------------------------------------
# 4. 错误口径：404 / 422（中文 detail）
# ---------------------------------------------------------------------------


def test_unknown_template_404_and_missing_template_422(demos):
    client, _holder = demos
    unknown_build = client.post(
        "/api/demos/build", json={"template_id": "no-such-template"}
    )
    assert unknown_build.status_code == 404
    assert "不存在" in unknown_build.json()["detail"]
    assert client.post(
        "/api/demos/simulate", json={"template_id": "no-such-template"}
    ).status_code == 404
    missing = client.post("/api/demos/build", json={})
    assert missing.status_code == 422
    assert "模板 ID" in missing.json()["detail"]
    wrong_type = client.post("/api/demos/build", json={"template_id": 123})
    assert wrong_type.status_code == 422
    assert "文本" in wrong_type.json()["detail"]


def test_unknown_parameter_and_type_validation_422(demos):
    client, _holder = demos
    unknown = client.post(
        "/api/demos/build",
        json={"template_id": "gearbox-3d", "params": {"nonsense": 1}},
    )
    assert unknown.status_code == 422
    assert unknown.json()["detail"] == "未知参数：nonsense"

    out_of_range = client.post(
        "/api/demos/build", json={"template_id": "gearbox-3d", "params": {"input_rpm": 99999}}
    )
    assert out_of_range.status_code == 422
    assert "需在 300–3000" in out_of_range.json()["detail"]

    not_integer = client.post(
        "/api/demos/build", json={"template_id": "gearbox-3d", "params": {"input_rpm": 1500.5}}
    )
    assert not_integer.status_code == 422
    assert "整数" in not_integer.json()["detail"]

    bool_rejected = client.post(
        "/api/demos/build", json={"template_id": "gearbox-3d", "params": {"driver_teeth": True}}
    )
    assert bool_rejected.status_code == 422

    bad_float = client.post(
        "/api/demos/build", json={"template_id": "gearbox-3d", "params": {"module_mm": "2"}}
    )
    assert bad_float.status_code == 422
    assert "数字" in bad_float.json()["detail"]

    params_not_object = client.post(
        "/api/demos/build", json={"template_id": "gearbox-3d", "params": [1, 2, 3]}
    )
    assert params_not_object.status_code == 422
    assert "JSON 对象" in params_not_object.json()["detail"]


def test_service_s_must_be_three_number_list(demos):
    client, _holder = demos
    cases = [
        ([1, 2], "长度为 3"),
        ("not-a-list", "长度为 3"),
        ([2, 40, 2], "[1]"),
        ([2, True, 8], "[1] 必须是数字"),
    ]
    for service_s, expected in cases:
        response = client.post(
            "/api/demos/simulate",
            json={
                "template_id": "factory-line",
                "params": {**SIM_PARAMS, "service_s": service_s},
            },
        )
        assert response.status_code == 422, service_s
        assert expected in response.json()["detail"], response.json()["detail"]


def test_simulate_rejects_templates_without_factory_kind(demos):
    client, _holder = demos
    for template_id in ("pump-3d", "factory-line-2d", "gearbox-3d"):
        response = client.post(
            "/api/demos/simulate", json={"template_id": template_id, "params": {}}
        )
        assert response.status_code == 422, template_id
        assert "不支持持久化仿真" in response.json()["detail"]


# ---------------------------------------------------------------------------
# 5. 工厂 DES：确定性、build 与 simulate 一致、不同种子不同
# ---------------------------------------------------------------------------


def test_factory_build_equals_simulate_and_is_seed_deterministic(demos):
    client, _holder = demos
    first = _build(client, "factory-line", SIM_PARAMS)
    second = _build(client, "factory-line", SIM_PARAMS)
    assert _stable(first["data"]) == _stable(second["data"])
    assert first["data"]["basis"] == "deterministic-des"
    assert first["seed"] == 42
    assert first["data"]["metrics"]["sim_seconds"] == 240
    assert first["data"]["events"]

    simulation = _simulate(client, "factory-line", SIM_PARAMS)
    assert simulation["deterministic"] is True
    assert simulation["run"]["seed"] == 42
    assert _stable(simulation["run"]["result"]) == _stable(first["data"])
    # 参数归一化口径：整数数组输入回显为浮点列表。
    assert simulation["run"]["params"]["service_s"] == [4.0, 7.0, 5.0]

    other_seed = _build(client, "factory-line", {**SIM_PARAMS, "seed": 43})
    assert _stable(other_seed["data"]) != _stable(first["data"])
    assert len(other_seed["data"]["events"]) != len(first["data"]["events"])
    assert other_seed["data"]["metrics"] != first["data"]["metrics"]


# ---------------------------------------------------------------------------
# 6. 运行记录持久化与归属
# ---------------------------------------------------------------------------


def test_simulate_persists_run_and_authorizes(demos):
    client, holder = demos
    first = _simulate(client, "factory-line", SIM_PARAMS)
    second = _simulate(client, "factory-line", SIM_PARAMS)
    run_a = first["run"]
    run_b = second["run"]
    assert run_a["run_id"] != run_b["run_id"]
    assert _stable(run_a["result"]) == _stable(run_b["result"])
    assert run_a["created_at"]
    assert run_a["template_id"] == "factory-line"
    assert run_a["params"]["seed"] == 42

    fetched = client.get(f"/api/demos/simulate/{run_a['run_id']}")
    assert fetched.status_code == 200, fetched.text
    fetched_run = fetched.json()["run"]
    assert _stable(fetched_run["result"]) == _stable(run_a["result"])
    assert fetched_run["params"] == run_a["params"]

    # 他人不可读、不存在/非法编号统一 404。
    _switch_user(holder, USER_B)
    assert client.get(f"/api/demos/simulate/{run_a['run_id']}").status_code == 404
    _switch_user(holder, USER_A)
    assert client.get("/api/demos/simulate/999999").status_code == 404
    assert client.get("/api/demos/simulate/not-a-number").status_code == 404


# ---------------------------------------------------------------------------
# 7. 指标与事件契约
# ---------------------------------------------------------------------------


def test_metrics_and_events_contract(demos):
    client, _holder = demos
    result = _simulate(client, "factory-line", SIM_PARAMS)["run"]["result"]
    metrics = result["metrics"]
    assert SIM_KEYS <= set(metrics)
    assert metrics["sim_seconds"] == 240
    assert isinstance(metrics["arrivals"], int) and metrics["arrivals"] >= 0
    assert isinstance(metrics["completed"], int) and 0 <= metrics["completed"] <= metrics["arrivals"]
    assert isinstance(metrics["throughput_per_min"], float)
    assert metrics["throughput_per_min"] == pytest.approx(
        round(metrics["completed"] / metrics["sim_seconds"] * 60.0, 4)
    )
    assert len(metrics["avg_wait_s"]) == 3
    assert all(value >= 0 for value in metrics["avg_wait_s"])
    assert len(metrics["max_queue"]) == 3
    assert all(isinstance(value, int) and value >= 0 for value in metrics["max_queue"])
    assert len(metrics["utilization"]) == 3
    assert all(0.0 <= value <= 1.0 for value in metrics["utilization"])
    assert metrics["bottleneck_station"] in (0, 1, 2)
    assert metrics["bottleneck_reason"]
    assert metrics["dropped"] >= 0 and metrics["blocked"] >= 0

    events = result["events"]
    assert events
    assert len(events) <= demo_library.MAX_EVENTS
    order = {name: index for index, name in enumerate(demo_library.EVENT_TYPES)}
    previous = None
    for event in events:
        assert set(event) == {"t", "type", "station", "item_id", "queue"}
        assert event["type"] in demo_library.EVENT_TYPES
        assert event["station"] in (0, 1, 2)
        assert isinstance(event["item_id"], int) and event["item_id"] >= 1
        assert isinstance(event["queue"], int) and event["queue"] >= 0
        assert 0.0 <= event["t"] <= metrics["sim_seconds"]
        # 事件 t 保留 5 位小数
        assert round(event["t"], 5) == event["t"]
        key = (event["t"], order[event["type"]], event["station"], event["item_id"])
        if previous is not None:
            assert key >= previous
        previous = key


def test_sim_seconds_window_bounds_all_events(demos):
    client, _holder = demos
    params = {**SIM_PARAMS, "sim_seconds": 60, "arrival_interval_s": 4.0}
    result = _build(client, "factory-line", params)["data"]
    assert result["metrics"]["sim_seconds"] == 60
    assert result["events"]
    assert max(event["t"] for event in result["events"]) <= 60
    assert all(event["t"] >= 0 for event in result["events"])


# ---------------------------------------------------------------------------
# 8. 小缓冲大流量：queue / blocked / 丢弃
# ---------------------------------------------------------------------------


def test_small_buffer_generates_queue_blocked_and_dropped_events(demos):
    client, _holder = demos
    params = {
        "seed": 5,
        "sim_seconds": 600,
        "arrival_interval_s": 4,
        "service_s": [2, 2, 30],
        "buffer_capacity": 1,
    }
    first = _build(client, "factory-line", params)
    second = _build(client, "factory-line", params)
    assert _stable(first["data"]) == _stable(second["data"])
    data = first["data"]
    event_types = {event["type"] for event in data["events"]}
    assert {"queue", "blocked"} <= event_types
    metrics = data["metrics"]
    assert metrics["arrivals"] > metrics["completed"]
    assert metrics["dropped"] > 0 or metrics["blocked"] > 0
    assert max(metrics["max_queue"]) >= 1


# ---------------------------------------------------------------------------
# 9. 事件截断说明（monkeypatch 小上限，避免构造超长仿真）
# ---------------------------------------------------------------------------


def test_event_truncation_is_documented_in_assumptions(demos, monkeypatch):
    client, _holder = demos
    monkeypatch.setattr(demo_library, "MAX_EVENTS", 10)
    data = _build(client, "factory-line", SIM_PARAMS)["data"]
    assert len(data["events"]) == 10
    assert any("截断" in note for note in data["assumptions"])
    # 指标仍按全量事件计算，不受展示截断影响。
    assert data["metrics"]["arrivals"] > 10


# ---------------------------------------------------------------------------
# 10. 新增附件类型：真实 client fixture 下保存并插入课堂 demo 场景
# ---------------------------------------------------------------------------


def test_mech_and_factory_artifacts_accepted_by_artifacts_and_classroom(client):
    from app.classroom import schema

    assert {"mech_3d", "factory_sim"} <= set(schema.DEMO_ARTIFACT_KINDS)
    assert schema.DEMO_ARTIFACT_KIND_LABELS["mech_3d"] == "机械 3D"
    assert schema.DEMO_ARTIFACT_KIND_LABELS["factory_sim"] == "工厂仿真"

    gearbox = demo_library.build_template("gearbox-3d", params=GEAR_PARAMS)
    factory = demo_library.build_template("factory-line", params=SIM_PARAMS)
    payloads = [
        ("mech_3d", "M5 机械演示附件", gearbox["data"]),
        ("factory_sim", "M5 工厂仿真附件", factory["data"]),
    ]
    artifacts = []
    for kind, title, data in payloads:
        response = client.post(
            "/api/artifacts",
            json={"kind": kind, "title": title, "data": data, "preview": {"kind": "m5-demo"}},
        )
        assert response.status_code == 201, response.text
        body = response.json()
        artifacts.append(
            {"artifact_id": body["artifact"]["artifact_id"], "revision": body["revision"]["revision"]}
        )
        fetched = client.get(f"/api/artifacts/{artifacts[-1]['artifact_id']}")
        assert fetched.status_code == 200, fetched.text
        assert fetched.json()["artifact"]["kind"] == kind
        assert _stable(fetched.json()["revision"]["data"]) == _stable(data)

    meta = client.get("/api/classroom/meta").json()
    assert {"mech_3d", "factory_sim"} <= set(meta["demo_artifact_kinds"]["demo"])

    course = client.post(
        "/api/classroom/courses",
        json={"topic": "M5 机械与工厂演示课堂", "subject": "物理", "grade": "高中"},
    )
    assert course.status_code in (200, 201), course.text
    course_id = course.json()["course"]["id"]
    for artifact in artifacts:
        attached = client.post(
            f"/api/classroom/courses/{course_id}/scenes/demo",
            json={"artifact_id": artifact["artifact_id"], "revision": artifact["revision"]},
        )
        assert attached.status_code in (200, 201), attached.text
        assert attached.json()["scene"]["scene_type"] == "demo"
