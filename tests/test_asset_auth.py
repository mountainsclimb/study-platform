"""P0.2 素材逐题授权：答案/解析图只对当前小题作答后可见，管理员预览走单独入口。"""
import json
from pathlib import Path

import pytest
from fastapi import HTTPException

from app import db, practice


def _insert_attempt(user_id: int, qid: str):
    from app import db as app_db
    with app_db.connection() as con:
        con.execute(
            "INSERT INTO attempts(user_id,qid,answer_text,image_path,result,grade_source,created_at,graded_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (user_id, qid, "A", None, "correct", "reference", app_db.utcnow(), app_db.utcnow()),
        )


def test_answer_asset_requires_the_same_sub_question(client, monkeypatch):
    user_id = client.get("/api/auth/me").json()["id"]
    monkeypatch.setattr(practice, "_question_group_key", lambda qid: "doc:g1" if qid else None)
    entry = {
        "asset_id": "asset-1",
        "refs": [
            {"qid": "doc:3", "group_id": None, "field": "answer", "permission": "after_attempt"},
        ],
    }
    # 未作答任何小题
    with pytest.raises(HTTPException) as caught:
        practice._asset_authorized(entry, "doc:3", user_id)
    assert caught.value.status_code == 403

    # 只作答第（2）问，不能解锁第（3）问的答案图
    _insert_attempt(user_id, "doc:2")
    with pytest.raises(HTTPException) as caught:
        practice._asset_authorized(entry, "doc:3", user_id)
    assert caught.value.status_code == 403
    # 拿第（2）问的 qid 去请求第（3）问的素材也不放行
    with pytest.raises(HTTPException) as caught:
        practice._asset_authorized(entry, "doc:2", user_id)
    assert caught.value.status_code == 403
    # 素材必须指明当前小题
    with pytest.raises(HTTPException) as caught:
        practice._asset_authorized(entry, None, user_id)
    assert caught.value.status_code == 403

    # 作答第（3）问后才放行
    _insert_attempt(user_id, "doc:3")
    practice._asset_authorized(entry, "doc:3", user_id)
    # 公共素材无需作答即可读
    practice._asset_authorized({"refs": [{"qid": "doc:9", "field": "stem", "permission": "public"}]}, None, user_id)


def test_group_shared_asset_follows_group_membership(client, monkeypatch):
    user_id = client.get("/api/auth/me").json()["id"]
    monkeypatch.setattr(practice, "_question_group_key", lambda qid: "doc:g1")
    entry = {
        "asset_id": "asset-2",
        "refs": [{"qid": None, "group_id": "doc:g1", "field": "shared", "permission": "public"}],
    }
    practice._asset_authorized(entry, "doc:7", user_id)  # 组内成员可读共用素材
    gated = {
        "asset_id": "asset-3",
        "refs": [{"qid": None, "group_id": "doc:g1", "field": "solution", "permission": "after_attempt"}],
    }
    with pytest.raises(HTTPException):
        practice._asset_authorized(gated, "doc:7", user_id)
    _insert_attempt(user_id, "doc:7")
    practice._asset_authorized(gated, "doc:7", user_id)


def test_conflicting_public_and_answer_refs_stay_gated(client, monkeypatch):
    """核收记录阻断项 2：同一文件既有公开材料引用又有答案引用时，答案关联仍受门禁。"""
    user_id = client.get("/api/auth/me").json()["id"]
    monkeypatch.setattr(practice, "_question_group_key", lambda qid: None)
    entry = {
        "asset_id": "conflict",
        "refs": [
            {"qid": "doc:16", "group_id": None, "field": "answer", "permission": "after_attempt"},
            {"qid": "doc:16", "group_id": None, "field": "legacy", "permission": "public"},
            {"qid": None, "group_id": "other:g1", "field": "material", "permission": "public"},
            {"qid": "other:3", "group_id": None, "field": "stem", "permission": "public"},
        ],
    }
    with pytest.raises(HTTPException) as caught:
        practice._asset_authorized(entry, "doc:16", user_id)
    assert caught.value.status_code == 403
    # 旧请求未带小题时也不能靠 legacy/公开引用绕过答案门禁
    with pytest.raises(HTTPException) as caught:
        practice._asset_authorized(entry, None, user_id)
    assert caught.value.status_code == 403
    _insert_attempt(user_id, "doc:16")
    practice._asset_authorized(entry, "doc:16", user_id)
    # 其他题的公开内容素材仍可读，且无需作答
    plain = {
        "asset_id": "plain",
        "refs": [
            {"qid": "other:4", "group_id": None, "field": "stem", "permission": "public"},
            {"qid": "other:4", "group_id": None, "field": "legacy", "permission": "public"},
        ],
    }
    practice._asset_authorized(plain, None, user_id)
    practice._asset_authorized(plain, "other:4", user_id)


