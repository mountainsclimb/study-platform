"""学习附件共用模块（``app/study_artifact.py``）测试。

本文件不依赖 ``app.main``：用本地 ``FastAPI()`` 只挂载 ``study_artifact.router``，并通过
``app.dependency_overrides[current_user]`` 注入当前用户，覆盖：

* 创建 / 授权读取 / 中文与英文 JSON 原样往返（``ensure_ascii=False``）与 checksum 口径；
* 版本不可变、``expected_revision`` CAS 409、仅改标题不新增 revision；
* 关联 ``revision=NULL`` 跟随最新版 vs 固定版本（新增 revision 后仍读旧版）与重链；
* 跨用户 404 且与「不存在」同一文案、软删除后读取 404 / 列表排除 / 历史与关联保留；
* ``kind`` / ``target_kind`` 白名单、限额（256 KiB / 64 KiB / 标题 120 / 关联 20）、
  重复 ``artifact_id`` 409 与列表分页过滤。
"""
import hashlib
import json

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest

from app import db
from app import study_artifact
from app.auth import current_user

USER_A = {"id": 101, "username": "甲同学", "role": "student"}
USER_B = {"id": 102, "username": "乙同学", "role": "student"}

PUBLIC_KEYS = {"artifact_id", "kind", "title", "current_revision", "updated_at", "source_ref"}


# ---------------------------------------------------------------------------
# 夹具与工具
# ---------------------------------------------------------------------------


@pytest.fixture
def artifacts(isolated_db):
    """隔离应用库 + 本地 FastAPI（不依赖 app.main），返回 (TestClient, 用户持有器)。"""
    study_artifact.init_study_artifacts()
    app = FastAPI()
    app.include_router(study_artifact.router)
    holder = {"user": dict(USER_A)}
    app.dependency_overrides[current_user] = lambda: holder["user"]
    client = TestClient(app)
    try:
        yield client, holder
    finally:
        client.close()


