"""AI 课堂 P7：交互 widget 高保真能力（3D three.js 内联 / 在线编程 / Canvas 游戏）验收。

覆盖点（不联网，模型全部 mock，不访问外网）：

1. 静态库资产：``web/vendor/interactive/three.module.min.js`` 存在、体积/哈希自洽、可安全内联
   （无 ``</script``、无 eval/new Function）、MIT LICENSE 与来源 README 齐全；
2. 注入正确性：3D HTML 先校验后注入、库脚本在用户脚本之前、用户 HTML 逐字节不变、幂等；
3. 安全边界：注入后用户危险 API（fetch/eval/父窗口/Cookie/存储/外链）仍被拒；伪造
   ``data-zhixu-lib`` 片段不被豁免；THREE 只允许用于 visualization3d；沙箱 CSP 不含 unsafe-eval；
4. 体积：普通交互 600KB、3D 含内联库 1.5MB（schema 按 widget 分别限制，且都 < DB 2MB 上限）；
5. 真实生成链路（mock 模型）：visualization3d 生成 → 注入 → 入库 → 沙箱路由可播放；
   code / game 生成不注入任何库；生成失败可带纠错重试且失败不伪造内容；
6. 导入/导出兼容：导入侧重新校验已注入内容不二次注入、资源包外链扫描无误报。
"""
import hashlib
import json
import time
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from app import ai, db
from app.classroom import interactive, schema
from app.classroom import db as cdb

REPO_ROOT = Path(__file__).resolve().parents[1]
VENDOR_DIR = REPO_ROOT / "web" / "vendor" / "interactive"

THREE_SCRIPT_OPEN = '<script data-zhixu-lib="three@0.186.1">'


# ---------------------------------------------------------------------------
# 样例 HTML（模型产物）
# ---------------------------------------------------------------------------

