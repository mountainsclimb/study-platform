"""试卷工坊 · 整卷练习版本冻结（P0）测试。

覆盖：
- 开始练习的同一事务内冻结逐题快照（题目引用 / snapshot_json / 分值 / 题序 / 配图引用）；
- 开始后改题干、改参考答案、改分值：续做、判分、结果回看仍按冻结快照（参考答案与分值冻结）；
- 开始后移除题、重排：作答序号与题数仍按冻结快照，补答不按当前卷面重算题序；
- 删卷后已作答记录（进行中/已提交）仍可回看、补答、判分，历史列表可用，不 500；
- 新开作答读取试卷的新内容；
- 旧作答（无冻结行）回退现读当前试卷，返回 frozen=false 与「历史题面不可追溯」提示，不 500；
- 作答侧配图路由 ``/attempts/{id}/items/{pos}/images/{idx}`` 使用冻结引用（试卷改题后仍可读）。

全部使用应用题库临时数据 + 只读题库真实 qid，不联网。
"""
import json

from fastapi.testclient import TestClient

from app import db, practice
from app.main import app
from app.paperlab import papers as paper_store
from app.paperlab import practice as paperlab_practice


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def user_id(client) -> int:
    return client.get("/api/auth/me").json()["id"]


def create_question(client, **overrides) -> dict:
    payload = {
        "type": "SINGLE",
        "subject": "数学",
        "grade": "高中一年级",
        "difficulty": 0.2,
        "knowledge_tags": ["函数"],
        "content": {
            "stem": "已知 $f(x)=x^2$，则 $f(2)$ 等于（  ）",
            "options": [{"label": "A", "text": "2"}, {"label": "B", "text": "4"}],
            "answers": ["B"],
            "explanation": "代入 x=2 得 4。",
        },
        "status": "ready",
    }
    payload.update(overrides)
    response = client.post("/api/paperlab/questions", json=payload)
    assert response.status_code == 200, response.text
    return response.json()["question"]


def create_paper(client, items, title="冻结测试卷") -> dict:
    response = client.post("/api/paperlab/papers", json={
        "title": title, "kind": "manual", "subject": "数学", "grade": "高中一年级",
        "items": items,
    })
    assert response.status_code == 200, response.text
    return response.json()


def paper_version(client, paper_id: int) -> int:
    response = client.get(f"/api/paperlab/papers/{paper_id}")
    assert response.status_code == 200, response.text
    return response.json()["paper"]["version"]


def patch_paper_items(client, paper_id: int, items: list[dict]) -> dict:
    response = client.patch(f"/api/paperlab/papers/{paper_id}", json={
        "version": paper_version(client, paper_id), "items": items,
    })
    assert response.status_code == 200, response.text
    return response.json()


def patch_question(client, question_id: int, content: dict) -> dict:
    current = client.get(f"/api/paperlab/questions/{question_id}").json()["question"]
    response = client.patch(f"/api/paperlab/questions/{question_id}", json={
        "version": current["version"], "content": content,
    })
    assert response.status_code == 200, response.text
    return response.json()["question"]


def start_attempt(client, paper_id: int) -> dict:
    response = client.post(f"/api/paperlab/papers/{paper_id}/attempts")
    assert response.status_code == 200, response.text
    return response.json()


def save_answer(client, attempt_id: int, position: int, answer):
    return client.patch(f"/api/paperlab/attempts/{attempt_id}/answers",
                        json={"item_position": position, "answer": answer})


def submit(client, attempt_id: int, answers=None):
    payload = {"answers": answers} if answers is not None else {}
    return client.post(f"/api/paperlab/attempts/{attempt_id}/submit", json=payload)


def get_attempt(client, attempt_id: int) -> dict:
    response = client.get(f"/api/paperlab/attempts/{attempt_id}")
    assert response.status_code == 200, response.text
    return response.json()


def drop_frozen_rows(attempt_id: int) -> None:
    """模拟冻结功能上线前的历史作答（无 pl_attempt_items 行）。"""
    with db.connection() as con:
        con.execute("DELETE FROM pl_attempt_items WHERE attempt_id=?", (attempt_id,))


def bank_qid_with_image() -> str:
    with practice._bank() as con:
        rows = con.execute(
            "SELECT qid FROM questions WHERE subject='数学' ORDER BY qid LIMIT 300"
        ).fetchall()
    for row in rows:
        question = practice.load_question(row[0])
        if not question.get("practice_allowed"):
            continue
        images = (paper_store.snapshot_bank(row[0]).get("content") or {}).get("images") or []
        if images:
            return row[0]
    raise AssertionError("no bank question with image")


