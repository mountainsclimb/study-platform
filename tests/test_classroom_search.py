"""P3 联网搜索进课堂生成/导出验收（``task-33``）。

覆盖：
1. ``GET /api/classroom/meta`` 的 ``search`` 能力位（有无 DeepSeek 连接）；
2. ``POST /courses/{id}/generate`` 的 ``web_search`` / ``web_search_limit`` 参数注入与准入上限；
3. mock ``app.ai.search.web_search`` 后：检索 query 拼接、大纲 prompt 含 URL、``outline.sources``
   与 ``course.config.web_search`` 落库、场景 prompt 带来源摘要、任务 summary/结果记录来源数；
4. 无搜索连接 / 搜索失败 / 未开启 → 降级（不报错、明确标注未使用资料、不伪造来源）；
5. 导出含来源：PPTX 末页「参考来源」、离线 HTML 文末列表、课程 ZIP ``sources.json``、
   教学资源包 README。

不联网：``_call_model`` 与 ``app.ai.search.web_search`` 全部替换为假实现（搜索连接仍走真实 DB 查询）。
"""
import asyncio
import json
import time
from types import SimpleNamespace

from fastapi import HTTPException
import pytest

from app import ai, db
from app.ai import search as ai_search
from app.classroom import generate, prompts

# ---------------------------------------------------------------------------
# 假模型与公共辅助
# ---------------------------------------------------------------------------

OUTLINE_RAW = {
    "title": "光合作用入门",
    "description": "面向初中生物的光合作用原理入门课。",
    "scenes": [
        {"title": "为什么植物离不开光", "type": "slide", "outline_points": ["生活现象", "光合作用定义", "学习目标"]},
        {"title": "课堂小测", "type": "quiz", "outline_points": ["场所", "原料"],
         "quiz": {"question_count": 2, "difficulty": "medium"}},
        {"title": "原料与产物", "type": "slide", "outline_points": ["水", "二氧化碳", "叶绿体"]},
    ],
}

SLIDE_RAW = {
    "elements": [
        {"id": "text_001", "type": "text", "left": 60, "top": 50, "width": 880, "height": 76,
         "content": '<p style="font-size:32px;"><strong>光合作用</strong></p>',
         "defaultFontName": "", "defaultColor": "#333333"},
        {"id": "text_002", "type": "text", "left": 80, "top": 160, "width": 840, "height": 130,
         "content": '<p style="font-size:20px;">• 水</p><p style="font-size:20px;">• 二氧化碳</p>',
         "defaultFontName": "", "defaultColor": "#333333"},
    ]
}

QUIZ_RAW = [
    {"id": "q1", "type": "single", "question": "光合作用发生的场所是？",
     "options": [{"label": "叶绿体", "value": "A"}, {"label": "线粒体", "value": "B"}],
     "answer": ["A"], "analysis": "叶绿体是场所。", "points": 10},
    {"id": "q2", "type": "multiple", "question": "光合作用的原料包括？（多选）",
     "options": [{"label": "水", "value": "A"}, {"label": "氧气", "value": "B"}, {"label": "二氧化碳", "value": "C"}],
     "answer": ["A", "C"], "analysis": "水和二氧化碳是原料。", "points": 10},
]

ACTIONS_RAW = [
    {"type": "action", "name": "spotlight", "params": {"elementId": "text_001"}},
    {"type": "text", "content": "这一页我们来看光合作用。"},
]

_MARKERS = (
    ("课程大纲生成器", "outline"),
    ("幻灯片内容生成器", "slide"),
    ("测验内容生成器", "quiz"),
    ("课堂旁白与动作生成器", "actions"),
)

#: mock 的联网搜索返回（真实结构：title/url/site_name/summary/date/authority）。
SEARCH_RESULTS = [
    {"title": "光合作用的过程与影响因素", "url": "https://www.example.com/photosynthesis",
     "site": "示例百科", "date": "2026-01-05", "summary": "光合作用需要水与二氧化碳，在叶绿体中进行。"},
    {"title": "光照强度对光合速率的影响", "url": "https://edu.example.org/light",
     "site": "教育研究院", "date": "2025-11-20", "summary": "光照增强时速率上升，达到光饱和点后不再上升。"},
    {"title": "初中生物：光合作用教学设计", "url": "https://teach.example.net/lesson",
     "site": "教学资源网", "date": "2026-03-01", "summary": "从生活现象导入，结合实验观察。"},
    {"title": "光合作用的原料与产物", "url": "https://science.example.cn/raw",
     "site": "科学网", "date": "2026-02-14", "summary": "原料是水和二氧化碳，产物是有机物和氧气。"},
]

