"""试卷工坊 P1：答题卡模板 / 几何校验 / SVG / 打印导出 / 试卷分栏排版。

覆盖：模板生成与四角定位点/气泡/学号区/页面码、几何校验失败 422、version CAS、
试卷结构变化后 stale、SVG 结构与印刷色、HTML/SVG 导出、归属/未登录、
HTML 分栏 + 自动分页、docx 分栏与 A4/A3 页面设置（python-docx 读回验证）。
"""
import io
import json

from docx import Document
from fastapi.testclient import TestClient
import pytest

from app.main import app
from app.paperlab import card_svg


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def create_question(client, *, qtype="SINGLE", options=4, score_note=None) -> dict:
    if qtype == "SUBJECTIVE":
        content = {
            "stem": "证明函数 $f(x)=x^2$ 在区间 $[0,+\\infty)$ 上单调递增。",
            "answers": ["任取 0≤x1<x2，比较 f(x2)-f(x1)。"],
            "explanation": "利用差商与因式分解可证。",
            "rubric": "构造差商 3 分，结论 2 分。",
        }
    else:
        content = {
            "stem": "已知 $f(x)=x^2$，则 $f(2)$ 等于？",
            "options": [{"label": chr(ord("A") + index), "text": f"选项 {index}"} for index in range(options)],
            "answers": ["A"],
            "explanation": "代入 x=2 得 4。",
        }
    response = client.post("/api/paperlab/questions", json={
        "type": qtype, "subject": "数学", "grade": "高中一年级", "difficulty": 0.4,
        "knowledge_tags": ["函数"], "content": content, "status": "ready",
    })
    assert response.status_code == 200, response.text
    return response.json()["question"]


def create_paper(client, questions, *, layout=None, title="函数综合练习") -> dict:
    items = []
    for index, question in enumerate(questions):
        qtype = "SUBJECTIVE" if question["type"] == "SUBJECTIVE" else "选择题"
        items.append({
            "ref": f"app:{question['id']}",
            "score": 10 if question["type"] == "SUBJECTIVE" else 5,
            "section": "二、解答题" if qtype == "SUBJECTIVE" else "一、选择题",
        })
    response = client.post("/api/paperlab/papers", json={
        "title": title, "kind": "manual", "subject": "数学", "grade": "高中一年级",
        "layout": layout or {"paper": "A4", "columns": 2, "answer_space_mm": 20},
        "items": items,
    })
    assert response.status_code == 200, response.text
    return response.json()


def user_id(client) -> int:
    return client.get("/api/auth/me").json()["id"]


def objective_items(layout: dict) -> list[dict]:
    return [
        question
        for page in layout["pages"]
        for group in page["groups"]
        for question in group["questions"]
        if question["type"] in ("SINGLE", "MULTIPLE")
    ]


def subjective_items(layout: dict) -> list[dict]:
    return [
        question
        for page in layout["pages"]
        for group in page["groups"]
        for question in group["questions"]
        if question["type"] not in ("SINGLE", "MULTIPLE")
    ]


def _text_rect(cell: dict) -> dict:
    """测试用文字矩形（与 card_svg 的估算一致：中文 1em、ASCII 0.55em）。"""
    size = float(cell.get("size") or 4.0)
    text = str(cell.get("text") or "")
    width = sum(size if ord(char) > 0x2E80 else size * 0.55 for char in text)
    x = float(cell.get("x") or 0)
    if str(cell.get("anchor") or "start") == "middle":
        x -= width / 2
    elif str(cell.get("anchor") or "start") == "end":
        x -= width
    return {"x": x, "y": float(cell.get("y") or 0) - size * 0.85, "w": width, "h": size * 1.2}


def _intersects(first: dict, second: dict) -> bool:
    return (
        first["x"] < second["x"] + second["w"]
        and second["x"] < first["x"] + first["w"]
        and first["y"] < second["y"] + second["h"]
        and second["y"] < first["y"] + first["h"]
    )


# ---------------------------------------------------------------------------
# 1. 模板生成与几何
# ---------------------------------------------------------------------------

