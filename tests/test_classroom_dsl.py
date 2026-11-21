"""AI 课堂 DSL 校验与规范化（``app/classroom/schema.py``）单元测试。

覆盖：幻灯片 1000×562.5 画布/默认主题/越界钳制/未知元素剔除/HTML 净化；测验单选多选答案校验
与 label-value 反转修正；动作白名单（未知动作拒绝、元素 id 校验、白板 op 归一化）；
交互 HTML 外链拒绝与文档补全；PBL 结构；大纲上游兼容与类型过滤；tutor 回复校验。
"""
import json

import pytest

from app.classroom import generate, schema


# ---------------------------------------------------------------------------
# 幻灯片
# ---------------------------------------------------------------------------

def _text_element(**overrides):
    element = {
        "id": "text_001", "type": "text", "left": 60, "top": 50, "width": 880, "height": 76,
        "content": '<p style="font-size:32px;"><strong>光合作用</strong></p>',
        "defaultFontName": "", "defaultColor": "#333333",
    }
    element.update(overrides)
    return element


def test_slide_normalizes_canvas_theme_and_text_fields():
    content, warnings = schema.normalize_slide_content({"elements": [_text_element()]})
    assert warnings == []
    assert content["type"] == "slide"
    assert content["schemaVersion"] == 1
    canvas = content["canvas"]
    assert canvas["width"] == 1000
    assert canvas["height"] == 562.5
    # 无 theme 时补默认主题
    assert canvas["theme"]["backgroundColor"] == "#ffffff"
    assert canvas["theme"]["fontName"]
    assert len(canvas["theme"]["themeColors"]) >= 3
    text = canvas["elements"][0]
    assert text["text"] == "光合作用"
    assert text["fontSize"] == 32
    assert text["color"] == "#333333"
    assert text["rotate"] == 0
    assert text["type"] == "text" and text["id"] == "text_001"


def test_slide_keeps_provided_theme_and_background_color():
    content, _ = schema.normalize_slide_content({
        "theme": {"backgroundColor": "#101820", "fontColor": "#ffffff", "themeColors": ["#00aaff"]},
        "background": {"type": "solid", "color": "#fafafa"},
        "elements": [_text_element()],
    })
    theme = content["canvas"]["theme"]
    assert theme["backgroundColor"] == "#fafafa"  # background.color 覆盖主题底色
    assert theme["fontColor"] == "#ffffff"
    assert theme["themeColors"] == ["#00aaff"]


def test_slide_clamps_out_of_bounds_geometry_and_rotation():
    content, _ = schema.normalize_slide_content({"elements": [
        _text_element(left=5000, top=-80, width=5000, height=4000, rotate=999),
    ]})
    element = content["canvas"]["elements"][0]
    assert 0 <= element["left"] <= 1000
    assert 0 <= element["top"] <= 562.5
    assert element["width"] <= 1000
    assert element["height"] <= 562.5
    assert -360 <= element["rotate"] <= 360


def test_slide_drops_unknown_and_invalid_elements_with_warnings():
    content, warnings = schema.normalize_slide_content({"elements": [
        _text_element(),
        {"type": "hologram", "left": 10, "top": 10, "width": 100, "height": 100},
        {"type": "image", "left": 10, "top": 10, "width": 100, "height": 100, "src": "https://cdn.example.com/a.png"},
        {"type": "text", "left": 10, "top": 10, "width": 100, "height": 50, "content": ""},
    ]})
    assert len(content["canvas"]["elements"]) == 1
    joined = "\n".join(warnings)
    assert "hologram" in joined
    assert "外网" in joined
    assert "空" in joined


def test_slide_without_usable_elements_is_rejected():
    with pytest.raises(schema.ClassroomSchemaError, match="elements"):
        schema.normalize_slide_content({"canvas": {"elements": []}})
    with pytest.raises(schema.ClassroomSchemaError):
        schema.normalize_slide_content({"elements": [{"type": "unknown"}]})