SEARCH_URLS = [item["url"] for item in SEARCH_RESULTS]


class FakeModel:
    """按 prompt 标记返回固定 JSON 的假模型（与既有课堂测试同一套产物）。"""

    def __init__(self):
        self.payloads = {
            "outline": json.dumps(OUTLINE_RAW, ensure_ascii=False),
            "slide": json.dumps(SLIDE_RAW, ensure_ascii=False),
            "quiz": json.dumps(QUIZ_RAW, ensure_ascii=False),
            "actions": json.dumps(ACTIONS_RAW, ensure_ascii=False),
        }
        self.calls = []
        self.prompts = []
        self.kinds = []

    def classify(self, prompt):
        for marker, kind in _MARKERS:
            if marker in prompt:
                return kind
        raise AssertionError("未知 prompt：%s" % str(prompt)[:200])

    def install(self, monkeypatch):
        async def fake_call(provider, model, prompt, images=None, json_output=False, token_limit=5000, **kwargs):
            kind = self.classify(prompt)
            self.calls.append(kind)
            self.kinds.append(kind)
            self.prompts.append(prompt)
            return {"content": self.payloads[kind], "model": model,
                    "provider_name": "mock", "usage": {"total_tokens": 12}}

        monkeypatch.setattr(ai, "_call_model", fake_call)
        return self

    def prompts_of(self, kind):
        return [prompt for prompt, item in zip(self.prompts, self.kinds) if item == kind]


def install_search(monkeypatch, *, results=None, fail=None):
    """替换 ``app.ai.search.web_search``（连接查询仍走真实 DB，便于验证有无 DeepSeek 连接）。"""
    calls = []

    async def fake_web_search(provider, query, limit=ai_search.DEFAULT_LIMIT, **kwargs):
        calls.append({"provider": provider, "query": query, "limit": limit})
        if fail is not None:
            raise fail
        selected = (results if results is not None else SEARCH_RESULTS)[: int(limit)]
        return {"query": query, "results": selected, "provider": "mock-deepseek",
                "endpoint": "/v1/tools/search"}

    monkeypatch.setattr(ai_search, "web_search", fake_web_search)
    return calls


def save_provider(client, *, name="课堂搜索连接", base_url="https://api.deepseek.com/v1",
                  models=("deepseek-chat",)):
    response = client.post("/api/providers", json={
        "name": name, "kind": "openai", "base_url": base_url,
        "api_key": "sk-classroom-search-unit-test-123456", "models": list(models),
    })
    assert response.status_code == 200, response.text
    return response.json()


def create_course(client, **overrides):
    payload = {"topic": "光合作用", "subject": "生物", "grade": "初中二年级"}
    payload.update(overrides)
    response = client.post("/api/classroom/courses", json=payload)
    assert response.status_code == 200, response.text
    return response.json()["course"]


def user_id_of(client):
    return client.get("/api/auth/me").json()["id"]


def start_generate(client, course, provider, **overrides):
    payload = {
        "stage": "all", "provider_id": provider["id"], "model": "deepseek-chat",
        "scene_count": 3,
    }
    payload.update(overrides)
    response = client.post(f"/api/classroom/courses/{course['id']}/generate", json=payload)
    assert response.status_code == 200, response.text
    return response.json()["job"]


def wait_for_status(client, job_id, status, tries=300, delay=0.03):
    job = None
    for _ in range(tries):
        response = client.get(f"/api/ai/jobs/{job_id}")
        assert response.status_code == 200, response.text
        job = response.json()
        if job["status"] == status:
            return job
        if job["status"] in ("failed", "cancelled") and status not in (job["status"],):
            pytest.fail(f"任务提前进入 {job['status']}（期望 {status}）：{job.get('error')}")
        time.sleep(delay)
    pytest.fail(f"任务未进入 {status}，当前 {job}")


