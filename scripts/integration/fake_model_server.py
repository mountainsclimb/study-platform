"""本机验收用 OpenAI 兼容假模型服务（AI 课堂浏览器验收专用）。

- **只监听 127.0.0.1**，不访问外网，不读取任何真实 Key / 学生数据；
- 按 prompt 标记返回固定 JSON（大纲 / 幻灯片 / 测验 / 旁白动作 / 交互 HTML / PBL / tutor /
  多智能体名册 / 多角色讨论 / 学生插话），
  用于在隔离实例上完整走通「创建课堂 → 大纲确认 → 场景生成 → 播放器 → 测验 / 交互 / PBL / 对话 /
  多智能体讨论与插话」；
- 未识别的 prompt 返回明确 JSON `{"error":"unknown_prompt",...}`（HTTP 200，不 500）；
- 仅用于本机 UI 验收，不得用于生产或对外提供服务。

用法：
    python scripts/integration/fake_model_server.py --port 8902

配合 classroom_accept.py：
    python scripts/integration/classroom_accept.py \
        --base-url http://127.0.0.1:8832 \
        --fake-model-base-url http://127.0.0.1:8902/v1 \
        --username 课堂验收 --password ui-pass-12345
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOOPBACK_HOST = "127.0.0.1"

OUTLINE = {
    "title": "光合作用入门",
    "description": "面向初中生物的光合作用原理与影响因素入门课。",
    "scenes": [
        {"title": "为什么植物离不开光", "type": "slide", "outline_points": ["生活现象导入", "光合作用的定义", "本节学习目标"]},
        {"title": "原料与产物", "type": "slide", "outline_points": ["反应物与生成物", "叶绿体的作用", "总反应式"]},
        {"title": "光照强度模拟", "type": "interactive", "outline_points": ["调节光照观察速率", "找到光饱和点"], "widget": "simulation"},
        {"title": "课堂小测", "type": "quiz", "outline_points": ["场所与原料", "影响因素"], "quiz": {"question_count": 2, "difficulty": "medium"}},
    ],
}

QUIZ = {"questions": [
    {"id": "q1", "type": "single", "question": "光合作用发生的场所是？",
     "options": [{"label": "叶绿体", "value": "A"}, {"label": "线粒体", "value": "B"},
                 {"label": "细胞核", "value": "C"}, {"label": "液泡", "value": "D"}],
     "answer": ["A"], "analysis": "叶绿体含有叶绿素，是光合作用的场所。", "points": 10},
    {"id": "q2", "type": "multiple", "question": "光合作用的原料包括哪些？（多选）",
     "options": [{"label": "水", "value": "A"}, {"label": "二氧化碳", "value": "B"},
                 {"label": "氧气", "value": "C"}, {"label": "有机物", "value": "D"}],
     "answer": ["A", "B"], "analysis": "水和二氧化碳是原料，氧气和有机物是产物。", "points": 10},
]}

ACTIONS = [
    {"type": "action", "name": "spotlight", "params": {"elementId": "title_001"}},
    {"type": "text", "content": "我们先从生活现象出发：为什么植物离开光就长不好？"},
    {"type": "action", "name": "spotlight", "params": {"elementId": "point_001"}},
    {"type": "text", "content": "植物把光能转化成化学能，原料是水和二氧化碳，场所是叶绿体。"},
    {"type": "action", "name": "laser", "params": {"elementId": "latex_001"}},
    {"type": "text", "content": "这是总反应式，二氧化碳和水在光照与叶绿体作用下生成有机物，并释放氧气。"},
]

TUTOR = {
    "reply": {
        "speaker": "teacher",
        "text": "因为水在光照下会被分解：**水**是光合作用的原料之一。可以想想，如果把植物放进没有光的房间，它还能持续制造有机物吗？",
        "actions": [{"type": "spotlight", "element_id": "point_001"}],
    },
    "scene_hint": {"index": 1, "reason": "下一页会把原料与产物整理成表格，继续看更清楚。"},
}

INTERACTIVE_HTML = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>光照强度模拟</title>
<style>body{margin:0;padding:16px;font-family:system-ui,sans-serif;background:#f8fafc;color:#1f2933}
h2{font-size:18px;margin:0 0 10px}.row{display:flex;gap:12px;align-items:center;margin:12px 0;flex-wrap:wrap}
input[type=range]{flex:1;min-width:160px}canvas{width:100%;height:200px;background:#fff;border-radius:10px;border:1px solid #dbe5ea}
p{font-size:13px;line-height:1.7;color:#526b76}</style></head>
<body><div id="experiment"><h2>光照强度与光合速率</h2>
<div class="row"><label for="light">光照强度</label><input id="light" type="range" min="0" max="100" value="40"><strong id="value">40%</strong></div>
<canvas id="chart" width="640" height="220"></canvas>
<p>调节光照强度，观察光合速率如何变化，并尝试找到光饱和点。</p></div>
<script>
(function () {
  var slider = document.getElementById('light');
  var valueText = document.getElementById('value');
  var canvas = document.getElementById('chart');
  var ctx = canvas.getContext('2d');
  function zhixuReport(type, payload) {
    try { window.parent.postMessage({ source: 'zhixu-interactive', type: type, payload: payload || {} }, '*'); }
    catch (error) { /* 上报失败不影响页面交互 */ }
  }
  function draw() {
    var level = Number(slider.value);
    var rate = Math.min(36, level * 0.9);
    valueText.textContent = level + '%';
    ctx.clearRect(0, 0, 640, 220);
    ctx.strokeStyle = '#2f9e44'; ctx.lineWidth = 3; ctx.beginPath();
    for (var x = 0; x <= 640; x += 8) {
      var lv = x / 640 * 100;
      var r = Math.min(36, lv * 0.9);
      var y = 210 - (r / 36) * 190;
      if (x === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    }
    ctx.stroke();
    zhixuReport('state', { summary: '光照强度 ' + level + '%，光合速率 ' + rate.toFixed(1) });
  }
  slider.addEventListener('input', draw);
  draw();
  window.addEventListener('load', function () { zhixuReport('ready', {}); });
})();
</script></body></html>"""

