#!/usr/bin/env python
"""真实录取库冒烟：用已导入的 ``admission.sqlite3`` 启动一个隔离实例并跑通全部志愿接口。

只读校验，不联网、不启动端口：脚本在数据目录旁建临时目录，硬链接录取库（同一卷），
把 ``STUDY_DATA_DIR`` 指向临时目录（应用库、会话都在临时目录里创建），因此不会写入
正式 ``app.sqlite3``、不会污染账号与学习记录。跑完自动清理。

用法::

    cd study-platform
    python scripts/admission_smoke.py            # 代表性省年矩阵 + 延迟统计
    python scripts/admission_smoke.py --sweep    # 额外遍历全部省年的筛选器
    python scripts/admission_smoke.py --json-out smoke.json

退出码 0 表示全部断言通过；缺少录取库或任一断言失败时返回非 0。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
DEFAULT_DATA_DIR = Path(os.environ.get("STUDY_DATA_DIR", str(ROOT / "data"))).resolve()


class SmokeFailure(RuntimeError):
    """冒烟断言失败。"""


class Timer:
    def __init__(self):
        self.records: dict[str, list[float]] = {}

    def measure(self, label: str, duration_ms: float) -> None:
        self.records.setdefault(label, []).append(duration_ms)

    def summary(self) -> list[dict]:
        rows = []
        for label, values in sorted(self.records.items()):
            rows.append({
                "label": label,
                "calls": len(values),
                "p50_ms": round(statistics.median(values), 1),
                "max_ms": round(max(values), 1),
            })
        return rows


def _prepare_isolated_data_dir(data_dir: Path, db_path: Path, keep: bool, allow_copy: bool) -> Path:
    """在数据目录旁建临时目录并硬链接录取库，避免跨卷复制 2.6GB 文件。"""
    temp_dir = data_dir / f".admission-smoke-{os.getpid()}"
    if temp_dir.exists():
        shutil.rmtree(temp_dir, ignore_errors=True)
    temp_dir.mkdir(parents=True)
    target = temp_dir / db_path.name
    try:
        os.link(db_path, target)
        kind = "硬链接"
    except OSError:
        if not allow_copy:
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise SmokeFailure(
                f"无法在 {temp_dir} 与 {db_path} 之间建立硬链接（可能不在同一磁盘卷）。"
                "请用 --allow-copy 允许复制（约 2.6GB，耗时较久），或把数据目录与临时目录放在同一卷。"
            ) from None
        print(f"[提示] 硬链接失败，改为复制录取库（约 {db_path.stat().st_size / 1073741824:.1f}GB）…")
        shutil.copyfile(db_path, target)
        kind = "复制"
    print(f"隔离数据目录: {temp_dir}（录取库：{kind}，{target.stat().st_size / 1073741824:.2f}GB）")
    official_source = data_dir / "admission-official.sqlite3"
    if official_source.is_file():
        official_target = temp_dir / official_source.name
        try:
            os.link(official_source, official_target)
            print(f"官方补采批次线库：硬链接 {official_target.stat().st_size / 1048576:.1f}MB")
        except OSError:
            shutil.copyfile(official_source, official_target)
            print("官方补采批次线库：复制（无法硬链接）")
    gaps_source = data_dir / "admission-gaps.json"
    if gaps_source.is_file():
        shutil.copyfile(gaps_source, temp_dir / gaps_source.name)
        print("数据缺失声明：已复制 admission-gaps.json")
    if keep:
        print("[提示] --keep 已开启，跑完后保留该目录方便排查。")
    return temp_dir


def _cleanup(temp_dir: Path, keep: bool) -> None:
    if keep:
        return
    shutil.rmtree(temp_dir, ignore_errors=True)


def _timed(client, timer: Timer, label: str, method: str, path: str, **kwargs):
    started = time.perf_counter()
    response = client.request(method, path, **kwargs)
    duration = (time.perf_counter() - started) * 1000
    timer.measure(label, duration)
    return response


def _expect(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeFailure(message)


def run_smoke(args) -> dict:
    data_dir = Path(args.data_dir).resolve()
    db_path = Path(args.db).resolve() if args.db else data_dir / "admission.sqlite3"
    if not db_path.is_file():
        raise SmokeFailure(
            f"未找到录取库：{db_path}\n"
            "请先运行 `python scripts/import_admission.py` 导入数据后再执行本冒烟。"
        )

    temp_dir = _prepare_isolated_data_dir(data_dir, db_path, args.keep, args.allow_copy)
    os.environ["STUDY_DATA_DIR"] = str(temp_dir)
    os.environ["STUDY_ALLOWED_HOSTS"] = "127.0.0.1,localhost,[::1],testserver"
    os.environ.setdefault("STUDY_INDEX_PREWARM", "0")

    timer = Timer()
    failures: list[str] = []
    checks = 0

    def check(condition: bool, message: str) -> None:
        nonlocal checks
        checks += 1
        if not condition:
            failures.append(message)

    started_all = time.perf_counter()
    try:
        # 环境变量必须在导入 app 之前设置：db.DATA_DIR 在模块导入时解析。
        from fastapi.testclient import TestClient  # noqa: PLC0415

        from app.main import app  # noqa: PLC0415

        with TestClient(app) as client:
            username = f"admission_smoke_{os.getpid()}"
            response = client.post("/api/auth/register", json={
                "username": username, "password": "admission-smoke-pass-123",
            })
            check(response.status_code == 200, f"注册隔离账号失败：{response.status_code} {response.text[:200]}")

            # 1) 元信息：年份、省份、覆盖率与行数必须与真实导入一致。
            started = time.perf_counter()
            meta_response = client.get("/api/admission/meta")
            timer.measure("meta", (time.perf_counter() - started) * 1000)
            check(meta_response.status_code == 200, f"/meta 状态码 {meta_response.status_code}")
            meta = meta_response.json()
            check(meta.get("ready") is True, "/meta ready 不为 true")
            years = meta.get("years") or []
            check(years == list(range(2017, 2026)), f"年份应为 2017–2025，实际 {years}")
            provinces = meta.get("provinces") or []
            check(len(provinces) == 31, f"省份数量应为 31，实际 {len(provinces)}")
            coverage = meta.get("coverage") or []
            check(len(coverage) == 232, f"覆盖省年应为 232，实际 {len(coverage)}")
            rows = meta.get("rows") or {}
            for table in ("score-range", "school-admission", "major-admission", "enrollment-plan"):
                check(int(rows.get(table) or 0) > 0, f"表 {table} 行数为 0")
            check(int(meta.get("total_rows") or 0) > 10_000_000,
                  f"总行数异常：{meta.get('total_rows')}")
            check("MIT" in str(meta.get("license") or ""), "元信息缺少 MIT 许可声明")
            print(f"数据集: {meta.get('dataset')}  导入时间: {meta.get('imported_at')}  "
                  f"总行数: {int(meta.get('total_rows') or 0):,}")

            # 官方补采批次线（可选）：存在时验证 2020/2021 补齐与来源标注。
            official = meta.get("official") or {}
            if official.get("available"):
                check(official.get("rows", 0) > 0, "官方补采库行数为 0")
                check(2021 in (official.get("years") or []), "官方补采库未覆盖 2021 年")
                lines = client.get("/api/admission/control-lines", params={
                    "province": "anhui", "year": 2021, "category": "理科",
                }).json()
                check(len(lines.get("items") or []) >= 3,
                      f"安徽 2021 理科官方批次线不足 3 条：{len(lines.get('items') or [])}")
                scores = {item["batch"]: item["score"] for item in lines.get("items") or []}
                check(scores.get("本科一批") == 488,
                      f"安徽 2021 理科本科一批应为 488，实际 {scores.get('本科一批')}")
                fallback = client.get("/api/admission/score-rank", params={
                    "province": "anhui", "year": 2021, "category": "理科", "score": 600,
                }).json()
                check(fallback.get("control_score") == 488,
                      f"安徽 2021 理科 600 分批次线应补齐为 488，实际 {fallback.get('control_score')}")
                check(fallback.get("control_source") == "official-inferred",
                      f"补齐来源标记应为 official-inferred，实际 {fallback.get('control_source')}")
                check(bool(fallback.get("official_batches")), "score-rank 未返回官方批次线明细")
                filters = client.get("/api/admission/filters", params={
                    "province": "anhui", "year": 2021,
                }).json()
                check(filters.get("official_control_supported") is True,
                      "安徽 2021 filters 未标记官方批次线可用")
            else:
                print("[提示] 未发现官方补采批次线库，跳过补齐相关断言。")

            # 数据缺失声明（可选）：存在时校验逐省年缺口与接口。
            gaps = client.get("/api/admission/gaps").json()
            if gaps.get("available"):
                check(gaps.get("summary", {}).get("province_years") == 232,
                      "缺失声明的省年数应为 232")
                check(gaps.get("summary", {}).get("rank", {}).get("missing", 0) > 0,
                      "缺失声明未统计到位次缺失")
                check(len(gaps.get("province_years") or []) == 232,
                      "缺失声明逐省年明细条数不足")
            else:
                print("[提示] 未发现 admission-gaps.json，跳过缺失声明断言。")

            # 2) 代表性省年的筛选器与完整度标记（含已知缺失场景）。
            representative = [
                ("zhejiang", 2024, {"rank_supported": True, "category_available": True,
                                    "plan_supported": True, "control_score_supported": True}),
                ("henan", 2024, {"rank_supported": True, "category_available": False,
                                 "plan_supported": False}),
                ("anhui", 2017, {"rank_supported": False, "control_score_supported": False}),
                ("beijing", 2025, {"rank_supported": True}),
            ]
            for province, year, expected in representative:
                response = _timed(client, timer, "filters", "GET", "/api/admission/filters",
                                  params={"province": province, "year": year})
                check(response.status_code == 200, f"filters {province}/{year} 状态码 {response.status_code}")
                if response.status_code != 200:
                    continue
                payload = response.json()
                for key, value in expected.items():
                    check(payload.get(key) is value,
                          f"filters {province}/{year} 的 {key} 应为 {value}，实际 {payload.get(key)}")

            # 3) 一分一段：浙江 2024 综合 600 分 → 53,964 位；安徽 2017 理科 600 分 → 5,388 位。
            response = _timed(client, timer, "score-rank", "GET", "/api/admission/score-rank",
                              params={"province": "zhejiang", "year": 2024, "category": "综合", "score": 600})
            check(response.status_code == 200, f"score-rank 状态码 {response.status_code}")
            if response.status_code == 200:
                payload = response.json()
                check(payload.get("cumulative_count") == 53964,
                      f"浙江 2024 综合 600 分位次应为 53964，实际 {payload.get('cumulative_count')}")
                check(payload.get("exact") is True, "浙江 600 分应为精确匹配")
                check(len(payload.get("distribution") or []) >= 10, "一分一段分布点过少")
            response = client.get("/api/admission/score-rank", params={
                "province": "anhui", "year": 2017, "category": "理科", "score": 600,
            })
            check(response.status_code == 200, f"安徽 2017 score-rank 状态码 {response.status_code}")
            if response.status_code == 200:
                check(response.json().get("cumulative_count") == 5388,
                      f"安徽 2017 理科 600 分位次应为 5388，实际 {response.json().get('cumulative_count')}")

            # 4) 找学校：分数 / 位次 / 关键词三种模式。
            response = _timed(client, timer, "schools", "GET", "/api/admission/schools", params={
                "province": "zhejiang", "year": 2024, "category": "综合",
                "score": 600, "band": 15, "page_size": 20,
            })
            check(response.status_code == 200, f"schools(score) 状态码 {response.status_code}")
            if response.status_code == 200:
                payload = response.json()
                check(payload.get("mode") == "score", f"schools 模式应为 score，实际 {payload.get('mode')}")
                check(payload.get("total") == 277, f"浙江 2024 600±15 应有 277 所，实际 {payload.get('total')}")
                items = payload.get("items") or []
                check(len(items) == 20, f"首页应返回 20 条，实际 {len(items)}")
                levels = {item.get("level") for item in items}
                check(levels <= {"冲", "稳", "保"}, f"出现未知冲稳保标记：{levels}")
                check(any(item.get("delta") is not None for item in items), "分数模式缺少分差字段")

            response = client.get("/api/admission/schools", params={
                "province": "henan", "year": 2024, "rank": 50000, "page_size": 10,
            })
            check(response.status_code == 200, f"schools(rank) 状态码 {response.status_code}")
            if response.status_code == 200:
                payload = response.json()
                check(payload.get("mode") == "rank", "位次模式 mode 应为 rank")
                check(payload.get("rank_supported") is True, "河南 2024 应支持位次匹配")
                check(payload.get("total") == 593, f"河南 2024 位次 50000 应有 593 所，实际 {payload.get('total')}")

            response = client.get("/api/admission/schools", params={
                "province": "zhejiang", "year": 2024, "keyword": "浙江大学", "page_size": 10,
            })
            check(response.status_code == 200, f"schools(keyword) 状态码 {response.status_code}")
            if response.status_code == 200:
                names = [item.get("university_name") for item in response.json().get("items") or []]
                check(sorted(names) == sorted(["浙江大学", "浙江大学医学院"]),
                      f"关键词检索结果异常：{names}")

            # 5) 院校详情：投档线 / 专业线 / 招生计划 / 趋势。
            response = _timed(client, timer, "school", "GET", "/api/admission/school", params={
                "province": "zhejiang", "year": 2024, "name": "浙江大学",
            })
            check(response.status_code == 200, f"school 状态码 {response.status_code}")
            if response.status_code == 200:
                payload = response.json()
                check(len(payload.get("cutoffs") or []) == 4, "浙江大学投档线记录应为 4 条")
                check(len(payload.get("majors") or []) >= 20, "浙江大学专业录取记录过少")
                check(len(payload.get("plans") or []) >= 20, "浙江大学招生计划记录过少")
                check(len(payload.get("trend") or []) >= 3, "浙江大学历年趋势点过少")
                check(payload.get("university", {}).get("is_985") == 1, "浙江大学 985 标记缺失")

            # 6) 招生计划与专业检索（含缺失科类的省份）。
            response = _timed(client, timer, "plans", "GET", "/api/admission/plans", params={
                "province": "zhejiang", "year": 2024, "keyword": "计算机", "page_size": 20,
            })
            check(response.status_code == 200, f"plans 状态码 {response.status_code}")
            if response.status_code == 200:
                payload = response.json()
                check(payload.get("total") == 767, f"浙江 2024 计算机类计划应为 767，实际 {payload.get('total')}")
                check(payload.get("total_people") == 9937,
                      f"浙江 2024 计算机类计划人数应为 9937，实际 {payload.get('total_people')}")

            response = _timed(client, timer, "majors", "GET", "/api/admission/majors", params={
                "province": "henan", "year": 2024, "keyword": "临床医学", "page_size": 20,
            })
            check(response.status_code == 200, f"majors 状态码 {response.status_code}")
            if response.status_code == 200:
                payload = response.json()
                check(payload.get("total") == 390, f"河南 2024 临床医学应为 390 条，实际 {payload.get('total')}")
                check(payload.get("category_filtered") is False,
                      "河南 2024 科类整列为空时应标记 category_filtered=false")

            # 7) 联想与趋势。
            response = _timed(client, timer, "suggest", "GET", "/api/admission/suggest", params={"q": "浙江大学"})
            check(response.status_code == 200, f"suggest 状态码 {response.status_code}")
            if response.status_code == 200:
                check(bool(response.json().get("universities")), "院校联想结果为空")
            response = client.get("/api/admission/trend", params={
                "province": "zhejiang", "university": "浙江大学",
            })
            check(response.status_code == 200, f"trend 状态码 {response.status_code}")
            if response.status_code == 200:
                check(len(response.json().get("items") or []) >= 3, "浙江大学趋势点过少")

            # 8) 未登录必须 401。
            anonymous = TestClient(app)
            try:
                response = anonymous.get("/api/admission/meta")
                check(response.status_code == 401, f"未登录访问 /meta 应为 401，实际 {response.status_code}")
            finally:
                anonymous.close()

            # 9) 可选：遍历全部省年筛选器，确认没有 500。
            if args.sweep:
                sweep_failures = 0
                for entry in meta.get("coverage") or []:
                    province, year = entry.get("province"), entry.get("year")
                    response = client.get("/api/admission/filters",
                                          params={"province": province, "year": year})
                    if response.status_code != 200:
                        sweep_failures += 1
                        failures.append(f"filters {province}/{year} 状态码 {response.status_code}")
                check(sweep_failures == 0, f"全量筛选器遍历有 {sweep_failures} 个省年失败")
                print(f"全量筛选器遍历：{len(meta.get('coverage') or [])} 个省年")
    finally:
        _cleanup(temp_dir, args.keep)

    elapsed = time.perf_counter() - started_all
    report = {
        "ok": not failures,
        "checks": checks,
        "failures": failures,
        "elapsed_seconds": round(elapsed, 1),
        "latency": timer.summary(),
    }
    print(f"\n断言 {checks} 项，失败 {len(failures)} 项，耗时 {elapsed:.1f}s")
    print(f"{'接口':<12}{'调用':>6}{'p50(ms)':>10}{'max(ms)':>10}")
    for row in report["latency"]:
        print(f"{row['label']:<12}{row['calls']:>6}{row['p50_ms']:>10}{row['max_ms']:>10}")
    if failures:
        print("\n失败项：")
        for failure in failures:
            print(" -", failure)
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"报告已写入 {args.json_out}")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="真实录取库（2.6GB）接口冒烟")
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR),
                        help="数据目录（默认取 STUDY_DATA_DIR 或 ./data）")
    parser.add_argument("--db", default="", help="admission.sqlite3 路径（默认 <data-dir>/admission.sqlite3）")
    parser.add_argument("--sweep", action="store_true", help="额外遍历全部 232 个省年的筛选器")
    parser.add_argument("--keep", action="store_true", help="保留隔离数据目录，便于排查")
    parser.add_argument("--allow-copy", action="store_true", help="无法硬链接时允许复制录取库")
    parser.add_argument("--json-out", default="", help="把结果写入 JSON 文件")
    args = parser.parse_args(argv)
    try:
        report = run_smoke(args)
    except SmokeFailure as error:
        print(f"冒烟失败：{error}", file=sys.stderr)
        return 1
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