def confirm_outline(client, job, outline):
    response = client.post(f"/api/ai/jobs/{job['id']}/confirm-outline", json={
        "outline": json.dumps(outline, ensure_ascii=False),
    })
    assert response.status_code == 200, response.text


def generate_with_search(client, monkeypatch, *, limit=4, requirements="重点讲清原料与产物"):
    """跑通「联网检索 → 大纲暂停 → 确认 → 场景完成」，返回 (course, provider, fake, search_calls)。"""
    provider = save_provider(client)
    course = create_course(client, requirements=requirements)
    fake = FakeModel().install(monkeypatch)
    calls = install_search(monkeypatch)
    job = start_generate(client, course, provider, web_search=True, web_search_limit=limit)
    paused = wait_for_status(client, job["id"], "awaiting_outline")
    confirm_outline(client, job, paused["result"]["outline"])
    done = wait_for_status(client, job["id"], "completed")
    return course, provider, fake, calls, paused, done


# ---------------------------------------------------------------------------
# 1. /meta.search 能力位
# ---------------------------------------------------------------------------

def test_meta_search_capability_reflects_deepseek_connection(client):
    body = client.get("/api/classroom/meta").json()
    assert body["search"]["available"] is False
    assert body["search"]["provider"] is None
    assert "DeepSeek" in body["search"]["hint"]

    provider = save_provider(client, name="我的 DeepSeek")
    body = client.get("/api/classroom/meta").json()
    assert body["search"]["available"] is True
    assert body["search"]["provider"] == "我的 DeepSeek"
    assert body["search"]["provider_id"] == provider["id"]
    # 其它能力位保持既有字段（增量不破坏契约）
    assert body["enabled"] is True and "images" in body and "interactive_mode" in body


# ---------------------------------------------------------------------------
# 2. 生成参数注入 + prompt + 落库 + 场景来源 + 任务摘要
# ---------------------------------------------------------------------------

def test_generate_with_web_search_injects_sources_and_persists(client, monkeypatch):
    provider = save_provider(client)
    course = create_course(client, requirements="重点讲清原料与产物，多举生活例子")
    fake = FakeModel().install(monkeypatch)
    calls = install_search(monkeypatch)

    job = start_generate(client, course, provider, web_search=True, web_search_limit=4)
    paused = wait_for_status(client, job["id"], "awaiting_outline")

    # 2.1 检索 query = 主题 + 学科/年级 + 补充要求关键词，≤300 字，条数按参数
    assert len(calls) == 1, calls
    query = calls[0]["query"]
    assert calls[0]["limit"] == 4
    for marker in ("光合作用", "生物", "初中二年级", "重点讲清原料与产物"):
        assert marker in query, query
    assert len(query) <= ai_search.MAX_QUERY_CHARS

    # 2.2 大纲 prompt 注入来源块（带编号/链接/不得编造来源规则）
    assert fake.kinds[0] == "outline"
    outline_prompt = fake.prompts[0]
    for url in SEARCH_URLS[:4]:
        assert url in outline_prompt, url
    assert "只能引用这里出现的事实与链接" in outline_prompt
    assert "不得编造来源" in outline_prompt
    assert "待核实" in outline_prompt

    # 2.3 任务结果与课程落库结构
    result = paused["result"]
    assert result["source_count"] == 4
    assert result["web_search"]["enabled"] is True
    assert result["web_search"]["used"] is True
    assert result["web_search"]["limit"] == 4
    assert result["web_search"]["query"] == query
    sources = result["outline"]["sources"]
    assert [item["url"] for item in sources] == SEARCH_URLS[:4]
    assert [item["index"] for item in sources] == [1, 2, 3, 4]
    assert sources[0]["title"] == SEARCH_RESULTS[0]["title"]
    assert sources[0]["site"] == SEARCH_RESULTS[0]["site"]
    assert sources[0]["date"] == SEARCH_RESULTS[0]["date"]
    assert "水与二氧化碳" in sources[0]["summary"]

    detail = client.get(f"/api/classroom/courses/{course['id']}").json()
    stored_sources = detail["course"]["outline"]["sources"]
    assert [item["url"] for item in stored_sources] == SEARCH_URLS[:4]
    config = detail["course"]["config"]["web_search"]
    assert config["enabled"] is True and config["used"] is True
    assert config["limit"] == 4 and config["count"] == 4
    assert config["query"] == query
    assert "skipped" in config

    # 2.4 确认大纲后：场景 prompt 带来源摘要（每场景 ≤3000 字），结果 summary 记录来源数
    confirm_outline(client, job, result["outline"])
    done = wait_for_status(client, job["id"], "completed")
    scene_prompts = [prompt for prompt, kind in zip(fake.prompts, fake.kinds) if kind in ("slide", "quiz")]
    assert scene_prompts, fake.kinds
    for prompt in scene_prompts:
        assert SEARCH_URLS[0] in prompt
        assert "参考来源" in prompt
        block = prompt.split("## 参考来源（可选引用）", 1)[1]
        assert len(block) <= generate.SCENE_SOURCES_MAX_CHARS + 200
    assert done["result"]["source_count"] == 4
    assert done["result"]["web_search"]["count"] == 4
    assert "4 条联网资料" in done["result"]["summary"]

    # 课程 config 的来源配置在确认后仍保留
    detail = client.get(f"/api/classroom/courses/{course['id']}").json()
    assert detail["course"]["config"]["web_search"]["count"] == 4
    assert detail["course"]["outline"]["sources"]