def test_slide_text_html_is_sanitized():
    content, _ = schema.normalize_slide_content({"elements": [
        _text_element(content=(
            '<p style="font-size:20px; background:url(http://evil/x)">正常文字</p>'
            '<script>alert(1)</script><a href="javascript:alert(1)" onclick="x()">链接</a>'
        )),
    ]})
    html = content["canvas"]["elements"][0]["content"]
    assert "script" not in html.lower()
    assert "onclick" not in html.lower()
    assert "javascript:" not in html.lower()
    assert "background" not in html
    assert "正常文字" in html


def test_slide_accepts_model_html_wrappers_and_element_subtypes():
    content, _ = schema.normalize_slide_content({"elements": [
        {"type": "latex", "left": 100, "top": 200, "width": 300, "height": 90, "latex": "$$E = mc^2$$"},
        {"type": "shape", "left": 60, "top": 300, "width": 200, "height": 100, "path": "M 0 0 L 1 0 L 1 1 L 0 1 Z", "viewBox": [1, 1], "fill": "#5b9bd5"},
        {"type": "line", "left": 100, "top": 400, "width": 60, "start": [0, 0], "end": [200, 0], "style": "dashed", "color": "#333333", "points": ["", "arrow"]},
        {"type": "table", "left": 100, "top": 100, "width": 600, "height": 180,
         "data": [[{"text": "项目"}, {"text": "数值"}], ["光照", "增强"]], "colWidths": [0.5, 0.5]},
        {"type": "chart", "left": 500, "top": 200, "width": 400, "height": 240, "chartType": "line",
         "data": {"labels": ["1", "2"], "legends": ["速率"], "series": [[1, 2]]}},
        {"type": "code", "left": 100, "top": 200, "width": 600, "height": 200, "language": "python", "code": "print(1)"},
    ]})
    elements = content["canvas"]["elements"]
    types = [element["type"] for element in elements]
    assert types == ["latex", "shape", "line", "table", "chart", "code"]
    latex = elements[0]
    assert latex["latex"] == "E = mc^2"
    assert latex["align"] == "center"
    line = elements[2]
    assert line["width"] <= 6  # 误把跨度当描边宽度的输出被归一化
    assert line["points"] == ["", "arrow"]
    assert elements[3]["data"][1][1]["text"] == "增强"
    assert elements[5]["code"] == "print(1)"


def test_slide_element_ids_helper():
    content, _ = schema.normalize_slide_content({"elements": [_text_element()]})
    assert schema.slide_element_ids(content) == ["text_001"]


# ---------------------------------------------------------------------------
# 测验
# ---------------------------------------------------------------------------

def test_quiz_accepts_three_question_types():
    content, warnings = schema.normalize_quiz_content({"questions": [
        {"id": "q1", "type": "single", "question": "场所?", "options": [
            {"label": "叶绿体", "value": "A"}, {"label": "线粒体", "value": "B"}],
         "answer": ["A"], "analysis": "解析", "points": 10},
        {"id": "q2", "type": "multiple", "question": "原料?", "options": [
            {"label": "水", "value": "A"}, {"label": "氧气", "value": "B"}, {"label": "二氧化碳", "value": "C"}],
         "answer": ["A", "C"], "points": 10},
        {"id": "q3", "type": "short_answer", "question": "意义?", "commentPrompt": "评分标准", "analysis": "要点", "points": 20},
    ]})
    assert warnings == []
    questions = content["questions"]
    assert [question["type"] for question in questions] == ["single", "multiple", "short_answer"]
    assert questions[1]["answer"] == ["A", "C"]
    assert questions[2]["options"] == [] and questions[2]["hasAnswer"] is False
    assert questions[0]["points"] == 10


