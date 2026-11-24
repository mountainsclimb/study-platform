"""工坊逐页识别块读取/保存（乐观锁）验收。"""
import json

from app.db import connection, utcnow
from app.paperlab import db as paperlab_db


def _seed_page(user_id: int, blocks=None) -> int:
    now = utcnow()
    with connection() as con:
        cursor = con.execute(
            "INSERT INTO pl_imports(user_id,title,status,page_count,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?)",
            (user_id, "块校对测试", "ready", 1, now, now),
        )
        import_id = int(cursor.lastrowid)
        con.execute(
            "INSERT INTO pl_pages(user_id,import_id,page_no,width,height,storage_name,"
            "ocr_blocks_json,ocr_blocks_version,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                user_id, import_id, 1, 800, 600, "page.jpg",
                json.dumps(blocks or [], ensure_ascii=False), 0, now, now,
            ),
        )
    return import_id


def _sample_blocks():
    return [
        {
            "id": "b1", "kind": "math", "text": "x^2 + 2x = 0", "latex": "x^2+2x=0",
            "needs_review": True, "order": 1, "page": 1, "confidence": 0.9,
            "bbox": {"x": 10, "y": 10, "w": 100, "h": 24},
        },
        {
            "id": "b2", "kind": "table", "text": "A | B",
            "table_html": "<table><tr><td>1</td><td>2</td></tr></table>",
            "row_count": 1, "col_count": 2, "order": 2, "page": 1,
        },
    ]


def test_get_and_save_page_blocks_with_optimistic_lock(client):
    user_id = client.get("/api/auth/me").json()["id"]
    import_id = _seed_page(user_id, _sample_blocks())

    fetched = client.get(f"/api/paperlab/imports/{import_id}/pages/1/blocks")
    assert fetched.status_code == 200, fetched.text
    payload = fetched.json()
    assert payload["version"] == 0
    assert payload["ocr_block_count"] == 2
    assert payload["blocks"][0]["latex"] == "x^2+2x=0"
    assert payload["blocks"][1]["table_html"].startswith("<table>")

    edited = _sample_blocks()
    edited[0]["latex"] = "x^2+2x=1"
    edited[0]["needs_review"] = False
    saved = client.put(
        f"/api/paperlab/imports/{import_id}/pages/1/blocks",
        json={"blocks": edited, "expected_version": 0},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["version"] == 1
    assert saved.json()["blocks"][0]["latex"] == "x^2+2x=1"
    assert saved.json()["blocks"][0]["needs_review"] is False

    stale = client.put(
        f"/api/paperlab/imports/{import_id}/pages/1/blocks",
        json={"blocks": edited, "expected_version": 0},
    )
    assert stale.status_code == 409
    assert "重新载入" in stale.json()["detail"]

    again = client.get(f"/api/paperlab/imports/{import_id}/pages/1/blocks").json()
    assert again["version"] == 1
    assert again["blocks"][0]["latex"] == "x^2+2x=1"


def test_save_page_blocks_sanitizes_table_html(client):
    user_id = client.get("/api/auth/me").json()["id"]
    import_id = _seed_page(user_id, [])
    blocks = [{
        "id": "b1", "kind": "table", "text": "A",
        "table_html": '<table><tr><td onclick="x()">1</td></tr></table><script>x</script>',
        "order": 1, "page": 1,
    }]
    response = client.put(
        f"/api/paperlab/imports/{import_id}/pages/1/blocks",
        json={"blocks": blocks, "expected_version": 0},
    )
    assert response.status_code == 200, response.text
    html = response.json()["blocks"][0]["table_html"]
    assert "<script" not in html and "onclick" not in html
    assert "<table>" in html


def test_page_blocks_are_owner_scoped(client, second_client):
    user_id = client.get("/api/auth/me").json()["id"]
    import_id = _seed_page(user_id, _sample_blocks())
    assert client.get(
        f"/api/paperlab/imports/{import_id}/pages/1/blocks"
    ).status_code == 200
    assert second_client.get(
        f"/api/paperlab/imports/{import_id}/pages/1/blocks"
    ).status_code == 404


def test_public_page_reports_blocks_version(client):
    user_id = client.get("/api/auth/me").json()["id"]
    import_id = _seed_page(user_id, _sample_blocks())
    detail = client.get(f"/api/paperlab/imports/{import_id}").json()
    page = detail["pages"][0]
    assert page["has_blocks"] is True
    assert page["ocr_block_count"] == 2
    assert page["ocr_blocks_version"] == 0
    assert "blocks" not in page  # 列表不携带完整块，避免页面列表过大
