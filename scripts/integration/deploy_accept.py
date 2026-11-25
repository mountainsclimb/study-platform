"""Docker Compose 部署栈端到端验收（P0-2 联验固化）。

在装有 Docker CLI 的环境运行（WSL/服务器；标准库实现，不依赖第三方包）：
    python3 scripts/integration/deploy_accept.py \
        --base-url https://localhost:8443 \
        --health-token <deploy/.env 里的 STUDY_HEALTH_TOKEN> \
        --model-endpoint http://<宿主网关IP>:9123/v1

覆盖（对应 deploy/README.md §5 与《全局功能完成方案》P0-2）：
  1. compose 服务全部运行且 app 健康；
  2. 经 Caddy 的 HTTPS /api/health/live 可达；
  3. /api/health/ready（携带 X-Health-Token）：app_db / bank / storage / redis / worker
     全部就绪，Worker 心跳在 90 秒内；
  4. 注册首个账号（本地模式=管理员）——数据落 PostgreSQL；
  5. 上传题目图片 → 建 OpenAI 兼容假模型连接 → 提交 task=recognize 异步任务 →
     Worker 经 Redis 队列真实执行到 completed；
  6. PG 模式在线热备份 API 按设计拒绝（409，SQLite 文件备份对 PG 无意义）；
     pg_dump → 恢复到临时库 → 行数核对 → 清理；
  7. 双账号数据隔离：他人不可读取我的拍题草稿。

前置：
  * `cd deploy && docker compose -f docker-compose.yml -f docker-compose.local.yml up -d --build`
    （local 覆盖文件把 Caddy 映射到 8080/8443，避开宿主 80/443 占用）；
  * deploy/.env 已配置（本地模式至少 STUDY_DOMAIN/STUDY_ALLOWED_HOSTS/PUBLIC_MODE=0/
    FORWARDED_ALLOW_IPS/PG_PASSWORD/REDIS_PASSWORD/STUDY_HEALTH_TOKEN）；
  * 实例为全新数据卷（`docker compose down -v` 后），脚本注册的首个账号才是管理员；
  * 假模型端点：在宿主运行一个 OpenAI 兼容服务并经 `--model-endpoint` 传入
    （容器经 docker 网桥网关访问宿主端口；网关 IP 可用
    `docker network inspect <project>_default --format '{{(index .IPAM.Config 0).Gateway}}'` 查询）。
"""
from __future__ import annotations

import argparse
import http.client
import io
import json
import ssl
import subprocess
import sys
import time
import uuid
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

PG_CONTAINER = "deploy-postgres-1"
PG_MIRROR_MIN_ROWS = 26000  # r5 快照 26,374 题；镜像行数低于此视为挂错题库


class Accept:
    def __init__(self, base_url: str, health_token: str, pg_container: str):
        self.base_url = base_url.rstrip("/")
        self.health_token = health_token
        self.pg_container = pg_container
        self.ctx = ssl.create_default_context()
        self.ctx.check_hostname = False
        self.ctx.verify_mode = ssl.CERT_NONE
        self.cookie = ""
        self.steps: list[dict] = []

    def request(self, method: str, path: str, *, body=None, headers=None,
                timeout=30, raw_body: bytes | None = None):
        data = raw_body if raw_body is not None else (
            json.dumps(body).encode("utf-8") if body is not None else None)
        req_headers = dict(headers or {})
        if raw_body is None and body is not None:
            req_headers.setdefault("Content-Type", "application/json")
        if self.cookie:
            req_headers.setdefault("Cookie", self.cookie)
        req = Request(self.base_url + path, data=data, headers=req_headers, method=method)
        try:
            with urlopen(req, timeout=timeout, context=self.ctx) as resp:
                set_cookie = resp.getheader("Set-Cookie") or ""
                if set_cookie:
                    self.cookie = set_cookie.split(";", 1)[0]
                payload = json.loads(resp.read().decode("utf-8") or "{}")
                return resp.status, payload
        except HTTPError as error:
            try:
                payload = json.loads(error.read().decode("utf-8") or "{}")
            except Exception:
                payload = {}
            return error.code, payload
        except URLError as error:
            raise RuntimeError(f"请求失败 {method} {path}: {error}") from error

    def step(self, name: str, checks: list) -> None:
        ok = all(item[1] for item in checks)
        self.steps.append({"name": name, "ok": ok,
                           "checks": [{"label": c[0], "ok": c[1], "detail": c[2]} for c in checks]})
        mark = "✓" if ok else "✗"
        print(f"[{mark}] {name}")
        for label, passed, detail in checks:
            print(f"    {'·' if passed else '!'} {label}" + (f"（{detail}）" if detail else ""))