def test_create_card_generates_anchors_bubbles_and_page_code(client):
    questions = [create_question(client) for _ in range(3)] + [
        create_question(client, qtype="SUBJECTIVE") for _ in range(2)
    ]
    paper = create_paper(client, questions)
    paper_id = paper["paper"]["id"]
    response = client.post(f"/api/paperlab/papers/{paper_id}/cards", json={
        "preset": "A4", "columns": 2, "bubble": "circle", "include_subjective": True,
        "student_no": {"digits": 6, "bubble": True},
    })
    assert response.status_code == 200, response.text
    body = response.json()
    card = body["card"]
    layout = body["layout"]
    assert card["paper_id"] == paper_id
    assert card["status"] == "ready"
    assert card["page_count"] == len(layout["pages"]) >= 1
    assert card["preset"] == "A4"
    assert card["columns"] == 2
    assert card["template_signature"]
    page = layout["pages"][0]
    # 四角定位点：恰好 4 个，名称与顺序固定，位于页边距内
    assert [anchor["corner"] for anchor in page["anchors"]] == [
        "top_left", "top_right", "bottom_left", "bottom_right",
    ]
    assert page["anchors"][0]["x"] == layout["paper"]["margin_mm"]
    assert page["anchors"][1]["x"] > page["anchors"][0]["x"]
    # 客观题气泡：4 选项 4 泡
    objective = objective_items(layout)
    assert len(objective) == 3
    assert all(len(item["bubbles"]) == 4 for item in objective)
    # 主观题书写区
    subjective = subjective_items(layout)
    assert len(subjective) == 2
    for item in subjective:
        assert item["write_area"] and item["write_area"]["lines"] >= 3
    # 学号区 6 位 × 10 泡
    student_area = page["header"]["student_no"]
    assert student_area["digits"] == 6 and student_area["bubble"] is True
    assert len(student_area["digit_rows"]) == 6
    assert all(len(row["bubbles"]) == 10 for row in student_area["digit_rows"])
    # 页面码 46 位：101 + 签名 + 页码 + 110
    code = page["footer"]["page_code"]["text"]
    assert len(code) == 46 and code.startswith("101") and code.endswith("110")
    assert code[3:35] == format(card["template_signature"], "032b")
    assert code[35:43] == format(1, "08b")
    # 可选二维码信息存在（qrcode 已装则由 SVG 渲染）
    assert page.get("qr") and page["qr"]["text"].startswith("zhixu://card/")
    # 几何校验可重复通过
    card_svg.validate_card_layout(layout)


def test_create_card_a3_landscape_and_4_columns(client):
    questions = [create_question(client) for _ in range(6)]
    paper = create_paper(client, questions, layout={"paper": "A3", "columns": 4})
    response = client.post(f"/api/paperlab/papers/{paper['paper']['id']}/cards", json={
        "preset": "A3", "columns": 4, "bubble": "oval",
        "student_no": {"digits": 8, "bubble": False},
    })
    assert response.status_code == 200, response.text
    layout = response.json()["layout"]
    assert layout["paper"]["width_mm"] == 420 and layout["paper"]["height_mm"] == 297
    assert layout["orientation"] == "landscape"
    assert layout["bubble"] == "oval"
    page = layout["pages"][0]
    assert len(page["header"]["student_no"]["digit_rows"]) == 8
    assert all(not row["bubbles"] for row in page["header"]["student_no"]["digit_rows"])
    assert all(row.get("line") for row in page["header"]["student_no"]["digit_rows"])
    card_svg.validate_card_layout(layout)


