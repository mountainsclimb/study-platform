"""阶段 5 前端静态契约：引擎总览、学习事件上报与学情展示。"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_settings_page_wires_engine_overview_and_admin_reset():
    source = _read(WEB / "js" / "pages" / "settings.js")
    assert "settings.engines.title" in source
    assert "api('/engines')" in source
    assert "/reset" in source and "can_reset" in source
    assert "loadEngines" in source
    assert "settings.engines.adminOnly" in source


def test_usage_reporter_only_sends_whitelisted_fields():
    source = _read(WEB / "js" / "core" / "usage.js")
    assert "/api/evidence/feature-event" in source
    assert "feature" in source and "duration_ms" in source
    for forbidden in ("text:", "content:", "audio:", "transcript:"):
        assert forbidden not in source, forbidden
    assert "keepalive: true" in source


def test_usage_reporter_is_wired_into_three_feature_groups():
    for relative in (
        "js/components/demoCreateMenu.js",
        "js/components/chemBalancer.js",
        "js/components/formulaEditor.js",
        "js/components/readAloud.js",
        "js/components/shadowingCheck.js",
        "js/components/dictationPractice.js",
    ):
        source = _read(WEB / relative)
        assert "reportFeatureUse" in source, relative
    assert "reportFeatureUse('demo'" in _read(WEB / "js/components/demoCreateMenu.js")
    assert "reportFeatureUse('formula'" in _read(WEB / "js/components/chemBalancer.js")
    assert "reportFeatureUse('speech'" in _read(WEB / "js/components/readAloud.js")


def test_insight_page_renders_feature_usage_card():
    source = _read(WEB / "js" / "pages" / "insight.js")
    assert "feature_usage" in source
    assert "insight.feature.title" in source
    assert "insight.feature.line" in source


def test_phase5_locale_keys_complete_and_placeholders_match():
    zh = json.loads(_read(WEB / "locales" / "zh-CN.json"))
    en = json.loads(_read(WEB / "locales" / "en-US.json"))
    prefixes = ("settings.engines.", "insight.feature.")
    keys = sorted(key for key in zh if key.startswith(prefixes))
    assert keys
    assert keys == sorted(key for key in en if key.startswith(prefixes))
    placeholder = re.compile(r"\{(\w+)\}")
    for key in keys:
        assert placeholder.findall(str(zh[key])) == placeholder.findall(str(en[key])), key

    sources = [
        _read(WEB / "js" / "pages" / "settings.js"),
        _read(WEB / "js" / "pages" / "insight.js"),
    ]
    used = set()
    for source in sources:
        used.update(re.findall(r"t\('((?:settings\.engines|insight\.feature)\.[^']+)'", source))
    assert used
    assert used <= set(zh), sorted(used - set(zh))
