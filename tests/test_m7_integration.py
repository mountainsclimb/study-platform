"""M7 集成测试：课堂离线可交互演示播放 + 专题报告保存为带出处的笔记。

真实应用（client fixture）：
- 学习附件 → 课堂演示场景 → 离线 HTML 内联离线播放器（flow/isometric/factory）且无外链、
  静态海报与固定版本标记保留；ZIP 同样带播放器与演示快照；无演示课程不含播放器。
- 上传资料 → 专题报告 → 保存为笔记 → 笔记可读且正文含出处清单与真实来源；重复保存不重复建笔记。
"""
from __future__ import annotations

DOC_TEXT = (
    "第一章 函数与导数\n"
    "1.1 函数的概念\n"
    "M7验收专题短语GAMMA 用于验证专题报告保存笔记的引用。\n"
)


def _mech_or_flow_artifact(client, kind: str, data: dict, title: str) -> dict:
    response = client.post(
        "/api/artifacts",
        json={"kind": kind, "title": title, "data": data, "preview": {"kind": "principle-demo"}},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    return {"artifact_id": body["artifact"]["artifact_id"], "revision": body["revision"]["revision"]}


def _flow_data() -> dict:
    return {
        "layout": "flow",
        "nodes": [
            {"id": "n1", "label": "起点", "kind": "start", "x": 40, "y": 80},
            {"id": "n2", "label": "中间步骤", "kind": "process", "x": 240, "y": 80},
            {"id": "n3", "label": "终点", "kind": "end", "x": 440, "y": 80},
        ],
        "edges": [{"from": "n1", "to": "n2"}, {"from": "n2", "to": "n3"}],
        "steps": [{"label": "第一步", "node_ids": ["n1"]}, {"label": "第二步", "node_ids": ["n2", "n3"]}],
        "source_note": "M7 离线回放",
        "assumptions": ["教学示意"],
    }


def _iso_data() -> dict:
    return {
        "style": "clean",
        "parts": [
            {"id": "p1", "label": "底座", "shape": "plate", "position": [0, 0, 0], "size": [3, 0.3, 2]},
            {"id": "p2", "label": "立柱", "shape": "box", "position": [0, 1.2, 0], "size": [0.4, 2, 0.4]},
        ],
        "labels": [{"part_id": "p2", "text": "立柱"}],
        "steps": [{"label": "整体", "part_ids": []}, {"label": "拆开", "part_ids": ["p2"], "explode": 0.6}],
        "source_note": "M7 等距回放",
        "assumptions": ["教学示意"],
    }


def _factory_data() -> dict:
    return {
        "version": 1,
        "basis": "deterministic-des",
        "seed": 42,
        "flow": {"nodes": [{"id": "s0", "label": "工位 1", "kind": "station"},
                           {"id": "s1", "label": "工位 2", "kind": "station"},
                           {"id": "s2", "label": "工位 3", "kind": "station"}],
                 "edges": [{"from": "s0", "to": "s1"}, {"from": "s1", "to": "s2"}]},
        "station_labels": ["工位 1", "工位 2", "工位 3"],
        "metrics": {"sim_seconds": 180, "arrivals": 22, "completed": 18, "throughput_per_min": 6.0,
                    "avg_wait_s": [1.2, 3.4, 5.6], "max_queue": [1, 2, 3],
                    "utilization": [0.5, 0.6, 0.7], "bottleneck_station": 2,
                    "bottleneck_reason": "工位 3 平均等待最高"},
        "events": [
            {"t": 1.0, "type": "arrival", "station": 0, "item_id": 1, "queue": 0},
            {"t": 2.0, "type": "service_start", "station": 0, "item_id": 1, "queue": 0},
            {"t": 4.0, "type": "service_end", "station": 0, "item_id": 1, "queue": 1},
        ],
        "assumptions": ["固定种子教学仿真"],
        "source_note": "M7 工厂回放",
    }


def _course_with_demo(client, artifact: dict, title: str = "M7 离线播放课堂") -> int:
    course = client.post(
        "/api/classroom/courses",
        json={"topic": title, "subject": "数学", "grade": "高中"},
    ).json()["course"]
    attached = client.post(
        f"/api/classroom/courses/{course['id']}/scenes/demo",
        json={"artifact_id": artifact["artifact_id"], "revision": artifact["revision"]},
    )
    assert attached.status_code in (200, 201), attached.text
    return course["id"]


def test_offline_html_embeds_interactive_demo_player(client):
    artifact = _mech_or_flow_artifact(client, "flow_animation", _flow_data(), "M7 流程回放")
    course_id = _course_with_demo(client, artifact)
    html = client.get(f"/api/classroom/courses/{course_id}/export", params={"format": "html"})
    assert html.status_code == 200, html.text
    text = html.text
    assert "ZhixuOfflineDemo" in text
    assert "data-offline-demo" in text
    assert 'data-demo-frame-ready="1"' in text or "data-demo-frame-ready" in text
    assert "cls-demo-scene" in text
    # 离线播放器必须自包含：无外链脚本/样式/CDN。
    assert "http://" not in text and "https://" not in text
    assert "new Function" not in text
    assert artifact["artifact_id"] in text


def test_offline_html_iso_and_factory_players(client):
    iso = _mech_or_flow_artifact(client, "isometric", _iso_data(), "M7 等距回放")
    iso_course = _course_with_demo(client, iso)
    iso_html = client.get(f"/api/classroom/courses/{iso_course}/export", params={"format": "html"}).text
    assert "ZhixuOfflineDemo" in iso_html and "isometric" in iso_html

    factory = _mech_or_flow_artifact(client, "factory_sim", _factory_data(), "M7 工厂回放")
    factory_course = _course_with_demo(client, factory)
    factory_html = client.get(
        f"/api/classroom/courses/{factory_course}/export", params={"format": "html"}
    ).text
    assert "ZhixuOfflineDemo" in factory_html
    assert "bottleneck" in factory_html


def test_zip_contains_offline_player_and_demo_snapshots(client):
    artifact = _mech_or_flow_artifact(client, "flow_animation", _flow_data(), "M7 ZIP 回放")
    course_id = _course_with_demo(client, artifact)
    zip_resp = client.get(f"/api/classroom/courses/{course_id}/export", params={"format": "zip"})
    assert zip_resp.status_code == 200, zip_resp.text
    import io
    import zipfile

    with zipfile.ZipFile(io.BytesIO(zip_resp.content)) as archive:
        names = archive.namelist()
        html_name = next((name for name in names if name.endswith(".html")), "")
        assert html_name, names
        html_text = archive.read(html_name).decode("utf-8", "ignore")
        assert "ZhixuOfflineDemo" in html_text
        assert any(name.startswith("demos/") for name in names)


def _insert_plain_slide(course_id: int, user_id: int = 1) -> None:
    """直接落库插入一个最小 slide 场景（零场景课程按既有契约不可导出）。"""
    from app import db
    from app.classroom import db as classroom_db
    from app.classroom import schema

    content, _ = schema.normalize_slide_content({
        "elements": [{
            "id": "t1", "type": "text", "left": 40, "top": 40, "width": 400, "height": 60,
            "content": "<p>M7 普通课堂</p>", "text": "M7 普通课堂",
        }],
    })
    now = db.utcnow()
    with db.connection() as con:
        con.execute(
            "INSERT INTO cls_scenes(course_id,user_id,scene_type,order_index,title,content_json,"
            "meta_json,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (course_id, user_id, "slide", 1, "M7 普通课堂",
             classroom_db.json_dump(content), classroom_db.json_dump({"total": 1}), "ready", now, now),
        )


