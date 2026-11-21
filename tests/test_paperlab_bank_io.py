"""我的题库 P1：统计 / JSON·CSV 导出 / JSON·CSV 导入（dry_run、去重、逐行错误、上限）。

数据全部用应用库 API 构造（不依赖只读高考题库），AI 只用直接写 ai_job_id 模拟，不联网。
"""
import csv
import io
import json

from fastapi.testclient import TestClient
import pytest

from app import db
from app.main import app
from app.paperlab import bank_io


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def user_id(client) -> int:
    return client.get("/api/auth/me").json()["id"]


def create_question(client, *, qtype="SINGLE", subject="数学", grade="高中一年级",
                    difficulty=0.4, tags=("函数",), content=None, status="ready") -> dict:
    payload_content = {
        "stem": "已知 $f(x)=x^2$，则 $f(2)$ 等于？",
        "options": [{"label": "A", "text": "2"}, {"label": "B", "text": "4"}],
        "answers": ["B"],
        "explanation": "代入 x=2 得 4。",
        "rubric": "",
    }
    if content is not None:
        payload_content = content
    response = client.post("/api/paperlab/questions", json={
        "type": qtype, "subject": subject, "grade": grade, "difficulty": difficulty,
        "knowledge_tags": list(tags), "content": payload_content, "status": status,
    })
    assert response.status_code == 200, response.text
    return response.json()["question"]


def list_all(client) -> list[dict]:
    response = client.get("/api/paperlab/questions", params={"page_size": 100})
    assert response.status_code == 200, response.text
    return response.json()["items"]


def csv_rows(content: bytes) -> list[dict]:
    text = content.decode("utf-8-sig")
    return list(csv.DictReader(io.StringIO(text)))


def import_file(client, filename: str, content: bytes, *, dry_run: bool = False, media="application/octet-stream"):
    data = {"dry_run": "true"} if dry_run else {}
    return client.post("/api/paperlab/questions/import",
                       files={"file": (filename, content, media)}, data=data)


def import_json(client, items, *, dry_run=False, filename="题库.json"):
    raw = json.dumps(items, ensure_ascii=False).encode("utf-8")
    return import_file(client, filename, raw, dry_run=dry_run, media="application/json")


# ---------------------------------------------------------------------------
# 1. 统计
# ---------------------------------------------------------------------------

def test_stats_counts_and_user_isolation(client, second_client):
    first = create_question(client)
    second = create_question(client, qtype="SUBJECTIVE", tags=("函数", "导数"),
                             content={"stem": "证明函数单调递增。", "answers": ["略"], "explanation": "略"})
    third = create_question(client, qtype="BLANK", subject="物理", tags=(),
                            content={"stem": "重力加速度约为多少？", "answers": ["9.8"], "explanation": ""})
    missing = create_question(client, qtype="SUBJECTIVE", subject="数学", tags=(),
                              content={"stem": "缺少答案与解析的题目。"})
    client.post(f"/api/paperlab/questions/{second['id']}/verify", json={"status": "verified"})
    # 模拟 AI 生成（补全/识别任务的产物）：直接写 ai_job_id，统计 ai_generated。
    with db.connection() as con:
        con.execute("UPDATE pl_questions SET ai_job_id=9001, ai_model='demo-model' WHERE id=?",
                    (second["id"],))
    response = client.get("/api/paperlab/questions/stats")
    assert response.status_code == 200, response.text
    stats = response.json()
    assert stats["total"] == 4
    assert stats["by_type"]["SINGLE"] == 1
    assert stats["by_type"]["SUBJECTIVE"] == 2
    assert stats["by_type"]["BLANK"] == 1
    assert stats["by_type"]["MULTIPLE"] == 0 and stats["by_type"]["MIXED"] == 0
    assert stats["by_status"] == {"draft": 0, "ready": 4, "archived": 0}
    assert stats["by_verify"]["verified"] == 1
    assert stats["by_verify"]["pending"] == 3
    assert stats["by_subject"][0] == {"subject": "数学", "count": 3}
    assert {"subject": "物理", "count": 1} in stats["by_subject"]
    # 缺答案：missing 题（无 answers）；缺解析：third 与 missing；缺知识点：third 与 missing
    assert stats["missing_answers"] == 1
    assert stats["missing_explanation"] == 2
    assert stats["missing_tags"] == 2
    tags = {item["tag"]: item["count"] for item in stats["knowledge"]["top"]}
    assert tags["函数"] == 2 and tags["导数"] == 1
    assert stats["knowledge"]["total"] == 2
    assert stats["recent_7d"] == 4
    assert stats["ai_generated"] == 1
    # 学科筛选
    math_stats = client.get("/api/paperlab/questions/stats?subject=数学").json()
    assert math_stats["total"] == 3
    assert math_stats["by_subject"] == [{"subject": "数学", "count": 3}]
    assert math_stats["missing_answers"] == 1
    # 用户隔离
    other = second_client.get("/api/paperlab/questions/stats").json()
    assert other["total"] == 0
    assert other["by_subject"] == [] and other["knowledge"]["top"] == []
    assert other["recent_7d"] == 0
    # 空库不报错（新用户空筛选项）
    empty = second_client.get("/api/paperlab/questions/stats?subject=数学&grade=高中一年级")
    assert empty.status_code == 200 and empty.json()["total"] == 0


