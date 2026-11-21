"""试卷工坊 P9：答题卡扫描识别（OMR）测试。

覆盖：合成答题卡图回环（多页/随机填涂/6-8 位学号/页面码）、倾斜 3°/5° 与亮度变化鲁棒性、
页面码不符/缺定位点/分辨率过低的中文原因（不伪造读数）、气泡冲突标 unreadable、
上传上限/空文件/格式/归属/未登录、confirm 人工纠错判分 + 生成练习记录（错题联动）、
缺页/重复页/重复确认 409、阈值与 meta。

合成图用 ``card_svg`` 生成的真实 SVG（页面码/定位点/气泡几何与打印一致）经
``app/ai/render.py::screenshot_png`` 渲染为 PNG，再按 layout 坐标叠加填涂圆点；
浏览器不可用时 skip（与 ``tests/test_ai_render.py`` 同口径）。
"""
import asyncio
import io
import json
import random
import struct
import zlib

import cv2
import numpy as np
from PIL import Image, ImageEnhance
import pytest
from fastapi.testclient import TestClient

from app.ai import render as ai_render
from app.main import app
from app.paperlab import card_svg, scan

MM = card_svg.MM_TO_PX


# ---------------------------------------------------------------------------
# 合成答题卡图像
# ---------------------------------------------------------------------------

_RENDER_CACHE: dict[str, bytes] = {}


def _render_available() -> bool:
    return bool(ai_render.render_available()["available"])


def _require_render() -> None:
    info = ai_render.render_available()
    if not info["available"]:
        pytest.skip(f"本机无可用浏览器，跳过合成图渲染：{info['detail']}")


def _filled_svg(layout: dict, page_no: int, fills: dict[int, list[str]], student: str | None) -> str:
    svg = card_svg.render_card_svg(layout, page_no=page_no)
    page = next(item for item in layout["pages"] if int(item["page_no"]) == int(page_no))
    marks: list[tuple[float, float, float]] = []
    for group in page["groups"]:
        for question in group["questions"]:
            chosen = fills.get(int(question["no"])) or []
            for bubble in question["bubbles"]:
                if bubble["label"] in chosen:
                    marks.append((bubble["x"], bubble["y"], bubble["r"]))
    student_area = page["header"]["student_no"]
    if student and student_area.get("bubble"):
        for row in student_area["digit_rows"]:
            digit = int(student[int(row["position"]) - 1])
            for bubble in row["bubbles"]:
                if int(bubble["value"]) == digit:
                    marks.append((bubble["x"], bubble["y"], bubble["r"]))
    overlay = "<g fill='#111111'>" + "".join(
        f"<circle cx='{card_svg._px(x)}' cy='{card_svg._px(y)}' r='{card_svg._px(r)}'/>"
        for x, y, r in marks) + "</g>"
    return svg.replace("</svg>", overlay + "</svg>")


def _svg_html(layout: dict, svg: str, scale: float = 1.0) -> str:
    paper = layout.get("paper") or {}
    width = round(float(paper.get("width_mm") or 210) * MM * scale, 1)
    return (
        "<!doctype html><html><head><meta charset='utf-8'><style>"
        "html,body{margin:0;padding:0;background:#fff;}"
        f".sheet{{width:{width}px;}} svg{{display:block;width:100%;height:auto;}}"
        "</style></head><body><div class='sheet'>" + svg + "</div></body></html>"
    )


def render_page_png(layout: dict, page_no: int, fills: dict[int, list[str]],
                    student: str | None = None) -> bytes:
    """渲染一页「已填涂」答题卡 PNG（缓存同一页/同一填涂，避免重复启动浏览器）。"""
    _require_render()
    key = json.dumps([layout.get("template", {}).get("signature"), page_no,
                      sorted((int(no), list(labels)) for no, labels in fills.items()),
                      student], ensure_ascii=False, sort_keys=True)
    cached = _RENDER_CACHE.get(key)
    if cached:
        return cached
    paper = layout.get("paper") or {}
    svg = _filled_svg(layout, page_no, fills, student)
    html = _svg_html(layout, svg, 1.0)
    width = max(320, int(round(float(paper.get("width_mm") or 210) * MM)))
    png = asyncio.run(ai_render.screenshot_png(html, width=width, height=1123, full_page=True))
    _RENDER_CACHE[key] = png
    return png


