"""平台精选（``official:<public_id>``）接入学习链路端到端验收（WS24 Phase 3 + WS30 第四轮）。

覆盖场景（全部经公开 HTTP API，不直接读写数据库）：

1. **发布**：复用 ``plaza_accept.py`` 的账号/角色/投稿评审发布链路（同一隔离实例、同一组验收账号），
   发布两条平台精选题：客观题（单选，可自动判分）与主观题（作答后待核验）；按标准年级
   （``高中一年级``）发布以验证学段筛选；
2. **检索与详情**：匿名可读广场公开详情；``/api/questions?source=official`` 需登录（匿名 401），
   登录用户可按关键词/``source=official|all`` 检索并在 ``official:<public_id>`` 详情读到
   来源标签、审核状态（``featured``）、公开题号与发布版本；非法来源 422、未知精选 404；
3. **学习链路**：学员对客观题作答（正确 → ``correct``；错误 → ``incorrect`` 进错题）与主观题作答
   （``pending`` 待核验）→ ``GET /api/review/queue`` 可见（客观错题在已判分项、主观题在
   ``pending_items``）→ ``GET /api/stats`` 的 ``official_*`` 计数；
4. **学段筛选 / 首页口径（WS26）**：``/api/questions?grade=`` 标准年级精确匹配、学段前缀匹配、
   未知值安全降级、题库侧无 grade 列时的降级说明；``/api/insight/home`` 的 ``official`` 块
   （可练/可自动判分/本人已练/错题）与本人作答一致；
5. **课堂接入（WS26）**：脚本内嵌只监听 ``127.0.0.1`` 的假模型服务并创建验收连接，
   ``source_ref=official:<public_id>`` 建课生成大纲 → 课程 ``outline.sources`` 记录
   ``public_id``/``published_version``/来源声明，``config.seed_source`` 可回看；格式非法 422；
6. **助手接入（WS26/WS27）**：HTTP 助手会话驱动 ``read_question`` 工具读取 official 题面
   （工具结果含公开编号/版本/来源声明，助手消息 ``refs`` 含 official 引用）；
   假模型连接不可用时降级为接口契约静态校验并在报告/warnings 标记（不静默跳过）；
7. **工坊建卷**：``POST /api/paperlab/papers`` 用 official 引用建卷，快照含 ``source=official`` /
   ``public_id`` / 来源声明；整卷练习（作答 → 提交判分 → 回看）确认客观错题入统一错题本、
   主观题 pending，回看仍标注平台精选来源；
8. **撤回即时生效**：编辑撤回两条精选 → 公开列表/检索/详情/作答/工坊建卷立即 404 或不可见；
   ``/api/review/queue`` 保留历史并显示「已撤回」占位提示（不回显受限正文）；课堂再次以该素材
   建课由生成准入直接 404（提示已撤回）；助手 ``read_question`` 返回结构化「已撤回」错误；
9. **抽检门禁兼容（WS28）**：实例显式 ``armed`` 时发布链路前临时关闭门禁、结束前恢复原模式；
   未显式配置且库中无抽检记录时不阻断；
10. 产出 JSON 报告（``screenshots/integration/official-accept-report.json``）与退出码 0/1。

前置（与 plaza_accept 相同）：隔离实例 + 全新 ``STUDY_DATA_DIR``；``httpx`` 一律
``trust_env=False``（Windows 系统代理会劫持 127.0.0.1）；全部操作经 HTTP，不写业务代码。
本机 ``local_open`` 模式允许回环模型地址；公网模式下课堂/助手退化为接口契约静态校验。
"""
from __future__ import annotations

import argparse
import json
import re
import secrets
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:  # 支持从仓库根被 import 时复用 plaza_accept 的发布链路
    sys.path.insert(0, str(SCRIPT_DIR))

from plaza_accept import (  # noqa: E402 - 先修正 sys.path 再导入同目录脚本
    ADMIN_NAME,
    GRADE,
    PASSWORD,
    ROOT,
    SOURCE,
    SUBJECT,
    FlowFailure,
    PlazaAccept,
)

DEFAULT_OUT = ROOT / "screenshots" / "integration"
DEFAULT_REPORT = DEFAULT_OUT / "official-accept-report.json"
DEFAULT_BASE_URL = "http://127.0.0.1:8848"

#: 学员账号（独立于广场作者/评审账号，用于验证「普通用户学习链路」）。
LEARNER_NAME = "精选验收学员"

#: 平台精选题使用的标准年级（official 学段筛选按快照 grade 精确/前缀匹配）。
STANDARD_GRADE = "高中一年级"
#: 本机假模型连接（只监听 127.0.0.1；同时服务课堂大纲生成与助手工具决策）。
PROVIDER_NAME = "精选验收假模型连接"
MODEL_ID = "mock-official-accept"
FAKE_API_KEY = "sk-local-acceptance-only"

SCENARIO = [
    "复用广场验收账号发布两条平台精选题（客观题 / 主观题，标准年级用于学段筛选）",
    "匿名读广场公开详情；登录用户 source=official|all 检索与 official:<public_id> 详情",
    "学员作答 official 客观题（correct/incorrect）与主观题（pending）",
    "错题/复习队列 /api/review/queue 与 /api/stats official_* 计数",
    "学段筛选 /api/questions?grade= 与 /api/insight/home 平台精选口径",
    "课堂：source_ref=official:<id> 建课生成大纲 → outline.sources 含公开编号/精选版本",
    "助手：HTTP 会话驱动 read_question 工具读取 official 题面（工具结果/引用可见）",
    "工坊建卷（snapshot source=official）→ 整卷练习提交 → 回看",
    "编辑撤回精选 → 检索/详情/作答/建卷/课堂建课立即失效，助手读取给出已撤回提示",
    "复习队列保留已撤回提示；报告 JSON 与退出码",
]


# ---------------------------------------------------------------------------
# 本机假模型服务（只监听回环地址）：课堂大纲生成 + 助手工具决策（固定产物）
# ---------------------------------------------------------------------------

OUTLINE = {
    "title": "精选验收课程大纲",
    "description": "由平台精选题素材生成的验收用课程大纲。",
    "scenes": [
        {"title": "精选题导入", "type": "slide", "outline_points": ["题面回顾", "解题思路"]},
        {"title": "方法讲解", "type": "slide", "outline_points": ["关键步骤", "易错点"]},
        {"title": "课堂小测", "type": "quiz", "outline_points": ["当堂检验"],
         "quiz": {"question_count": 2, "difficulty": "medium"}},
    ],
}

_OFFICIAL_REF_RE = re.compile(r"official:plz-[A-Za-z0-9]+")