def test_header_content_avoids_corner_anchors(client):
    """页眉标题/学号区与四角定位点保持安全间距（避免标题首字被定位点覆盖）。"""
    questions = [create_question(client) for _ in range(2)]
    paper = create_paper(
        client, questions,
        title="很长的标题：函数与导数综合训练（第一轮复习）答题卡",
    )
    response = client.post(f"/api/paperlab/papers/{paper['paper']['id']}/cards", json={
        "preset": "A4", "columns": 2, "student_no": {"digits": 6, "bubble": True},
    })
    assert response.status_code == 200, response.text
    layout = response.json()["layout"]
    width = layout["paper"]["width_mm"]
    for page in layout["pages"]:
        anchors = page["anchors"]
        title = page["header"]["title"]
        assert title["anchor"] == "start"
        title_rect = _text_rect(title)
        student = page["header"]["student_no"]
        student_rect = {
            "x": float(student["x"]), "y": float(student["y"]),
            "w": float(student["w"]), "h": float(student["h"]),
        }
        for anchor in anchors:
            anchor_rect = {
                "x": float(anchor["x"]), "y": float(anchor["y"]),
                "w": float(anchor["w"]), "h": float(anchor["h"]),
            }
            assert not _intersects(title_rect, anchor_rect), "标题与定位点重叠"
            assert not _intersects(student_rect, anchor_rect), "学号区与定位点重叠"
        assert not _intersects(title_rect, student_rect), "标题与学号区重叠"
        assert title_rect["x"] >= 0 and title_rect["x"] + title_rect["w"] <= width + 0.5
    # 手工把标题挪进左上定位点：几何校验必须拒绝
    bad = json.loads(json.dumps(layout))
    bad["pages"][0]["header"]["title"]["x"] = bad["pages"][0]["anchors"][0]["x"]
    bad["pages"][0]["header"]["title"]["y"] = bad["pages"][0]["anchors"][0]["y"] + 4
    with pytest.raises(ValueError) as exc:
        card_svg.validate_card_layout(bad)
    assert "定位点" in str(exc.value)


def test_header_long_title_truncated_within_safe_area(client):
    """超长标题按可用宽度截断并加省略号，不越出页眉安全区、不与定位点/学号区重叠。"""
    long_title = (
        "函数与导数综合训练（第一轮复习）答题卡 · 数学 · 高中一年级 · "
        "请认真填写姓名班级学号并保持定位点完整 Function Derivative Midterm"
    )
    questions = [create_question(client) for _ in range(2)]
    paper = create_paper(client, questions, title=long_title)
    response = client.post(f"/api/paperlab/papers/{paper['paper']['id']}/cards", json={
        "preset": "A4", "columns": 2, "student_no": {"digits": 6, "bubble": True},
    })
    assert response.status_code == 200, response.text
    layout = response.json()["layout"]
    width = float(layout["paper"]["width_mm"])
    margin = float(layout["paper"]["margin_mm"])
    page = layout["pages"][0]
    title = page["header"]["title"]
    student = page["header"]["student_no"]
    assert title["text"].endswith("…"), "超长标题应截断并加省略号"
    assert len(title["text"]) < len(long_title)
    title_rect = _text_rect(title)
    assert title_rect["x"] >= page["anchors"][0]["x"] + page["anchors"][0]["w"]
    assert title_rect["x"] + title_rect["w"] <= float(student["x"]) - 2.0 + 0.5
    assert title_rect["x"] + title_rect["w"] <= width - margin - card_svg.ANCHOR_W_MM + 0.5
    for anchor in page["anchors"]:
        anchor_rect = {"x": float(anchor["x"]), "y": float(anchor["y"]),
                       "w": float(anchor["w"]), "h": float(anchor["h"])}
        assert not _intersects(title_rect, anchor_rect)
    student_rect = {"x": float(student["x"]), "y": float(student["y"]),
                    "w": float(student["w"]), "h": float(student["h"])}
    assert not _intersects(title_rect, student_rect)
    card_svg.validate_card_layout(layout)
    # SVG 里使用的也是截断后的标题（不会把超长原文画到页眉外）
    svg = client.get(f"/api/paperlab/cards/{response.json()['card']['id']}/svg?page=1").text
    assert "…" in svg
    # 8 位学号（更窄的标题区）同样安全
    eight = client.post(f"/api/paperlab/papers/{paper['paper']['id']}/cards", json={
        "preset": "A4", "columns": 2, "student_no": {"digits": 8, "bubble": True},
    })
    assert eight.status_code == 200, eight.text
    eight_layout = eight.json()["layout"]
    eight_page = eight_layout["pages"][0]
    eight_title = eight_page["header"]["title"]
    eight_rect = _text_rect(eight_title)
    eight_student = eight_page["header"]["student_no"]
    assert eight_rect["x"] + eight_rect["w"] <= float(eight_student["x"]) - 2.0 + 0.5
    for anchor in eight_page["anchors"]:
        assert not _intersects(eight_rect, {
            "x": float(anchor["x"]), "y": float(anchor["y"]),
            "w": float(anchor["w"]), "h": float(anchor["h"]),
        })


