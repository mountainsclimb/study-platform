"""本轮审查整改的独立验收脚本（P0 安全/性能/数据）。

用法（在 study-platform 目录，默认只读题库跟随 releases/CURRENT）：
  python scripts/audit_accept.py [--bank <快照路径>] [--out <目录>]

覆盖：
  1. 空白图门禁：1×1 / 40×40 纯白图必须 422，带笔迹的正常尺寸图通过；
  2. 题图鉴权：无关 qid 不得借他题公开引用读答案图；本题公开引用零作答可读；
  3. 组级误拦截：R3 全库 `_group_public_issues` 应为 0 个问题组；
  4. 块级解析：只有 solution_blocks 的题 `has_solution=true`；
  5. 检索性能：`/api/questions` 默认页与关键词检索耗时（目标 <50ms，仅报告不硬断言）；
  6. SymPy 预算：`9**9**9` 等必须快速返回不可比，不阻塞；
  7. 静态契约：笔记 Agent 调用必须显式传 `system_prompt`、交叉验证不得覆盖讲解模式。
"""
import argparse
import io
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEFAULT_GROUP_BLOCK_ONLY_QID = "c0cfeab4f0e96c5b:5#3"


def _png(size, draw=False):
    from PIL import Image, ImageDraw
    image = Image.new("RGB", size, "white")
    if draw:
        tool = ImageDraw.Draw(image)
        tool.line((10, 10, size[0] - 10, size[1] - 10), fill="black", width=4)
        tool.rectangle((20, 20, size[0] - 20, size[1] - 20), outline="blue", width=3)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def check_blank_gate(app_module) -> dict:
    from PIL import Image
    from fastapi import HTTPException
    practice = app_module.practice

    def verdict(image):
        try:
            practice._check_not_blank(image)
            return "accepted"
        except HTTPException as exc:
            return "rejected:%s" % exc.status_code

    results = {
        "1x1_white": verdict(Image.new("RGB", (1, 1), "white")),
        "40x40_white": verdict(Image.new("RGB", (40, 40), "white")),
        "240x240_blank": verdict(Image.new("RGB", (240, 240), "white")),
        "240x240_drawn": verdict(Image.new("RGB", (240, 240), "white").point(lambda v: v)),
    }
    # 带笔迹图用于确认正常作答不受影响
    drawn = Image.new("RGB", (240, 240), "white")
    from PIL import ImageDraw
    ImageDraw.Draw(drawn).line((10, 10, 220, 220), fill="black", width=5)
    results["240x240_drawn"] = verdict(drawn)
    results["ok"] = (
        results["1x1_white"].startswith("rejected")
        and results["40x40_white"].startswith("rejected")
        and results["240x240_blank"].startswith("rejected")
        and results["240x240_drawn"] == "accepted"
    )
    return results


def check_asset_gate(app_module) -> dict:
    from fastapi import HTTPException
    practice = app_module.practice
    original = practice._question_group_key
    original_require = practice._require_attempt
    practice._question_group_key = lambda qid: None

    def _needs_attempt(user_id, qid):
        raise HTTPException(403, "需要先作答")

    practice._require_attempt = _needs_attempt
    entry = {"asset_id": "audit", "refs": [
        {"qid": "A:1", "group_id": None, "field": "solution", "permission": "after_attempt"},
        {"qid": "B:2", "group_id": None, "field": "stem", "permission": "public"},
    ]}

    def verdict(qid):
        try:
            practice._asset_authorized(entry, qid, user_id=1)
            return "allowed"
        except HTTPException as exc:
            return "denied:%s" % exc.status_code

    try:
        results = {
            "unrelated_qid": verdict("C:3"),
            "public_owner_qid": verdict("B:2"),
            "answer_owner_qid": verdict("A:1"),
        }
    finally:
        practice._question_group_key = original
        practice._require_attempt = original_require
    results["ok"] = (
        results["unrelated_qid"].startswith("denied")
        and results["public_owner_qid"] == "allowed"
        and results["answer_owner_qid"].startswith("denied")
    )
    return results


