"""题目素材缺失降级的静态契约（P1-1）。

背景：`[公式N]` / `[图N]` 在题库缺图、精选图未公开、个人题裁图失败时都会命中占位
分支。原实现只输出 `<span class="muted">题目公式</span>`（官方题页）或
`image-error` 小字，用户分不清「题干就是这么写的」还是「本该有张图但丢了」。
现在统一到 `missingMediaNode(role, part)`：虚线框 + 图标 + 明确文案，四个视图
（题目详情、原卷文档、作答页、内容块）共用同一实现，避免又出现两处显示不一致。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"


def _read(relative: str) -> str:
    return (WEB / relative).read_text(encoding="utf-8")


def _locale(name: str) -> dict:
    return json.loads((WEB / "locales" / name).read_text(encoding="utf-8"))


def test_missing_media_node_is_exported_and_uses_visible_styles():
    dom = _read("js/core/dom.js")
    assert "export function missingMediaNode(" in dom
    # 占位必须带独立的类名与角色标记，样式与测试都依赖它们。
    assert "media-missing" in dom
    assert "data-missing-role" in dom
    # 公式与配图分别取不同文案，不能共用「图片信息缺失」。
    assert "t('question.formulaMissing')" in dom
    assert "t('question.figureMissing')" in dom


def test_all_four_views_use_the_shared_placeholder():
    sources = {
        "js/pages/question.js": "官方题页",
        "js/components/paperDocument.js": "原卷文档",
        "js/components/paperAttempt.js": "作答页",
        "js/core/dom.js": "内容块与题干",
    }
    for relative in sources:
        source = _read(relative)
        assert "missingMediaNode" in source, relative
        # 旧实现：官方题页输出裸 muted 文案，其余输出 image-error 小字。
        assert "'question.sourceFormula' : 'question.sourceFigure'" not in source, relative
    dom = _read("js/core/dom.js")
    # 内容块（图片块/公式块）也必须走统一占位。
    assert dom.count("missingMediaNode('figure')") >= 1
    assert dom.count("missingMediaNode('formula')") >= 1
    # 题干占位分支不能再退化成裸 image-error。
    assert "t('question.partMissing', { part })" in dom


def test_missing_media_styles_cover_light_and_dark_theme():
    styles = (WEB / "styles.css").read_text(encoding="utf-8")
    assert ".media-missing{" in styles
    assert "html[data-theme=\"dark\"] .media-missing{" in styles
    # 虚线框是「此位置应有素材」的核心视觉信号。
    assert "border:1px dashed" in styles.split(".media-missing{", 1)[1].split("}", 1)[0]


def test_figure_missing_locale_keys_exist_in_both_languages():
    for name in ("zh-CN.json", "en-US.json"):
        data = _locale(name)
        assert data.get("question.figureMissing"), name
        assert data.get("question.formulaMissing"), name