def test_web_search_limit_defaults_and_scene_stage_does_not_research(client, monkeypatch):
    provider = save_provider(client)
    course = create_course(client)
    fake = FakeModel().install(monkeypatch)
    calls = install_search(monkeypatch)

    # 不传 web_search → 默认关闭，不检索；web_search_limit 不传时落库默认 5
    job = start_generate(client, course, provider, scene_count=3)
    paused = wait_for_status(client, job["id"], "awaiting_outline")
    assert calls == []
    assert paused["result"]["source_count"] == 0
    assert paused["result"]["web_search"]["used"] is False
    assert "未开启" in paused["result"]["web_search"]["skipped"]
    detail = client.get(f"/api/classroom/courses/{course['id']}").json()
    assert detail["course"]["config"]["web_search"]["limit"] == generate.DEFAULT_WEB_SEARCH_LIMIT
    assert "联网搜索资料" not in fake.prompts[0]

    # 确认后场景阶段不重复检索（calls 仍为空）
    confirm_outline(client, job, paused["result"]["outline"])
    wait_for_status(client, job["id"], "completed")
    assert calls == []

    # scenes 阶段（已有大纲）即使带 web_search=true 也不会重新检索
    calls2 = install_search(monkeypatch)
    second = start_generate(client, course, provider, stage="scenes", web_search=True,
                            web_search_limit=3, scene_ids=None)
    wait_for_status(client, second["id"], "completed")
    assert calls2 == []


# ---------------------------------------------------------------------------
# 3. 降级：无连接 / 搜索失败 / 参数非法
# ---------------------------------------------------------------------------

def test_generate_without_search_connection_degrades_without_error(client, monkeypatch):
    provider = save_provider(client, name="普通 OpenAI 连接", base_url="https://api.openai.com/v1",
                             models=("gpt-test",))
    course = create_course(client)
    fake = FakeModel().install(monkeypatch)
    calls = install_search(monkeypatch)
    job = start_generate(client, course, provider, model="gpt-test", web_search=True,
                         web_search_limit=5)
    paused = wait_for_status(client, job["id"], "awaiting_outline")

    # 没有可用搜索连接：不调用 web_search、不报错、大纲正常生成
    assert calls == []
    assert paused["result"]["outline"]["scenes"]
    assert paused["result"]["source_count"] == 0
    skipped = paused["result"]["web_search"]["skipped"]
    assert "没有可用的联网搜索连接" in skipped
    assert "DeepSeek" in skipped
    assert "联网搜索资料" not in fake.prompts[0]

    detail = client.get(f"/api/classroom/courses/{course['id']}").json()
    config = detail["course"]["config"]["web_search"]
    assert config["enabled"] is True and config["used"] is False and config["count"] == 0
    assert config["skipped"] == skipped
    assert not detail["course"]["outline"].get("sources")

    confirm_outline(client, job, paused["result"]["outline"])
    done = wait_for_status(client, job["id"], "completed")
    assert done["result"]["failed_scene_ids"] == []
    assert "未使用联网资料" in done["result"]["summary"]