def test_quiz_rejects_invalid_choice_answers():
    base = {"type": "single", "question": "场所?", "answer": ["C"],
            "options": [{"label": "甲", "value": "A"}, {"label": "乙", "value": "B"}]}
    with pytest.raises(schema.ClassroomSchemaError, match="answer"):
        schema.normalize_quiz_content({"questions": [base]})
    with pytest.raises(schema.ClassroomSchemaError, match="选项"):
        schema.normalize_quiz_content({"questions": [{
            "type": "single", "question": "x", "options": [{"label": "甲", "value": "A"}], "answer": ["A"]}]})


def test_quiz_fixes_reversed_label_value_and_defaults_ids():
    content, _ = schema.normalize_quiz_content([{
        "type": "single", "question": "场所?", "options": [
            {"value": "(6, 2)", "label": "A"}, {"value": "乙选项", "label": "B"}],
        "answer": ["A"],
    }])
    options = content["questions"][0]["options"]
    assert options[0]["value"] == "A" and options[0]["label"] == "(6, 2)"
    assert content["questions"][0]["id"] == "q1"


def test_quiz_drops_unknown_question_type_and_resolves_label_answer():
    content, warnings = schema.normalize_quiz_content({"questions": [
        {"type": "essay-question", "question": "x"},
        {"type": "single", "question": "y", "options": [
            {"label": "甲", "value": "A"}, {"label": "乙", "value": "B"}], "answer": ["甲"]},
    ]})
    assert len(content["questions"]) == 1
    # 旧数据/模型把选项文本写进 answer 时，按 label 精确且唯一映射回 value
    assert content["questions"][0]["answer"] == ["A"]
    assert any("essay-question" in warning for warning in warnings)


# ---------------------------------------------------------------------------
# 动作
# ---------------------------------------------------------------------------

def test_actions_whitelist_accepts_known_actions():
    actions, warnings = schema.normalize_actions([
        {"type": "speech", "text": "我们来看这一页", "speed": 9},
        {"type": "spotlight", "elementId": "text_001", "dimOpacity": 0.8},
        {"type": "laser", "element_id": "text_001"},
        {"type": "highlight", "element_id": "text_001", "color": "#ff0"},
        {"type": "whiteboard", "ops": [
            {"kind": "text", "content": "能量", "x": 10, "y": 20},
            {"kind": "line", "x1": 1, "y1": 2, "x2": 3, "y2": 4},
            {"kind": "circle", "x": 5, "y": 6, "width": 30, "height": 30},
            {"kind": "formula", "latex": "E=mc^2", "x": 7, "y": 8},
            {"kind": "unknown-op"},
        ]},
        {"type": "next_scene"},
        {"type": "show_slide", "index": 2},
        {"type": "play_audio", "asset_id": 3},
    ], element_ids=["text_001"])
    assert warnings == []
    types = [action["type"] for action in actions]
    assert types == ["speech", "spotlight", "laser", "highlight", "whiteboard", "next_scene", "show_slide", "play_audio"]
    assert actions[0]["speed"] == 2.0  # 速度钳制到 0.5-2.0
    assert actions[1]["dim_opacity"] == 0.8
    assert [op["kind"] for op in actions[4]["ops"]] == ["text", "line", "circle", "formula"]
    assert schema.speech_text_of(actions) == "我们来看这一页"


def test_actions_reject_unknown_types_and_foreign_element_ids():
    actions, warnings = schema.normalize_actions([
        {"type": "dance"},
        {"type": "spotlight", "element_id": "not_exists"},
        {"type": "laser", "element_id": "text_001"},
        {"type": "play_audio"},
    ], element_ids=["text_001"])
    assert [action["type"] for action in actions] == ["laser"]
    joined = "\n".join(warnings)
    assert "dance" in joined and "not_exists" in joined and "play_audio" in joined