PBL_PROJECT = {
    "project": {
        "title": "校园节能方案",
        "background": "学校近三个月电费持续上涨，后勤部门希望学生团队提出可执行的节能方案。",
        "goals": ["理解用电统计方法", "能用数据支撑节能建议"],
        "roles": [
            {"name": "数据分析师", "description": "统计并分析校园用电数据"},
            {"name": "节能设计师", "description": "提出并论证节能措施"},
        ],
        "milestones": [
            {"title": "用电调研", "description": "记录教学楼一周的用电情况",
             "tasks": ["记录一周分时段用电", "访谈后勤老师"], "deliverable": "用电调研表",
             "checkpoint": "数据覆盖一周且标注异常时段"},
            {"title": "方案设计与展示", "description": "提出可执行的节能措施并估算收益",
             "tasks": ["提出 3 条节能措施", "估算每条措施的节电比例"], "deliverable": "节能方案海报",
             "checkpoint": "每条措施都有数据与收益估算"},
        ],
        "rubric": [
            {"criterion": "数据充分", "levels": "优秀/合格/待改进"},
            {"criterion": "方案可行", "levels": "优秀/合格/待改进"},
        ],
        "mentor_hints": ["先算清基线用电量，再谈节省", "比较措施的实施成本与收益"],
    },
}

# 多智能体课堂（P1）：名册 / 多角色讨论 / 学生插话固定产物。
# 讨论脚本里的 whiteboard 使用 0-1 归一化坐标（coord_space="unit"），覆盖 7 种 op。
ROSTER = {
    "teacher": {
        "name": "林老师",
        "persona": "善于用追问推进推导，讲题时先让学生猜再纠正，语气亲切。",
    },
    "classmates": [
        {"name": "小舟", "persona": "好奇心强，常问为什么，但计算容易粗心。"},
        {"name": "小雨", "persona": "喜欢先记结论，需要老师带着看推导过程。"},
    ],
}

MULTI_WHITEBOARD_OPS = [
    {"kind": "rect", "x": 0.06, "y": 0.62, "width": 0.2, "height": 0.13,
     "fill": "transparent", "stroke": "#5b9bd5"},
    {"kind": "text", "x": 0.075, "y": 0.655, "content": "光反应", "font_size": 18, "color": "#333333"},
    {"kind": "arrow", "x1": 0.27, "y1": 0.685, "x2": 0.37, "y2": 0.685, "color": "#333333", "width": 2},
    {"kind": "line", "x1": 0.06, "y1": 0.79, "x2": 0.27, "y2": 0.79, "color": "#94a3b8", "width": 2},
    {"kind": "circle", "x": 0.41, "y": 0.61, "width": 0.14, "height": 0.14,
     "fill": "transparent", "stroke": "#e8590c"},
    {"kind": "formula", "x": 0.06, "y": 0.82, "width": 0.38,
     "latex": "6CO_2 + 6H_2O \\rightarrow C_6H_{12}O_6 + 6O_2", "color": "#1f6feb"},
    {"kind": "path", "x": 0.62, "y": 0.62, "width": 0.24, "height": 0.14,
     "d": "M0 1 L1 0 M1 0 L0.78 0 M1 0 L1 0.24", "view_box": [1, 1],
     "fill": "transparent", "stroke": "#2f9e44", "stroke_width": 2},
]