def tilt(png: bytes, angle: float) -> bytes:
    image = Image.open(io.BytesIO(png)).convert("RGB")
    image = image.rotate(angle, expand=True, fillcolor=(255, 255, 255), resample=Image.BICUBIC)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def brightness(png: bytes, factor: float) -> bytes:
    image = Image.open(io.BytesIO(png)).convert("RGB")
    image = ImageEnhance.Brightness(image).enhance(factor)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def resize(png: bytes, width: int, height: int) -> bytes:
    image = Image.open(io.BytesIO(png)).convert("RGB").resize((width, height), Image.BILINEAR)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def erase_anchor(png: bytes, layout: dict, page_no: int, corner: str) -> bytes:
    image = np.array(Image.open(io.BytesIO(png)).convert("RGB"))
    page = next(item for item in layout["pages"] if int(item["page_no"]) == int(page_no))
    anchor = next(item for item in page["anchors"] if item["corner"] == corner)
    pad = 2.5
    x0 = int((float(anchor["x"]) - pad) * MM)
    y0 = int((float(anchor["y"]) - pad) * MM)
    x1 = int((float(anchor["x"]) + float(anchor["w"]) + pad) * MM)
    y1 = int((float(anchor["y"]) + float(anchor["h"]) + pad) * MM)
    image[max(0, y0):y1, max(0, x0):x1] = 255
    buffer = io.BytesIO()
    Image.fromarray(image).save(buffer, format="PNG")
    return buffer.getvalue()


def random_fills(layout: dict, seed: int = 42, overrides: dict[int, list[str]] | None = None) -> dict[int, list[str]]:
    rng = random.Random(seed)
    fills: dict[int, list[str]] = {}
    for page in layout["pages"]:
        for group in page["groups"]:
            for question in group["questions"]:
                labels = [bubble["label"] for bubble in question["bubbles"]]
                if not labels:
                    continue
                if question["type"] == "MULTIPLE":
                    chosen = rng.sample(labels, rng.choice([1, 2, 3]))
                    chosen.sort(key=labels.index)
                else:
                    chosen = [rng.choice(labels)]
                fills[int(question["no"])] = chosen
    if overrides:
        fills.update(overrides)
    return fills


# ---------------------------------------------------------------------------
# 试卷 / 答题卡 / 上传辅助
# ---------------------------------------------------------------------------

def create_question(client, *, qtype="SINGLE", options="ABCD", answers=None, score_note=None) -> dict:
    labels = list(options) if options else []
    if qtype in ("SINGLE", "MULTIPLE"):
        content = {
            "stem": f"第 {len(labels)} 项选择题干（测试）",
            "options": [{"label": label, "text": f"选项 {label}"} for label in labels],
            "answers": answers or (["A"] if qtype == "SINGLE" else ["A", "C"]),
            "explanation": "测试解析",
        }
    else:
        content = {
            "stem": "证明：函数在给定区间上单调递增（测试主观题）。",
            "answers": ["证明过程略。"],
            "explanation": "测试解析",
            "rubric": "结论 2 分。",
        }
    response = client.post("/api/paperlab/questions", json={
        "type": qtype, "subject": "数学", "grade": "高中一年级", "difficulty": 0.3,
        "knowledge_tags": ["函数"], "content": content, "status": "ready",
    })
    assert response.status_code == 200, response.text
    return response.json()["question"]


def create_paper(client, questions, *, title="函数综合练习", scores=None) -> dict:
    items = []
    for index, question in enumerate(questions):
        items.append({
            "ref": f"app:{question['id']}",
            "score": (scores or {}).get(index + 1, 10 if question["type"] == "SUBJECTIVE" else 5),
            "section": "二、解答题" if question["type"] == "SUBJECTIVE" else "一、选择题",
        })
    response = client.post("/api/paperlab/papers", json={
        "title": title, "kind": "manual", "subject": "数学", "grade": "高中一年级",
        "layout": {"paper": "A4", "columns": 2}, "items": items,
    })
    assert response.status_code == 200, response.text
    return response.json()