def test_actions_accept_upstream_envelope_format():
    """兼容上游 slide-actions 的 {"type":"action","name":...,"params":...} 信封与 {"type":"text"} 旁白。"""
    actions, warnings = schema.normalize_actions([
        {"type": "action", "name": "spotlight", "params": {"elementId": "text_001"}},
        {"type": "text", "content": "这一页先看标题。"},
        {"type": "action", "name": "laser", "params": {"elementId": "text_001", "color": "#ff0000"}},
        {"type": "action", "name": "whiteboard", "params": {"ops": [{"kind": "text", "content": "能量", "x": 1, "y": 2}]}},
        {"type": "action", "name": "widget_highlight", "params": {"target": "#x"}},
    ], element_ids=["text_001"])
    assert warnings == ["已拒绝未知课堂动作：widget_highlight"]
    assert [action["type"] for action in actions] == ["spotlight", "speech", "laser", "whiteboard"]
    assert actions[0]["element_id"] == "text_001"
    assert actions[1]["text"] == "这一页先看标题。"
    assert actions[3]["ops"][0]["kind"] == "text"


def test_actions_capped_and_ids_unique():
    raw = [{"type": "speech", "text": "第%d句" % index} for index in range(50)]
    actions, warnings = schema.normalize_actions(raw, max_actions=5)
    assert len(actions) == 5
    assert len({action["id"] for action in actions}) == 5
    assert any("上限" in warning for warning in warnings)


# ---------------------------------------------------------------------------
# 交互 / PBL
# ---------------------------------------------------------------------------

def test_interactive_normalizes_widget_alias_and_wraps_fragment():
    content, warnings = schema.normalize_interactive_content({
        "widgetType": "diagram", "html": "<div>流程</div>", "height": 520,
    })
    assert content["widget"] == "mindmap"
    assert content["height"] == 520
    assert content["html"].startswith("<!DOCTYPE html>")
    assert "<div>流程</div>" in content["html"]
    assert warnings == []


def test_interactive_rejects_external_resources_and_empty_html():
    with pytest.raises(schema.ClassroomSchemaError, match="外网"):
        schema.normalize_interactive_content({"html": '<script src="https://cdn.example.com/x.js"></script>'})
    with pytest.raises(schema.ClassroomSchemaError, match="外网"):
        schema.normalize_interactive_content({"html": '<link rel="stylesheet" href="//cdn/a.css">'})
    with pytest.raises(schema.ClassroomSchemaError, match="html"):
        schema.normalize_interactive_content({"widget": "game"})


def test_interactive_placeholder_is_clearly_labeled():
    html = schema.placeholder_interactive_html("光照实验", "simulation", "内容待生成")
    assert "占位" in html and "待生成" in html
    assert "https://" not in html


def test_pbl_structure_normalized():
    content, warnings = schema.normalize_pbl_content({"project": {
        "title": "校园节能方案",
        "background": "为学校设计节能方案",
        "roles": ["工程师", {"name": "数据分析师", "description": "负责统计"}],
        "milestones": [{"title": "调研", "tasks": ["记录一周用电", {"title": "访谈"}], "deliverable": "调研报告"}],
        "rubric": ["方案可行", {"criterion": "数据充分"}],
    }})
    project = content["project"]
    assert project["title"] == "校园节能方案"
    assert [role["name"] for role in project["roles"]] == ["工程师", "数据分析师"]
    assert project["milestones"][0]["tasks"] == ["记录一周用电", "访谈"]
    assert project["rubric"] == ["方案可行", "数据充分"]
    assert warnings == []


def test_pbl_without_title_is_rejected():
    with pytest.raises(schema.ClassroomSchemaError):
        schema.normalize_pbl_content({"project": {"background": "x"}})


# ---------------------------------------------------------------------------
# 大纲 / tutor
# ---------------------------------------------------------------------------