# ---------------------------------------------------------------------------
# 2. 导出
# ---------------------------------------------------------------------------

def test_export_json_and_csv_contents(client):
    question = create_question(client)
    # 制造需要 CSV 转义的内容：逗号、双引号、换行
    tricky = create_question(client, qtype="SUBJECTIVE", subject="语文", tags=("阅读",),
                             content={
                                 "stem": '阅读材料，回答"问题"一：\n第一行, 第二行',
                                 "answers": ["要点一", "要点二"],
                                 "explanation": "解析包含,逗号与\"引号\"。",
                                 "rubric": "内容 3 分，语言 2 分",
                             })
    response = client.get("/api/paperlab/questions/export?format=json")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    disposition = response.headers["content-disposition"]
    assert "filename*=UTF-8''" in disposition and "%E9%A2%98%E5%BA%93" in disposition
    items = response.json()
    assert len(items) == 2
    assert items[0]["id"] == question["id"]
    assert items[0]["content"]["answers"] == ["B"]
    assert "content" in items[0] and "knowledge_tags" in items[0]
    filtered = client.get("/api/paperlab/questions/export?format=json&subject=语文").json()
    assert len(filtered) == 1 and filtered[0]["id"] == tricky["id"]

    csv_response = client.get("/api/paperlab/questions/export?format=csv&subject=语文")
    assert csv_response.status_code == 200
    assert csv_response.headers["content-type"].startswith("text/csv")
    raw = csv_response.content
    assert raw.startswith(b"\xef\xbb\xbf"), "CSV 需要 UTF-8 BOM 便于 Excel 打开"
    text = raw.decode("utf-8-sig")
    header = text.splitlines()[0]
    for label in ("题型", "学科", "年级", "难度", "知识点", "题干", "选项", "答案", "解析", "评分标准"):
        assert label in header
    rows = csv_rows(raw)
    assert len(rows) == 1
    row = rows[0]
    assert row["题型"] == "SUBJECTIVE"
    assert row["学科"] == "语文"
    assert row["知识点"] == "阅读"
    # csv 模块往返：双引号/逗号/换行必须原样保留
    assert row["题干"] == '阅读材料，回答"问题"一：\n第一行, 第二行'
    assert row["答案"] == "要点一 | 要点二"
    assert "逗号" in row["解析"] and '"引号"' in row["解析"]
    assert row["评分标准"] == "内容 3 分，语言 2 分"

    invalid = client.get("/api/paperlab/questions/export?format=xlsx")
    assert invalid.status_code == 422
    assert "json" in invalid.json()["detail"] and "csv" in invalid.json()["detail"]


def test_export_limit_and_isolation(client, second_client, monkeypatch):
    for _ in range(3):
        create_question(client)
    monkeypatch.setattr(bank_io, "MAX_EXPORT_QUESTIONS", 2)
    limited = client.get("/api/paperlab/questions/export?format=json")
    assert limited.status_code == 422
    assert "上限" in limited.json()["detail"] and "缩小筛选" in limited.json()["detail"]
    # 他人题库不可见
    other = second_client.get("/api/paperlab/questions/export?format=json")
    assert other.status_code == 200 and other.json() == []
    assert second_client.get("/api/paperlab/questions/stats").json()["total"] == 0


# ---------------------------------------------------------------------------
# 3. 导入：dry_run / 正式 / 去重 / 逐行错误
# ---------------------------------------------------------------------------

def valid_json_items() -> list[dict]:
    return [
        {
            "type": "SINGLE", "subject": "英语", "grade": "高中一年级", "difficulty": 0.3,
            "knowledge_tags": ["语法"],
            "content": {
                "stem": "He ____ to school every day.",
                "options": [{"label": "A", "text": "go"}, {"label": "B", "text": "goes"}],
                "answers": ["B"], "explanation": "第三人称单数。",
            },
        },
        {
            "type": "SUBJECTIVE", "subject": "语文", "grade": "高中一年级",
            "knowledge_tags": ["作文"],
            "content": {
                "stem": "以「坚持」为题写一段话。", "answers": ["略"],
                "explanation": "言之成理即可。", "rubric": "内容 3 分，语言 2 分",
            },
        },
    ]


def test_import_json_dry_run_does_not_write(client):
    before = len(list_all(client))
    items = valid_json_items() + [{"type": "BOGUS", "subject": "英语", "content": {"stem": "x"}}]
    response = import_json(client, items, dry_run=True)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["dry_run"] is True
    assert body["total"] == 3
    assert body["would_create"] == 2
    assert body["created"] == 0
    assert len(body["errors"]) == 1
    assert body["errors"][0]["row"] == 3 and "题型" in body["errors"][0]["error"]
    assert len(list_all(client)) == before, "dry_run 不能写库"


