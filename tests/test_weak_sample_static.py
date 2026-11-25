"""P1-2：薄弱知识点样本门槛的静态契约（防回退）。

门槛规则（与 ``app/insight.py`` 顶部 CALIBER 一致）：
* 已判定作答 < ``MIN_WEAK_SAMPLE``（3）次 → 不标「薄弱」，只标 ``sample_limited``；
* 样本达标且正确率 < 70% → 标「薄弱」；
* 学情页把「样本不足但正确率偏低」的知识点**单独成组**，不能混进「其他」里消失。

这里用源码/接口双重断言，不需要浏览器。数值行为由
``tests/test_insight.py``、``tests/test_insight_kp_graph.py``、
``tests/test_mastery_path.py``、``tests/test_home_stats.py`` 覆盖。
"""
from __future__ import annotations

from pathlib import Path

from app import home_stats, insight, mastery_path

ROOT = Path(__file__).resolve().parents[1]


def test_threshold_constants_stay_in_sync():
    """三处门槛必须同值：insight 判薄弱、home_stats 报样本、mastery_path 自动选点。"""
    assert insight.MIN_WEAK_SAMPLE == 3
    assert home_stats.MIN_WEAK_SAMPLE == insight.MIN_WEAK_SAMPLE
    assert mastery_path.MIN_WEAK_SAMPLE == insight.MIN_WEAK_SAMPLE


def test_weak_flag_requires_sample_threshold():
    """``weak`` 必须同时要求样本达标与正确率偏低（错一题不标红）。"""
    source = (ROOT / "app" / "insight.py").read_text(encoding="utf-8")
    assert '"weak": bucket["graded"] >= MIN_WEAK_SAMPLE and accuracy < WEAK_ACCURACY' in source
    # 样本不足要显式标注，前端才能给出「仅展示真实样本数」的降级提示。
    assert '"sample_limited": bucket["graded"] < MIN_WEAK_SAMPLE' in source
    # 薄弱徽标、知识图谱节点、报告瘦身副本都要带上该字段。
    assert source.count('"sample_limited"') >= 3


def test_mastery_path_auto_select_skips_small_samples():
    source = (ROOT / "app" / "mastery_path.py").read_text(encoding="utf-8")
    assert "if graded < MIN_WEAK_SAMPLE:" in source
    assert "MIN_WEAK_SAMPLE" in source.split("__all__")[-1] or '"MIN_WEAK_SAMPLE"' in source


def test_insight_page_has_limited_group():
    """学情页必须有独立的「样本不足」分组，且不在该组打薄弱徽标。"""
    source = (ROOT / "web" / "js" / "pages" / "insight.js").read_text(encoding="utf-8")
    assert "insight.kp.groupLimited" in source
    assert "item.sample_limited && !item.weak" in source
    # 样本不足组不能被排除出「已展示」集合，否则会掉进「其他」。
    assert "shown = new Set([...weak, ...limited, ...strong]" in source


def test_locales_have_limited_group_label():
    import json

    for name in ("zh-CN.json", "en-US.json"):
        data = json.loads((ROOT / "web" / "locales" / name).read_text(encoding="utf-8"))
        assert data.get("insight.kp.groupLimited"), f"{name} 缺少 insight.kp.groupLimited"
        assert data.get("insight.kp.sampleLimited"), f"{name} 缺少 insight.kp.sampleLimited"


def test_caliber_documents_sample_threshold():
    """口径说明必须写明样本门槛，避免前端/文档各自解释。"""
    assert "MIN_WEAK_SAMPLE" in insight.CALIBER["weak"] or "3 次" in insight.CALIBER["weak"]
    assert "sample_limited" in insight.CALIBER["weak"]