def test_direct_functions_lazily_create_tables(isolated_db):
    """未经 ``init_study_artifacts`` 直接调用公开函数也会懒建表（接线与集成测试更稳）。"""
    created = study_artifact.create_artifact(
        USER_A["id"], kind="flow_animation", title="流程动画", data={"steps": []}
    )
    artifact_id = created["artifact"]["artifact_id"]
    assert created["artifact"]["current_revision"] == 1
    assert study_artifact.get_artifact(USER_A["id"], artifact_id)["revision"]["revision"] == 1
    with db.connection() as con:
        tables = {
            row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert {"study_artifacts", "study_artifact_revisions", "study_artifact_links"} <= tables


def _switch_user(holder, user) -> None:
    holder["user"] = dict(user)


def _create(client, **overrides):
    payload = {
        "kind": "function_graph",
        "title": "二次函数图像",
        "data": {"expression": "y=x^2"},
    }
    payload.update(overrides)
    return client.post("/api/artifacts", json=payload)


def _created(client, **overrides) -> dict:
    response = _create(client, **overrides)
    assert response.status_code == 201, response.text
    return response.json()


def _aid(payload: dict) -> str:
    return payload["artifact"]["artifact_id"]


def _checksum(data) -> str:
    serialized = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _patch_data(client, artifact_id: str, data, **extra):
    body = {"data": data}
    body.update(extra)
    return client.patch(f"/api/artifacts/{artifact_id}", json=body)


def _target(client, kind: str, key: str) -> dict:
    response = client.get(
        "/api/artifacts/by-target", params={"target_kind": kind, "target_key": key}
    )
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# 建表与创建 / 读取
# ---------------------------------------------------------------------------


def test_init_study_artifacts_is_idempotent(isolated_db):
    """建表幂等：重复调用不报错，三张表与关键列齐全。"""
    study_artifact.init_study_artifacts()
    study_artifact.init_study_artifacts()
    with db.connection() as con:
        tables = {
            row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        artifact_columns = {
            row[1] for row in con.execute("PRAGMA table_info(study_artifacts)")
        }
        revision_columns = {
            row[1] for row in con.execute("PRAGMA table_info(study_artifact_revisions)")
        }
        link_columns = {
            row[1] for row in con.execute("PRAGMA table_info(study_artifact_links)")
        }
    assert {"study_artifacts", "study_artifact_revisions", "study_artifact_links"} <= tables
    assert {
        "id", "user_id", "artifact_id", "kind", "title", "source_ref", "source_kind",
        "current_revision", "published_revision", "visibility", "created_at", "updated_at",
        "deleted_at",
    } <= artifact_columns
    assert {
        "id", "artifact_id", "revision", "data_json", "preview_json", "checksum",
        "author_id", "created_at",
    } <= revision_columns
    assert {
        "id", "user_id", "artifact_id", "target_kind", "target_key", "revision", "created_at",
    } <= link_columns


def test_create_get_roundtrip_and_public_projection(artifacts):
    """中文 + 英文 JSON 原样往返，checksum 口径稳定，公开投影不暴露归属与内容。"""
    client, _holder = artifacts
    data = {
        "caption": "二次函数顶点式（中文）",
        "expression": "y = a(x - h)^2 + k",
        "points": [{"x": 0, "y": 1.5}, {"x": 2, "y": "顶点"}],
        "labels": ["顶点", "vertex", "对称轴"],
    }
    preview = {"type": "graph", "label": "函数图像预览", "note": "English preview"}
    body = _created(
        client,
        data=data,
        preview=preview,
        title="二次函数图像",
        source_ref="app:12",
        source_kind="question",
    )
    artifact, revision = body["artifact"], body["revision"]
    artifact_id = _aid(body)
    assert study_artifact.ARTIFACT_ID_RE.match(artifact_id)
    assert len(artifact_id) == 24  # secrets.token_hex(12)
    assert artifact["kind"] == "function_graph"
    assert artifact["title"] == "二次函数图像"
    assert artifact["current_revision"] == 1
    assert artifact["source_ref"] == "app:12" and artifact["source_kind"] == "question"
    assert artifact["visibility"] == "private"
    assert "user_id" not in artifact and "data" not in artifact
    assert revision["revision"] == 1
    assert revision["data"] == data
    assert revision["preview"] == preview
    assert revision["checksum"] == _checksum(data)
    assert revision["author_id"] == USER_A["id"]
    assert revision["created_at"]

    fetched = client.get(f"/api/artifacts/{artifact_id}")
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["artifact"]["title"] == "二次函数图像"
    assert fetched.json()["revision"]["data"] == data
    assert fetched.json()["revision"]["preview"] == preview

    with db.connection() as con:
        stored = con.execute(
            "SELECT data_json, checksum FROM study_artifact_revisions WHERE artifact_id="
            "(SELECT id FROM study_artifacts WHERE artifact_id=?)",
            (artifact_id,),
        ).fetchone()
    expected_json = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert stored["data_json"] == expected_json
    # ensure_ascii=False：中文以原文存储，不出现 \\uXXXX 转义。
    assert "二次函数顶点式" in stored["data_json"]
    assert "\\u4e8c" not in stored["data_json"]
    assert stored["checksum"] == _checksum(data)

    public = study_artifact.preview_public(artifact)
    assert set(public) == PUBLIC_KEYS
    assert public["artifact_id"] == artifact_id
    assert "user_id" not in public and "data" not in public
    with db.connection() as con:
        row = con.execute(
            "SELECT * FROM study_artifacts WHERE artifact_id=?", (artifact_id,)
        ).fetchone()
    assert study_artifact.preview_public(row) == public

    listed = client.get("/api/artifacts")
    assert listed.status_code == 200
    page_payload = listed.json()
    assert page_payload["total"] == 1
    assert page_payload["page"] == 1 and page_payload["page_size"] == 20
    assert set(page_payload["items"][0]) == PUBLIC_KEYS
    assert "data" not in page_payload["items"][0]

    # 纯英文 JSON 同样原样往返（数值 / 布尔 / null / 嵌套数组）。
    english = {
        "caption": "Vertex form of a quadratic",
        "expression": "y = a(x - h)^2 + k",
        "enabled": True,
        "scale": 1.25,
        "anchor": None,
        "points": [{"x": 0, "y": 1}, {"x": -1.5, "y": 2}],
    }
    english_created = _created(
        client, data=english, title="English artifact", kind="science_demo"
    )
    english_fetched = client.get(
        f"/api/artifacts/{_aid(english_created)}"
    ).json()["revision"]
    assert english_fetched["data"] == english
    assert english_fetched["checksum"] == _checksum(english)
    assert english_created["revision"]["checksum"] == _checksum(english)


def test_create_generates_artifact_id_and_accepts_custom_one(artifacts):
    """不传 artifact_id 时服务端生成；传入合法 ID 时使用调用方 ID。"""
    client, _holder = artifacts
    generated = _created(client)
    assert study_artifact.ARTIFACT_ID_RE.match(_aid(generated))
    custom = _created(client, artifact_id="lesson-graph-0001")
    assert _aid(custom) == "lesson-graph-0001"
    for bad in ("SHORT", "UPPER-ID-1234", "bad id 12345", "1"):
        response = _create(client, artifact_id=bad)
        assert response.status_code == 422, (bad, response.text)
        assert "附件 ID 格式不正确" in response.json()["detail"]


# ---------------------------------------------------------------------------
# 版本语义
# ---------------------------------------------------------------------------


def test_history_immutable_and_expected_revision_conflict(artifacts):
    """每次内容变化新增不可变版本；CAS 不一致返回 409 且不写入。"""
    client, _holder = artifacts
    first = {"caption": "第一版", "x": 1}
    created = _created(client, data=first)
    artifact_id = _aid(created)

    second = {"caption": "第二版", "x": 2}
    updated = _patch_data(client, artifact_id, second, expected_revision=1)
    assert updated.status_code == 200, updated.text
    assert updated.json()["artifact"]["current_revision"] == 2
    assert updated.json()["revision"]["revision"] == 2
    assert updated.json()["revision"]["data"] == second
    assert updated.json()["revision"]["checksum"] == _checksum(second)

    old_version = client.get(f"/api/artifacts/{artifact_id}", params={"revision": 1})
    assert old_version.status_code == 200
    assert old_version.json()["revision"]["data"] == first
    assert old_version.json()["revision"]["checksum"] == _checksum(first)

    latest = client.get(f"/api/artifacts/{artifact_id}").json()["revision"]
    assert latest["revision"] == 2 and latest["data"] == second

    history = client.get(f"/api/artifacts/{artifact_id}/revisions").json()
    assert history["total"] == 2
    assert [item["revision"] for item in history["items"]] == [2, 1]
    assert history["items"][0]["checksum"] == _checksum(second)
    assert all(item["author_id"] == USER_A["id"] for item in history["items"])

    stale = _patch_data(client, artifact_id, {"caption": "过期写入"}, expected_revision=1)
    assert stale.status_code == 409
    assert "附件已被更新，请刷新后重试" in stale.json()["detail"]
    assert client.get(f"/api/artifacts/{artifact_id}").json()["artifact"]["current_revision"] == 2
    assert client.get(f"/api/artifacts/{artifact_id}/revisions").json()["total"] == 2

    assert client.get(f"/api/artifacts/{artifact_id}", params={"revision": 99}).status_code == 404


def test_add_revision_keeps_title_when_not_given(artifacts):
    """追加版本不传 title 时保留原标题；传 title 时一并更新。"""
    client, _holder = artifacts
    created = _created(client, title="原标题")
    artifact_id = _aid(created)
    renamed = _patch_data(client, artifact_id, {"v": 2}, expected_revision=1, title="新标题")
    assert renamed.status_code == 200
    assert renamed.json()["artifact"]["title"] == "新标题"
    assert renamed.json()["revision"]["revision"] == 2
    kept = _patch_data(client, artifact_id, {"v": 3}, expected_revision=2)
    assert kept.status_code == 200
    assert kept.json()["artifact"]["title"] == "新标题"


def test_title_update_does_not_create_revision(artifacts):
    """仅改标题不新增 revision（历史条数不变）。"""
    client, _holder = artifacts
    created = _created(client, title="旧标题", data={"v": 1})
    artifact_id = _aid(created)

    response = client.patch(f"/api/artifacts/{artifact_id}", json={"title": "  新标题  "})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["artifact"]["title"] == "新标题"
    assert payload["artifact"]["current_revision"] == 1
    assert payload["revision"]["revision"] == 1
    assert payload["revision"]["data"] == {"v": 1}
    assert client.get(f"/api/artifacts/{artifact_id}/revisions").json()["total"] == 1

    assert client.patch(f"/api/artifacts/{artifact_id}", json={}).status_code == 422
    assert client.patch(f"/api/artifacts/{artifact_id}", json={"title": "   "}).status_code == 422
    assert client.patch(f"/api/artifacts/{artifact_id}", json={"title": None}).status_code == 422


# ---------------------------------------------------------------------------
# 关联：跟随最新 vs 固定版本
# ---------------------------------------------------------------------------


def test_links_follow_latest_and_fixed_revision(artifacts):
    """links.revision=NULL 跟随最新版；固定版本在新增 revision 后仍读旧版。"""
    client, _holder = artifacts
    first = {"version": "v1", "说明": "初版"}
    created = _created(
        client,
        data=first,
        links=[{"target_kind": "question", "target_key": "q-1"}],
    )
    artifact_id = _aid(created)

    second = {"version": "v2", "说明": "第二版"}
    assert _patch_data(client, artifact_id, second, expected_revision=1).status_code == 200

    followed = _target(client, "question", "q-1")
    assert followed["total"] == 1 and followed["skipped"] == 0
    item = followed["items"][0]
    assert item["revision"] == 2 and item["data"] == second
    assert item["link"]["revision"] is None
    assert item["link"]["target_kind"] == "question"
    assert "user_id" not in item["artifact"]
    assert set(item["artifact"]) == PUBLIC_KEYS

    fixed = client.post(
        f"/api/artifacts/{artifact_id}/links",
        json={"target_kind": "note", "target_key": "n-7", "revision": 1},
    )
    assert fixed.status_code == 200, fixed.text
    assert fixed.json()["link"]["revision"] == 1
    assert fixed.json()["link"]["revision"] == 1

    third = {"version": "v3", "说明": "第三版"}
    assert _patch_data(client, artifact_id, third, expected_revision=2).status_code == 200

    fixed_read = _target(client, "note", "n-7")
    assert fixed_read["total"] == 1
    assert fixed_read["items"][0]["revision"] == 1
    assert fixed_read["items"][0]["data"] == first
    assert fixed_read["items"][0]["link"]["revision"] == 1

    latest_read = _target(client, "question", "q-1")
    assert latest_read["items"][0]["revision"] == 3
    assert latest_read["items"][0]["data"] == third

    # 重链同一目标：改为跟随最新版，不新增关联记录。
    relinked = client.post(
        f"/api/artifacts/{artifact_id}/links",
        json={"target_kind": "note", "target_key": "n-7", "revision": None},
    )
    assert relinked.status_code == 200
    assert relinked.json()["link"]["revision"] is None
    assert _target(client, "note", "n-7")["items"][0]["revision"] == 3

    # 固定到不存在的版本 → 422
    bad = client.post(
        f"/api/artifacts/{artifact_id}/links",
        json={"target_kind": "review", "target_key": "r-1", "revision": 9},
    )
    assert bad.status_code == 422
    assert "关联版本不存在" in bad.json()["detail"]


def test_link_and_unlink_endpoints(artifacts):
    """关联 / 解除关联接口：解除后按目标查询为空，重复解除 404。"""
    client, _holder = artifacts
    artifact_id = _aid(_created(client))

    linked = client.post(
        f"/api/artifacts/{artifact_id}/links",
        json={"target_kind": "classroom", "target_key": "cls-1", "revision": 1},
    )
    assert linked.status_code == 200, linked.text
    assert linked.json()["link"]["target_key"] == "cls-1"
    assert _target(client, "classroom", "cls-1")["total"] == 1

    unlinked = client.request(
        "DELETE",
        f"/api/artifacts/{artifact_id}/links",
        json={"target_kind": "classroom", "target_key": "cls-1"},
    )
    assert unlinked.status_code == 200, unlinked.text
    assert unlinked.json()["deleted"] is True
    assert _target(client, "classroom", "cls-1")["total"] == 0

    again = client.request(
        "DELETE",
        f"/api/artifacts/{artifact_id}/links",
        json={"target_kind": "classroom", "target_key": "cls-1"},
    )
    assert again.status_code == 404

    bad_kind = client.post(
        f"/api/artifacts/{artifact_id}/links",
        json={"target_kind": "paper", "target_key": "p-1"},
    )
    assert bad_kind.status_code == 422
    assert "关联目标类型不正确" in bad_kind.json()["detail"]


# ---------------------------------------------------------------------------
# 权限：跨用户 404 不泄露存在性
# ---------------------------------------------------------------------------


def test_cross_user_access_is_404_without_leaking(artifacts):
    """他人附件与不存在附件同样 404（同一文案），列表/按目标查询也看不到。"""
    client, holder = artifacts
    created = _created(
        client,
        data={"secret": "甲的私有附件"},
        links=[{"target_kind": "question", "target_key": "q-9"}],
    )
    artifact_id = _aid(created)

    _switch_user(holder, USER_B)
    missing = client.get("/api/artifacts/missing-artifact-01")
    forbidden = client.get(f"/api/artifacts/{artifact_id}")
    assert missing.status_code == 404 and forbidden.status_code == 404
    assert missing.json()["detail"] == forbidden.json()["detail"]

    assert client.patch(
        f"/api/artifacts/{artifact_id}", json={"data": {"x": 1}}
    ).status_code == 404
    assert client.delete(f"/api/artifacts/{artifact_id}").status_code == 404
    assert client.get(f"/api/artifacts/{artifact_id}/revisions").status_code == 404
    assert client.post(
        f"/api/artifacts/{artifact_id}/links",
        json={"target_kind": "note", "target_key": "n-1"},
    ).status_code == 404
    assert client.get("/api/artifacts").json()["total"] == 0
    assert _target(client, "question", "q-9")["total"] == 0

    # 全局唯一 artifact_id：他人已用也不能复用（409）。
    duplicate = _create(client, artifact_id=artifact_id)
    assert duplicate.status_code == 409

    # 甲仍可正常读取自己的附件。
    _switch_user(holder, USER_A)
    assert client.get(f"/api/artifacts/{artifact_id}").status_code == 200
    assert client.get("/api/artifacts").json()["total"] == 1


# ---------------------------------------------------------------------------
# 软删除
# ---------------------------------------------------------------------------


def test_soft_delete_hides_reads_but_keeps_history_and_links(artifacts):
    """软删除后读取/改版/列表/按目标均不可见，数据库仍保留历史版本与关联。"""
    client, _holder = artifacts
    created = _created(
        client,
        data={"v": 1},
        links=[{"target_kind": "question", "target_key": "q-del"}],
    )
    artifact_id = _aid(created)
    assert _patch_data(client, artifact_id, {"v": 2}, expected_revision=1).status_code == 200

    deleted = client.delete(f"/api/artifacts/{artifact_id}")
    assert deleted.status_code == 200, deleted.text
    assert deleted.json() == {"deleted": True, "artifact_id": artifact_id}

    assert client.get(f"/api/artifacts/{artifact_id}").status_code == 404
    assert client.get(f"/api/artifacts/{artifact_id}/revisions").status_code == 404
    assert client.get("/api/artifacts").json()["total"] == 0
    assert _patch_data(client, artifact_id, {"v": 3}).status_code == 404
    assert client.delete(f"/api/artifacts/{artifact_id}").status_code == 404

    by_target = _target(client, "question", "q-del")
    assert by_target["total"] == 0
    assert by_target["skipped"] == 1
    assert by_target["items"] == []

    # 历史与关联记录仍保留（供备份/审计/冻结引用核对）。
    with db.connection() as con:
        row = con.execute(
            "SELECT id, deleted_at FROM study_artifacts WHERE artifact_id=?", (artifact_id,)
        ).fetchone()
        assert row["deleted_at"]
        revisions = con.execute(
            "SELECT COUNT(*) FROM study_artifact_revisions WHERE artifact_id=?", (row["id"],)
        ).fetchone()[0]
        links = con.execute(
            "SELECT COUNT(*) FROM study_artifact_links WHERE artifact_id=?", (row["id"],)
        ).fetchone()[0]
    assert revisions == 2
    assert links == 1

    # 已删除附件的 ID 不能复用（唯一约束保留）。
    assert _create(client, artifact_id=artifact_id).status_code == 409


# ---------------------------------------------------------------------------
# 参数校验、限额与重复 ID
# ---------------------------------------------------------------------------


def test_invalid_kinds_and_payloads_are_422(artifacts):
    """kind / target_kind 白名单与 JSON 对象约束返回 422（中文 detail）。"""
    client, _holder = artifacts

    bad_kind = _create(client, kind="video")
    assert bad_kind.status_code == 422
    assert "附件类型不正确" in bad_kind.json()["detail"]

    assert _create(client, data=[1, 2, 3]).status_code == 422
    assert _create(client, data="not-an-object").status_code == 422
    assert _create(client, preview="text-preview").status_code == 422
    # 非标准 JSON 数值（NaN）不得落库：稳定序列化拒绝，避免响应渲染失败。
    nan_body = '{"kind":"function_graph","title":"坏数据","data":{"x":NaN}}'
    nan = client.post(
        "/api/artifacts", content=nan_body, headers={"content-type": "application/json"}
    )
    assert nan.status_code == 422

    missing_data = client.post(
        "/api/artifacts", json={"kind": "function_graph", "title": "缺内容"}
    )
    assert missing_data.status_code == 422

    long_title = _create(client, title="题" * 121)
    assert long_title.status_code == 422
    assert "标题最多 120 个字符" in long_title.json()["detail"]
    assert _create(client, title="   ").status_code == 422

    artifact_id = _aid(_created(client))
    assert client.get("/api/artifacts", params={"kind": "video"}).status_code == 422
    assert client.get(
        "/api/artifacts/by-target", params={"target_kind": "paper", "target_key": "p-1"}
    ).status_code == 422
    assert client.get("/api/artifacts/by-target").status_code == 422
    assert client.patch(
        f"/api/artifacts/{artifact_id}",
        json={"data": {"v": 2}, "expected_revision": 0},
    ).status_code == 422


def test_size_limits_and_link_limit(artifacts):
    """data >256 KiB / preview >64 KiB → 413；关联目标 >20 → 422；未超限可正常创建。"""
    client, _holder = artifacts

    oversized_data = _create(client, data={"blob": "x" * (256 * 1024)})
    assert oversized_data.status_code == 413
    assert "256 KiB" in oversized_data.json()["detail"]

    oversized_preview = _create(client, preview={"blob": "y" * (64 * 1024)})
    assert oversized_preview.status_code == 413
    assert "64 KiB" in oversized_preview.json()["detail"]

    # 未超上限的大数据可正常保存（256 KiB 以内）。
    assert _create(client, data={"pad": "z" * (200 * 1024)}).status_code == 201

    many_links = [
        {"target_kind": "question", "target_key": f"q-many-{index}"} for index in range(21)
    ]
    over = _create(client, links=many_links)
    assert over.status_code == 422
    assert "最多关联 20 个目标" in over.json()["detail"]

    exact = _created(client, links=many_links[:20])
    artifact_id = _aid(exact)
    extra = client.post(
        f"/api/artifacts/{artifact_id}/links",
        json={"target_kind": "question", "target_key": "q-many-extra"},
    )
    assert extra.status_code == 422
    assert "最多关联 20 个目标" in extra.json()["detail"]


def test_duplicate_artifact_id_is_409(artifacts):
    """调用方指定 artifact_id 重复（含已软删除）→ 409；服务端生成的 ID 互不重复。"""
    client, _holder = artifacts
    first = _created(client, artifact_id="shared-artifact-1")
    assert _aid(first) == "shared-artifact-1"
    duplicate = _create(client, artifact_id="shared-artifact-1")
    assert duplicate.status_code == 409
    assert "附件 ID 已存在" in duplicate.json()["detail"]

    generated = {_aid(_created(client)) for _ in range(3)}
    assert len(generated) == 3


# ---------------------------------------------------------------------------
# 列表分页与过滤
# ---------------------------------------------------------------------------


def test_list_pagination_and_filters(artifacts):
    """列表返回公开元数据、按用户隔离，支持类型过滤、目标过滤与分页。"""
    client, _holder = artifacts
    graph = _created(client, kind="function_graph", title="函数图", data={"v": "graph"})
    board = _created(client, kind="whiteboard", title="白板", data={"v": "board"})
    excerpt = _created(client, kind="reading_excerpt", title="摘录", data={"v": "excerpt"})
    graph_id, board_id, excerpt_id = _aid(graph), _aid(board), _aid(excerpt)

    linked = client.post(
        f"/api/artifacts/{board_id}/links",
        json={"target_kind": "note", "target_key": "note-42"},
    )
    assert linked.status_code == 200

    first_page = client.get("/api/artifacts", params={"page": 1, "page_size": 2}).json()
    second_page = client.get("/api/artifacts", params={"page": 2, "page_size": 2}).json()
    assert first_page["total"] == 3 and second_page["total"] == 3
    assert first_page["page"] == 1 and first_page["page_size"] == 2
    assert len(first_page["items"]) == 2 and len(second_page["items"]) == 1
    all_ids = {item["artifact_id"] for item in first_page["items"] + second_page["items"]}
    assert all_ids == {graph_id, board_id, excerpt_id}
    assert all("data" not in item for item in first_page["items"])

    beyond = client.get("/api/artifacts", params={"page": 5, "page_size": 2}).json()
    assert beyond["items"] == [] and beyond["total"] == 3

    by_kind = client.get("/api/artifacts", params={"kind": "whiteboard"}).json()
    assert by_kind["total"] == 1
    assert by_kind["items"][0]["artifact_id"] == board_id

    by_target = client.get(
        "/api/artifacts", params={"target_kind": "note", "target_key": "note-42"}
    ).json()
    assert by_target["total"] == 1
    assert by_target["items"][0]["artifact_id"] == board_id

    kind_only = client.get("/api/artifacts", params={"target_kind": "note"}).json()
    assert kind_only["total"] == 1

    none_match = client.get(
        "/api/artifacts", params={"target_kind": "question", "target_key": "q-none"}
    ).json()
    assert none_match["total"] == 0 and none_match["items"] == []

    assert client.get("/api/artifacts", params={"page": 0}).status_code == 422
    assert client.get("/api/artifacts", params={"page_size": 0}).status_code == 422
    assert client.get("/api/artifacts", params={"page_size": 101}).status_code == 422


# ---------------------------------------------------------------------------
# 直接函数调用（题目/笔记/课堂/复习接线口径）
# ---------------------------------------------------------------------------


def test_direct_function_api_and_shared_connection(isolated_db):
    """公开函数签名可直接调用，``con`` 参数复用调用方事务（供接线方多步写入）。"""
    study_artifact.init_study_artifacts()
    created = study_artifact.create_artifact(
        USER_A["id"],
        kind="whiteboard",
        title="解题白板",
        data={"strokes": [{"x": 1, "y": 2}], "caption": "草稿"},
        preview={"label": "白板预览"},
        links=[{"target_kind": "note", "target_key": "note-direct"}],
    )
    artifact_id = created["artifact"]["artifact_id"]
    assert created["revision"]["revision"] == 1

    with db.connection() as con:
        read_back = study_artifact.get_artifact(USER_A["id"], artifact_id, con=con)
        assert read_back["revision"]["data"] == {"strokes": [{"x": 1, "y": 2}], "caption": "草稿"}
        second = study_artifact.create_artifact(
            USER_A["id"],
            kind="reading_excerpt",
            title="阅读摘录",
            data={"text": "摘录内容"},
            con=con,
        )
        third = study_artifact.create_artifact(
            USER_A["id"],
            kind="isometric",
            title="等距结构",
            data={"parts": []},
            con=con,
        )
        added = study_artifact.add_revision(
            USER_A["id"],
            artifact_id,
            data={"strokes": [{"x": 3, "y": 4}]},
            expected_revision=1,
            con=con,
        )
        listed = study_artifact.list_artifacts(USER_A["id"], con=con)
    assert second["artifact"]["kind"] == "reading_excerpt"
    assert third["artifact"]["kind"] == "isometric"
    assert added["revision"]["revision"] == 2
    assert listed["total"] == 3

    target = study_artifact.artifacts_for_target(USER_A["id"], "note", "note-direct")
    assert target["total"] == 1
    assert target["items"][0]["revision"] == 2
    assert target["items"][0]["data"] == {"strokes": [{"x": 3, "y": 4}]}
    assert target["skipped"] == 0

    link = study_artifact.link_artifact(
        USER_A["id"], artifact_id, "review", "review-direct", revision=1
    )
    assert link["link"]["revision"] == 1
    unlinked = study_artifact.unlink_artifact(
        USER_A["id"], artifact_id, "review", "review-direct"
    )
    assert unlinked["deleted"] is True

    with pytest.raises(HTTPException) as cross_user:
        study_artifact.get_artifact(USER_B["id"], artifact_id)
    assert cross_user.value.status_code == 404
    assert "附件不存在或无权访问" in str(cross_user.value.detail)