def test_create_card_include_subjective_false_and_empty_error(client):
    questions = [create_question(client) for _ in range(2)]
    paper = create_paper(client, questions)
    response = client.post(f"/api/paperlab/papers/{paper['paper']['id']}/cards", json={
        "preset": "A4", "columns": 1, "include_subjective": False,
        "student_no": {"digits": 6, "bubble": True},
    })
    assert response.status_code == 200, response.text
    layout = response.json()["layout"]
    assert subjective_items(layout) == []
    assert len(objective_items(layout)) == 2

    only_subjective = [create_question(client, qtype="SUBJECTIVE")]
    subject_paper = create_paper(client, only_subjective)
    empty = client.post(f"/api/paperlab/papers/{subject_paper['paper']['id']}/cards", json={
        "preset": "A4", "columns": 1, "include_subjective": False,
    })
    assert empty.status_code == 422
    assert "答题卡" in empty.json()["detail"]


def test_validate_card_layout_rejects_bad_geometry(client):
    questions = [create_question(client) for _ in range(2)]
    paper = create_paper(client, questions)
    created = client.post(f"/api/paperlab/papers/{paper['paper']['id']}/cards", json={
        "preset": "A4", "columns": 2,
    }).json()
    layout = created["layout"]
    card = created["card"]

    bad = json.loads(json.dumps(layout))
    bad["pages"][0]["anchors"][0]["x"] = 0
    with pytest.raises(ValueError) as exc:
        card_svg.validate_card_layout(bad)
    assert "定位点" in str(exc.value)

    overlap = json.loads(json.dumps(layout))
    first = objective_items(overlap)[0]
    first["bubbles"][1]["x"] = first["bubbles"][0]["x"]
    first["bubbles"][1]["y"] = first["bubbles"][0]["y"]
    with pytest.raises(ValueError) as exc2:
        card_svg.validate_card_layout(overlap)
    assert "气泡重叠" in str(exc2.value)

    out_of_bounds = json.loads(json.dumps(layout))
    out_of_bounds["pages"][0]["groups"][0]["questions"][0]["x"] = -30
    with pytest.raises(ValueError) as exc3:
        card_svg.validate_card_layout(out_of_bounds)
    assert "边界" in str(exc3.value)

    bad_code = json.loads(json.dumps(layout))
    bad_code["pages"][0]["footer"]["page_code"]["text"] = "101"
    with pytest.raises(ValueError) as exc4:
        card_svg.validate_card_layout(bad_code)
    assert "页面码" in str(exc4.value)

    # API：PATCH 非法 layout → 422
    response = client.patch(f"/api/paperlab/cards/{card['id']}", json={
        "version": card["version"], "layout": bad,
    })
    assert response.status_code == 422
    assert "定位点" in response.json()["detail"]


# ---------------------------------------------------------------------------
# 2. CAS / stale / 归属
# ---------------------------------------------------------------------------