def test_search_failure_degrades_and_marks_reason(client, monkeypatch):
    provider = save_provider(client)
    course = create_course(client)
    FakeModel().install(monkeypatch)
    install_search(monkeypatch, fail=ai_search.SearchUnavailable("搜索服务返回 HTTP 502"))
    job = start_generate(client, course, provider, web_search=True, web_search_limit=3)
    paused = wait_for_status(client, job["id"], "awaiting_outline")
    assert paused["result"]["outline"]["scenes"]
    assert paused["result"]["source_count"] == 0
    skipped = paused["result"]["web_search"]["skipped"]
    assert "联网检索失败" in skipped and "502" in skipped
    detail = client.get(f"/api/classroom/courses/{course['id']}").json()
    assert detail["course"]["config"]["web_search"]["used"] is False
    assert not detail["course"]["outline"].get("sources")


def test_web_search_params_validation(client):
    provider = save_provider(client)
    course = create_course(client)
    base = {"stage": "all", "provider_id": provider["id"], "model": "deepseek-chat"}

    # HTTP 参数上限：1-8
    assert client.post(f"/api/classroom/courses/{course['id']}/generate",
                       json={**base, "web_search_limit": 0}).status_code == 422
    assert client.post(f"/api/classroom/courses/{course['id']}/generate",
                       json={**base, "web_search_limit": 9}).status_code == 422

    # 直接经 /api/ai/jobs 提交时的准入校验（validate_course_job）
    user = {"id": user_id_of(client)}
    for params in (
        {"course_id": course["id"], "stage": "all", "web_search": "yes"},
        {"course_id": course["id"], "stage": "all", "web_search": True, "web_search_limit": 0},
        {"course_id": course["id"], "stage": "all", "web_search": True, "web_search_limit": 99},
    ):
        data = SimpleNamespace(params=params)
        with pytest.raises(HTTPException) as excinfo:
            generate.validate_course_job(data, user)
        assert excinfo.value.status_code == 422
    # 合法值不抛错
    generate.validate_course_job(
        SimpleNamespace(params={"course_id": course["id"], "stage": "all",
                                "web_search": True, "web_search_limit": 8}), user,
    )


def test_search_helpers_limits_and_query_truncation():
    # query 拼接 ≤300 字
    long_requirements = "重点讲清光合作用的原料与产物，" * 40
    course = {"topic": "光合作用与细胞呼吸", "subject": "生物", "grade": "初中二年级"}
    query = generate.build_web_search_query(course, long_requirements)
    assert query.startswith("光合作用与细胞呼吸")
    assert "生物" in query and "初中二年级" in query
    assert len(query) <= ai_search.MAX_QUERY_CHARS

    # 来源清洗：缺 url/title 的条目被丢弃，index 重排；摘要截断
    sources = generate._sources_from_search({"results": [
        {"title": "", "url": "", "site": "空"},
        {"title": "有效来源", "url": "https://example.com/a", "site": "站点",
         "date": "2026-01-01", "summary": "长" * 5000},
    ]})
    assert len(sources) == 1
    assert sources[0]["index"] == 1 and sources[0]["url"] == "https://example.com/a"
    assert len(sources[0]["summary"]) <= generate.SOURCE_SUMMARY_MAX_CHARS

    # 场景来源 prompt ≤3000 字（20 条也不超）
    many = [{"index": index, "title": "来源 %d" % index, "url": "https://example.com/%d" % index,
             "site": "站点", "date": "2026-01-01", "summary": "摘要" * 80}
            for index in range(1, 21)]
    text = generate.format_sources_for_prompt(many)
    assert text and len(text) <= generate.SCENE_SOURCES_MAX_CHARS + 100
    assert "https://example.com/1" in text


# ---------------------------------------------------------------------------
# 4. 导出含来源：PPTX / 离线 HTML / ZIP / 教学资源包
# ---------------------------------------------------------------------------