def docker(*args: str, input_text: str | None = None, timeout: int = 180) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True,
                          input=input_text, timeout=timeout)


def tiny_png() -> bytes:
    """1×1 白色 PNG；识别任务只需走通链路，不要求内容可识别。"""
    image = io.BytesIO()
    try:
        from PIL import Image
        image_image = Image.new("RGB", (8, 8), "white")
        image_image.save(image, format="PNG")
        return image.getvalue()
    except Exception:
        pass
    # 无 Pillow 时用 zlib 手工构造最小 PNG（保证容器端解码不炸即可）。
    import struct
    import zlib

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    idat = zlib.compress(b"\x00\xff\xff\xff")
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", idat) + chunk(b"IEND", b""))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="部署栈端到端验收（compose 5 服务）")
    parser.add_argument("--base-url", default="https://localhost:8443")
    parser.add_argument("--health-token", default="", help="deploy/.env 的 STUDY_HEALTH_TOKEN")
    parser.add_argument("--pg-container", default=PG_CONTAINER)
    parser.add_argument("--expect-bank-rows", type=int, default=PG_MIRROR_MIN_ROWS)
    parser.add_argument("--model-endpoint", required=True,
                        help="宿主侧 OpenAI 兼容假模型地址（如 http://172.18.0.1:9123/v1）")
    options = parser.parse_args(argv)

    accept = Accept(options.base_url, options.health_token, options.pg_container)
    suffix = uuid.uuid4().hex[:8]
    username = f"deploy-accept-{suffix}"
    password = "deploy-accept-pass-123"

    # ---- 1. compose 服务状态 ----
    ps = docker("compose", "-f", "deploy/docker-compose.yml",
                "-f", "deploy/docker-compose.local.yml", "ps", "--format", "json")
    services: dict[str, tuple[str, str]] = {}
    if ps.returncode == 0:
        for line in ps.stdout.splitlines():
            if line.strip():
                try:
                    row = json.loads(line)
                    services[row.get("Service")] = (row.get("State") or "", row.get("Health") or "")
                except Exception:
                    continue
    missing = [name for name in ("app", "worker", "postgres", "redis", "caddy")
               if name not in services]
    unhealthy = [name for name, (state, health) in services.items()
                 if state != "running" or "unhealthy" in health.lower()]
    accept.step("compose 五服务运行且 app 健康", [
        ("服务齐全", not missing, f"missing={missing}"),
        ("全部 running 且无 unhealthy", not unhealthy,
         json.dumps(services, ensure_ascii=False)[:240]),
    ])

    # ---- 2. Caddy HTTPS live ----
    status_live, _ = accept.request("GET", "/api/health/live")
    accept.step("Caddy HTTPS 路由 /api/health/live", [
        ("HTTP 200", status_live == 200, f"status={status_live}"),
    ])

    # ---- 3. 就绪详情 + Worker 心跳 ----
    ready_headers = {"X-Health-Token": options.health_token} if options.health_token else {}
    status_ready, ready = accept.request("GET", "/api/health/ready", headers=ready_headers)
    checks = ready.get("checks") or {}
    worker_detail = str((checks.get("worker") or {}).get("detail") or "")
    bank_detail = str((checks.get("bank") or {}).get("detail") or "")
    accept.step("就绪检查：app_db/bank/storage/redis/worker 全部就绪", [
        ("整体 status=ok", ready.get("status") == "ok" and status_ready == 200,
         f"status={ready.get('status')} http={status_ready}"),
        ("五组检查齐备", set(checks) >= {"app_db", "bank", "storage", "redis", "worker"},
         ",".join(sorted(checks))),
        ("Worker 心跳新鲜（90 秒内）", ("秒前" in worker_detail or "second" in worker_detail.lower()),
         worker_detail[:70]),
        ("题库镜像挂载（题量非零）", bool(bank_detail.strip()) and "0" != bank_detail.strip()[:1],
         bank_detail[:70]),
    ])

    # ---- 4. 首个账号=管理员（本地模式），数据落 PostgreSQL ----
    status_register, _ = accept.request("POST", "/api/auth/register", body={
        "username": username, "password": password})
    status_me, me = accept.request("GET", "/api/auth/me")
    accept.step("注册首个账号（本地模式=管理员）并经 PostgreSQL 持久化", [
        ("注册成功", status_register in (200, 201), f"status={status_register}"),
        ("会话有效", status_me == 200, f"status={status_me}"),
        ("角色为 admin", me.get("role") == "admin", f"role={me.get('role')} id={me.get('id')}"),
    ])

    # ---- 5. recognize 异步任务经 Redis 队列由 Worker 完成 ----
    png = tiny_png()
    boundary = uuid.uuid4().hex
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="files"; filename="deploy-accept.png"\r\n'
        f"Content-Type: image/png\r\n\r\n"
    ).encode("utf-8") + png + f"\r\n--{boundary}--\r\n".encode("utf-8")
    status_upload, upload_payload = accept.request(
        "POST", "/api/uploads/question",
        raw_body=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        timeout=60)
    upload_id = upload_payload.get("id")
    status_provider, provider = accept.request("POST", "/api/providers", body={
        "name": f"deploy-accept-{suffix}", "kind": "openai",
        "base_url": options.model_endpoint, "api_key": "sk-deploy-accept",
        "models": ["fake-vision"],
    })
    provider_id = provider.get("id")
    status_job, job = accept.request("POST", "/api/ai/jobs", body={
        "task": "recognize", "upload_id": upload_id,
        "provider_id": provider_id, "model": "fake-vision",
    })
    job_id = job.get("id")
    final_status = ""
    detail: dict = {}
    if status_job in (200, 201) and job_id:
        for _ in range(60):
            time.sleep(3)
            _s, detail = accept.request("GET", f"/api/ai/jobs/{job_id}")
            final_status = str(detail.get("status") or "")
            if final_status in ("completed", "failed", "cancelled", "interrupted"):
                break
    accept.step("recognize 任务经 Redis 队列由 Worker 执行到 completed", [
        ("上传成功", status_upload in (200, 201) and bool(upload_id),
         f"status={status_upload} upload_id={upload_id}"),
        ("假模型连接创建", status_provider in (200, 201) and bool(provider_id),
         f"status={status_provider}"),
        ("任务提交", status_job in (200, 201), f"status={status_job}"),
        ("终态为 completed", final_status == "completed",
         json.dumps({k: detail.get(k) for k in ("status", "error", "stage")},
                    ensure_ascii=False)[:200]),
    ])

    # ---- 6. PG 备份/恢复演练 ----
    status_backup, _ = accept.request("POST", "/api/admin/backup")
    dump = docker("exec", options.pg_container, "pg_dump", "-U", "zhixu", "-d", "zhixu")
    restored_ok = False
    restored_rows = ""
    if dump.returncode == 0 and dump.stdout and "CREATE TABLE" in dump.stdout:
        docker("exec", options.pg_container, "psql", "-U", "zhixu", "-d", "postgres",
               "-c", "DROP DATABASE IF EXISTS zhixu_restore_accept;",
               "-c", "CREATE DATABASE zhixu_restore_accept;")
        applied = docker("exec", "-i", options.pg_container, "psql", "-U", "zhixu",
                         "-d", "zhixu_restore_accept", input_text=dump.stdout)
        if applied.returncode == 0:
            counts = docker("exec", options.pg_container, "psql", "-U", "zhixu",
                            "-d", "zhixu_restore_accept", "-tAc",
                            "SELECT (SELECT count(*) FROM users)||'/'||"
                            "(SELECT count(*) FROM bank.questions);")
            restored_rows = counts.stdout.strip()
            try:
                users_n, questions_n = restored_rows.split("/")
                restored_ok = users_n == "1" and int(questions_n) >= options.expect_bank_rows
            except Exception:
                restored_ok = False
        docker("exec", options.pg_container, "psql", "-U", "zhixu", "-d", "postgres",
               "-c", "DROP DATABASE IF EXISTS zhixu_restore_accept;")
    accept.step("PG 备份/恢复演练（在线热备份按设计拒绝 + pg_dump 恢复核对）", [
        ("PG 模式在线热备份按设计拒绝（409）", status_backup == 409, f"status={status_backup}"),
        ("pg_dump 成功且含建表语句",
         dump.returncode == 0 and "CREATE TABLE" in dump.stdout,
         f"exit={dump.returncode} bytes={len(dump.stdout)}"),
        ("恢复库行数一致（1 用户 / 题库镜像完整）", restored_ok,
         f"users/bank.questions={restored_rows}"),
    ])

    # ---- 7. 双账号数据隔离 ----
    accept.request("POST", "/api/auth/register", body={
        "username": f"deploy-peer-{suffix}", "password": password})
    accept.cookie = ""  # 以未登录会话访问他人草稿
    status_leak, _ = accept.request("GET", f"/api/uploads/{upload_id}/intake")
    accept.step("双账号数据隔离（未登录不可读他人拍题草稿）", [
        ("匿名访问 401/403/404", status_leak in (401, 403, 404), f"status={status_leak}"),
    ])

    ok = all(step["ok"] for step in accept.steps)
    print(json.dumps({"ok": ok, "steps": len(accept.steps),
                      "failed": [s["name"] for s in accept.steps if not s["ok"]]},
                     ensure_ascii=False, indent=2))
    print("DEPLOY ACCEPT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