def test_card_version_cas_and_stale_marking(client):
    questions = [create_question(client) for _ in range(3)]
    paper = create_paper(client, questions)
    paper_id = paper["paper"]["id"]
    created = client.post(f"/api/paperlab/papers/{paper_id}/cards", json={
        "preset": "A4", "columns": 2,
    }).json()
    card = created["card"]
    stale_patch = client.patch(f"/api/paperlab/cards/{card['id']}", json={
        "version": card["version"] + 1, "title": "冲突标题",
    })
    assert stale_patch.status_code == 409
    renamed = client.patch(f"/api/paperlab/cards/{card['id']}", json={
        "version": card["version"], "title": "函数练习答题卡（改）",
    })
    assert renamed.status_code == 200
    assert renamed.json()["card"]["title"] == "函数练习答题卡（改）"
    assert renamed.json()["card"]["version"] == card["version"] + 1
    # 合法 layout 微调：服务端按新模板签名重算页面码后通过几何校验
    edited_layout = json.loads(json.dumps(created["layout"]))
    edited_layout["pages"][0]["footer"]["notice"]["text"] = "请用 2B 铅笔填涂，禁止折叠。"
    edited = client.patch(f"/api/paperlab/cards/{card['id']}", json={
        "version": renamed.json()["card"]["version"], "layout": edited_layout,
    })
    assert edited.status_code == 200, edited.text
    assert edited.json()["card"]["version"] == card["version"] + 2
    new_code = edited.json()["layout"]["pages"][0]["footer"]["page_code"]["text"]
    assert new_code == card_svg.page_code(edited.json()["card"]["template_signature"], 1)

    # 试卷结构变化（整卷 items 替换）→ 旧卡 stale，且不可再编辑
    paper_version = client.get(f"/api/paperlab/papers/{paper_id}").json()["paper"]["version"]
    changed = client.patch(f"/api/paperlab/papers/{paper_id}", json={
        "version": paper_version,
        "items": [{"ref": f"app:{questions[0]['id']}", "score": 5}],
    })
    assert changed.status_code == 200
    card_now = client.get(f"/api/paperlab/cards/{card['id']}").json()["card"]
    assert card_now["status"] == "stale"
    listed = client.get(f"/api/paperlab/papers/{paper_id}/cards").json()
    assert listed["items"][0]["status"] == "stale"
    blocked = client.patch(f"/api/paperlab/cards/{card['id']}", json={
        "version": card_now["version"], "title": "再改",
    })
    assert blocked.status_code == 409
    assert "失效" in blocked.json()["detail"]
    # stale 卡仍可导出历史 SVG（带提示），便于核对
    svg = client.get(f"/api/paperlab/cards/{card['id']}/svg?page=1")
    assert svg.status_code == 200
    assert svg.headers.get("x-card-stale") == "1"

    # 重新生成新卡
    regenerated = client.post(f"/api/paperlab/papers/{paper_id}/cards", json={"preset": "A4"})
    assert regenerated.status_code == 200
    assert regenerated.json()["card"]["status"] == "ready"


def test_patch_card_options_regenerates_layout_in_place(client):
    """options 局部修改：基于试卷快照重新生成几何，保留 card id，签名/页面码重算。"""
    questions = [create_question(client) for _ in range(6)]
    paper = create_paper(client, questions)
    paper_id = paper["paper"]["id"]
    created = client.post(f"/api/paperlab/papers/{paper_id}/cards", json={
        "preset": "A4", "columns": 2, "student_no": {"digits": 6, "bubble": True},
    }).json()
    card = created["card"]
    original_sig = card["template_signature"]
    assert card["preset"] == "A4" and card["columns"] == 2

    # A4 → A3：默认方向随纸张变化为横向，页面尺寸/页数按 A3 重新计算
    a3 = client.patch(f"/api/paperlab/cards/{card['id']}", json={
        "version": card["version"], "options": {"preset": "A3"},
    })
    assert a3.status_code == 200, a3.text
    a3_card = a3.json()["card"]
    a3_layout = a3.json()["layout"]
    assert a3_card["id"] == card["id"]
    assert a3_card["preset"] == "A3" and a3_card["orientation"] == "landscape"
    assert a3_layout["paper"]["width_mm"] == 420 and a3_layout["paper"]["height_mm"] == 297
    assert a3_card["version"] == card["version"] + 1
    assert a3_card["template_signature"] != original_sig
    assert a3_layout["pages"][0]["footer"]["page_code"]["text"] == \
        card_svg.page_code(a3_card["template_signature"], 1)

    # 分栏 2 → 4：重排后页数不增加，且每题仍只出现一次
    before_count = a3_card["page_count"]
    a3_4col = client.patch(f"/api/paperlab/cards/{card['id']}", json={
        "version": a3_card["version"], "options": {"columns": 4},
    })
    assert a3_4col.status_code == 200, a3_4col.text
    four_card = a3_4col.json()["card"]
    four_layout = a3_4col.json()["layout"]
    assert four_card["columns"] == 4 and four_card["preset"] == "A3"
    assert four_card["page_count"] == len(four_layout["pages"]) <= before_count
    numbers = [
        question["no"] for page in four_layout["pages"]
        for group in page["groups"] for question in group["questions"]
    ]
    assert sorted(numbers) == list(range(1, 7))

    # 学号 6 → 8 位且改为手写线：布局与签名同步更新
    eight = client.patch(f"/api/paperlab/cards/{card['id']}", json={
        "version": four_card["version"],
        "options": {"student_no": {"digits": 8, "bubble": False}},
    })
    assert eight.status_code == 200, eight.text
    eight_card = eight.json()["card"]
    eight_layout = eight.json()["layout"]
    student = eight_layout["pages"][0]["header"]["student_no"]
    assert student["digits"] == 8 and student["bubble"] is False
    assert len(student["digit_rows"]) == 8
    assert all(row.get("line") and not row["bubbles"] for row in student["digit_rows"])
    assert eight_card["template_signature"] != four_card["template_signature"]
    # 标题仍保留（options 不带 title 时沿用原卡标题）
    assert eight_layout["template"]["title"] == card["title"]

    # stale 卡：options 修改同样 409
    paper_version = client.get(f"/api/paperlab/papers/{paper_id}").json()["paper"]["version"]
    changed = client.patch(f"/api/paperlab/papers/{paper_id}", json={
        "version": paper_version,
        "items": [{"ref": f"app:{questions[0]['id']}", "score": 5}],
    })
    assert changed.status_code == 200
    current = client.get(f"/api/paperlab/cards/{card['id']}").json()["card"]
    assert current["status"] == "stale"
    blocked = client.patch(f"/api/paperlab/cards/{card['id']}", json={
        "version": current["version"], "options": {"preset": "A4"},
    })
    assert blocked.status_code == 409
    assert "失效" in blocked.json()["detail"]