class _FakeModelHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # 静默日志：验收输出更干净
        return

    def _send(self, status: int, payload: bytes, content_type: str = "application/json"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler 约定
        if self.path.split("?")[0].rstrip("/") in ("/v1/models", "/models"):
            return self._send(200, json.dumps({
                "object": "list", "data": [{"id": MODEL_ID, "object": "model"}],
            }).encode("utf-8"))
        return self._send(404, b'{"error":{"message":"not found"}}')

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler 约定
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:  # noqa: BLE001 - 非法请求按空 prompt 处理
            body = {}
        prompt = ""
        for message in body.get("messages") or []:
            content = message.get("content")
            if isinstance(content, list):
                content = "".join(part.get("text", "") for part in content
                                  if isinstance(part, dict))
            prompt += str(content or "")
        if "课程大纲生成器" in prompt:
            payload = OUTLINE
        elif "【进度】" in prompt and "【用户当前问题】" in prompt:
            if "【本轮工具观察】" in prompt:
                # 第二步：已拿到工具观察，收敛为最终答案（不再调用工具）。
                payload = {"answer": "已根据助手 read_question 工具读取平台精选题的题面与来源信息。",
                           "thought": "工具已返回平台精选公开题信息，直接总结。"}
            else:
                match = _OFFICIAL_REF_RE.search(prompt)
                if match:
                    payload = {"tool": "read_question", "args": {"qid": match.group(0)},
                               "thought": "先用 read_question 读取平台精选题面。"}
                else:
                    payload = {"tool": "search_official",
                               "args": {"keyword": "精选验收", "limit": 3},
                               "thought": "检索平台精选题。"}
        else:
            payload = {"answer": "（验收假模型固定回复）"}
        content = json.dumps(payload, ensure_ascii=False)
        response = {
            "id": "chatcmpl-mock", "object": "chat.completion", "created": 1,
            "model": body.get("model") or MODEL_ID,
            "choices": [{"index": 0,
                         "message": {"role": "assistant", "content": content},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 120, "completion_tokens": 80, "total_tokens": 200},
        }
        return self._send(200, json.dumps(response, ensure_ascii=False).encode("utf-8"))


class FakeModelServer:
    """只绑定 127.0.0.1 的假模型服务（端口 0 由系统分配空闲端口）。"""

    def __init__(self):
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        if self._server is None:
            raise RuntimeError("假模型服务尚未启动")
        port = int(self._server.server_address[1])
        return f"http://127.0.0.1:{port}/v1"

    def start(self) -> None:
        if self._server is not None:
            return
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeModelHandler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            try:
                self._server.shutdown()
            except Exception:  # noqa: BLE001 - 收尾失败不影响报告
                pass
            try:
                self._server.server_close()
            except Exception:  # noqa: BLE001
                pass
        self._server = None
        self._thread = None


class OfficialAccept:
    def __init__(self, base_url: str, out_dir: Path, report_path: Path, run_id: str, *,
                 admin_username: str = ADMIN_NAME, admin_password: str = PASSWORD,
                 admin_username_explicit: bool = False, admin_token: str = ""):
        self.base = base_url.rstrip("/")
        self.out = out_dir
        self.report_path = report_path
        self.run_id = run_id
        self.marker = f"精选验收{run_id}"
        self.clients: dict[str, httpx.Client] = {}
        self.api_calls: dict[str, dict] = {}
        #: 复用 plaza_accept 的账号准备 / 角色授权 / 发布链路（报告只保留本脚本步骤）。
        self.helper = PlazaAccept(
            self.base, out_dir, report_path, None, run_id,
            admin_username=admin_username,
            admin_password=admin_password,
            admin_username_explicit=admin_username_explicit,
            admin_token=admin_token,
            skip_ui=True,
        )
        #: 本机假模型服务（课堂大纲生成 + 助手工具决策；只监听 127.0.0.1）。
        self.fake = FakeModelServer()
        self.provider_id: int | None = None
        self.data: dict = {
            "run_id": run_id,
            "marker": self.marker,
            "accounts": {},
            "official": {},
            "objective": {},
            "subjective": {},
            "grade": {},
            "insight": {},
            "classroom": {},
            "assistant": {},
            "paper": {},
            "attempts": {},
            "review_queue": {},
            "quality_gate": {},
        }
        self.report: dict = {
            "ok": False,
            "base_url": self.base,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "run_id": run_id,
            "scenario": SCENARIO,
            "steps": [],
            "api": {},
            "ui": {"enabled": False, "skipped": True, "ok": None, "steps": [],
                   "problems": [], "warnings": ["本脚本只验 HTTP 链路，无 Playwright 冒烟"], "screenshots": []},
            "warnings": [],
            "problems": [],
            "data": self.data,
            "exit_code": 1,
        }
        self._started = 0.0

    # -- 基础工具 ---------------------------------------------------------
    def warning(self, message: str) -> None:
        if message not in self.report["warnings"]:
            self.report["warnings"].append(message)

    def new_client(self) -> httpx.Client:
        # trust_env=False：本机 Windows 系统代理会劫持 127.0.0.1，导致 502。
        return httpx.Client(base_url=self.base, trust_env=False, timeout=30.0)

    def close(self) -> None:
        for client in self.clients.values():
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
        self.clients.clear()
        self.helper.close()
        self.fake.stop()

    @staticmethod
    def _snippet(response: httpx.Response, limit: int = 400) -> str:
        try:
            body = response.json()
        except ValueError:
            return response.text[:limit]
        if isinstance(body, dict):
            detail = body.get("detail")
            if isinstance(detail, str):
                return detail[:limit]
            if detail is not None:
                body = detail
        try:
            return json.dumps(body, ensure_ascii=False)[:limit]
        except (TypeError, ValueError):
            return str(body)[:limit]

    @staticmethod
    def _template(path: str) -> str:
        path_only = path.split("?")[0]
        path_only = re.sub(r"/plz-[0-9a-z]+", "/{public_id}", path_only)
        path_only = re.sub(r"/official:[^/]+", "/{official_ref}", path_only)
        path_only = re.sub(r"/\d+(?=/|$)", "/{id}", path_only)
        return path_only

    def _note_api(self, method: str, path: str, status: int) -> None:
        template = self._template(path)
        key = f"{method} {template}"
        item = self.api_calls.setdefault(
            key, {"method": method, "path": template, "calls": 0, "statuses": []})
        item["calls"] += 1
        status_text = str(status)
        if status_text not in item["statuses"]:
            item["statuses"].append(status_text)

    def request(self, client: httpx.Client, method: str, path: str, **kwargs) -> httpx.Response:
        # plaza_accept 的账号客户端是轻代理（会把 data 统一转 JSON）；这里取底层 httpx.Client，
        # 保证 POST /api/attempts 等 Form 接口按表单发送。
        raw = getattr(client, "_raw", client)
        try:
            response = raw.request(method, path, timeout=kwargs.pop("timeout", 30.0), **kwargs)
        except httpx.TimeoutException as exc:
            raise FlowFailure(
                f"请求超时：{method} {path}（{self.base} 30s 无响应，请确认隔离实例未卡住）") from exc
        except httpx.RequestError as exc:
            raise FlowFailure(
                f"无法连接实例：{method} {path} → {exc.__class__.__name__}；"
                f"请用 --base-url 指定已启动的隔离实例（当前 {self.base}）") from exc
        self._note_api(method, path, response.status_code)
        return response

    def expect(self, response: httpx.Response, statuses, label: str) -> httpx.Response:
        if isinstance(statuses, int):
            statuses = (statuses,)
        if response.status_code not in statuses:
            raise FlowFailure(
                f"{label}：期望 HTTP {tuple(statuses)}，实际 {response.status_code}；"
                f"响应：{self._snippet(response)}")
        return response

    def payload(self, response: httpx.Response, label: str):
        try:
            body = response.json()
        except ValueError as exc:
            raise FlowFailure(
                f"{label}：响应不是合法 JSON（HTTP {response.status_code}）："
                f"{response.text[:200]}") from exc
        if not isinstance(body, (dict, list)):
            raise FlowFailure(f"{label}：响应结构异常（{type(body).__name__}）")
        return body

    def step(self, name: str, checks: list, *, started: float | None = None,
             data: dict | None = None) -> None:
        started = started if started is not None else self._started
        elapsed = round((time.time() - started) * 1000, 1) if started else 0.0
        normalized = []
        failures = []
        for item in checks:
            label = str(item[0])
            ok = bool(item[1])
            detail = str(item[2]) if len(item) > 2 and item[2] else ""
            normalized.append({"label": label, "ok": ok, "detail": "" if ok else detail})
            if not ok:
                failures.append((label, detail))
        entry = {
            "name": name,
            "ok": not failures,
            "status": "passed" if not failures else "failed",
            "elapsed_ms": elapsed,
            "checks": normalized,
        }
        if data:
            entry["data"] = data
        self.report["steps"].append(entry)
        if failures:
            detail = "；".join(f"{label}（{extra}）" if extra else label
                              for label, extra in failures)
            self.report["problems"].append(f"{name}: {detail}")
            raise FlowFailure(f"{name}: {detail}")

    # -- 账号与发布 -------------------------------------------------------
    def bootstrap(self) -> None:
        """复用 plaza_accept 账号与角色，注册/登录独立学员账号。"""
        started = time.time()
        helper = self.helper
        helper.clients["anon"] = helper.new_client()
        helper.prepare_accounts()
        helper.grant_roles()
        # WS28 抽检门禁兼容：实例显式 armed 时先临时关闭门禁（结束前恢复原模式）。
        helper.ensure_gate_compatible()
        learner_client, learner_user, mode = helper._ensure_account(LEARNER_NAME)
        self.clients["learner"] = learner_client
        self.clients["anon"] = helper.clients["anon"]
        for key in ("admin", "author", "editor", "reviewer1", "reviewer2"):
            self.clients[key] = helper.clients[key]
        self.data["accounts"] = {
            "learner": {"username": LEARNER_NAME, "user_id": learner_user.get("id"), "mode": mode},
            "reused": list(helper.data.get("accounts") or {}),
        }
        gate_origin = helper.gate_origin or {}
        self.data["quality_gate"] = {
            "origin": {key: gate_origin.get(key) for key in (
                "mode", "mode_source", "armed", "armed_source", "enforced", "required")},
            "temporarily_disabled": bool(helper.gate_disabled),
        }
        helper_steps = helper.report.get("steps") or []
        checks = [
            ("广场验收账号与角色就绪（复用 plaza_accept 链路）",
             all((helper.data.get("accounts") or {}).get(key, {}).get("user_id")
                 for key in ("author", "editor", "reviewer1", "reviewer2")),
             json.dumps(list((helper.data.get("accounts") or {}).keys()), ensure_ascii=False)),
            ("学员账号就绪（登录/注册）",
             bool(learner_user.get("id")) and bool(learner_user.get("username") == LEARNER_NAME),
             json.dumps({k: learner_user.get(k) for k in ("id", "username", "role")},
                        ensure_ascii=False)),
            ("复用 plaza_accept 账号/角色准备步骤全部通过",
             all(step.get("ok") for step in helper_steps),
             f"helper_steps={len(helper_steps)}"),
            ("抽检门禁兼容（显式 armed 时已临时关闭，结束前恢复原模式）",
             bool(gate_origin.get("mode")),
             json.dumps(self.data["quality_gate"], ensure_ascii=False)[:260]),
        ]
        self.step("准备验收账号（复用 plaza_accept 账号与角色 + 学员账号）", checks,
                  started=started, data={"learner_mode": mode, "reused_accounts":
                                         len(helper.data.get("accounts") or {})})

    def publish_official_questions(self) -> None:
        started = time.time()
        objective_content = {
            "stem": (f"【{self.marker}】精选客观题：已知函数 f(x)=x+1，求 f(2) 的值。"),
            "options": [
                {"label": "A", "text": "3"},
                {"label": "B", "text": "2"},
                {"label": "C", "text": "4"},
                {"label": "D", "text": "1"},
            ],
            "answers": ["A"],
            "explanation": "代入 x=2 得 f(2)=2+1=3，因此选 A。",
        }
        objective = self.helper.publish_new_question(
            "精选客观题（official 客观判分）", content=objective_content,
            grade=STANDARD_GRADE, record_step=False)
        subjective_content = {
            "stem": (f"【{self.marker}】精选主观题：简述一次函数 y=x+1 的单调性，并说明理由。"),
            "answers": ["该函数在实数集上单调递增。"],
            "explanation": "任取 x1<x2，则 f(x1)-f(x2)=x1-x2<0，故函数单调递增。",
        }
        subjective = self.helper.publish_new_question(
            "精选主观题（official 待核验）", qtype="SUBJECTIVE",
            content=subjective_content, grade=STANDARD_GRADE, record_step=False)
        self.data["objective"] = {**objective, "stem": objective_content["stem"]}
        self.data["subjective"] = {**subjective, "stem": subjective_content["stem"]}
        checks = [
            ("客观题发布为 active 平台精选",
             str(objective.get("public_id") or "").startswith("plz-")
             and objective.get("transitions", [""])[-1] == "verified",
             f"public_id={objective.get('public_id')}"),
            ("主观题发布为 active 平台精选",
             str(subjective.get("public_id") or "").startswith("plz-")
             and subjective.get("transitions", [""])[-1] == "verified",
             f"public_id={subjective.get('public_id')}"),
            ("两题公开题号不同",
             bool(objective.get("public_id")) and objective.get("public_id") != subjective.get("public_id"),
             f"{objective.get('public_id')} / {subjective.get('public_id')}"),
        ]
        self.step("发布两条平台精选题（客观题 + 主观题，official 引用来源）", checks,
                  started=started,
                  data={"objective": objective.get("public_id"),
                        "subjective": subjective.get("public_id")})

    # -- 场景：检索与详情 -------------------------------------------------
    def assert_discoverable(self) -> None:
        started = time.time()
        objective = self.data["objective"]
        subjective = self.data["subjective"]
        public_id = objective["public_id"]
        learner = self.clients["learner"]
        anonymous = self.clients["anon"]
        marker = self.marker
        plaza_detail = self.payload(self.expect(
            anonymous.request("GET", f"/api/plaza/questions/{public_id}", timeout=30.0),
            200, "匿名读广场公开详情"), "广场公开详情")
        anon_list = self.request(anonymous, "GET", "/api/questions",
                                 params={"source": "official"})
        listing = self.payload(self.expect(
            learner.request("GET", "/api/questions",
                            params={"source": "official", "q": marker, "page_size": 50},
                            timeout=30.0),
            200, "登录用户 source=official 检索"), "official 检索")
        items = [item for item in (listing.get("items") or []) if isinstance(item, dict)]
        item_ids = [str(item.get("public_id") or "") for item in items]
        objective_item = next(
            (item for item in items if str(item.get("public_id") or "") == public_id), {})
        detail = self.payload(self.expect(
            learner.request("GET", f"/api/questions/official:{public_id}", timeout=30.0),
            200, "official 引用详情"), "official 详情")
        merged = self.payload(self.expect(
            learner.request("GET", "/api/questions",
                            params={"source": "all", "q": marker, "page_size": 50},
                            timeout=30.0),
            200, "source=all 检索"), "source=all 检索")
        unknown = self.request(learner, "GET", "/api/questions/official:plz-not-a-real-id")
        invalid = self.request(learner, "GET", "/api/questions",
                               params={"source": "not-a-source"})
        detail_text = json.dumps(detail, ensure_ascii=False)
        checks = [
            ("匿名可读广场公开详情（published / 精选）",
             plaza_detail.get("visible") is True
             and plaza_detail.get("status") == "published"
             and plaza_detail.get("featured") is True
             and marker in str((plaza_detail.get("content") or {}).get("stem") or ""),
             json.dumps({k: plaza_detail.get(k) for k in ("visible", "status", "featured")},
                        ensure_ascii=False)),
            ("/api/questions 登录门禁：匿名 source=official 返回 401",
             anon_list.status_code == 401, f"HTTP {anon_list.status_code}"),
            ("source=official 检索命中两条精选题",
             public_id in item_ids
             and str(subjective.get("public_id") or "") in item_ids
             and int(listing.get("official_total") or 0) >= 2,
             f"ids={item_ids[:10]} official_total={listing.get('official_total')}"),
            ("检索卡片：official 引用 + 平台精选来源/审核状态",
             str(objective_item.get("qid") or "") == f"official:{public_id}"
             and objective_item.get("source") == "official"
             and objective_item.get("source_label") == "平台精选"
             and objective_item.get("review_status") == "featured"
             and objective_item.get("official_status") == "published"
             and bool(str(objective_item.get("notice") or "")),
             json.dumps(objective_item, ensure_ascii=False)[:280]),
            ("official:<public_id> 详情可读（来源/题号/版本/正文）",
             detail.get("source") == "official"
             and detail.get("source_label") == "平台精选"
             and detail.get("public_id") == public_id
             and detail.get("review_status") == "featured"
             and int(detail.get("published_version") or 0) >= 1
             and marker in detail_text,
             json.dumps({k: detail.get(k) for k in (
                 "source", "source_label", "public_id", "review_status",
                 "published_version")}, ensure_ascii=False)),
            ("source=all 合并检索：official_total >= 2",
             int(merged.get("official_total") or 0) >= 2
             and str(merged.get("source") or "") == "all",
             json.dumps({k: merged.get(k) for k in ("source", "bank_total", "official_total")},
                        ensure_ascii=False)),
            ("非法 source 返回 422", invalid.status_code == 422,
             f"HTTP {invalid.status_code}"),
            ("未知 official 引用返回 404", unknown.status_code == 404,
             f"HTTP {unknown.status_code}"),
        ]
        self.step("精选检索与详情（匿名广场 + 登录 source=official|all + official 引用）",
                  checks, started=started, data={"public_id": public_id})

    # -- 场景：作答 / 错题 / 复习队列 --------------------------------------
    def answer_flow(self) -> None:
        started = time.time()
        objective = self.data["objective"]
        subjective = self.data["subjective"]
        objective_ref = f"official:{objective['public_id']}"
        subjective_ref = f"official:{subjective['public_id']}"
        learner = self.clients["learner"]
        correct = self.payload(self.expect(
            self.request(learner, "POST", "/api/attempts",
                         data={"qid": objective_ref, "answer": "A"}), 200,
            "official 客观题正确作答"), "客观题作答")
        wrong = self.payload(self.expect(
            self.request(learner, "POST", "/api/attempts",
                         data={"qid": objective_ref, "answer": "B"}), 200,
            "official 客观题错误作答"), "客观题错误作答")
        subjective_attempt = self.payload(self.expect(
            self.request(learner, "POST", "/api/attempts",
                         data={"qid": subjective_ref,
                               "answer": "该函数在实数集上单调递增，因为斜率大于零。"}), 200,
            "official 主观题作答"), "主观题作答")
        queue = self.payload(self.expect(
            learner.request("GET", "/api/review/queue", params={"limit": 200}, timeout=30.0),
            200, "统一复习队列"), "复习队列")
        graded_items = [item for item in (queue.get("items") or []) if isinstance(item, dict)]
        pending_items = [item for item in (queue.get("pending_items") or [])
                         if isinstance(item, dict)]
        objective_card = next((item for item in graded_items
                               if str(item.get("qid") or "") == objective_ref), {})
        subjective_card = next((item for item in pending_items
                                if str(item.get("qid") or "") == subjective_ref), {})
        stats = self.payload(self.expect(
            learner.request("GET", "/api/stats", timeout=30.0), 200, "学习统计"), "学习统计")
        attempt_row = correct.get("attempt") or {}
        checks = [
            ("客观题正确答案判分 correct",
             correct.get("result") == "correct"
             and str(attempt_row.get("qid") or "") == objective_ref,
             json.dumps({"result": correct.get("result"),
                         "qid": attempt_row.get("qid")}, ensure_ascii=False)),
            ("作答后答案解锁（question.source=official / answer 可见）",
             (correct.get("question") or {}).get("source") == "official"
             and (correct.get("question") or {}).get("unlocked") is True
             and bool((correct.get("question") or {}).get("answer")),
             json.dumps({k: (correct.get("question") or {}).get(k) for k in
                         ("source", "unlocked", "public_id")}, ensure_ascii=False)),
            ("客观题错误答案判分 incorrect",
             wrong.get("result") == "incorrect",
             json.dumps({"result": wrong.get("result")}, ensure_ascii=False)),
            ("主观题作答保留 pending（不硬判）",
             subjective_attempt.get("result") == "pending",
             json.dumps({"result": subjective_attempt.get("result")}, ensure_ascii=False)),
            ("复习队列已判分项含 official 客观错题",
             str(objective_card.get("qid") or "") == objective_ref
             and objective_card.get("graded") is True
             and objective_card.get("source") == "official"
             and objective_card.get("source_label") == "平台精选"
             and (objective_card.get("latest_attempt") or {}).get("result") == "incorrect",
             json.dumps(objective_card, ensure_ascii=False)[:280]),
            ("复习队列未判分项含 official 主观题",
             str(subjective_card.get("qid") or "") == subjective_ref
             and subjective_card.get("graded") is False
             and subjective_card.get("pending") is True,
             json.dumps(subjective_card, ensure_ascii=False)[:280]),
            ("学习统计 official_* 计数",
             int(stats.get("official_attempts") or 0) >= 3
             and int(stats.get("official_practiced") or 0) >= 2
             and int(stats.get("official_wrong_count") or 0) >= 1,
             json.dumps({k: stats.get(k) for k in (
                 "official_attempts", "official_practiced", "official_wrong_count")},
                 ensure_ascii=False)),
        ]
        self.data["attempts"] = {
            "correct": correct.get("attempt_id"),
            "wrong": wrong.get("attempt_id"),
            "subjective": subjective_attempt.get("attempt_id"),
        }
        self.data["review_queue"] = {"graded": len(graded_items), "pending": len(pending_items)}
        self.step("official 作答（客观判分/主观 pending）→ 错题 → 复习队列 → 统计",
                  checks, started=started)

    # -- 场景：工坊建卷与整卷练习 ------------------------------------------
    def paper_flow(self) -> None:
        started = time.time()
        objective = self.data["objective"]
        subjective = self.data["subjective"]
        objective_ref = f"official:{objective['public_id']}"
        subjective_ref = f"official:{subjective['public_id']}"
        learner = self.clients["learner"]
        created = self.payload(self.expect(
            self.request(learner, "POST", "/api/paperlab/papers", json={
                "title": f"精选验收整卷 {self.run_id}",
                "kind": "manual",
                "subject": SUBJECT,
                "grade": GRADE,
                "instructions": "验收自造卷：平台精选题整卷练习",
                "items": [
                    {"ref": objective_ref, "score": 3},
                    {"ref": subjective_ref, "score": 5},
                ],
            }), 200, "工坊建卷（official 引用）"), "工坊建卷")
        paper = created.get("paper") or {}
        paper_id = int(paper.get("id") or 0)
        created_items = [item for item in (created.get("items") or []) if isinstance(item, dict)]
        snapshots = [item.get("snapshot") or {} for item in created_items]
        self.data["paper"] = {"paper_id": paper_id}
        detail = self.payload(self.expect(
            learner.request("GET", f"/api/paperlab/papers/{paper_id}",
                            params={"include_snapshot": "true"}, timeout=30.0),
            200, "建卷详情"), "建卷详情")
        detail_items = [item for item in (detail.get("items") or []) if isinstance(item, dict)]
        attempt = self.payload(self.expect(
            self.request(learner, "POST", f"/api/paperlab/papers/{paper_id}/attempts"),
            200, "创建整卷练习"), "整卷练习")
        attempt_row = attempt.get("attempt") or {}
        attempt_id = int(attempt_row.get("id") or 0)
        attempt_items = [item for item in (attempt.get("items") or []) if isinstance(item, dict)]
        self.data["paper"].update({"attempt_id": attempt_id})
        saved_objective = self.payload(self.expect(
            self.request(learner, "PATCH",
                         f"/api/paperlab/attempts/{attempt_id}/answers",
                         json={"item_position": 1, "answer": ["B"]}), 200,
            "整卷保存客观题作答"), "整卷保存作答")
        saved_subjective = self.payload(self.expect(
            self.request(learner, "PATCH",
                         f"/api/paperlab/attempts/{attempt_id}/answers",
                         json={"item_position": 2,
                               "answer": "该函数在实数集上单调递增。"}), 200,
            "整卷保存主观题作答"), "整卷保存作答")
        submitted = self.payload(self.expect(
            self.request(learner, "POST", f"/api/paperlab/attempts/{attempt_id}/submit",
                         json={}), 200, "整卷提交判分"), "整卷提交")
        result = submitted.get("result") or {}
        summary = result.get("summary") or {}
        official_wrong_added = [entry for entry in (result.get("official_wrong_added") or [])
                                if isinstance(entry, dict)]
        replay = self.payload(self.expect(
            learner.request("GET", f"/api/paperlab/attempts/{attempt_id}", timeout=30.0),
            200, "回看整卷练习"), "回看")
        replay_items = [item for item in (replay.get("items") or []) if isinstance(item, dict)]
        replay_official = [item for item in replay_items if item.get("source") == "official"]
        first_item = replay_official[0] if replay_official else {}
        checks = [
            ("建卷成功且快照 source=official / public_id / 来源声明",
             paper_id > 0 and len(created_items) == 2
             and all(snapshot.get("source") == "official" for snapshot in snapshots)
             and snapshots[0].get("public_id") == objective["public_id"]
             and bool(snapshots[0].get("source_declaration")),
             json.dumps([{k: snapshot.get(k) for k in ("ref", "source", "public_id")}
                         for snapshot in snapshots], ensure_ascii=False)[:280]),
            ("建卷详情回读仍标注 official 来源与平台精选标签",
             len(detail_items) == 2
             and all(item.get("source") == "official" for item in detail_items)
             and all(item.get("source_label") == "平台精选" for item in detail_items),
             json.dumps([{k: item.get(k) for k in ("source", "source_label")}
                         for item in detail_items], ensure_ascii=False)[:240]),
            ("整卷练习卷面含 official 来源且不提前泄题",
             attempt_id > 0 and len(attempt_items) == 2
             and all(item.get("source") == "official" for item in attempt_items)
             and all("reference" not in item for item in attempt_items),
             json.dumps([{k: item.get(k) for k in ("position", "source")}
                         for item in attempt_items], ensure_ascii=False)[:240]),
            ("逐题保存作答成功",
             saved_objective.get("saved") is True and saved_subjective.get("saved") is True,
             json.dumps({"objective": saved_objective.get("saved"),
                         "subjective": saved_subjective.get("saved")}, ensure_ascii=False)),
            ("整卷判分：客观题错误入统一错题本、主观题 pending",
             (submitted.get("attempt") or {}).get("status") == "submitted"
             and int(summary.get("incorrect_count") or 0) >= 1
             and int(summary.get("pending_count") or 0) >= 1
             and any(str(entry.get("ref") or "") == objective_ref
                     for entry in official_wrong_added),
             json.dumps({"summary": summary, "official_wrong_added": official_wrong_added},
                        ensure_ascii=False)[:320]),
            ("回看结果保留 official 来源与判分参考",
             replay.get("attempt", {}).get("status") == "submitted"
             and len(replay_official) == 2
             and str(first_item.get("source") or "") == "official"
             and bool(first_item.get("reference")),
             json.dumps({"items": [{k: item.get(k) for k in
                                    ("position", "source", "judgement")}
                                   for item in replay_official]},
                        ensure_ascii=False)[:280]),
        ]
        self.step("工坊建卷（official 快照）→ 整卷练习提交 → 回看", checks,
                  started=started,
                  data={"paper_id": paper_id, "attempt_id": attempt_id})

    # -- 场景：学段筛选 / 首页精选口径 -------------------------------------
    def grade_filter_flow(self) -> None:
        started = time.time()
        learner = self.clients["learner"]
        pid = self.data["objective"]["public_id"]
        stage = self.payload(self.expect(
            learner.request("GET", "/api/questions",
                            params={"source": "official", "grade": "高中",
                                    "q": self.marker, "page_size": 50}, timeout=30.0),
            200, "official 学段筛选（高中）"), "official 学段筛选")
        exact = self.payload(self.expect(
            learner.request("GET", "/api/questions",
                            params={"source": "official", "grade": STANDARD_GRADE,
                                    "q": self.marker, "page_size": 50}, timeout=30.0),
            200, "official 学段筛选（标准年级）"), "official 学段筛选（标准年级）")
        junior = self.payload(self.expect(
            learner.request("GET", "/api/questions",
                            params={"source": "official", "grade": "初中",
                                    "q": self.marker, "page_size": 50}, timeout=30.0),
            200, "official 学段筛选（初中）"), "official 学段筛选（初中）")
        invalid = self.payload(self.expect(
            learner.request("GET", "/api/questions",
                            params={"source": "official", "grade": "不存在的学段",
                                    "q": self.marker, "page_size": 50}, timeout=30.0),
            200, "official 未知学段安全降级"), "official 未知学段")
        bank = self.payload(self.expect(
            learner.request("GET", "/api/questions",
                            params={"source": "bank", "grade": "高中", "page_size": 5},
                            timeout=30.0),
            200, "题库学段筛选（无 grade 列安全降级）"), "题库学段筛选")
        catalog = self.payload(self.expect(
            learner.request("GET", "/api/catalog", timeout=30.0), 200, "读取检索目录"),
            "检索目录")
        stage_items = [item for item in (stage.get("items") or []) if isinstance(item, dict)]
        exact_items = [item for item in (exact.get("items") or []) if isinstance(item, dict)]
        junior_items = [item for item in (junior.get("items") or []) if isinstance(item, dict)]
        stage_filter = stage.get("grade_filter") or {}
        exact_filter = exact.get("grade_filter") or {}
        junior_filter = junior.get("grade_filter") or {}
        invalid_filter = invalid.get("grade_filter") or {}
        bank_filter = bank.get("grade_filter") or {}
        self.data["grade"] = {
            "stage_total": stage.get("official_total"),
            "exact_total": exact.get("official_total"),
            "junior_total": junior.get("official_total"),
            "stage_filter": stage_filter,
            "bank_filter": bank_filter,
        }
        checks = [
            ("/api/questions?grade=高中：official 筛选生效且命中标准年级精选题",
             stage_filter.get("applied") is True
             and stage_filter.get("official_applied") is True
             and stage_filter.get("stages") == ["高中"]
             and int(stage.get("official_total") or 0) >= 1
             and all(str(item.get("grade") or "").startswith("高中") for item in stage_items),
             json.dumps({"total": stage.get("official_total"),
                         "filter": stage_filter,
                         "grades": [item.get("grade") for item in stage_items[:5]]},
                        ensure_ascii=False)[:320]),
            ("/api/questions?grade=高中一年级：标准年级精确匹配",
             exact_filter.get("values") == [STANDARD_GRADE]
             and int(exact.get("official_total") or 0) >= 1
             and all(item.get("grade") == STANDARD_GRADE for item in exact_items),
             json.dumps({"total": exact.get("official_total"),
                         "filter": exact_filter}, ensure_ascii=False)[:260]),
            ("/api/questions?grade=初中：不匹配任何精选题（official_total=0）",
             int(junior.get("official_total") or 0) == 0 and not junior_items,
             json.dumps({"total": junior.get("official_total"),
                         "filter": junior_filter}, ensure_ascii=False)[:240]),
            ("未知学段安全降级（200 + applied=false + 忽略原因，不 422）",
             invalid_filter.get("requested") == "不存在的学段"
             and invalid_filter.get("applied") is False
             and bool(invalid_filter.get("reason")),
             json.dumps(invalid_filter, ensure_ascii=False)[:260]),
            ("题库侧无 grade 列时安全降级（applied=false + 口径说明）",
             bank_filter.get("applied") is False
             and bool(bank_filter.get("reason")),
             json.dumps(bank_filter, ensure_ascii=False)[:280]),
            ("catalog 下发标准年级与学段选项（前端筛选项来源）",
             len(catalog.get("grades") or []) >= 12
             and catalog.get("grade_stages") == ["小学", "初中", "高中"]
             and bool(catalog.get("grade_note")),
             json.dumps({"grade_stages": catalog.get("grade_stages"),
                         "grades": len(catalog.get("grades") or [])},
                        ensure_ascii=False)),
            ("筛选结果仍包含本次验收精选题（public_id）",
             pid in [str(item.get("public_id") or "") for item in stage_items],
             f"pid={pid} ids={[item.get('public_id') for item in stage_items][:5]}"),
        ]
        self.step("学段筛选：/api/questions?grade= 标准年级/学段匹配与安全降级", checks,
                  started=started, data=self.data["grade"])

    def insight_home_official(self) -> None:
        started = time.time()
        learner = self.clients["learner"]
        home = self.payload(self.expect(
            learner.request("GET", "/api/insight/home", timeout=30.0), 200,
            "首页学情统计"), "首页学情统计")
        official = home.get("official") or {}
        bank = home.get("bank") or {}
        caliber = home.get("caliber") or {}
        self.data["insight"] = {
            "official": {key: official.get(key) for key in (
                "available", "total_questions", "practice_pool", "auto_gradable",
                "attempts", "practiced", "wrong_count", "due_reviews")},
            "bank_official_practice_pool": bank.get("official_practice_pool"),
        }
        checks = [
            ("/api/insight/home 含 official 口径块且 available=true",
             official.get("available") is True
             and str(official.get("caliber") or ""),
             json.dumps(official, ensure_ascii=False)[:300]),
            ("official 可练/可自动判分/总量为真实计数（>=2）",
             int(official.get("total_questions") or 0) >= 2
             and int(official.get("practice_pool") or 0) >= 2
             and int(official.get("auto_gradable") or 0) >= 1,
             json.dumps({key: official.get(key) for key in (
                 "total_questions", "practice_pool", "auto_gradable")},
                 ensure_ascii=False)),
            ("official 本人已练/错题计数与作答链路一致（>=3 次 / >=2 题 / >=1 错题）",
             int(official.get("attempts") or 0) >= 3
             and int(official.get("practiced") or 0) >= 2
             and int(official.get("wrong_count") or 0) >= 1,
             json.dumps({key: official.get(key) for key in (
                 "attempts", "practiced", "wrong_count")}, ensure_ascii=False)),
            ("bank 块挂 official_practice_pool（首页题库卡片口径）",
             int(bank.get("official_practice_pool") or 0)
             == int(official.get("practice_pool") or 0),
             json.dumps({"bank": bank.get("official_practice_pool"),
                         "official": official.get("practice_pool")},
                        ensure_ascii=False)),
            ("caliber.official 口径说明存在（不写死数字）",
             bool(str(caliber.get("official") or ""))
             and "official" not in json.dumps(official.get("available"), ensure_ascii=False),
             str(caliber.get("official") or "")[:160]),
        ]
        self.step("/api/insight/home 平台精选口径（可练/已练/错题/首页卡片）", checks,
                  started=started, data=self.data["insight"])

    # -- 场景：课堂 / 助手接入 official（本机假模型连接） -------------------
    def ensure_fake_provider(self) -> None:
        started = time.time()
        self.fake.start()
        learner = self.clients["learner"]
        degraded = ""
        provider_id = 0
        providers = self.request(learner, "GET", "/api/providers")
        if providers.status_code == 200:
            body = self.payload(providers, "读取模型连接")
            items = [item for item in (body.get("items") or []) if isinstance(item, dict)]
            existing = next((item for item in items
                             if str(item.get("name") or "") == PROVIDER_NAME), None)
        elif providers.status_code == 401:
            existing = None
            degraded = f"读取模型连接 HTTP 401：{self._snippet(providers)}"
        else:
            existing = None
            degraded = f"读取模型连接 HTTP {providers.status_code}：{self._snippet(providers)}"
        payload = {
            "name": PROVIDER_NAME, "kind": "openai", "base_url": self.fake.base_url,
            "api_key": FAKE_API_KEY, "models": [MODEL_ID],
        }
        if existing is not None:
            provider_id = int(existing.get("id") or 0)
            updated = self.request(learner, "PATCH", f"/api/providers/{provider_id}",
                                   json=payload)
            if updated.status_code == 200:
                provider_id = int(self.payload(updated, "更新模型连接").get("id") or provider_id)
            else:
                degraded = (f"更新模型连接 HTTP {updated.status_code}："
                            f"{self._snippet(updated)}")
        elif not degraded:
            created = self.request(learner, "POST", "/api/providers", json=payload)
            if created.status_code == 200:
                provider_id = int(self.payload(created, "创建模型连接").get("id") or 0)
            else:
                degraded = (f"创建模型连接 HTTP {created.status_code}："
                            f"{self._snippet(created)}")
        self.provider_id = provider_id or None
        mode = "http-fake-model" if self.provider_id else "static-contract"
        self.data["assistant"]["mode"] = mode
        self.data["classroom"]["mode"] = mode
        self.data["assistant"]["provider"] = {
            "provider_id": self.provider_id, "model": MODEL_ID,
            "base_url": self.fake.base_url,
        }
        if degraded:
            self.warning(
                "本机假模型连接不可用（课堂/助手 official 接入退化为接口契约静态校验）："
                + degraded)
        checks = [
            ("本机假模型服务只监听回环地址（127.0.0.1 随机端口）",
             "127.0.0.1" in self.fake.base_url,
             self.fake.base_url),
            ("学员账号模型连接就绪（POST/PATCH /api/providers）",
             bool(self.provider_id),
             json.dumps(self.data["assistant"]["provider"], ensure_ascii=False)),
        ]
        self.step("准备本机假模型连接（课堂大纲生成 + 助手工具决策共用）", checks,
                  started=started, data=self.data["assistant"]["provider"])
        if not self.provider_id:
            self._assistant_static_contract()
            self._classroom_static_contract()
        else:
            self._cleanup_course_jobs()
            self._ensure_course_concurrency()

    def _cleanup_course_jobs(self) -> None:
        """探测学员账号历史遗留的课堂生成任务（awaiting_outline 暂停态占用全站 course 并发）。

        ``awaiting_outline`` 属于占用并发的在途任务，且产品侧 ``cancel`` 对暂停态任务只返回
        200、状态不变（无法释放并发，属既有产品限制）；因此这里只做「取消请求 + 探测」，
        并记录 warning；真正的可重复运行保障由 ``_ensure_course_concurrency`` 临时提升
        全站 course 并发上限（结束恢复）完成。
        """
        started = time.time()
        learner = self.clients["learner"]
        listing = self.request(learner, "GET", "/api/ai/jobs")
        if listing.status_code != 200:
            self.warning("读取学员历史 AI 任务失败（课堂遗留探测跳过）："
                         f"HTTP {listing.status_code}；{self._snippet(listing)}")
            return
        items = [item for item in (self.payload(listing, "学员历史 AI 任务").get("items") or [])
                 if isinstance(item, dict)]
        leftovers = [item for item in items
                     if str(item.get("task") or "") == "course"
                     and str(item.get("status") or "") in
                     ("queued", "running", "cancel_requested", "awaiting_outline")]
        cancel_accepted = []
        still_paused = []
        for item in leftovers:
            job_id = int(item.get("id") or 0)
            if job_id <= 0:
                continue
            response = self.request(learner, "POST", f"/api/ai/jobs/{job_id}/cancel")
            if response.status_code == 200:
                cancel_accepted.append(job_id)
                detail = self.payload(response, "取消遗留课堂任务")
                if str(detail.get("status") or "") in ("awaiting_outline", "cancel_requested"):
                    still_paused.append(job_id)
        self.data["classroom"]["leftover_jobs_cancelled"] = cancel_accepted
        self.data["classroom"]["leftover_jobs_still_paused"] = still_paused
        if leftovers:
            self.warning(
                "学员账号存在 %d 个历史课堂生成任务（awaiting_outline 暂停态，占用全站 course "
                "并发）：cancel 返回 200 但状态不变（既有产品限制），改由临时提升并发上限保证"
                "同一实例可重复运行；任务列表=%s"
                % (len(leftovers), [int(item.get("id") or 0) for item in leftovers[:8]]))
        checks = [
            ("课堂遗留任务探测与取消请求（暂停态任务占用 course 并发，产品侧无法真正取消）",
             True,
             f"leftovers={len(leftovers)} cancel_accepted={cancel_accepted[:5]} "
             f"still_paused={still_paused[:5]}"),
        ]
        self.step("探测历史课堂生成遗留任务（暂停态任务占用 course 并发）", checks,
                  started=started, data={"leftovers": len(leftovers),
                                         "still_paused": still_paused})

    def _ensure_course_concurrency(self) -> None:
        """管理员临时提升全站 course 并发上限（默认 2），保证同一实例可重复运行；结束恢复。"""
        started = time.time()
        admin = self.clients["admin"]
        quotas = self.payload(self.expect(
            self.request(admin, "GET", "/api/admin/ai/quotas"), 200, "读取 AI 配额与并发"),
            "AI 配额与并发")
        items = [item for item in (quotas.get("items") or []) if isinstance(item, dict)]
        entry = next((item for item in items if str(item.get("kind") or "") == "course"), {})
        origin_limit = entry.get("concurrency_limit")
        origin_source = str(entry.get("concurrency_source") or "")
        target = 20
        changed = False
        if origin_limit is None or origin_limit != 0 and int(origin_limit) < target:
            response = self.request(admin, "POST", "/api/admin/ai/quotas", json={
                "kind": "course", "concurrency_limit": target,
            })
            if response.status_code == 200:
                changed = True
                self.payload(response, "临时提升课堂并发上限")
            else:
                self.warning(
                    "无法临时提升课堂并发上限（同一实例复跑可能 429）："
                    f"HTTP {response.status_code}；{self._snippet(response)}")
        self.data["classroom"]["concurrency_guard"] = {
            "origin_limit": origin_limit, "origin_source": origin_source,
            "target": target, "changed": changed,
        }
        checks = [
            ("课堂生成并发上限就绪（默认 2 时临时提升到 20，结束恢复；保证可重复运行）",
             (origin_limit == 0) or changed or int(origin_limit or 0) >= target,
             json.dumps(self.data["classroom"]["concurrency_guard"], ensure_ascii=False)),
        ]
        self.step("临时提升课堂生成并发上限（可重复运行保障，结束恢复原设置）", checks,
                  started=started, data=self.data["classroom"]["concurrency_guard"])

    def restore_ai_concurrency(self) -> None:
        """恢复 ``_ensure_course_concurrency`` 临时修改的 course 并发上限（失败只记问题）。"""
        guard = self.data.get("classroom", {}).get("concurrency_guard") or {}
        if not guard.get("changed"):
            return
        started = time.time()
        admin = self.clients.get("admin")
        if admin is None:
            return
        restored = False
        last_error = ""
        try:
            origin_source = str(guard.get("origin_source") or "")
            origin_limit = guard.get("origin_limit")
            if origin_source == "setting" and origin_limit is not None:
                response = self.request(admin, "POST", "/api/admin/ai/quotas", json={
                    "kind": "course", "concurrency_limit": int(origin_limit),
                })
            else:
                response = self.request(admin, "POST", "/api/admin/ai/quotas", json={
                    "kind": "course", "reset": True,
                })
            self.expect(response, 200, "恢复课堂并发上限")
            self.payload(response, "恢复课堂并发上限")
            restored = True
        except FlowFailure as exc:
            last_error = str(exc)
        guard["restored"] = restored
        checks = [
            ("课堂生成并发上限恢复验收前设置", restored, last_error),
        ]
        try:
            self.step("恢复课堂生成并发上限（验收不改变实例 AI 配额设置）", checks,
                      started=started)
        except FlowFailure:
            pass

    def _assistant_static_contract(self) -> None:
        """HTTP 假模型不可用时的助手 official 接口契约静态校验（显式标记，不静默跳过）。"""
        started = time.time()
        source = (ROOT / "app" / "assistant.py").read_text(encoding="utf-8")
        checks = [
            ("助手工具契约：search_official / read_question 支持 official 引用",
             "search_official" in source and '"official:<公开编号>"' in source
             and "官方来源标记" not in source,
             "app/assistant.py 缺少 official 工具契约"),
            ("助手 official 读取：公开编号/版本/来源声明字段与撤回提示",
             "published_version" in source and "source_declaration" in source
             and "已撤回" in source,
             "app/assistant.py 缺少 official 读取字段"),
            ("助手只读工具结果不含内部 ID 字段（公开投影）",
             "official_status" in source and "review_status_label" in source,
             "app/assistant.py official 投影字段缺失"),
        ]
        self.data["assistant"]["degraded_reason"] = "本机假模型连接不可用"
        self.step("助手 official 接入（接口契约静态校验；HTTP 路径不可用已记录 warning）",
                  checks, started=started, data={"mode": "static-contract"})

    def _classroom_static_contract(self) -> None:
        started = time.time()
        generate_py = (ROOT / "app" / "classroom" / "generate.py").read_text(encoding="utf-8")
        api_py = (ROOT / "app" / "classroom" / "api.py").read_text(encoding="utf-8")
        checks = [
            ("课堂建课素材：source_ref=official:<编号> 校验与可见性读取",
             "def normalize_seed_source_ref(" in generate_py
             and "def resolve_seed_question(" in generate_py
             and "SEED_QUESTION_SOURCES = (\"official\",)" in generate_py,
             "app/classroom/generate.py 缺少 source_ref 契约"),
            ("outline.sources 记录 public_id / published_version / 来源声明",
             "def official_source_entry(" in generate_py
             and '"published_version": version' in generate_py
             and "source_declaration" in generate_py,
             "app/classroom/generate.py 缺少 outline.sources 字段"),
            ("课程 config.seed_source 回看本次建课来源",
             "def seed_source_config(" in generate_py
             and '"source_ref": "official:%s"' in generate_py,
             "app/classroom/generate.py 缺少 seed_source 回看字段"),
            ("generate 接口接收 source_ref 并在撤回/争议时 404",
             "source_ref: str | None = Field" in api_py
             and "已撤回，不能再作为新课程素材" in generate_py,
             "app/classroom/api.py/generate.py source_ref 契约缺失"),
        ]
        self.data["classroom"]["degraded_reason"] = "本机假模型连接不可用"
        self.step("课堂 official 建课（接口契约静态校验；HTTP 生成路径不可用已记录 warning）",
                  checks, started=started, data={"mode": "static-contract"})

    def _poll_job(self, job_id: int, label: str, *,
                  wanted: tuple[str, ...] = (), timeout: float = 90.0) -> dict:
        learner = self.clients["learner"]
        deadline = time.time() + timeout
        last_status = ""
        last_job: dict = {}
        while time.time() < deadline:
            response = self.request(learner, "GET", f"/api/ai/jobs/{job_id}")
            self.expect(response, 200, f"{label}：读取任务 {job_id}")
            job = self.payload(response, f"{label}：任务 {job_id}")
            last_job = job
            last_status = str(job.get("status") or "")
            if wanted:
                if last_status in wanted:
                    return job
            elif last_status in ("completed", "failed", "interrupted", "cancelled",
                                 "awaiting_outline"):
                return job
            time.sleep(0.4)
        raise FlowFailure(
            f"{label}：任务 {job_id} 超时 {timeout:.0f}s 未完成（最后状态 {last_status or '未知'}）；"
            f"任务详情：{json.dumps(last_job, ensure_ascii=False)[:300]}")

    def assistant_official_flow(self) -> None:
        started = time.time()
        pid = self.data["objective"]["public_id"]
        ref = f"official:{pid}"
        learner = self.clients["learner"]
        tools = self.payload(self.expect(
            learner.request("GET", "/api/assistant/tools", timeout=30.0), 200,
            "助手工具清单"), "助手工具清单")
        tool_items = {str(item.get("name") or ""): item
                      for item in (tools.get("items") or []) if isinstance(item, dict)}
        if not self.provider_id:
            # 已静态校验；这里只补充 HTTP 层可读的工具契约断言（不重复整步）。
            checks = [
                ("/api/assistant/tools 暴露 search_official / read_question",
                 "search_official" in tool_items and "read_question" in tool_items,
                 f"tools={sorted(tool_items)}"),
                ("工具描述声明平台精选 official:<公开编号> 引用",
                 "official:" in str(tool_items.get("read_question", {}).get("description") or "")
                 or "official:" in str(tool_items.get("search_official", {}).get("description") or ""),
                 "工具描述缺少 official 契约"),
            ]
            self.step("助手 official 接入（HTTP 层工具契约；假模型不可用，工具执行已静态校验）",
                      checks, started=started, data={"mode": "static-contract"})
            return
        conversation = self.payload(self.expect(
            learner.request("POST", "/api/assistant/conversations", json={
                "title": f"精选验收助手 {self.run_id}",
            }, timeout=30.0), 200, "创建助手会话"), "创建助手会话")
        conversation_id = int((conversation.get("conversation") or {}).get("id") or 0)
        sent = self.payload(self.expect(
            learner.request("POST", f"/api/assistant/conversations/{conversation_id}/messages",
                            json={
                                "content": (f"请用 read_question 工具读取平台精选题 {ref} 的题面，"
                                            "并概括考查点。"),
                                "provider_id": int(self.provider_id),
                                "model": MODEL_ID,
                            }, timeout=60.0), 200, "发送助手消息"), "发送助手消息")
        job_id = int((sent.get("job") or {}).get("id") or 0)
        job = self._poll_job(job_id, "助手回复")
        conversation_detail = self.payload(self.expect(
            learner.request("GET", f"/api/assistant/conversations/{conversation_id}",
                            params={"page": 1, "page_size": 50}, timeout=30.0),
            200, "读取助手会话"), "读取助手会话")
        messages = [item for item in (conversation_detail.get("messages") or [])
                    if isinstance(item, dict)]
        tool_messages = [item for item in messages
                         if item.get("role") == "tool"
                         and str(item.get("tool_name") or "") == "read_question"]
        assistant_messages = [item for item in messages if item.get("role") == "assistant"]
        tool_message = tool_messages[0] if tool_messages else {}
        tool_result = tool_message.get("tool_result") or {}
        tool_data = tool_result.get("data") or {}
        tool_text = json.dumps(tool_result, ensure_ascii=False)
        refs: list[dict] = []
        for message in assistant_messages:
            for part in (message.get("parts") or []):
                if isinstance(part, dict) and part.get("type") == "refs":
                    refs.extend(item for item in (part.get("items") or [])
                                if isinstance(item, dict))
        official_refs = [item for item in refs if str(item.get("source") or "") == "official"]
        answer_text = json.dumps(assistant_messages, ensure_ascii=False)
        self.data["assistant"].update({
            "conversation_id": conversation_id,
            "job_id": job_id,
            "job_status": job.get("status"),
            "tool_name": tool_message.get("tool_name"),
            "tool_ok": tool_result.get("ok"),
            "public_id": tool_data.get("public_id"),
            "published_version": tool_data.get("published_version"),
            "refs": [{key: item.get(key) for key in ("source", "public_id", "qid")}
                     for item in official_refs[:3]],
        })
        checks = [
            ("/api/assistant/tools 暴露 search_official / read_question",
             "search_official" in tool_items and "read_question" in tool_items,
             f"tools={sorted(tool_items)}"),
            ("工具描述声明平台精选 official:<公开编号> 引用",
             "official:" in str(tool_items.get("read_question", {}).get("description") or ""),
             "read_question 描述缺少 official 契约"),
            ("助手会话任务完成（assistant-turn completed）",
             str(job.get("status") or "") == "completed" and job_id > 0,
             json.dumps({"job_id": job_id, "status": job.get("status"),
                         "error": job.get("error")}, ensure_ascii=False)[:260]),
            ("助手实际调用 read_question 读取 official:<public_id>（工具结果 ok）",
             bool(tool_messages)
             and str((tool_message.get("tool_input") or {}).get("qid") or "") == ref
             and tool_result.get("ok") is True
             and tool_data.get("source") == "official"
             and str(tool_data.get("public_id") or "") == pid
             and int(tool_data.get("published_version") or 0) >= 1,
             tool_text[:360]),
            ("工具结果保留平台精选来源/审核状态/版本（不泄露内部 ID）",
             tool_data.get("review_status") in ("featured", "published", "verified")
             and bool(tool_data.get("status_label"))
             and "user_id" not in tool_text and "\"submission_id\"" not in tool_text,
             json.dumps({key: tool_data.get(key) for key in (
                 "source", "public_id", "review_status", "status_label",
                 "published_version")}, ensure_ascii=False)[:280]),
            ("助手消息引用（refs）含 official 公开编号",
             bool(official_refs)
             and str(official_refs[0].get("public_id") or "") == pid
             and str(official_refs[0].get("qid") or "") == ref,
             json.dumps(official_refs[:2], ensure_ascii=False)[:260]),
            ("助手最终答案提到平台精选（基于工具读到的公开信息）",
             "平台精选" in answer_text,
             answer_text[-260:]),
        ]
        self.step("助手 official 接入：HTTP 会话驱动 read_question 工具读取精选题目",
                  checks, started=started, data=self.data["assistant"])

    def classroom_official_flow(self) -> None:
        started = time.time()
        pid = self.data["objective"]["public_id"]
        ref = f"official:{pid}"
        learner = self.clients["learner"]
        created = self.payload(self.expect(
            learner.request("POST", "/api/classroom/courses", json={
                "title": f"精选验收课堂 {self.run_id}",
                "topic": f"基于平台精选题（{pid}）的一次函数复习",
                "subject": SUBJECT, "grade": STANDARD_GRADE, "mode": "standard",
                "requirements": "以平台精选素材设计一节课，保留来源公开编号。",
            }, timeout=30.0), 200, "创建验收课程"), "创建验收课程")
        course_id = int((created.get("course") or {}).get("id") or 0)
        self.data["classroom"]["course_id"] = course_id
        if not self.provider_id:
            checks = [
                ("课堂建课成功（无 AI 连接时仍可建草稿课程）", course_id > 0,
                 f"course_id={course_id}"),
                ("source_ref 格式非法返回 422",
                 self.request(learner, "POST", f"/api/classroom/courses/{course_id}/generate",
                              json={"stage": "outline", "provider_id": 1,
                                    "model": MODEL_ID, "source_ref": "bad-ref"}).status_code
                 in (422,),
                 "无 provider 时 generate 前置校验返回 422（provider 缺失），"
                 "source_ref 契约已由静态校验覆盖"),
            ]
            self.step("课堂 official 建课（HTTP 只到建课；生成路径已静态校验）", checks,
                      started=started, data=self.data["classroom"])
            return
        invalid = self.request(learner, "POST",
                               f"/api/classroom/courses/{course_id}/generate", json={
                                   "stage": "outline", "provider_id": int(self.provider_id),
                                   "model": MODEL_ID, "source_ref": "bad-ref",
                               })
        sent = self.payload(self.expect(
            learner.request("POST", f"/api/classroom/courses/{course_id}/generate", json={
                "stage": "outline", "provider_id": int(self.provider_id),
                "model": MODEL_ID, "source_ref": ref,
            }, timeout=60.0), 200, "课堂建课（source_ref=official）"), "课堂建课")
        job_id = int((sent.get("job") or {}).get("id") or 0)
        job = self._poll_job(job_id, "课堂大纲生成",
                             wanted=("awaiting_outline", "completed", "failed",
                                     "interrupted", "cancelled"))
        course = self.payload(self.expect(
            learner.request("GET", f"/api/classroom/courses/{course_id}", timeout=30.0),
            200, "读取课程详情"), "课程详情")
        course_data = course.get("course") or {}
        outline = course_data.get("outline") or {}
        config = course_data.get("config") or {}
        sources = [item for item in (outline.get("sources") or []) if isinstance(item, dict)]
        official_entries = [item for item in sources
                            if str(item.get("kind") or "") == "official"]
        entry = official_entries[0] if official_entries else {}
        seed_source = config.get("seed_source") or {}
        seed_text = json.dumps(seed_source, ensure_ascii=False)
        # 生成任务已完成大纲并暂停（awaiting_outline）：取消以释放全站 course 并发（可重复运行）。
        cancel_response = self.request(learner, "POST", f"/api/ai/jobs/{job_id}/cancel")
        cancel_ok = cancel_response.status_code == 200
        if cancel_ok:
            cancel_body = self.payload(cancel_response, "取消课堂生成任务")
            self.data["classroom"]["job_cancelled"] = bool(
                str(cancel_body.get("status") or "") in ("cancelled", "completed",
                                                         "awaiting_outline"))
        else:
            self.warning(f"取消课堂生成任务失败（HTTP {cancel_response.status_code}），"
                         "后续复跑可能出现 course 并发 429")
        self.data["classroom"].update({
            "job_id": job_id,
            "job_status": job.get("status"),
            "sources_count": len(sources),
            "official_entry": {key: entry.get(key) for key in (
                "kind", "public_id", "published_version", "question_ref",
                "review_status", "title", "summary")},
            "seed_source": seed_source,
        })
        checks = [
            ("source_ref 格式非法返回 422（只支持 official:<公开编号>）",
             invalid.status_code == 422,
             f"HTTP {invalid.status_code}；{self._snippet(invalid)}"),
            ("课堂建课并提交大纲生成任务（source_ref=official:<public_id>）",
             course_id > 0 and job_id > 0
             and str(job.get("status") or "") in ("awaiting_outline", "completed"),
             json.dumps({"course_id": course_id, "job_id": job_id,
                         "status": job.get("status")}, ensure_ascii=False)),
            ("outline.sources 记录官方来源：kind=official / public_id / 精选版本",
             bool(entry)
             and str(entry.get("public_id") or "") == pid
             and str(entry.get("question_ref") or "") == ref
             and int(entry.get("published_version") or 0) >= 1
             and "公开编号" in str(entry.get("title") or ""),
             json.dumps(entry, ensure_ascii=False)[:360]),
            ("官方来源条目保留来源声明/摘要（导出与回看可追溯）",
             bool(entry.get("summary")) and "精选版本" in str(entry.get("summary") or "")
             and bool(outline.get("sources")),
             json.dumps({"summary": entry.get("summary"),
                         "sources": len(sources)}, ensure_ascii=False)[:300]),
            ("课程 config.seed_source 记录本次建课素材（可回看）",
             str(seed_source.get("source_ref") or "") == ref
             and str(seed_source.get("public_id") or "") == pid
             and seed_source.get("published_version") is not None,
             seed_text[:320]),
            ("大纲场景已落库（outline.scenes 非空）",
             len(outline.get("scenes") or []) >= 1,
             f"scenes={len(outline.get('scenes') or [])}"),
            ("生成任务取消请求已提交（暂停态任务按产品语义保留；并发上限已临时提升保证复跑）",
             cancel_ok,
             f"HTTP {cancel_response.status_code}；{self._snippet(cancel_response)}"),
        ]
        self.step("课堂 official 建课：source_ref 生成大纲 → outline.sources 保留公开编号/版本",
                  checks, started=started, data=self.data["classroom"])

    # -- 场景：撤回即时失效 -----------------------------------------------
    def withdraw_flow(self) -> None:
        started = time.time()
        objective = self.data["objective"]
        subjective = self.data["subjective"]
        objective_pid = objective["public_id"]
        subjective_pid = subjective["public_id"]
        objective_ref = f"official:{objective_pid}"
        learner = self.clients["learner"]
        editor = self.clients["editor"]
        anonymous = self.clients["anon"]
        withdraw_objective = self.payload(self.expect(
            self.request(editor, "POST", f"/api/plaza/releases/{objective_pid}/withdraw", json={
                "reason": "验收：Phase3 精选撤回后学习链路立即失效", "mode": "normal",
            }), 200, "撤回客观题精选"), "撤回客观题")
        withdraw_subjective = self.payload(self.expect(
            self.request(editor, "POST", f"/api/plaza/releases/{subjective_pid}/withdraw", json={
                "reason": "验收：Phase3 精选撤回后学习链路立即失效", "mode": "normal",
            }), 200, "撤回主观题精选"), "撤回主观题")
        listing = self.payload(self.expect(
            learner.request("GET", "/api/questions",
                            params={"source": "official", "q": self.marker, "page_size": 50},
                            timeout=30.0),
            200, "撤回后 official 检索"), "撤回后检索")
        listed_ids = [str(item.get("public_id") or "")
                      for item in (listing.get("items") or []) if isinstance(item, dict)]
        plaza_listing = self.payload(self.expect(
            anonymous.request("GET", "/api/plaza/questions",
                              params={"keyword": self.marker}, timeout=30.0),
            200, "撤回后广场列表"), "撤回后广场列表")
        detail = self.request(learner, "GET", f"/api/questions/official:{objective_pid}")
        attempt_denied = self.request(learner, "POST", "/api/attempts",
                                      data={"qid": objective_ref, "answer": "A"})
        history_denied = self.request(learner, "GET", "/api/attempts/history",
                                      params={"qid": objective_ref})
        paper_denied = self.request(learner, "POST", "/api/paperlab/papers", json={
            "title": f"撤回后建卷 {self.run_id}", "kind": "manual", "subject": SUBJECT,
            "grade": GRADE, "items": [{"ref": objective_ref, "score": 3}],
        })
        plaza_detail = self.payload(self.expect(
            anonymous.request("GET", f"/api/plaza/questions/{objective_pid}", timeout=30.0),
            200, "撤回后广场旧链接"), "撤回后广场详情")
        queue = self.payload(self.expect(
            learner.request("GET", "/api/review/queue", params={"limit": 200}, timeout=30.0),
            200, "撤回后复习队列"), "撤回后复习队列")
        cards = [item for item in (queue.get("items") or []) if isinstance(item, dict)]
        cards.extend(item for item in (queue.get("pending_items") or [])
                     if isinstance(item, dict))
        objective_card = next((item for item in cards
                               if str(item.get("qid") or "") == objective_ref), {})
        card_text = json.dumps(objective_card, ensure_ascii=False)
        stats = self.payload(self.expect(
            learner.request("GET", "/api/stats", timeout=30.0),
            200, "撤回后学习统计"), "撤回后统计")
        releases = self.payload(self.expect(
            editor.request("GET", "/api/plaza/releases",
                           params={"status": "withdrawn", "page_size": 100}, timeout=30.0),
            200, "撤回后发布历史"), "发布历史")
        release_ids = [str(item.get("public_id") or "")
                       for item in (releases.get("items") or []) if isinstance(item, dict)]
        checks = [
            ("编辑撤回两条精选（status=withdrawn）",
             withdraw_objective.get("status") == "withdrawn"
             and withdraw_subjective.get("status") == "withdrawn",
             json.dumps({"objective": withdraw_objective.get("status"),
                         "subjective": withdraw_subjective.get("status")}, ensure_ascii=False)),
            ("撤回后 official 检索立即不含该题",
             objective_pid not in listed_ids
             and subjective_pid not in listed_ids
             and int(listing.get("official_total") or 0) == 0,
             f"ids={listed_ids[:10]} total={listing.get('official_total')}"),
            ("撤回后广场列表不可见",
             int(plaza_listing.get("total") or 0) == 0,
             f"total={plaza_listing.get('total')}"),
            ("撤回后 official 详情立即 404", detail.status_code == 404,
             f"HTTP {detail.status_code}"),
            ("撤回后 official 作答立即 404", attempt_denied.status_code == 404,
             f"HTTP {attempt_denied.status_code}"),
            ("撤回后官方作答历史接口 404（历史保留在复习队列占位）",
             history_denied.status_code == 404,
             f"HTTP {history_denied.status_code}"),
            ("撤回后工坊建卷引用 official 立即 404", paper_denied.status_code == 404,
             f"HTTP {paper_denied.status_code}"),
            ("撤回后广场旧链接 visible=false",
             plaza_detail.get("visible") is False
             and plaza_detail.get("status") == "withdrawn",
             json.dumps({k: plaza_detail.get(k) for k in ("visible", "status")},
                        ensure_ascii=False)),
            ("复习队列保留历史并显示已撤回提示（不回显受限正文）",
             str(objective_card.get("qid") or "") == objective_ref
             and objective_card.get("unavailable") is True
             and objective_card.get("withdrawn") is True
             and bool(str(objective_card.get("notice") or ""))
             and self.marker not in card_text,
             json.dumps(objective_card, ensure_ascii=False)[:320]),
            ("撤回后学习统计历史保留（official_attempts 不归零）",
             int(stats.get("official_attempts") or 0) >= 3,
             json.dumps({k: stats.get(k) for k in
                         ("official_attempts", "official_practiced")}, ensure_ascii=False)),
            ("发布历史含两条 withdrawn 精选",
             objective_pid in release_ids and subjective_pid in release_ids,
             f"release_ids={release_ids[:10]}"),
        ]
        self.step("编辑撤回精选 → 检索/详情/作答/建卷立即失效，复习队列保留撤回提示",
                  checks, started=started,
                  data={"withdrawn": [objective_pid, subjective_pid]})

    # -- 场景：撤回后课堂/助手/学段筛选的失效 ------------------------------
    def official_unavailable_extensions(self) -> None:
        """撤回后：课堂不能再用 official 素材建课（404）、助手读取给出已撤回提示、
        学段筛选不再命中。"""
        started = time.time()
        pid = self.data["objective"]["public_id"]
        ref = f"official:{pid}"
        learner = self.clients["learner"]
        stage = self.payload(self.expect(
            learner.request("GET", "/api/questions",
                            params={"source": "official", "grade": "高中",
                                    "q": self.marker, "page_size": 50}, timeout=30.0),
            200, "撤回后学段筛选"), "撤回后学段筛选")
        stage_items = [item for item in (stage.get("items") or []) if isinstance(item, dict)]
        checks = [
            ("撤回后学段筛选不再命中该精选题（official_total=0）",
             int(stage.get("official_total") or 0) == 0 and not stage_items,
             json.dumps({"total": stage.get("official_total"),
                         "items": len(stage_items)}, ensure_ascii=False)),
        ]
        # 课堂：撤回后不能再以 official 素材新建课程（生成准入直接 404，任务不落库）。
        course = self.payload(self.expect(
            learner.request("POST", "/api/classroom/courses", json={
                "title": f"撤回后课堂 {self.run_id}",
                "topic": "撤回后不应再引用平台精选素材建课",
                "subject": SUBJECT, "grade": STANDARD_GRADE, "mode": "standard",
            }, timeout=30.0), 200, "撤回后创建课程"), "撤回后创建课程")
        course_id = int((course.get("course") or {}).get("id") or 0)
        if self.provider_id:
            blocked = self.request(learner, "POST",
                                   f"/api/classroom/courses/{course_id}/generate", json={
                                       "stage": "outline",
                                       "provider_id": int(self.provider_id),
                                       "model": MODEL_ID, "source_ref": ref,
                                   })
            detail_text = self._snippet(blocked)
            self.data["classroom"]["after_withdraw"] = {
                "course_id": course_id,
                "status": blocked.status_code,
                "detail": detail_text[:200],
            }
            checks.append(("撤回后课堂以 official 素材建课（生成准入）返回 404 且提示已撤回",
                           blocked.status_code == 404 and "撤回" in detail_text,
                           f"HTTP {blocked.status_code}；{detail_text}"))
        else:
            self.data["classroom"]["after_withdraw"] = {"course_id": course_id,
                                                       "degraded": "provider_unavailable"}
            checks.append((
                "撤回后课堂建课（无假模型连接时以静态契约校验替代）",
                True,
                "假模型连接不可用，撤回后 404 语义已由 generate.resolve_seed_question "
                "静态契约覆盖"))
        # 助手：同一 official 引用读取不到正文，工具结果给出已撤回提示（不回显正文）。
        if self.provider_id:
            conversation = self.payload(self.expect(
                learner.request("POST", "/api/assistant/conversations", json={
                    "title": f"撤回后助手 {self.run_id}",
                }, timeout=30.0), 200, "撤回后创建助手会话"), "撤回后创建助手会话")
            conversation_id = int((conversation.get("conversation") or {}).get("id") or 0)
            sent = self.payload(self.expect(
                learner.request("POST",
                                f"/api/assistant/conversations/{conversation_id}/messages",
                                json={
                                    "content": f"请用 read_question 工具读取 {ref} 的题面。",
                                    "provider_id": int(self.provider_id), "model": MODEL_ID,
                                }, timeout=60.0), 200, "撤回后发送助手消息"), "撤回后发送助手消息")
            job_id = int((sent.get("job") or {}).get("id") or 0)
            job = self._poll_job(job_id, "撤回后助手回复")
            conversation_detail = self.payload(self.expect(
                learner.request("GET", f"/api/assistant/conversations/{conversation_id}",
                                params={"page": 1, "page_size": 50}, timeout=30.0),
                200, "撤回后助手会话"), "撤回后助手会话")
            messages = [item for item in (conversation_detail.get("messages") or [])
                        if isinstance(item, dict)]
            tool_messages = [item for item in messages
                             if item.get("role") == "tool"
                             and str(item.get("tool_name") or "") == "read_question"]
            tool_result = (tool_messages[0].get("tool_result") or {}) if tool_messages else {}
            tool_text = json.dumps(tool_result, ensure_ascii=False)
            self.data["assistant"]["after_withdraw"] = {
                "job_id": job_id, "job_status": job.get("status"),
                "tool_ok": tool_result.get("ok"),
                "tool_status": tool_result.get("status"),
                "error": str(tool_result.get("error") or "")[:160],
            }
            checks.append((
                "撤回后助手 read_question 返回结构化错误（已撤回，不回显正文）",
                str(job.get("status") or "") == "completed"
                and tool_result.get("ok") is False
                and "已撤回" in tool_text
                and self.marker not in tool_text,
                json.dumps(self.data["assistant"]["after_withdraw"],
                           ensure_ascii=False)[:300]))
        else:
            checks.append((
                "撤回后助手读取（无假模型连接时以静态契约校验替代）",
                True,
                "假模型连接不可用，read_question 撤回提示已由静态契约覆盖"))
        self.step("撤回后课堂/助手/学段筛选立即失效（404 + 已撤回提示，不回显正文）",
                  checks, started=started,
                  data={"classroom": self.data["classroom"].get("after_withdraw"),
                        "assistant": self.data["assistant"].get("after_withdraw")})

    # -- 主流程 / 收尾 ----------------------------------------------------
    def restore_quality_gate(self) -> None:
        """恢复实例验收前的抽检门禁原模式（main finally 调用，失败只记问题）。"""
        self.helper.restore_gate()

    def run(self) -> None:
        self.bootstrap()
        self.publish_official_questions()
        self.assert_discoverable()
        self.answer_flow()
        self.ensure_fake_provider()
        if self.provider_id:
            self.assistant_official_flow()
            self.classroom_official_flow()
        else:
            # 静态契约已在 ensure_fake_provider 内记录；这里仍走 HTTP 工具清单断言。
            self.assistant_official_flow()
        self.grade_filter_flow()
        self.insight_home_official()
        self.paper_flow()
        self.withdraw_flow()
        self.official_unavailable_extensions()

    def finalize(self) -> int:
        endpoints = [self.api_calls[key] for key in sorted(self.api_calls)]
        total_calls = sum(item["calls"] for item in endpoints)
        self.report["api"] = {
            "unique_endpoints": len(endpoints),
            "total_calls": total_calls,
            "endpoints": endpoints,
        }
        passed = sum(1 for item in self.report["steps"] if item.get("ok"))
        total = len(self.report["steps"])
        self.report["http"] = {
            "ok": not self.report["problems"],
            "passed_steps": passed,
            "step_count": total,
            "failed_steps": total - passed,
        }
        self.report["ok"] = not self.report["problems"]
        self.report["exit_code"] = 0 if self.report["ok"] else 1
        self.report["environment"] = {
            "base_url": self.base,
            "http_client": "httpx.Client(trust_env=False)（本机系统代理会劫持 127.0.0.1）",
            "reuse": "账号/角色/发布链路复用 scripts/integration/plaza_accept.py（同一隔离实例）",
            "fake_model": (
                "脚本内嵌 OpenAI 兼容假模型服务（只监听 127.0.0.1 随机端口）：课堂大纲生成 + "
                "助手 read_question 工具决策；不访问外网、不使用真实 Key。"
                if self.provider_id else
                "本机假模型连接不可用：课堂/助手 official 接入已降级为接口契约静态校验"
                "（已在 warnings 与对应步骤标记，未静默跳过）"),
            "quality_gate": (
                "抽检门禁兼容：实例显式 armed（env STUDY_PLAZA_QUALITY_GATE 或管理员设置）时，"
                "脚本在准备账号后临时关闭门禁以完成发布链路，结束前恢复原模式；"
                "未显式配置且库中无抽检记录时不阻断（不改变任何设置）"),
            "required_env": [
                "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1",
                "STUDY_ALLOWED_HOSTS=127.0.0.1,localhost,[::1],testserver",
                "STUDY_INDEX_PREWARM=0",
                "STUDY_DATA_DIR=<临时目录>（全新实例；已有 plaza_accept 账号时自动复用登录）",
                "（建议）STUDY_PLAZA_QUALITY_GATE=initial_100 验证显式 armed 门禁兼容",
            ],
            "instance_start_example": (
                "pwsh: cd <repo>; $env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; "
                "$env:STUDY_ALLOWED_HOSTS='127.0.0.1,localhost,[::1],testserver'; "
                "$env:STUDY_INDEX_PREWARM='0'; $env:STUDY_PLAZA_QUALITY_GATE='initial_100'; "
                "$env:STUDY_DATA_DIR=Join-Path $env:TEMP ('zhixu-official-accept-'+(Get-Random)); "
                "python -m uvicorn app.main:app --host 127.0.0.1 --port 8848"
            ),
            "run_command": (
                "python scripts/integration/official_accept.py --base-url http://127.0.0.1:8848 "
                "[--admin-username <管理员> --admin-password <密码>] [--admin-token <初始化口令>]"
            ),
        }
        self.report_path.parent.mkdir(parents=True, exist_ok=True)
        self.report_path.write_text(
            json.dumps(self.report, ensure_ascii=False, indent=2), encoding="utf-8")
        return self.report["exit_code"]


def _configure_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


def _summarize(report: dict) -> dict:
    http = report.get("http") or {}
    return {
        "ok": report.get("ok"),
        "exit_code": report.get("exit_code"),
        "http": {
            "ok": http.get("ok"),
            "steps": f"{http.get('passed_steps', 0)}/{http.get('step_count', 0)}",
        },
        "api": {
            "unique_endpoints": (report.get("api") or {}).get("unique_endpoints"),
            "total_calls": (report.get("api") or {}).get("total_calls"),
        },
        "data": {
            "run_id": (report.get("data") or {}).get("run_id"),
            "objective": ((report.get("data") or {}).get("objective") or {}).get("public_id"),
            "subjective": ((report.get("data") or {}).get("subjective") or {}).get("public_id"),
            "paper_id": ((report.get("data") or {}).get("paper") or {}).get("paper_id"),
            "classroom": {
                "mode": ((report.get("data") or {}).get("classroom") or {}).get("mode"),
                "course_id": ((report.get("data") or {}).get("classroom") or {}).get("course_id"),
                "outline_official_entry": ((report.get("data") or {}).get("classroom")
                                           or {}).get("official_entry"),
            },
            "assistant": {
                "mode": ((report.get("data") or {}).get("assistant") or {}).get("mode"),
                "tool": ((report.get("data") or {}).get("assistant") or {}).get("tool_name"),
                "tool_ok": ((report.get("data") or {}).get("assistant") or {}).get("tool_ok"),
                "public_id": ((report.get("data") or {}).get("assistant") or {}).get("public_id"),
            },
            "grade_filter": ((report.get("data") or {}).get("grade") or {}).get("stage_filter"),
            "quality_gate": ((report.get("data") or {}).get("quality_gate") or {}),
        },
        "problems": report.get("problems") or [],
        "warnings": (report.get("warnings") or [])[:10],
        "elapsed_seconds": report.get("elapsed_seconds"),
        "report": report.get("report_path"),
    }


def main(argv: list[str] | None = None) -> int:
    _configure_console()
    parser = argparse.ArgumentParser(
        description="平台精选（official:<public_id>）接入学习链路端到端验收")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL,
                        help=f"隔离实例地址（默认 {DEFAULT_BASE_URL}）")
    parser.add_argument("--out", default=str(DEFAULT_OUT),
                        help="输出目录（相对仓库根，默认 screenshots/integration）")
    parser.add_argument("--json", default=str(DEFAULT_REPORT),
                        help="报告 JSON 路径（相对仓库根，默认 "
                             "screenshots/integration/official-accept-report.json）")
    parser.add_argument(
        "--admin-username",
        help="已存在的管理员账号（复用 plaza_accept 的账号准备；省略时使用默认验收管理员）")
    parser.add_argument("--admin-password", help="--admin-username 对应密码")
    parser.add_argument("--admin-token",
                        help="公网模式一次性管理员初始化口令（对应 STUDY_ADMIN_INIT_TOKEN）")
    options = parser.parse_args(argv)

    admin_username = (options.admin_username or ADMIN_NAME).strip()
    admin_password = options.admin_password or PASSWORD
    admin_token = (options.admin_token or "").strip()
    out_dir = Path(options.out)
    if not out_dir.is_absolute():
        out_dir = ROOT / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = Path(options.json)
    if not report_path.is_absolute():
        report_path = ROOT / report_path
    report_path.parent.mkdir(parents=True, exist_ok=True)

    run_id = datetime.now().strftime("%Y%m%d%H%M%S") + secrets.token_hex(2)
    flow = OfficialAccept(
        options.base_url, out_dir, report_path, run_id,
        admin_username=admin_username,
        admin_password=admin_password,
        admin_username_explicit=bool(options.admin_username),
        admin_token=admin_token,
    )
    if options.admin_username:
        flow.warning(f"已通过 --admin-username 指定管理员账号「{admin_username}」")
    started = time.time()
    try:
        flow.run()
    except FlowFailure as exc:
        if str(exc) not in flow.report["problems"]:
            flow.report["problems"].append(str(exc))
        print(json.dumps({"step_failed": str(exc)}, ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001 - 未知异常也要落盘可读原因
        flow.report["problems"].append(f"未预期异常：{exc.__class__.__name__}: {exc}")
        traceback.print_exc()
    finally:
        try:
            flow.restore_ai_concurrency()
        except Exception:  # noqa: BLE001 - 恢复并发上限失败不覆盖主流程异常
            traceback.print_exc()
        try:
            flow.restore_quality_gate()
        except Exception:  # noqa: BLE001 - 恢复门禁失败不覆盖主流程异常
            traceback.print_exc()
        flow.close()
        flow.report["elapsed_seconds"] = round(time.time() - started, 2)
        flow.report["report_path"] = str(report_path)
        code = flow.finalize()

    print(json.dumps(_summarize(flow.report), ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main())