def test_outline_supports_upstream_shape_and_limits_scene_types():
    outline, warnings = schema.normalize_outline({
        "courseTitle": "光合作用",
        "outlines": [
            {"title": "导入", "type": "slide", "keyPoints": ["现象", "目标"]},
            {"title": "课堂小测", "type": "quiz", "quizConfig": {"questionCount": 2, "difficulty": "easy"}},
            {"title": "虚拟实验", "type": "interactive", "widgetType": "simulation"},
            {"title": "总结", "type": "slide", "outline_points": ["回顾"]},
        ],
    }, scene_count=3, scene_types=["slide", "quiz"])
    assert outline["title"] == "光合作用"
    assert [scene["type"] for scene in outline["scenes"]] == ["slide", "quiz", "slide"]  # interactive 降级为 slide
    assert outline["scenes"][1]["quiz"]["question_count"] == 2
    assert outline["scenes"][0]["outline_points"] == ["现象", "目标"]
    assert any("不在本次范围" in warning for warning in warnings)


def test_outline_truncates_to_requested_count_and_rejects_empty():
    outline, warnings = schema.normalize_outline({
        "title": "课", "scenes": [{"title": "第%d页" % index, "type": "slide"} for index in range(6)],
    }, scene_count=3)
    assert len(outline["scenes"]) == 3
    assert [scene["order"] for scene in outline["scenes"]] == [1, 2, 3]
    assert any("截断" in warning for warning in warnings)
    with pytest.raises(schema.ClassroomSchemaError):
        schema.normalize_outline({"title": "x", "scenes": []})


def test_tutor_reply_validates_actions_and_scene_hint():
    reply, warnings = schema.normalize_tutor_reply({
        "reply": {"speaker": "teacher", "text": "我们看叶绿体。", "actions": [
            {"type": "spotlight", "element_id": "text_001"},
            {"type": "dance"},
        ]},
        "scene_hint": {"index": 2, "reason": "下一页更深入"},
    }, element_ids=["text_001"], scene_count=3)
    assert reply["reply"]["speaker"] == "teacher"
    assert len(reply["reply"]["actions"]) == 1
    assert reply["scene_hint"] == {"index": 2, "reason": "下一页更深入"}
    assert any("dance" in warning for warning in warnings)
    with pytest.raises(schema.ClassroomSchemaError):
        schema.normalize_tutor_reply({"reply": {"text": ""}})


# ---------------------------------------------------------------------------
# confirmed_outline 兼容（三种 UI 提交形态）
# ---------------------------------------------------------------------------

def test_parse_confirmed_outline_json_object_and_string():
    raw = {"title": "光合作用", "scenes": [
        {"title": "导入", "type": "slide", "outline_points": ["现象"]},
        {"title": "小测", "type": "quiz"},
    ]}
    from_object = generate.parse_confirmed_outline(raw, scene_count=2, scene_types=list(schema.SCENE_TYPES))
    assert from_object and len(from_object["scenes"]) == 2
    from_string = generate.parse_confirmed_outline(json.dumps(raw, ensure_ascii=False),
                                                   scene_count=2, scene_types=list(schema.SCENE_TYPES))
    assert from_string and from_string["scenes"][1]["type"] == "quiz"


def test_parse_confirmed_outline_markdown_and_sections():
    markdown = "## 导入\n- 现象\n## 课堂小测\n- 检验\n## 总结"
    outline = generate.parse_confirmed_outline(markdown, scene_count=3, scene_types=list(schema.SCENE_TYPES))
    assert outline is not None
    assert [scene["title"] for scene in outline["scenes"]] == ["导入", "课堂小测", "总结"]
    assert outline["scenes"][1]["type"] == "quiz"  # 标题含「测」→ 推断为测验
    from_sections = generate.parse_confirmed_outline(
        None, sections=["导入", "实验", "总结"], scene_count=3, scene_types=list(schema.SCENE_TYPES),
    )
    assert from_sections is not None and [scene["title"] for scene in from_sections["scenes"]] == ["导入", "实验", "总结"]