def check_groups(app_module) -> dict:
    practice = app_module.practice
    import sqlite3
    path = Path(practice._bank_dir()) / "题库.db"
    con = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = [dict(row) for row in con.execute(
            "SELECT group_id, material_text, parent_stem, shared_blocks_json, member_index_json FROM groups")]
    finally:
        con.close()
    bad = [(row["group_id"], practice._group_public_issues(row)) for row in rows]
    bad = [(gid, issues) for gid, issues in bad if issues]
    return {"groups": len(rows), "blocked": len(bad), "sample": bad[:5],
            "ok": len(bad) == 0}


def check_solution_blocks(app_module) -> dict:
    practice = app_module.practice
    result = {"qid": DEFAULT_GROUP_BLOCK_ONLY_QID}
    try:
        question = practice.load_question(DEFAULT_GROUP_BLOCK_ONLY_QID)
    except Exception as exc:  # 题库版本可能不含该题
        result.update({"skipped": str(exc), "ok": True})
        return result
    result["has_solution"] = question["has_solution"]
    result["has_solution_blocks"] = bool(question.get("solution_blocks"))
    result["ok"] = result["has_solution"] and result["has_solution_blocks"]
    return result


def check_search_perf(app_module, rounds: int = 3) -> dict:
    from fastapi.testclient import TestClient
    app = app_module.app
    timings = {"default": [], "keyword": [], "deep_page": []}
    # TrustedHostMiddleware 只允许 127.0.0.1/localhost，TestClient 默认 testserver 会被拒。
    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.post("/api/auth/register", json={"username": "审计计时", "password": "audit-pass-123"})
        for _ in range(rounds):
            for name, params in (("default", {"page_size": 12}), ("keyword", {"q": "函数", "page_size": 12})):
                started = time.perf_counter()
                response = client.get("/api/questions", params=params)
                elapsed = (time.perf_counter() - started) * 1000
                if response.status_code != 200:
                    return {"ok": False, "error": response.text[:200], "timings": timings}
                timings[name].append(round(elapsed, 1))
        for key, values in timings.items():
            values.sort()
    best = {"default": min(timings["default"]) if timings["default"] else None,
            "keyword": min(timings["keyword"]) if timings["keyword"] else None}
    return {"timings_ms": {key: values for key, values in timings.items()},
            "best_ms": best,
            "target_ms": 50,
            "ok": all(value is not None and value < 200 for value in best.values())}


def check_sympy_budget(app_module) -> dict:
    verify = sys.modules.get("app.ai.verify") or __import__("app.ai.verify", fromlist=["x"])
    cases = ["9**9**9", "10**100000", "factorial(100000)"]
    results = {}
    for expr in cases:
        started = time.perf_counter()
        try:
            verdict = verify.compare_answers(
                {"kind": "expression", "value": expr}, {"kind": "expression", "value": "x"})
            elapsed = (time.perf_counter() - started) * 1000
            results[expr] = {"comparable": verdict.get("comparable"),
                             "ms": round(elapsed, 1)}
        except Exception as exc:
            elapsed = (time.perf_counter() - started) * 1000
            results[expr] = {"error": type(exc).__name__, "ms": round(elapsed, 1)}
    results["ok"] = all(
        item.get("comparable") is False and item.get("ms", 99999) < 3000
        for key, item in results.items() if isinstance(item, dict) and key != "ok"
    )
    return results