def test_patch_card_options_validation_and_cas(client):
    questions = [create_question(client) for _ in range(2)]
    paper = create_paper(client, questions)
    created = client.post(f"/api/paperlab/papers/{paper['paper']['id']}/cards", json={
        "preset": "A4", "columns": 2,
    }).json()
    card = created["card"]
    # 版本冲突 409
    assert client.patch(f"/api/paperlab/cards/{card['id']}", json={
        "version": card["version"] + 1, "options": {"columns": 1},
    }).status_code == 409
    # 非法参数 422
    assert client.patch(f"/api/paperlab/cards/{card['id']}", json={
        "version": card["version"], "options": {"columns": 5},
    }).status_code == 422
    invalid_preset = client.patch(f"/api/paperlab/cards/{card['id']}", json={
        "version": card["version"], "options": {"preset": "B5"},
    })
    assert invalid_preset.status_code == 422
    assert "A4" in invalid_preset.json()["detail"] or "A3" in invalid_preset.json()["detail"]
    # layout 与 options 互斥
    both = client.patch(f"/api/paperlab/cards/{card['id']}", json={
        "version": card["version"], "layout": created["layout"], "options": {"columns": 1},
    })
    assert both.status_code == 422 and "二选一" in both.json()["detail"]
    # 空 options 等价“无实质修改”仍会重建并升版本（允许），但 CAS 使用最新版本
    latest = client.get(f"/api/paperlab/cards/{card['id']}").json()["card"]
    ok = client.patch(f"/api/paperlab/cards/{card['id']}", json={
        "version": latest["version"], "options": {"columns": 1},
    })
    assert ok.status_code == 200
    assert ok.json()["card"]["columns"] == 1


def test_card_ownership_and_unauthenticated(client, second_client):
    questions = [create_question(client)]
    paper = create_paper(client, questions)
    card = client.post(f"/api/paperlab/papers/{paper['paper']['id']}/cards", json={}).json()["card"]
    assert second_client.get(f"/api/paperlab/cards/{card['id']}").status_code == 404
    assert second_client.get(f"/api/paperlab/cards/{card['id']}/svg?page=1").status_code == 404
    assert second_client.get(f"/api/paperlab/cards/{card['id']}/export?format=html").status_code == 404
    assert second_client.patch(f"/api/paperlab/cards/{card['id']}",
                               json={"version": 1, "title": "x"}).status_code == 404
    assert second_client.delete(f"/api/paperlab/cards/{card['id']}").status_code == 404
    anonymous = TestClient(app)
    assert anonymous.get("/api/paperlab/cards/1").status_code == 401
    assert anonymous.post(f"/api/paperlab/papers/{paper['paper']['id']}/cards", json={}).status_code == 401

    deleted = client.delete(f"/api/paperlab/cards/{card['id']}")
    assert deleted.status_code == 200
    assert client.get(f"/api/paperlab/cards/{card['id']}").status_code == 404