def test_exports_include_sources_after_web_search_generation(client, monkeypatch):
    import io
    import zipfile

    from pptx import Presentation

    course, _provider, _fake, _calls, _paused, _done = generate_with_search(
        client, monkeypatch, limit=3,
    )

    def export_bytes(course_id, fmt):
        response = client.get(f"/api/classroom/courses/{course_id}/export?format={fmt}")
        assert response.status_code == 200, response.text
        return response

    # 4.1 PPTX：末页「参考来源」，标题 + 链接纯文本
    pptx_response = export_bytes(course["id"], "pptx")
    prs = Presentation(io.BytesIO(pptx_response.content))
    all_text = "\n".join(
        shape.text_frame.text
        for slide in prs.slides
        for shape in slide.shapes
        if getattr(shape, "has_text_frame", False)
    )
    assert "参考来源" in all_text
    for item in SEARCH_RESULTS[:3]:
        assert item["title"] in all_text
        assert item["url"] in all_text

    # 4.2 离线 HTML：文末来源列表（静态、纯文本链接）
    html = export_bytes(course["id"], "html").text
    assert "参考来源（3 条" in html
    assert 'class="source-list"' in html
    for item in SEARCH_RESULTS[:3]:
        assert item["title"] in html and item["url"] in html
    # 来源只是文本引用，没有外链资源标签
    assert "<script src" not in html.lower()
    assert "src=\"http" not in html.lower()

    # 4.3 课程 ZIP：sources.json + manifest.sourceCount/capabilities
    with zipfile.ZipFile(io.BytesIO(export_bytes(course["id"], "zip").content)) as archive:
        names = set(archive.namelist())
        assert "sources.json" in names
        payload = json.loads(archive.read("sources.json").decode("utf-8"))
        assert payload["count"] == 3
        assert [item["url"] for item in payload["sources"]] == SEARCH_URLS[:3]
        manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
        assert manifest["sourceCount"] == 3
        assert "web-search" in manifest["capabilities"]
        # course.json 快照同样带来源（course.json 顶层即课程快照，来源在 outline.sources）
        snapshot = json.loads(archive.read("course.json").decode("utf-8"))
        assert [item["url"] for item in snapshot["outline"]["sources"]] == SEARCH_URLS[:3]

    # 4.4 教学资源包 README 列出来源
    with zipfile.ZipFile(io.BytesIO(export_bytes(course["id"], "package").content)) as archive:
        readme = archive.read("README.txt").decode("utf-8")
        assert "参考来源" in readme
        assert "DeepSeek" in readme
        for item in SEARCH_RESULTS[:3]:
            assert item["title"] in readme and item["url"] in readme
        # 资源包 PPTX 也有来源页
        prs = Presentation(io.BytesIO(archive.read("course.pptx")))
        package_text = "\n".join(
            shape.text_frame.text
            for slide in prs.slides
            for shape in slide.shapes
            if getattr(shape, "has_text_frame", False)
        )
        assert "参考来源" in package_text


def test_exports_without_sources_have_no_source_page(client, monkeypatch):
    """没有联网来源时不加来源页/来源列表（保持既有导出结构与离线断言）。"""
    provider = save_provider(client)
    course = create_course(client)
    FakeModel().install(monkeypatch)
    job = start_generate(client, course, provider, scene_count=3)
    paused = wait_for_status(client, job["id"], "awaiting_outline")
    confirm_outline(client, job, paused["result"]["outline"])
    wait_for_status(client, job["id"], "completed")

    from pptx import Presentation
    import io
    import zipfile

    response = client.get(f"/api/classroom/courses/{course['id']}/export?format=pptx")
    assert response.status_code == 200
    all_text = "\n".join(
        shape.text_frame.text
        for slide in Presentation(io.BytesIO(response.content)).slides
        for shape in slide.shapes
        if getattr(shape, "has_text_frame", False)
    )
    assert "参考来源" not in all_text

    html = client.get(f"/api/classroom/courses/{course['id']}/export?format=html").text
    assert "参考来源" not in html
    assert "https://" not in html and "http://" not in html

    with zipfile.ZipFile(io.BytesIO(
        client.get(f"/api/classroom/courses/{course['id']}/export?format=zip").content
    )) as archive:
        assert "sources.json" not in set(archive.namelist())
        manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
        assert manifest["sourceCount"] == 0
        assert "web-search" not in manifest["capabilities"]