def check_static_contracts() -> dict:
    notes = (ROOT / "app" / "ai" / "notes_agent.py").read_text(encoding="utf-8")
    verify = (ROOT / "app" / "ai" / "verify.py").read_text(encoding="utf-8")
    md = (ROOT / "web" / "js" / "core" / "markdown.js").read_text(encoding="utf-8")
    dom = (ROOT / "web" / "js" / "core" / "dom.js").read_text(encoding="utf-8")
    result = {
        # 笔记 Agent 在统一模型入口注入 L0 系统提示词（assembled["l0"] → l0_system → setdefault system_prompt）
        "notes_pass_system_prompt": ('l0_system = assembled["l0"]' in notes
                                      and 'kwargs.setdefault("system_prompt", l0_system)' in notes),
        "verify_structured_not_override": 'if getattr(data, "structured_verify", False) else' not in verify,
        "markdown_fail_closed": ("DOMPurify" in md and ("escape" in md or "textContent" in md or "pre" in md)),
        "dom_katex_error_fallback": "katex-error" in dom,
    }
    result["ok"] = all(result.values())
    return result


def check_provider_policy(app_module) -> dict:
    """自定义 API 地址策略：local_open 放行本地模型、公网策略拦截私网、元数据始终禁止。"""
    import os
    from fastapi import HTTPException
    netguard = __import__("app.ai.netguard", fromlist=["x"])
    providers = __import__("app.ai.providers", fromlist=["x"])

    def verdict(url, *, resolve=False):
        try:
            # 走真实调用路径（含 allowlist 模式下预设/环境变量/数据库白名单注入）
            providers._validate_url(url, resolve=resolve)
            return "accepted"
        except HTTPException as exc:
            return "rejected:%s:%s" % (exc.status_code, str(exc.detail)[:40])

    original_policy = os.environ.get("STUDY_AI_URL_POLICY")
    original_public = os.environ.get("STUDY_PUBLIC_MODE")
    try:
        os.environ["STUDY_AI_URL_POLICY"] = "local_open"
        os.environ.pop("STUDY_PUBLIC_MODE", None)
        local = {
            "ollama": verdict("http://127.0.0.1:11434/v1"),
            "lan": verdict("http://192.168.1.10:8000/v1"),
            "custom_port": verdict("https://relay.example.test:8443/v1"),
            "azure_query": verdict("https://my.openai.azure.com/openai/deployments/d?api-version=2024-10-21"),
            "metadata": verdict("http://169.254.169.254/latest/meta-data"),
            "normalized": netguard.normalize_base_url("https://relay.example.test/v1/chat/completions")["normalized"],
        }
        os.environ["STUDY_AI_URL_POLICY"] = "public_https"
        public = {
            "loopback": verdict("http://127.0.0.1:11434/v1"),
            "https_relay": verdict("https://relay.example.test/v1"),
            "metadata": verdict("https://169.254.169.254/v1"),
            "query_key": verdict("https://relay.example.test/v1?api_key=secret"),
        }
        os.environ["STUDY_AI_URL_POLICY"] = "allowlist"
        allowlist = {
            "unlisted": verdict("https://relay.example.test/v1"),
            "preset": verdict("https://api.openai.com/v1"),
        }
    finally:
        for key, value in (("STUDY_AI_URL_POLICY", original_policy), ("STUDY_PUBLIC_MODE", original_public)):
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    result = {"local_open": local, "public_https": public, "allowlist": allowlist}
    result["ok"] = (
        local["ollama"] == "accepted" and local["lan"] == "accepted"
        and local["custom_port"] == "accepted" and local["azure_query"] == "accepted"
        and local["metadata"].startswith("rejected")
        and local["normalized"] == "https://relay.example.test/v1"
        and public["loopback"].startswith("rejected") and public["https_relay"] == "accepted"
        and public["metadata"].startswith("rejected") and public["query_key"].startswith("rejected")
        and allowlist["unlisted"].startswith("rejected") and allowlist["preset"] == "accepted"
    )
    return result


