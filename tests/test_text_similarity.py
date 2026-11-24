"""文本相似度可选依赖与 difflib 兜底。"""
from app import text_similarity


def test_ratio_identical_and_empty():
    assert text_similarity.ratio("函数定义域", "函数定义域") == 1.0
    assert text_similarity.ratio("", "内容") == 0.0
    assert 0.0 < text_similarity.ratio(
        "函数定义域是自变量取值范围", "函数定义域为自变量的取值范围"
    ) < 1.0


def test_near_duplicate_threshold():
    assert text_similarity.is_near_duplicate("函数定义域", "函数定义域")
    assert not text_similarity.is_near_duplicate("函数定义域", "概率初步", threshold=0.9)


def test_difflib_fallback(monkeypatch):
    monkeypatch.setattr(text_similarity, "backend", lambda: "difflib")
    assert text_similarity.ratio("中华人民共和国", "中华人民共和国") == 1.0
    assert text_similarity.ratio("中华人民共和国", "中华人名共和国") > 0.8


def test_normalize_text_folds_whitespace_and_fullwidth():
    assert text_similarity.normalize_text("  Ａ  b\t\n") == "A b"
