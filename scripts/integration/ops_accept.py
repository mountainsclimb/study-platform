"""WS30 第四轮验收：运维监控 / 抽检门禁 / 一键恢复 / 批量处理 / 通知增强（端到端 HTTP）。

覆盖场景（全部经公开 HTTP API，不直接读写数据库；复用 ``plaza_accept.py`` 的账号/角色/
发布链路，同一隔离实例）：

1. **运维监控**（``GET /api/admin/monitor``）：管理员可读、普通用户 403、匿名 401；
   响应结构（``ok/status/checks{tasks,ai,media,data,plaza}/build/thresholds/cache``）、
   短缓存语义（``refresh=1`` 绕过缓存）；构造单组降级（积压 20 条待处理纠错 →
   ``checks.plaza.status=warn`` + 告警文案），确认整体不 500、其他组结构完整，
   清理积压后恢复 ``ok``。
2. **批量处理**（``POST /api/plaza/admin/{issues|reports|appeals}/batch``）：≥2 条纠错/
   举报/申诉 → 批量 resolve/reject → 部分成功语义（``updated``/``skipped``、
   ``already_handled``/``not_found``）、重复批量不重复通知；指标 ``ops.batch.*`` 可查。
3. **抽检门禁**（WS28）：管理员显式 ``PUT .../quality/gate {mode:initial_100, armed:true}``
   → 未抽检的 verified 投稿签发 409（``detail.code=quality_gate_blocked`` + guidance +
   审计 blocked）→ ``POST .../quality/checks``（``phase=pre_publish``，
   ``gate.publish_ready=true``）→ 再签发 200（``satisfied/pass_check`` 回显）；
   显式 override（``quality_gate_override+override_reason``）审计事件/指标可查；普通用户
   改门禁 403。
4. **一键恢复**（``POST /api/plaza/releases/{public_id}/restore``）：
   抽检 ``issue+high`` 自动撤回 → 收藏者/作者通知 → ``confirm_resolved=true`` 恢复公开
   （``restore_source=quality_auto_withdraw``、事件 ``release_restored``、收藏者
   ``favorite_restored``），重复恢复幂等不重复通知；编辑撤回后可恢复
   （``restore_source=editor_withdraw``）；未确认问题已处理 422；同投稿已有更新版本活跃
   时恢复旧发布 409。
5. **通知增强**：``GET /api/plaza/notifications/unread-count`` 与列表同口径；按 kind 全读、
   按 ids 去重读取、未知 kind 422、``all=true`` 全读；同一事件重复触发在去重窗口内只
   产生一条通知。
6. **质量指标补充**：``GET /api/plaza/admin/metrics`` 的 gate/restorations/ops.batch/
   notifications/caliber 字段与前面操作一致。
7. 产出 JSON 报告（``screenshots/integration/ops-accept-report.json``）与退出码 0/1。

门禁说明（WS28 语义）：抽检门禁模式 off|initial_100|always；``armed`` 为显式配置
（env ``STUDY_PLAZA_QUALITY_GATE`` 或管理员 PUT）时激活，未显式配置且库中无抽检记录时不
阻断。本脚本会在开头读取门禁状态（实例显式 armed 时先临时关闭以完成基线发布），随后
显式设置为 ``initial_100+armed=true`` 验证拦截/抽检/override，**结束时恢复为 off**
（脚本负责的显式 armed 场景，不把实例留在强制抽检状态）。

环境约束：``httpx.Client(trust_env=False)``（Windows 系统代理会劫持 127.0.0.1）；
全部操作经 HTTP，不写业务代码；可在 Lead 启动的隔离实例上重复运行。
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
if str(SCRIPT_DIR) not in sys.path:  # 复用 plaza_accept 的账号/发布链路
    sys.path.insert(0, str(SCRIPT_DIR))

from plaza_accept import (  # noqa: E402 - 先修正 sys.path 再导入同目录脚本
    ADMIN_NAME,
    AUTHOR_NAME,
    GRADE,
    PASSWORD,
    REVIEWER_NAMES,
    ROOT,
    SOURCE,
    SUBJECT,
    FlowFailure,
    PlazaAccept,
)

DEFAULT_OUT = ROOT / "screenshots" / "integration"
DEFAULT_REPORT = DEFAULT_OUT / "ops-accept-report.json"
DEFAULT_BASE_URL = "http://127.0.0.1:8856"

#: 独立学员账号：通知中心/收藏/反馈操作使用（普通用户权限）。
LEARNER_NAME = "运维验收学员"

SCENARIO = [
    "准备管理员/编辑/评审/学员账号（复用 plaza_accept 链路）并按需临时关闭抽检门禁",
    "发布 3 条基线精选题（恢复/门禁/冲突场景）且匿名可见",
    "管理员监控结构/短缓存/权限负向；构造反馈积压使 checks.plaza 单组降级（warn）",
    "批量处理：纠错部分成功（updated/skipped/重复批量）→ 清空积压后监控恢复 ok",
    "批量处理：≥2 条举报 reject、≥1 条申诉 resolve（通知与指标）",
    "抽检门禁：显式 initial_100+armed → 签发 409（blocked 审计）→ 签发前抽检 pass → 签发 200",
    "抽检门禁：override 无理由 422、带理由 200（事件/审计/指标）；普通用户改门禁 403",
    "一键恢复：抽检 high 自动撤回 → 收藏者/作者通知 → confirm_resolved 恢复公开（幂等）",
    "一键恢复：编辑撤回后恢复（editor_withdraw）、未确认 422、未知发布 404、活跃幂等",
    "一键恢复：改版 v2 重新签发后恢复旧发布 409（避免内容回退）",
    "通知增强：unread-count 同口径、kind 全读、ids 去重、未知 kind 422、all 全读",
    "通知去重：同事件两次只一条；质量指标 gate/restorations/ops.batch/notifications 可查",
    "收尾：抽检门禁恢复 off（不把实例留在强制抽检状态）",
]


class OpsAccept:
    def __init__(self, base_url: str, out_dir: Path, report_path: Path, run_id: str, *,
                 admin_username: str = ADMIN_NAME, admin_password: str = PASSWORD,
                 admin_username_explicit: bool = False, admin_token: str = ""):
        self.base = base_url.rstrip("/")
        self.out = out_dir
        self.report_path = report_path
        self.run_id = run_id
        self.marker = f"运维验收{run_id}"
        self.clients: dict[str, httpx.Client] = {}
        #: 复用 plaza_accept 的账号/角色/发布链路（报告只保留本脚本步骤）。
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
            "baseline": {},
            "monitor": {},
            "batch": {"issues": {}, "reports": {}, "appeals": {}},
            "gate": {},
            "restore": {},
            "notifications": {},
            "metrics": {},
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
        self.helper.warning(message)

    def close(self) -> None:
        self.helper.close()

    @staticmethod
    def _snippet(response: httpx.Response, limit: int = 500) -> str:
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

    def request(self, client, method: str, path: str, **kwargs) -> httpx.Response:
        return self.helper.request(client, method, path, **kwargs)

    def expect(self, response: httpx.Response, statuses, label: str) -> httpx.Response:
        return self.helper.expect(response, statuses, label)

    def payload(self, response: httpx.Response, label: str):
        return self.helper.payload(response, label)

    def step(self, name: str, checks: list, *, started: float | None = None,
             data: dict | None = None) -> None:
        started = started if started is not None else self._started
        elapsed = round((time.time() - started) * 1000, 1) if started else 0.0
        normalized, failures = [], []
        for item in checks:
            label, ok = str(item[0]), bool(item[1])
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

    # -- 通用查询封装 -----------------------------------------------------
    def monitor(self, *, refresh: bool = True) -> dict:
        path = "/api/admin/monitor" + ("?refresh=1" if refresh else "")
        return self.payload(self.expect(
            self.request(self.clients["admin"], "GET", path), 200, "读取运维监控"), "运维监控")

    def gate_state(self) -> dict:
        return self.payload(self.expect(
            self.request(self.clients["editor"], "GET", "/api/plaza/admin/quality/gate"),
            200, "读取抽检门禁状态"), "抽检门禁状态")

    def set_gate(self, data: dict, label: str = "更新抽检门禁") -> dict:
        return self.payload(self.expect(
            self.request(self.clients["admin"], "PUT", "/api/plaza/admin/quality/gate",
                         data=data), 200, label), label)

    def metrics(self) -> dict:
        return self.payload(self.expect(
            self.request(self.clients["editor"], "GET", "/api/plaza/admin/metrics"),
            200, "读取质量指标"), "质量指标")

    def notifications(self, client, *, kind: str | None = None,
                      unread_only: bool = False, page_size: int = 100) -> dict:
        params: dict = {"page": 1, "page_size": page_size}
        if kind:
            params["kind"] = kind
        if unread_only:
            params["unread_only"] = True
        return self.payload(self.expect(
            self.request(client, "GET", "/api/plaza/notifications", params=params),
            200, "读取通知中心"), "通知中心")

    def unread_summary(self, client) -> dict:
        return self.payload(self.expect(
            self.request(client, "GET", "/api/plaza/notifications/unread-count"),
            200, "读取未读通知汇总"), "未读通知汇总")

    @staticmethod
    def find_notifications(payload: dict, *, kind: str | None = None,
                           submission_id: int | None = None,
                           public_id: str | None = None) -> list[dict]:
        items = payload.get("items") if isinstance(payload, dict) else []
        found = []
        for item in items or []:
            if not isinstance(item, dict):
                continue
            if kind and str(item.get("kind") or "") != kind:
                continue
            if submission_id is not None and int(item.get("submission_id") or 0) != int(submission_id):
                continue
            if public_id and str(item.get("public_id") or "") != public_id:
                continue
            found.append(item)
        return found

    def events(self, submission_id: int) -> dict:
        response = self.request(self.clients["author"], "GET",
                                f"/api/plaza/submissions/{submission_id}/events")
        if response.status_code == 404:  # 兼容旧路径（仅编辑/作者可读）
            return {"items": []}
        return self.payload(self.expect(response, 200, "投稿事件链"), "投稿事件链")

    def submit_issue(self, client, public_id: str, description: str,
                     field: str = "answer") -> int:
        body = self.payload(self.expect(
            self.request(client, "POST", f"/api/plaza/questions/{public_id}/issues",
                         data={"field": field, "description": description}),
            201, f"提交纠错（{description[:12]}）"), "提交纠错")
        return int((body.get("issue") or {}).get("id") or 0)

    def submit_report(self, client, public_id: str, reason: str = "copyright",
                      detail: str = "验收：疑似未授权转载") -> int:
        body = self.payload(self.expect(
            self.request(client, "POST", f"/api/plaza/questions/{public_id}/reports",
                         data={"reason": reason, "detail": detail}),
            201, "提交举报"), "提交举报")
        return int((body.get("report") or {}).get("id") or 0)

    def quality_check(self, public_id: str | None = None, *,
                      submission_id: int | None = None,
                      decision: str = "pass", severity: str | None = None,
                      findings: list[str] | None = None,
                      note: str = "", expected: int = 201):
        data: dict = {"decision": decision, "note": note}
        if public_id:
            data["public_id"] = public_id
        if submission_id is not None:
            data["submission_id"] = int(submission_id)
        if severity:
            data["severity"] = severity
        if findings:
            data["findings"] = list(findings)
        response = self.request(self.clients["editor"], "POST",
                                "/api/plaza/admin/quality/checks", data=data)
        self.expect(response, expected, f"抽检（decision={decision}）")
        return self.payload(response, "抽检响应")

    # -- 1. 准备 / 基线发布 -------------------------------------------------
    def bootstrap(self) -> None:
        started = time.time()
        helper = self.helper
        helper.clients["anon"] = helper.new_client()
        helper.prepare_accounts()
        helper.grant_roles()
        learner_client, learner_user, learner_mode = helper._ensure_account(LEARNER_NAME)
        self.clients = {
            "admin": helper.clients["admin"],
            "author": helper.clients["author"],
            "editor": helper.clients["editor"],
            "reviewer1": helper.clients["reviewer1"],
            "reviewer2": helper.clients["reviewer2"],
            "learner": learner_client,
            "anon": helper.clients["anon"],
        }
        # 门禁兼容：实例显式 armed 时先临时关闭（否则基线发布会被 409 拦截）。
        helper.ensure_gate_compatible()
        origin = helper.gate_origin or {}
        self.data["accounts"] = {
            "learner": {"username": LEARNER_NAME, "user_id": learner_user.get("id"),
                        "mode": learner_mode},
            "reused": sorted((helper.data.get("accounts") or {}).keys()),
        }
        self.data["gate"]["origin"] = {
            key: origin.get(key) for key in (
                "mode", "mode_source", "armed", "armed_source", "enforced", "required",
                "pilot_limit", "published_total", "checks_total")
        }
        checks = [
            ("管理员 + 编辑 + 评审 + 学员账号就绪",
             bool(learner_user.get("id"))
             and all((helper.data.get("accounts") or {}).get(key, {}).get("user_id")
                     for key in ("admin", "author", "editor", "reviewer1", "reviewer2")),
             json.dumps({"learner": learner_user.get("id")}, ensure_ascii=False)),
            ("复用 plaza_accept 账号/角色准备步骤全部通过",
             all(step.get("ok") for step in (helper.report.get("steps") or [])),
             f"helper_steps={len(helper.report.get('steps') or [])}"),
            ("抽检门禁原状态已记录（显式 armed 时已临时关闭）",
             bool(origin.get("mode")),
             json.dumps(self.data["gate"]["origin"], ensure_ascii=False)[:240]),
        ]
        self.step("准备验收账号与抽检门禁兼容（复用 plaza_accept 链路）", checks,
                  started=started, data=self.data["accounts"])

    def publish_baseline(self) -> None:
        started = time.time()
        helper = self.helper
        p1 = helper.publish_new_question(
            "WS30 恢复-抽检自动撤回", record_step=False,
            content={
                "stem": f"【{self.marker}】恢复场景基线：已知函数 f(x)=x+1，求 f(2) 的值。",
                "options": [{"label": "A", "text": "3"}, {"label": "B", "text": "2"},
                            {"label": "C", "text": "4"}, {"label": "D", "text": "1"}],
                "answers": ["A"],
                "explanation": "代入 x=2 得 f(2)=2+1=3，因此选 A。（恢复场景基线）",
            })
        p2 = helper.publish_new_question(
            "WS30 恢复-编辑撤回", record_step=False,
            content={
                "stem": f"【{self.marker}】编辑撤回场景基线：已知函数 f(x)=x+2，求 f(2) 的值。",
                "options": [{"label": "A", "text": "4"}, {"label": "B", "text": "2"},
                            {"label": "C", "text": "3"}, {"label": "D", "text": "1"}],
                "answers": ["A"],
                "explanation": "代入 x=2 得 f(2)=2+2=4，因此选 A。（编辑撤回场景基线）",
            })
        p3 = helper.publish_new_question(
            "WS30 恢复-新旧版本冲突", record_step=False,
            content={
                "stem": f"【{self.marker}】版本冲突场景初始版：已知函数 f(x)=x+1，求 f(2) 的值。",
                "options": [{"label": "A", "text": "3"}, {"label": "B", "text": "2"},
                            {"label": "C", "text": "4"}, {"label": "D", "text": "1"}],
                "answers": ["A"],
                "explanation": "代入 x=2 得 f(2)=2+1=3，因此选 A。（版本冲突初始版）",
            })
        self.data["baseline"] = {"p1": p1, "p2": p2, "p3": p3}
        anonymous = self.clients["anon"]
        listing = self.payload(self.expect(
            anonymous.request("GET", "/api/plaza/questions",
                              params={"keyword": self.marker}, timeout=30.0),
            200, "基线公开列表"), "基线公开列表")
        listed = {str(item.get("public_id") or "") for item in (listing.get("items") or [])
                  if isinstance(item, dict)}
        checks = [
            (f"基线题 {name} 发布为 active 且匿名可见",
             str(item.get("public_id") or "") in listed
             and item.get("transitions", [""])[-1] == "verified",
             f"public_id={item.get('public_id')} listed={sorted(listed)[:6]}")
            for name, item in (("P1", p1), ("P2", p2), ("P3", p3))
        ]
        checks.append(("三条基线题公开题号互不相同",
                       len({p1.get("public_id"), p2.get("public_id"), p3.get("public_id")}) == 3,
                       f"{p1.get('public_id')}/{p2.get('public_id')}/{p3.get('public_id')}"))
        self.step("发布 3 条基线精选题（恢复 / 编辑撤回 / 版本冲突场景）", checks,
                  started=started,
                  data={key: value.get("public_id") for key, value in
                        (("p1", p1), ("p2", p2), ("p3", p3))})

    # -- 2. 运维监控结构 / 权限 / 缓存 -------------------------------------
    def monitor_structure(self) -> None:
        started = time.time()
        admin = self.clients["admin"]
        anonymous = self.clients["anon"]
        learner = self.clients["learner"]
        first = self.monitor(refresh=True)
        second = self.monitor(refresh=False)
        normal_denied = self.request(learner, "GET", "/api/admin/monitor?refresh=1")
        anon_denied = self.request(anonymous, "GET", "/api/admin/monitor?refresh=1")
        checks_dict = first.get("checks") or {}
        expected_groups = ["ai", "data", "media", "plaza", "tasks"]
        ttl = float((first.get("cache") or {}).get("ttl_seconds") or 0)
        cache_ok = (second.get("cache", {}).get("cached") is True
                    if ttl > 0 else second.get("cache", {}).get("cached") is False)
        self.data["monitor"]["structure"] = {
            "status": first.get("status"),
            "groups": sorted(checks_dict),
            "degraded": first.get("degraded"),
            "build": first.get("build"),
        }
        checks = [
            ("监控结构：五组检查齐全（tasks/ai/media/data/plaza）",
             sorted(checks_dict) == expected_groups,
             f"实际={sorted(checks_dict)}"),
            ("每组 check 含 status/detail/alerts 且 status 合法",
             all(isinstance(item.get("status"), str)
                 and item.get("status") in ("ok", "warn", "error")
                 and isinstance(item.get("detail"), str) and item.get("detail")
                 and isinstance(item.get("alerts"), list)
                 for item in checks_dict.values()),
             json.dumps({name: item.get("status") for name, item in checks_dict.items()},
                        ensure_ascii=False)),
            ("整体状态与 ok/degraded 口径一致",
             first.get("status") in ("ok", "warn", "error")
             and bool(first.get("ok")) == (first.get("status") != "error")
             and set(first.get("degraded") or [])
             == {name for name, item in checks_dict.items() if item.get("status") != "ok"},
             json.dumps({"status": first.get("status"), "ok": first.get("ok"),
                         "degraded": first.get("degraded")}, ensure_ascii=False)),
            ("构建元信息/阈值/窗口/duration 存在",
             bool((first.get("build") or {}).get("build_id"))
             and bool((first.get("build") or {}).get("version"))
             and isinstance((first.get("thresholds") or {}).get("job_timeout_seconds"), int)
             and int(first.get("window_days") or 0) >= 1
             and isinstance(first.get("duration_ms"), (int, float)),
             json.dumps({"build": first.get("build"),
                         "window_days": first.get("window_days")}, ensure_ascii=False)[:300]),
            ("短缓存语义（ttl>0 时第二次 cached=true；refresh=1 绕过缓存）",
             cache_ok,
             json.dumps({"ttl": ttl, "second_cache": second.get("cache")},
                        ensure_ascii=False)),
            ("普通用户访问监控 403、匿名 401",
             normal_denied.status_code == 403 and anon_denied.status_code == 401,
             f"normal=HTTP {normal_denied.status_code} anon=HTTP {anon_denied.status_code}"),
            ("监控响应不包含敏感 Key/本地数据目录路径",
             "sk-" not in json.dumps(first, ensure_ascii=False)
             and "api_key" not in json.dumps(first, ensure_ascii=False).lower(),
             "监控响应可能泄露敏感信息"),
        ]
        self.step("运维监控看板结构 / 短缓存 / 权限负向（管理员可读，普通用户 403）",
                  checks, started=started, data=self.data["monitor"]["structure"])

    # -- 3. 单组降级：构造 20 条待处理纠错 --------------------------------
    def monitor_degrade(self) -> None:
        started = time.time()
        p1_pid = self.data["baseline"]["p1"]["public_id"]
        learner = self.clients["learner"]
        reviewer = self.clients["reviewer1"]
        issue_ids: list[int] = []
        for index in range(20):
            reporter = learner if index % 2 == 0 else reviewer
            issue_ids.append(self.submit_issue(
                reporter, p1_pid, f"WS30 反馈积压构造 #{index + 1}：解析步骤不清晰"))
        self.data["monitor"]["degrade_issue_ids"] = issue_ids
        monitor = self.monitor(refresh=True)
        plaza = (monitor.get("checks") or {}).get("plaza") or {}
        degraded = list(monitor.get("degraded") or [])
        alerts = [str(item) for item in (plaza.get("alerts") or [])]
        feedback = plaza.get("feedback") or {}
        withdrawn = plaza.get("withdrawn") or {}
        leak = int(withdrawn.get("leaked") or 0)
        # 历史上同 public_id 重新签发会误报泄露（WS27），已在 058be1a 修复
        # （监控排除存在 active 指针的同 public_id）；现在 leaked>0 即真实泄露，按失败断言。
        checks_insert = (
            ("撤回传播抽样零泄露（同 public_id 重新签发已排除误报）", leak == 0,
             json.dumps({"leaked": leak, "items": (withdrawn.get("leaked_items") or [])[:3]},
                        ensure_ascii=False)),
        )
        status_order = {"ok": 0, "warn": 1, "error": 2}
        expected_status = "ok"
        for item in (monitor.get("checks") or {}).values():
            candidate = str(item.get("status") or "ok")
            if status_order.get(candidate, 0) > status_order.get(expected_status, 0):
                expected_status = candidate
        checks = [
            ("20 条待处理纠错已提交（反馈积压可构造）",
             len(issue_ids) == 20 and all(issue_id > 0 for issue_id in issue_ids),
             f"issues={len(issue_ids)} 首批={issue_ids[:3]}"),
            checks_insert[0],
            ("单组降级：checks.plaza.status=warn/error 且给出积压告警",
             plaza.get("status") in ("warn", "error")
             and int(feedback.get("total_pending") or 0) >= 20
             and any("反馈" in alert for alert in alerts),
             json.dumps({"status": plaza.get("status"), "alerts": alerts[:3],
                         "feedback": feedback}, ensure_ascii=False)[:320]),
            ("单组降级不外溢：整体 status 取各组最差值、ok=status!=error（不 500）",
             monitor.get("status") == expected_status
             and bool(monitor.get("ok")) == (expected_status != "error")
             and "plaza" in degraded,
             json.dumps({"status": monitor.get("status"),
                         "ok": monitor.get("ok"),
                         "expected": expected_status,
                         "degraded": degraded}, ensure_ascii=False)),
            ("其他检查分组结构完整（tasks/ai/media/data 均仍可读）",
             all(sorted((monitor.get("checks") or {})) ==
                 ["ai", "data", "media", "plaza", "tasks"]
                 and (monitor.get("checks") or {}).get(name, {}).get("status")
                 in ("ok", "warn", "error")
                 for name in ("tasks", "ai", "media", "data")),
             json.dumps({name: (monitor.get("checks") or {}).get(name, {}).get("status")
                         for name in ("tasks", "ai", "media", "data")},
                        ensure_ascii=False)),
        ]
        self.step("构造反馈积压 → 监控单组降级（plaza=warn/error）且整体不 500", checks,
                  started=started, data={"total_pending": feedback.get("total_pending"),
                                         "withdrawn_leaked": leak})

    # -- 4. 批量处理：纠错部分成功 + 清空积压 -----------------------------
    def batch_issues(self) -> None:
        started = time.time()
        editor = self.clients["editor"]
        p1 = self.data["baseline"]["p1"]
        issue_ids = list(self.data["monitor"].get("degrade_issue_ids") or [])
        # 单条处理一条，制造「已处理」跳过项。
        handled = self.payload(self.expect(
            self.request(editor, "PATCH", f"/api/plaza/admin/issues/{issue_ids[0]}",
                         data={"action": "resolve", "reason": "验收：单条先行处理（部分成功样本）"}),
            200, "单条处理纠错"), "单条处理纠错")
        batch = self.payload(self.expect(
            self.request(editor, "POST", "/api/plaza/admin/issues/batch", data={
                "ids": [*issue_ids, 999999],
                "action": "resolve",
                "reason": "验收：批量核对后统一采纳",
            }), 200, "批量处理纠错"), "批量处理纠错")
        skipped = {int(item.get("id") or 0): item for item in (batch.get("skipped") or [])
                   if isinstance(item, dict)}
        repeat = self.payload(self.expect(
            self.request(editor, "POST", "/api/plaza/admin/issues/batch", data={
                "ids": issue_ids, "action": "resolve", "reason": "验收：重复批量（应全部跳过）",
            }), 200, "重复批量处理纠错"), "重复批量处理纠错")
        # 清理历史运行遗留的 pending 积压（失败重跑时可能残留），保证监控降级可恢复。
        pending_queue = self.payload(self.expect(
            self.request(editor, "GET", "/api/plaza/admin/issues",
                         params={"status": "pending", "page_size": 100}),
            200, "读取待处理纠错队列"), "待处理纠错队列")
        pending_ids = [int(item.get("id") or 0) for item in (pending_queue.get("items") or [])
                       if isinstance(item, dict) and int(item.get("id") or 0) > 0]
        cleanup_updated = 0
        if pending_ids:
            cleaned = self.payload(self.expect(
                self.request(editor, "POST", "/api/plaza/admin/issues/batch", data={
                    "ids": pending_ids, "action": "resolve",
                    "reason": "验收：清理历史运行遗留的待处理积压",
                }), 200, "清理遗留积压纠错"), "清理遗留积压纠错")
            cleanup_updated = int(cleaned.get("updated_count") or 0)
        events = self.events(int(p1["submission_id"]))
        actions = [str(item.get("action") or "") for item in (events.get("items") or [])]
        self.data["batch"]["issues"] = {
            "requested": batch.get("requested"),
            "updated_count": batch.get("updated_count"),
            "skipped_count": batch.get("skipped_count"),
            "batch_id": batch.get("batch_id"),
            "repeat_updated": repeat.get("updated_count"),
            "cleanup_updated": cleanup_updated,
            "event_resolved": actions.count("issue_resolved"),
        }
        # 清空积压后监控不再有「反馈积压」告警（验证降级可恢复；撤回抽样误报属既有缺陷）。
        monitor = self.monitor(refresh=True)
        plaza = (monitor.get("checks") or {}).get("plaza") or {}
        feedback = plaza.get("feedback") or {}
        plaza_alerts = [str(item) for item in (plaza.get("alerts") or [])]
        backlog_cleared = (int(feedback.get("total_pending") or 0) == 0
                           and not any("反馈积压" in alert or "未处理反馈" in alert
                                       for alert in plaza_alerts))
        checks = [
            ("单条处理成功（resolve）",
             (handled.get("issue") or {}).get("status") == "resolved",
             json.dumps(handled.get("issue"), ensure_ascii=False)[:200]),
            ("批量部分成功语义：requested=21 / updated=19 / skipped=2",
             int(batch.get("requested") or 0) == 21
             and int(batch.get("updated_count") or 0) == 19
             and int(batch.get("skipped_count") or 0) == 2,
             json.dumps({key: batch.get(key) for key in (
                 "requested", "updated_count", "skipped_count")}, ensure_ascii=False)),
            ("跳过原因区分 already_handled（已处理）与 not_found（不存在）",
             skipped.get(issue_ids[0], {}).get("reason") == "already_handled"
             and skipped.get(999999, {}).get("reason") == "not_found",
             json.dumps([{"id": key, "reason": value.get("reason")}
                         for key, value in skipped.items()], ensure_ascii=False)[:260]),
            ("更新 id 列表与剩余 19 条一致（逐条部分成功）",
             sorted(int(item) for item in (batch.get("updated_ids") or []))
             == sorted(issue_ids[1:]),
             f"updated={(batch.get('updated_ids') or [])[:5]}..."),
            ("重复批量不重复处理（updated=0，全部 skipped）",
             int(repeat.get("updated_count") or 0) == 0
             and int(repeat.get("skipped_count") or 0) == len(issue_ids),
             json.dumps({key: repeat.get(key) for key in ("updated_count",
                                                          "skipped_count")}, ensure_ascii=False)),
            ("每条处理都写入事件链（issue_resolved 共 20 条：单条 1 + 批量 19）",
             actions.count("issue_resolved") == 20,
             f"issue_resolved={actions.count('issue_resolved')}"),
            ("积压清空后监控不再有反馈积压告警（降级可恢复）",
             backlog_cleared,
             json.dumps({"status": plaza.get("status"),
                         "alerts": plaza_alerts[:3],
                         "feedback": feedback}, ensure_ascii=False)[:280]),
        ]
        self.step("批量处理纠错（部分成功 / 跳过原因 / 重复批量 / 监控恢复）", checks,
                  started=started, data=self.data["batch"]["issues"])

    def batch_reports(self) -> None:
        started = time.time()
        editor = self.clients["editor"]
        learner = self.clients["learner"]
        reviewer = self.clients["reviewer1"]
        p1_pid = self.data["baseline"]["p1"]["public_id"]
        report_ids = [
            self.submit_report(learner, p1_pid, detail="验收：批量举报样本 1"),
            self.submit_report(reviewer, p1_pid, detail="验收：批量举报样本 2"),
        ]
        rejected = self.payload(self.expect(
            self.request(editor, "POST", "/api/plaza/admin/reports/batch", data={
                "ids": report_ids, "action": "reject",
                "reason": "验收：来源与授权材料齐全，举报不成立",
            }), 200, "批量驳回举报"), "批量驳回举报")
        learner_notes = self.notifications(learner, kind="report_rejected")
        reviewer_notes = self.notifications(reviewer, kind="report_rejected")
        metrics = self.metrics()
        ops_batch = (metrics.get("ops") or {}).get("batch") or {}
        by_kind = {str(item.get("kind") or ""): item for item in (ops_batch.get("by_kind") or [])
                   if isinstance(item, dict)}
        self.data["batch"]["reports"] = {
            "ids": report_ids,
            "updated_count": rejected.get("updated_count"),
            "target_status": rejected.get("target_status"),
        }
        checks = [
            ("2 条举报提交成功", len(report_ids) == 2 and all(item > 0 for item in report_ids),
             f"report_ids={report_ids}"),
            ("批量驳回 2 条（updated=2 / target_status=rejected）",
             int(rejected.get("updated_count") or 0) == 2
             and rejected.get("target_status") == "rejected"
             and all((item.get("status") == "rejected")
                     for item in (rejected.get("updated") or []) if isinstance(item, dict)),
             json.dumps({key: rejected.get(key) for key in (
                 "updated_count", "target_status", "skipped_count")}, ensure_ascii=False)),
            ("举报人收到 report_rejected 通知",
             len(learner_notes.get("items") or []) >= 1
             and len(reviewer_notes.get("items") or []) >= 1,
             json.dumps({"learner": learner_notes.get("total"),
                         "reviewer": reviewer_notes.get("total")}, ensure_ascii=False)),
            ("指标 ops.batch.by_kind.report 记录本次批量（updated≥2，实例累计口径）",
             int((by_kind.get("report") or {}).get("updated", 0)) >= 2,
             json.dumps(by_kind.get("report") or {}, ensure_ascii=False)[:240]),
        ]
        self.step("批量处理举报（≥2 条 reject + 通知 + 指标）", checks, started=started,
                  data=self.data["batch"]["reports"])

    def batch_appeals(self) -> None:
        started = time.time()
        author = self.clients["author"]
        editor = self.clients["editor"]
        marker = f"{self.marker}-申诉"
        created = self.payload(self.expect(
            self.request(author, "POST", "/api/paperlab/questions", data={
                "type": "SINGLE", "subject": SUBJECT, "grade": GRADE, "difficulty": 0.5,
                "knowledge_tags": [self.marker, "申诉"], "content": {
                    "stem": f"【{marker}】申诉场景：已知函数 f(x)=x+1，求 f(2) 的值。",
                    "options": [{"label": "A", "text": "3"}, {"label": "B", "text": "2"}],
                    "answers": ["A"], "explanation": "代入 x=2 得 3。",
                }}), 200, "申诉场景创建个人题"), "创建个人题")
        question_id = int((created.get("question") or {}).get("id") or 0)
        created_sub = self.payload(self.expect(
            self.request(author, "POST", "/api/plaza/submissions", data={
                "question_id": question_id, "source": {}, "license_confirmed": False,
            }), 201, "申诉场景创建投稿"), "创建投稿")
        submission_id = int((created_sub.get("submission") or {}).get("id") or 0)
        patched = self.payload(self.expect(
            self.request(author, "PATCH", f"/api/plaza/submissions/{submission_id}", data={
                "version": 1, "source": SOURCE, "license_confirmed": True,
            }), 200, "申诉场景声明来源"), "声明来源")
        version = int((patched.get("submission") or {}).get("version") or 1)
        self.expect(self.request(author, "POST",
                                 f"/api/plaza/submissions/{submission_id}/submit",
                                 data={"version": version, "confirm_preview": True}),
                    200, "申诉场景送审")
        rejected_review = self.payload(self.expect(
            self.request(editor, "POST",
                         f"/api/plaza/submissions/{submission_id}/initial-review", data={
                             "version": version, "decision": "reject",
                             "reason": "验收：来源信息不足，请补充出处后再送审",
                         }), 200, "申诉场景初审驳回"), "初审驳回")
        appeal = self.payload(self.expect(
            self.request(author, "POST",
                         f"/api/plaza/submissions/{submission_id}/appeals", data={
                             "reason": "验收：来源出处已在第 12 页题号 3，请复核",
                         }), 201, "提交申诉"), "提交申诉")
        appeal_id = int((appeal.get("appeal") or {}).get("id") or 0)
        resolved = self.payload(self.expect(
            self.request(editor, "POST", "/api/plaza/admin/appeals/batch", data={
                "ids": [appeal_id], "action": "resolve",
                "reason": "验收：已复核来源，恢复作者修订权限",
            }), 200, "批量受理申诉"), "批量受理申诉")
        author_notes = self.notifications(author, kind="appeal_resolved")
        self.data["batch"]["appeals"] = {
            "submission_id": submission_id, "appeal_id": appeal_id,
            "initial_status": (rejected_review.get("submission") or {}).get("status"),
            "updated_count": resolved.get("updated_count"),
        }
        checks = [
            ("初审驳回 → 投稿 rejected",
             (rejected_review.get("submission") or {}).get("status") == "rejected",
             json.dumps(rejected_review.get("submission"), ensure_ascii=False)[:200]),
            ("作者申诉创建成功",
             appeal_id > 0 and (appeal.get("appeal") or {}).get("status") == "pending",
             json.dumps(appeal.get("appeal"), ensure_ascii=False)[:200]),
            ("批量受理申诉（updated=1，状态 resolved）",
             int(resolved.get("updated_count") or 0) == 1
             and any(isinstance(item, dict) and item.get("status") == "resolved"
                     for item in (resolved.get("updated") or [])),
             json.dumps({key: resolved.get(key) for key in (
                 "updated_count", "skipped_count")}, ensure_ascii=False)),
            ("作者收到 appeal_resolved 通知",
             len(author_notes.get("items") or []) >= 1,
             f"appeal_resolved={author_notes.get('total')}"),
        ]
        self.step("批量处理申诉（初审驳回 → 申诉 → 批量受理 + 通知）", checks,
                  started=started, data=self.data["batch"]["appeals"])

    # -- 5. 抽检门禁：显式 initial_100 + armed -----------------------------
    def gate_armed(self) -> None:
        started = time.time()
        admin = self.clients["admin"]
        learner = self.clients["learner"]
        anonymous = self.clients["anon"]
        state = self.set_gate({"mode": "initial_100", "armed": True},
                              "管理员显式启用抽检门禁（initial_100 + armed）")
        denied = self.request(learner, "PUT", "/api/plaza/admin/quality/gate",
                              data={"mode": "off"})
        anon_read = self.request(anonymous, "GET", "/api/plaza/admin/quality/gate")
        editor_state = self.gate_state()
        window_required = bool(state.get("required"))
        self.data["gate"]["armed_state"] = {
            key: state.get(key) for key in (
                "mode", "mode_source", "armed", "armed_source", "enforced", "required",
                "window_open", "pilot_limit", "published_total", "checks_total",
                "allowed_modes")
        }
        checks = [
            ("显式启用后 state：mode=initial_100 / armed / enforced / required",
             state.get("mode") == "initial_100"
             and state.get("mode_source") == "setting"
             and bool(state.get("armed")) is True
             and str(state.get("armed_source") or "") in ("setting", "env")
             and bool(state.get("enforced")) is True
             and window_required is True,
             json.dumps(self.data["gate"]["armed_state"], ensure_ascii=False)[:320]),
            ("试点窗口计数正确（published_total < pilot_limit）",
             int(state.get("pilot_limit") or 0) == 100
             and int(state.get("published_total") or 0) < 100
             and bool(state.get("window_open")) is True,
             json.dumps({"pilot_limit": state.get("pilot_limit"),
                         "published_total": state.get("published_total")},
                        ensure_ascii=False)),
            ("门禁状态含审计计数与 recent 结构、allowed_modes=off/initial_100/always",
             isinstance(state.get("counts"), dict)
             and all(key in (state.get("counts") or {})
                     for key in ("blocked", "override", "passed"))
             and isinstance(state.get("recent"), list)
             and set(state.get("allowed_modes") or []) == {"off", "initial_100", "always"},
             json.dumps({"counts": state.get("counts"),
                         "allowed_modes": state.get("allowed_modes")}, ensure_ascii=False)),
            ("普通用户修改门禁 403、匿名读门禁状态 401",
             denied.status_code == 403 and anon_read.status_code == 401,
             f"PUT=HTTP {denied.status_code} GET=HTTP {anon_read.status_code}"),
            ("编辑可读取门禁状态且与管理员设置一致",
             editor_state.get("mode") == "initial_100"
             and bool(editor_state.get("armed")) is True,
             json.dumps({"mode": editor_state.get("mode"),
                         "armed": editor_state.get("armed")}, ensure_ascii=False)),
        ]
        self.step("抽检门禁显式启用（initial_100 + armed）与权限负向", checks,
                  started=started, data=self.data["gate"]["armed_state"])

    def gate_block_and_pass(self) -> None:
        started = time.time()
        helper = self.helper
        editor = self.clients["editor"]
        author = self.clients["author"]
        verified = helper.create_verified_submission(
            "WS30 门禁-签发前抽检",
            content={
                "stem": f"【{self.marker}】门禁场景：已知函数 f(x)=x+1，求 f(2) 的值。",
                "options": [{"label": "A", "text": "3"}, {"label": "B", "text": "2"},
                            {"label": "C", "text": "4"}, {"label": "D", "text": "1"}],
                "answers": ["A"],
                "explanation": "代入 x=2 得 f(2)=2+1=3，因此选 A。（门禁场景）",
            })
        submission_id = int(verified["submission_id"])
        version = int(verified["version"])
        self.data["gate"]["submission"] = verified
        blocked = self.request(editor, "POST",
                               f"/api/plaza/submissions/{submission_id}/publish",
                               data={"version": version, "reason": "验收：未抽检直接签发（应 409）"})
        self.expect(blocked, 409, "未抽检签发被门禁阻断")
        detail = self.payload(blocked, "抽检门禁阻断")["detail"]
        state_after_block = self.gate_state()
        submission = self.payload(self.expect(
            author.request("GET", f"/api/plaza/submissions/{submission_id}", timeout=30.0),
            200, "阻断后投稿状态"), "阻断后投稿状态").get("submission") or {}
        releases = self.payload(self.expect(
            editor.request("GET", "/api/plaza/releases", params={"page_size": 100},
                           timeout=30.0), 200, "阻断后发布历史"), "阻断后发布历史")
        own_releases = [item for item in (releases.get("items") or [])
                        if isinstance(item, dict)
                        and int(item.get("submission_id") or 0) == submission_id]
        check = self.quality_check(public_id=None, submission_id=submission_id,
                                   decision="pass", note="验收：签发前抽检通过（题干/答案/来源）")
        published = self.payload(self.expect(
            editor.request("POST", f"/api/plaza/submissions/{submission_id}/publish",
                           data={"version": version,
                                 "reason": "验收：抽检通过后重新签发"}, timeout=30.0),
            200, "抽检通过后签发"), "抽检通过后签发")
        published_gate = published.get("quality_gate") or {}
        state_after_pass = self.gate_state()
        self.data["gate"]["blocked"] = {
            "blocked_total": state_after_block.get("blocked_total"),
            "latest_recent": (state_after_block.get("recent") or [{}])[0],
        }
        self.data["gate"]["passed"] = {
            "check_id": (check.get("check") or {}).get("id"),
            "passed_total": state_after_pass.get("passed_total"),
            "public_id": (published.get("release") or {}).get("public_id"),
        }
        checks = [
            ("未抽检的 verified 投稿签发被阻断：409 + code=quality_gate_blocked",
             detail.get("code") == "quality_gate_blocked"
             and detail.get("submission_id") == submission_id
             and int(detail.get("version") or 0) == version
             and detail.get("mode") == "initial_100",
             json.dumps(detail, ensure_ascii=False)[:320]),
            ("409 含 guidance / override 入口 / gate 状态字段",
             bool(str(detail.get("guidance") or "").strip())
             and detail.get("override_allowed") is True
             and (detail.get("gate") or {}).get("armed") is True
             and (detail.get("gate") or {}).get("required") is True,
             json.dumps({"guidance": str(detail.get("guidance") or "")[:80],
                         "gate": detail.get("gate")}, ensure_ascii=False)[:280]),
            ("阻断不改变投稿状态、不产生发布",
             submission.get("status") == "verified" and not own_releases
             and int(state_after_block.get("blocked_total") or 0) >= 1
             and str((state_after_block.get("recent") or [{}])[0].get("outcome") or "")
             == "blocked",
             json.dumps({"submission": submission.get("status"),
                         "releases": len(own_releases),
                         "blocked_total": state_after_block.get("blocked_total")},
                        ensure_ascii=False)),
            ("签发前抽检 pass：phase=pre_publish / release_id=null / publish_ready=true",
             check.get("phase") == "pre_publish"
             and (check.get("check") or {}).get("phase") == "pre_publish"
             and (check.get("check") or {}).get("release_id") is None
             and (check.get("gate") or {}).get("publish_ready") is True
             and bool((check.get("gate") or {}).get("blocking")) is False,
             json.dumps({"phase": check.get("phase"),
                         "check": check.get("check"),
                         "gate": check.get("gate")}, ensure_ascii=False)[:320]),
            ("抽检通过后签发 200：satisfied/pass_check 回显、无 override",
             published_gate.get("satisfied") is True
             and bool(published_gate.get("blocking")) is False
             and bool(published_gate.get("override")) is False
             and int((published_gate.get("pass_check") or {}).get("id") or 0)
             == int(((check.get("check") or {}).get("id")) or 0)
             and int(state_after_pass.get("passed_total") or 0) >= 1,
             json.dumps({"gate": {key: published_gate.get(key) for key in (
                 "mode", "armed", "enforced", "required", "satisfied", "blocking",
                 "override")}}, ensure_ascii=False)[:300]),
        ]
        self.step("抽检门禁：409 阻断 → 签发前抽检 pass → 再签发 200（审计可查）", checks,
                  started=started, data=self.data["gate"]["blocked"])

    def gate_override(self) -> None:
        started = time.time()
        helper = self.helper
        editor = self.clients["editor"]
        verified = helper.create_verified_submission(
            "WS30 门禁-override",
            content={
                "stem": f"【{self.marker}】门禁 override 场景：已知函数 f(x)=x+1，求 f(3) 的值。",
                "options": [{"label": "A", "text": "4"}, {"label": "B", "text": "2"},
                            {"label": "C", "text": "3"}, {"label": "D", "text": "1"}],
                "answers": ["A"],
                "explanation": "代入 x=3 得 f(3)=3+1=4，因此选 A。（override 场景）",
            })
        submission_id = int(verified["submission_id"])
        version = int(verified["version"])
        blocked = self.request(editor, "POST",
                               f"/api/plaza/submissions/{submission_id}/publish",
                               data={"version": version, "reason": "验收：override 前先触发阻断"})
        self.expect(blocked, 409, "override 场景先触发门禁阻断")
        no_reason = self.request(editor, "POST",
                                 f"/api/plaza/submissions/{submission_id}/publish",
                                 data={"version": version, "quality_gate_override": True,
                                       "reason": "验收：override 缺理由（应 422）"})
        self.expect(no_reason, 422, "override 缺理由应 422")
        overridden = self.payload(self.expect(
            editor.request("POST", f"/api/plaza/submissions/{submission_id}/publish", data={
                "version": version,
                "quality_gate_override": True,
                "override_reason": "验收：试点紧急上线，内容已由双人人工复核",
                "reason": "验收：编辑显式 override 签发",
            }, timeout=30.0), 200, "override 签发"), "override 签发")
        gate = overridden.get("quality_gate") or {}
        events = self.events(submission_id)
        override_events = [item for item in (events.get("items") or [])
                           if isinstance(item, dict)
                           and str(item.get("action") or "") == "quality_gate_override"]
        state = self.gate_state()
        recent = (state.get("recent") or [{}])[0]
        metrics = self.metrics()
        metric_gate = (metrics.get("quality") or {}).get("gate") or {}
        self.data["gate"]["override"] = {
            "override_total": state.get("override_total"),
            "check_id": None,
            "event_count": len(override_events),
        }
        checks = [
            ("override 缺 override_reason 时 422（字段提示可读）",
             no_reason.status_code == 422
             and "override_reason" in json.dumps(no_reason.json(), ensure_ascii=False),
             f"HTTP {no_reason.status_code}"),
            ("显式 override + 理由签发 200（quality_gate.override=true）",
             bool(gate.get("override")) is True
             and bool(gate.get("blocking")) is False
             and str(gate.get("override_reason") or "").startswith("验收：试点紧急上线"),
             json.dumps({key: gate.get(key) for key in (
                 "mode", "armed", "required", "override", "override_reason")},
                 ensure_ascii=False)[:280]),
            ("override 写入审计事件（quality_gate_override + 理由留痕）",
             len(override_events) == 1
             and "双人人工复核" in str(override_events[0].get("reason") or ""),
             json.dumps(override_events[:1], ensure_ascii=False)[:320]),
            ("门禁状态 recent 最新判定 outcome=override 且 override_total>=1",
             str(recent.get("outcome") or "") == "override"
             and int(state.get("override_total") or 0) >= 1,
             json.dumps({"recent": recent,
                         "override_total": state.get("override_total")},
                        ensure_ascii=False)[:280]),
            ("质量指标 gate.override_total 可查",
             int(metric_gate.get("override_total") or 0) >= 1
             and int(metric_gate.get("blocked_total") or 0) >= 2,
             json.dumps({key: metric_gate.get(key) for key in (
                 "mode", "armed", "blocked_total", "passed_total", "override_total")},
                 ensure_ascii=False)[:280]),
        ]
        self.step("抽检门禁 override：缺理由 422 / 带理由 200 / 事件与指标审计", checks,
                  started=started, data=self.data["gate"]["override"])

    # -- 6. 一键恢复：抽检 high 自动撤回 -----------------------------------
    def restore_high_withdraw(self) -> None:
        started = time.time()
        editor = self.clients["editor"]
        learner = self.clients["learner"]
        author = self.clients["author"]
        anonymous = self.clients["anon"]
        published = self.data["gate"]["submission"]
        public_id = str(published["public_id"])
        submission_id = int(published["submission_id"])
        favorite = self.payload(self.expect(
            self.request(learner, "POST", f"/api/plaza/questions/{public_id}/favorite"),
            200, "收藏待恢复精选题"), "收藏")
        checked = self.quality_check(
            public_id=public_id, decision="issue", severity="high",
            findings=["验收：题干条件缺失导致答案不唯一", "验收：选项与参考答案不匹配"],
            note="验收：高风险抽检自动撤回（用于一键恢复）")
        check = checked.get("check") or {}
        notified = checked.get("notified") or {}
        detail = self.payload(self.expect(
            anonymous.request("GET", f"/api/plaza/questions/{public_id}", timeout=30.0),
            200, "自动撤回后公开详情"), "自动撤回后公开详情")
        listing = self.payload(self.expect(
            anonymous.request("GET", "/api/plaza/questions",
                              params={"keyword": self.marker}, timeout=30.0),
            200, "自动撤回后公开列表"), "自动撤回后公开列表")
        listed = {str(item.get("public_id") or "") for item in (listing.get("items") or [])
                  if isinstance(item, dict)}
        restored = self.payload(self.expect(
            editor.request("POST", f"/api/plaza/releases/{public_id}/restore", data={
                "reason": "验收：错题答案已修订并由学科复核员复核通过，恢复公开发布",
                "confirm_resolved": True,
            }, timeout=30.0), 200, "一键恢复发布"), "一键恢复发布")
        restored_submission = restored.get("submission") or {}
        public_detail = self.payload(self.expect(
            anonymous.request("GET", f"/api/plaza/questions/{public_id}", timeout=30.0),
            200, "恢复后公开详情"), "恢复后公开详情")
        releases = self.payload(self.expect(
            editor.request("GET", "/api/plaza/releases", params={"page_size": 100},
                           timeout=30.0), 200, "恢复后发布历史"), "恢复后发布历史")
        own_releases = [item for item in (releases.get("items") or [])
                        if isinstance(item, dict)
                        and str(item.get("public_id") or "") == public_id]
        events = self.events(submission_id)
        actions = [str(item.get("action") or "") for item in (events.get("items") or [])]
        author_notes = self.notifications(author, kind="release_restored")
        learner_notes = self.notifications(learner, kind="favorite_restored")
        again = self.payload(self.expect(
            editor.request("POST", f"/api/plaza/releases/{public_id}/restore", data={
                "reason": "验收：重复恢复（幂等）", "confirm_resolved": True,
            }, timeout=30.0), 200, "重复恢复（幂等）"), "重复恢复")
        author_notes_again = self.notifications(author, kind="release_restored")
        self.data["restore"]["high"] = {
            "public_id": public_id,
            "check_id": check.get("id"),
            "notified": notified,
            "restored_version": restored.get("restored_version"),
            "restore_source": restored.get("restore_source"),
            "idempotent": again.get("idempotent"),
        }
        checks = [
            ("收藏精选题成功（收藏者为恢复通知对象）",
             bool(favorite.get("favorited")) is True,
             json.dumps(favorite, ensure_ascii=False)[:200]),
            ("抽检 issue+high 自动撤回（auto_withdrawn=true + 作者/收藏者通知）",
             bool(checked.get("auto_withdrawn")) is True
             and str(check.get("severity") or "") == "high"
             and (checked.get("submission") or {}).get("status") == "withdrawn"
             and notified.get("author") is True
             and int(notified.get("favoriters") or 0) >= 1,
             json.dumps({"auto_withdrawn": checked.get("auto_withdrawn"),
                         "notified": notified,
                         "submission": checked.get("submission")},
                        ensure_ascii=False)[:300]),
            ("自动撤回后公开不可见（详情 visible=false、列表不含该题）",
             detail.get("visible") is False and public_id not in listed,
             json.dumps({"visible": detail.get("visible"),
                         "status": detail.get("status")}, ensure_ascii=False)),
            ("一键恢复 200：restore_source=quality_auto_withdraw + 版本/通知回显",
             restored.get("restore_source") == "quality_auto_withdraw"
             and int(restored.get("restored_version") or 0) == int(published["version"])
             and restored_submission.get("status") == "published"
             and restored_submission.get("public_visible") is True
             and (restored.get("notified") or {}).get("author") is True
             and int((restored.get("notified") or {}).get("favoriters") or 0) >= 1
             and str((restored.get("quality_check") or {}).get("severity") or "") == "high",
             json.dumps({"restore_source": restored.get("restore_source"),
                         "submission": restored_submission,
                         "notified": restored.get("notified")}, ensure_ascii=False)[:320]),
            ("恢复后公开可读且发布历史回到 active",
             public_detail.get("visible") is True
             and public_detail.get("status") == "published"
             and any(item.get("status") == "active" for item in own_releases),
             json.dumps({"visible": public_detail.get("visible"),
                         "releases": len(own_releases)}, ensure_ascii=False)),
            ("事件链含 release_restored、作者/收藏者收到恢复通知",
             "release_restored" in actions
             and len(author_notes.get("items") or []) >= 1
             and len(learner_notes.get("items") or []) >= 1,
             json.dumps({"actions": actions[-6:],
                         "author_notes": author_notes.get("total"),
                         "favorite_notes": learner_notes.get("total")},
                        ensure_ascii=False)[:300]),
            ("重复恢复幂等（idempotent=true）且不重复通知",
             again.get("idempotent") is True
             and int(author_notes_again.get("total") or 0) == int(author_notes.get("total") or 0),
             json.dumps({"idempotent": again.get("idempotent"),
                         "notes": author_notes_again.get("total")}, ensure_ascii=False)),
        ]
        self.step("一键恢复：抽检 high 自动撤回 → 恢复公开（事件/通知/幂等）", checks,
                  started=started, data=self.data["restore"]["high"])

    # -- 7. 一键恢复：编辑撤回 / 负向 -------------------------------------
    def restore_editor_withdraw(self) -> None:
        started = time.time()
        editor = self.clients["editor"]
        anonymous = self.clients["anon"]
        p2 = self.data["baseline"]["p2"]
        public_id = str(p2["public_id"])
        withdrawal = self.payload(self.expect(
            editor.request("POST", f"/api/plaza/releases/{public_id}/withdraw", data={
                "reason": "验收：来源权利待确认，先下架复核", "mode": "normal",
            }, timeout=30.0), 200, "编辑撤回发布"), "编辑撤回发布")
        no_confirm = self.request(editor, "POST",
                                  f"/api/plaza/releases/{public_id}/restore",
                                  data={"reason": "验收：未确认问题已处理（应 422）"})
        self.expect(no_confirm, 422, "未确认问题已处理应 422")
        short_reason = self.request(editor, "POST",
                                    f"/api/plaza/releases/{public_id}/restore",
                                    data={"reason": "x", "confirm_resolved": True})
        unknown = self.request(editor, "POST", "/api/plaza/releases/plz-not-a-real-id/restore",
                               data={"reason": "验收：未知发布", "confirm_resolved": True})
        restored = self.payload(self.expect(
            editor.request("POST", f"/api/plaza/releases/{public_id}/restore", data={
                "reason": "验收：来源权利已确认，恢复公开发布", "confirm_resolved": True,
            }, timeout=30.0), 200, "编辑撤回后恢复"), "编辑撤回后恢复")
        detail = self.payload(self.expect(
            anonymous.request("GET", f"/api/plaza/questions/{public_id}", timeout=30.0),
            200, "恢复后公开详情"), "恢复后公开详情")
        active_again = self.payload(self.expect(
            editor.request("POST", f"/api/plaza/releases/{public_id}/restore", data={
                "reason": "验收：活跃发布无需恢复（幂等）", "confirm_resolved": True,
            }, timeout=30.0), 200, "活跃发布重复恢复"), "活跃发布重复恢复")
        self.data["restore"]["editor"] = {
            "public_id": public_id,
            "restore_source": restored.get("restore_source"),
            "restored_version": restored.get("restored_version"),
        }
        checks = [
            ("编辑撤回发布（status=withdrawn）",
             withdrawal.get("status") == "withdrawn",
             json.dumps(withdrawal, ensure_ascii=False)[:200]),
            ("恢复必须 confirm_resolved=true（缺确认 422、理由过短 422）",
             no_confirm.status_code == 422 and short_reason.status_code == 422,
             f"no_confirm=HTTP {no_confirm.status_code} short=HTTP {short_reason.status_code}"),
            ("未知公开题恢复 404",
             unknown.status_code == 404, f"HTTP {unknown.status_code}"),
            ("编辑撤回后恢复 200：restore_source=editor_withdraw、无抽检记录关联",
             restored.get("restore_source") == "editor_withdraw"
             and restored.get("quality_check") is None
             and (restored.get("submission") or {}).get("status") == "published",
             json.dumps({"restore_source": restored.get("restore_source"),
                         "quality_check": restored.get("quality_check")},
                        ensure_ascii=False)[:260]),
            ("恢复后公开详情 visible=true / status=published",
             detail.get("visible") is True and detail.get("status") == "published",
             json.dumps({"visible": detail.get("visible"),
                         "status": detail.get("status")}, ensure_ascii=False)),
            ("活跃发布再次恢复幂等（idempotent=true）",
             active_again.get("idempotent") is True,
             json.dumps(active_again, ensure_ascii=False)[:200]),
        ]
        self.step("一键恢复：编辑撤回后恢复 / 确认与理由负向 / 未知 404 / 活跃幂等",
                  checks, started=started, data=self.data["restore"]["editor"])

    # -- 8. 一键恢复：已有更新版本活跃时 409 -------------------------------
    def restore_newer_version_conflict(self) -> None:
        started = time.time()
        helper = self.helper
        editor = self.clients["editor"]
        author = self.clients["author"]
        anonymous = self.clients["anon"]
        p3 = self.data["baseline"]["p3"]
        public_id = str(p3["public_id"])
        submission_id = int(p3["submission_id"])
        checked = self.quality_check(
            public_id=public_id, decision="issue", severity="high",
            findings=["验收：初始版答案错误，需修订"],
            note="验收：高风险抽检自动撤回（用于版本冲突场景）")
        self.expect(
            author.request("PATCH", f"/api/paperlab/questions/{p3['question_id']}", data={
                "version": 1,
                "content": {
                    "stem": (f"【{self.marker}】版本冲突场景修订版："
                             "已知函数 f(x)=2x+1，求 f(2) 的值。"),
                },
            }, timeout=30.0), 200, "修订个人题内容（v2）")
        revised = self.payload(self.expect(
            author.request("POST", f"/api/plaza/submissions/{submission_id}/revise",
                           data={"version": int(p3["version"])}, timeout=30.0),
            200, "改版创建新投稿版本"), "改版创建新投稿版本")
        new_version = int((revised.get("submission") or {}).get("version") or 0)
        self.expect(editor.request("POST",
                                   f"/api/plaza/submissions/{submission_id}/initial-review",
                                   data={
                                       "version": new_version, "decision": "accept",
                                       "reason": "验收：v2 初审通过",
                                       "source_checked": True, "privacy_checked": True,
                                       "completeness_checked": True,
                                   }, timeout=30.0), 200, "v2 编辑初审通过")
        review_order = ["subject_reviewer1", "subject_reviewer2",
                        "reviewer1", "reviewer2", "reviewer3"]
        transitions: list[str] = []
        for index, key in enumerate(review_order, start=1):
            reviewed_body = self.payload(self.expect(
                self.request(self.helper.clients[key], "POST",
                             f"/api/plaza/submissions/{submission_id}/reviews", data={
                                 "version": new_version, "verdict": "approve", "reasons": "",
                                 "independent_answer": f"验收 v2 独立作答（第 {index} 票）：A",
                                 "conflict_declared": False,
                             }), 201, f"v2 第 {index} 票评审"), "v2 评审响应")
            transitions.append(str(reviewed_body.get("transition") or ""))
        pre_check = self.quality_check(
            submission_id=submission_id, decision="pass",
            note="验收：v2 签发前抽检通过")
        published_v2 = self.payload(self.expect(
            editor.request("POST", f"/api/plaza/submissions/{submission_id}/publish", data={
                "version": new_version, "reason": "验收：v2 评审阈值达成，签发新版本",
            }, timeout=30.0), 200, "v2 签发"), "v2 签发")
        conflict = self.request(editor, "POST",
                                f"/api/plaza/releases/{public_id}/restore", data={
                                    "reason": "验收：尝试恢复旧发布（应 409）",
                                    "confirm_resolved": True,
                                }, timeout=30.0)
        self.expect(conflict, 409, "已有新版本活跃时恢复旧发布应 409")
        detail = self.payload(conflict, "恢复冲突")["detail"]
        public_detail = self.payload(self.expect(
            anonymous.request("GET", f"/api/plaza/questions/{public_id}", timeout=30.0),
            200, "冲突后公开详情"), "冲突后公开详情")
        public_text = json.dumps(public_detail, ensure_ascii=False)
        self.data["restore"]["conflict"] = {
            "public_id": public_id,
            "new_version": new_version,
            "active_version": detail.get("active_version"),
            "restore_version": detail.get("restore_version"),
            "transitions": transitions,
        }
        checks = [
            ("high 抽检自动撤回初始版本",
             bool(checked.get("auto_withdrawn")) is True
             and (checked.get("submission") or {}).get("status") == "withdrawn",
             json.dumps(checked.get("submission"), ensure_ascii=False)[:200]),
            ("改版 v2 → 初审 → 5 票 → verified",
             new_version == int(p3["version"]) + 1
             and transitions[-1:] == ["verified"],
             f"version={new_version} transitions={transitions}"),
            ("v2 签发前抽检 pass 后签发 active v2",
             (pre_check.get("gate") or {}).get("publish_ready") is True
             and (published_v2.get("release") or {}).get("status") == "active"
             and int((published_v2.get("release") or {}).get("version") or 0) == new_version,
             json.dumps(published_v2.get("release"), ensure_ascii=False)[:240]),
            ("恢复旧发布被拒 409：已有更新版本活跃（active_version=2）",
             detail.get("message") and "活跃发布" in str(detail.get("message"))
             and int(detail.get("active_version") or 0) == new_version
             and int(detail.get("restore_version") or 0) == int(p3["version"]),
             json.dumps(detail, ensure_ascii=False)[:320]),
            ("线上仍是 v2 正文（恢复冲突不造成内容回退）",
             "修订版" in str((public_detail.get("content") or {}).get("stem") or "")
             and "2x+1" in str((public_detail.get("content") or {}).get("stem") or "")
             and "2x+1" in public_text,
             str((public_detail.get("content") or {}).get("stem") or "")[:160]),
        ]
        self.step("一键恢复：改版 v2 已活跃时恢复旧发布 409（避免内容回退）", checks,
                  started=started, data=self.data["restore"]["conflict"])

    # -- 9. 通知增强：unread-count / kind 全读 / ids 去重 ------------------
    def notification_center(self) -> None:
        started = time.time()
        learner = self.clients["learner"]
        anonymous = self.clients["anon"]
        listing = self.notifications(learner, page_size=100)
        summary = self.unread_summary(learner)
        anon_summary = self.request(anonymous, "GET", "/api/plaza/notifications/unread-count")
        by_kind = dict(summary.get("by_kind") or {})
        unread_before = int(summary.get("unread_count") or 0)
        kind_target = next((kind for kind in ("issue_resolved", "report_rejected",
                                             "favorite_restored") if by_kind.get(kind)),
                           next(iter(by_kind), ""))
        marked_kind = self.payload(self.expect(
            learner.request("POST", "/api/plaza/notifications/read", data={"kind": kind_target},
                            timeout=30.0), 200, "按 kind 全读"), "按 kind 全读")
        after_kind = self.unread_summary(learner)
        after_kind_by = dict(after_kind.get("by_kind") or {})
        invalid = self.request(learner, "POST", "/api/plaza/notifications/read",
                               data={"kind": "not_a_kind"})
        remaining = self.notifications(learner, unread_only=True, page_size=100)
        remaining_items = [item for item in (remaining.get("items") or [])
                           if isinstance(item, dict)]
        duplicate_probe = remaining_items[0] if remaining_items else {}
        ids_read = self.payload(self.expect(
            learner.request("POST", "/api/plaza/notifications/read", data={
                "ids": [int(duplicate_probe.get("id") or 0),
                        int(duplicate_probe.get("id") or 0)],
            }, timeout=30.0), 200, "按 ids 读取（重复 id 去重）"), "按 ids 读取")
        repeat_ids = self.payload(self.expect(
            learner.request("POST", "/api/plaza/notifications/read", data={
                "ids": [int(duplicate_probe.get("id") or 0)],
            }, timeout=30.0), 200, "重复按 ids 读取"), "重复按 ids 读取")
        all_read = self.payload(self.expect(
            learner.request("POST", "/api/plaza/notifications/read", data={"all": True},
                            timeout=30.0), 200, "全部标记已读"), "全部标记已读")
        after_all = self.unread_summary(learner)
        self.data["notifications"]["center"] = {
            "unread_before": unread_before,
            "by_kind": by_kind,
            "kind_target": kind_target,
            "kind_updated": marked_kind.get("updated"),
            "all_unread": after_all.get("unread_count"),
        }
        checks = [
            ("unread-count 与通知列表 unread_count 同口径、by_kind 求和一致",
             int(summary.get("unread_count") or 0) == int(listing.get("unread_count") or 0)
             and sum(int(value) for value in by_kind.values()) == unread_before
             and int(summary.get("kinds_total") or 0) == len(by_kind),
             json.dumps({"summary": summary.get("unread_count"),
                         "listing": listing.get("unread_count"),
                         "by_kind": by_kind}, ensure_ascii=False)[:300]),
            ("存在 ≥2 类未读通知（反馈/举报/收藏恢复）",
             unread_before >= 2 and len(by_kind) >= 2,
             f"unread={unread_before} kinds={sorted(by_kind)}"),
            ("按 kind 全读：scope=kind、updated=该类型未读数、其他类型保留",
             marked_kind.get("scope") == "kind"
             and int(marked_kind.get("updated") or 0) == int(by_kind.get(kind_target) or 0)
             and kind_target not in after_kind_by
             and int(after_kind.get("unread_count") or 0)
             == unread_before - int(by_kind.get(kind_target) or 0),
             json.dumps({"marked": marked_kind,
                         "after": after_kind_by}, ensure_ascii=False)[:300]),
            ("未知 kind 返回 422 且给出 allowed 列表",
             invalid.status_code == 422
             and "allowed" in json.dumps(invalid.json(), ensure_ascii=False),
             f"HTTP {invalid.status_code}"),
            ("按 ids 读取时重复 id 去重（updated=1），再次读取 updated=0",
             int(ids_read.get("updated") or 0) == 1
             and int(repeat_ids.get("updated") or 0) == 0
             and ids_read.get("scope") == "ids",
             json.dumps({"first": ids_read.get("updated"),
                         "repeat": repeat_ids.get("updated")}, ensure_ascii=False)),
            ("匿名读未读数 401；all=true 全读后未读归零",
             anon_summary.status_code == 401
             and int(after_all.get("unread_count") or 0) == 0
             and all_read.get("scope") == "all",
             f"anon=HTTP {anon_summary.status_code} unread={after_all.get('unread_count')}"),
        ]
        self.step("通知增强：unread-count 口径 / kind 全读 / ids 去重 / 未知 kind / all 全读",
                  checks, started=started, data=self.data["notifications"]["center"])

    # -- 10. 通知去重 + 质量指标 -------------------------------------------
    def notification_dedupe(self) -> None:
        started = time.time()
        editor = self.clients["editor"]
        author = self.clients["author"]
        p1 = self.data["baseline"]["p1"]
        public_id = str(p1["public_id"])
        first = self.quality_check(
            public_id=public_id, decision="issue", severity="medium",
            findings=["验收：解析步骤跳步"], note="验收：通知去重样本 1")
        second = self.quality_check(
            public_id=public_id, decision="issue", severity="low",
            findings=["验收：解析排版问题"], note="验收：通知去重样本 2")
        notes = self.notifications(author, kind="quality_issue", page_size=100)
        scoped_notes = self.find_notifications(
            notes, kind="quality_issue", submission_id=int(p1["submission_id"]))
        queue = self.payload(self.expect(
            editor.request("GET", "/api/plaza/admin/quality/queue",
                           params={"status": "issues", "page_size": 100}, timeout=30.0),
            200, "抽检问题队列"), "抽检问题队列")
        queue_entry = next((item for item in (queue.get("items") or [])
                            if isinstance(item, dict)
                            and int(item.get("submission_id") or 0)
                            == int(p1["submission_id"])), {})
        self.data["notifications"]["dedupe"] = {
            "check_ids": [(first.get("check") or {}).get("id"),
                          (second.get("check") or {}).get("id")],
            "quality_issue_notifications": len(scoped_notes),
        }
        checks = [
            ("两次 medium 抽检记录均创建",
             all(bool((item.get("check") or {}).get("id")) for item in (first, second))
             and (first.get("notified") or {}).get("author") is True
             and (second.get("notified") or {}).get("author") is True,
             json.dumps({"first": (first.get("check") or {}).get("id"),
                         "second": (second.get("check") or {}).get("id")},
                        ensure_ascii=False)),
            ("同一事件重复触发在去重窗口内只产生一条通知",
             len(scoped_notes) == 1,
             f"quality_issue 通知数={len(scoped_notes)}（submission={p1['submission_id']}）"),
            ("抽检问题队列显示最新一次结论（severity=low）",
             str((queue_entry.get("latest_check") or {}).get("decision") or "") == "issue"
             and str((queue_entry.get("latest_check") or {}).get("severity") or "") == "low",
             json.dumps(queue_entry.get("latest_check") or {}, ensure_ascii=False)[:240]),
        ]
        self.step("通知去重（同事件重复触发只一条）+ 抽检队列最新结论", checks,
                  started=started, data=self.data["notifications"]["dedupe"])

    def final_metrics(self) -> None:
        started = time.time()
        metrics = self.metrics()
        quality = metrics.get("quality") or {}
        gate = quality.get("gate") or {}
        restorations = metrics.get("restorations") or {}
        withdrawal = metrics.get("withdrawals") or {}
        ops_batch = (metrics.get("ops") or {}).get("batch") or {}
        notifications = metrics.get("notifications") or {}
        caliber = metrics.get("caliber") or {}
        by_kind = {str(item.get("kind") or ""): item for item in (ops_batch.get("by_kind") or [])
                   if isinstance(item, dict)}
        self.data["metrics"] = {
            "gate": {key: gate.get(key) for key in (
                "mode", "mode_source", "armed", "enforced", "required",
                "blocked_total", "passed_total", "override_total", "pilot_limit")},
            "restorations": {key: restorations.get(key) for key in ("total", "latest_event")},
            "ops_batch": {key: ops_batch.get(key) for key in (
                "batches", "requested", "updated_total", "skipped_total")},
            "notifications": {key: notifications.get(key) for key in (
                "total", "unread_total", "read_total")},
        }
        checks = [
            ("指标 quality.gate：initial_100 / setting / armed / enforced",
             gate.get("mode") == "initial_100"
             and gate.get("mode_source") == "setting"
             and bool(gate.get("armed")) is True
             and bool(gate.get("enforced")) is True
             and int(gate.get("pilot_limit") or 0) == 100,
             json.dumps(self.data["metrics"]["gate"], ensure_ascii=False)[:300]),
            ("指标 quality.gate：blocked/passed/override 计数与操作一致",
             int(gate.get("blocked_total") or 0) >= 2
             and int(gate.get("passed_total") or 0) >= 2
             and int(gate.get("override_total") or 0) >= 1,
             json.dumps({key: gate.get(key) for key in (
                 "blocked_total", "passed_total", "override_total")},
                 ensure_ascii=False)),
            ("指标 quality：签发前/后抽检计数与抽样覆盖率",
             int(quality.get("pre_publish_checks") or 0) >= 2
             and int(quality.get("post_publish_checks") or 0) >= 1
             and float((quality.get("sampling") or {}).get("coverage_rate") or 0) >= 0,
             json.dumps({"pre": quality.get("pre_publish_checks"),
                         "post": quality.get("post_publish_checks"),
                         "sampling": quality.get("sampling")},
                        ensure_ascii=False)[:320]),
            ("指标 withdrawals/restorations：一键恢复次数与最近事件",
             int(withdrawal.get("restorations") or 0) >= 2
             and int(restorations.get("total") or 0) >= 2
             and bool(restorations.get("latest_event")),
             json.dumps({"withdrawals": withdrawal,
                         "restorations": restorations}, ensure_ascii=False)[:320]),
            ("指标 ops.batch：本脚本 4 个批次（纠错 2 + 举报 1 + 申诉 1）/ updated≥22 / 实例累计口径",
             int(ops_batch.get("batches") or 0) >= 4
             and int(ops_batch.get("requested") or 0) >= 44
             and int(ops_batch.get("updated_total") or 0) >= 22
             and int(ops_batch.get("skipped_total") or 0) >= 2
             and int((by_kind.get("issue") or {}).get("updated") or 0) >= 19
             and int((by_kind.get("report") or {}).get("updated") or 0) >= 2
             and int((by_kind.get("appeal") or {}).get("updated") or 0) >= 1,
             json.dumps({"batch": self.data["metrics"]["ops_batch"],
                         "by_kind": list(by_kind)}, ensure_ascii=False)[:320]),
            ("指标 notifications：total = unread_total + read_total",
             int(notifications.get("total") or 0) >= 1
             and int(notifications.get("unread_total") or 0)
             + int(notifications.get("read_total") or 0)
             == int(notifications.get("total") or 0),
             json.dumps(self.data["metrics"]["notifications"], ensure_ascii=False)),
            ("指标 caliber 含 quality/ops/notifications 口径说明",
             "gate" in str(caliber.get("quality") or "")
             and "批量" in str(caliber.get("ops") or "")
             and "通知" in str(caliber.get("notifications") or ""),
             json.dumps({key: str(caliber.get(key))[:80] for key in (
                 "quality", "ops", "notifications")}, ensure_ascii=False)[:320]),
        ]
        self.step("质量运营指标字段（gate / restorations / ops.batch / notifications / caliber）",
                  checks, started=started, data=self.data["metrics"])

    # -- 11. 收尾：门禁恢复 off --------------------------------------------
    def restore_gate_off(self) -> None:
        started = time.time()
        state = self.set_gate({"mode": "off"}, "收尾：抽检门禁恢复 off")
        editor_state = self.gate_state()
        self.data["gate"]["final"] = {
            key: state.get(key) for key in (
                "mode", "mode_source", "armed", "armed_source", "enforced", "required")
        }
        checks = [
            ("门禁模式恢复 off（enforced=false，不阻断后续验收签发）",
             state.get("mode") == "off"
             and bool(state.get("enforced")) is False
             and bool(editor_state.get("enforced")) is False,
             json.dumps(self.data["gate"]["final"], ensure_ascii=False)[:240]),
            ("脚本显式设置的门禁场景已收尾（mode_source=setting）",
             str(state.get("mode_source") or "") == "setting",
             json.dumps({"mode_source": state.get("mode_source")}, ensure_ascii=False)),
        ]
        self.step("收尾：抽检门禁恢复 off（显式 armed 场景由本脚本负责最终关闭）", checks,
                  started=started, data=self.data["gate"]["final"])

    # -- 主流程 / 收尾 ----------------------------------------------------
    def restore_gate_off_for_failure(self) -> None:
        """失败/异常退出时的兜底收尾：尽力把门禁恢复为 off（不覆盖主流程异常）。"""
        if "admin" not in self.clients:
            return
        if str((self.data.get("gate") or {}).get("final", {}).get("mode") or "") == "off":
            return
        origin = (self.data.get("gate") or {}).get("origin") or {}
        if not (origin.get("armed") or self.data.get("gate", {}).get("armed_state")):
            return
        try:
            self.request(self.clients["admin"], "PUT", "/api/plaza/admin/quality/gate",
                         data={"mode": "off"})
        except Exception:  # noqa: BLE001 - 收尾失败已在主流程异常中体现
            return

    def run(self) -> None:
        self.bootstrap()
        self.publish_baseline()
        self.monitor_structure()
        self.monitor_degrade()
        self.batch_issues()
        self.batch_reports()
        self.batch_appeals()
        self.gate_armed()
        self.gate_block_and_pass()
        self.gate_override()
        self.restore_high_withdraw()
        self.restore_editor_withdraw()
        self.restore_newer_version_conflict()
        self.notification_center()
        self.notification_dedupe()
        self.final_metrics()
        self.restore_gate_off()

    def finalize(self) -> int:
        endpoints = [self.helper.api_calls[key] for key in sorted(self.helper.api_calls)]
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
            "gate_semantics": (
                "抽检门禁 off|initial_100(默认)|always；显式配置（env STUDY_PLAZA_QUALITY_GATE "
                "或管理员 PUT /api/plaza/admin/quality/gate {mode,armed}）即激活；未显式配置且"
                "库中无抽检记录时不阻断（兼容既有脚本）。本脚本开头读取门禁状态（显式 armed 时"
                "临时关闭以完成基线发布），显式设置为 initial_100+armed 验证，结束时恢复 off。"),
            "required_env": [
                "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1",
                "STUDY_ALLOWED_HOSTS=127.0.0.1,localhost,[::1],testserver",
                "STUDY_INDEX_PREWARM=0",
                "STUDY_DATA_DIR=<临时目录>（全新实例；已有验收账号时自动复用登录）",
                "（建议）STUDY_PLAZA_QUALITY_GATE=initial_100 验证实例显式 armed 场景",
            ],
            "instance_start_example": (
                "pwsh: cd <repo>; $env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; "
                "$env:STUDY_ALLOWED_HOSTS='127.0.0.1,localhost,[::1],testserver'; "
                "$env:STUDY_INDEX_PREWARM='0'; $env:STUDY_PLAZA_QUALITY_GATE='initial_100'; "
                "$env:STUDY_DATA_DIR=Join-Path $env:TEMP ('zhixu-ops-accept-'+(Get-Random)); "
                "python -m uvicorn app.main:app --host 127.0.0.1 --port 8856"
            ),
            "run_command": (
                "python scripts/integration/ops_accept.py --base-url http://127.0.0.1:8856 "
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
        except Exception:  # noqa: BLE001 - 非交互流不支持时忽略
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
            "baseline": {key: (value or {}).get("public_id")
                         for key, value in (data.get("baseline") or {}).items()},
            "gate": {
                "origin_mode": (data.get("gate") or {}).get("origin", {}).get("mode"),
                "final_mode": (data.get("gate") or {}).get("final", {}).get("mode"),
                "blocked_total": ((data.get("metrics") or {}).get("gate") or {}).get(
                    "blocked_total"),
                "override_total": ((data.get("metrics") or {}).get("gate") or {}).get(
                    "override_total"),
            },
            "batch": {key: (value or {}).get("updated_count")
                      for key, value in (data.get("batch") or {}).items()},
            "restore": {
                "high": (data.get("restore") or {}).get("high", {}).get("restore_source"),
                "editor": (data.get("restore") or {}).get("editor", {}).get("restore_source"),
                "conflict_active_version": (data.get("restore") or {}).get(
                    "conflict", {}).get("active_version"),
            },
        },
        "problems": report.get("problems") or [],
        "warnings": (report.get("warnings") or [])[:10],
        "elapsed_seconds": report.get("elapsed_seconds"),
        "report": report.get("report_path"),
    }


def main(argv: list[str] | None = None) -> int:
    _configure_console()
    parser = argparse.ArgumentParser(
        description="WS30 第四轮端到端验收：运维监控 / 抽检门禁 / 一键恢复 / 批量 / 通知")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL,
                        help=f"隔离实例地址（默认 {DEFAULT_BASE_URL}）")
    parser.add_argument("--out", default=str(DEFAULT_OUT),
                        help="输出目录（相对仓库根，默认 screenshots/integration）")
    parser.add_argument("--json", default=str(DEFAULT_REPORT),
                        help="报告 JSON 路径（相对仓库根，默认 "
                             "screenshots/integration/ops-accept-report.json）")
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
    flow = OpsAccept(
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
            flow.restore_gate_off_for_failure()
        except Exception:  # noqa: BLE001 - 收尾失败不覆盖主流程异常
            traceback.print_exc()
        flow.close()
        flow.report["elapsed_seconds"] = round(time.time() - started, 2)
        flow.report["report_path"] = str(report_path)
        code = flow.finalize()

    print(json.dumps(_summarize(flow.report), ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main())
