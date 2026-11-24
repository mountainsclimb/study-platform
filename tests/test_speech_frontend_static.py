"""通用朗读/跟读前端接线的静态契约（阶段 4-2 / 4-3）。"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
READ = WEB / "js" / "components" / "readAloud.js"
SHADOW = WEB / "js" / "components" / "shadowingCheck.js"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_read_aloud_component_uses_generic_api_and_browser_fallback():
    source = _read(READ)
    assert "export function createReadAloudButton" in source
    assert "export function stopAllReadAloud" in source
    assert "export function plainTextForSpeech" in source
    assert "api/speech/tts" in source
    assert "speechSynthesis" in source
    assert "SpeechSynthesisUtterance" in source
    assert "SEGMENT_CHARS" in source
    assert "hashchange" in source
    # 不在模块加载时自动播放
    assert "new Audio(" in source
    assert "playBlob" in source


def test_shadowing_component_posts_expected_and_file():
    source = _read(SHADOW)
    assert "export function createShadowingPanel" in source
    assert "api/speech/shadowing/check" in source
    assert "form.append('expected', expected)" in source
    assert "form.append('file', wav, 'shadowing.wav')" in source
    assert "blobToWav16k" in source
    assert "不代表发音质量" not in source  # 文案走 locale，不在组件里硬编码
    assert "speech.shadow.note" in source


def test_question_note_and_vocab_pages_wire_read_and_shadow():
    question = _read(WEB / "js" / "pages" / "question.js")
    assert "createReadAloudButton" in question
    assert "createShadowingPanel" in question
    assert "questionSpeechText" in question
    assert "speech.readQuestion" in question
    assert "speech.readAnswer" in question
    assert "speech.readSolution" in question
    assert "stopAllReadAloud" in question

    notes = _read(WEB / "js" / "pages" / "notes.js")
    assert "createReadAloudButton" in notes
    assert "speech.readNote" in notes

    vocab = _read(WEB / "js" / "pages" / "vocab.js")
    assert "createReadAloudButton" in vocab
    assert "createShadowingPanel" in vocab
    assert "speech.readWord" in vocab


def test_speech_locale_keys_complete_and_placeholders_match():
    zh = json.loads(_read(WEB / "locales" / "zh-CN.json"))
    en = json.loads(_read(WEB / "locales" / "en-US.json"))
    keys = sorted(key for key in zh if key.startswith("speech."))
    assert keys
    assert keys == sorted(key for key in en if key.startswith("speech."))
    placeholder = re.compile(r"\{(\w+)\}")
    for key in keys:
        assert placeholder.findall(str(zh[key])) == placeholder.findall(str(en[key])), key

    sources = [
        _read(READ), _read(SHADOW),
        _read(WEB / "js" / "pages" / "question.js"),
        _read(WEB / "js" / "pages" / "notes.js"),
        _read(WEB / "js" / "pages" / "vocab.js"),
    ]
    used = set()
    for source in sources:
        used.update(re.findall(r"t\('(speech\.[^']+)'", source))
    assert used
    assert used <= set(zh), sorted(used - set(zh))