def create_card(client, paper_id: int, **options) -> dict:
    payload = {"preset": "A4", "columns": 2, "bubble": "circle", "include_subjective": True,
               "student_no": {"digits": 6, "bubble": True}}
    payload.update(options)
    response = client.post(f"/api/paperlab/papers/{paper_id}/cards", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def scan_pages(client, paper_id: int, images: list[bytes], *, card_id: int | None = None,
               threshold: float | None = None) -> dict:
    data: dict[str, str] = {}
    if card_id is not None:
        data["card_id"] = str(card_id)
    if threshold is not None:
        data["threshold"] = str(threshold)
    files = [("files", (f"page-{index + 1}.png", raw, "image/png"))
             for index, raw in enumerate(images)]
    return client.post(f"/api/paperlab/papers/{paper_id}/scan", files=files, data=data)


def objective_layout_questions(layout: dict) -> list[dict]:
    return [
        question for page in layout["pages"] for group in page["groups"]
        for question in group["questions"] if question["bubbles"]
    ]


def standard_card(client, *, single=8, multiple=2, subjective=1, digits=6, title="函数综合练习",
                  include_subjective=True):
    questions = [create_question(client, qtype="SINGLE") for _ in range(single)]
    questions += [create_question(client, qtype="MULTIPLE") for _ in range(multiple)]
    questions += [create_question(client, qtype="SUBJECTIVE") for _ in range(subjective)]
    paper = create_paper(client, questions, title=title)
    created = create_card(client, paper["paper"]["id"], student_no={"digits": digits, "bubble": True},
                          include_subjective=include_subjective)
    return {
        "questions": questions, "paper": paper, "card": created["card"], "layout": created["layout"],
        "paper_id": paper["paper"]["id"],
    }


def expected_student(layout: dict, digits: str) -> str:
    return digits[: int(layout["pages"][0]["header"]["student_no"]["digits"])]


def assert_answers_match(result: dict, layout: dict, fills: dict[int, list[str]]) -> None:
    got = {int(question["layout_no"]): sorted(question["selected"]) for question in result["questions"]}
    for layout_no, labels in fills.items():
        assert got.get(int(layout_no)) == sorted(labels), f"第 {layout_no} 题读数不一致"


# ---------------------------------------------------------------------------
# 1. 合成图回环：随机填涂 / 学号 / 页面码 / 判分
# ---------------------------------------------------------------------------

def test_scan_roundtrip_random_answers_and_student_no(client):
    env = standard_card(client, single=8, multiple=2, subjective=1, digits=6)
    layout = env["layout"]
    assert _render_available() or pytest.skip("无浏览器")
    for seed in (1, 2, 7):
        fills = random_fills(layout, seed=seed)
        student = "314159"
        png = render_page_png(layout, 1, fills, student)
        response = scan_pages(client, env["paper_id"], [png], card_id=env["card"]["id"])
        assert response.status_code == 200, response.text
        body = response.json()
        result = body["result"]
        page = result["pages"][0]
        assert page["status"] == "ok", page["reason"]
        assert page["code_ok"] is True
        assert page["page_no"] == 1
        assert page["expected_code"] == page["code"]
        assert len(page["anchors"]) == 4 and len(page["homography"]) == 8
        assert result["student_no"]["value"] == expected_student(layout, student)
        assert_answers_match(result, layout, fills)
        assert result["unreadable_items"] == []
        # 判分：与快照参考答案逐题比对后重算，得分 = 正确题分值和。
        expected_score = round(sum(
            question["score"] for question in result["questions"]
            if sorted(question["selected"]) == sorted(question["reference_answers"])
        ), 2)
        assert result["score"] == expected_score
        assert result["max_score"] == round(sum(
            question["score"] for question in result["questions"]), 2)
        assert body["scan"]["status"] == "recognized"
        assert body["scan"]["page_count"] == 1
        # 逐页 canvas 叠加数据：选中气泡/页面码中心都映射回原图坐标内。
        for bubble in page["bubbles"]:
            assert 0 <= bubble["img_x"] <= page["image_width"]
            assert 0 <= bubble["img_y"] <= page["image_height"]
        code_marker = page["markers"]["page_code"]
        assert 0 <= code_marker["x"] <= page["image_width"]
        assert 0 <= code_marker["y"] <= page["image_height"]
        assert result["score"] <= result["max_score"]


def test_scan_8_digit_student_no_and_history(client):
    env = standard_card(client, single=4, multiple=1, subjective=0, digits=8)
    layout = env["layout"]
    fills = random_fills(layout, seed=5)
    student = "87654321"
    png = render_page_png(layout, 1, fills, student)
    response = scan_pages(client, env["paper_id"], [png], card_id=env["card"]["id"])
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert result["student_no"]["value"] == "87654321"
    assert result["student_no"]["digits"] == 8
    assert_answers_match(result, layout, fills)
    marks = result["pages"][0]["student_no"]["marks"]
    assert marks and all(mark["selected"] for mark in marks)
    # GET 单条 + 列表
    scan_id = response.json()["scan"]["id"]
    one = client.get(f"/api/paperlab/scans/{scan_id}")
    assert one.status_code == 200
    assert one.json()["result"]["student_no"]["value"] == "87654321"
    listed = client.get(f"/api/paperlab/papers/{env['paper_id']}/scans")
    assert listed.status_code == 200
    items = listed.json()["items"]
    assert [item["id"] for item in items] == [scan_id]
    assert items[0]["summary"]["page_count"] == 1
    assert items[0]["student_no"] == "87654321"


def test_scan_multi_page_reverse_order(client):
    questions = [create_question(client, qtype="SINGLE") for _ in range(20)]
    questions += [create_question(client, qtype="MULTIPLE") for _ in range(3)]
    questions += [create_question(client, qtype="SUBJECTIVE") for _ in range(6)]
    paper = create_paper(client, questions, title="多页综合练习")
    created = create_card(client, paper["paper"]["id"], columns=2,
                          student_no={"digits": 8, "bubble": True})
    layout = created["layout"]
    assert len(layout["pages"]) >= 2, "题目较多时应自动分成多页答题卡"
    fills = random_fills(layout, seed=11)
    student = "12345678"
    images = [render_page_png(layout, page["page_no"], fills, student) for page in layout["pages"]]
    # 逆序上传：页面码决定版面，不依赖上传顺序。
    response = scan_pages(client, paper["paper"]["id"], list(reversed(images)),
                          card_id=created["card"]["id"])
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert all(page["status"] == "ok" for page in result["pages"]), [
        (page["index"], page["reason"]) for page in result["pages"]]
    assert [page["page_no"] for page in result["pages"]] == list(
        reversed([page["page_no"] for page in layout["pages"]]))
    assert result["missing_pages"] == [] and result["duplicate_pages"] == []
    assert_answers_match(result, layout, fills)
    assert result["student_no"]["value"] == student
    confirm = client.post(f"/api/paperlab/scans/{response.json()['scan']['id']}/confirm", json={})
    assert confirm.status_code == 200, confirm.text
    assert confirm.json()["scan"]["status"] == "confirmed"


# ---------------------------------------------------------------------------
# 2. 倾斜 / 亮度鲁棒性
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("angle,factor", [(3.0, 1.0), (5.0, 1.0), (5.0, 0.75), (3.0, 1.2)])
def test_scan_tilt_and_brightness(client, angle, factor):
    env = standard_card(client, single=6, multiple=2, subjective=1, digits=6)
    layout = env["layout"]
    fills = random_fills(layout, seed=21)
    student = "271828"
    png = render_page_png(layout, 1, fills, student)
    variant = tilt(brightness(png, factor), angle) if angle else brightness(png, factor)
    response = scan_pages(client, env["paper_id"], [variant], card_id=env["card"]["id"])
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    page = result["pages"][0]
    assert page["status"] == "ok", page["reason"]
    assert page["code_ok"] is True
    assert_answers_match(result, layout, fills)
    assert result["student_no"]["value"] == expected_student(layout, student)


# ---------------------------------------------------------------------------
# 3. 识别失败：页面码不符 / 缺定位点 / 分辨率过低 / 空文件 / 格式
# ---------------------------------------------------------------------------

def test_scan_page_code_mismatch_marks_unreadable(client):
    env_a = standard_card(client, single=5, multiple=1, subjective=0, title="答题卡 A")
    env_b = standard_card(client, single=5, multiple=1, subjective=0, title="答题卡 B")
    layout_b = env_b["layout"]
    fills_b = random_fills(layout_b, seed=31)
    png_b = render_page_png(layout_b, 1, fills_b, "111111")
    response = scan_pages(client, env_a["paper_id"], [png_b], card_id=env_a["card"]["id"])
    assert response.status_code == 200, response.text
    body = response.json()
    page = body["result"]["pages"][0]
    assert page["status"] == "unreadable"
    assert page["code_ok"] is False
    assert "页面码" in page["reason"]
    assert body["result"]["answers"] == {}
    assert body["result"]["score"] == 0
    assert body["result"]["unreadable_items"], "不可读页的题目应标待人工确认而非猜答案"
    confirm = client.post(f"/api/paperlab/scans/{body['scan']['id']}/confirm", json={})
    assert confirm.status_code == 422
    assert "页面" in confirm.json()["detail"]


def test_scan_missing_anchor_gives_chinese_reason(client):
    env = standard_card(client, single=4, multiple=1, subjective=0)
    layout = env["layout"]
    fills = random_fills(layout, seed=41)
    png = render_page_png(layout, 1, fills, "123456")
    damaged = erase_anchor(png, layout, 1, "bottom_right")
    response = scan_pages(client, env["paper_id"], [damaged], card_id=env["card"]["id"])
    assert response.status_code == 200, response.text
    page = response.json()["result"]["pages"][0]
    assert page["status"] == "unreadable"
    assert "定位点" in page["reason"] or "页面码" in page["reason"]
    assert response.json()["result"]["score"] == 0


def test_scan_low_resolution_and_bad_image(client):
    env = standard_card(client, single=3, multiple=1, subjective=0)
    layout = env["layout"]
    fills = random_fills(layout, seed=51)
    png = render_page_png(layout, 1, fills, "654321")
    tiny = resize(png, 300, 424)
    response = scan_pages(client, env["paper_id"], [tiny], card_id=env["card"]["id"])
    assert response.status_code == 200, response.text
    page = response.json()["result"]["pages"][0]
    assert page["status"] == "unreadable"
    assert "分辨率过低" in page["reason"]
    # 非图片 / 空文件 → 422 中文提示
    assert client.post(f"/api/paperlab/papers/{env['paper_id']}/scan").status_code == 422
    garbage = client.post(f"/api/paperlab/papers/{env['paper_id']}/scan",
                          files=[("files", ("bad.png", b"not-an-image", "image/png"))])
    assert garbage.status_code == 200
    assert "无法读取" in garbage.json()["result"]["pages"][0]["reason"]


def test_scan_upload_limits_and_formats(client, monkeypatch):
    env = standard_card(client, single=2, multiple=0, subjective=0)
    layout = env["layout"]
    png = render_page_png(layout, 1, random_fills(layout, seed=61), "111111")
    too_many = [png] * (scan.MAX_SCAN_PAGES + 1)
    response = scan_pages(client, env["paper_id"], too_many, card_id=env["card"]["id"])
    assert response.status_code == 422
    assert "最多扫描" in response.json()["detail"]
    empty = client.post(f"/api/paperlab/papers/{env['paper_id']}/scan",
                        files=[("files", ("empty.png", b"", "image/png"))])
    assert empty.status_code == 422
    assert "空文件" in empty.json()["detail"]
    bad_ext = client.post(f"/api/paperlab/papers/{env['paper_id']}/scan",
                          files=[("files", ("card.gif", png, "image/gif"))])
    assert bad_ext.status_code == 422
    assert "格式" in bad_ext.json()["detail"]
    # 单页大小上限：临时调小上限验证（不真的制造 30MB 数据）
    with monkeypatch.context() as patch:
        patch.setattr(scan, "MAX_PAGE_BYTES", 1024)
        oversized = client.post(f"/api/paperlab/papers/{env['paper_id']}/scan",
                                files=[("files", ("big.png", png, "image/png"))])
        assert oversized.status_code == 422
        assert "上限" in oversized.json()["detail"]
    # 上传张数 > 答题卡页数
    multi_env = standard_card(client, single=2, multiple=0, subjective=0)
    multi_png = render_page_png(multi_env["layout"], 1,
                                random_fills(multi_env["layout"], seed=62), "222222")
    too_many_pages = scan_pages(client, multi_env["paper_id"], [multi_png, multi_png],
                                card_id=multi_env["card"]["id"])
    assert too_many_pages.status_code == 422
    assert "页" in too_many_pages.json()["detail"]
    # 填涂阈值越界
    threshold_error = scan_pages(client, env["paper_id"], [png], card_id=env["card"]["id"],
                                 threshold=2.0)
    assert threshold_error.status_code == 422
    assert "阈值" in threshold_error.json()["detail"]
    meta = client.get("/api/paperlab/scan/meta")
    assert meta.status_code == 200
    assert meta.json()["max_pages"] == scan.MAX_SCAN_PAGES
    assert meta.json()["threshold"] == scan.DEFAULT_BUBBLE_THRESHOLD
    assert "PNG" in meta.json()["formats"]


# ---------------------------------------------------------------------------
# 4. 气泡冲突 / 不确定项 / confirm 判分
# ---------------------------------------------------------------------------

def test_scan_conflict_unreadable_then_confirm_grading(client):
    questions = [
        create_question(client, qtype="SINGLE", answers=["C"]),
        create_question(client, qtype="SINGLE", answers=["B"]),
        create_question(client, qtype="MULTIPLE", answers=["A", "C"]),
        create_question(client, qtype="SINGLE", answers=["D"]),
        create_question(client, qtype="SUBJECTIVE"),
    ]
    paper = create_paper(client, questions)
    created = create_card(client, paper["paper"]["id"])
    layout = created["layout"]
    # 第 1 题（layout_no=1）同题双泡 → 单选冲突；第 2 题故意选错；其余答对。
    fills = {
        1: ["A", "C"],   # 冲突（单选两个填涂）
        2: ["A"],        # 错误（正确 B）
        3: ["A", "C"],   # 正确
        4: ["D"],        # 正确
    }
    png = render_page_png(layout, 1, fills, "135790")
    response = scan_pages(client, paper["paper"]["id"], [png], card_id=created["card"]["id"])
    assert response.status_code == 200, response.text
    body = response.json()
    result = body["result"]
    scan_id = body["scan"]["id"]
    unreadable = {entry["item_position"] for entry in result["unreadable_items"]}
    assert unreadable == {1}
    assert result["questions"][0]["unreadable"] is True
    assert "多个填涂" in result["questions"][0]["reason"]
    # 冲突题不判分（pending），不猜答案：答案表里没有该题。
    assert "1" not in result["answers"]
    assert result["summary"]["pending_count"] == 1
    assert result["score"] == 5 + 5  # 第 2 题错、其余识别确定的客观题对（每题 5 分）
    # 未确认冲突项 → 422
    missing = client.post(f"/api/paperlab/scans/{scan_id}/confirm", json={"answers": {}})
    assert missing.status_code == 422
    assert "人工确认" in missing.json()["detail"]
    # 非法选项 / 单选多选 → 422
    bad_option = client.post(f"/api/paperlab/scans/{scan_id}/confirm",
                             json={"answers": {"1": ["Z"]}})
    assert bad_option.status_code == 422
    multi_single = client.post(f"/api/paperlab/scans/{scan_id}/confirm",
                               json={"answers": {"1": ["A", "B"]}})
    assert multi_single.status_code == 422
    # 人工纠错：第 1 题改为正确 C，第 2 题保持错误。
    confirm = client.post(f"/api/paperlab/scans/{scan_id}/confirm", json={
        "answers": {"1": ["C"], "2": ["A"], "3": ["A", "C"], "4": ["D"]},
    })
    assert confirm.status_code == 200, confirm.text
    confirmed = confirm.json()
    assert confirmed["scan"]["status"] == "confirmed"
    final = confirmed["result"]
    assert final["unreadable_items"] == []
    assert final["summary"]["correct_count"] == 3
    assert final["summary"]["incorrect_count"] == 1
    assert final["summary"]["pending_count"] == 0
    assert final["score"] == 5 + 5 + 5
    assert final["max_score"] == 5 + 5 + 5 + 5
    assert confirmed["attempt"] and confirmed["attempt"]["id"]
    assert confirmed["attempt"]["score"] == final["score"]
    # 练习记录已生成（source=scan），可从整卷练习历史读取
    attempts = client.get(f"/api/paperlab/papers/{paper['paper']['id']}/attempts").json()
    assert any(entry["id"] == confirmed["attempt"]["id"] for entry in attempts["items"])
    attempt_detail = client.get(f"/api/paperlab/attempts/{confirmed['attempt']['id']}")
    assert attempt_detail.status_code == 200
    detail_items = attempt_detail.json()["items"]
    assert len(detail_items) == 5
    assert attempt_detail.json()["result"]["summary"]["pending_count"] == 1  # 主观题待核验
    # 重复确认 → 409
    again = client.post(f"/api/paperlab/scans/{scan_id}/confirm", json={"answers": {}})
    assert again.status_code == 409
    assert "已确认" in again.json()["detail"]


def test_confirm_requires_complete_pages(client):
    questions = [create_question(client, qtype="SINGLE") for _ in range(20)]
    questions += [create_question(client, qtype="SUBJECTIVE") for _ in range(6)]
    paper = create_paper(client, questions)
    created = create_card(client, paper["paper"]["id"], columns=2)
    layout = created["layout"]
    assert len(layout["pages"]) >= 2
    fills = random_fills(layout, seed=71)
    first_page_png = render_page_png(layout, 1, fills, "123456")
    response = scan_pages(client, paper["paper"]["id"], [first_page_png],
                          card_id=created["card"]["id"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["result"]["missing_pages"] == [2]
    assert body["result"]["pages"][0]["status"] == "ok"
    confirm = client.post(f"/api/paperlab/scans/{body['scan']['id']}/confirm", json={})
    assert confirm.status_code == 422
    assert "页" in confirm.json()["detail"]


def test_scan_include_subjective_false_mapping(client):
    questions = [create_question(client, qtype="SINGLE") for _ in range(3)]
    questions += [create_question(client, qtype="SUBJECTIVE")]
    questions += [create_question(client, qtype="MULTIPLE")]
    paper = create_paper(client, questions)
    created = create_card(client, paper["paper"]["id"], include_subjective=False)
    layout = created["layout"]
    assert len(objective_layout_questions(layout)) == 4
    fills = random_fills(layout, seed=81)
    png = render_page_png(layout, 1, fills, "246810")
    response = scan_pages(client, paper["paper"]["id"], [png], card_id=created["card"]["id"])
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert result["pages"][0]["status"] == "ok"
    # 题号映射跳过未排卡的主观题（position 4）
    positions = [question["item_position"] for question in result["questions"]]
    assert positions == [1, 2, 3, 5]
    assert_answers_match(result, layout, fills)
    assert result["max_score"] == round(sum(
        question["score"] for question in result["questions"]), 2)
    confirm = client.post(f"/api/paperlab/scans/{response.json()['scan']['id']}/confirm", json={})
    assert confirm.status_code == 200, confirm.text
    attempt = confirm.json()["attempt"]
    # 练习表口径：主观题不自动判分但计入 max_score；本题卷面总分 3×5 + 10 + 5 = 30。
    assert attempt["max_score"] == 30
    assert attempt["pending_count"] == 1  # 主观题待核验
    assert attempt["score"] == confirm.json()["result"]["score"]


# ---------------------------------------------------------------------------
# 5. 归属 / 未登录 / stale 卡
# ---------------------------------------------------------------------------

def test_scan_ownership_and_unauthenticated(client, second_client):
    env = standard_card(client, single=3, multiple=1, subjective=0)
    layout = env["layout"]
    png = render_page_png(layout, 1, random_fills(layout, seed=91), "123456")
    response = scan_pages(client, env["paper_id"], [png], card_id=env["card"]["id"])
    assert response.status_code == 200, response.text
    scan_id = response.json()["scan"]["id"]
    assert second_client.get(f"/api/paperlab/scans/{scan_id}").status_code == 404
    assert second_client.post(f"/api/paperlab/scans/{scan_id}/confirm", json={}).status_code == 404
    assert second_client.get(f"/api/paperlab/papers/{env['paper_id']}/scans").status_code == 404
    other_card = create_card(second_client, create_paper(
        second_client, [create_question(second_client)])["paper"]["id"])
    other_png = render_page_png(other_card["layout"], 1,
                                random_fills(other_card["layout"], seed=92), "123456")
    denied = second_client.post(f"/api/paperlab/papers/{env['paper_id']}/scan",
                                files=[("files", ("p.png", png, "image/png"))])
    assert denied.status_code == 404
    # 他人的答题卡 id 扫自己的试卷 → 404
    assert client.post(
        f"/api/paperlab/papers/{env['paper_id']}/scan",
        files=[("files", ("p.png", other_png, "image/png"))],
        data={"card_id": str(other_card["card"]["id"])},
    ).status_code == 404
    anonymous = TestClient(app)
    assert anonymous.get(f"/api/paperlab/scans/{scan_id}").status_code == 401
    assert anonymous.post(f"/api/paperlab/papers/{env['paper_id']}/scan",
                          files=[("files", ("p.png", png, "image/png"))]).status_code == 401


def test_scan_stale_card_rejected(client):
    env = standard_card(client, single=4, multiple=0, subjective=0)
    layout = env["layout"]
    png = render_page_png(layout, 1, random_fills(layout, seed=99), "123456")
    # 修改试卷结构 → 旧答题卡失效
    paper_id = env["paper_id"]
    paper = client.get(f"/api/paperlab/papers/{paper_id}").json()
    changed = client.patch(f"/api/paperlab/papers/{paper_id}", json={
        "version": paper["paper"]["version"],
        "items": [{"ref": f"app:{env['questions'][0]['id']}", "score": 5}],
    })
    assert changed.status_code == 200, changed.text
    response = scan_pages(client, paper_id, [png], card_id=env["card"]["id"])
    assert response.status_code == 422
    assert "失效" in response.json()["detail"]
    # 不传 card_id：默认取与当前试卷指纹一致的卡；全部失效 → 422
    auto = scan_pages(client, paper_id, [png])
    assert auto.status_code == 422
    assert "答题卡" in auto.json()["detail"]


# ---------------------------------------------------------------------------
# 6. 上传资源限额（P0）：有界读取、页数/像素预算、正常小文件不回归
#
# 说明：题库导入（413）与扫描（422）同属「上传资源和备份覆盖」P0 交付，放在本文件是为
# 让验收命令覆盖全部限额路径；题库导入端点在 app/paperlab/bank_io.py。
# ---------------------------------------------------------------------------

def _spy_upload_read(monkeypatch) -> list[int]:
    """记录 Starlette UploadFile.read(size) 的每次调用参数，用于证明读取有界。"""
    from starlette.datastructures import UploadFile as StarletteUploadFile

    calls: list[int] = []
    original = StarletteUploadFile.read

    async def spy(self, size: int = -1):
        calls.append(size)
        return await original(self, size)

    monkeypatch.setattr(StarletteUploadFile, "read", spy)
    return calls


def _tiny_png(width: int = 800, height: int = 1200) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (255, 255, 255)).save(buffer, "PNG")
    return buffer.getvalue()


def _png_declaring_size(width: int, height: int) -> bytes:
    """只含文件头的 PNG：声明巨大宽高但体积很小，用于模拟像素炸弹（无需生成大文件）。"""
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(b"")) + chunk(b"IEND", b""))


