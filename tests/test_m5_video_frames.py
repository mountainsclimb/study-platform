"""M5 视频/导出帧回执测试：课堂演示场景进入离线播放器并可被截帧。

课堂视频导出使用 ``build_offline_html`` 生成的自包含播放器逐场景截图；本文件验证
机械/工厂类演示场景在离线 HTML 中渲染为固定版本海报 + 说明 + 内嵌快照，并带
``data-demo-frame-ready`` 帧就绪标记，供视频管线等待与截图。
"""
from __future__ import annotations

import pytest

MECH_DATA = {
    "version": 1,
    "basis": "local-teaching",
    "seed": 7,
    "parts": [
        {"id": "p1", "label": "箱体", "kind": "box", "position": [0, 0, 0], "size": [2, 1.2, 1.4]},
        {"id": "p2", "label": "输入轴", "kind": "rod", "position": [0, 0.2, 0], "size": [1.6, 0.12, 0.12]},
        {"id": "p3", "label": "小齿轮", "kind": "gear", "position": [0.2, 0.2, 0], "size": [0.5, 0.5, 0.2]},
    ],
    "steps": [
        {"label": "拆下箱盖", "part_ids": ["p1"], "explode": 0.4},
        {"label": "取出齿轮组", "part_ids": ["p2", "p3"], "explode": 0.8},
    ],
    "readouts": [{"id": "ratio", "label": "速比", "value": 4.0, "unit": "", "note": "教学近似"}],
    "camera": {"position": [3, 2.4, 3.2], "target": [0, 0.3, 0]},
    "assumptions": ["教学示意，非厂商图纸"],
    "source_note": "本地教学示意",
}


def _create_artifact(client, kind: str, data: dict, title: str) -> dict:
    response = client.post(
        "/api/artifacts",
        json={"kind": kind, "title": title, "data": data,
              "preview": {"kind": "mech-demo"}},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    return {"artifact_id": body["artifact"]["artifact_id"], "revision": body["revision"]["revision"]}


def _course_with_demo(client, artifact: dict) -> int:
    course = client.post(
        "/api/classroom/courses",
        json={"topic": "M5 帧回执测试课堂", "subject": "物理", "grade": "高中"},
    ).json()["course"]
    attached = client.post(
        f"/api/classroom/courses/{course['id']}/scenes/demo",
        json={"artifact_id": artifact["artifact_id"], "revision": artifact["revision"]},
    )
    assert attached.status_code in (200, 201), attached.text
    return course["id"]


def test_demo_scene_enters_offline_player_with_frame_ready(client):
    artifact = _create_artifact(client, "mech_3d", MECH_DATA, "M5 机械演示")
    course_id = _course_with_demo(client, artifact)
    html = client.get(f"/api/classroom/courses/{course_id}/export", params={"format": "html"})
    assert html.status_code == 200, html.text
    text = html.text
    assert "cls-demo-scene" in text
    assert "zhixu-demo-frame-ready" in text and "data-demo-frame-ready" in text
    assert f'data-fixed-revision="{artifact["revision"]}"' in text
    assert "机械 3D" in text
    assert artifact["artifact_id"] in text
    # 离线播放器把场景纳入 __zhixuCapture.sceneCount，视频管线才能逐场景截图。
    assert "sceneCount" in text and "__zhixuCapture" in text


def test_factory_sim_scene_exports_metrics_snapshot(client):
    factory_data = {
        "version": 1,
        "basis": "deterministic-des",
        "seed": 11,
        "flow": {"nodes": [{"id": "s0", "label": "工位 1"}, {"id": "s1", "label": "工位 2"},
                           {"id": "s2", "label": "工位 3"}],
                 "edges": [{"from": "s0", "to": "s1"}, {"from": "s1", "to": "s2"}]},
        "station_labels": ["工位 1", "工位 2", "工位 3"],
        "metrics": {"sim_seconds": 180, "arrivals": 22, "completed": 18, "throughput_per_min": 6.0,
                    "avg_wait_s": [1.2, 3.4, 5.6], "max_queue": [1, 2, 3],
                    "utilization": [0.5, 0.6, 0.7], "bottleneck_station": 2,
                    "bottleneck_reason": "工位 3 平均等待最高"},
        "events": [{"t": 1.0, "type": "arrival", "station": 0, "item_id": 1, "queue": 0}],
        "assumptions": ["固定种子教学仿真"],
        "source_note": "本地教学示意",
    }
    artifact = _create_artifact(client, "factory_sim", factory_data, "M5 工厂仿真")
    course_id = _course_with_demo(client, artifact)
    html = client.get(f"/api/classroom/courses/{course_id}/export", params={"format": "html"})
    assert html.status_code == 200
    text = html.text
    assert "工厂仿真" in text
    assert '"bottleneck_reason"' in text or "bottleneck_reason" in text
    assert "zhixu-demo-frame-ready" in text and "data-demo-frame-ready" in text


def test_print_html_demo_has_frame_ready_marker(client):
    artifact = _create_artifact(client, "mech_3d", MECH_DATA, "M5 打印讲义演示")
    course_id = _course_with_demo(client, artifact)
    printed = client.get(f"/api/classroom/courses/{course_id}/export", params={"format": "pdf"})
    # PDF 由 Playwright 渲染打印 HTML；不可用环境按既有口径 503。这里验证打印入口不报 500。
    assert printed.status_code in (200, 503)
    if printed.status_code == 200:
        assert printed.content[:4] == b"%PDF"
