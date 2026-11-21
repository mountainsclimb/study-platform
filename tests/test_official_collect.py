"""官方渠道采集器解析测试：不联网，用内联 HTML 验证批次线解析与去重。"""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_collector():
    spec = importlib.util.spec_from_file_location(
        "collect_official_data_for_tests", ROOT / "scripts" / "collect_official_data.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_parse_batch_lines_multi_category_and_years():
    collector = _load_collector()
    html = """
    <div class="city">安徽</div>
    <div class="sline clearfix">
      <div class="year act mtf_0">2021年</div>
      <div class="year">2020年</div>
    </div>
    <table>
      <tr><td>批次</td><td>文科</td><td>理科</td></tr>
      <tr><td>本科一批</td><td>560</td><td>488</td></tr>
      <tr><td>本科二批</td><td>519</td><td>415</td></tr>
    </table>
    <table>
      <tr><td>批次</td><td>文科</td><td>理科</td></tr>
      <tr><td>本科一批</td><td>541</td><td>515</td></tr>
    </table>
    """
    records, warnings = collector.parse_batch_lines(html, "https://example.org/eol", "2026-11-20T00:00:00Z", "sha")
    assert warnings == []
    index = {(item["province_slug"], item["year"], item["category"], item["batch"]): item["score"]
             for item in records}
    assert index[("anhui", 2021, "文科", "本科一批")] == 560
    assert index[("anhui", 2021, "理科", "本科一批")] == 488
    assert index[("anhui", 2021, "理科", "本科二批")] == 415
    assert index[("anhui", 2020, "理科", "本科一批")] == 515
    assert all(item["source_url"] == "https://example.org/eol" for item in records)
    assert all(item["sha256"] == "sha" for item in records)


def test_parse_batch_lines_single_category_and_dedup():
    collector = _load_collector()
    html = """
    <div class="city">北京</div>
    <div class="year act">2021年</div>
    <table>
      <tr><td>批次</td><td>分数线</td></tr>
      <tr><td>普通本科</td><td>400</td></tr>
      <tr><td>特殊类型招生</td><td>513</td></tr>
      <tr><td>艺术体育类</td><td><a href="#">查看</a></td></tr>
    </table>
    <table>
      <tr><td>批次</td><td>分数线</td></tr>
      <tr><td>普通本科</td><td>400</td></tr>
    </table>
    """
    records, _ = collector.parse_batch_lines(html, "https://example.org/eol", "t", "sha")
    index = {(item["year"], item["batch"]): item["score"] for item in records}
    assert index[(2021, "普通本科")] == 400
    assert index[(2021, "特殊类型招生")] == 513
    # 年份标签少于表格时，多出的表格按更早年份补齐（eol 实际为 2021–2026 标签 + 2020 年表）。
    assert index[(2020, "普通本科")] == 400
    # 无分数（“查看”）的行被跳过。
    assert all(item["score"] >= 0 for item in records)
    assert len([item for item in records if item["batch"] == "普通本科"]) == 2