# ---------------------------------------------------------------------------
# 1. 创建练习时同事务冻结逐题快照
# ---------------------------------------------------------------------------

def test_create_attempt_freezes_items_and_paper_metadata(client):
    first = create_question(client)
    second = create_question(client, type="SUBJECTIVE", content={
        "stem": "证明 $f(x)=x^2$ 在 $[0,+\\infty)$ 上单调递增。",
        "answers": ["利用导数可证。"],
        "explanation": "对 $f$ 求导得 $f'(x)=2x\\ge 0$。",
    })
    paper = create_paper(client, [
        {"ref": f"app:{first['id']}", "score": 3, "section": "一、客观题"},
        {"ref": f"app:{second['id']}", "score": 10, "section": "二、解答题"},
    ])
    paper_id = paper["paper"]["id"]
    sheet = start_attempt(client, paper_id)
    attempt_id = sheet["attempt"]["id"]

    assert sheet["frozen"] is True
    assert sheet["attempt"]["frozen"] is True
    assert sheet["paper_deleted"] is False
    assert sheet["notice"] == ""
    assert sheet["attempt"]["paper_version"] == paper["paper"]["version"]
    assert [item["question_ref"] for item in sheet["items"]] == [
        f"app:{first['id']}", f"app:{second['id']}",
    ]
    # 卷面（未提交）仍不泄题
    dumped = json.dumps(sheet, ensure_ascii=False)
    assert "参考答案" not in dumped and "利用导数可证" not in dumped

    uid = user_id(client)
    with db.connection() as con:
        rows = con.execute(
            "SELECT * FROM pl_attempt_items WHERE attempt_id=? ORDER BY item_position",
            (attempt_id,),
        ).fetchall()
        attempt_row = con.execute(
            "SELECT * FROM pl_paper_attempts WHERE id=?", (attempt_id,)
        ).fetchone()
    assert len(rows) == 2
    frozen_snapshot = json.loads(rows[0]["snapshot_json"])
    assert frozen_snapshot["content"]["answers"] == ["B"]
    assert rows[0]["question_ref"] == f"app:{first['id']}"
    assert float(rows[0]["score"]) == 3 and float(rows[0]["max_score"]) == 3
    assert rows[1]["user_id"] == uid
    paper_meta = json.loads(attempt_row["paper_snapshot_json"])
    assert paper_meta["title"] == "冻结测试卷"
    assert paper_meta["subject"] == "数学"


# ---------------------------------------------------------------------------
# 2. 开始后改题干 / 改参考答案：续做与判分仍按冻结快照
# ---------------------------------------------------------------------------

def test_attempt_keeps_stem_and_grading_after_question_and_score_change(client):
    question = create_question(client)
    paper = create_paper(client, [{"ref": f"app:{question['id']}", "score": 3}])
    paper_id = paper["paper"]["id"]
    attempt_id = start_attempt(client, paper_id)["attempt"]["id"]

    # 试卷开始后：改题干、改参考答案、改分值（重新冻结试卷快照）
    patch_question(client, question["id"], {
        "stem": "修改后的题干：$f(3)$ 等于（  ）",
        "answers": ["A"],
        "explanation": "修改后的解析。",
    })
    patch_paper_items(client, paper_id, [{"ref": f"app:{question['id']}", "score": 8}])

    old_sheet = get_attempt(client, attempt_id)
    assert old_sheet["frozen"] is True
    assert "修改后的题干" not in old_sheet["items"][0]["content"]["stem"]
    assert "$f(2)$" in old_sheet["items"][0]["content"]["stem"]
    assert old_sheet["items"][0]["score"] == 3
    assert old_sheet["attempt"]["max_score"] == 3

    # 判分用冻结的参考答案（B）与冻结分值（3），而不是当前试卷的 A / 8 分
    assert save_answer(client, attempt_id, 1, ["B"]).status_code == 200
    result = submit(client, attempt_id)
    assert result.status_code == 200, result.text
    graded = result.json()
    assert graded["items"][0]["judgement"] == "correct"
    assert graded["items"][0]["score"] == 3
    assert graded["attempt"]["score"] == 3
    assert graded["attempt"]["max_score"] == 3

    # 新开作答读取新内容（新题干 / 新答案 A / 新分值 8）
    fresh = start_attempt(client, paper_id)
    assert fresh["frozen"] is True
    assert "修改后的题干" in fresh["items"][0]["content"]["stem"]
    assert fresh["items"][0]["score"] == 8
    fresh_result = submit(client, fresh["attempt"]["id"],
                          [{"item_position": 1, "answer": ["A"]}])
    assert fresh_result.status_code == 200, fresh_result.text
    assert fresh_result.json()["items"][0]["judgement"] == "correct"
    assert fresh_result.json()["items"][0]["score"] == 8
    assert fresh_result.json()["attempt"]["max_score"] == 8


