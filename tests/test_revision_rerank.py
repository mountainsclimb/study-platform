"""错题巩固候选重排（app/paperlab/revision.py）。"""
from app.paperlab import revision


def _candidates():
    return [
        {"ref": "bank:1", "stem_preview": "函数题", "_kp_overlap": 2},
        {"ref": "bank:2", "stem_preview": "概率题", "_kp_overlap": 1},
        {"ref": "bank:3", "stem_preview": "几何题", "_kp_overlap": 1},
    ]


def test_apply_rerank_reorders_and_keeps_scores(monkeypatch):
    from app.ai import rerank

    monkeypatch.setattr(rerank, "available", lambda: True)
    monkeypatch.setattr(rerank, "score_texts", lambda query, texts: [0.1, 0.9, 0.5])
    result = revision._apply_rerank(_candidates(), [{"stem_plain": "错题题干"}])
    assert [item["ref"] for item in result] == ["bank:2", "bank:3", "bank:1"]
    assert result[0]["rerank_score"] == 0.9


def test_apply_rerank_failure_keeps_original_order(monkeypatch):
    from app.ai import rerank

    monkeypatch.setattr(rerank, "available", lambda: True)

    def broken(query, texts):
        raise RuntimeError("重排不可用")

    monkeypatch.setattr(rerank, "score_texts", broken)
    candidates = _candidates()
    assert revision._apply_rerank(candidates, [{"stem_plain": "题干"}]) == candidates


def test_apply_rerank_skips_when_disabled(monkeypatch):
    from app.ai import rerank

    monkeypatch.setattr(rerank, "available", lambda: False)
    candidates = _candidates()
    assert revision._apply_rerank(candidates, [{"stem_plain": "题干"}]) == candidates
    assert all("rerank_score" not in item for item in candidates)
