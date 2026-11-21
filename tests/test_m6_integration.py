"""M6 集成测试：专题报告、互动讲义与小纸「按步骤提示」工具（真实应用）。

覆盖：资料上传 → 专题报告规则汇编（真实引用/页码/chunk）、版本 CAS 与两账号隔离；
课程 + 学习附件 → 互动讲义小节与自包含 HTML/PDF 导出；助手工具清单包含只读 step_hint。
扫描资料本地 OCR 内容块由 ``tests/test_m6_ocr_blocks.py`` 覆盖。
"""
from __future__ import annotations

import secrets

DOC_TEXT = (
    "第一章 函数与导数\n"
    "1.1 函数的概念\n"
    "M6专题验收关键词ALPHA 用于验证专题报告的引用与定位。\n"
    "1.2 函数的单调性\n"
    "函数单调递增时导数非负。\n"
)


def _upload_document(client) -> dict:
    response = client.post(
        "/api/documents",
        files={"file": ("m6-topic.txt", DOC_TEXT.encode("utf-8"), "text/plain")},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _course(client) -> dict:
    response = client.post(
        "/api/classroom/courses",
        json={"topic": "M6 互动讲义验收课堂", "subject": "数学", "grade": "高中"},
    )
    assert response.status_code in (200, 201), response.text
    return response.json()["course"]


def _artifact(client) -> dict:
    response = client.post(
        "/api/artifacts",
        json={
            "kind": "flow_animation",
            "title": "M6 讲义流程图",
            "data": {"layout": "flow", "nodes": [{"id": "n1", "label": "起点"}], "edges": [],
                     "steps": [{"label": "第一步"}]},
            "preview": {"kind": "principle-demo"},
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    return {"artifact_id": body["artifact"]["artifact_id"], "revision": body["revision"]["revision"]}


def test_topic_report_cites_real_document_chunks(client, second_client):
    document = _upload_document(client)
    created = client.post(
        "/api/topic-reports",
        json={"topic": "M6专题验收关键词ALPHA", "document_ids": [document["id"]]},
    )
    assert created.status_code in (200, 201), created.text
    body = created.json()
    report = body["report"]
    content = body["content_md"]
    assert report["generator"] == "rules"
    assert "规则汇编" in content
    assert "M6专题验收关键词ALPHA" in content  # 命中的真实资料片段
    assert "第 1 页" in content and "chunk" in content  # 页码与 chunk 引用
    assert any(source["kind"] == "document" for source in body["sources"])

    detail = client.get(f"/api/topic-reports/{report['report_id']}")
    assert detail.status_code == 200 and detail.json()["content_md"] == content
    listing = client.get("/api/topic-reports")
    assert listing.status_code == 200 and listing.json()["total"] >= 1

    # 版本 CAS：旧版本 PATCH 冲突 409，新版本可改标题。
    stale = client.patch(
        f"/api/topic-reports/{report['report_id']}",
        json={"title": "改名", "expected_version": report["version"] - 1},
    )
    assert stale.status_code == 409
    renamed = client.patch(
        f"/api/topic-reports/{report['report_id']}",
        json={"title": "M6 专题报告（改）", "expected_version": report["version"]},
    )
    assert renamed.status_code == 200 and renamed.json()["report"]["version"] == report["version"] + 1

    markdown = client.get(f"/api/topic-reports/{report['report_id']}/markdown")
    assert markdown.status_code == 200
    assert "text/markdown" in markdown.headers.get("content-type", "")
    assert "# " in markdown.text or "## " in markdown.text

    # 两账号隔离：他人不可读/不可删。
    assert second_client.get(f"/api/topic-reports/{report['report_id']}").status_code == 404
    assert second_client.delete(f"/api/topic-reports/{report['report_id']}").status_code == 404
    assert client.delete(f"/api/topic-reports/{report['report_id']}").status_code == 200


def test_topic_report_rejects_empty_sources_and_foreign_documents(client, second_client):
    assert client.post("/api/topic-reports", json={"topic": "没有来源"}).status_code == 422
    document = _upload_document(client)
    foreign = second_client.post(
        "/api/topic-reports", json={"topic": "他人资料", "document_ids": [document["id"]]}
    )
    assert foreign.status_code in (404, 422)


def test_handout_sections_and_exports(client, second_client):
    course = _course(client)
    artifact = _artifact(client)
    created = client.post(
        "/api/handouts",
        json={"title": "M6 互动讲义", "course_id": course["id"],
              "artifact_ids": [artifact["artifact_id"]]},
    )
    assert created.status_code in (200, 201), created.text
    body = created.json()
    handout = body["handout"]
    sections = body["sections"]
    kinds = [section["kind"] for section in sections]
    assert "course_outline" in kinds
    assert "artifact" in kinds
    assert any(
        item.get("ref") == artifact["artifact_id"] or artifact["artifact_id"] in str(item)
        for section in sections for item in section.get("items") or []
    ) or any(artifact["artifact_id"] in str(section) for section in sections)

    detail = client.get(f"/api/handouts/{handout['handout_id']}")
    assert detail.status_code == 200 and detail.json()["sections"] == sections
    listing = client.get("/api/handouts")
    assert listing.status_code == 200 and listing.json()["total"] >= 1

    html = client.get(f"/api/handouts/{handout['handout_id']}/export", params={"format": "html"})
    assert html.status_code == 200
    text = html.text
    assert "data-handout-id" in text and "规则汇编" in text
    assert "http://" not in text and "https://" not in text
    assert "<script" not in text.lower()

    pdf = client.get(f"/api/handouts/{handout['handout_id']}/export", params={"format": "pdf"})
    assert pdf.status_code in (200, 503)
    if pdf.status_code == 200:
        assert pdf.content[:4] == b"%PDF"
    assert client.get(
        f"/api/handouts/{handout['handout_id']}/export", params={"format": "exe"}
    ).status_code == 422

    assert second_client.get(f"/api/handouts/{handout['handout_id']}").status_code == 404
    assert client.delete(f"/api/handouts/{handout['handout_id']}").status_code == 200


def test_handout_rejects_empty_sources(client):
    assert client.post("/api/handouts", json={"title": "空讲义"}).status_code == 422


def test_assistant_exposes_readonly_step_hint_tool(client):
    tools = client.get("/api/assistant/tools")
    assert tools.status_code == 200
    items = tools.json()["items"]
    spec = next((item for item in items if item["name"] == "step_hint"), None)
    assert spec is not None, f"未找到 step_hint 工具：{[item['name'] for item in items]}"
    assert spec["side_effect"] is False and spec["requires_confirmation"] is False
    params = {param["name"] for param in spec["params"]}
    assert "ref" in params
