"""平台精选（``official:<public_id>``）接入学习链路端到端验收（WS24 Phase 3）。

覆盖场景（全部经公开 HTTP API，不直接读写数据库）：

1. **发布**：复用 ``plaza_accept.py`` 的账号/角色/投稿评审发布链路（同一隔离实例、同一组验收账号），
   发布两条平台精选题：客观题（单选，可自动判分）与主观题（作答后待核验）；
2. **检索与详情**：匿名可读广场公开详情；``/api/questions?source=official`` 需登录（匿名 401），
   登录用户可按关键词/``source=official|all`` 检索并在 ``official:<public_id>`` 详情读到
   来源标签、审核状态（``featured``）、公开题号与发布版本；非法来源 422、未知精选 404；
3. **学习链路**：学员对客观题作答（正确 → ``correct``；错误 → ``incorrect`` 进错题）与主观题作答
   （``pending`` 待核验）→ ``GET /api/review/queue`` 可见（客观错题在已判分项、主观题在
   ``pending_items``）→ ``GET /api/stats`` 的 ``official_*`` 计数；
4. **工坊建卷**：``POST /api/paperlab/papers`` 用 official 引用建卷，快照含 ``source=official`` /
   ``public_id`` / 来源声明；整卷练习（作答 → 提交判分 → 回看）确认客观错题入统一错题本、
   主观题 pending，回看仍标注平台精选来源；
5. **撤回即时生效**：编辑撤回两条精选 → 公开列表/检索/详情/作答/工坊建卷立即 404 或不可见；
   ``/api/review/queue`` 保留历史并显示「已撤回」占位提示（不回显受限正文）；
6. 产出 JSON 报告（``screenshots/integration/official-accept-report.json``）与退出码 0/1。

前置（与 plaza_accept 相同）：隔离实例 + 全新 ``STUDY_DATA_DIR``；``httpx`` 一律
``trust_env=False``（Windows 系统代理会劫持 127.0.0.1）；全部操作经 HTTP，不写业务代码。
"""
from __future__ import annotations

import argparse
import json
import re
import secrets
import sys
import time
import traceback
from datetime import datetime, timezone
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

SCENARIO = [
    "复用广场验收账号发布两条平台精选题（客观题 / 主观题）",
    "匿名读广场公开详情；登录用户 source=official|all 检索与 official:<public_id> 详情",
    "学员作答 official 客观题（correct/incorrect）与主观题（pending）",
    "错题/复习队列 /api/review/queue 与 /api/stats official_* 计数",
    "工坊建卷（snapshot source=official）→ 整卷练习提交 → 回看",
    "编辑撤回精选 → 检索/详情/作答/建卷立即失效，复习队列保留已撤回提示",
]


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
        self.data: dict = {
            "run_id": run_id,
            "marker": self.marker,
            "accounts": {},
            "objective": {},
            "subjective": {},
            "paper": {},
            "attempts": {},
            "review_queue": {},
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
        learner_client, learner_user, mode = helper._ensure_account(LEARNER_NAME)
        self.clients["learner"] = learner_client
        self.clients["anon"] = helper.clients["anon"]
        for key in ("author", "editor", "reviewer1", "reviewer2"):
            self.clients[key] = helper.clients[key]
        self.data["accounts"] = {
            "learner": {"username": LEARNER_NAME, "user_id": learner_user.get("id"), "mode": mode},
            "reused": list(helper.data.get("accounts") or {}),
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
            "精选客观题（official 客观判分）", content=objective_content, record_step=False)
        subjective_content = {
            "stem": (f"【{self.marker}】精选主观题：简述一次函数 y=x+1 的单调性，并说明理由。"),
            "answers": ["该函数在实数集上单调递增。"],
            "explanation": "任取 x1<x2，则 f(x1)-f(x2)=x1-x2<0，故函数单调递增。",
        }
        subjective = self.helper.publish_new_question(
            "精选主观题（official 待核验）", qtype="SUBJECTIVE",
            content=subjective_content, record_step=False)
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

    # -- 主流程 / 收尾 ----------------------------------------------------
    def run(self) -> None:
        self.bootstrap()
        self.publish_official_questions()
        self.assert_discoverable()
        self.answer_flow()
        self.paper_flow()
        self.withdraw_flow()

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
            "required_env": [
                "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1",
                "STUDY_ALLOWED_HOSTS=127.0.0.1,localhost,[::1],testserver",
                "STUDY_INDEX_PREWARM=0",
                "STUDY_DATA_DIR=<临时目录>（全新实例；已有 plaza_accept 账号时自动复用登录）",
            ],
            "instance_start_example": (
                "pwsh: cd <repo>; $env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; "
                "$env:STUDY_ALLOWED_HOSTS='127.0.0.1,localhost,[::1],testserver'; "
                "$env:STUDY_INDEX_PREWARM='0'; "
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
        flow.close()
        flow.report["elapsed_seconds"] = round(time.time() - started, 2)
        flow.report["report_path"] = str(report_path)
        code = flow.finalize()

    print(json.dumps(_summarize(flow.report), ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main())
