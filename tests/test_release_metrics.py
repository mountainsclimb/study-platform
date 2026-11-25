"""发布指标：上传完成率与复习回流率（``/api/admin/release-metrics``）。

口径（``docs/知序全面审核与下一版优化方案.md`` §8 发布跟踪）：

* **上传完成率** = 成功校对入库数 ÷ 发起上传数，区分图片（拍照/OCR）与文本（手录/粘贴）
  两条通道；``confirmed_at`` 有值但 ``saved_at`` 为空且确认超过 ``STALE_CONFIRM_HOURS``
  的小时数计入 ``stalled``（校对完成却放弃保存，是最需要运营介入的一步）。
* **复习回流率** = 已进入待复习队列的错题 ÷ 未归档错题；分母为 0 时返回 ``None``
  而不是 0，避免「没有数据」被读成「0% 完成」。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app import db, release_metrics, study_review
from app.ai import upload_pipeline


def _user_id(client) -> int:
    response = client.get("/api/auth/me")
    assert response.status_code == 200, response.text
    return response.json()["id"]


def _iso(delta_hours: float = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=delta_hours)).isoformat()


def _ensure_upload_schema() -> None:
    """真实流程里 ``_ensure_intake_schema`` 在上传/保存时补齐入库字段；测试里显式调用一次。"""
    upload_pipeline._ensure_intake_schema()


def _insert_upload(
    user_id: int,
    *,
    media_type: str = "image/png",
    status: str = "uploaded",
    created_at: str | None = None,
    confirmed_at: str | None = None,
    saved_at: str | None = None,
) -> int:
    _ensure_upload_schema()
    with db.connection() as con:
        cursor = con.execute(
            "INSERT INTO ai_uploads(user_id,filename,storage_name,media_type,text_content,"
            "extra_files_json,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (user_id, "a.png", "s.png", media_type, "", "[]", status, created_at or _iso()),
        )
        upload_id = int(cursor.lastrowid)
        # 入库留痕字段由 upload_pipeline._ensure_intake_schema 补齐，这里直接写以模拟真实流程。
        for column, value in (("confirmed_at", confirmed_at), ("saved_at", saved_at)):
            if value is not None:
                con.execute(
                    "UPDATE ai_uploads SET %s=? WHERE id=?" % column, (value, upload_id),
                )
    return upload_id


def _add_wrong(user_id: int, qid: str, *, archived: int = 0, added_at: str | None = None) -> None:
    moment = added_at or _iso()
    with db.connection() as con:
        con.execute(
            "INSERT INTO wrong_questions(user_id,qid,reason,next_review_at,added_at,archived) "
            "VALUES(?,?,?,?,?,?)",
            (user_id, qid, "答题错误", moment, moment, archived),
        )


def _add_card(user_id: int, ref: str, *, archived: int = 0) -> None:
    moment = datetime.now(timezone.utc)
    with db.connection() as con:
        study_review._insert_card(
            con,
            user_id=user_id,
            card_key="q:" + ref,
            card_type="wrong",
            ref=ref,
            due_at=_iso(),
            source_kind="migrated",
            archived=archived,
            moment=moment,
        )


@pytest.fixture()
def admin_client(client):
    """把首个注册账号提升为管理员（``require_admin`` 只看 ``users.role``）。"""
    user_id = _user_id(client)
    with db.connection() as con:
        con.execute("UPDATE users SET role='admin' WHERE id=?", (user_id,))
    return client


def test_release_metrics_requires_admin(second_client):
    # 非公网模式下首个注册账号即管理员，普通用户用第二个账号（second_client 挂在 client 之上）。
    response = second_client.get("/api/admin/release-metrics")
    assert response.status_code in (401, 403), response.text


def test_upload_completion_rate_by_channel(admin_client):
    user_id = _user_id(admin_client)
    # 图片通道：3 次发起，1 次入库 → 33.3%
    _insert_upload(user_id, media_type="image/png", status="saved", saved_at=_iso())
    _insert_upload(user_id, media_type="image/jpeg", status="recognized")
    _insert_upload(user_id, media_type="image/png")
    # 文本通道：2 次发起，1 次入库 → 50.0%
    _insert_upload(user_id, media_type="text/plain", status="saved", saved_at=_iso())
    _insert_upload(user_id, media_type="text/plain")

    body = admin_client.get("/api/admin/release-metrics").json()
    upload = body["upload"]
    assert upload["available"] is True
    assert upload["days"] == 30
    assert upload["total"]["started"] == 5
    assert upload["total"]["saved"] == 2
    assert upload["total"]["completion_rate"] == 40.0
    assert upload["channels"]["image"]["started"] == 3
    assert upload["channels"]["image"]["saved"] == 1
    assert upload["channels"]["image"]["completion_rate"] == 33.3
    assert upload["channels"]["text"]["started"] == 2
    assert upload["channels"]["text"]["completion_rate"] == 50.0
    assert "发起上传数" in body["caliber"]["upload"]


def test_upload_confirmed_but_not_saved_marks_stalled(admin_client):
    user_id = _user_id(admin_client)
    # 确认后 1 小时未入库：还没到滞留阈值
    _insert_upload(user_id, status="confirmed", confirmed_at=_iso(-1))
    # 确认后 48 小时仍未入库：计入滞留
    _insert_upload(user_id, status="confirmed", confirmed_at=_iso(-48))

    total = admin_client.get("/api/admin/release-metrics").json()["upload"]["total"]
    assert total["confirmed"] == 2
    assert total["saved"] == 0
    assert total["stalled"] == 1
    assert total["completion_rate"] == 0.0


def test_upload_metrics_empty_is_none_not_zero(admin_client):
    upload = admin_client.get("/api/admin/release-metrics").json()["upload"]
    assert upload["total"]["started"] == 0
    # 无样本 → None（前端显示「—」），不能是 0.0
    assert upload["total"]["completion_rate"] is None


def test_upload_window_excludes_old_rows(admin_client):
    user_id = _user_id(admin_client)
    _insert_upload(user_id, status="saved", saved_at=_iso(), created_at=_iso(-24 * 40))
    _insert_upload(user_id, status="saved", saved_at=_iso())
    body = admin_client.get("/api/admin/release-metrics?days=30").json()
    assert body["upload"]["total"]["started"] == 1
    wide = admin_client.get("/api/admin/release-metrics?days=90").json()
    assert wide["upload"]["total"]["started"] == 2


def test_review_return_rate(admin_client):
    user_id = _user_id(admin_client)
    for index in range(3):
        _add_wrong(user_id, "rwk:rm-%d" % index)
    # 只给前两道生成复习卡（第三道用户还没处理）
    _add_card(user_id, "bank:rwk:rm-0")
    _add_card(user_id, "bank:rwk:rm-1")

    review = admin_client.get("/api/admin/release-metrics").json()["review"]
    assert review["eligible"] == 3
    assert review["scheduled"] == 2
    assert review["rate"] == 66.7
    assert "待复习队列" in review["note"]


def test_review_return_rate_ignores_archived(admin_client):
    user_id = _user_id(admin_client)
    _add_wrong(user_id, "rwk:archived-1", archived=1)
    _add_wrong(user_id, "rwk:archived-2")
    _add_card(user_id, "bank:rwk:archived-1")

    review = admin_client.get("/api/admin/release-metrics").json()["review"]
    assert review["eligible"] == 1
    assert review["scheduled"] == 0
    assert review["rate"] == 0.0


def test_review_return_rate_none_without_eligible(admin_client):
    review = admin_client.get("/api/admin/release-metrics").json()["review"]
    assert review["eligible"] == 0
    assert review["rate"] is None


def test_review_stats_personal_question_refs(admin_client):
    """个人题错题带 ``app:`` 前缀，也要能和复习卡的 ``app:<id>`` 引用对上。"""
    user_id = _user_id(admin_client)
    _add_wrong(user_id, "app:999")
    _add_card(user_id, "app:999")

    review = admin_client.get("/api/admin/release-metrics").json()["review"]
    assert review["eligible"] == 1
    assert review["scheduled"] == 1
    assert review["rate"] == 100.0


def test_review_stats_archived_cards_not_counted(admin_client):
    user_id = _user_id(admin_client)
    _add_wrong(user_id, "rwk:card-archived")
    _add_card(user_id, "bank:rwk:card-archived", archived=1)

    review = admin_client.get("/api/admin/release-metrics").json()["review"]
    assert review["eligible"] == 1
    assert review["scheduled"] == 0


def test_release_metrics_caliber_is_explained(admin_client):
    body = admin_client.get("/api/admin/release-metrics").json()
    caliber = body["caliber"]
    assert caliber["stale_confirm_hours"] == release_metrics.STALE_CONFIRM_HOURS
    assert caliber["upload"]
    assert caliber["review"]
    assert body["generated_at"]


def test_days_query_is_bounded(admin_client):
    assert admin_client.get("/api/admin/release-metrics?days=0").status_code == 422
    assert admin_client.get("/api/admin/release-metrics?days=999").status_code == 422