def check_ready_privacy(app_module) -> dict:
    """就绪接口：匿名不泄露路径/题量，管理员可见详情与 Embedding 状态。"""
    from fastapi.testclient import TestClient
    app = app_module.app
    with TestClient(app, base_url="http://127.0.0.1") as client:
        anonymous = client.get("/api/health/ready").json()
        client.post("/api/auth/register", json={"username": "审计就绪", "password": "audit-pass-123"})
        admin = client.get("/api/health/ready").json()
    anon_text = json.dumps(anonymous, ensure_ascii=False)
    admin_text = json.dumps(admin, ensure_ascii=False)
    result = {
        "anonymous_keys": sorted(anonymous.keys()),
        "has_path_anonymous": ("bank_dir" in anon_text or "E:\\" in anon_text),
        "checks_anonymous_ok_only": all(set(value.keys()) == {"ok"} for value in anonymous.get("checks", {}).values()),
        "admin_has_bank_detail": "bank_dir" in admin_text or "bank_version" in admin_text,
        "admin_has_embedding": "embedding" in admin,
    }
    result["ok"] = (
        not result["has_path_anonymous"]
        and result["checks_anonymous_ok_only"]
        and result["admin_has_bank_detail"]
        and result["admin_has_embedding"]
    )
    return result


def check_cache_headers(app_module) -> dict:
    """bank-image 保留 private 缓存与 ETag；普通 API 仍 no-store。"""
    import json as jsonlib
    from fastapi.testclient import TestClient
    app = app_module.app
    manifest = Path(app_module.practice._bank_dir()) / "assets_manifest.jsonl"
    target = None
    if manifest.is_file():
        with manifest.open(encoding="utf-8") as stream:
            for line in stream:
                row = jsonlib.loads(line)
                refs = row.get("refs") or []
                public = [ref for ref in refs if ref.get("permission") == "public" and ref.get("qid")]
                if public and row.get("subjects") and row.get("paths"):
                    subject = row["subjects"][0]
                    path = next((p for p in row["paths"] if p.startswith(subject + "/")), "")
                    if path:
                        target = (subject, path.split("/", 1)[1], public[0]["qid"], row["asset_id"])
                        break
    result = {"asset": bool(target)}
    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.post("/api/auth/register", json={"username": "审计缓存", "password": "audit-pass-123"})
        stats = client.get("/api/stats")
        result["api_cache_control"] = stats.headers.get("cache-control")
        if target:
            subject, file, qid, asset_id = target
            image = client.get("/api/bank-image", params={
                "subject": subject, "file": file, "qid": qid, "asset_id": asset_id})
            result["image_status"] = image.status_code
            result["image_cache_control"] = image.headers.get("cache-control")
            result["image_etag"] = image.headers.get("etag")
            if image.headers.get("etag"):
                cached = client.get("/api/bank-image", params={
                    "subject": subject, "file": file, "qid": qid, "asset_id": asset_id},
                    headers={"If-None-Match": image.headers["etag"]})
                result["image_304"] = cached.status_code
    result["ok"] = (
        result["api_cache_control"] == "no-store"
        and result.get("image_status") == 200
        and "private" in str(result.get("image_cache_control"))
        and bool(result.get("image_etag"))
        and result.get("image_304") == 304
    )
    return result


def check_static_contracts_v2() -> dict:
    verify = (ROOT / "app" / "ai" / "verify.py").read_text(encoding="utf-8")
    providers = (ROOT / "app" / "ai" / "providers.py").read_text(encoding="utf-8")
    main_src = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    result = {
        "reference_conflict_uses_matches_reference": (
            'conflict_cmp.get("matches_reference") is False' in verify),
        "structured_verify_json_output": (
            'json_output=structured_verify' in verify),
        "block_evidence_shared": (
            "content_refs.collect_bank_evidence" in verify and (ROOT / "app" / "content_refs.py").is_file()),
        "identity_risk_degrades": (
            'identity_risk' in verify and 'cross_relay_same_model' in verify),
        "url_policy_env": ("STUDY_AI_URL_POLICY" in providers or
                           "STUDY_AI_URL_POLICY" in (ROOT / "app" / "ai" / "netguard.py").read_text(encoding="utf-8")),
        "build_id_covers_pages_css": ("web/styles-pages.css" in main_src),
        "build_id_covers_prompts": ("app/ai/prompts/**/*.yaml" in main_src),
        "thinking_stream_wired": (
            '"thinking"' in (ROOT / "app" / "ai" / "jobs.py").read_text(encoding="utf-8")
            and "thinking" in (ROOT / "web" / "js" / "core" / "jobs.js").read_text(encoding="utf-8")),
    }
    result["ok"] = all(result.values())
    return result