# ---------------------------------------------------------------------------
# 3. 开始后移除题 / 重排：题数与题序不按当前卷面重算
# ---------------------------------------------------------------------------

def test_attempt_ignores_item_removal_and_reorder(client):
    first = create_question(client)
    second = create_question(client, content={
        "stem": "第二题题干", "options": [{"label": "A", "text": "甲"}, {"label": "B", "text": "乙"}],
        "answers": ["B"],
    })
    third = create_question(client, content={
        "stem": "第三题题干", "options": [{"label": "A", "text": "甲"}, {"label": "B", "text": "乙"}],
        "answers": ["B"],
    })
    refs = [f"app:{first['id']}", f"app:{second['id']}", f"app:{third['id']}"]
    paper = create_paper(client, [
        {"ref": refs[0], "score": 3},
        {"ref": refs[1], "score": 4},
        {"ref": refs[2], "score": 5},
    ])
    paper_id = paper["paper"]["id"]
    attempt_id = start_attempt(client, paper_id)["attempt"]["id"]
    assert save_answer(client, attempt_id, 2, ["B"]).status_code == 200

    # 移除第二题并重排：当前卷面只剩 [第三题, 第一题]
    patch_paper_items(client, paper_id, [
        {"ref": refs[2], "score": 5},
        {"ref": refs[0], "score": 3},
    ])

    sheet = get_attempt(client, attempt_id)
    assert sheet["frozen"] is True
    assert sheet["attempt"]["item_count"] == 3
    assert [item["question_ref"] for item in sheet["items"]] == refs
    assert [item["content"]["stem"] for item in sheet["items"]][:2] == [
        "已知 $f(x)=x^2$，则 $f(2)$ 等于（  ）", "第二题题干",
    ]

    # 第 3 题在当前卷面已不存在，但冻结卷面里仍可补答；第 4 题越界
    assert save_answer(client, attempt_id, 3, ["B"]).status_code == 200
    assert save_answer(client, attempt_id, 4, ["B"]).status_code == 422

    result = submit(client, attempt_id)
    assert result.status_code == 200, result.text
    graded = result.json()
    assert graded["frozen"] is True
    assert [item["question_ref"] for item in graded["items"]] == refs
    assert graded["attempt"]["max_score"] == 12
    assert graded["attempt"]["item_count"] == 3
    assert graded["items"][1]["judgement"] == "correct"
    # 移除题后重新回看结果仍是冻结节面
    replay = get_attempt(client, attempt_id)
    assert [item["question_ref"] for item in replay["items"]] == refs
    assert replay["result"]["summary"]["max_score"] == 12


# ---------------------------------------------------------------------------
# 4. 作答侧配图路由使用冻结引用
# ---------------------------------------------------------------------------

def test_attempt_image_route_uses_frozen_snapshot(client):
    qid = bank_qid_with_image()
    fallback = create_question(client)
    paper = create_paper(client, [
        {"ref": f"bank:{qid}", "score": 5},
        {"ref": f"app:{fallback['id']}", "score": 2},
    ])
    paper_id = paper["paper"]["id"]
    attempt_id = start_attempt(client, paper_id)["attempt"]["id"]
    sheet = get_attempt(client, attempt_id)
    assert sheet["items"][0]["content"]["images"], "只读题库配图应进入冻结卷面"

    url = f"/api/paperlab/attempts/{attempt_id}/items/1/images/0"
    image = client.get(url)
    assert image.status_code == 200, image.text
    assert image.headers["content-type"].startswith("image/")
    assert len(image.content) > 0
    assert client.get(f"/api/paperlab/attempts/{attempt_id}/items/1/images/99").status_code == 404
    assert client.get(f"/api/paperlab/attempts/{attempt_id}/items/9/images/0").status_code == 404
    assert client.get("/api/paperlab/attempts/999999/items/1/images/0").status_code == 404

    # 把试卷改成不含配图的卷面：现卷配图路由 404，冻结作答路由仍能读原图
    patch_paper_items(client, paper_id, [{"ref": f"app:{fallback['id']}", "score": 2}])
    assert client.get(f"/api/paperlab/papers/{paper_id}/items/1/images/0").status_code == 404
    assert client.get(url).status_code == 200

    fresh = start_attempt(client, paper_id)
    assert fresh["attempt"]["item_count"] == 1
    assert fresh["items"][0]["content"]["images"] == []