def test_course_without_demo_keeps_offline_html_unchanged(client):
    course = client.post(
        "/api/classroom/courses",
        json={"topic": "M7 无演示课堂", "subject": "数学", "grade": "高中"},
    ).json()["course"]
    _insert_plain_slide(course["id"])
    html = client.get(f"/api/classroom/courses/{course['id']}/export", params={"format": "html"})
    assert html.status_code == 200, html.text
    assert "ZhixuOfflineDemo" not in html.text
    assert "data-offline-demo" not in html.text


def test_topic_report_saves_as_cited_note(client):
    document = client.post(
        "/api/documents",
        files={"file": ("m7-note.txt", DOC_TEXT.encode("utf-8"), "text/plain")},
    )
    assert document.status_code == 200, document.text
    document_id = document.json()["id"]
    created = client.post(
        "/api/topic-reports",
        json={"topic": "M7验收专题短语GAMMA", "document_ids": [document_id]},
    )
    assert created.status_code in (200, 201), created.text
    report = created.json()["report"]

    saved = client.post(f"/api/topic-reports/{report['report_id']}/save-note")
    assert saved.status_code in (200, 201), saved.text
    body = saved.json()
    assert body.get("saved") is True
    note_id = body["note"]["id"]
    assert body["note"]["source_count"] >= 1

    note = client.get(f"/api/notes/{note_id}")
    assert note.status_code == 200
    note_obj = note.json().get("note") or note.json()
    content = note_obj.get("content") or ""
    assert "M7验收专题短语GAMMA" in content
    assert "出处" in content
    assert "第 1 页" in content

    duplicate = client.post(f"/api/topic-reports/{report['report_id']}/save-note")
    assert duplicate.status_code in (200, 201)
    duplicate_body = duplicate.json()
    assert duplicate_body.get("saved") is False
    assert duplicate_body.get("duplicate_of_note_id") == note_id