DISCUSSION = {
    "mode": "discussion",
    "turns": [
        {"speaker_id": "teacher_1",
         "text": "这一页我们解决两个问题：光合作用的原料是什么，产物又去了哪里。先别急着记结论，看这页的元素。",
         "actions": [{"type": "spotlight", "element_id": "text_001", "dim_opacity": 0.5}]},
        {"speaker_id": "student_1",
         "text": "老师，为什么水也算原料？我一直以为只需要二氧化碳。",
         "actions": []},
        {"speaker_id": "teacher_1",
         "text": "问得好。水在光反应里被分解，提供氢和电子，所以它确实是原料。我们用白板把这条链条画出来。",
         "actions": [{"type": "whiteboard", "coord_space": "unit", "ops": MULTI_WHITEBOARD_OPS}]},
        {"speaker_id": "student_2",
         "text": "那氧气是从水来的，还是从二氧化碳来的？",
         "actions": []},
        {"speaker_id": "teacher_1",
         "text": "氧气来自水的分解，这一点和植物释放氧气是一致的。",
         "actions": [{"type": "laser", "element_id": "point_001", "color": "#ff0000"}]},
    ],
    "scene_hint": {"index": 3, "reason": "下一页的课堂小测可以检验刚才的讨论。"},
}

INTERJECT = {
    "mode": "interject",
    "turns": [
        {"speaker_id": "teacher_1",
         "text": "这个问题很关键：叶绿体里的色素先吸收光能，再推动水的分解。",
         "actions": [{"type": "spotlight", "element_id": "point_001", "dim_opacity": 0.45}]},
        {"speaker_id": "student_1",
         "text": "所以光和叶绿体缺一不可，我记住了。",
         "actions": []},
    ],
    "scene_hint": None,
}

UNKNOWN_REPLY = {
    "error": "unknown_prompt",
    "message": "本机假模型服务未识别该 prompt；请在 fake_model_server.classify() 中补充标记与固定产物。",
}


def classify(prompt: str) -> str:
    # 多智能体课堂（P1）：标记来自 app/classroom/multiagent.py 的 prompt 标题，按最长/最独特标记优先匹配。
    if "课堂角色名册生成" in prompt:
        return "roster"
    if "多智能体课堂 · 学生插话回应" in prompt:
        return "interject"
    if "多智能体课堂脚本生成" in prompt:
        return "discussion"
    if "课堂 AI 教师对话" in prompt:
        return "tutor"
    if "课程大纲生成器" in prompt:
        return "outline"
    if "幻灯片内容生成器" in prompt:
        return "slide1" if ("为什么植物离不开光" in prompt or "导入" in prompt) else "slide2"
    if "测验内容生成器" in prompt:
        return "quiz"
    if "交互场景 HTML 生成器" in prompt:
        return "interactive"
    if "项目式学习（PBL）设计器" in prompt:
        return "pbl"
    if "课堂旁白与动作生成器" in prompt:
        return "actions"
    return "unknown"


def page_title(prompt: str, default: str) -> str:
    match = re.search(r"本页标题：([^\n]+)", prompt)
    title = (match.group(1).strip() if match else "") or default
    return title[:40]