# ---------------------------------------------------------------------------
# 5. 旧作答回退现读 + 不可追溯提示（不 500）
# ---------------------------------------------------------------------------

def test_legacy_attempt_falls_back_to_live_paper_with_notice(client):
    question = create_question(client)
    paper = create_paper(client, [{"ref": f"app:{question['id']}", "score": 3}])
    paper_id = paper["paper"]["id"]
    attempt_id = start_attempt(client, paper_id)["attempt"]["id"]
    drop_frozen_rows(attempt_id)

    patch_question(client, question["id"], {
        "stem": "旧作答回退测试：新题干", "answers": ["B"], "explanation": "新解析。",
    })
    patch_paper_items(client, paper_id, [{"ref": f"app:{question['id']}", "score": 7}])

    sheet = get_attempt(client, attempt_id)
    assert sheet["frozen"] is False
    assert sheet["paper_deleted"] is False
    assert sheet["attempt"]["frozen"] is False
    assert "不可追溯" in sheet["notice"]
    assert "旧作答回退测试：新题干" in sheet["items"][0]["content"]["stem"]
    assert sheet["items"][0]["score"] == 7

    saved = save_answer(client, attempt_id, 1, ["B"])
    assert saved.status_code == 200, saved.text
    assert saved.json()["frozen"] is False
    result = submit(client, attempt_id)
    assert result.status_code == 200, result.text
    assert result.json()["items"][0]["judgement"] == "correct"
    assert result.json()["attempt"]["max_score"] == 7


# ---------------------------------------------------------------------------
# 6. 删卷后历史作答仍可回看 / 补答 / 判分
# ---------------------------------------------------------------------------

def test_paper_delete_keeps_attempts_readable(client):
    first = create_question(client)
    second = create_question(client)
    paper = create_paper(client, [
        {"ref": f"app:{first['id']}", "score": 3},
        {"ref": f"app:{second['id']}", "score": 5},
    ], title="删除后回看测试卷")
    paper_id = paper["paper"]["id"]

    submitted_id = start_attempt(client, paper_id)["attempt"]["id"]
    assert save_answer(client, submitted_id, 1, ["B"]).status_code == 200
    assert submit(client, submitted_id).status_code == 200
    open_id = start_attempt(client, paper_id)["attempt"]["id"]
    assert save_answer(client, open_id, 1, ["B"]).status_code == 200

    deleted = client.delete(f"/api/paperlab/papers/{paper_id}")
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["attempts_kept"] == 2
    assert client.get(f"/api/paperlab/papers/{paper_id}").status_code == 404

    # 已提交结果：删卷后仍可回看，卷面/判分来自冻结快照
    submitted_sheet = get_attempt(client, submitted_id)
    assert submitted_sheet["frozen"] is True
    assert submitted_sheet["paper_deleted"] is True
    assert submitted_sheet["paper"]["deleted"] is True
    assert submitted_sheet["paper"]["title"] == "删除后回看测试卷"
    assert submitted_sheet["attempt"]["status"] == "submitted"
    assert [item["question_ref"] for item in submitted_sheet["items"]] == [
        f"app:{first['id']}", f"app:{second['id']}",
    ]
    assert submitted_sheet["items"][0]["judgement"] == "correct"
    assert "已删除" in submitted_sheet["notice"]

    # 进行中的练习：删卷后仍可补答并提交判分
    open_sheet = get_attempt(client, open_id)
    assert open_sheet["frozen"] is True and open_sheet["paper_deleted"] is True
    assert open_sheet["items"][0]["answer"] == ["B"]
    assert save_answer(client, open_id, 2, ["B"]).status_code == 200
    finished = submit(client, open_id)
    assert finished.status_code == 200, finished.text
    body = finished.json()
    assert body["paper_deleted"] is True
    assert body["attempt"]["max_score"] == 8
    assert body["items"][0]["judgement"] == "correct"
    assert body["items"][1]["judgement"] == "correct"

    # 历史列表：试卷已删除，但本卷练习历史可用
    history = client.get(f"/api/paperlab/papers/{paper_id}/attempts")
    assert history.status_code == 200, history.text
    assert history.json()["total"] == 2
    assert all(entry["paper_deleted"] is True for entry in history.json()["items"])

    # 新开练习：试卷不存在 → 404
    assert client.post(f"/api/paperlab/papers/{paper_id}/attempts").status_code == 404


