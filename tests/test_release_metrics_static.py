"""发布指标前端契约（静态检查）。

管理页「发布指标」卡片只做呈现，口径文案来自后端 ``caliber``。这里守住三件事，
避免以后改前端时把口径重新硬编码或悄悄漏掉某条通道：

* 卡片挂在运维分区（``loadOps`` 里 ``releaseMetricsCard()`` 必须出现）；
* 比率显示走 ``releaseRateText``，``null`` 显示「—」而不是 0%；
* 图片/文本两条通道与 zh/en 两语言的 locale 键都存在。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ADMIN_JS = ROOT / "web" / "js" / "pages" / "admin.js"
LOCALES = ROOT / "web" / "locales"

REQUIRED_KEYS = (
    "admin.releaseMetrics.section",
    "admin.releaseMetrics.badge",
    "admin.releaseMetrics.refresh",
    "admin.releaseMetrics.loading",
    "admin.releaseMetrics.unavailable",
    "admin.releaseMetrics.uploadRate",
    "admin.releaseMetrics.uploadCounts",
    "admin.releaseMetrics.reviewRate",
    "admin.releaseMetrics.reviewCounts",
    "admin.releaseMetrics.uploadTitle",
    "admin.releaseMetrics.uploadEmpty",
    "admin.releaseMetrics.headChannel",
    "admin.releaseMetrics.headSaved",
    "admin.releaseMetrics.headRate",
    "admin.releaseMetrics.headStalled",
    "admin.releaseMetrics.channel.image",
    "admin.releaseMetrics.channel.text",
    "admin.releaseMetrics.countPair",
    "admin.releaseMetrics.stalledCount",
    "admin.releaseMetrics.noChannelData",
    "admin.releaseMetrics.reviewTitle",
    "admin.releaseMetrics.reviewEligible",
    "admin.releaseMetrics.reviewScheduled",
    "admin.releaseMetrics.caliber",
    "admin.releaseMetrics.caliberUnknown",
)


def _admin_js() -> str:
    return ADMIN_JS.read_text(encoding="utf-8")


def _locale(name: str) -> dict:
    return json.loads((LOCALES / name).read_text(encoding="utf-8"))


def test_release_metrics_card_is_mounted_in_ops_section():
    source = _admin_js()
    assert "function releaseMetricsCard()" in source
    assert "releaseMetricsCard()," in source
    # 挂在运维分区的卡片列表里（与 AI 用量/志愿数据同批渲染）。
    # 用 `opsCard,` 定位运维分区那一次 replaceChildren，避免命中前面别的分区。
    start = source.index("      opsCard,")
    window = source[start:start + 400]
    assert "releaseMetricsCard()" in window, window
    # 紧随 AI 用量之后、志愿数据之前，顺序稳定便于人工核对
    assert window.index("adminAiUsageCard()") < window.index("releaseMetricsCard()")
    assert window.index("releaseMetricsCard()") < window.index("admissionCard(),")


def test_release_metrics_card_calls_admin_endpoint():
    source = _admin_js()
    assert "/admin/release-metrics?days=30" in source
    # 口径必须来自后端 caliber，不得在前端重新写一段解释
    assert "data.caliber" in source
    assert "caliber.upload" in source


def test_rate_null_renders_dash_not_zero():
    source = _admin_js()
    body = source[source.index("function releaseRateText("):]
    body = body[: body.index("\n}")]
    assert "return '—'" in body
    # 不能把 null 兜底成 0
    assert "?? 0" not in body
    assert "|| 0" not in body


def test_section_title_receives_days_placeholder():
    """标题含 ``{days}``，调用时必须传参，否则页面直接显示字面量「近 {days} 天」。"""
    source = _admin_js()
    assert "t('admin.releaseMetrics.section', { days })" in source
    zh = _locale("zh-CN.json")
    assert "{days}" in zh["admin.releaseMetrics.section"]


def test_release_metrics_channel_order_and_columns():
    source = _admin_js()
    assert "const order = ['image', 'text'];" in source
    assert "releaseChannelLabel(key)" in source
    for column in ("headChannel", "headSaved", "headRate", "headStalled"):
        assert "admin.releaseMetrics.%s" % column in source


def test_release_metrics_locale_keys_present_in_both_languages():
    for name in ("zh-CN.json", "en-US.json"):
        data = _locale(name)
        missing = [key for key in REQUIRED_KEYS if key not in data]
        assert not missing, f"{name} 缺少：{missing}"


def test_release_metrics_locale_placeholders_match():
    """两个语言的占位符必须一致，否则运行时会出现未替换的 ``{x}``。"""
    pattern = re.compile(r"\{(\w+)\}")
    zh = _locale("zh-CN.json")
    en = _locale("en-US.json")
    mismatched = {
        key: (sorted(pattern.findall(zh[key])), sorted(pattern.findall(en[key])))
        for key in REQUIRED_KEYS
        if sorted(pattern.findall(zh[key])) != sorted(pattern.findall(en[key]))
    }
    assert not mismatched, mismatched