def test_scan_upload_read_is_bounded_and_oversize_is_422(client, monkeypatch):
    """超大扫描图片：只读 MAX_PAGE_BYTES+1 字节即拒绝，绝不整读大文件。"""
    env = standard_card(client, single=1, multiple=0, subjective=0)
    calls = _spy_upload_read(monkeypatch)
    limit = 4096
    with monkeypatch.context() as patch:
        patch.setattr(scan, "MAX_PAGE_BYTES", limit)
        response = scan_pages(client, env["paper_id"], [b"x" * (limit * 3)],
                              card_id=env["card"]["id"])
    assert response.status_code == 422
    assert "上限" in response.json()["detail"]
    # 有界读取：读请求必须是有限的 limit+1（不是 -1 的整读），且只发起一次。
    assert calls == [limit + 1], calls


def test_scan_page_count_limit_rejected_before_reading_files(client, monkeypatch):
    """页数超限在读取任何文件内容之前就拒绝（页数预算先于内存分配）。"""
    env = standard_card(client, single=1, multiple=0, subjective=0)
    calls = _spy_upload_read(monkeypatch)
    response = scan_pages(client, env["paper_id"],
                          [b"x"] * (scan.MAX_SCAN_PAGES + 1),
                          card_id=env["card"]["id"])
    assert response.status_code == 422
    assert "最多扫描" in response.json()["detail"]
    assert calls == []


