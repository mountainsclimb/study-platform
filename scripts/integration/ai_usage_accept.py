"""AI 用量可追踪与配额/并发端到端验收（WS24 Phase 3 · WS23 交付）。

覆盖场景（全部经公开 HTTP API，不直接读写数据库；不产生真实模型费用）：

1. **本机假模型连接**：脚本内嵌一个只监听 ``127.0.0.1`` 的 OpenAI 兼容假模型服务
   （支持流式/非流式，回传 token usage），通过 ``POST /api/providers`` 为普通用户创建
   指向该假服务的连接（地址仅写入隔离实例数据目录；不访问外网、不使用真实 Key）；
2. **任务与用量入账**：普通用户对一道已解锁题库题创建 ``explain`` 任务 → 假模型返回固定
   讲解 → 任务 ``completed`` → ``GET /api/ai/usage/me`` 出现该 ``job_id`` 的用量行
   （kind/model/status/duration/tokens/input_kind，不记录题目内容明文）；
3. **管理台汇总**：管理员 ``GET /api/admin/ai/usage`` 可见全站汇总（总量/失败率/P50/P95/
   token/by_kind/by_model/分页）；普通用户访问管理接口 403；
4. **配额收紧 → 429**：管理员 ``POST /api/admin/ai/quotas`` 把 ``explain`` 每日配额调为 1，
   普通用户当日已消耗 1 次后再建任务返回 429（``ai_quota_exceeded``），且被拒请求不落库、
   不消耗配额；随后 ``reset=true`` 恢复内置默认配额；
5. **权限负向**：匿名访问用量接口 401、普通用户不能管理配额、未知任务类型配额 422；
6. 产出 JSON 报告（``screenshots/integration/ai-usage-accept-report.json``）与退出码 0/1；
   脚本退出前删除本次创建的假模型连接并停止假模型服务，可重复运行。

环境约束：``httpx.Client(trust_env=False)``；隔离实例需本地模式（``local_open``）才允许
指向 ``127.0.0.1`` 的模型地址；公网模式下本脚本会给出可读失败提示（不会静默跳过）。
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
if str(SCRIPT_DIR) not in sys.path:  # 与 plaza_accept 共享验收账号命名（accept_runner 串联）
    sys.path.insert(0, str(SCRIPT_DIR))

from plaza_accept import (  # noqa: E402 - 先修正 sys.path 再导入同目录脚本
    ADMIN_NAME,
    AUTHOR_NAME,
    PASSWORD,
    ROOT,
    FlowFailure,
)

DEFAULT_OUT = ROOT / "screenshots" / "integration"
DEFAULT_REPORT = DEFAULT_OUT / "ai-usage-accept-report.json"
DEFAULT_BASE_URL = "http://127.0.0.1:8849"

PROVIDER_NAME = "验收假模型连接"
MODEL_ID = "mock-usage-model"
FAKE_API_KEY = "sk-local-acceptance-only"

EXPLAIN_CONTENT = "验收假模型讲解：把 x=2 代入 f(x)=x+1 得 f(2)=3，因此选 A。"
TOKENS_IN = 120
TOKENS_OUT = 80

SCENARIO = [
    "管理员/普通用户账号就绪，启动本机假模型服务并创建验收连接",
    "普通用户对已解锁题创建 explain 任务 → 假模型完成 → 任务 completed",
    "/api/ai/usage/me 出现该任务用量行（kind/model/tokens/耗时/入参分类）",
    "管理员 /api/admin/ai/usage 全站汇总可见；普通用户 403",
    "配额调小为 1 后再建任务返回 429，且被拒请求不落库、不消耗配额",
    "恢复默认配额并清理临时连接，报告 JSON 与退出码",
]


# ---------------------------------------------------------------------------
# 本机假模型服务（只监听回环地址；固定回复 + 固定 token usage）
# ---------------------------------------------------------------------------

class _FakeModelHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # 静默日志：验收输出更干净
        return

    def _send(self, status: int, payload: bytes, content_type: str):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path.split("?")[0].rstrip("/") in ("/v1/models", "/models"):
            body = json.dumps({"object": "list", "data": [
                {"id": MODEL_ID, "object": "model"}]}).encode("utf-8")
            return self._send(200, body, "application/json")
        return self._send(404, b'{"error":{"message":"not found"}}', "application/json")

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:  # noqa: BLE001 - 非法请求按非流式返回
            body = {}
        usage = {
            "prompt_tokens": TOKENS_IN,
            "completion_tokens": TOKENS_OUT,
            "total_tokens": TOKENS_IN + TOKENS_OUT,
        }
        if body.get("stream"):
            chunks = [
                {"id": "chatcmpl-mock", "object": "chat.completion.chunk", "model": MODEL_ID,
                 "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""},
                              "finish_reason": None}]},
                {"id": "chatcmpl-mock", "object": "chat.completion.chunk", "model": MODEL_ID,
                 "choices": [{"index": 0, "delta": {"content": EXPLAIN_CONTENT},
                              "finish_reason": None}]},
                {"id": "chatcmpl-mock", "object": "chat.completion.chunk", "model": MODEL_ID,
                 "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                 "usage": usage},
            ]
            events = [f"data: {json.dumps(item, ensure_ascii=False)}" for item in chunks]
            events.append("data: [DONE]")
            payload = ("\n\n".join(events) + "\n\n").encode("utf-8")
            return self._send(200, payload, "text/event-stream")
        payload = json.dumps({
            "id": "chatcmpl-mock", "object": "chat.completion", "created": 1,
            "model": MODEL_ID,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": EXPLAIN_CONTENT},
                         "finish_reason": "stop"}],
            "usage": usage,
        }, ensure_ascii=False).encode("utf-8")
        return self._send(200, payload, "application/json")


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
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeModelHandler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            try:
                self._server.shutdown()
            except Exception:  # noqa: BLE001
                pass
            try:
                self._server.server_close()
            except Exception:  # noqa: BLE001
                pass
        self._server = None
        self._thread = None


# ---------------------------------------------------------------------------
# 验收主体
# ---------------------------------------------------------------------------

class AiUsageAccept:
    def __init__(self, base_url: str, out_dir: Path, report_path: Path, run_id: str, *,
                 admin_username: str = ADMIN_NAME, admin_password: str = PASSWORD,
                 admin_username_explicit: bool = False, normal_username: str = AUTHOR_NAME):
        self.base = base_url.rstrip("/")
        self.out = out_dir
        self.report_path = report_path
        self.run_id = run_id
        self.admin_username = admin_username
        self.admin_password = admin_password
        self.admin_username_explicit = admin_username_explicit
        self.normal_username = normal_username
        self.clients: dict[str, httpx.Client] = {}
        self.fake = FakeModelServer()
        self.api_calls: dict[str, dict] = {}
        self.provider_id: int | None = None
        self.job_id: int | None = None
        self.data: dict = {
            "run_id": run_id,
            "provider": {},
            "job": {},
            "quota": {},
            "accounts": {},
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
                   "problems": [], "warnings": ["本脚本只验 HTTP 链路，无 Playwright 冒烟"],
                   "screenshots": []},
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
        try:
            response = client.request(method, path, timeout=kwargs.pop("timeout", 30.0), **kwargs)
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

    # -- 账号 -------------------------------------------------------------
    def _login_or_register(self, client: httpx.Client, username: str,
                           password: str) -> dict:
        payload = {"username": username, "password": password}
        login = self.request(client, "POST", "/api/auth/login", json=payload)
        mode = "login"
        if login.status_code != 200:
            register = self.request(client, "POST", "/api/auth/register", json=payload)
            if register.status_code == 200:
                mode = "register"
            elif register.status_code in (403, 409):
                retry = self.request(client, "POST", "/api/auth/login", json=payload)
                if retry.status_code != 200:
                    raise FlowFailure(
                        f"账号「{username}」注册失败（HTTP {register.status_code}）且登录失败"
                        f"（HTTP {retry.status_code}：{self._snippet(retry)}）；"
                        f"请使用全新 STUDY_DATA_DIR 或 --admin-username 指定已存在管理员")
                mode = "login-fallback"
            elif register.status_code == 429:
                raise FlowFailure(
                    f"注册账号「{username}」触发限流（429）：请等待或换全新 STUDY_DATA_DIR")
            else:
                raise FlowFailure(
                    f"注册账号「{username}」失败：HTTP {register.status_code}；"
                    f"响应：{self._snippet(register)}")
        me = self.payload(self.request(client, "GET", "/api/auth/me"), "读取账号会话")
        if not isinstance(me, dict) or me.get("username") != username:
            raise FlowFailure(
                f"账号「{username}」会话异常：{json.dumps(me, ensure_ascii=False)[:200]}")
        return {"user": me, "mode": mode}

    def bootstrap(self) -> None:
        started = time.time()
        admin_client = self.new_client()
        admin_info = self._login_or_register(admin_client, self.admin_username,
                                             self.admin_password)
        admin_user = admin_info["user"]
        if str(admin_user.get("role") or "") != "admin":
            raise FlowFailure(
                f"管理员账号「{self.admin_username}」role={admin_user.get('role')} 不是 admin；"
                f"请用 --admin-username 指定已存在的管理员，或换全新 STUDY_DATA_DIR "
                f"（本地模式首个注册账号自动成为管理员）")
        normal_client = self.new_client()
        normal_info = self._login_or_register(normal_client, self.normal_username, PASSWORD)
        self.clients = {"admin": admin_client, "normal": normal_client, "anon": self.new_client()}
        self.data["accounts"] = {
            "admin": {"username": self.admin_username, "mode": admin_info["mode"],
                      "user_id": admin_user.get("id")},
            "normal": {"username": self.normal_username, "mode": normal_info["mode"],
                       "user_id": normal_info["user"].get("id")},
        }
        self.fake.start()
        self.data["fake_model"] = {"base_url": self.fake.base_url, "model": MODEL_ID,
                                   "listens_on": "127.0.0.1（仅回环）"}
        self.warning(f"本机假模型服务：{self.fake.base_url}（仅 127.0.0.1，不访问外网）")
        checks = [
            ("管理员账号就绪且 role=admin",
             str(admin_user.get("role") or "") == "admin",
             json.dumps({"username": self.admin_username,
                         "role": admin_user.get("role")}, ensure_ascii=False)),
            ("普通用户账号就绪（非管理员）",
             bool(normal_info["user"].get("id"))
             and str(normal_info["user"].get("role") or "") != "admin",
             json.dumps({"username": self.normal_username,
                         "role": normal_info["user"].get("role")}, ensure_ascii=False)),
            ("本机假模型服务已启动（只监听回环）",
             "127.0.0.1" in self.fake.base_url, self.fake.base_url),
        ]
        self.step("准备验收账号与本机假模型服务", checks, started=started)

    # -- 连接与任务 -------------------------------------------------------
    def ensure_provider(self) -> None:
        started = time.time()
        normal = self.clients["normal"]
        providers = self.payload(self.expect(
            self.request(normal, "GET", "/api/providers"), 200, "读取模型连接"), "模型连接")
        items = [item for item in (providers.get("items") or []) if isinstance(item, dict)]
        existing = next((item for item in items
                         if str(item.get("name") or "") == PROVIDER_NAME), None)
        payload = {
            "name": PROVIDER_NAME,
            "kind": "openai",
            "base_url": self.fake.base_url,
            "api_key": FAKE_API_KEY,
            "models": [MODEL_ID],
            "auth_mode": "bearer",
        }
        if existing is None:
            created = self.payload(self.expect(
                self.request(normal, "POST", "/api/providers", json=payload),
                200, "创建验收假模型连接"), "创建连接")
            provider_id = int(created.get("id") or 0)
            mode = "created"
        else:
            provider_id = int(existing.get("id") or 0)
            updated = self.payload(self.expect(
                self.request(normal, "PATCH", f"/api/providers/{provider_id}", json={
                    "base_url": self.fake.base_url, "models": [MODEL_ID],
                    "api_key": FAKE_API_KEY, "kind": "openai", "auth_mode": "bearer",
                }), 200, "复用并更新验收假模型连接"), "更新连接")
            provider_id = int(updated.get("id") or provider_id)
            mode = "reused"
        self.provider_id = provider_id
        self.data["provider"] = {"provider_id": provider_id, "mode": mode,
                                 "base_url": self.fake.base_url, "model": MODEL_ID}
        refreshed = self.payload(self.expect(
            self.request(normal, "GET", "/api/providers"), 200, "复核模型连接"), "模型连接")
        entry = next((item for item in (refreshed.get("items") or [])
                      if isinstance(item, dict)
                      and int(item.get("id") or 0) == provider_id), {})
        model_ids = [detail.get("model_id") for detail in (entry.get("model_details") or [])
                     if isinstance(detail, dict)]
        checks = [
            ("假模型连接就绪（base_url 指向本机假模型）",
             provider_id > 0 and str(entry.get("base_url") or "") == self.fake.base_url,
             json.dumps({k: entry.get(k) for k in ("id", "base_url")}, ensure_ascii=False)),
            ("连接包含验收模型且 Key 已加密保存",
             MODEL_ID in (entry.get("models") or []) and MODEL_ID in model_ids
             and entry.get("has_api_key") is True,
             json.dumps({"models": entry.get("models"),
                         "has_api_key": entry.get("has_api_key")}, ensure_ascii=False)),
        ]
        self.step("创建/复用本机假模型连接（POST /api/providers）", checks,
                  started=started, data=self.data["provider"])

    def prepare_question(self) -> None:
        started = time.time()
        normal = self.clients["normal"]
        listing = self.payload(self.expect(
            self.request(normal, "GET", "/api/questions",
                         params={"source": "bank", "subject": "数学", "page_size": 20}),
            200, "题库检索"), "题库列表")
        items = [item for item in (listing.get("items") or []) if isinstance(item, dict)]
        chosen = next(
            (item for item in items
             if item.get("practice_allowed") is True
             and item.get("objective") is True
             and bool(item.get("options"))
             and bool(item.get("qid"))), None)
        if chosen is None:
            raise FlowFailure(
                "题库检索未找到可用于验收的客观题（需要 practice_allowed/objective/options）；"
                "请确认隔离实例题库基线可用")
        qid = str(chosen.get("qid") or "")
        answer_label = str((chosen.get("options") or [{}])[0].get("label") or "A")
        attempt = self.payload(self.expect(
            self.request(normal, "POST", "/api/attempts",
                         data={"qid": qid, "answer": answer_label}), 200, "作答解锁"), "作答")
        self.data["question"] = {"qid": qid, "answer_sent": answer_label,
                                 "attempt_id": attempt.get("attempt_id")}
        checks = [
            ("题库客观题已选（qid 有效）", bool(qid), f"qid={qid[:80]}"),
            ("作答解锁成功（explain 任务前置条件）",
             bool(attempt.get("attempt_id")), json.dumps(attempt.get("result") or "",
                                                         ensure_ascii=False)),
        ]
        self.step("选择题库客观题并作答解锁（explain 任务前置）", checks, started=started)

    def wait_job(self, job_id: int, *, timeout: float = 90.0) -> dict:
        normal = self.clients["normal"]
        deadline = time.time() + timeout
        last: dict = {}
        while time.time() < deadline:
            last = self.payload(self.expect(
                self.request(normal, "GET", f"/api/ai/jobs/{job_id}", timeout=30.0),
                200, "查询任务状态"), "任务状态")
            status = str(last.get("status") or "")
            if status in ("completed", "failed", "cancelled", "interrupted"):
                return last
            time.sleep(0.5)
        raise FlowFailure(
            f"AI 任务 {job_id} 在 {timeout:.0f}s 内未到终态（最后状态 "
            f"{last.get('status')!r}）；请确认假模型服务与连接可用")

    def create_explain_job(self) -> dict:
        normal = self.clients["normal"]
        question = self.data["question"]
        created = self.payload(self.expect(
            self.request(normal, "POST", "/api/ai/jobs", json={
                "task": "explain",
                "qid": question["qid"],
                "provider_id": self.provider_id,
                "model": MODEL_ID,
            }), 200, "创建 AI 讲解任务"), "创建任务")
        job_id = int(created.get("id") or 0)
        self.job_id = job_id
        return self.wait_job(job_id)

    def job_flow(self) -> None:
        started = time.time()
        finished = self.create_explain_job()
        job_id = int(finished.get("id") or 0)
        result = finished.get("result") or {}
        self.data["job"] = {
            "job_id": job_id,
            "status": finished.get("status"),
            "model": MODEL_ID,
        }
        checks = [
            ("explain 任务创建并完成（status=completed）",
             job_id == self.job_id and finished.get("status") == "completed",
             json.dumps({"job_id": job_id, "status": finished.get("status")},
                        ensure_ascii=False)),
            ("任务结果含假模型讲解正文（未产生真实模型费用）",
             EXPLAIN_CONTENT in json.dumps(result, ensure_ascii=False),
             json.dumps(result, ensure_ascii=False)[:280]),
        ]
        self.step("普通用户创建 explain 任务 → 本机假模型完成", checks, started=started)

    # -- 用量查询 ---------------------------------------------------------
    def usage_flow(self) -> None:
        started = time.time()
        normal = self.clients["normal"]
        admin = self.clients["admin"]
        anon = self.clients["anon"]
        me = self.payload(self.expect(
            self.request(normal, "GET", "/api/ai/usage/me", params={"days": 7}),
            200, "本人 AI 用量"), "本人用量")
        recent = [item for item in (me.get("recent") or []) if isinstance(item, dict)]
        my_row = next((item for item in recent
                       if int(item.get("job_id") or 0) == int(self.job_id or 0)), {})
        quota_kinds = [item.get("kind") for item in
                       ((me.get("quota") or {}).get("kinds") or []) if isinstance(item, dict)]
        today = me.get("today") or {}
        anonymous = self.request(anon, "GET", "/api/ai/usage/me")
        admin_usage = self.payload(self.expect(
            self.request(admin, "GET", "/api/admin/ai/usage", params={"days": 7}),
            200, "管理台 AI 用量"), "管理台用量")
        totals = admin_usage.get("totals") or {}
        by_kind = [item for item in (admin_usage.get("by_kind") or []) if isinstance(item, dict)]
        by_model = [item for item in (admin_usage.get("by_model") or []) if isinstance(item, dict)]
        page = admin_usage.get("page") or {}
        page_items = [item for item in (page.get("items") or []) if isinstance(item, dict)]
        normal_denied = self.request(normal, "GET", "/api/admin/ai/usage")
        checks = [
            ("/api/ai/usage/me 含本次任务用量行",
             int(my_row.get("job_id") or 0) == int(self.job_id or 0)
             and my_row.get("kind") == "explain"
             and my_row.get("status") == "completed"
             and my_row.get("model") == MODEL_ID,
             json.dumps(my_row, ensure_ascii=False)[:320]),
            ("用量行记录 token / 耗时 / 入参分类（不含题目内容）",
             int(my_row.get("tokens_in") or 0) >= TOKENS_IN
             and int(my_row.get("tokens_out") or 0) >= TOKENS_OUT
             and int(my_row.get("duration_ms") or 0) >= 0
             and my_row.get("input_kind") == "text"
             and self.data["question"]["qid"] not in json.dumps(my_row, ensure_ascii=False),
             json.dumps({k: my_row.get(k) for k in
                         ("tokens_in", "tokens_out", "duration_ms", "input_kind")},
                        ensure_ascii=False)),
            ("本人今日/配额汇总包含 explain",
             int(today.get("jobs") or 0) >= 1 and "explain" in quota_kinds,
             json.dumps({"today_jobs": today.get("jobs"), "quota_kinds": quota_kinds[:8]},
                        ensure_ascii=False)),
            ("匿名不能读取本人用量（401）", anonymous.status_code == 401,
             f"HTTP {anonymous.status_code}"),
            ("管理台汇总可见（总量/失败率/P50/P95/tokens）",
             int(totals.get("jobs") or 0) >= 1
             and totals.get("failure_rate") is not None
             and totals.get("duration_p50_ms") is not None
             and totals.get("duration_p95_ms") is not None
             and int(totals.get("tokens_in") or 0) >= TOKENS_IN
             and any(item.get("kind") == "explain" for item in by_kind)
             and any(item.get("model") == MODEL_ID for item in by_model)
             and any(int(item.get("job_id") or 0) == int(self.job_id or 0)
                     for item in page_items),
             json.dumps({"totals": totals, "by_kind": by_kind[:4], "by_model": by_model[:4]},
                        ensure_ascii=False)[:400]),
            ("普通用户不能看全站用量（403）",
             normal_denied.status_code == 403, f"HTTP {normal_denied.status_code}"),
        ]
        self.data["usage"] = {
            "jobs": totals.get("jobs"), "failure_rate": totals.get("failure_rate"),
            "tokens_in": totals.get("tokens_in"), "tokens_out": totals.get("tokens_out"),
        }
        self.step("AI 用量查询：本人在账 / 管理台汇总 / 权限负向", checks, started=started)

    # -- 配额 -------------------------------------------------------------
    def quota_flow(self) -> None:
        started = time.time()
        admin = self.clients["admin"]
        normal = self.clients["normal"]
        before_quota = self.payload(self.expect(
            self.request(admin, "GET", "/api/admin/ai/quotas"), 200, "读取配额视图"), "配额视图")
        before_items = [item for item in (before_quota.get("items") or [])
                        if isinstance(item, dict)]
        explain_entry = next((item for item in before_items
                              if item.get("kind") == "explain"), {})
        # 收紧配额前先记录当日已消耗（复跑同一实例时可能已 >1；被拒请求必须不改变该值）。
        usage_before = self.payload(self.expect(
            self.request(normal, "GET", "/api/ai/usage/me", params={"days": 7}),
            200, "收紧配额前本人用量"), "本人用量")
        used_before = int(next(
            (item for item in ((usage_before.get("quota") or {}).get("kinds") or [])
             if isinstance(item, dict) and item.get("kind") == "explain"), {}
        ).get("used_today") or 0)
        normal_get_denied = self.request(normal, "GET", "/api/admin/ai/quotas")
        normal_post_denied = self.request(normal, "POST", "/api/admin/ai/quotas",
                                          json={"kind": "explain", "daily_limit": 1})
        unknown_kind = self.request(admin, "POST", "/api/admin/ai/quotas",
                                    json={"kind": "not-a-real-task", "daily_limit": 1})
        updated = self.payload(self.expect(
            self.request(admin, "POST", "/api/admin/ai/quotas",
                         json={"kind": "explain", "daily_limit": 1}),
            200, "收紧 explain 配额"), "配额更新")
        jobs_before = self.payload(self.expect(
            self.request(normal, "GET", "/api/ai/jobs"), 200, "读取任务列表"), "任务列表")
        jobs_before_count = len([item for item in (jobs_before.get("items") or [])
                                 if isinstance(item, dict)])
        rejected = self.request(normal, "POST", "/api/ai/jobs", json={
            "task": "explain",
            "qid": self.data["question"]["qid"],
            "provider_id": self.provider_id,
            "model": MODEL_ID,
        })
        rejected_body: dict = {}
        try:
            rejected_body = rejected.json() if rejected.status_code else {}
        except ValueError:
            rejected_body = {}
        detail = rejected_body.get("detail") if isinstance(rejected_body, dict) else None
        if isinstance(detail, dict):
            rejected_body = detail
        jobs_after = self.payload(self.expect(
            self.request(normal, "GET", "/api/ai/jobs"), 200, "读取任务列表（配额拒绝后）"),
            "任务列表")
        jobs_after_count = len([item for item in (jobs_after.get("items") or [])
                                if isinstance(item, dict)])
        me_after = self.payload(self.expect(
            self.request(normal, "GET", "/api/ai/usage/me", params={"days": 7}),
            200, "配额拒绝后本人用量"), "本人用量")
        explain_quota = next(
            (item for item in ((me_after.get("quota") or {}).get("kinds") or [])
             if isinstance(item, dict) and item.get("kind") == "explain"), {})
        reset = self.payload(self.expect(
            self.request(admin, "POST", "/api/admin/ai/quotas",
                         json={"kind": "explain", "reset": True}),
            200, "恢复默认配额"), "配额恢复")
        checks = [
            ("配额视图含 explain（默认值/来源/并发）",
             bool(explain_entry)
             and int(explain_entry.get("daily_limit") or 0) > 1
             and bool(explain_entry.get("daily_source")),
             json.dumps(explain_entry, ensure_ascii=False)[:280]),
            ("普通用户不能管理配额（GET/POST 403）",
             normal_get_denied.status_code == 403
             and normal_post_denied.status_code == 403,
             f"GET={normal_get_denied.status_code} POST={normal_post_denied.status_code}"),
            ("未知任务类型配额更新 422", unknown_kind.status_code == 422,
             f"HTTP {unknown_kind.status_code}"),
            ("配额收紧成功（daily_limit=1，来源=db）",
             int(updated.get("daily_limit") or 0) == 1
             and str(updated.get("daily_source") or "") == "db",
             json.dumps(updated, ensure_ascii=False)[:280]),
            ("超额创建任务返回 429（ai_quota_exceeded）",
             rejected.status_code == 429
             and str(rejected_body.get("code") or "") == "ai_quota_exceeded"
             and str(rejected_body.get("kind") or "") == "explain",
             f"HTTP {rejected.status_code} body={json.dumps(rejected_body, ensure_ascii=False)[:200]}"),
            ("被拒请求不落库（任务数不变）",
             jobs_after_count == jobs_before_count,
             f"before={jobs_before_count} after={jobs_after_count}"),
            ("被拒请求不消耗配额（今日已用量不变、额度仍为 1）",
             int(explain_quota.get("used_today") or 0) == used_before
             and int(explain_quota.get("daily_limit") or 0) == 1,
             json.dumps({"before": used_before, **explain_quota}, ensure_ascii=False)[:320]),
            ("reset=true 恢复内置默认配额",
             int(reset.get("daily_limit") or 0) > 1,
             json.dumps(reset, ensure_ascii=False)[:280]),
        ]
        self.data["quota"] = {
            "default_daily_limit": explain_entry.get("daily_limit"),
            "used_today_before_tighten": used_before,
            "tightened_daily_limit": updated.get("daily_limit"),
            "rejected_status": rejected.status_code,
            "rejected_code": rejected_body.get("code"),
            "reset_daily_limit": reset.get("daily_limit"),
        }
        self.step("配额收紧 → 超额 429（不落库/不消耗）→ 恢复默认", checks,
                  started=started)

    # -- 清理 -------------------------------------------------------------
    def cleanup(self) -> None:
        """删除本次创建的假模型连接并停止假模型服务（失败只记 warning，不改变判定）。"""
        if self.provider_id is not None and "normal" in self.clients:
            try:
                response = self.request(self.clients["normal"], "DELETE",
                                        f"/api/providers/{self.provider_id}")
                if response.status_code not in (200, 404):
                    self.warning(f"清理假模型连接返回 HTTP {response.status_code}（可忽略）")
            except Exception as exc:  # noqa: BLE001 - 清理失败不影响验收结论
                self.warning(f"清理假模型连接失败（可忽略）：{exc}")
        self.fake.stop()

    # -- 主流程 -----------------------------------------------------------
    def run(self) -> None:
        self.bootstrap()
        self.ensure_provider()
        self.prepare_question()
        self.job_flow()
        self.usage_flow()
        self.quota_flow()

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
            "fake_model": "脚本内嵌 OpenAI 兼容假模型服务（只监听 127.0.0.1 随机端口，固定 token usage）",
            "no_real_cost": "任务只调用本机假模型，不使用真实 Key、不访问外网、不产生模型费用",
            "required_env": [
                "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1",
                "STUDY_ALLOWED_HOSTS=127.0.0.1,localhost,[::1],testserver",
                "STUDY_INDEX_PREWARM=0",
                "STUDY_DATA_DIR=<临时目录>（本地模式，STUDY_AI_URL_POLICY 缺省 local_open）",
            ],
            "instance_start_example": (
                "pwsh: cd <repo>; $env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; "
                "$env:STUDY_ALLOWED_HOSTS='127.0.0.1,localhost,[::1],testserver'; "
                "$env:STUDY_INDEX_PREWARM='0'; "
                "$env:STUDY_DATA_DIR=Join-Path $env:TEMP ('zhixu-ai-usage-accept-'+(Get-Random)); "
                "python -m uvicorn app.main:app --host 127.0.0.1 --port 8849"
            ),
            "run_command": (
                "python scripts/integration/ai_usage_accept.py --base-url http://127.0.0.1:8849 "
                "[--admin-username <管理员> --admin-password <密码>]"
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
    data = report.get("data") or {}
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
            "run_id": data.get("run_id"),
            "provider_id": (data.get("provider") or {}).get("provider_id"),
            "job_id": (data.get("job") or {}).get("job_id"),
            "job_status": (data.get("job") or {}).get("status"),
            "quota": data.get("quota"),
        },
        "problems": report.get("problems") or [],
        "warnings": (report.get("warnings") or [])[:10],
        "elapsed_seconds": report.get("elapsed_seconds"),
        "report": report.get("report_path"),
    }


def main(argv: list[str] | None = None) -> int:
    _configure_console()
    parser = argparse.ArgumentParser(
        description="AI 用量可追踪与配额/并发端到端验收（本机假模型，不产生真实费用）")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL,
                        help=f"隔离实例地址（默认 {DEFAULT_BASE_URL}）")
    parser.add_argument("--out", default=str(DEFAULT_OUT),
                        help="输出目录（相对仓库根，默认 screenshots/integration）")
    parser.add_argument("--json", default=str(DEFAULT_REPORT),
                        help="报告 JSON 路径（相对仓库根，默认 "
                             "screenshots/integration/ai-usage-accept-report.json）")
    parser.add_argument("--admin-username",
                        help="已存在的管理员账号（与 plaza_accept 共用验收管理员命名，"
                             "便于 accept_runner 串联）")
    parser.add_argument("--admin-password", help="--admin-username 对应密码（默认验收密码）")
    parser.add_argument("--normal-username",
                        default=AUTHOR_NAME,
                        help=f"普通用户账号（默认「{AUTHOR_NAME}」，不存在时自动注册）")
    options = parser.parse_args(argv)

    admin_username = (options.admin_username or ADMIN_NAME).strip()
    admin_password = options.admin_password or PASSWORD
    out_dir = Path(options.out)
    if not out_dir.is_absolute():
        out_dir = ROOT / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = Path(options.json)
    if not report_path.is_absolute():
        report_path = ROOT / report_path
    report_path.parent.mkdir(parents=True, exist_ok=True)

    run_id = datetime.now().strftime("%Y%m%d%H%M%S") + secrets.token_hex(2)
    flow = AiUsageAccept(
        options.base_url, out_dir, report_path, run_id,
        admin_username=admin_username,
        admin_password=admin_password,
        admin_username_explicit=bool(options.admin_username),
        normal_username=(options.normal_username or AUTHOR_NAME).strip(),
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
        flow.cleanup()
        flow.close()
        flow.report["elapsed_seconds"] = round(time.time() - started, 2)
        flow.report["report_path"] = str(report_path)
        code = flow.finalize()

    print(json.dumps(_summarize(flow.report), ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main())
