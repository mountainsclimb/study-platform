"""M6 扫描资料本地 OCR 内容块测试：页面级块保留、定位字段与来源标注。

验证视觉模型不可用时，扫描页保留本地 OCR 文本的同时带上内容块（bbox/norm/阅读顺序/
置信度），并随页结果落库、可再次读取；视觉模型成功时不会把本地块冒充视觉转写。
"""
from __future__ import annotations

import asyncio
import json

import pytest


def test_blocks_normalization_keeps_location_fields():
    from app.ai import notes_agent

    raw = [
        {"id": "b1", "kind": "text", "text": "已知函数", "order": 0, "page": 1,
         "bbox": {"x": 1, "y": 2, "w": 3, "h": 4}, "norm": {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4},
         "confidence": 0.98},
        {"id": "b2", "kind": "math", "text": "x^2", "latex": None, "needs_review": True,
         "order": 1, "page": 1, "confidence": 0.9},
        {"id": "b3", "kind": "text", "text": "   "},  # 空文字块被丢弃
        "not-a-dict",
    ]
    blocks = notes_agent._normalize_ocr_blocks(raw)
    assert [block["id"] for block in blocks] == ["b1", "b2"]
    assert blocks[0]["bbox"]["x"] == 1 and blocks[0]["norm"]["w"] == 0.3
    assert blocks[1]["needs_review"] is True and blocks[1]["latex"] is None
    # 超量截断到 200 块。
    many = [{"id": f"b{i}", "kind": "text", "text": f"t{i}"} for i in range(300)]
    assert len(notes_agent._normalize_ocr_blocks(many)) == 200


def test_page_entry_omits_empty_blocks_and_keeps_records():
    from app.ai import notes_agent

    plain = notes_agent._page_entry("文字", "recognized", "local_ocr")
    assert "blocks" not in plain  # 旧页面结构不新增空键
    with_blocks = notes_agent._page_entry(
        "文字", "recognized", "local_ocr",
        blocks=[{"id": "b1", "kind": "text", "text": "文字", "order": 0}],
    )
    assert with_blocks["blocks"][0]["order"] == 0


def test_normalize_ocr_pages_preserves_blocks_round_trip():
    from app.ai import notes_agent

    pages = {"1": {"text": "第一页", "state": "recognized", "source": "local_ocr",
                   "blocks": [{"id": "b1", "kind": "math", "text": "x^2", "needs_review": True,
                               "bbox": {"x": 0, "y": 0, "w": 10, "h": 10}}]}}
    raw = json.dumps({"version": 3, "pages": pages}, ensure_ascii=False)
    normalized = notes_agent._normalize_ocr_pages(raw, 3)
    entry = normalized["pages"]["1"]
    assert entry["source"] == "local_ocr"
    assert entry["blocks"][0]["kind"] == "math"
    # 再序列化后仍可读（落库路径）。
    again = notes_agent._normalize_ocr_pages(notes_agent._ocr_pages_json(3, normalized["pages"]), 3)
    assert again["pages"]["1"]["blocks"][0]["id"] == "b1"


def test_local_ocr_fallback_entry_carries_blocks():
    from app.ai import notes_agent

    entry = notes_agent._ocr_fallback_entry(
        2, "本地文字", timeout=True, blocks=[{"id": "b1", "kind": "text", "text": "本地文字"}],
    )
    assert entry["source"] == "local_ocr" and entry["state"] == "recognized"
    assert entry["blocks"][0]["text"] == "本地文字"
    failed = notes_agent._ocr_fallback_entry(3, "", error=RuntimeError("vision down"))
    assert failed["state"] == "failed" and "blocks" not in failed


def test_local_ocr_page_requests_blocks(monkeypatch):
    from app.ai import notes_agent, ocr

    calls: list[dict] = []

    async def fake_recognize_image(data=None, path=None, *, include_blocks=False, **kwargs):
        calls.append({"include_blocks": include_blocks})
        return {
            "text": "第一行\n第二行",
            "blocks": [
                {"id": "b1", "kind": "text", "text": "第一行", "order": 0, "page": 1,
                 "bbox": {"x": 1, "y": 2, "w": 30, "h": 10}, "norm": {"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2}},
                {"id": "b2", "kind": "math", "text": "x=1", "latex": None, "needs_review": True, "order": 1},
            ],
        }

    monkeypatch.setattr(ocr, "recognize_image", fake_recognize_image)
    text, blocks, note = asyncio.run(notes_agent._local_ocr_page({"mime": "image/png", "data": "aGVsbG8="}))
    assert calls and calls[0]["include_blocks"] is True
    assert text == "第一行\n第二行" and note is None
    assert [block["id"] for block in blocks] == ["b1", "b2"]


def test_save_ocr_pages_persists_blocks_for_local_scan(client):
    """扫描任务保存页结果时，本地 OCR 块随页持久化，可由资料页状态读回。"""
    from app import ai, db
    from app.ai import notes_agent

    with db.connection() as con:
        con.execute(
            "INSERT INTO ai_documents(user_id,title,storage_name,pages,status,chunks_json,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (1, "扫描验收资料", "scan.pdf", 2, "needs_ocr", "[]", db.utcnow()),
        )
        document_id = con.execute("SELECT id FROM ai_documents ORDER BY id DESC LIMIT 1").fetchone()[0]
    asyncio.run(notes_agent._save_ocr_pages(
        document_id, 1,
        [{"page": 1, "text": "本地识别文字", "state": "recognized", "source": "local_ocr",
          "blocks": [{"id": "b1", "kind": "text", "text": "本地识别文字", "order": 0, "page": 1,
                      "bbox": {"x": 3, "y": 4, "w": 50, "h": 20}}]}],
    ))
    with db.connection() as con:
        row = con.execute("SELECT ocr_pages_json FROM ai_documents WHERE id=?", (document_id,)).fetchone()
    normalized = notes_agent._normalize_ocr_pages(row[0], 1)
    entry = normalized["pages"]["1"]
    assert entry["source"] == "local_ocr" and entry["blocks"][0]["bbox"]["w"] == 50