def test_scan_pixel_budget_rejected_before_decode(client, monkeypatch):
    """声明 64MP 的图片在 cv2 解码前被 422 拒绝（40MP 像素预算）。"""
    env = standard_card(client, single=1, multiple=0, subjective=0)
    bomb = _png_declaring_size(8_000, 8_000)
    assert len(bomb) < 1024, "测试用炸弹图本身必须很小，说明拒绝与文件体积无关"
    decoded: list[tuple] = []

    def fake_decode(*args, **kwargs):
        decoded.append(args)
        raise AssertionError("像素超预算的图片不应进入 _decode_image")

    monkeypatch.setattr(scan, "_decode_image", fake_decode)
    response = scan_pages(client, env["paper_id"], [bomb], card_id=env["card"]["id"])
    assert response.status_code == 422
    assert "像素" in response.json()["detail"]
    assert decoded == []


def test_scan_pixel_budget_and_decode_guard_consistent():
    """直接调用 _decode_image 时同样按像素预算拒绝，不分配解码内存。"""
    bomb = _png_declaring_size(8_000, 8_000)
    with pytest.raises(ValueError) as excinfo:
        scan._decode_image(bomb, "bomb.png")
    assert "像素" in str(excinfo.value)


def test_scan_normal_small_image_passes_upload_validation(client):
    """正常尺寸图片不受限额影响：通过上传与像素校验，进入识别（白图无定位点，不伪造读数）。"""
    env = standard_card(client, single=2, multiple=0, subjective=0)
    png = _tiny_png()
    response = scan_pages(client, env["paper_id"], [png], card_id=env["card"]["id"])
    assert response.status_code == 200, response.text
    page = response.json()["result"]["pages"][0]
    assert page["status"] == "unreadable"
    assert page["reason"]
    meta = client.get("/api/paperlab/scan/meta").json()
    assert meta["max_page_pixels"] == scan.MAX_PAGE_PIXELS
    assert meta["max_pages"] == scan.MAX_SCAN_PAGES