def slide_payload(prompt: str) -> dict:
    if classify(prompt) == "slide1":
        title = page_title(prompt, "为什么植物离不开光")
        return {"elements": [
            {"id": "title_001", "type": "text", "left": 60, "top": 50, "width": 880, "height": 76,
             "content": f'<p style="font-size:32px;"><strong>{title}</strong></p>', "defaultFontName": "", "defaultColor": "#333333"},
            {"id": "point_001", "type": "text", "left": 80, "top": 170, "width": 500, "height": 130,
             "content": '<p style="font-size:20px;">• 植物把光能转化为化学能</p><p style="font-size:20px;">• 原料：水、二氧化碳</p><p style="font-size:20px;">• 场所：叶绿体</p>',
             "defaultFontName": "", "defaultColor": "#333333"},
            {"id": "shape_001", "type": "shape", "left": 640, "top": 150, "width": 280, "height": 240,
             "path": "M 0 0 L 1 0 L 1 1 L 0 1 Z", "viewBox": [1, 1], "fill": "#e7f5ff", "fixedRatio": False},
            {"id": "latex_001", "type": "latex", "left": 660, "top": 250, "width": 240, "height": 80,
             "latex": "6CO_2 + 6H_2O \\rightarrow C_6H_{12}O_6 + 6O_2", "color": "#1f6feb", "align": "center"},
        ]}
    title = page_title(prompt, "原料与产物")
    return {"elements": [
        {"id": "title_002", "type": "text", "left": 60, "top": 50, "width": 880, "height": 58,
         "content": f'<p style="font-size:28px;"><strong>{title}</strong></p>', "defaultFontName": "", "defaultColor": "#333333"},
        {"id": "table_001", "type": "table", "left": 70, "top": 150, "width": 560, "height": 200,
         "colWidths": [0.34, 0.33, 0.33],
         "data": [[{"colspan": 1, "rowspan": 1, "text": "类别"}, {"colspan": 1, "rowspan": 1, "text": "物质"}, {"colspan": 1, "rowspan": 1, "text": "说明"}],
                  [{"colspan": 1, "rowspan": 1, "text": "原料"}, {"colspan": 1, "rowspan": 1, "text": "水、二氧化碳"}, {"colspan": 1, "rowspan": 1, "text": "叶绿体吸收"}],
                  [{"colspan": 1, "rowspan": 1, "text": "产物"}, {"colspan": 1, "rowspan": 1, "text": "有机物、氧气"}, {"colspan": 1, "rowspan": 1, "text": "释放到环境"}]],
         "outline": {"width": 1, "style": "solid", "color": "#cccccc"}},
        {"id": "chart_001", "type": "chart", "left": 660, "top": 150, "width": 280, "height": 200,
         "chartType": "line", "data": {"labels": ["弱光", "中光", "强光"], "legends": ["光合速率"], "series": [[12, 28, 36]]},
         "themeColors": ["#2f9e44", "#e8590c"]},
    ]}


def payload_for(kind: str, prompt: str) -> dict | list:
    if kind == "outline":
        return OUTLINE
    if kind in ("slide1", "slide2"):
        return slide_payload(prompt)
    if kind == "quiz":
        return QUIZ
    if kind == "interactive":
        return {"widget": "simulation", "height": 440, "html": INTERACTIVE_HTML}
    if kind == "pbl":
        return PBL_PROJECT
    if kind == "actions":
        return ACTIONS
    if kind == "tutor":
        return TUTOR
    if kind == "roster":
        return ROSTER
    if kind == "discussion":
        return DISCUSSION
    if kind == "interject":
        return INTERJECT
    # 未知 prompt 也返回明确 JSON（HTTP 200 + 可解析 error 对象），便于后端给出结构化校验提示，绝不 500。
    return UNKNOWN_REPLY


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # 静默日志：验收输出更干净
        return

    def _send(self, status: int, payload, content_type: str = "application/json"):
        body = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.rstrip("/") in ("/v1/models", "/models"):
            return self._send(200, {"object": "list", "data": [{"id": "mock-classroom-model", "object": "model"}]})
        return self._send(404, {"error": {"message": "not found"}})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            body = {}
        prompt = ""
        for message in body.get("messages") or []:
            content = message.get("content")
            if isinstance(content, list):
                content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
            prompt += str(content or "")
        kind = classify(prompt)
        content = json.dumps(payload_for(kind, prompt), ensure_ascii=False)
        response = {
            "id": "chatcmpl-mock", "object": "chat.completion", "created": 1,
            "model": body.get("model") or "mock-classroom-model",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 120, "completion_tokens": 80, "total_tokens": 200},
        }
        return self._send(200, response)


def _assert_loopback(host: str) -> None:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if host != "localhost":
            raise SystemExit("假模型服务只允许监听本机回环地址（127.0.0.1 / ::1）")
        return
    if not address.is_loopback:
        raise SystemExit("假模型服务只允许监听本机回环地址（127.0.0.1 / ::1）")


def main() -> int:
    parser = argparse.ArgumentParser(description="本机验收用 OpenAI 兼容假模型服务（只监听回环地址）")
    parser.add_argument("--port", type=int, default=8899, help="监听端口，默认 8899")
    args = parser.parse_args()
    host = LOOPBACK_HOST
    _assert_loopback(host)
    server = ThreadingHTTPServer((host, args.port), Handler)
    print(f"fake model server on http://{host}:{args.port}/v1", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
