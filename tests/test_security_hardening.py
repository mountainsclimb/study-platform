"""P0 安全门禁回归：题图鉴权绕过、空白图门禁、块级解析、题组公开内容门禁。

对应《知序全面审查与下一步优化方案》P0 第 1/2/3/7 项与 P1 题组误杀。
"""
import json
from pathlib import Path
import sqlite3

from fastapi import HTTPException
from PIL import Image
from io import BytesIO
import pytest

from app import db, practice
from bank_fixture import blank_image_bytes, drawn_image_bytes, make_bank, question_row


def _clear_bank_caches():
    practice._sqlite_question_columns.cache_clear()
    practice._sqlite_group_columns.cache_clear()
    practice._source_groups.cache_clear()


def _insert_attempt(user_id: int, qid: str):
    with db.connection() as con:
        con.execute(
            "INSERT INTO attempts(user_id,qid,answer_text,image_path,result,grade_source,created_at,graded_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (user_id, qid, "A", None, "correct", "reference", db.utcnow(), db.utcnow()),
        )


# ---------------------------------------------------------------------------
# 1. 题图鉴权：别处的 public 引用不能让任意 qid 零作答读取答案/解析图
# ---------------------------------------------------------------------------

def test_public_ref_on_other_question_cannot_authorize_foreign_qid(client, monkeypatch):
    user_id = client.get("/api/auth/me").json()["id"]
    monkeypatch.setattr(practice, "_question_group_key", lambda qid: None)
    entry = {
        "asset_id": "shared-asset",
        "refs": [
            # A 题的答案图（作答后可见）
            {"qid": "docA:1", "group_id": None, "field": "answer", "permission": "after_attempt"},
            # B 题把同一文件当公开题干图使用
            {"qid": "docA:2", "group_id": None, "field": "stem", "permission": "public"},
        ],
    }
    # 攻击者拿完全无关的 C 题 qid 请求：必须 403，而不是靠 B 题的 public 引用放行。
    with pytest.raises(HTTPException) as caught:
        practice._asset_authorized(entry, "docA:9", user_id)
    assert caught.value.status_code == 403
    # 不带 qid 的旧请求同样不能借 public 引用绕过答案门禁。
    with pytest.raises(HTTPException) as caught:
        practice._asset_authorized(entry, None, user_id)
    assert caught.value.status_code == 403
    # 只有 A 题自己作答后才可读。
    with pytest.raises(HTTPException) as caught:
        practice._asset_authorized(entry, "docA:1", user_id)
    assert caught.value.status_code == 403
    _insert_attempt(user_id, "docA:1")
    practice._asset_authorized(entry, "docA:1", user_id)
    # B 题自己的公开图零作答可读。
    practice._asset_authorized(entry, "docA:2", user_id)


# ---------------------------------------------------------------------------
# 2. 空白图门禁：小尺寸图片直接 422，不再有 <100px 豁免
# ---------------------------------------------------------------------------

def _tiny_png(size):
    buffer = BytesIO()
    Image.new("RGB", size, "white").save(buffer, format="PNG")
    return buffer.getvalue()


def test_tiny_image_no_longer_bypasses_blank_gate(client):
    from test_practice import bank_question

    question = bank_question(False)
    for size in ((1, 1), (40, 40), (99, 240)):
        response = client.post(
            "/api/attempts", data={"qid": question["qid"]},
            files={"file": ("tiny.png", _tiny_png(size), "image/png")},
        )
        assert response.status_code == 422, (size, response.text)
        assert "分辨率过低" in response.json()["detail"]

    blank = client.post(
        "/api/attempts", data={"qid": question["qid"]},
        files={"file": ("blank.png", blank_image_bytes(), "image/png")},
    )
    assert blank.status_code == 422
    assert "空白" in blank.json()["detail"]

    drawn = client.post(
        "/api/attempts", data={"qid": question["qid"]},
        files={"file": ("drawn.png", drawn_image_bytes(), "image/png")},
    )
    assert drawn.status_code == 200, drawn.text


# ---------------------------------------------------------------------------
# 3. has_solution 纳管块级解析（R3 170 题只有 solution_blocks_json）
# ---------------------------------------------------------------------------