# ---------------------------------------------------------------------------
# 3. SVG / HTML / 打印导出
# ---------------------------------------------------------------------------

def test_card_svg_structure_and_print_colors(client):
    questions = [create_question(client) for _ in range(2)] + [
        create_question(client, qtype="SUBJECTIVE")
    ]
    paper = create_paper(client, questions)
    card = client.post(f"/api/paperlab/papers/{paper['paper']['id']}/cards", json={
        "preset": "A4", "columns": 2, "student_no": {"digits": 6, "bubble": True},
    }).json()
    card_id = card["card"]["id"]
    layout = card["layout"]
    response = client.get(f"/api/paperlab/cards/{card_id}/svg?page=1&color=black")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/svg+xml")
    assert response.headers.get("x-card-page-count") == str(card["card"]["page_count"])
    svg = response.text
    assert svg.startswith("<svg") and 'data-card-page="1"' in svg
    assert "viewBox=" in svg
    page = layout["pages"][0]
    # 四角定位点（黑色填充矩形）
    assert svg.count('fill="#111111"') >= 1
    for anchor in page["anchors"]:
        assert f'x="{round(anchor["x"] * card_svg.MM_TO_PX, 3)}"' in svg
    # 气泡数量 = 学号 6×10 + 客观题选项数
    expected_bubbles = 60 + sum(len(item["bubbles"]) for item in objective_items(layout))
    assert svg.count('class="card-bubble"') == expected_bubbles
    # 主观题书写线区域
    assert 'class="card-write-area"' in svg
    # 页面码与页码
    assert page["footer"]["page_code"]["text"] in svg
    assert f'第 1 / {card["card"]["page_count"]} 页' in svg
    # 红色印刷版替换描边色，定位点/页面码仍为黑色
    red = client.get(f"/api/paperlab/cards/{card_id}/svg?page=1&color=red")
    assert red.status_code == 200
    assert "#c41e1e" in red.text and red.text != svg
    assert 'fill="#111111"' in red.text
    assert client.get(f"/api/paperlab/cards/{card_id}/svg?page=99").status_code == 404
    assert client.get(f"/api/paperlab/cards/{card_id}/svg?page=1&color=blue").status_code == 422


def test_card_export_html_self_contained_and_multi_page(client):
    questions = [create_question(client, qtype="SUBJECTIVE") for _ in range(6)]
    paper = create_paper(client, questions)
    created = client.post(f"/api/paperlab/papers/{paper['paper']['id']}/cards", json={
        "preset": "A4", "columns": 1,
    }).json()
    card = created["card"]
    assert card["page_count"] > 1, "6 道大题应自动分页"
    html = client.get(f"/api/paperlab/cards/{card['id']}/export?format=html&page=all")
    assert html.status_code == 200
    assert html.headers["content-type"].startswith("text/html")
    assert html.headers.get("x-frame-options") == "SAMEORIGIN"
    assert html.headers.get("x-card-page-count") == str(card["page_count"])
    body = html.text
    assert body.count("<svg") == card["page_count"]
    assert "@page" in body and "page-break-after" in body
    assert "card-page" in body
    assert client.get(f"/api/paperlab/cards/{card['id']}/export?format=html&page=1").status_code == 200
    # pdf：服务端浏览器渲染；不可用时 503 提示改 html 打印。
    pdf = client.get(f"/api/paperlab/cards/{card['id']}/export?format=pdf")
    assert pdf.status_code in (200, 503)
    if pdf.status_code == 200:
        assert pdf.headers["content-type"].startswith("application/pdf")
        assert pdf.content.startswith(b"%PDF")
    assert client.get(f"/api/paperlab/cards/{card['id']}/export?format=xyz").status_code == 422
    svg_page = client.get(f"/api/paperlab/cards/{card['id']}/export?format=svg&page=2")
    assert svg_page.status_code == 200 and svg_page.text.startswith("<svg")
    all_svg = client.get(f"/api/paperlab/cards/{card['id']}/export?format=svg&page=all")
    assert all_svg.status_code == 422