def test_bank_import_oversize_is_bounded_and_413(client, monkeypatch):
    """题库导入超大文件：有界读取（MAX_IMPORT_BYTES+1）后返回 413，不整读。"""
    from app.paperlab import bank_io

    calls = _spy_upload_read(monkeypatch)
    limit = 2048
    with monkeypatch.context() as patch:
        patch.setattr(bank_io, "MAX_IMPORT_BYTES", limit)
        response = client.post(
            "/api/paperlab/questions/import",
            files={"file": ("题库.json", b"[]" + b" " * (limit * 2), "application/json")},
        )
    assert response.status_code == 413
    assert "不能超过" in response.json()["detail"]
    assert calls == [limit + 1], calls


def test_bank_import_normal_small_file_unchanged(client, monkeypatch):
    """正常小文件导入行为不变（dry_run 只校验、不写库）。"""
    raw = json.dumps([{
        "type": "SINGLE", "subject": "数学", "grade": "高中一年级",
        "content": {
            "stem": "1+1=?", "options": [{"label": "A", "text": "1"}, {"label": "B", "text": "2"}],
            "answers": ["B"], "explanation": "基础算术。",
        },
    }], ensure_ascii=False).encode("utf-8")
    response = client.post(
        "/api/paperlab/questions/import",
        files={"file": ("题库.json", raw, "application/json")},
        data={"dry_run": "true"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["dry_run"] is True
    assert body["total"] == 1 and body["valid"] == 1