THREE_HTML = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>水分子的 3D 结构</title>
<style>html,body{margin:0;padding:0;background:#0b1220;color:#e2e8f0;font-family:system-ui,sans-serif}
#experiment{display:flex;flex-direction:column;height:100vh}#stage{flex:1;min-height:280px;position:relative}
#scene-canvas{display:block;width:100%;height:100%}#hud{padding:10px 14px;font-size:13px;line-height:1.7}
button{margin-top:6px;min-height:40px;min-width:72px;border-radius:8px;border:1px solid #334155;
background:#1e293b;color:#e2e8f0}</style></head>
<body><div id="experiment"><div id="stage"><canvas id="scene-canvas"></canvas></div>
<div id="hud"><strong>水分子（H₂O）结构观察</strong><span id="angle">水平角度 0°</span>
<button id="reset" type="button">重置视角</button></div></div>
<script>
(function () {
  function zhixuReport(type, payload) {
    try { window.parent.postMessage({ source: 'zhixu-interactive', type: type, payload: payload || {} }, '*'); }
    catch (error) { /* 上报失败不影响页面交互 */ }
  }
  window.zhixuReport = zhixuReport;
  window.addEventListener('error', function (event) {
    zhixuReport('error', { message: String((event && event.message) || '脚本错误').slice(0, 500) });
  });
  window.addEventListener('load', function () { zhixuReport('ready', {}); });
  var stage = document.getElementById('stage');
  var canvas = document.getElementById('scene-canvas');
  var angleText = document.getElementById('angle');
  var renderer;
  try {
    renderer = new THREE.WebGLRenderer({ canvas: canvas, antialias: true });
  } catch (error) {
    stage.insertAdjacentHTML('beforeend', '<p>当前浏览器不支持 WebGL，3D 视图不可用。</p>');
    return;
  }
  renderer.setPixelRatio(Math.min(2, window.devicePixelRatio || 1));
  var scene = new THREE.Scene();
  scene.background = new THREE.Color(0x0b1220);
  var camera = new THREE.PerspectiveCamera(45, 1, 0.1, 100);
  camera.position.set(0.6, 1.4, 4.2);
  camera.lookAt(0, 0, 0);
  scene.add(new THREE.AmbientLight(0xffffff, 0.85));
  var light = new THREE.DirectionalLight(0xffffff, 2.0);
  light.position.set(3, 5, 4);
  scene.add(light);
  var group = new THREE.Group();
  var oxygen = new THREE.Mesh(
    new THREE.SphereGeometry(0.95, 32, 24),
    new THREE.MeshStandardMaterial({ color: 0xef4444, roughness: 0.35, metalness: 0.1 })
  );
  group.add(oxygen);
  var hydrogenMaterial = new THREE.MeshStandardMaterial({ color: 0xe2e8f0, roughness: 0.4 });
  var bondMaterial = new THREE.MeshStandardMaterial({ color: 0x94a3b8, roughness: 0.8 });
  [[1.1, 0.75, 0.2], [-1.1, 0.75, -0.2]].forEach(function (pos) {
    var hydrogen = new THREE.Mesh(new THREE.SphereGeometry(0.42, 24, 16), hydrogenMaterial);
    hydrogen.position.set(pos[0], pos[1], pos[2]);
    group.add(hydrogen);
    var bond = new THREE.Mesh(new THREE.CylinderGeometry(0.09, 0.09, 1.0, 12), bondMaterial);
    bond.position.set(pos[0] * 0.55, pos[1] * 0.55, pos[2] * 0.55);
    bond.lookAt(oxygen.position);
    bond.rotateX(Math.PI / 2);
    group.add(bond);
  });
  group.add(new THREE.AxesHelper(1.6));
  scene.add(group);
  var yaw = 0.35, pitch = 0.18, distance = 4.6, dragging = false, lastX = 0, lastY = 0;
  function resize() {
    var width = Math.max(120, stage.clientWidth);
    var height = Math.max(160, stage.clientHeight);
    renderer.setSize(width, height, false);
    camera.aspect = width / height;
    camera.updateProjectionMatrix();
  }
  function placeCamera() {
    camera.position.set(Math.sin(yaw) * distance, Math.sin(pitch) * distance + 0.9, Math.cos(yaw) * distance);
    camera.lookAt(0, 0, 0);
  }
  function report() {
    var deg = Math.round(yaw * 180 / Math.PI) % 360;
    angleText.textContent = '水平角度 ' + deg + '° · 缩放 ' + distance.toFixed(1);
    zhixuReport('state', {
      summary: '水分子模型视角：水平 ' + deg + '°，距离 ' + distance.toFixed(1),
      state: { yaw: deg, pitch: Math.round(pitch * 180 / Math.PI), distance: Number(distance.toFixed(2)) }
    });
  }
  canvas.addEventListener('pointerdown', function (event) { dragging = true; lastX = event.clientX; lastY = event.clientY; });
  window.addEventListener('pointerup', function () { dragging = false; });
  window.addEventListener('pointermove', function (event) {
    if (!dragging) { return; }
    yaw -= (event.clientX - lastX) * 0.01; pitch += (event.clientY - lastY) * 0.01;
    pitch = Math.max(-0.9, Math.min(0.9, pitch));
    lastX = event.clientX; lastY = event.clientY;
    placeCamera(); report();
  });
  canvas.addEventListener('wheel', function (event) {
    if (event.cancelable) { event.preventDefault(); }
    distance = Math.max(2.4, Math.min(9, distance + (event.deltaY > 0 ? 0.4 : -0.4)));
    placeCamera(); report();
  }, { passive: false });
  document.getElementById('reset').addEventListener('click', function () {
    yaw = 0.35; pitch = 0.18; distance = 4.6; placeCamera(); report();
  });
  window.addEventListener('resize', resize);
  resize(); placeCamera();
  var clock = new THREE.Clock();
  function animate() {
    group.rotation.y += clock.getDelta() * 0.35;
    renderer.render(scene, camera);
    requestAnimationFrame(animate);
  }
  animate();
  report();
})();
</script></body></html>"""

GAME_HTML = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>分数配对游戏</title>
<style>body{margin:0;padding:14px;font-family:system-ui,sans-serif;background:#f8fafc;color:#1f2933}
canvas{width:100%;height:260px;background:#fff;border:1px solid #dbe5ea;border-radius:10px}
button{min-height:44px;min-width:96px;border-radius:10px;border:1px solid #cbd5e1;background:#eef6ff}
p{margin:8px 0;font-size:14px}</style></head>
<body><div id="experiment"><h2>分数配对：点「开始」计时配对</h2>
<canvas id="board" width="640" height="300"></canvas><p id="score">得分：0 · 用时：0 秒</p>
<button id="start" type="button">开始</button></div>
<script>
(function () {
  function zhixuReport(type, payload) {
    try { window.parent.postMessage({ source: 'zhixu-interactive', type: type, payload: payload || {} }, '*'); }
    catch (error) { /* 上报失败不影响页面交互 */ }
  }
  window.zhixuReport = zhixuReport;
  window.addEventListener('error', function (event) {
    zhixuReport('error', { message: String((event && event.message) || '脚本错误').slice(0, 500) });
  });
  window.addEventListener('load', function () { zhixuReport('ready', {}); });
  var canvas = document.getElementById('board');
  var ctx = canvas.getContext('2d');
  var scoreText = document.getElementById('score');
  var score = 0;
  var pairs = [['1/2', '0.5'], ['1/4', '0.25'], ['3/4', '0.75']];
  var picked = [];
  function draw() {
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.font = '20px system-ui';
    pairs.forEach(function (pair, index) {
      var y = 60 + index * 70;
      ctx.fillStyle = picked.indexOf(index) >= 0 ? '#bfdbfe' : '#e2e8f0';
      ctx.fillRect(40, y - 32, 220, 46);
      ctx.fillStyle = '#1f2933';
      ctx.fillText(pair[0] + '  =  ' + pair[1], 56, y);
    });
    scoreText.textContent = '得分：' + score + ' · 用时：0 秒';
  }
  document.getElementById('start').addEventListener('click', function () {
    picked = picked.length >= pairs.length ? [] : picked.concat([picked.length]);
    score = picked.length;
    draw();
    zhixuReport('state', { summary: '已完成 ' + score + ' 组分数配对', state: { score: score } });
  });
  draw();
  zhixuReport('state', { summary: '分数配对游戏就绪', state: { score: 0 } });
})();
</script></body></html>"""

CODE_HTML = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>变量累加追踪</title>
<style>body{margin:0;padding:14px;font-family:system-ui,sans-serif;background:#fff;color:#1f2933}
pre{background:#0f172a;color:#e2e8f0;padding:12px;border-radius:10px;overflow:auto;font-size:13px}
input{min-height:44px;padding:0 10px;border:1px solid #cbd5e1;border-radius:8px;font-size:15px;width:120px}
button{min-height:44px;min-width:96px;border-radius:10px;border:1px solid #cbd5e1;background:#eef6ff}</style></head>
<body><div id="experiment"><h2>追踪变量：循环结束后 total 是多少？</h2>
<pre id="code">total = 0
for i in range(3):
    total += i</pre>
<label for="answer">总和的最终值：</label><input id="answer" inputmode="numeric" placeholder="填写数字">
<button id="check" type="button">检查</button><p id="feedback" role="status">提示：循环变量 i 依次取 0、1、2。</p></div>
<script>
(function () {
  function zhixuReport(type, payload) {
    try { window.parent.postMessage({ source: 'zhixu-interactive', type: type, payload: payload || {} }, '*'); }
    catch (error) { /* 上报失败不影响页面交互 */ }
  }
  window.zhixuReport = zhixuReport;
  window.addEventListener('error', function (event) {
    zhixuReport('error', { message: String((event && event.message) || '脚本错误').slice(0, 500) });
  });
  window.addEventListener('load', function () { zhixuReport('ready', {}); });
  var input = document.getElementById('answer');
  var feedback = document.getElementById('feedback');
  document.getElementById('check').addEventListener('click', function () {
    var value = String(input.value || '').trim();
    var ok = value === '3';
    feedback.textContent = ok ? '正确：0 + 1 + 2 = 3。' : '再想想：把 i 的取值依次累加。';
    zhixuReport('state', {
      summary: ok ? '学生答对了变量累加题' : '学生作答变量累加题（待订正）',
      state: { answer: value, correct: ok }
    });
  });
  zhixuReport('state', { summary: '代码练习就绪', state: { correct: false } });
})();
</script></body></html>"""

PAYLOAD_3D = {"widget": "visualization3d", "height": 520, "html": THREE_HTML}
PAYLOAD_GAME = {"widget": "game", "height": 420, "html": GAME_HTML}
PAYLOAD_CODE = {"widget": "code", "height": 480, "html": CODE_HTML}


# ---------------------------------------------------------------------------
# 假模型 / 公共辅助
# ---------------------------------------------------------------------------

SLIDE_RAW = {"elements": [
    {"id": "text_001", "type": "text", "left": 60, "top": 50, "width": 880, "height": 76,
     "content": "<p style=\"font-size:32px\">本课回顾</p>", "defaultFontName": "", "defaultColor": "#333333"},
]}

QUIZ_RAW = {"questions": [
    {"id": "q1", "type": "single", "question": "水分子中氧原子与氢原子的键角约为？",
     "options": [{"label": "约 104.5°", "value": "A"}, {"label": "约 180°", "value": "B"}],
     "answer": ["A"], "analysis": "水分子呈 V 形，键角约 104.5°。", "points": 10},
]}

OUTLINE_3D = {
    "title": "分子结构 3D 互动课",
    "scenes": [
        {"title": "分子结构导入", "type": "slide", "outline_points": ["分子由原子构成"]},
        {"title": "水分子 3D 结构", "type": "interactive",
         "outline_points": ["旋转观察水分子", "找到键角"], "widget": "visualization3d"},
        {"title": "课堂小测", "type": "quiz", "outline_points": ["键角"],
         "quiz": {"question_count": 1, "difficulty": "easy"}},
    ],
}

OUTLINE_CODE_GAME = {
    "title": "编程与游戏互动课",
    "scenes": [
        {"title": "变量回顾", "type": "slide", "outline_points": ["变量与循环"]},
        {"title": "变量累加追踪", "type": "interactive",
         "outline_points": ["读懂循环", "填空判定"], "widget": "code"},
        {"title": "分数配对游戏", "type": "interactive",
         "outline_points": ["分数与小数配对"], "widget": "game"},
    ],
}

_MARKERS = (
    ("课程大纲生成器", "outline"),
    ("幻灯片内容生成器", "slide"),
    ("测验内容生成器", "quiz"),
    ("课堂旁白与动作生成器", "actions"),
    ("交互场景 HTML 生成器", "interactive"),
)


class FakeModel:
    """按 prompt 标记返回固定 JSON；交互场景可按顺序返回「先非法、重试合法」的序列。"""

    def __init__(self, *, outline=None, interactive_payloads=None):
        self.payloads = {
            "outline": json.dumps(outline if outline is not None else OUTLINE_3D, ensure_ascii=False),
            "slide": json.dumps(SLIDE_RAW, ensure_ascii=False),
            "quiz": json.dumps(QUIZ_RAW, ensure_ascii=False),
            "actions": json.dumps([{"type": "text", "content": "请观察这一页的结构。"}], ensure_ascii=False),
        }
        self.sequences = {
            "interactive": [
                json.dumps(item, ensure_ascii=False) if not isinstance(item, str) else item
                for item in (interactive_payloads or [PAYLOAD_3D])
            ],
        }
        self.calls: list[str] = []
        self.prompts: list[str] = []

    def classify(self, prompt: str) -> str:
        for marker, kind in _MARKERS:
            if marker in prompt:
                return kind
        raise AssertionError("未知 prompt：%s" % prompt[:200])

    def install(self, monkeypatch):
        async def fake_call(provider, model, prompt, images=None, json_output=False, token_limit=5000, **kwargs):
            kind = self.classify(prompt)
            self.calls.append(kind)
            self.prompts.append(prompt)
            sequence = self.sequences.get(kind)
            if sequence is not None and len(sequence) > 1:
                content = sequence.pop(0)
            elif sequence:
                content = sequence[0]
            else:
                content = self.payloads[kind]
            return {"content": content, "model": model, "provider_name": "mock",
                    "usage": {"total_tokens": 12}}

        monkeypatch.setattr(ai, "_call_model", fake_call)
        return self

    def count(self, kind: str) -> int:
        return self.calls.count(kind)


def save_provider(client, name="课堂 3D 测试连接"):
    response = client.post("/api/providers", json={
        "name": name, "kind": "openai", "base_url": "https://api.openai.com/v1",
        "api_key": "sk-classroom-3d-unit-test-123456", "models": ["test-model"],
    })
    assert response.status_code == 200, response.text
    return response.json()


def create_course(client, **overrides):
    payload = {"topic": "分子的三维结构", "subject": "化学", "grade": "初中三年级", "mode": "interactive"}
    payload.update(overrides)
    response = client.post("/api/classroom/courses", json=payload)
    assert response.status_code == 200, response.text
    return response.json()["course"]


def job_view(client, job_id):
    response = client.get(f"/api/ai/jobs/{job_id}")
    assert response.status_code == 200, response.text
    return response.json()


def wait_for_status(client, job_id, status, tries=250, delay=0.03):
    job = None
    for _ in range(tries):
        job = job_view(client, job_id)
        if job["status"] == status:
            return job
        if job["status"] in ("failed", "cancelled") and job["status"] != status:
            pytest.fail("任务提前进入 %s（期望 %s）：%s" % (job["status"], status, job.get("error")))
        time.sleep(delay)
    pytest.fail("任务未进入 %s，当前 %s" % (status, job))


def generate_all(client, provider, course, *, scene_types, scene_count=3):
    response = client.post(f"/api/classroom/courses/{course['id']}/generate", json={
        "stage": "all", "provider_id": provider["id"], "model": "test-model",
        "scene_types": scene_types, "scene_count": scene_count,
    })
    assert response.status_code == 200, response.text
    return response.json()["job"]


def confirm_outline(client, job_id, outline):
    response = client.post(f"/api/ai/jobs/{job_id}/confirm-outline", json={
        "outline": json.dumps(outline, ensure_ascii=False),
    })
    assert response.status_code == 200, response.text


def run_generation(client, monkeypatch, *, outline, scene_types, interactive_payloads=None):
    provider = save_provider(client)
    course = create_course(client)
    fake = FakeModel(outline=outline, interactive_payloads=interactive_payloads).install(monkeypatch)
    job = generate_all(client, provider, course, scene_types=scene_types)
    wait_for_status(client, job["id"], "awaiting_outline")
    confirm_outline(client, job["id"], outline)
    done = wait_for_status(client, job["id"], "completed")
    detail = client.get(f"/api/classroom/courses/{course['id']}").json()
    return fake, done, detail


def user_id_of(client):
    return client.get("/api/auth/me").json()["id"]


def insert_scene(course_id, user_id, scene_type, order_index, title, content,
                 *, meta=None, status="ready"):
    now = db.utcnow()
    with db.connection() as con:
        return con.execute(
            "INSERT INTO cls_scenes(course_id,user_id,scene_type,order_index,title,content_json,meta_json,status,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (course_id, user_id, scene_type, order_index, title, cdb.json_dump(content),
             cdb.json_dump(meta or {}), status, now, now),
        ).lastrowid


def normalize_3d():
    content, warnings = interactive.normalize_generated_interactive(
        PAYLOAD_3D, title="水分子 3D 结构", widget="visualization3d",
    )
    assert warnings == [], warnings
    return content


# ---------------------------------------------------------------------------
# 1. 静态库资产（来源/许可/可安全内联）
# ---------------------------------------------------------------------------

def test_three_vendor_asset_exists_and_is_inline_safe():
    asset = VENDOR_DIR / "three.module.min.js"
    assert asset.is_file(), "缺少 web/vendor/interactive/three.module.min.js"
    raw = asset.read_bytes()
    text = raw.decode("utf-8")
    info = interactive.load_inline_library("three")
    assert info["version"] == "0.186.1"
    assert info["revision"] == "186"
    assert info["bytes"] == len(raw) and info["sha256"] == hashlib.sha256(raw).hexdigest()
    assert 600_000 <= len(raw) <= 1_500_000, "three 内联包体积异常：%d" % len(raw)
    lowered = text.lower()
    assert "</script" not in lowered, "库内含 </script 序列，无法安全内联"
    assert "<!--" not in lowered
    assert "eval(" not in lowered and "new Function" not in lowered, "库内含动态执行 API"
    assert "document.cookie" not in lowered and "localstorage" not in lowered
    assert "globalThis.THREE=" in text.replace(" ", ""), "库未暴露全局 THREE"


def test_three_vendor_license_and_provenance_documented():
    license_text = (VENDOR_DIR / "LICENSE").read_text(encoding="utf-8", errors="replace")
    assert "MIT License" in license_text
    assert "three.js authors" in license_text
    readme = (VENDOR_DIR / "README.md").read_text(encoding="utf-8")
    info = interactive.load_inline_library("three")
    assert "MIT" in readme
    assert info["version"] in readme
    assert info["sha256"] in readme
    assert "data-zhixu-lib" in readme and "IIFE" in readme
    assert (VENDOR_DIR / "build_three.py").is_file()


def test_three_vendor_asset_has_no_real_external_urls():
    """库源码只允许 w3.org 命名空间 URI（导出外链扫描零误报的契约）。"""
    text = interactive.load_inline_library("three")["text"]
    scan = text
    for uri in ("http://www.w3.org/", "https://www.w3.org/"):
        scan = scan.replace(uri, "")
    assert "http://" not in scan and "https://" not in scan
    for marker in ('<script src', "<link", "@import", 'src="http', "href=\"http"):
        assert marker not in scan


# ---------------------------------------------------------------------------
# 2. 注入正确性（先校验 → 再注入 → 内容不变 → 幂等）
# ---------------------------------------------------------------------------

def test_3d_html_injects_three_library_before_user_scripts():
    lib = interactive.load_inline_library("three")
    content = normalize_3d()
    html = content["html"]
    assert content["widget"] == "visualization3d"
    assert content["height"] == 520
    assert [item["name"] for item in content["libraries"]] == ["three"]
    assert content["libraries"][0]["sha256"] == lib["sha256"]
    # 注入不改变用户 HTML：等价于在 <meta charset> 之后插入一段库脚本，其余逐字节一致。
    assert html == THREE_HTML.replace(
        '<meta charset="utf-8">', '<meta charset="utf-8">' + lib["script"], 1)
    assert html.count(THREE_SCRIPT_OPEN) == 1
    # 库脚本在用户脚本之前（同步经典脚本先执行，THREE 才可用）。
    assert html.index(THREE_SCRIPT_OPEN) < html.index("<script>\n(function () {")
    # charset meta 仍在文档最前面（离线/srcdoc 打开时编码嗅探不失效）。
    assert html.index("charset=") < html.index(THREE_SCRIPT_OPEN)
    assert 700_000 < len(html.encode("utf-8")) < schema.MAX_INTERACTIVE3D_HTML_BYTES
    assert interactive.static_check_interactive_html(html, widget="visualization3d") == []


def test_injection_is_idempotent_for_import_roundtrip():
    content = normalize_3d()
    again, warnings = interactive.normalize_generated_interactive(
        json.loads(json.dumps(content, ensure_ascii=False)),
        title="水分子 3D 结构", widget="visualization3d",
    )
    assert warnings == []
    assert again["html"] == content["html"]
    assert again["libraries"] == content["libraries"]
    assert again["html"].count(THREE_SCRIPT_OPEN) == 1
    # schema 侧（导入兜底路径）也接受含内联库的 3D HTML，且按 widget 放宽体积上限。
    schema_content, schema_warnings = schema.normalize_interactive_content(content)
    assert schema_content["html"] == content["html"]
    assert schema_warnings == []


def test_3d_fragment_is_wrapped_then_injected():
    fragment = THREE_HTML.split("<body>")[1]
    content, warnings = interactive.normalize_generated_interactive(
        {"widget": "visualization3d", "html": fragment}, title="水分子片段", widget="visualization3d",
    )
    assert warnings == []
    html = content["html"]
    assert html.lower().startswith("<!doctype html>")
    assert THREE_SCRIPT_OPEN in html
    assert "<head>" in html and html.index(THREE_SCRIPT_OPEN) < html.index("<body>")


def test_3d_without_three_usage_does_not_inject_library():
    """3D 类型允许退化为原生 Canvas 2D（不强行注入 ~720KB 库）。"""
    content, warnings = interactive.normalize_generated_interactive(
        {"widget": "visualization3d", "height": 480, "html": GAME_HTML},
        title="2D 退化的 3D 页", widget="visualization3d",
    )
    assert warnings == []
    assert "libraries" not in content
    assert THREE_SCRIPT_OPEN not in content["html"]
    assert len(content["html"].encode("utf-8")) < schema.MAX_INTERACTIVE_HTML_BYTES


def test_detect_inline_libraries_by_usage_or_explicit_marker():
    assert interactive.detect_inline_libraries(THREE_HTML, "visualization3d") == ["three"]
    assert interactive.detect_inline_libraries(GAME_HTML, "visualization3d") == []
    assert interactive.detect_inline_libraries(GAME_HTML, "game") == []
    marker = "<!-- zhixu-lib: three -->" + GAME_HTML
    assert interactive.detect_inline_libraries(marker, "visualization3d") == ["three"]
    # 非 3D 类型不注入
    assert interactive.detect_inline_libraries(THREE_HTML, "simulation") == []


def test_strip_verified_libraries_returns_user_html_and_metadata():
    content = normalize_3d()
    stripped, found = interactive.strip_inline_libraries(content["html"])
    assert [item["name"] for item in found] == ["three"]
    assert "data-zhixu-lib=" not in stripped
    # 去掉库标记注释后，剩余内容与模型原始 HTML 逐字节一致。
    assert stripped.replace("<!--zhixu-inline-library:three@0.186.1-->", "") == THREE_HTML
    # 不是仓库内联库逐字节内容的片段不会被剔除
    fake = '<script data-zhixu-lib="three@0.186.1">var x = 1;</script>' + THREE_HTML
    stripped_fake, found_fake = interactive.strip_inline_libraries(fake)
    assert found_fake == []
    assert "data-zhixu-lib=" in stripped_fake


# ---------------------------------------------------------------------------
# 3. 安全边界：用户危险 API 仍拒 / 伪造库不豁免 / CSP 不变
# ---------------------------------------------------------------------------

DANGEROUS_SNIPPETS = (
    ("<script>fetch('/api/steal');</script>", "fetch"),
    ("<script>var f = new Function('return 1');</script>", "Function"),
    ("<script>eval('1+1');</script>", "eval"),
    ("<script>var x = parent.document.cookie;</script>", "父窗口"),
    ("<script>localStorage.setItem('token','x');</script>", "凭据"),
    ("<script>var x = new XMLHttpRequest();</script>", "XMLHttpRequest"),
    ("<script src=\"https://cdn.example.com/three.js\"></script>", "外链"),
)


@pytest.mark.parametrize("snippet,reason", DANGEROUS_SNIPPETS)
def test_3d_user_html_dangerous_apis_are_still_rejected(snippet, reason):
    bad = THREE_HTML.replace("</body>", snippet + "</body>")
    with pytest.raises(schema.ClassroomSchemaError) as excinfo:
        interactive.normalize_generated_interactive(
            {"widget": "visualization3d", "html": bad}, title="危险 3D", widget="visualization3d",
        )
    assert "安全校验" in str(excinfo.value)
    assert reason in str(excinfo.value)


def test_fake_library_fragment_is_not_whitelisted():
    fake = THREE_HTML.replace(
        "<script>",
        '<script data-zhixu-lib="three@0.186.1">eval("1");</script><script>', 1,
    )
    with pytest.raises(schema.ClassroomSchemaError) as excinfo:
        interactive.normalize_generated_interactive(
            {"widget": "visualization3d", "html": fake}, title="伪造库", widget="visualization3d",
        )
    message = str(excinfo.value)
    assert "eval" in message and "data-zhixu-lib" in message


def test_three_is_only_allowed_for_3d_widget():
    with pytest.raises(schema.ClassroomSchemaError) as excinfo:
        interactive.normalize_generated_interactive(
            {"widget": "simulation", "html": THREE_HTML}, title="错误类型", widget="simulation",
        )
    assert "visualization3d" in str(excinfo.value)


def test_three_cannot_be_defined_by_user_html():
    self_defined = THREE_HTML.replace(
        "<script>", "<script>var THREE = { Scene: function () {} };</script><script>", 1,
    )
    with pytest.raises(schema.ClassroomSchemaError) as excinfo:
        interactive.normalize_generated_interactive(
            {"widget": "visualization3d", "html": self_defined}, title="自建库", widget="visualization3d",
        )
    assert "THREE" in str(excinfo.value)


def test_csp_unchanged_no_unsafe_eval_and_sandbox_still_allow_scripts_only():
    expected = (
        "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
        "img-src data: blob:; font-src data:; media-src data: blob:; "
        "connect-src 'none'; frame-src 'none'; worker-src 'none'; object-src 'none'; "
        "base-uri 'none'; form-action 'none'"
    )
    assert interactive.INTERACTIVE_CSP == expected
    assert "unsafe-eval" not in interactive.INTERACTIVE_CSP
    assert interactive.IFRAME_SANDBOX == "allow-scripts"
    assert "allow-same-origin" not in interactive.IFRAME_SANDBOX


# ---------------------------------------------------------------------------
# 4. 体积限制：按 widget 分别限制，且都低于 DB 上限
# ---------------------------------------------------------------------------

def test_size_limits_are_widget_specific_and_below_db_cap():
    assert schema.MAX_INTERACTIVE_HTML_BYTES == 600 * 1024
    assert schema.MAX_INTERACTIVE3D_HTML_BYTES == 1_500_000
    assert schema.interactive_html_limit("visualization3d") == 1_500_000
    assert schema.interactive_html_limit("simulation") == 600 * 1024
    assert schema.MAX_INTERACTIVE3D_HTML_BYTES < schema.MAX_HTML_BYTES
    assert interactive.MAX_INTERACTIVE3D_HTML_BYTES == schema.MAX_INTERACTIVE3D_HTML_BYTES


def test_3d_injected_html_fits_limit_but_not_plain_widget_limit():
    content = normalize_3d()
    html = content["html"]
    assert len(html.encode("utf-8")) < schema.MAX_INTERACTIVE3D_HTML_BYTES
    assert interactive.static_check_interactive_html(html, widget="visualization3d") == []
    problems = interactive.static_check_interactive_html(html, widget="simulation")
    assert any("超过" in item for item in problems)


def test_model_raw_3d_html_is_still_limited_to_600kb_before_injection():
    padded = THREE_HTML.replace(
        "</body>",
        "<div>" + "x" * (interactive.MAX_INTERACTIVE_HTML_BYTES + 10) + "</div></body>",
    )
    with pytest.raises(schema.ClassroomSchemaError) as excinfo:
        interactive.normalize_generated_interactive(
            {"widget": "visualization3d", "html": padded}, title="超大 3D", widget="visualization3d",
        )
    assert "体积" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 5. 真实生成链路（mock 模型）：3D 注入 / code / game 不注入
# ---------------------------------------------------------------------------

def test_3d_scene_generation_injects_three_and_serves_sandbox_html(client, monkeypatch):
    fake, done, detail = run_generation(
        client, monkeypatch, outline=OUTLINE_3D,
        scene_types=["slide", "interactive", "quiz"], interactive_payloads=[PAYLOAD_3D],
    )
    assert done["result"]["failed_scene_ids"] == []
    assert done["result"]["generated_scene_count"] == 3
    assert fake.count("interactive") == 1
    prompt = next(text for text in fake.prompts if "交互场景 HTML 生成器" in text)
    assert "visualization3d" in prompt and "3D 可视化" in prompt
    assert "可用内联库与 API 清单" in prompt
    assert "THREE.WebGLRenderer" in prompt and "data-zhixu-lib" in prompt
    assert "1.5MB" in prompt and "unsafe-eval" not in prompt
    assert "不要" in prompt and "粘贴 three 源码" in prompt

    scene = next(item for item in detail["scenes"] if item["type"] == "interactive")
    assert scene["status"] == "ready"
    assert scene["meta"]["placeholder"] is False
    assert scene["meta"]["interactive_checked"] is True
    assert scene["meta"]["widget"] == "visualization3d"
    content = scene["content"]
    assert content["widget"] == "visualization3d"
    lib = interactive.load_inline_library("three")
    assert content["libraries"][0]["name"] == "three"
    assert content["libraries"][0]["sha256"] == lib["sha256"]
    assert THREE_SCRIPT_OPEN in content["html"]
    assert len(content["html"].encode("utf-8")) > 700_000
    assert scene["meta"]["html_bytes"] == len(content["html"].encode("utf-8"))

    response = client.get(f"/api/classroom/scenes/{scene['id']}/html")
    assert response.status_code == 200, response.text
    assert THREE_SCRIPT_OPEN in response.text
    assert len(response.text.encode("utf-8")) > 700_000
    assert response.headers["content-security-policy"] == interactive.INTERACTIVE_CSP
    assert "unsafe-eval" not in response.headers["content-security-policy"]
    assert response.headers["x-frame-options"] == "SAMEORIGIN"


def test_3d_generation_retries_after_dangerous_user_html_then_succeeds(client, monkeypatch):
    bad = {"widget": "visualization3d",
           "html": THREE_HTML.replace("</body>", "<script>fetch('/x');</script></body>")}
    fake, done, detail = run_generation(
        client, monkeypatch, outline=OUTLINE_3D,
        scene_types=["slide", "interactive", "quiz"], interactive_payloads=[bad, PAYLOAD_3D],
    )
    assert done["result"]["failed_scene_ids"] == []
    assert fake.count("interactive") == 2  # 首次校验失败 → 带纠错提示重试一次
    assert any("未通过校验" in prompt or "格式修正" in prompt for prompt in fake.prompts)
    scene = next(item for item in detail["scenes"] if item["type"] == "interactive")
    assert scene["status"] == "ready"
    assert "libraries" in scene["content"]


def test_code_and_game_widgets_generate_without_any_inline_library(client, monkeypatch):
    fake, done, detail = run_generation(
        client, monkeypatch, outline=OUTLINE_CODE_GAME,
        scene_types=["slide", "interactive"], interactive_payloads=[PAYLOAD_CODE, PAYLOAD_GAME],
    )
    assert done["result"]["failed_scene_ids"] == []
    assert fake.count("interactive") == 2
    prompts = [text for text in fake.prompts if "交互场景 HTML 生成器" in text]
    code_prompt = next(text for text in prompts if "在线编程" in text)
    game_prompt = next(text for text in prompts if "互动游戏" in text)
    assert "不注入任何第三方库" in code_prompt and "不执行任意代码" in code_prompt
    assert "THREE.WebGLRenderer" not in code_prompt
    assert "600KB" in code_prompt and "600KB" in game_prompt
    assert "Canvas 2D" in game_prompt

    scenes = [item for item in detail["scenes"] if item["type"] == "interactive"]
    by_widget = {item["content"]["widget"]: item for item in scenes}
    assert set(by_widget) == {"code", "game"}
    for widget, scene in by_widget.items():
        assert scene["status"] == "ready", widget
        assert scene["meta"]["interactive_checked"] is True
        content = scene["content"]
        assert "libraries" not in content, widget
        assert "data-zhixu-lib=" not in content["html"], widget
        assert len(content["html"].encode("utf-8")) < schema.MAX_INTERACTIVE_HTML_BYTES
        response = client.get(f"/api/classroom/scenes/{scene['id']}/html")
        assert response.status_code == 200, widget
        assert "unsafe-eval" not in response.headers["content-security-policy"]


def test_3d_generation_failure_is_recorded_not_faked(client, monkeypatch):
    bad = {"widget": "visualization3d",
           "html": THREE_HTML.replace("</body>", "<script>eval('1');</script></body>")}
    fake, done, detail = run_generation(
        client, monkeypatch, outline=OUTLINE_3D,
        scene_types=["slide", "interactive", "quiz"], interactive_payloads=[bad, bad],
    )
    assert len(done["result"]["failed_scene_ids"]) == 1
    assert fake.count("interactive") == 2
    scene = next(item for item in detail["scenes"] if item["type"] == "interactive")
    assert scene["status"] == "failed"
    assert "安全校验" in scene["meta"].get("error", "")
    assert scene["content"] == {}
    assert client.get(f"/api/classroom/scenes/{scene['id']}/html").status_code == 404


# ---------------------------------------------------------------------------
# 6. 导入/导出兼容（不重复注入、外链扫描零误报）
# ---------------------------------------------------------------------------

def test_importer_revalidation_accepts_injected_3d_content_without_double_injection():
    from app.classroom import importer

    content = normalize_3d()
    normalized, warnings = importer._validate_scene_content("interactive", content, "水分子 3D 结构")
    assert normalized["html"] == content["html"]
    assert normalized["libraries"] == content["libraries"]
    assert normalized["html"].count(THREE_SCRIPT_OPEN) == 1
    assert not any("兼容模式" in warning for warning in warnings), warnings


def test_package_export_wrapper_has_no_external_reference_markers():
    from app.classroom import exports

    content = normalize_3d()
    wrapper = exports._interactive_package_html(
        {"title": "分子结构 3D 互动课"}, {"title": "水分子 3D 结构"}, content, position=2, total=3,
    )
    assert exports._external_reference_problems(wrapper) == []
    assert "three@0.186.1" in wrapper


def test_normalized_3d_scene_is_served_with_owner_isolation(client, second_client):
    course = create_course(client)
    uid = user_id_of(client)
    content = normalize_3d()
    scene_id = insert_scene(course["id"], uid, "interactive", 1, "水分子 3D 结构", content,
                            meta={"interactive_checked": True, "widget": "visualization3d"})
    response = client.get(f"/api/classroom/scenes/{scene_id}/html")
    assert response.status_code == 200
    assert second_client.get(f"/api/classroom/scenes/{scene_id}/html").status_code == 404
    from app.main import app

    anon = TestClient(app)
    assert anon.get(f"/api/classroom/scenes/{scene_id}/html").status_code == 401