# ---------------------------------------------------------------------------
# 7. 旧作答 + 删卷：回退读取不 500
# ---------------------------------------------------------------------------

def test_legacy_attempt_after_paper_delete_does_not_crash(client):
    question = create_question(client)
    paper = create_paper(client, [{"ref": f"app:{question['id']}", "score": 3}])
    paper_id = paper["paper"]["id"]
    attempt_id = start_attempt(client, paper_id)["attempt"]["id"]
    drop_frozen_rows(attempt_id)
    assert client.delete(f"/api/paperlab/papers/{paper_id}").status_code == 200

    sheet = get_attempt(client, attempt_id)
    assert sheet["frozen"] is False
    assert sheet["paper_deleted"] is True
    assert sheet["items"] == []
    assert "不可追溯" in sheet["notice"]

    response = submit(client, attempt_id)
    assert response.status_code == 422, response.text
    assert "题目" in response.json()["detail"]


# ---------------------------------------------------------------------------
# 8. 旧库迁移：移除作答表到试卷的级联外键（删卷不再丢练习记录）
# ---------------------------------------------------------------------------

def test_legacy_database_migration_drops_paper_cascade(tmp_path, monkeypatch):
    import sqlite3

    monkeypatch.setattr(paperlab_practice, "postgres_enabled", lambda: False)
    path = tmp_path / "legacy.sqlite3"
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript("""
    CREATE TABLE users(id INTEGER PRIMARY KEY);
    CREATE TABLE pl_papers(id INTEGER PRIMARY KEY, title TEXT);
    CREATE TABLE pl_paper_attempts (
        id INTEGER PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        paper_id INTEGER NOT NULL REFERENCES pl_papers(id) ON DELETE CASCADE,
        paper_version INTEGER NOT NULL DEFAULT 1,
        status TEXT NOT NULL DEFAULT 'in_progress',
        answers_json TEXT NOT NULL DEFAULT '{}',
        score REAL NOT NULL DEFAULT 0,
        max_score REAL NOT NULL DEFAULT 0,
        detail_json TEXT NOT NULL DEFAULT '{}',
        started_at TEXT NOT NULL,
        submitted_at TEXT,
        created_at TEXT NOT NULL
    );
    CREATE TABLE pl_paper_answers (
        id INTEGER PRIMARY KEY,
        attempt_id INTEGER NOT NULL REFERENCES pl_paper_attempts(id) ON DELETE CASCADE,
        user_id INTEGER NOT NULL,
        item_position INTEGER NOT NULL,
        question_ref TEXT NOT NULL DEFAULT '',
        answer_json TEXT NOT NULL DEFAULT 'null',
        result TEXT NOT NULL DEFAULT 'pending',
        score REAL NOT NULL DEFAULT 0,
        max_score REAL NOT NULL DEFAULT 0,
        detail_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        UNIQUE(attempt_id,item_position)
    );
    """)
    con.execute("INSERT INTO users(id) VALUES(1)")
    con.execute("INSERT INTO pl_papers(id,title) VALUES(1,'旧库试卷')")
    con.execute(
        "INSERT INTO pl_paper_attempts(id,user_id,paper_id,status,answers_json,score,max_score,"
        "detail_json,started_at,created_at) VALUES(1,1,1,'submitted','{}',5,5,'{}','now','now')"
    )
    con.execute(
        "INSERT INTO pl_paper_answers(id,attempt_id,user_id,item_position,created_at) "
        "VALUES(1,1,1,1,'now')"
    )
    con.commit()

    paperlab_practice._migrate_attempt_paper_fk(con)
    fks = con.execute("PRAGMA foreign_key_list(pl_paper_attempts)").fetchall()
    assert not any(str(fk["table"]).lower() == "pl_papers" for fk in fks)
    con.execute("DELETE FROM pl_papers WHERE id=1")
    con.commit()
    assert con.execute("SELECT COUNT(*) FROM pl_paper_attempts").fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM pl_paper_answers").fetchone()[0] == 1
    # 迁移幂等：再跑一次不报错、不重复建表
    paperlab_practice._migrate_attempt_paper_fk(con)
    con.close()

