"""P1 题目报错与管理员校勘闭环：报错提交、审核链路、临时隔离与导出。"""
import io
import json

from PIL import Image, ImageDraw

from app import practice


def _question():
    with practice._bank() as con:
        ids = [r[0] for r in con.execute(
            "SELECT qid FROM questions WHERE objective=1 ORDER BY year DESC LIMIT 200"
        )]
    for qid in ids:
        question = practice.load_question(qid)
        blocking = [flag for flag in question["quality_flags"] if "参考答案待核验" not in flag]
        if question["practice_allowed"] and not blocking and question["has_answer"]:
            return question
    raise AssertionError("No suitable source question")


def _screenshot() -> bytes:
    image = Image.new("RGB", (240, 160), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((20, 20, 210, 130), outline="black", width=3)
    draw.line((30, 60, 190, 90), fill="red", width=3)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _report(client, qid, **overrides):
    data = {"field": "formula", "issue_type": "formula", "note": "公式分数线和根号显示不对"}
    data.update(overrides)
    response = client.post(f"/api/questions/{qid}/issues", data=data)
    assert response.status_code == 200, response.text
    return response.json()


def test_user_report_and_admin_review_chain(client, second_client):
    question = _question()
    qid = question["qid"]
    created = _report(client, qid)
    assert created["status"] == "pending_check"
    assert created["risk"] == "high"

    own = client.get(f"/api/questions/{qid}/issues").json()["items"]
    assert [item["id"] for item in own] == [created["id"]]
    assert own[0]["field_label"] == "公式"
    # 其他用户看不到别人的报错内容
    assert second_client.get(f"/api/questions/{qid}/issues").json()["items"] == []

    queue = client.get("/api/admin/content-issues", params={"status": "pending_check"}).json()
    assert any(item["id"] == created["id"] for item in queue["items"])
    item = next(item for item in queue["items"] if item["id"] == created["id"])
    assert item["before"]["bank_version"] == question["bank_version"]
    assert item["before"]["stem_excerpt"]
    assert item["reporter"] == "测试管理员"

    # 非管理员不能审核
    assert second_client.get("/api/admin/content-issues").status_code == 403
    assert second_client.post(
        f"/api/admin/content-issues/{created['id']}/transition", json={"status": "to_revise"},
    ).status_code == 403

    # 状态不能跳级
    skipped = client.post(
        f"/api/admin/content-issues/{created['id']}/transition", json={"status": "published"},
    )
    assert skipped.status_code == 409

    # 待核对 → 待修订（此步可只补原卷依据）
    revised = client.post(
        f"/api/admin/content-issues/{created['id']}/transition",
        json={"status": "to_revise", "evidence": "数学卷第20题第(1)问"},
    )
    assert revised.status_code == 200, revised.text
    assert revised.json()["status"] == "to_revise"
    assert revised.json()["reviewer"] == "测试管理员"

    # 待修订 → 待发布必须写修改说明
    missing_decision = client.post(
        f"/api/admin/content-issues/{created['id']}/transition", json={"status": "to_publish"},
    )
    assert missing_decision.status_code == 422

    to_publish = client.post(
        f"/api/admin/content-issues/{created['id']}/transition",
        json={"status": "to_publish", "decision": "恢复原卷 LaTeX 与原图"},
    )
    assert to_publish.status_code == 200

    # 待发布 → 已发布必须带拟修改内容
    no_after = client.post(
        f"/api/admin/content-issues/{created['id']}/transition",
        json={"status": "published", "decision": "恢复公式"},
    )
    assert no_after.status_code == 422

    published = client.post(
        f"/api/admin/content-issues/{created['id']}/transition",
        json={
            "status": "published",
            "decision": "恢复原卷 LaTeX 与原图",
            "evidence": "数学卷第20题第(1)问",
            "after": {"stem_blocks": [{"kind": "math", "latex": "\\frac{1}{2}"}]},
        },
    )
    assert published.status_code == 200, published.text
    assert published.json()["status"] == "published"
    assert published.json()["resolved_at"]

    exported = client.get("/api/admin/content-issues/export", params={"status": "published"})
    assert exported.status_code == 200
    rows = [json.loads(line) for line in exported.text.splitlines() if line.strip()]
    row = next(item for item in rows if item["issue_id"] == created["id"])
    assert row["qid"] == qid
    assert row["after"]["stem_blocks"][0]["latex"] == "\\frac{1}{2}"
    assert row["evidence"] == "数学卷第20题第(1)问"
    assert row["reviewer"] == "测试管理员"


def test_confirm_without_revision_still_closes(client):
    question = _question()
    created = _report(client, question["qid"], field="stem", issue_type="other", note="看起来没问题")
    confirmed = client.post(
        f"/api/admin/content-issues/{created['id']}/transition",
        json={"status": "confirmed", "decision": "核对原卷一致"},
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "confirmed"
    assert confirmed.json()["resolved_at"]


def test_publish_reuses_after_json_from_to_publish(client):
    """「待发布 → 已发布」复用 to_publish 阶段已提交的 after_json，不必重复提交。"""
    question = _question()
    created = _report(client, question["qid"], field="formula", issue_type="formula")
    url = f"/api/admin/content-issues/{created['id']}/transition"
    assert client.post(url, json={"status": "to_revise", "evidence": "原卷 p1"}).status_code == 200
    planned = {"stem_plain": "按原卷重排后的题干", "solution": "修订解析"}
    to_publish = client.post(
        url, json={"status": "to_publish", "decision": "按原卷重排", "after": planned},
    )
    assert to_publish.status_code == 200, to_publish.text
    assert to_publish.json()["after"] == planned
    published = client.post(url, json={"status": "published", "evidence": "数学卷 p1"})
    assert published.status_code == 200, published.text
    assert published.json()["status"] == "published"
    assert published.json()["after"] == planned, "发布应沿用待发布阶段提交的拟修改内容"
    exported = client.get("/api/admin/content-issues/export", params={"status": "published"}).text
    rows = [json.loads(line) for line in exported.splitlines() if line.strip()]
    assert next(row for row in rows if row["issue_id"] == created["id"])["after"] == planned


def test_publish_without_any_after_json_still_rejected(client):
    """没有既有 after_json 时，发布门禁不放松（原有 422 行为保留）。"""
    question = _question()
    created = _report(client, question["qid"], field="stem", issue_type="other")
    url = f"/api/admin/content-issues/{created['id']}/transition"
    assert client.post(url, json={"status": "to_revise"}).status_code == 200
    assert client.post(url, json={"status": "to_publish", "decision": "改写题干"}).status_code == 200
    missing = client.post(url, json={"status": "published", "evidence": "原卷 p2"})
    assert missing.status_code == 422


def test_isolation_blocks_practice_and_is_revocable(client, second_client):
    question = _question()
    qid = question["qid"]
    created = _report(client, qid, field="options", issue_type="option", note="选项 D 被并入 C")

    missing_reason = client.post(f"/api/admin/questions/{qid}/isolation", json={"active": True})
    assert missing_reason.status_code == 422

    isolated = client.post(
        f"/api/admin/questions/{qid}/isolation",
        json={"active": True, "reason": "原卷选项 D 缺失，待校勘", "issue_id": created["id"]},
    )
    assert isolated.status_code == 200

    detail = client.get(f"/api/questions/{qid}")
    assert detail.status_code == 409
    assert "隔离" in detail.json()["detail"]
    assert second_client.post("/api/attempts", data={"qid": qid, "answer": "A"}).status_code == 409

    clause, params = practice._excluded_sql("q")
    assert qid in params

    revoked = client.post(
        f"/api/admin/questions/{qid}/isolation", json={"active": False, "issue_id": created["id"]},
    )
    assert revoked.status_code == 200
    assert client.get(f"/api/questions/{qid}").status_code == 200


def test_report_screenshot_permissions(client, second_client):
    question = _question()
    response = client.post(
        f"/api/questions/{question['qid']}/issues",
        data={"field": "image", "issue_type": "image", "note": "配图缺失"},
        files={"screenshot": ("shot.png", _screenshot(), "image/png")},
    )
    assert response.status_code == 200, response.text
    issue_id = response.json()["id"]
    admin_view = client.get(f"/api/issue-image/{issue_id}")
    assert admin_view.status_code == 200
    assert admin_view.headers["content-type"].startswith("image/jpeg")
    # 其他用户不可见
    assert second_client.get(f"/api/issue-image/{issue_id}").status_code == 403
    # 报错记录里带有截图入口
    queue = client.get("/api/admin/content-issues", params={"qid": question["qid"]}).json()
    item = next(item for item in queue["items"] if item["id"] == issue_id)
    assert item["screenshot_url"] == f"/api/issue-image/{issue_id}"


def test_published_issue_reports_revision_state(client, tmp_path, monkeypatch):
    """P1：已发布修订区分「待进入构建」与「已生效于版本」。

    用独立题库副本覆盖两种状态，不依赖当前 STUDY_BANK_DIR 是否已带 revision_report。
    """
    import shutil

    from app import db
    from app import practice as practice_mod

    bank = tmp_path / "bank-copy"
    bank.mkdir()
    shutil.copyfile(db.BANK_DIR / "题库.db", bank / "题库.db")
    for folder in ("数学", "语文", "英语", "物理", "化学", "生物", "政治", "历史", "地理"):
        source = db.BANK_DIR / folder
        if source.is_dir():
            shutil.copytree(source, bank / folder, dirs_exist_ok=True)
    monkeypatch.setattr(db, "BANK_DIR", bank)
    practice_mod._revision_report.cache_clear()
    practice_mod._sqlite_question_columns.cache_clear()
    practice_mod._sqlite_group_columns.cache_clear()
    practice_mod._source_groups.cache_clear()

    question = _question()
    created = _report(client, question["qid"])
    for status in ("to_revise", "to_publish"):
        client.post(f"/api/admin/content-issues/{created['id']}/transition",
                    json={"status": status, "decision": "按原卷修订", "evidence": "原卷 p12"})
    published = client.post(
        f"/api/admin/content-issues/{created['id']}/transition",
        json={"status": "published", "decision": "恢复公式", "evidence": "原卷 p12",
              "after": {"stem_plain": "修订后的题干"}},
    )
    assert published.status_code == 200

    # 当前题库没有 revision_report.json → 显示「待进入构建」
    queue = client.get("/api/admin/content-issues", params={"status": "published"}).json()
    item = next(row for row in queue["items"] if row["id"] == created["id"])
    assert item["revision"]["state"] == "pending_build"
    assert queue["revisions"]["present"] is False
    report = client.get("/api/admin/revisions").json()
    assert report["present"] is False and "没有校勘应用报告" in report["notice"]

    # 在同一副本放入 revision_report → 显示「已生效于版本」
    report_data = {
        "bank_version": "bank-test-r9",
        "counts": {"applied": 1, "conflict": 0, "pending": 0},
        "applied": [{
            "qid": question["qid"], "issue_id": created["id"],
            "applied_fields": ["stem_plain"], "after_revision": "abcdef123456",
        }],
    }
    (bank / "revision_report.json").write_text(json.dumps(report_data), encoding="utf-8")
    practice_mod._revision_report.cache_clear()
    applied = client.get("/api/admin/content-issues", params={"status": "published"}).json()
    item = next(row for row in applied["items"] if row["id"] == created["id"])
    assert item["revision"]["state"] == "applied"
    assert item["revision"]["applied_in"] == "bank-test-r9"
    assert item["revision"]["after_revision"] == "abcdef123456"
    info = client.get("/api/admin/revisions").json()
    assert info["present"] is True and info["bank_version"] == "bank-test-r9"
    practice_mod._revision_report.cache_clear()