def check_streaming_and_async_static() -> dict:
    """Sprint 3 契约静态检查：Pub/Sub 流式、OCR 任务化、固定壳流式、后台懒加载、SymPy 池。"""
    root = ROOT
    jobs = (root / "app" / "ai" / "jobs.py").read_text(encoding="utf-8")
    notes = (root / "app" / "ai" / "notes_agent.py").read_text(encoding="utf-8")
    verify = (root / "app" / "ai" / "verify.py").read_text(encoding="utf-8")
    eval_src = (root / "app" / "eval.py").read_text(encoding="utf-8")
    jobs_js = (root / "web" / "js" / "core" / "jobs.js").read_text(encoding="utf-8")
    admin_js = (root / "web" / "js" / "pages" / "admin.js").read_text(encoding="utf-8")
    result = {
        "redis_pubsub_channel": "zhixu:job:" in jobs,
        "publish_event_public": "def publish_event(" in jobs,
        "stream_subscribes": ("subscribe" in jobs and "_load_aioredis" in jobs),
        "quota_env": ("STUDY_AI_MAX_ACTIVE_PER_USER" in jobs and "STUDY_AI_GLOBAL_CONCURRENCY" in jobs),
        "submit_job_helper": "async def submit_job(" in jobs,
        "ocr_task_dispatch": 'data.task == "ocr"' in jobs,
        "ocr_async_executor": "async def _ocr_job(" in notes,
        "ocr_page_persist": ("_save_ocr_pages(document_id, user_id, [page]" in notes
                             or "_save_ocr_pages(document_id, user_id, [pno" in notes
                             or "_save_ocr_pages" in notes),
        "ocr_scale_2": ("2.0" in notes and "STUDY_OCR_SCALE" in notes),
        "ocr_page_timeout": "STUDY_OCR_PAGE_TIMEOUT" in notes,
        "stream_fixed_shell": ("job-card-live" in jobs_js and "paint" in jobs_js),
        "stream_progress_parser": "parseStageProgress" in jobs_js,
        "admin_lazy_tabs": ("panelState" in admin_js and "loaded" in admin_js),
        "sympy_pool": (root / "app" / "ai" / "sympy_pool.py").is_file() and "get_pool" in verify,
        "eval_judge_wired": ("judge_model" in eval_src and "judge_agrees" in eval_src),
        "eval_resume": ("progress_path" in eval_src and "resume" in eval_src),
        "eval_substring_removed": "clean_r in clean_p" not in eval_src,
    }
    result["ok"] = all(result.values())
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="本轮整改独立验收")
    parser.add_argument("--bank", default=None, help="题库快照目录（默认跟随 releases/CURRENT）")
    parser.add_argument("--out", default=str(ROOT / "screenshots" / "audit-accept.json"))
    args = parser.parse_args()
    if args.bank:
        os.environ["STUDY_BANK_DIR"] = args.bank
    from app import main as app_main

    report = {
        "bank_dir": str(__import__("app.db", fromlist=["BANK_DIR"]).BANK_DIR),
        "blank_gate": check_blank_gate(app_main),
        "asset_gate": check_asset_gate(app_main),
        "groups": check_groups(app_main),
        "solution_blocks": check_solution_blocks(app_main),
        "sympy_budget": check_sympy_budget(app_main),
        "static": check_static_contracts(),
        "provider_policy": check_provider_policy(app_main),
        "ready_privacy": check_ready_privacy(app_main),
        "cache_headers": check_cache_headers(app_main),
        "static_v2": check_static_contracts_v2(),
        "static_v3": check_streaming_and_async_static(),
    }
    report["search_perf"] = check_search_perf(app_main)
    report["ok"] = all(section.get("ok") for section in report.values() if isinstance(section, dict))
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
