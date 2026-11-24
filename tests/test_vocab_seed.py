"""ECDICT 词汇种子验收（方案 §7）：来源、SHA-256、字段白名单与条数。"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEED = ROOT / "app" / "data" / "vocab_seed.json"
README = ROOT / "app" / "data" / "vocab_seed.README.md"
LICENSE = ROOT / "app" / "data" / "vocab_seed.LICENSE"

ALLOWED_FIELDS = {
    "word", "phonetic", "translation", "pos", "tag",
    "collins", "oxford", "bnc", "frq", "exchange",
}
WORD_RE = re.compile(r"^[a-z][a-z' -]{0,63}$")


def test_vocab_seed_exists_and_readme_checksum():
    assert SEED.is_file(), "缺少 app/data/vocab_seed.json"
    assert README.is_file(), "缺少 vocab_seed.README.md"
    assert LICENSE.is_file(), "缺少 vocab_seed.LICENSE"
    digest = hashlib.sha256(SEED.read_bytes()).hexdigest()
    readme = README.read_text(encoding="utf-8")
    assert digest in readme, "README 未登记种子 SHA-256"
    assert "skywind3000/ECDICT" in readme
    assert "MIT" in LICENSE.read_text(encoding="utf-8")


def test_vocab_seed_fields_and_counts():
    payload = json.loads(SEED.read_text(encoding="utf-8"))
    items = payload.get("items")
    assert isinstance(items, list)
    assert 5000 <= len(items) <= 10000, len(items)
    assert len(items) == 8000
    assert payload["meta"]["count"] == len(items)
    assert payload["meta"]["source"] == "ECDICT"
    assert set(payload["meta"]["fields"]) == ALLOWED_FIELDS

    seen = set()
    gk = 0
    for item in items:
        assert set(item) == ALLOWED_FIELDS, set(item) ^ ALLOWED_FIELDS
        assert WORD_RE.match(item["word"]), item["word"]
        assert item["word"] not in seen, item["word"]
        seen.add(item["word"])
        assert item["translation"].strip(), item["word"]
        if "gk" in str(item["tag"]).split():
            gk += 1
        for key in ("collins", "oxford", "bnc", "frq"):
            assert item[key] is None or isinstance(item[key], int)
    assert gk >= 3000, gk

    def rank(item):
        frq = item["frq"] if item["frq"] else 10 ** 9
        return (frq, item["word"])

    assert [rank(item) for item in items] == sorted(rank(item) for item in items)