@pytest.fixture
def block_solution_bank(tmp_path, monkeypatch):
    rows = [
        question_row(
            "docA:1", "只有块级解析的题目：函数单调性",
            solution="", solution_blocks_json=json.dumps(
                [{"kind": "text", "text": "解析块：先求导再判号"}], ensure_ascii=False),
        ),
        question_row("docA:2", "纯文本解析题目：集合运算", solution="解析：先化简再求交集"),
        question_row("docA:3", "没有任何解析的题目：概率初步"),
    ]
    bank = make_bank(tmp_path / "bank", rows)
    monkeypatch.setattr(db, "BANK_DIR", bank)
    _clear_bank_caches()
    yield bank
    _clear_bank_caches()


def test_block_only_solution_counts_as_has_solution(client, block_solution_bank):
    # 详情：块级解析非空即 has_solution=True。
    assert practice.load_question("docA:1")["has_solution"] is True
    assert practice.load_question("docA:2")["has_solution"] is True
    assert practice.load_question("docA:3")["has_solution"] is False

    # 筛选：has_solution=true 能命中只有块级解析的题，且列表卡同步标记。
    filtered = client.get("/api/questions", params={"has_solution": "true", "page_size": 50}).json()
    assert filtered["total"] == 2, filtered
    by_qid = {item["qid"]: item for item in filtered["items"]}
    assert by_qid["docA:1"]["has_solution"] is True

    missing = client.get("/api/questions", params={"has_solution": "false", "page_size": 50}).json()
    assert [item["qid"] for item in missing["items"]] == ["docA:3"]

    detail = client.get("/api/questions/docA:1")
    assert detail.status_code == 200
    assert detail.json()["has_solution"] is True
    # 作答后详情返回块级解析正文。
    submitted = client.post("/api/attempts", data={"qid": "docA:1", "answer": "A"})
    assert submitted.status_code == 200, submitted.text
    revealed = client.get("/api/questions/docA:1").json()
    assert revealed["solution_blocks"][0]["text"] == "解析块：先求导再判号"


# ---------------------------------------------------------------------------
# 4. 题组公开内容门禁：共用块已结构化时不再误报旧文本裸占位
# ---------------------------------------------------------------------------

def test_structured_shared_blocks_tolerate_legacy_bare_placeholders():
    stored = {
        "material_text": "材料正文：如图，[图1] 所示，函数 [公式2] 的图象…",
        "parent_stem": "父题：阅读材料，完成下列各题 [公式3]",
        "shared_blocks_json": json.dumps(
            [{"kind": "text", "text": "材料正文（结构化）"}], ensure_ascii=False),
        "member_index_json": "[]",
    }
    # 旧文本投影里的 [图N]/[公式N] 是正常形态，不应阻断组接口。
    assert practice._group_public_issues(stored) == []

    # 私用区字符仍然拦截。
    polluted = dict(stored, material_text="材料正文\ue023乱码")
    assert any("私用区字符" in item for item in practice._group_public_issues(polluted))

    # 共用块本身含未解析裸标记仍然拦截。
    bad_block = dict(
        stored,
        shared_blocks_json=json.dumps([{"kind": "text", "text": "共用块 [公式1]"}], ensure_ascii=False),
    )
    assert any("未解析" in item for item in practice._group_public_issues(bad_block))

    # 共用材料混入答案解析仍然拦截。
    answer_leak = dict(stored, material_text="材料正文【解析】答案就在材料里")
    assert any("答案解析" in item for item in practice._group_public_issues(answer_leak))


def _r3_bank() -> Path:
    return Path(__file__).resolve().parents[2] / "高考题库_按学科" / "releases" / "bank-20261120-r3"


@pytest.mark.skipif(not (_r3_bank() / "题库.db").is_file(), reason="R3 只读题库快照不存在")
def test_r3_all_groups_pass_public_content_gate():
    """R3 真实只读库：全部 2,246 个题组在修复后 0 个公开内容 issues（同一次只读统计）。"""
    bank = _r3_bank() / "题库.db"
    con = sqlite3.connect(bank.as_uri() + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        groups = [dict(row) for row in con.execute("SELECT * FROM groups")]
    finally:
        con.close()
    assert len(groups) == 2246, len(groups)
    offenders = []
    for stored in groups:
        issues = practice._group_public_issues(stored)
        if issues:
            offenders.append((stored["group_id"], issues))
    assert offenders == [], offenders[:5]