def test_bank_image_version_and_asset_id_checks(client, monkeypatch):
    monkeypatch.setattr(practice, "_current_bank_version", lambda: "bank-test-r1")
    stale = client.get("/api/bank-image", params={
        "subject": "数学", "file": "images/not-exist.png", "bank_version": "bank-old-r0",
    })
    assert stale.status_code == 409
    assert "题库已更新" in stale.json()["detail"]


def _release_bank() -> Path:
    return Path(__file__).resolve().parents[2] / "高考题库_按学科" / "releases" / "bank-20261120-r1"


def _find_gated_asset(release: Path):
    manifest = release / "assets_manifest.jsonl"
    if not manifest.is_file():
        return None
    with manifest.open(encoding="utf-8") as stream:
        for line in stream:
            if "after_attempt" not in line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            for ref in row.get("refs") or []:
                if ref.get("permission") != "after_attempt" or not ref.get("qid"):
                    continue
                subject = (row.get("subjects") or [None])[0]
                path = next((p for p in row.get("paths") or [] if p.split("/", 1)[0] == subject), None)
                if not subject or not path:
                    continue
                relative = path.split("/", 1)[1]
                if (release / subject / relative).is_file():
                    return subject, relative, row.get("asset_id"), ref["qid"]
    return None


@pytest.mark.skipif(not _release_bank().is_dir(), reason="snapshot release package is not available")
def test_bank_image_route_authorizes_after_attempt(client, second_client, monkeypatch):
    release = _release_bank()
    found = _find_gated_asset(release)
    if not found:
        pytest.skip("no answer-only asset in the snapshot")
    subject, file, asset_id, qid = found
    monkeypatch.setattr(db, "BANK_DIR", release)
    user_id = client.get("/api/auth/me").json()["id"]
    params = {"subject": subject, "file": file, "qid": qid, "asset_id": asset_id}

    before = client.get("/api/bank-image", params=params)
    assert before.status_code == 403

    _insert_attempt(user_id, qid)
    after = client.get("/api/bank-image", params=params)
    assert after.status_code == 200
    # 素材 ID 与题目记录不一致时拒绝
    assert client.get("/api/bank-image", params={**params, "asset_id": "mismatch"}).status_code == 403
    # 管理员原卷预览用单独预览入口
    assert client.get("/api/bank-image", params={**params, "preview": "true"}).status_code == 200
    # 学生账号不能使用管理员预览入口
    assert second_client.get("/api/bank-image", params={**params, "preview": "true"}).status_code == 403


@pytest.mark.skipif(not _release_bank().is_dir(), reason="snapshot release package is not available")
def test_legacy_images_table_does_not_bypass_manifest_gating(client, monkeypatch):
    """核收记录阻断项 2：同一素材出现在旧 images 表时，仍按清单的答案引用门禁。"""
    release = _release_bank()
    subject = "政治"
    file = "images/4fc2f96f2588c180df89.png"
    qid = "5ed492c968851a7f:None#16"
    if not (release / subject / file).is_file():
        pytest.skip("conflict asset not available in the snapshot")
    monkeypatch.setattr(db, "BANK_DIR", release)
    user_id = client.get("/api/auth/me").json()["id"]
    params = {"subject": subject, "file": file, "qid": qid, "asset_id": "4fc2f96f2588c180df89"}
    assert client.get("/api/bank-image", params=params).status_code == 403
    _insert_attempt(user_id, qid)
    assert client.get("/api/bank-image", params=params).status_code == 200