def test_import_json_formal_and_dedup(client):
    items = valid_json_items()
    created = import_json(client, items)
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["created"] == 2 and body["skipped"] == 0 and body["errors"] == []
    rows = list_all(client)
    assert len(rows) == 2
    english = next(item for item in rows if item["subject"] == "英语")
    assert english["status"] == "draft"
    assert english["source"] == "import"
    assert english["verify_status"] == "pending"
    assert english["content"]["answers"] == ["B"]
    assert english["knowledge_tags"] == ["语法"]
    # 重复导入：按内容摘要去重，不覆盖已有题目
    repeat = import_json(client, items)
    assert repeat.status_code == 200
    assert repeat.json()["created"] == 0 and repeat.json()["skipped"] == 2
    assert len(list_all(client)) == 2
    # 文件内重复也跳过
    dup = import_json(client, [items[0], items[0]])
    assert dup.json()["created"] == 0 and dup.json()["skipped"] == 2


def test_import_csv_with_errors_and_roundtrip(client):
    seed = create_question(client, qtype="SUBJECTIVE", subject="语文", tags=("阅读",), content={
        "stem": "阅读下面材料，回答问题。", "answers": ["要点"], "explanation": "解析",
        "rubric": "",
    })
    exported = client.get("/api/paperlab/questions/export?format=csv&subject=语文")
    # 1) 导出的 CSV 可被再导入（内容摘要一致 → skipped）
    dry = import_file(client, "题库.csv", exported.content, dry_run=True)
    assert dry.status_code == 200, dry.text
    assert dry.json()["format"] == "csv"
    assert dry.json()["would_skip"] == 1 and dry.json()["would_create"] == 0
    # 2) 带一行非法数据的 CSV：逐行错误 + 合法行可正式导入
    text = (
        "\ufeff题型,学科,年级,难度,知识点,题干,选项,答案,解析,评分标准,子题摘要\n"
        "SINGLE,历史,高中一年级,0.4,古代史,\"材料, 含逗号的选择题\",A. 秦朝 | B. 汉朝,B,解析,,\n"
        "SINGLE,历史,高中一年级,abc,古代史,难度非法的问题,A. 甲 | B. 乙,B,,\n"
        "BOGUS,历史,,0.5,,未知题型,x,,,\n"
    )
    result = import_file(client, "历史.csv", text.encode("utf-8"))
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["created"] == 1
    assert len(body["errors"]) == 2
    rows_by_line = {item["row"]: item["error"] for item in body["errors"]}
    assert 3 in rows_by_line and 4 in rows_by_line
    assert "难度" in rows_by_line[3]
    assert "未知的题型" in rows_by_line[4]
    imported = next(item for item in list_all(client) if item["subject"] == "历史")
    assert imported["content"]["options"][0] == {"label": "A", "text": "秦朝"}
    assert imported["content"]["stem"] == "材料, 含逗号的选择题"
    assert seed["id"] in {item["id"] for item in list_all(client)}


def test_import_structural_errors_and_limits(client):
    bad_json = import_file(client, "bad.json", b"not-json", media="application/json")
    assert bad_json.status_code == 422
    assert "JSON 解析失败" in bad_json.json()["detail"]
    missing_columns = import_file(client, "bad.csv", "题干,答案\n题目,x\n".encode("utf-8"))
    assert missing_columns.status_code == 422
    assert "缺少必要列" in missing_columns.json()["detail"]
    empty = import_file(client, "empty.json", b"[]", media="application/json")
    assert empty.status_code == 422 and "没有题目记录" in empty.json()["detail"]
    oversized = import_file(client, "big.json", b"[" + b" " * (10 * 1024 * 1024), media="application/json")
    assert oversized.status_code == 413 and "10MB" in oversized.json()["detail"]
    too_many = [valid_json_items()[0].copy() for _ in range(2001)]
    for index, item in enumerate(too_many):
        item["content"] = dict(item["content"], stem=f"批量题目 {index}")
    too_many_response = import_json(client, too_many, dry_run=True)
    assert too_many_response.status_code == 422
    assert "最多导入" in too_many_response.json()["detail"]


# ---------------------------------------------------------------------------
# 4. 归属 / 未登录
# ---------------------------------------------------------------------------

def test_stats_export_import_require_auth(client, second_client):
    anonymous = TestClient(app)
    assert anonymous.get("/api/paperlab/questions/stats").status_code == 401
    assert anonymous.get("/api/paperlab/questions/export?format=json").status_code == 401
    assert anonymous.post("/api/paperlab/questions/import",
                          files={"file": ("x.json", b"[]", "application/json")}).status_code == 401
    # 他人导入的题目不可见（隔离）
    import_json(second_client, valid_json_items())
    assert client.get("/api/paperlab/questions/stats").json()["total"] == 0
    assert client.get("/api/paperlab/questions/export?format=json").json() == []
    assert len(list_all(second_client)) == 2