# ---------------------------------------------------------------------------
# 4. 试卷打印分栏（html / docx）
# ---------------------------------------------------------------------------

def test_paper_export_html_columns_and_auto_pagination(client):
    questions = [create_question(client) for _ in range(3)] + [
        create_question(client, qtype="SUBJECTIVE") for _ in range(10)
    ]
    paper = create_paper(client, questions, layout={"paper": "A4", "columns": 1, "answer_space_mm": 30})
    paper_id = paper["paper"]["id"]
    response = client.get(f"/api/paperlab/papers/{paper_id}/export?format=html&columns=2&answers=0")
    assert response.status_code == 200
    assert response.headers.get("x-paper-columns") == "2"
    page_count = int(response.headers.get("x-page-count") or "0")
    assert page_count >= 2, "多道主观题应自动分页"
    body = response.text
    assert f'<meta name="page-count" content="{page_count}">' in body
    assert "column-count: 2" in body
    assert "sheet-columns" in body
    assert body.count('class="sheet"') == page_count
    assert "sheet-head" in body and "sheet-foot" in body  # 页眉页脚
    assert "@page" in body and "break-inside: avoid" in body
    assert "参考答案与解析" not in body
    # columns=4 与 columns=1 都支持
    assert "column-count: 4" in client.get(
        f"/api/paperlab/papers/{paper_id}/export?format=html&columns=4").text
    assert client.get(
        f"/api/paperlab/papers/{paper_id}/export?format=html&columns=5").status_code == 422
    # 已发布卷同样支持分栏打印导出
    published = client.post(f"/api/paperlab/papers/{paper_id}/publish", json={"published": True})
    assert published.status_code == 200
    assert client.get(
        f"/api/paperlab/papers/{paper_id}/export?format=html&columns=2&answers=0").status_code == 200


def test_paper_export_docx_columns_and_page_setup(client):
    questions = [create_question(client) for _ in range(4)]
    paper = create_paper(client, questions, layout={"paper": "A4", "columns": 1})
    paper_id = paper["paper"]["id"]
    response = client.get(f"/api/paperlab/papers/{paper_id}/export?format=docx&columns=3&answers=0")
    assert response.status_code == 200
    assert response.headers.get("x-paper-columns") == "3"
    document = Document(io.BytesIO(response.content))
    assert document.sections
    section = document.sections[0]
    # A4 纵向 210×297mm
    assert abs(section.page_width.mm - 210) < 0.5
    assert abs(section.page_height.mm - 297) < 0.5
    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    columns = section._sectPr.find(ns + "cols")
    assert columns is not None
    assert columns.get(ns + "num") == "3"
    # 含答案导出有独立参考答案 section
    with_answers = client.get(f"/api/paperlab/papers/{paper_id}/export?format=docx&columns=2&answers=1")
    document_answers = Document(io.BytesIO(with_answers.content))
    assert len(document_answers.sections) >= 2
    answer_columns = document_answers.sections[-1]._sectPr.find(ns + "cols")
    assert answer_columns is not None
    assert answer_columns.get(ns + "num") == "1"
    text = "\n".join(paragraph.text for paragraph in document_answers.paragraphs)
    assert "参考答案与解析" in text

    # A3 横向页面设置
    a3 = create_paper(client, questions, layout={"paper": "A3", "columns": 2})
    a3_docx = client.get(f"/api/paperlab/papers/{a3['paper']['id']}/export?format=docx&columns=2")
    document_a3 = Document(io.BytesIO(a3_docx.content))
    assert abs(document_a3.sections[0].page_width.mm - 420) < 0.5
    assert abs(document_a3.sections[0].page_height.mm - 297) < 0.5
